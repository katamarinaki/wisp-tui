"""UI recovery and approval reconciliation against the scripted runtime."""
from __future__ import annotations

from collections import deque
from dataclasses import replace
from typing import Any

import pytest
from textual.widgets import Input

from tests.helpers import RID, SID, RequestGate
from tests.test_app import chat_text, eventually, make_app
from tests.test_runs import record
from wisp_tui.client import ClientError
from wisp_tui.runs import RunFollower

RUN_PATH = f"/v1/runs/{RID}"
LIST_PATH = f"/v1/runs?sessionId={SID}&status=queued,running,parked&limit=200"
PROMPT = {"id": "prompt-1", "toolName": "example", "args": {"target": "example"}}
EVENTS: list[dict[str, Any]] = [
    {"seq": 1, "kind": "confirmation.required", **PROMPT},
    {"seq": 2, "kind": "run.completed", "outcome": {"output": {"text": "done"}}},
]


@pytest.fixture(autouse=True)
def fast_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(RunFollower, "POLL_INTERVAL", 0.02)
    monkeypatch.setattr(RunFollower, "RETRY_INTERVAL", 0.02)


@pytest.mark.parametrize("status", ["queued", "running", "parked"])
async def test_reopen_reattaches_then_reconciles_current_terminal_state(
        monkeypatch: pytest.MonkeyPatch, status: str) -> None:
    app, client = make_app([], monkeypatch)
    client.replies[("GET", LIST_PATH)] = deque([
        {"runs": [record(status)], "nextCursor": None}])
    client.replies[("GET", RUN_PATH)] = deque([
        record("completed", output={"text": "finished while disconnected"})])
    gate = RequestGate("GET", RUN_PATH)
    client.request_hook = gate
    async with app.run_test():
        await eventually(lambda: "ready" in app._status_state)
        try:
            app._open_session(SID)
            await eventually(gate.entered.is_set)
            assert app.active_run == RID
            assert app.session_id == SID
            assert not any(method == "POST" for method, _, _ in client.calls)
        finally:
            gate.release.set()
        await eventually(lambda: app._status_state == "")
        assert not bool(app.active_run)
        assert "finished while disconnected" in chat_text(app)
        assert not client.event_requests


async def test_disconnection_keeps_run_cursor_and_blocks_new_submission(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(RunFollower, "MAX_FAILURES", 1)
    app, client = make_app([{"seq": 1, "kind": "text.delta", "text": "partial"}], monkeypatch)
    async with app.run_test() as pilot:
        await eventually(lambda: "ready" in app._status_state)
        app.session_id = SID
        app._turn_started(RID)
        app._start_follow()
        await eventually(lambda: app._follower is None)
        assert "disconnected" in app._status_state
        assert app.active_run == RID
        assert app.last_seq[RID] == 1
        editor = app.query_one("#input", Input)
        editor.value = "do not send twice"
        await pilot.press("enter")
        assert editor.value == "do not send twice"
        assert not any(method == "POST" for method, _, _ in client.calls)
        client.replies[("GET", RUN_PATH)] = deque([
            record("completed", output={"text": "final answer"})])
        app._handle_command("/reconnect")
        await eventually(lambda: app._status_state == "")
        assert "final answer" in chat_text(app)
        assert editor.value == "do not send twice"
        assert not bool(app.active_run)


async def test_external_approval_closes_open_dialog_without_posting(
        monkeypatch: pytest.MonkeyPatch) -> None:
    app, client = make_app(EVENTS, monkeypatch)
    client.run_status = "parked"
    async with app.run_test():
        await eventually(lambda: "ready" in app._status_state)
        app.session_id = SID
        app._turn_started(RID)
        app._start_follow()
        await eventually(lambda: app._approval_prompt_id == "prompt-1")
        client.answered_prompts.add("prompt-1")
        client.run_status = "running"
        app._start_follow()  # wakes the existing follower, does not create another
        await eventually(lambda: app._status_state == "")
        await eventually(lambda: len(app.screen_stack) == 1)
        assert not any("/confirmations/" in path for _, path, _ in client.calls)
        assert "done" in chat_text(app)


async def test_multiple_prompts_do_not_start_competing_followers(
        monkeypatch: pytest.MonkeyPatch) -> None:
    events = [EVENTS[0], {"seq": 2, "kind": "confirmation.required",
                         **PROMPT, "id": "prompt-2"},
              {"seq": 3, "kind": "run.completed", "outcome": {}}]
    app, client = make_app(events, monkeypatch)
    client.run_status = "parked"
    gate = RequestGate("POST", RUN_PATH + "/confirmations/prompt-1")
    client.request_hook = gate
    async with app.run_test() as pilot:
        await eventually(lambda: "ready" in app._status_state)
        app.session_id = SID
        app._turn_started(RID)
        app._start_follow()
        owner = app._follower
        try:
            await eventually(lambda: app._approval_prompt_id == "prompt-1")
            await pilot.press("y")
            await eventually(gate.entered.is_set)
            await eventually(lambda: app._approval_prompt_id == "prompt-2")
            await pilot.press("n")
            await eventually(lambda: "prompt-2" in client.answered_prompts)
            app._start_follow()
            app._start_follow()
            assert app._follower is owner
            assert not client.event_requests  # parked polling, not parallel SSE
        finally:
            gate.release.set()
        await eventually(lambda: app._status_state == "")
        confirms = [body for _, path, body in client.calls if "/confirmations/" in path]
        assert confirms == [{"decision": "approve", "wait": False},
                            {"decision": "deny", "wait": False}]


async def test_failed_approval_is_reconciled_not_automatically_reposted(
        monkeypatch: pytest.MonkeyPatch) -> None:
    app, client = make_app(EVENTS, monkeypatch)
    client.run_status = "parked"
    path = RUN_PATH + "/confirmations/prompt-1"
    client.replies[("POST", path)] = deque([ClientError("runtime_timeout")])
    async with app.run_test() as pilot:
        await eventually(lambda: "ready" in app._status_state)
        app.session_id = SID
        app._turn_started(RID)
        app._start_follow()
        await eventually(lambda: app._approval_prompt_id == "prompt-1")
        await pilot.press("y")
        await eventually(lambda: "approval error" in chat_text(app))
        await eventually(lambda: app._approval_prompt_id == "prompt-1")
        assert len([c for c in client.calls if c[0] == "POST"]) == 1
        await pilot.press("escape")
        await eventually(lambda: app._status_state == "")
        posts = [body for method, _, body in client.calls if method == "POST"]
        assert posts[-1] == {"decision": "deny", "wait": False}


async def test_already_resolved_approval_404_reconciles_terminal_record(
        monkeypatch: pytest.MonkeyPatch) -> None:
    app, client = make_app(EVENTS, monkeypatch)
    client.run_status = "parked"
    path = RUN_PATH + "/confirmations/prompt-1"
    client.replies[("POST", path)] = deque([ClientError("runtime_http_404")])
    # Keep the parked snapshot stable until the explicit decision races a
    # remote resolution; pause polling by using a long, interruptible wait.
    monkeypatch.setattr(RunFollower, "POLL_INTERVAL", 60.0)
    async with app.run_test() as pilot:
        await eventually(lambda: "ready" in app._status_state)
        app.session_id = SID
        app._turn_started(RID)
        app._start_follow()
        await eventually(lambda: app._approval_prompt_id == "prompt-1")
        client.replies[("GET", RUN_PATH)] = deque([record("cancelled")])
        await pilot.press("y")
        await eventually(lambda: app._status_state == "run cancelled")
        assert len([c for c in client.calls if c[0] == "POST"]) == 1
        await eventually(lambda: len(app.screen_stack) == 1)


async def test_stale_callbacks_cannot_modify_another_run(monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test():
        await eventually(lambda: "ready" in app._status_state)
        app.session_id = SID
        old = RunFollower(app.client, RID, SID, 600, None,
                          lambda e: None, lambda r: None, lambda e: None)
        app._follower = old
        app._turn_started(SID)  # different run identifier
        app._follow_event(old, {"seq": 1, "kind": "text.delta", "text": "stale text"})
        app._follow_snapshot(old, record("cancelled"))
        app._follow_error(old, "runtime_unreachable")
        app._confirmation_sent(RID, "old-prompt", "runtime_timeout")
        assert app.active_run == SID
        assert "stale text" not in chat_text(app)
        assert "disconnected" not in app._status_state
        old.stop()


async def test_cancel_failure_can_be_retried_without_losing_run(
        monkeypatch: pytest.MonkeyPatch) -> None:
    app, client = make_app([], monkeypatch)
    path = f"/v1/sessions/{SID}/cancel"
    client.replies[("POST", path)] = deque([ClientError("runtime_timeout")])
    async with app.run_test():
        await eventually(lambda: "ready" in app._status_state)
        app.session_id = SID
        app._turn_started(RID)
        app._cancel_run()
        await eventually(lambda: "cancel error" in app._status_state)
        assert app.active_run == RID
        assert not bool(app._cancel_requested)
        app._cancel_run()
        await eventually(lambda: app._status_state == "run cancelled")
        assert len([c for c in client.calls if c[0] == "POST"]) == 2


async def test_cancel_during_submission_waits_for_acknowledgement(
        monkeypatch: pytest.MonkeyPatch) -> None:
    app, client = make_app([], monkeypatch)
    client.run_status = "parked"
    path = f"/v1/sessions/{SID}/runs"
    gate = RequestGate("POST", path)
    client.request_hook = gate
    async with app.run_test() as pilot:
        await eventually(lambda: "ready" in app._status_state)
        app.session_id = SID
        try:
            app.query_one("#input", Input).value = "long task"
            await pilot.press("enter")
            await eventually(gate.entered.is_set)
            app._cancel_run()
            assert app._cancel_after_submit
            assert not any(p.endswith("/cancel") for _, p, _ in client.calls)
        finally:
            gate.release.set()
        await eventually(lambda: app._status_state == "run cancelled")
        posts = [p for m, p, _ in client.calls if m == "POST"]
        assert posts == [path, f"/v1/sessions/{SID}/cancel"]


async def test_gap_snapshot_does_not_truncate_long_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        await eventually(lambda: "ready" in app._status_state)
        app.session_id = SID
        app._turn_started(RID)
        text = "answer " * 800
        app._handle_event({"seq": 10, "kind": "gap", "status": "running", "text": text})
        app._handle_event({"seq": 11, "kind": "text.delta", "text": "end"})
        await pilot.pause()
        assert app._delta_text == text + "end"
        assert text + "end" in chat_text(app)


async def test_old_unacknowledged_submission_is_not_replayed(monkeypatch: pytest.MonkeyPatch) -> None:
    app, client = make_app([], monkeypatch)
    path = f"/v1/sessions/{SID}/runs"
    client.replies[("POST", path)] = deque([ClientError("runtime_timeout")])
    async with app.run_test() as pilot:
        await eventually(lambda: "ready" in app._status_state)
        app.session_id = SID
        app.query_one("#input", Input).value = "original task"
        await pilot.press("enter")
        await eventually(lambda: "submission error" in app._status_state)
        pending = app._pending_submission
        assert pending is not None
        app._pending_submission = replace(pending, created_at=pending.created_at - 24 * 3600)
        app._handle_command("/retry")
        assert app._status_state == "pending_submission_expired"
        assert len([c for c in client.calls if c[0] == "POST"]) == 1
