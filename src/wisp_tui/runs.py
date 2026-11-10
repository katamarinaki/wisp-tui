"""A session's runs as the runtime reports them, and a single-owner run worker.

A dropped stream is never proof of completion. Read the persisted run record
before following/reconnecting, and poll parked records for current prompts.
Callbacks are marshalled to the UI by the caller; this module has no widgets.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from threading import Event
from typing import Any, NamedTuple

from .client import PROMPT_ID, UUID, ClientError, WispClient
from .history import ChatMessage, parse_history

LIVE_STATUSES = ("queued", "running", "parked")
TERMINAL_STATUSES = ("completed", "failed", "cancelled")
TERMINAL_EVENTS = tuple(f"run.{status}" for status in TERMINAL_STATUSES)


def live_runs_path(session_id: str | None = None) -> str:
    session = f"sessionId={session_id}&" if session_id else ""
    return f"/v1/runs?{session}status={','.join(LIVE_STATUSES)}&limit=200"


class Confirmation(NamedTuple):
    """A prompt a parked run waits on, queued for the approval dialog."""

    run_id: str
    prompt_id: str
    tool_name: str
    args: Any
    note: str


def confirmations(record: dict[str, Any]) -> list[Confirmation]:
    """The current prompts of a record that passed ``validate_run``."""
    if record["status"] != "parked":
        return []
    return [
        Confirmation(
            record["runId"],
            prompt["id"],
            prompt["toolName"],
            prompt.get("args"),
            " · ".join(
                note
                for applies, note in (
                    (prompt.get("askedEveryTime") is True, "asked every time"),
                    (prompt.get("escalatedBy") == "judge", "escalated by judge"),
                )
                if applies
            ),
        )
        for prompt in record.get("confirmations", [])
    ]


def validate_run(payload: Any, run_id: str, session_id: str) -> dict[str, Any]:
    if (
        not isinstance(payload, dict)
        or payload.get("runId") != run_id
        or payload.get("sessionId") != session_id
        or payload.get("status") not in LIVE_STATUSES + TERMINAL_STATUSES
    ):
        raise ClientError("invalid_run_record")
    if payload["status"] == "parked":
        prompts = payload.get("confirmations", [])
        if not isinstance(prompts, list):
            raise ClientError("invalid_confirmations")
        seen: set[str] = set()
        for prompt in prompts:
            if (
                not isinstance(prompt, dict)
                or not isinstance(prompt.get("id"), str)
                or re.fullmatch(PROMPT_ID, prompt["id"]) is None
                or not isinstance(prompt.get("toolName"), str)
                or prompt["id"] in seen
            ):
                raise ClientError("invalid_confirmations")
            seen.add(prompt["id"])
    return payload


def find_active_run(payload: Any, session_id: str) -> str:
    """The documented listing is an envelope, not a bare list.

    Prefer the executing/parked run to newer queued work. The response is newest
    first, so attach to the oldest queued item when none is executing yet.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("runs"), list):
        raise ClientError("invalid_runs_response")
    executing: list[str] = []
    queued: list[str] = []
    for record in payload["runs"]:
        run_id = record.get("runId") if isinstance(record, dict) else None
        if not isinstance(run_id, str) or re.fullmatch(UUID, run_id) is None:
            raise ClientError("invalid_run_reference")
        status = validate_run(record, run_id, session_id)["status"]
        if status in ("running", "parked"):
            executing.append(run_id)
        elif status == "queued":
            queued.append(run_id)
    if len(executing) > 1:
        raise ClientError("ambiguous_active_runs")
    # Never enable input on the assumption that an incomplete listing is empty.
    if payload.get("nextCursor") is not None:
        raise ClientError("incomplete_runs_response")
    if executing:
        return executing[0]
    return queued[-1] if queued else ""


class SessionState(NamedTuple):
    session: dict[str, Any]
    messages: list[ChatMessage]
    active_run: str


def load_history(client: WispClient, session_id: str, timeout: float = 10.0) -> list[ChatMessage]:
    return parse_history(
        client.request("GET", f"/v1/sessions/{session_id}/history", timeout=timeout)
    )


def load_session(client: WispClient, session_id: str, timeout: float = 10.0) -> SessionState:
    """The session, its history and its active run (\"\" when none)."""
    session = client.request("GET", f"/v1/sessions/{session_id}", timeout=timeout)
    if not isinstance(session, dict) or session.get("id") != session_id:
        raise ClientError("invalid_session")
    # Discover first: a run that finishes while history loads still has a
    # known ID to reconcile, rather than disappearing from discovery.
    runs = client.request("GET", live_runs_path(session_id), timeout=timeout)
    active_run = find_active_run(runs, session_id)
    return SessionState(session, load_history(client, session_id, timeout), active_run)


class RunFollower:
    """One worker owns both SSE and parked-run polling for a single run.

    Retry/poll waits wake immediately on stop or an explicit user action. An
    outstanding HTTP read is bounded by READ_TIMEOUT (not a cancellable socket).
    A read timeout is *not* a failure: tools may run silently for minutes, so
    the worker re-reads the run record and reopens the stream from last_seq.
    Only real failures (unreachable runtime, bad responses, unexpected EOF)
    count. After repeated failures, stop reconnecting but leave the UI
    attached; its explicit reconnect action creates a replacement follower.
    """

    POLL_INTERVAL = 3.0
    RETRY_INTERVAL = 1.0
    MAX_FAILURES = 5
    READ_TIMEOUT = 20.0

    def __init__(
        self,
        client: WispClient,
        run_id: str,
        session_id: str,
        *,
        last_seq: int | None,
        on_event: Callable[[dict[str, Any]], None],
        on_snapshot: Callable[[dict[str, Any]], None],
        on_error: Callable[[str], None],
    ) -> None:
        self.client = client
        self.run_id = run_id
        self.session_id = session_id
        self.last_seq = last_seq
        self.on_event = on_event
        self.on_snapshot = on_snapshot
        self.on_error = on_error
        self.stopped = Event()
        self.wake = Event()
        self._failures = 0

    def stop(self) -> None:
        self.stopped.set()
        self.wake.set()

    def _wait(self, seconds: float) -> None:
        self.wake.wait(seconds)
        self.wake.clear()

    def run(self) -> None:
        self._failures = 0
        while not self.stopped.is_set():
            try:
                snapshot = validate_run(
                    self.client.request("GET", f"/v1/runs/{self.run_id}", timeout=5.0),
                    self.run_id,
                    self.session_id,
                )
                if self.stopped.is_set():
                    return
                self.on_snapshot(snapshot)
                status = snapshot["status"]
                if status in TERMINAL_STATUSES or self.stopped.is_set():
                    return
                if status == "parked":
                    self._failures = 0
                    self._wait(self.POLL_INTERVAL)
                    continue
                outcome = self._follow_stream()
                if outcome == "done":
                    return
                if outcome in ("reconcile", "quiet"):
                    continue
                raise ClientError("event_stream_ended")
            except ClientError as error:
                code = str(error)
            except Exception:
                code = "internal_error"
            if self.stopped.is_set():
                return
            self._failures += 1
            self.on_error(code)
            if self._failures >= self.MAX_FAILURES:
                self.on_error("stream_retry_exhausted")
                return
            self._wait(self.RETRY_INTERVAL)

    def _follow_stream(self) -> str:
        """Consume one SSE connection.

        Returns ``done`` (terminal event or stopped), ``reconcile`` (re-read the
        run record first), ``quiet`` (read timeout without events) or ``ended``
        (unexpected EOF). Other stream errors propagate as failures.
        """
        stream = self.client.iter_events(self.run_id, self.last_seq, self.READ_TIMEOUT)
        try:
            for event in stream:
                if self.stopped.is_set():
                    return "done"
                seq = event.get("seq")
                if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
                    raise ClientError("invalid_event_sequence")
                if self.last_seq is not None and seq <= self.last_seq:
                    continue
                self.last_seq = seq
                self.on_event(event)
                self._failures = 0
                kind = event.get("kind")
                if kind in TERMINAL_EVENTS:
                    return "done"
                # Prompts from replay may already be resolved. Only a fresh
                # snapshot may open an actionable approval dialog.
                if kind in ("run.parked", "confirmation.required", "gap"):
                    return "reconcile"
        except ClientError as error:
            if str(error) != "event_stream_timeout":
                raise
            # Silence is not a disconnect: long tool calls can be quiet for
            # minutes. The connection worked, so this is not a failure; the
            # next run-record read proves whether the run is still alive.
            self._failures = 0
            return "quiet"
        finally:
            close = getattr(stream, "close", None)
            if close is not None:
                close()
        return "ended"
