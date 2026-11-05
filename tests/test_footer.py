"""Persistent session metadata; no runtime, sockets, or private config access."""

from __future__ import annotations

from collections import deque
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Static

from tests.helpers import RID, SID, RequestGate
from tests.test_app import chat_text, eventually, make_app, text_of
from wisp_tui.app import WispTuiApp
from wisp_tui.editor import PromptInput as Input
from wisp_tui.text import usage_line

OTHER_SID = "af06ed54-82b0-45e6-af55-7c85ab55e7ea"


def footer(app: WispTuiApp, name: str) -> str:
    return text_of(app.query_one(f"#{name}", Static))


async def test_fresh_footer_and_full_status_explain_unavailable_data(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    launch = tmp_path / "project[/]"
    launch.mkdir(parents=True)
    monkeypatch.chdir(launch)
    app, client = make_app([], monkeypatch)
    async with app.run_test():
        await eventually(lambda: "ready" in app._status_text)
        assert "session new" in footer(app, "session-id")
        assert footer(app, "session-info") == "model: runtime default"
        assert not app.query_one("#thinking-info").display
        assert footer(app, "usage-info") == ""
        assert not app.query_one("#usage-info").display
        assert footer(app, "location") == str(launch)
        # A later chdir must not relabel the original launch directory.
        monkeypatch.chdir(tmp_path)
        app._render_metadata()
        assert str(launch) in footer(app, "location")
        app._handle_command("/status")
        await eventually(lambda: "not live" in chat_text(app))
        assert str(launch) in chat_text(app)
        assert "thinking: unavailable" in chat_text(app)
        assert "context: unavailable" in chat_text(app)
        assert "last run tokens: unavailable" in chat_text(app)
        assert "Wisp working directory: unavailable" in chat_text(app)
        assert "not live" in chat_text(app)
        assert client.calls == [("GET", "/v1/health", None)]


@pytest.mark.parametrize(
    "selection, model, thinking",
    [
        ({"mode": "model", "model": "model[/]x", "thinking": True}, "model: model[/]x", "on"),
        ({"mode": "balanced", "thinking": False}, "mode: balanced (auto)", "off"),
        ({"mode": "advanced"}, "mode: advanced (auto)", "unavailable"),
        ({"mode": "model", "thinking": "false"}, "model: not reported", "unavailable"),
        ({}, "model: runtime default", "unavailable"),
    ],
)
async def test_selection_stays_visible_through_run_status_changes(
    monkeypatch: pytest.MonkeyPatch, selection: dict[str, Any], model: str, thinking: str
) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test():
        await eventually(lambda: "ready" in app._status_text)
        app._session_loaded({"id": SID, "effectiveSelection": selection}, [], "")
        expected = footer(app, "session-info")
        assert model in expected
        if thinking == "unavailable":
            assert not app.query_one("#thinking-info").display
        else:
            assert f"thinking: {thinking}" in footer(app, "thinking-info")
        app._begin_run(RID)
        app._handle_event({"kind": "run.started"})
        assert footer(app, "session-info") == expected
        app._finalize_run("run.completed", {"usage": {"inputTokens": 3, "outputTokens": 4}})
        assert footer(app, "session-info") == expected
        assert footer(app, "usage-info") == "last run ↑3 ↓4"
        assert app.session_id == SID and app.active_run is None
        assert app._status_text == ""
        assert not app.query_one("#status").display


async def test_follow_up_uses_same_session_and_updates_last_run_not_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client = make_app([], monkeypatch)
    client.event_batches = deque(
        [
            [
                {
                    "seq": 1,
                    "kind": "run.completed",
                    "outcome": {
                        "output": {"text": "First answer"},
                        "usage": {"inputTokens": 10, "outputTokens": 20, "totalTokens": 30},
                    },
                }
            ],
            [
                {
                    "seq": 1,
                    "kind": "run.completed",
                    "outcome": {
                        "output": {"text": "Follow-up answer"},
                        "usage": {"inputTokens": 40, "outputTokens": 5},
                    },
                }
            ],
        ]
    )
    async with app.run_test() as pilot:
        await eventually(lambda: "ready" in app._status_text)
        editor = app.query_one("#input", Input)
        editor.value = "First question"
        await pilot.press("enter")
        await eventually(lambda: app._status_text == "")
        await eventually(lambda: app._follower is None)
        assert "↑10 ↓20 Σ30" in footer(app, "usage-info")
        editor.value = "And a follow-up?"
        await pilot.press("enter")
        await eventually(lambda: "↑40" in footer(app, "usage-info"))
        assert app._status_text == ""
        assert app.session_id == SID
        assert footer(app, "usage-info") == "last run ↑40 ↓5"
        assert "context" not in footer(app, "session-info")
        await eventually(
            lambda: "First answer" in chat_text(app) and "Follow-up answer" in chat_text(app)
        )
        answers = [text for kind, text in app.transcript() if kind == "assistant"]
        assert answers[-1] == "Follow-up answer"
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client = make_app([], monkeypatch)
    selection = {"mode": "model", "model": "model-b", "thinking": True}
    path = f"/v1/sessions/{SID}"
    client.replies[("PATCH", path)] = deque([{"id": SID, "effectiveSelection": selection}])
    gate = RequestGate("PATCH", path)
    client.request_hook = gate
    async with app.run_test():
        await eventually(lambda: "ready" in app._status_text)
        app._session_loaded({"id": SID}, [], "")
        try:
            app._model_answered(selection, SID)
            await eventually(gate.entered.is_set)
            assert "model-b" not in footer(app, "session-info")
        finally:
            gate.release.set()
        await eventually(lambda: not app._session_change_in_progress)
        assert "model: model-b" in footer(app, "session-info")
        assert "thinking: on" in footer(app, "thinking-info")
        app._selection_updated({"effectiveSelection": {"mode": "balanced"}}, OTHER_SID)
        assert "model: model-b" in footer(app, "session-info")


async def test_session_change_and_new_clear_usage_and_model_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test():
        await eventually(lambda: "ready" in app._status_text)
        session = {"id": SID, "effectiveSelection": {"mode": "balanced", "thinking": False}}
        app._session_loaded(session, [], "")
        app._begin_run(RID)
        app._finalize_run("run.completed", {"usage": {"inputTokens": 100}})
        # Reloading the same session retains the observed usage; switching doesn't.
        app._session_loaded(session, [], "")
        assert "↑100" in footer(app, "usage-info")
        app._session_loaded({"id": OTHER_SID}, [], "")
        assert footer(app, "usage-info") == ""
        assert not app.query_one("#usage-info").display
        assert "balanced" not in footer(app, "session-info")
        app._start_fresh("")
        assert "session new" in footer(app, "session-id")
        assert footer(app, "usage-info") == ""
        assert not app.query_one("#usage-info").display
        assert not app.query_one("#thinking-info").display


@pytest.mark.parametrize("kind", ["run.completed", "run.failed", "run.cancelled"])
async def test_missing_usage_does_not_show_previous_run_tokens(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test():
        await eventually(lambda: "ready" in app._status_text)
        app._session_loaded({"id": SID}, [], "")
        app._begin_run(RID)
        app._finalize_run("run.completed", {"usage": {"inputTokens": 100}})
        app._begin_run(RID)
        assert "↑100" in footer(app, "usage-info")  # last finished run, not live
        app._finalize_run(kind, {})
        assert footer(app, "usage-info") == ""
        assert not app.query_one("#usage-info").display


@pytest.mark.parametrize(
    "usage, expected",
    [
        (None, ""),
        ({"inputTokens": True, "outputTokens": -1, "totalTokens": "30"}, ""),
        ({"inputTokens": float("nan"), "outputTokens": float("inf")}, ""),
        ({"inputTokens": 0, "outputTokens": 5, "other": "secret"}, "input: 0, output: 5"),
        ({"inputTokens": 28606, "totalTokens": 28733}, "input: 28,606, total: 28,733"),
    ],
)
def test_only_nonnegative_integer_token_counts_are_displayed(usage: Any, expected: str) -> None:
    assert usage_line(usage) == expected


@pytest.mark.parametrize("width", [40, 100])
async def test_footer_is_bounded_transparent_and_survives_resize(
    monkeypatch: pytest.MonkeyPatch, width: int
) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test(size=(width, 24)) as pilot:
        await eventually(lambda: "ready" in app._status_text)
        app._session_loaded(
            {
                "id": SID,
                "effectiveSelection": {
                    "mode": "model",
                    "model": "model[/]x\n\x1b[31m" + "x" * 200,
                    "thinking": True,
                },
            },
            [],
            "",
        )
        app._launch_dir = "/example/project[/]\n\x1b[31m" + "x" * 1000
        app._render_metadata()
        names = ("session-info", "thinking-info", "session-id", "location")
        for new_width in (width, 40 if width == 100 else 100):
            await pilot.resize_terminal(new_width, 24)
            await eventually(
                lambda size=new_width: (
                    app.query_one("#bottom").region.width == size
                    and all(app.query_one(f"#{name}").region.right <= size - 1 for name in names)
                )
            )
            bottom = app.query_one("#bottom")
            assert 6 <= bottom.region.height <= 10
            assert bottom.region.bottom == 24
            assert app.query_one("#input").region.y >= 0
            for name in names:
                widget = app.query_one(f"#{name}", Static)
                assert widget.styles.background.a == 0
                assert widget.region.x >= 1
                assert widget.region.right <= new_width - 1
                assert "\x1b" not in footer(app, name) and "\n" not in footer(app, name)
            assert "context" not in footer(app, "session-info")
            assert "model[/]x" in footer(app, "session-info")


@pytest.mark.parametrize("theme", ["textual-dark", "textual-light"])
async def test_footer_contrast_and_summary_reflow(
    monkeypatch: pytest.MonkeyPatch, theme: str
) -> None:
    app, _ = make_app([], monkeypatch)
    app.theme = theme
    async with app.run_test(size=(100, 24)) as pilot:
        await eventually(lambda: "ready" in app._status_text)
        app._launch_dir = "~/projects/example"
        app._session_loaded(
            {"id": SID, "effectiveSelection": {"mode": "balanced", "thinking": False}},
            [],
            "",
            announce_empty=False,
        )
        app._begin_run(RID)
        app._finalize_run(
            "run.completed",
            {"usage": {"inputTokens": 28606, "outputTokens": 127, "totalTokens": 28733}},
        )
        for width in (100, 40, 100):
            await pilot.resize_terminal(width, 24)
            await eventually(
                lambda size=width: (
                    app.query_one("#bottom").region.width == size
                    and app.query_one("#bottom").region.height == (5 if size == 100 else 6)
                )
            )
            usage = app.query_one("#usage-info", Static)
            selection = app.query_one("#session-info", Static)
            status = app.query_one("#status", Static)
            assert footer(app, "usage-info") == "last run ↑28,606 ↓127 Σ28,733"
            assert footer(app, "session-info") == "mode: balanced (auto)"
            assert footer(app, "thinking-info") == " · thinking: off"
            for widget in (
                usage,
                selection,
                status,
                app.query_one("#location"),
                app.query_one("#thinking-info"),
                app.query_one("#session-id"),
            ):
                assert widget.styles.background.a == 0
                assert not widget.rich_style.dim
            if width == 100:
                assert usage.region.y == selection.region.y
                assert usage.region.right <= selection.region.x
                assert selection.styles.text_align == "right"
                assert app.query_one("#bottom").region.height == 5
            else:
                assert usage.region.bottom == selection.region.y
                assert selection.styles.text_align == "left"
                assert app.query_one("#bottom").region.height == 6
            assert not status.display


@pytest.mark.parametrize("width", [40, 80, 100])
async def test_session_and_thinking_stay_visible_with_long_metadata(
    monkeypatch: pytest.MonkeyPatch, width: int
) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test(size=(width, 24)) as pilot:
        await eventually(lambda: "ready" in app._status_text)
        for thinking in (True, False):
            app._session_loaded(
                {
                    "id": SID,
                    "effectiveSelection": {
                        "mode": "model",
                        "model": "provider/model-with-a-reasonably-long-name",
                        "thinking": thinking,
                    },
                },
                [],
                "",
                announce_empty=False,
            )
            app._begin_run(RID)
            app._finalize_run("run.completed", {"usage": {"inputTokens": 28606}})
            for path in ("~/projects/example", "/example/" + "界" * 200):
                app._launch_dir = path
                app._render_metadata()
                await pilot.pause()
                location = app.query_one("#location", Static)
                session = app.query_one("#session-id", Static)
                model = app.query_one("#session-info", Static)
                setting = app.query_one("#thinking-info", Static)
                assert session.render_line(0).text.strip() == f"· session {SID[:8]}"
                assert setting.render_line(0).text.strip() == (
                    f"· thinking: {'on' if thinking else 'off'}"
                )
                assert location.region.width > 0
                assert location.region.right == session.region.x
                assert model.region.width > 0
                assert model.region.right == setting.region.x
                assert session.region.right == setting.region.right == width - 1
                assert session.region.height == setting.region.height == 1
                assert session.region.bottom <= model.region.y
                assert app.query_one("#bottom").region.bottom == 24
                if path.startswith("/example/"):
                    assert "…" in location.render_line(0).text
