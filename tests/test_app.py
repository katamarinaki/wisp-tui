"""End-to-end app tests with a scripted client: no network, no real inference."""

from __future__ import annotations

import asyncio
from collections import deque
from types import SimpleNamespace
from typing import Any

import pytest
from textual.widgets import Markdown, Static

from tests.helpers import RID, SID, RequestGate, ScriptedClient
from wisp_tui import app as app_module
from wisp_tui.app import WispTuiApp
from wisp_tui.client import ClientError
from wisp_tui.config import Config
from wisp_tui.editor import PromptInput as Input
from wisp_tui.screens import ApprovalModal
from wisp_tui.tool_view import ToolCallView


def text_of(widget: object) -> str:
    return str(widget.render())  # type: ignore[attr-defined]


def screen_text(app: WispTuiApp) -> str:

    return "\n".join(str(w.render()) for w in app.screen.query(Static))


def chat_text(app: WispTuiApp) -> str:

    parts: list[str] = []
    for child in app.query("#chat > *"):
        if isinstance(child, Markdown):
            parts.append(str(child.source))
        elif isinstance(child, ToolCallView):
            parts.append(child.title)
        else:
            parts.append(str(child.render()))
    return "\n".join(parts)


async def eventually(condition: Any, timeout: float = 60.0) -> None:
    # The timeout only catches hangs; a slow but correct run must never hit it.
    deadline = asyncio.get_event_loop().time() + timeout
    while not condition():
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError("condition not met within timeout")
        await asyncio.sleep(0.05)


async def approval_armed(app: WispTuiApp) -> None:
    """Wait until the approval dialog accepts decision keys (ApprovalModal.ARM_DELAY)."""
    await eventually(lambda: isinstance(app.screen, ApprovalModal) and app.screen.armed)


def freeze_ctrl_c_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both Ctrl+C presses fall in the 3 s window however slow the test runs."""
    monkeypatch.setattr(app_module, "time", SimpleNamespace(monotonic=lambda: 1000.0))


def make_app(
    events: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> tuple[WispTuiApp, ScriptedClient]:
    client = ScriptedClient(events)
    # Local session records go to the per-test state dir (tests/conftest.py).
    app = WispTuiApp(client, Config(url=client.url, token=client.token))
    return app, client


async def test_boots_and_shows_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        await eventually(lambda: "ready" in text_of(app.query_one("#status")))
        await pilot.pause()


async def test_help_command(monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = make_app([], monkeypatch)
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
        {
            "seq": 4,
            "kind": "run.completed",
            "outcome": {
                "runId": RID,
                "status": "completed",
                "output": {"text": "Hello world!"},
                "toolCalls": [],
                "usage": {"inputTokens": 3, "outputTokens": 4},
                "artifacts": [],
            },
        },
    ]
    app, client = make_app(events, monkeypatch)
    async with app.run_test() as pilot:
        await eventually(lambda: "ready" in text_of(app.query_one("#status")))
        app.session_id = SID
        app.query_one("#input", Input).value = "hi there"
        await pilot.press("enter")
        await eventually(lambda: app.active_run is None)
        await eventually(lambda: app._status_text == "")
        # the run was submitted interactive and non-waiting
        posted = [c for c in client.calls if c[0] == "POST" and "/runs" in c[1]]
        assert posted, client.calls
        assert posted[0][2] is not None
        assert posted[0][2]["confirmationMode"] == "interactive"
        assert posted[0][2]["wait"] is False
        assert posted[0][2]["input"] == "hi there"
        # final answer text is rendered
        await eventually(lambda: "Hello world!" in chat_text(app))


async def test_confirmation_flow_approves(monkeypatch: pytest.MonkeyPatch) -> None:
    events = [
        {"seq": 1, "kind": "run.started"},
        {
            "seq": 2,
            "kind": "confirmation.required",
            "id": "prompt-1",
            "toolName": "restart_jellyfin",
            "args": {"target": "jellyfin"},
        },
        {"seq": 3, "kind": "run.parked"},
        {"seq": 4, "kind": "run.resumed"},
        {
            "seq": 5,
            "kind": "run.completed",
            "outcome": {
                "runId": RID,
                "status": "completed",
                "output": {"text": "done"},
                "toolCalls": [],
                "usage": {},
                "artifacts": [],
            },
        },
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
        await approval_armed(app)
        await pilot.press("y")  # approve
        await eventually(lambda: app.active_run is None)
        confirms = [c for c in client.calls if "/confirmations/" in c[1]]
        assert confirms, client.calls
        assert confirms[0][2] == {"decision": "approve", "wait": False}
        await eventually(lambda: "done" in chat_text(app))


async def test_refused_rule_decision_offers_only_approve_or_deny(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    events = [
        {
            "seq": 1,
            "kind": "confirmation.required",
            "id": "prompt-1",
            "toolName": "restart_jellyfin",
            "args": {},
        },
        {
            "seq": 2,
            "kind": "confirmation.required",
            "id": "prompt-2",
            "toolName": "restart_navidrome",
            "args": {},
        },
        {"seq": 3, "kind": "run.parked"},
        {
            "seq": 4,
            "kind": "run.completed",
            "outcome": {
                "runId": RID,
                "status": "completed",
                "output": {"text": "done"},
                "toolCalls": [],
                "usage": {},
                "artifacts": [],
            },
        },
    ]
    app, client = make_app(events, monkeypatch)
    client.replies[("POST", f"/v1/runs/{RID}/confirmations/prompt-1")] = deque(
        [ClientError("runtime_http_403")]
    )
    gate = RequestGate("POST", f"/v1/runs/{RID}/confirmations/prompt-1")
    client.request_hook = gate
    async with app.run_test() as pilot:
        await eventually(lambda: "ready" in text_of(app.query_one("#status")))
        app.session_id = SID
        app.query_one("#input", Input).value = "restart both"
        await pilot.press("enter")
        await approval_armed(app)
        await pilot.press("p")
        # The next prompt's dialog opens (still offering rules) before the 403.
        await eventually(
            lambda: (
                gate.entered.is_set()
                and isinstance(app.screen, ApprovalModal)
                and app.screen.allow_rules
                and app.screen.tool_name == "restart_navidrome"
            )
        )
        gate.release.set()
        await eventually(lambda: app._rule_decisions_refused)
        await eventually(
            lambda: isinstance(app.screen, ApprovalModal) and not app.screen.allow_rules
        )
        await approval_armed(app)
        assert "allow" not in text_of(app.screen.query_one("#approval-hint"))
        await pilot.press("g")
        await pilot.pause()
        await pilot.press("y")
        await approval_armed(app)
        assert isinstance(app.screen, ApprovalModal) and not app.screen.allow_rules
        await pilot.press("y")
        await eventually(lambda: app.active_run is None)
        decisions = [
            (c[1].rsplit("/", 1)[1], c[2]["decision"])
            for c in client.calls
            if "/confirmations/" in c[1] and c[2] is not None
        ]
        assert decisions[0] == ("prompt-1", "allow_project")
        assert sorted(decisions[1:]) == [("prompt-1", "approve"), ("prompt-2", "approve")]
        assert "may not create project/global rules" in chat_text(app)


async def test_confirmation_flow_esc_denies(monkeypatch: pytest.MonkeyPatch) -> None:
    events = [
        {
            "seq": 1,
            "kind": "confirmation.required",
            "id": "prompt-2",
            "toolName": "restart_navidrome",
            "args": {},
        },
        {"seq": 2, "kind": "run.parked"},
        {
            "seq": 3,
            "kind": "run.completed",
            "outcome": {
                "runId": RID,
                "status": "completed",
                "output": {"text": "ok"},
                "toolCalls": [],
                "usage": {},
                "artifacts": [],
            },
        },
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
        {
            "seq": 1,
            "kind": "run.failed",
            "outcome": {
                "runId": RID,
                "status": "failed",
                "output": {"text": ""},
                "failure": {"reason": "quota_exhausted", "message": "balance is zero"},
                "toolCalls": [],
                "usage": {},
                "artifacts": [],
            },
        },
    ]
    app, _ = make_app(events, monkeypatch)
    async with app.run_test() as pilot:
        await eventually(lambda: "ready" in text_of(app.query_one("#status")))
        app.session_id = SID
        app.query_one("#input", Input).value = "hello"
        await pilot.press("enter")
        await eventually(lambda: "quota_exhausted" in chat_text(app))
        assert app.active_run is None
        assert "run failed" in text_of(app.query_one("#status"))


async def test_session_history_load(monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test():
        await eventually(lambda: "ready" in text_of(app.query_one("#status")))
        app._open_session(SID)
        await eventually(lambda: app.session_id == SID)
        await eventually(lambda: "past answer" in str(chat_text(app)))
        assert app.title == "Wisp — test chat"


async def test_ctrl_c_exits_when_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    freeze_ctrl_c_clock(monkeypatch)
    app, _ = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        await eventually(lambda: "ready" in text_of(app.query_one("#status")))
        assert not bool(app._exit)
        await pilot.press("ctrl+c")  # first press arms, does not exit
        await eventually(lambda: "again" in text_of(app.query_one("#status")))
        assert not bool(app._exit)
        await pilot.press("ctrl+c")
        await eventually(lambda: bool(app._exit))


async def test_ctrl_c_cancels_active_run_then_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    freeze_ctrl_c_clock(monkeypatch)
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
        # first press cancels the run, it does not exit
        await eventually(lambda: any(c[1].endswith("/cancel") for c in client.calls))
        assert not bool(app._exit)
        # second press exits even though the run is still marked active
        await pilot.press("ctrl+c")
        await eventually(lambda: bool(app._exit))
