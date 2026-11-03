"""Submission/session races exercised with blocked scripted requests, not sleeps."""
from __future__ import annotations

from collections import deque
from typing import Any

import pytest
from textual.widgets import Input

from tests.helpers import RID, SID, RequestGate
from tests.test_app import chat_text, eventually, make_app
from wisp_tui.app import Startup
from wisp_tui.client import ClientError

RUN_PATH = f"/v1/sessions/{SID}/runs"
COMPLETED: list[dict[str, Any]] = [
    {"seq": 1, "kind": "run.completed", "outcome": {"output": {"text": "done"}}},
]


@pytest.mark.parametrize("state", ["active_run", "session_change"])
async def test_rejected_input_is_kept(monkeypatch: pytest.MonkeyPatch, state: str) -> None:
    app, client = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        await eventually(lambda: "ready" in app._status_state)
        if state == "active_run":
            app.session_id = SID
            app.active_run = RID
        else:
            app._session_busy = True
        editor = app.query_one("#input", Input)
        editor.value = "keep this draft"
        await pilot.press("enter")
        assert editor.value == "keep this draft"
        assert not any(method == "POST" for method, _, _ in client.calls)


async def test_inflight_submission_blocks_duplicates_and_session_changes(
        monkeypatch: pytest.MonkeyPatch) -> None:
    app, client = make_app(COMPLETED, monkeypatch)
    gate = RequestGate("POST", RUN_PATH)
    client.request_hook = gate
    async with app.run_test() as pilot:
        await eventually(lambda: "ready" in app._status_state)
        app.session_id = SID
        editor = app.query_one("#input", Input)
        editor.value = "original task"
        try:
            await pilot.press("enter")
            await eventually(gate.entered.is_set)
            assert bool(app._submitting)
            assert editor.value == "original task"
            editor.value = "newer draft"
            await pilot.press("enter")
            app._open_session(SID)
            app._new_session("should not be created")
            app._model_answered({"mode": "balanced"}, SID)
            app._handle_command("/retry")
            assert editor.value == "newer draft"
            assert [(m, p) for m, p, _ in client.calls] == [
                ("GET", "/v1/health"), ("POST", RUN_PATH)]
        finally:
            gate.release.set()
        await eventually(lambda: "run completed" in app._status_state)
        assert editor.value == "newer draft"
        assert chat_text(app).count("original task") == 1
        assert not app._submitting
        assert app._pending_submission is None


@pytest.mark.parametrize("reply", [
    ClientError("runtime_timeout"),
    {"runId": "not-a-run-id"},
    RuntimeError("untrusted diagnostic must not appear"),
])
async def test_retry_keeps_original_request_and_newer_draft(
        monkeypatch: pytest.MonkeyPatch, reply: Any) -> None:
    app, client = make_app(COMPLETED, monkeypatch)
    client.replies[("POST", RUN_PATH)] = deque([reply])
    async with app.run_test() as pilot:
        await eventually(lambda: "ready" in app._status_state)
        app.session_id = SID
        editor = app.query_one("#input", Input)
        editor.value = "original task"
        await pilot.press("enter")
        await eventually(lambda: "submission error" in app._status_state)
        assert editor.value == "original task"
        assert app._pending_submission is not None
        assert not app._submitting
        assert "untrusted diagnostic" not in app._status_state + chat_text(app)
        editor.value = "different task"
        await pilot.press("enter")
        app._new_session("blocked while uncertain")
        assert len([c for c in client.calls if c[0] == "POST"]) == 1
        app._handle_command("/retry")
        await eventually(lambda: "run completed" in app._status_state)
        posts = [body for method, path, body in client.calls if path == RUN_PATH]
        assert len(posts) == 2
        assert posts[0] == posts[1]
        assert posts[0] is not None
        assert posts[0]["input"] == "original task"
        assert editor.value == "different task"
        assert chat_text(app).count("original task") == 1


async def test_success_clears_only_acknowledged_draft(monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = make_app(COMPLETED, monkeypatch)
    async with app.run_test() as pilot:
        await eventually(lambda: "ready" in app._status_state)
        app.session_id = SID
        editor = app.query_one("#input", Input)
        editor.value = "  original task  "
        await pilot.press("enter")
        await eventually(lambda: "run completed" in app._status_state)
        assert editor.value == ""
        assert "original task" in chat_text(app)


async def test_session_load_serializes_changes_and_preserves_input(
        monkeypatch: pytest.MonkeyPatch) -> None:
    app, client = make_app([], monkeypatch)
    gate = RequestGate("GET", f"/v1/sessions/{SID}")
    client.request_hook = gate
    async with app.run_test() as pilot:
        await eventually(lambda: "ready" in app._status_state)
        editor = app.query_one("#input", Input)
        try:
            app._open_session(SID)
            await eventually(gate.entered.is_set)
            app._open_session(SID)
            app._new_session("blocked")
            editor.value = "wait for session"
            await pilot.press("enter")
            assert editor.value == "wait for session"
            assert app._session_busy
            assert len(client.calls) == 2  # health + the first session load
        finally:
            gate.release.set()
        await eventually(lambda: app.session_id == SID and not app._session_busy)
        assert "past answer" in chat_text(app)
        assert editor.value == "wait for session"


@pytest.mark.parametrize("reply", [
    ClientError("runtime_timeout"),
    RuntimeError("untrusted diagnostic"),
    {"id": "invalid"},
])
async def test_failed_session_load_releases_reservation(
        monkeypatch: pytest.MonkeyPatch, reply: Any) -> None:
    app, client = make_app([], monkeypatch)
    client.replies[("GET", f"/v1/sessions/{SID}")] = deque([reply])
    async with app.run_test():
        await eventually(lambda: "ready" in app._status_state)
        app._open_session(SID)
        await eventually(lambda: not app._session_busy)
        assert not bool(app.session_id)
        app._open_session(SID)
        await eventually(lambda: app.session_id == SID and not app._session_busy)
        assert "untrusted diagnostic" not in app._status_state + chat_text(app)


async def test_stale_model_callback_does_not_patch_another_session(
        monkeypatch: pytest.MonkeyPatch) -> None:
    app, client = make_app([], monkeypatch)
    async with app.run_test():
        await eventually(lambda: "ready" in app._status_state)
        app.session_id = RID  # different valid session identifier
        app._model_answered({"mode": "balanced"}, SID)
        app._show_model_picker([], None, SID)
        assert len(app.screen_stack) == 1
        assert not any(method == "PATCH" for method, _, _ in client.calls)


async def test_slow_bootstrap_does_not_replace_user_session(
        monkeypatch: pytest.MonkeyPatch) -> None:
    app, client = make_app([], monkeypatch)
    app.startup = Startup("session", RID)
    gate = RequestGate("GET", "/v1/health")
    client.request_hook = gate
    async with app.run_test():
        try:
            await eventually(gate.entered.is_set)
            app._new_session("chosen by user")
            await eventually(lambda: app.session_id == SID and not app._session_busy)
        finally:
            gate.release.set()
        await eventually(lambda: all(worker.is_finished for worker in app.workers))
        assert app.session_id == SID
        assert not any(path == f"/v1/sessions/{RID}" for _, path, _ in client.calls)


@pytest.mark.parametrize("reason, expected", [
    ("quota_exhausted", "quota_exhausted"),
    ("untrusted runtime explanation", "unknown"),
    ({"detail": "untrusted runtime explanation"}, "unknown"),
])
async def test_run_failure_displays_only_safe_code(
        monkeypatch: pytest.MonkeyPatch, reason: Any, expected: str) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        await eventually(lambda: "ready" in app._status_state)
        app.active_run = RID
        app._finalize_run("run.failed", {"failure": {
            "reason": reason, "message": "untrusted runtime explanation"}})
        await pilot.pause()
        assert f"run failed: {expected}" in chat_text(app)
        assert "untrusted runtime explanation" not in chat_text(app)


async def test_health_failure_does_not_display_runtime_status(
        monkeypatch: pytest.MonkeyPatch) -> None:
    app, client = make_app([], monkeypatch)
    client.replies[("GET", "/v1/health")] = deque([
        {"status": "untrusted runtime explanation"}])
    async with app.run_test():
        await eventually(lambda: app._status_state == "runtime_not_ready")
        assert "untrusted runtime explanation" not in chat_text(app)
