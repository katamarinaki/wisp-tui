"""The activity spinner is local UI state, not another runtime poller."""
from __future__ import annotations

import pytest
from textual.widgets import Input, Static

from tests.helpers import RID, SID, RequestGate
from tests.test_app import chat_text, eventually, make_app, text_of
from wisp_tui.app import SPINNER_FRAMES


@pytest.mark.parametrize("width", [40, 100])
async def test_working_animates_above_input_and_disappears_on_completion(
        monkeypatch: pytest.MonkeyPatch, width: int) -> None:
    app, client = make_app([], monkeypatch)
    async with app.run_test(size=(width, 24)) as pilot:
        await eventually(lambda: "ready" in app._status_state)
        indicator = app.query_one("#activity", Static)
        assert not bool(indicator.display)
        app.session_id = SID
        app._turn_started(RID)
        await pilot.pause()
        assert bool(indicator.display)
        assert text_of(indicator).endswith(" Working")
        first = text_of(indicator)
        await eventually(lambda: text_of(indicator) != first)
        assert text_of(indicator)[0] in SPINNER_FRAMES
        for new_width in (width, 40 if width == 100 else 100):
            await pilot.resize_terminal(new_width, 24)
            await pilot.pause()
            editor = app.query_one("#input", Input)
            assert indicator.region.x == editor.content_region.x == 1
            assert indicator.region.height == 1
            assert indicator.region.bottom == editor.region.y
            assert indicator.styles.background.a == 0
        app._finalize_run("run.completed", {"output": {"text": "Final answer"}})
        await pilot.pause()
        assert not bool(indicator.display) and text_of(indicator) == ""
        assert app._status_state == ""
        assert "Final answer" in chat_text(app)
        assert not any("Working" in text_of(w) for w in app.query("#chat > Static"))
        assert client.calls == [("GET", "/v1/health", None)]


@pytest.mark.parametrize("kind", ["run.failed", "run.cancelled"])
async def test_spinner_stops_on_every_terminal_outcome(
        monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test():
        await eventually(lambda: "ready" in app._status_state)
        app.session_id = SID
        app._turn_started(RID)
        assert app.query_one("#activity").display
        app._finalize_run(kind, {})
        assert not app.query_one("#activity").display


async def test_spinner_covers_submission_and_stops_after_ambiguous_failure(
        monkeypatch: pytest.MonkeyPatch) -> None:
    app, client = make_app([], monkeypatch)
    path = f"/v1/sessions/{SID}/runs"
    gate = RequestGate("POST", path)
    client.request_hook = gate
    # No acknowledgement: this remains pending, but isn't active work in the UI.
    from collections import deque
    from wisp_tui.client import ClientError
    client.replies[("POST", path)] = deque([ClientError("runtime_timeout")])
    async with app.run_test() as pilot:
        await eventually(lambda: "ready" in app._status_state)
        app.session_id = SID
        app.query_one("#input", Input).value = "hello"
        try:
            await pilot.press("enter")
            await eventually(gate.entered.is_set)
            assert text_of(app.query_one("#activity")).endswith(" Working")
        finally:
            gate.release.set()
        await eventually(lambda: "submission error" in app._status_state)
        assert not app.query_one("#activity").display
        assert app._pending_submission is not None


async def test_waiting_disconnected_and_cancelling_are_not_labelled_working(
        monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test():
        await eventually(lambda: "ready" in app._status_state)
        app.session_id = SID
        app._turn_started(RID)
        app._handle_event({"kind": "run.parked"})
        assert text_of(app.query_one("#activity")).endswith(" Waiting for approval")
        app._stream_failed("runtime_timeout")
        assert text_of(app.query_one("#activity")).endswith(" Reconnecting")
        app._disconnected = False
        app._cancel_requested = True
        app._render_activity()
        assert text_of(app.query_one("#activity")).endswith(" Cancelling")
        assert app.query_one("#activity").display  # Until runtime confirms it stopped.


async def test_queued_tick_after_widget_unmount_is_harmless(
        monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test():
        await eventually(lambda: "ready" in app._status_state)
        app._turn_started(RID)
        indicator = app.query_one("#activity", Static)
        await indicator.remove()
        app._tick_activity()
        assert not indicator.is_attached
