"""End-to-end app tests with a scripted client: no network, no real inference."""
from __future__ import annotations

import asyncio
from typing import Any

import pytest
from wisp_tui.editor import PromptInput as Input

from wisp_tui.app import WispTuiApp
from tests.helpers import RID, SID, ScriptedClient
from wisp_tui.config import Config


def text_of(widget: object) -> str:
    return str(widget.render())  # type: ignore[attr-defined]


def screen_text(app: WispTuiApp) -> str:
    from textual.widgets import Static
    return "\n".join(str(w.render()) for w in app.screen.query(Static))


def chat_text(app: WispTuiApp) -> str:
    from textual.widgets import Markdown
    parts: list[str] = []
    for child in app.query("#chat > *"):
        if isinstance(child, Markdown):
            parts.append(str(child.source))
        else:
            parts.append(str(child.render()))
    return "\n".join(parts)

async def eventually(condition: Any, timeout: float = 5.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while not condition():
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError("condition not met within timeout")
        await asyncio.sleep(0.05)


def make_app(events: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch) -> tuple[
        WispTuiApp, ScriptedClient]:
    client = ScriptedClient(events)
    # Local session records go to the per-test state dir (tests/conftest.py).
    app = WispTuiApp(client, Config(url=client.url, token=client.token))
    return app, client


async def test_boots_and_shows_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    app, client = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        await eventually(lambda: "ready" in text_of(app.query_one("#status")))
        await pilot.pause()


async def test_help_command(monkeypatch: pytest.MonkeyPatch) -> None:
    app, client = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        await eventually(lambda: "ready" in text_of(app.query_one("#status")))
        app.query_one("#input", Input).value = "/help"
        await pilot.press("enter")
        await pilot.pause()
        rendered = chat_text(app)
        assert "wisp-tui" in rendered


async def test_submit_streams_and_completes(monkeypatch: pytest.MonkeyPatch) -> None:
    events = [
        {"seq": 1, "kind": "run.started"},
        {"seq": 2, "kind": "text.delta", "text": "Hello "},
        {"seq": 3, "kind": "text.delta", "text": "world!"},
        {"seq": 4, "kind": "run.completed", "outcome": {
            "runId": RID, "status": "completed",
            "output": {"text": "Hello world!"},
            "toolCalls": [], "usage": {"inputTokens": 3, "outputTokens": 4},
            "artifacts": []}},
    ]
    app, client = make_app(events, monkeypatch)
    async with app.run_test() as pilot:
        await eventually(lambda: "ready" in text_of(app.query_one("#status")))
        app.session_id = SID
        app.query_one("#input", Input).value = "hi there"
        await pilot.press("enter")
        await eventually(lambda: app.active_run is None)
        await eventually(lambda: app._status_state == "")
        # the run was submitted interactive and non-waiting
        posted = [c for c in client.calls if c[0] == "POST" and "/runs" in c[1]]
        assert posted, client.calls
        assert posted[0][2] is not None
        assert posted[0][2]["confirmationMode"] == "interactive"
        assert posted[0][2]["wait"] is False
        assert posted[0][2]["input"] == "hi there"
        # final answer text is rendered
        assert "Hello world!" in str(chat_text(app))


async def test_confirmation_flow_approves(monkeypatch: pytest.MonkeyPatch) -> None:
    events = [
        {"seq": 1, "kind": "run.started"},
        {"seq": 2, "kind": "confirmation.required", "id": "prompt-1",
         "toolName": "restart_jellyfin", "args": {"target": "jellyfin"}},
        {"seq": 3, "kind": "run.parked"},
        {"seq": 4, "kind": "run.resumed"},
        {"seq": 5, "kind": "run.completed", "outcome": {
            "runId": RID, "status": "completed", "output": {"text": "done"},
            "toolCalls": [], "usage": {}, "artifacts": []}},
    ]
    app, client = make_app(events, monkeypatch)
    async with app.run_test() as pilot:
        await eventually(lambda: "ready" in text_of(app.query_one("#status")))
        app.session_id = SID
        app.query_one("#input", Input).value = "restart jellyfin please"
        await pilot.press("enter")
        # the approval dialog appears
        await eventually(lambda: len(app.screen_stack) > 1)
        assert "restart_jellyfin" in str(screen_text(app))
        await pilot.press("y")  # approve
        await eventually(lambda: app.active_run is None)
        confirms = [c for c in client.calls if "/confirmations/" in c[1]]
        assert confirms, client.calls
        assert confirms[0][2] == {"decision": "approve", "wait": False}
        await eventually(lambda: "done" in chat_text(app))


async def test_confirmation_flow_esc_denies(monkeypatch: pytest.MonkeyPatch) -> None:
    events = [
        {"seq": 1, "kind": "confirmation.required", "id": "prompt-2",
         "toolName": "restart_navidrome", "args": {}},
        {"seq": 2, "kind": "run.parked"},
        {"seq": 3, "kind": "run.completed", "outcome": {
            "runId": RID, "status": "completed", "output": {"text": "ok"},
            "toolCalls": [], "usage": {}, "artifacts": []}},
    ]
    app, client = make_app(events, monkeypatch)
    async with app.run_test() as pilot:
        await eventually(lambda: "ready" in text_of(app.query_one("#status")))
        app.session_id = SID
        app.query_one("#input", Input).value = "restart navidrome"
        await pilot.press("enter")
        await eventually(lambda: len(app.screen_stack) > 1)
        await pilot.press("escape")
        await eventually(lambda: app.active_run is None)
        confirms = [c for c in client.calls if "/confirmations/" in c[1]]
        assert confirms
        assert confirms[0][2] == {"decision": "deny", "wait": False}


async def test_failed_run_shows_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    events = [
        {"seq": 1, "kind": "run.failed", "outcome": {
            "runId": RID, "status": "failed", "output": {"text": ""},
            "failure": {"reason": "quota_exhausted", "message": "balance is zero"},
            "toolCalls": [], "usage": {}, "artifacts": []}},
    ]
    app, client = make_app(events, monkeypatch)
    async with app.run_test() as pilot:
        await eventually(lambda: "ready" in text_of(app.query_one("#status")))
        app.session_id = SID
        app.query_one("#input", Input).value = "hello"
        await pilot.press("enter")
        await eventually(lambda: app.active_run is None)
        assert "quota_exhausted" in str(chat_text(app))
        assert "run failed" in text_of(app.query_one("#status"))


async def test_session_history_load(monkeypatch: pytest.MonkeyPatch) -> None:
    app, client = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        await eventually(lambda: "ready" in text_of(app.query_one("#status")))
        app._open_session(SID)
        await eventually(lambda: app.session_id == SID)
        await eventually(lambda: "past answer" in str(chat_text(app)))
        assert app.title == "Wisp — test chat"


async def test_ctrl_c_exits_when_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    app, client = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        await eventually(lambda: "ready" in text_of(app.query_one("#status")))
        assert not bool(app._exit)
        await pilot.press("ctrl+c")  # first press arms, does not exit
        await pilot.pause()
        assert not bool(app._exit)
        assert "again" in text_of(app.query_one("#status"))
        await pilot.press("ctrl+c")
        await pilot.pause()
        assert bool(app._exit)


async def test_ctrl_c_cancels_active_run_then_exits(
        monkeypatch: pytest.MonkeyPatch) -> None:
    events = [
        {"seq": 1, "kind": "run.started"},
        {"seq": 2, "kind": "run.parked"},
    ]
    app, client = make_app(events, monkeypatch)
    async with app.run_test() as pilot:
        await eventually(lambda: "ready" in text_of(app.query_one("#status")))
        app.session_id = SID
        app.query_one("#input", Input).value = "long task"
        await pilot.press("enter")
        await eventually(lambda: app.active_run is not None)
        await pilot.press("ctrl+c")
        await pilot.pause()
        # first press cancels the run, it does not exit
        assert not bool(app._exit)
        cancels = [c for c in client.calls if c[1].endswith("/cancel")]
        assert cancels, client.calls
        # second press exits even though the run is still marked active
        await pilot.press("ctrl+c")
        await pilot.pause()
        assert bool(app._exit)
