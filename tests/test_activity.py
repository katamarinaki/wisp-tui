"""The activity spinner is local UI state, not another runtime poller."""
from __future__ import annotations

import pytest
from wisp_tui.editor import PromptInput as Input

from tests.helpers import RID, SID, RequestGate
from tests.test_app import chat_text, eventually, make_app, text_of
from wisp_tui.app import SPINNER_FRAMES, WispTuiApp


def spinner(app: WispTuiApp) -> str:
    """The activity label lives in the input's top rule, as its border title."""
    return str(app.query_one("#input", Input).border_title).strip()


@pytest.mark.parametrize("width", [40, 100])
async def test_working_animates_in_input_rule_and_disappears_on_completion(
        monkeypatch: pytest.MonkeyPatch, width: int) -> None:
    app, client = make_app([], monkeypatch)
    async with app.run_test(size=(width, 24)) as pilot:
        await eventually(lambda: "ready" in app._status_state)
        editor = app.query_one("#input", Input)
        assert spinner(app) == ""
        app.session_id = SID
        app._turn_started(RID)
        await pilot.pause()
        assert spinner(app).endswith(" Working")
        first = spinner(app)
        await eventually(lambda: spinner(app) != first)
        assert spinner(app)[0] in SPINNER_FRAMES
        assert editor.styles.border_title_align == "left"
        for new_width in (width, 40 if width == 100 else 100):
            await pilot.resize_terminal(new_width, 24)
            await pilot.pause()
            row = editor.region.y
            styles = [app.screen.get_style_at(x, row) for x in range(editor.region.width)]
            assert all(style.bgcolor is None or style.bgcolor.is_default for style in styles)
        app._finalize_run("run.completed", {"output": {"text": "Final answer"}})
        await pilot.pause()
        assert spinner(app) == ""
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
        assert spinner(app)
        app._finalize_run(kind, {})
        assert not spinner(app)


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
            assert spinner(app).endswith(" Working")
        finally:
            gate.release.set()
        await eventually(lambda: "submission error" in app._status_state)
        assert not spinner(app)
        assert app._pending_submission is not None


async def test_waiting_disconnected_and_cancelling_are_not_labelled_working(
        monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test():
        await eventually(lambda: "ready" in app._status_state)
        app.session_id = SID
        app._turn_started(RID)
        app._handle_event({"kind": "run.parked"})
        assert spinner(app).endswith(" Waiting for approval")
        app._stream_failed("runtime_timeout")
        assert spinner(app).endswith(" Reconnecting")
        app._disconnected = False
        app._cancel_requested = True
        app._render_activity()
        assert spinner(app).endswith(" Cancelling")  # Until runtime confirms it stopped.


async def test_queued_tick_after_widget_unmount_is_harmless(
        monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test():
        await eventually(lambda: "ready" in app._status_state)
        app._turn_started(RID)
        editor = app.query_one("#input", Input)
        await editor.remove()
        app._tick_activity()
        assert not editor.is_attached
