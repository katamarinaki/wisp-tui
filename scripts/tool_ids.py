#!/usr/bin/env python3
"""Diagnose the "Result not in stored history" gap: which side loses tool results?

Compares the tool-call IDs the runtime sends in live run events with the IDs
stored in the session history, so the fix lands on the right side:

- live ``tool_call`` events without a ``toolCallId`` ............ Wisp side
- live IDs that never appear as stored AI call/result IDs ........ Wisp side
- live IDs matched to AI calls but with no stored result ......... Wisp side
- everything matches yet the TUI still shows the notice ...... wisp-tui side

Privacy: prints only session/run IDs, tool names, call IDs, ``ok`` flags and
durations/counts — never prompts, answers, arguments or result content.
Read-only: GET requests and one SSE attachment; never submits a run.

Usage (project venv, private config only):

  scripts/tool_ids.py --list                     saved local sessions
  scripts/tool_ids.py [SESSION_ID_OR_PREFIX]     analyze stored history
  scripts/tool_ids.py --watch [--session ID]     follow the next live run
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any

from wisp_tui import config
from wisp_tui.client import UUID, ClientError, WispClient
from wisp_tui.history import HistoryError, parse_history
from wisp_tui.runs import LIVE_STATUSES, find_active_run
from wisp_tui.sessions import SessionStore

WATCH_POLL_SECONDS = 3.0
WATCH_WAIT_MINUTES = 60.0
STREAM_TIMEOUT_SECONDS = 20.0


@dataclass
class LiveTool:
    name: str
    call_id: str = ""
    ok: bool | None = None
    duration_ms: float | None = None


@dataclass
class HistoryReport:
    ai_calls: list[tuple[str, str]] = field(default_factory=list)  # (call_id, name)
    results: list[tuple[str, str, str]] = field(default_factory=list)  # (id, name, status)
    empty_id_calls: list[str] = field(default_factory=list)
    duplicate_ids: set[str] = field(default_factory=set)

    @property
    def ai_ids(self) -> set[str]:
        return {call_id for call_id, _ in self.ai_calls if call_id}

    @property
    def result_ids(self) -> set[str]:
        return {call_id for call_id, _, _ in self.results}


def analyze_history(messages: list[Any]) -> HistoryReport:
    report = HistoryReport()
    seen_calls: set[str] = set()
    seen_results: set[str] = set()
    for message in messages:
        for call in message.tool_calls:
            report.ai_calls.append((call.call_id, call.name))
            if not call.call_id:
                report.empty_id_calls.append(call.name)
            elif call.call_id in seen_calls:
                report.duplicate_ids.add(call.call_id)
            seen_calls.add(call.call_id)
        if message.role == "tool":
            report.results.append((message.tool_call_id, message.tool_name, message.tool_status))
            if message.tool_call_id and message.tool_call_id in seen_results:
                report.duplicate_ids.add(message.tool_call_id)
            seen_results.add(message.tool_call_id)
    return report


def print_history_report(report: HistoryReport) -> None:
    matched = report.ai_ids & report.result_ids
    missing_results = report.ai_ids - report.result_ids
    orphan_results = report.result_ids - report.ai_ids
    no_id_results = [name for call_id, name, _ in report.results if not call_id]
    print(f"  stored AI tool calls: {len(report.ai_calls)}")
    print(f"  stored tool results:  {len(report.results)}")
    print(f"  matched by call ID:   {len(matched)}")
    for call_id, name in report.ai_calls:
        state = "matched" if call_id in matched else "NO STORED RESULT"
        shown = call_id or "(no id)"
        print(f"    call {shown}  {name}  -> {state}")
    for call_id, name, status in report.results:
        if not call_id:
            print(f"    result (no id)  {name}  status={status or '?'}")
        elif call_id in orphan_results:
            print(f"    result {call_id}  {name}  -> NO MATCHING AI CALL")
    if report.empty_id_calls:
        print(f"  AI calls with empty/missing id: {len(report.empty_id_calls)}")
    if no_id_results:
        print(f"  results with empty/missing id: {len(no_id_results)}")
    if report.duplicate_ids:
        print(f"  duplicate ids (ambiguous join): {sorted(report.duplicate_ids)}")
    if missing_results:
        print("  verdict: the runtime stores AI calls whose results never appear")
        print("           -> the gap is on the Wisp/runtime side.")


def fetch_history(client: WispClient, session_id: str) -> list[Any]:
    payload = client.request("GET", f"/v1/sessions/{session_id}/history", timeout=20.0)
    return parse_history(payload)


def resolve_session(prefix: str | None) -> str | None:
    store = SessionStore()
    if prefix:
        matches = store.find(prefix)
        if not matches:
            print(f"no saved session matches {prefix!r}", file=sys.stderr)
            return None
        if len(matches) > 1:
            print(f"ambiguous session prefix {prefix!r}:", file=sys.stderr)
            for session_id in matches:
                print(f"  {session_id}", file=sys.stderr)
            return None
        return matches[0]
    return store.latest()


def list_sessions() -> int:
    for summary in SessionStore().recent():
        print(f"{summary.id}  {time.strftime('%Y-%m-%d %H:%M', time.localtime(summary.updated))}")
    return 0


def _wait_for_run(client: WispClient, session_id: str) -> str | None:
    """Poll the session's run listing until a live run appears (bounded wait)."""
    deadline = time.monotonic() + WATCH_WAIT_MINUTES * 60.0
    while True:
        payload = client.request(
            "GET",
            f"/v1/runs?sessionId={session_id}&status={','.join(LIVE_STATUSES)}&limit=200",
            timeout=10.0,
        )
        run_id = find_active_run(payload, session_id)
        if run_id:
            return run_id
        if time.monotonic() >= deadline:
            return None
        time.sleep(WATCH_POLL_SECONDS)


def _record_tool_event(live: list[LiveTool], event: dict[str, Any]) -> None:
    """Merge one tool event into the live list; ids/names only, never content."""
    kind = event.get("kind")
    name = str(event.get("name", "?"))
    call_id = str(event.get("toolCallId") or "")
    if kind == "tool_call.started":
        live.append(LiveTool(name, call_id))
        return
    duration = event.get("durationMs")
    tool = LiveTool(
        name,
        call_id,
        event.get("ok") is True,
        duration if isinstance(duration, (int, float)) else None,
    )
    previous = live[-1] if live else None
    if previous is not None and previous.call_id == "" and previous.name == name:
        live[-1] = tool  # completion of the just-started call
    else:
        live.append(tool)


def _print_tool_event(event: dict[str, Any]) -> None:
    kind = event.get("kind")
    if kind in ("tool_call.started", "tool_call"):
        suffix = f"  ok={event.get('ok')}" if kind == "tool_call" else ""
        shown = event.get("toolCallId") or "(none)"
        print(f"  event {kind}  {event.get('name')}  id={shown}{suffix}")
    elif kind in ("run.completed", "run.failed", "run.cancelled"):
        print(f"  event {kind}")


def _wait_for_any_run(client: WispClient) -> tuple[str, str] | None:
    """Poll the global run listing until any live run appears (bounded wait).

    Returns ``(run_id, session_id)``; prefers an executing/parked run over
    newer queued work, mirroring ``find_active_run``'s choice.
    """
    deadline = time.monotonic() + WATCH_WAIT_MINUTES * 60.0
    while True:
        payload = client.request(
            "GET", f"/v1/runs?status={','.join(LIVE_STATUSES)}&limit=200", timeout=10.0
        )
        executing: list[tuple[str, str]] = []
        queued: list[tuple[str, str]] = []
        records = payload.get("runs") if isinstance(payload, dict) else None
        if isinstance(records, list):
            for record in records:
                if not isinstance(record, dict):
                    continue
                run_id = record.get("runId")
                session_id = record.get("sessionId")
                status = record.get("status")
                if (
                    isinstance(run_id, str)
                    and re.fullmatch(UUID, run_id)
                    and isinstance(session_id, str)
                    and re.fullmatch(UUID, session_id)
                    and status in LIVE_STATUSES
                ):
                    pair = (run_id, session_id)
                    if status == "queued":
                        queued.append(pair)
                    else:
                        executing.append(pair)
        if executing:
            return executing[0]
        if queued:
            return queued[-1]
        if time.monotonic() >= deadline:
            return None
        time.sleep(WATCH_POLL_SECONDS)


def _run_status(client: WispClient, run_id: str) -> Any:
    record = client.request("GET", f"/v1/runs/{run_id}", timeout=10.0)
    return record.get("status") if isinstance(record, dict) else None


def _compare_after_run(client: WispClient, session_id: str, live: list[LiveTool]) -> int:
    """Fetch stored history after the run and compare it with the live ids."""
    no_id = [tool.name for tool in live if not tool.call_id]
    if no_id:
        print(f"  live events WITHOUT toolCallId: {len(no_id)} -> Wisp side (TUI cannot join)")
    live_ids = {tool.call_id for tool in live if tool.call_id}
    # Detect a checkpoint-commit race: the TUI's run-end read may precede
    # the history write, so poll a few times before concluding.
    report = None
    first_read_matched = False
    for attempt in range(4):
        try:
            report = analyze_history(fetch_history(client, session_id))
        except (ClientError, HistoryError) as error:
            print(f"history unavailable: {error}", file=sys.stderr)
            return 1
        matched = len(live_ids & report.result_ids)
        total = len(live_ids)
        print(f"  history read #{attempt + 1}: {matched}/{total} live ids have stored results")
        if attempt == 0:
            first_read_matched = not live_ids or matched == total
        if not live_ids or matched == total:
            break
        time.sleep(3.0)
    assert report is not None
    print("stored history after the run:")
    print_history_report(report)
    unknown = live_ids - report.ai_ids - report.result_ids
    if unknown:
        print(f"  live IDs absent from stored history: {sorted(unknown)} -> Wisp side")
    matched_live = live_ids & report.result_ids
    if live and not no_id and not unknown and len(matched_live) == len(live_ids):
        if first_read_matched:
            print("  verdict: live IDs matched stored results immediately -> any")
            print("           'Result not in stored history' notice is a wisp-tui bug;")
            print("           re-run the TUI on this session to confirm.")
        else:
            print("  verdict: results appear only seconds after run end -> Wisp-side")
            print("           checkpoint commit lag; the TUI's run-end read races it.")
    return 0


def watch(client: WispClient, session_id: str, run_id: str | None) -> int:
    if run_id is None:
        run_id = _wait_for_run(client, session_id)
        if run_id is None:
            print("no active run within the wait window", file=sys.stderr)
            return 1
    print(f"session {session_id}")
    print(f"run     {run_id}")
    live: list[LiveTool] = []
    last_seq: int | None = None
    while (status := _run_status(client, run_id)) in LIVE_STATUSES:
        try:
            for event in client.iter_events(run_id, last_seq, STREAM_TIMEOUT_SECONDS):
                seq = event.get("seq")
                last_seq = seq if isinstance(seq, int) else last_seq
                _print_tool_event(event)
                if event.get("kind") in ("tool_call.started", "tool_call"):
                    _record_tool_event(live, event)
        except ClientError as error:
            if str(error) != "event_stream_timeout":
                raise
            # Long quiet tool calls are normal; re-read the run record.
            continue
    print(f"run status: {status}")
    print(f"live tool events: {len(live)}")
    return _compare_after_run(client, session_id, live)


def _watch_main(client: WispClient, args: argparse.Namespace) -> int:
    if args.run is not None and args.session is None:
        record = client.request("GET", f"/v1/runs/{args.run}", timeout=10.0)
        session_id = record.get("sessionId") if isinstance(record, dict) else None
        if not isinstance(session_id, str):
            print("run record unavailable", file=sys.stderr)
            return 1
        return watch(client, session_id, args.run)
    if args.session is None:
        found = _wait_for_any_run(client)
        if found is None:
            print("no active run within the wait window", file=sys.stderr)
            return 1
        return watch(client, found[1], found[0])
    session_id = resolve_session(args.session)
    if session_id is None:
        print("no saved session (send a message in the TUI first)", file=sys.stderr)
        return 1
    return watch(client, session_id, args.run)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("session", nargs="?", help="session ID or prefix (default: latest)")
    parser.add_argument("--list", action="store_true", help="list saved local sessions")
    parser.add_argument("--watch", action="store_true", help="follow the session's live run")
    parser.add_argument("--run", help="attach to this run ID instead of discovering one")
    args = parser.parse_args(argv)
    if args.list:
        return list_sessions()
    try:
        settings = config.load_config()
    except config.ConfigError as error:
        print(f"config error: {error}", file=sys.stderr)
        return 1
    client = WispClient(settings.url, settings.token)
    if args.watch:
        return _watch_main(client, args)
    session_id = resolve_session(args.session)
    if session_id is None:
        print("no saved session (send a message in the TUI first)", file=sys.stderr)
        return 1
    print(f"session {session_id}")
    try:
        messages = fetch_history(client, session_id)
    except (ClientError, HistoryError) as error:
        print(f"history unavailable: {error}", file=sys.stderr)
        return 1
    print_history_report(analyze_history(messages))
    return 0


if __name__ == "__main__":
    sys.path.insert(0, "src")
    raise SystemExit(main())
