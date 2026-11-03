"""A recovered request can inspect its session without inferring an acknowledgement."""
from __future__ import annotations

from collections import deque
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Input

from tests.helpers import RID, SID, RequestGate
from tests.test_app import chat_text, eventually, make_app, text_of
from tests.test_runs import record
from tests.test_submissions import COMPLETED, URL, pending
from wisp_tui.app import WispTuiApp
from wisp_tui.client import ClientError
from wisp_tui.history import ChatMessage
from wisp_tui.submissions import SubmissionStore

SESSION = f"/v1/sessions/{SID}"
HISTORY = SESSION + "/history"
LIST = f"/v1/runs?sessionId={SID}&status=queued,running,parked&limit=200"
RUNS = SESSION + "/runs"
OTHER = "af06ed54-82b0-45e6-af55-7c85ab55e7ea"


def seed_pending(root: Path) -> None:
    store = SubmissionStore(URL, root)
    store.prepare(pending())
    store.close()


@pytest.mark.parametrize("active", [False, True])
async def test_recovery_loads_metadata_history_and_runs_without_resubmitting_or_attaching(
        monkeypatch: pytest.MonkeyPatch, private_state_dir: Path, active: bool) -> None:
    seed_pending(private_state_dir)
    app, client = make_app(COMPLETED, monkeypatch)
    client.replies[("GET", LIST)] = deque([
        {"runs": [record("running")] if active else [], "nextCursor": None}])
    async with app.run_test() as pilot:
        await eventually(lambda: app._pending_submission is not None
                         and app._recovery_view_token is None)
        await pilot.pause()
        assert app.session_id == SID
        assert app.session_title == "test chat"
        assert "past question" in chat_text(app) and "past answer" in chat_text(app)
        assert "mode: balanced" in text_of(app.query_one("#session-info"))
        assert "thinking: off" in text_of(app.query_one("#thinking-info"))
        assert app.query_one("#input", Input).value == " original task\nsecond line "
        assert all(method == "GET" for method, _, _ in client.calls)
        assert not client.event_requests
        assert app.active_run is None
        if active:
            assert "It is not proof this request was accepted" in chat_text(app)
        else:
            assert "not proof the request was rejected" in chat_text(app)
        app._new_session("blocked")
        app._model_answered({"mode": "advanced"}, SID)
        assert app._pending_submission is not None
        assert all(method == "GET" for method, _, _ in client.calls)


async def test_explicit_retry_waits_for_read_only_load_and_keeps_known_model_after_ack(
        monkeypatch: pytest.MonkeyPatch, private_state_dir: Path) -> None:
    seed_pending(private_state_dir)
    app, client = make_app(COMPLETED, monkeypatch)
    gate = RequestGate("GET", HISTORY)
    client.request_hook = gate
    async with app.run_test():
        try:
            await eventually(gate.entered.is_set)
            original = app._pending_submission
            assert original is not None
            app._handle_command("/retry")
            app._handle_command("/retry")
            assert app._retry_after_recovery
            assert not any(method == "POST" for method, _, _ in client.calls)
            app.query_one("#input", Input).value = "keep newer draft"
        finally:
            gate.release.set()
        await eventually(lambda: app._status_state == "" and app.active_run is None
                         and app._history_refresh_token is None)
        posted = [body for method, path, body in client.calls if (method, path) == ("POST", RUNS)]
        assert posted == [original.body()]
        assert app.query_one("#input", Input).value == "keep newer draft"
        assert app._selection == {"mode": "balanced", "thinking": False}
        assert "mode: balanced" in text_of(app.query_one("#session-info"))
        assert "done" in chat_text(app)
        assert not app._retry_after_recovery


@pytest.mark.parametrize("reply, code", [
    (ClientError("runtime_timeout"), "runtime_timeout"),
    ({"invalid": "session"}, "invalid_session"),
    (RuntimeError("private exception details"), "internal_error"),
])
async def test_failed_read_keeps_original_request_and_allows_explicit_retry(
        monkeypatch: pytest.MonkeyPatch, private_state_dir: Path,
        reply: Any, code: str) -> None:
    seed_pending(private_state_dir)
    app, client = make_app(COMPLETED, monkeypatch)
    client.replies[("GET", SESSION)] = deque([reply])
    async with app.run_test():
        await eventually(lambda: app._pending_submission is not None
                         and app._recovery_view_token is None)
        assert code in app._status_state
        assert "private exception details" not in app._status_state + chat_text(app)
        original = app._pending_submission
        assert original is not None
        assert app.query_one("#input", Input).value == original.draft
        assert not app._pending_state_error
        assert not any(method == "POST" for method, _, _ in client.calls)
        app._handle_command("/retry")
        await eventually(lambda: app._status_state == "" and app.active_run is None)
        posted = [body for method, path, body in client.calls if (method, path) == ("POST", RUNS)]
        assert posted == [original.body()]


async def test_clear_cancels_restored_history_but_can_still_show_model(
        monkeypatch: pytest.MonkeyPatch, private_state_dir: Path) -> None:
    seed_pending(private_state_dir)
    app, client = make_app(COMPLETED, monkeypatch)
    gate = RequestGate("GET", HISTORY)
    client.request_hook = gate
    async with app.run_test() as pilot:
        try:
            await eventually(gate.entered.is_set)
            app._handle_command("/clear")
            app.query_one("#input", Input).value = "keep this draft"
        finally:
            gate.release.set()
        await eventually(lambda: app._recovery_view_token is None)
        await pilot.pause()
        assert chat_text(app) == ""
        assert app._selection.get("mode") == "balanced"
        assert app.query_one("#input", Input).value == "keep this draft"
        assert not any(method == "POST" for method, _, _ in client.calls)


@pytest.mark.parametrize("change", ["session", "pending", "submitting", "run", "unmount"])
async def test_stale_pending_view_cannot_replace_view_or_send_deferred_retry(
        monkeypatch: pytest.MonkeyPatch, change: str) -> None:
    app, client = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        await eventually(lambda: "ready" in app._status_state)
        original = pending()
        app.session_id = SID
        app._pending_submission = original
        token = object()
        app._recovery_view_token = token
        app._retry_after_recovery = True
        epoch = app._chat_epoch
        app._add_message(ChatMessage("assistant", "Keep this view"))
        if change == "session":
            app.session_id = OTHER
        elif change == "pending":
            app._pending_submission = pending()
        elif change == "submitting":
            app._submitting = True
        elif change == "run":
            app.active_run = RID
        else:
            app.on_unmount()
        app._pending_view_loaded(token, original, epoch, {
            "id": SID, "title": "Wrong view", "effectiveSelection": {"mode": "advanced"}},
            [ChatMessage("assistant", "stale history")], RID)
        app._pending_view_failed(token, original, "runtime_timeout")
        app._pending_view_done(token, original)
        await pilot.pause()
        assert "Keep this view" in chat_text(app)
        assert "stale history" not in chat_text(app)
        assert app.session_title != "Wrong view"
        assert "recovery view unavailable" not in app._status_state
        assert not any(method == "POST" for method, _, _ in client.calls)


async def test_recovery_history_is_bounded_and_does_not_open_replayed_approvals(
        monkeypatch: pytest.MonkeyPatch, private_state_dir: Path) -> None:
    seed_pending(private_state_dir)
    monkeypatch.setattr(WispTuiApp, "MAX_CHAT_ITEMS", 5)
    app, client = make_app([], monkeypatch)
    client.replies[("GET", HISTORY)] = deque([[{"values": {"messages": [
        {"type": "human" if index % 2 == 0 else "ai", "content": f"history {index}"}
        for index in range(100)]}}]])
    client.replies[("GET", LIST)] = deque([{
        "runs": [record("parked", confirmations=[{
            "id": "old-prompt", "toolName": "example", "args": {}}])], "nextCursor": None}])
    async with app.run_test() as pilot:
        await eventually(lambda: app._pending_submission is not None
                         and app._recovery_view_token is None)
        await pilot.pause()
        assert "history 99" in chat_text(app)
        assert "history 0" not in chat_text(app)
        assert len(app._chat_items) <= 5
        assert app._trimmed_count > 0
        assert len(app.screen_stack) == 1
        assert app._approval_modal is None
        assert not client.event_requests
        assert all(method == "GET" for method, _, _ in client.calls)
