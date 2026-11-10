"""The activity spinner is local UI state, not another runtime poller."""

from __future__ import annotations

from collections import deque

import pytest

from tests.helpers import RID, SID, RequestGate
from tests.test_app import chat_text, eventually, make_app, ready, text_of
from wisp_tui.app import WispTuiApp
from wisp_tui.app_base import SPINNER_FRAMES
from wisp_tui.client import ClientError
from wisp_tui.editor import PromptInput as Input
from wisp_tui.terminal_status import TabStatus, tab_status_sequence


def spinner(app: WispTuiApp) -> str:
    """The activity label lives in the input's top rule, as its border title."""
    return str(app.query_one("#input", Input).border_title).strip()


def tab_status(app: WispTuiApp) -> TabStatus | None:
    return app._tab_status


@pytest.mark.parametrize("width", [40, 100])
async def test_working_animates_in_input_rule_and_disappears_on_completion(
    monkeypatch: pytest.MonkeyPatch, width: int
) -> None:
    app, client = make_app([], monkeypatch)
    async with app.run_test(size=(width, 24)) as pilot:
        await eventually(lambda: ready(app))
        editor = app.query_one("#input", Input)
        assert spinner(app) == ""
        assert tab_status(app) == TabStatus.IDLE
        app.session_id = SID
        app._begin_run(RID)
        await eventually(lambda: spinner(app).endswith(" Working"))
        assert tab_status(app) == TabStatus.WORKING
        first = spinner(app)
        await eventually(lambda: spinner(app) != first)
        assert spinner(app)[0] in SPINNER_FRAMES
        assert editor.styles.border_title_align == "left"
        for new_width in (width, 40 if width == 100 else 100):
            await pilot.resize_terminal(new_width, 24)
            await eventually(lambda new_width=new_width: app.screen.size.width == new_width)
            await app.screen.wait_for_refresh()
            row = editor.region.y
            styles = [app.screen.get_style_at(x, row) for x in range(editor.region.width)]
            assert all(style.bgcolor is None or style.bgcolor.is_default for style in styles)
        app._finalize_run("run.completed", {"output": {"text": "Final answer"}})
        await eventually(lambda: "Final answer" in chat_text(app))
        assert spinner(app) == ""
        assert app._status_text == ""
        assert tab_status(app) == TabStatus.IDLE
        assert not any("Working" in text_of(w) for w in app.query("#chat > Static"))
        assert client.calls == [("GET", "/v1/health", None)]


@pytest.mark.parametrize("kind", ["run.failed", "run.cancelled"])
async def test_spinner_stops_on_every_terminal_outcome(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test():
        await eventually(lambda: ready(app))
        app.session_id = SID
        app._begin_run(RID)
        assert spinner(app)
        app._finalize_run(kind, {})
        assert not spinner(app)
        assert tab_status(app) == (TabStatus.ERROR if kind == "run.failed" else TabStatus.CANCELLED)


async def test_spinner_covers_submission_and_stops_after_ambiguous_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client = make_app([], monkeypatch)
    path = f"/v1/sessions/{SID}/runs"
    gate = RequestGate("POST", path)
    client.request_hook = gate
    # No acknowledgement: this remains pending, but isn't active work in the UI.

    client.replies[("POST", path)] = deque([ClientError("runtime_timeout")])
    async with app.run_test() as pilot:
        await eventually(lambda: ready(app))
        app.session_id = SID
        app.query_one("#input", Input).value = "hello"
        try:
            await pilot.press("enter")
            await eventually(gate.entered.is_set)
            assert spinner(app).endswith(" Working")
            assert tab_status(app) == TabStatus.WORKING
        finally:
            gate.release.set()
        await eventually(lambda: "submission error" in app._status_text)
        assert not spinner(app)
        assert app._pending_submission is not None
        assert tab_status(app) == TabStatus.ATTENTION


async def test_waiting_disconnected_and_cancelling_are_not_labelled_working(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test():
        await eventually(lambda: ready(app))
        app.session_id = SID
        app._begin_run(RID)
        app._handle_event({"kind": "run.parked"})
        assert spinner(app).endswith(" Waiting for approval")
        assert tab_status(app) == TabStatus.WAITING
        app._stream_failed("runtime_timeout")
        assert spinner(app).endswith(" Reconnecting")
        assert tab_status(app) == TabStatus.RECONNECTING
        app._disconnected = False
        app._cancel_requested = True
        app._render_activity()
        assert spinner(app).endswith(" Cancelling")  # Until runtime confirms it stopped.
        assert tab_status(app) == TabStatus.CANCELLING


async def test_tab_status_writes_only_changes_and_stays_cleared_after_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client = make_app([], monkeypatch)
    async with app.run_test():
        await eventually(lambda: ready(app))
        driver = app._driver
        assert driver is not None
        writes: list[str] = []
        with monkeypatch.context() as patch:
            patch.setattr(driver, "write", writes.append)
            app._show_error("example_error")
            assert tab_status(app) == TabStatus.ERROR
            assert writes == []  # Headless sessions never emit control sequences.
            # Exercise the terminal writer synchronously, without running a real
            # terminal or letting Textual repaint while this stub is installed.
            patch.setattr(type(driver), "is_headless", property(lambda _: False))
            app.session_id = SID
            app._begin_run(RID)
            for _ in range(20):
                app._tick_activity()
            app._clear_tab_status()
            app._tick_activity()  # A late UI tick must not reinstate the subtitle.
            app._clear_tab_status()
        assert writes == [
            tab_status_sequence(TabStatus.WORKING),
            tab_status_sequence(None),
        ]
        assert client.calls == [("GET", "/v1/health", None)]


@pytest.mark.parametrize("health", [ClientError("runtime_timeout"), {"status": "starting"}])
async def test_failed_startup_is_not_idle_in_tab(
    monkeypatch: pytest.MonkeyPatch, health: object
) -> None:
    app, client = make_app([], monkeypatch)
    client.replies[("GET", "/v1/health")] = deque([health])
    async with app.run_test():
        await eventually(lambda: tab_status(app) == TabStatus.ERROR)
        assert app.query_one("#status").display
