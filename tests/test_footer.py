"""Persistent session metadata; no runtime, sockets, or private config access."""
from __future__ import annotations

from collections import deque
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Input, Markdown, Static

from tests.helpers import RID, SID, RequestGate
from tests.test_app import chat_text, eventually, make_app, text_of
from wisp_tui.app import WispTuiApp

OTHER_SID = "af06ed54-82b0-45e6-af55-7c85ab55e7ea"


def footer(app: WispTuiApp, name: str) -> str:
    return text_of(app.query_one(f"#{name}", Static))


async def test_fresh_footer_and_full_status_explain_unavailable_data(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    launch = tmp_path / "project[/]"
    launch.mkdir(parents=True)
    monkeypatch.chdir(launch)
    app, client = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        await eventually(lambda: "ready" in app._status_state)
        assert "session new" in footer(app, "session-info")
        assert "model: unavailable (runtime default)" in footer(app, "session-info")
        assert footer(app, "thinking-info") == "thinking: unavailable · context: unavailable"
        assert footer(app, "usage-info") == "last run tokens: unavailable"
        assert footer(app, "location") == f"launch dir (client only): {launch}"
        # A later chdir must not relabel the original launch directory.
        monkeypatch.chdir(tmp_path)
        app._status("submitting…")
        assert str(launch) in footer(app, "location")
        app._handle_command("/status")
        await pilot.pause()
        assert str(launch) in chat_text(app)
        assert "Wisp working directory: unavailable" in chat_text(app)
        assert "not live" in chat_text(app)
        assert client.calls == [("GET", "/v1/health", None)]


@pytest.mark.parametrize("selection, model, thinking", [
    ({"mode": "model", "model": "model[/]x", "thinking": True},
     "model: model[/]x", "on"),
    ({"mode": "balanced", "thinking": False},
     "mode: balanced (runtime-selected model)", "off"),
    ({"mode": "advanced"}, "mode: advanced (runtime-selected model)", "unavailable"),
    ({"mode": "model", "thinking": "false"}, "model: unavailable", "unavailable"),
    ({}, "model: unavailable (runtime default)", "unavailable"),
])
async def test_selection_stays_visible_through_run_status_changes(
        monkeypatch: pytest.MonkeyPatch, selection: dict[str, Any],
        model: str, thinking: str) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test():
        await eventually(lambda: "ready" in app._status_state)
        app._session_loaded({"id": SID, "effectiveSelection": selection}, [], "")
        expected = footer(app, "session-info")
        assert model in expected
        assert footer(app, "thinking-info").startswith(f"thinking: {thinking} ·")
        app._turn_started(RID)
        app._handle_event({"kind": "run.started"})
        assert footer(app, "session-info") == expected
        app._finalize_run("run.completed", {"usage": {"inputTokens": 3, "outputTokens": 4}})
        assert footer(app, "session-info") == expected
        assert footer(app, "usage-info") == "last run tokens: input: 3, output: 4"
        assert app.session_id == SID and app.active_run is None
        assert app._status_state == ""
        assert footer(app, "status") == "F1 help · F2 sessions · F3 model · Ctrl+C cancel/exit"


async def test_follow_up_uses_same_session_and_updates_last_run_not_context(
        monkeypatch: pytest.MonkeyPatch) -> None:
    app, client = make_app([], monkeypatch)
    client.event_batches = deque([
        [{"seq": 1, "kind": "run.completed", "outcome": {
            "output": {"text": "First answer"},
            "usage": {"inputTokens": 10, "outputTokens": 20, "totalTokens": 30}}}],
        [{"seq": 1, "kind": "run.completed", "outcome": {
            "output": {"text": "Follow-up answer"},
            "usage": {"inputTokens": 40, "outputTokens": 5}}}],
    ])
    async with app.run_test() as pilot:
        await eventually(lambda: "ready" in app._status_state)
        editor = app.query_one("#input", Input)
        editor.value = "First question"
        await pilot.press("enter")
        await eventually(lambda: app._status_state == "")
        await eventually(lambda: app._follower is None)
        assert "input: 10, output: 20, total: 30" in footer(app, "usage-info")
        editor.value = "And a follow-up?"
        await pilot.press("enter")
        await eventually(lambda: "input: 40" in footer(app, "usage-info"))
        assert app._status_state == ""
        assert app.session_id == SID
        assert footer(app, "usage-info") == "last run tokens: input: 40, output: 5"
        assert "context: unavailable" in footer(app, "thinking-info")
        assert "First answer" in chat_text(app) and "Follow-up answer" in chat_text(app)
        assert app.query("#chat > *").last(Markdown).source == "Follow-up answer"
        assert "Ready for your next message" not in chat_text(app)
        assert "run completed" not in footer(app, "status")
        creates = [c for c in client.calls if c[:2] == ("POST", "/v1/sessions")]
        runs = [c for c in client.calls if c[:2] == ("POST", f"/v1/sessions/{SID}/runs")]
        assert len(creates) == 1 and len(runs) == 2
        first, second = runs[0][2], runs[1][2]
        assert first is not None and second is not None
        assert first["idempotencyKey"] != second["idempotencyKey"]
        assert second["input"] == "And a follow-up?"


async def test_model_patch_updates_footer_only_after_acknowledgement(
        monkeypatch: pytest.MonkeyPatch) -> None:
    app, client = make_app([], monkeypatch)
    selection = {"mode": "model", "model": "model-b", "thinking": True}
    path = f"/v1/sessions/{SID}"
    client.replies[("PATCH", path)] = deque([{"id": SID, "effectiveSelection": selection}])
    gate = RequestGate("PATCH", path)
    client.request_hook = gate
    async with app.run_test():
        await eventually(lambda: "ready" in app._status_state)
        app._session_loaded({"id": SID}, [], "")
        try:
            app._model_answered(selection, SID)
            await eventually(gate.entered.is_set)
            assert "model-b" not in footer(app, "session-info")
        finally:
            gate.release.set()
        await eventually(lambda: not app._session_busy)
        assert "model: model-b" in footer(app, "session-info")
        assert "thinking: on" in footer(app, "thinking-info")
        app._selection_updated({"effectiveSelection": {"mode": "balanced"}}, OTHER_SID)
        assert "model: model-b" in footer(app, "session-info")


async def test_session_change_and_new_clear_usage_and_model_metadata(
        monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test():
        await eventually(lambda: "ready" in app._status_state)
        session = {"id": SID, "effectiveSelection": {"mode": "balanced", "thinking": False}}
        app._session_loaded(session, [], "")
        app._turn_started(RID)
        app._finalize_run("run.completed", {"usage": {"inputTokens": 100}})
        # Reloading the same session retains the observed usage; switching doesn't.
        app._session_loaded(session, [], "")
        assert "input: 100" in footer(app, "usage-info")
        app._session_loaded({"id": OTHER_SID}, [], "")
        assert footer(app, "usage-info") == "last run tokens: unavailable"
        assert "balanced" not in footer(app, "session-info")
        app._start_fresh("")
        assert "session new" in footer(app, "session-info")
        assert footer(app, "usage-info") == "last run tokens: unavailable"
        assert "thinking: unavailable" in footer(app, "thinking-info")


@pytest.mark.parametrize("kind", ["run.completed", "run.failed", "run.cancelled"])
async def test_missing_usage_does_not_show_previous_run_tokens(
        monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test():
        await eventually(lambda: "ready" in app._status_state)
        app._session_loaded({"id": SID}, [], "")
        app._turn_started(RID)
        app._finalize_run("run.completed", {"usage": {"inputTokens": 100}})
        app._turn_started(RID)
        assert "input: 100" in footer(app, "usage-info")  # last finished run, not live
        app._finalize_run(kind, {})
        assert footer(app, "usage-info") == "last run tokens: unavailable"


@pytest.mark.parametrize("usage, expected", [
    (None, ""),
    ({"inputTokens": True, "outputTokens": -1, "totalTokens": "30"}, ""),
    ({"inputTokens": float("nan"), "outputTokens": float("inf")}, ""),
    ({"inputTokens": 0, "outputTokens": 5, "other": "secret"}, "input: 0, output: 5"),
])
def test_only_nonnegative_integer_token_counts_are_displayed(usage: Any, expected: str) -> None:
    assert WispTuiApp._usage_line(usage) == expected


@pytest.mark.parametrize("width", [40, 100])
async def test_footer_is_bounded_transparent_and_survives_resize(
        monkeypatch: pytest.MonkeyPatch, width: int) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test(size=(width, 24)) as pilot:
        await eventually(lambda: "ready" in app._status_state)
        app._session_loaded({"id": SID, "effectiveSelection": {
            "mode": "model", "model": "model[/]x\n\x1b[31m" + "x" * 200,
            "thinking": True}}, [], "")
        app._launch_dir = "/example/project[/]\n\x1b[31m" + "x" * 1000
        app._render_metadata()
        for new_width in (width, 40 if width == 100 else 100):
            await pilot.resize_terminal(new_width, 24)
            await pilot.pause()
            bottom = app.query_one("#bottom")
            assert 8 <= bottom.region.height <= 10
            assert bottom.region.bottom == 24
            assert app.query_one("#input").region.y >= 0
            assert app.query_one("#chat").region.height >= 14
            for name in ("session-info", "thinking-info", "usage-info", "location"):
                widget = app.query_one(f"#{name}", Static)
                assert widget.styles.background.a == 0
                assert widget.region.x == 1
                assert widget.region.right <= new_width - 1
                assert "\x1b" not in footer(app, name) and "\n" not in footer(app, name)
            assert "context: unavailable" in footer(app, "thinking-info")
            assert "model[/]x" in footer(app, "session-info")
