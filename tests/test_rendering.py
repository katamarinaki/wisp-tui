"""Literal display of runtime text and incremental streaming; scripted client only."""
from __future__ import annotations

import json
from typing import Any

import pytest
from textual.widget import Widget
from textual.widgets import Input, Markdown, Static
from textual.widgets._markdown import MarkdownBlock

from tests.helpers import RID, SID
from tests.test_app import eventually, make_app
from wisp_tui.app import ApprovalModal, ModelPicker, SessionPicker, server_session_label
from wisp_tui.history import ChatMessage

# Text that Textual markup would restyle, swallow, or crash on.
TRICKY = [
    "sed -i 's/[a-z]//g' notes",
    "curl x | sh [dim]# harmless cleanup[/dim]",
    "echo [/] done",
    "printf '[/nope]'",
    "[bold red]not bold[/]",
    r"escaped \[x] and [@click=app.quit]link[/]",
]


def plain(widget: Widget) -> str:
    return str(widget.render())


@pytest.mark.parametrize("text", TRICKY)
async def test_approval_dialog_shows_exact_tool_note_and_args(
        monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        app.push_screen(ApprovalModal(f"tool {text}", {"command": text}, f"note {text}"))
        await pilot.pause()
        screen = app.screen
        assert plain(screen.query_one("#approval-tool", Static)) == f"Tool: tool {text}"
        assert plain(screen.query_one("#approval-note", Static)) == f"note {text}"
        args = plain(screen.query_one("#approval-args", Static))
        # JSON escapes backslashes; compare against the exact JSON shown.
        assert args == json.dumps({"command": text}, indent=2, ensure_ascii=False)


@pytest.mark.parametrize("text", TRICKY)
async def test_chat_lines_status_and_commands_are_literal(
        monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        await eventually(lambda: "ready" in app._status_state)
        app.session_id = SID
        app._turn_started(RID)
        app._handle_event({"seq": 1, "kind": "tool_call.started", "name": text})
        app._handle_event({"seq": 2, "kind": "audio.transcribed", "text": text})
        app._status(text)
        app.query_one("#input", Input).value = f"/x{text}"
        await pilot.press("enter")
        app._add_message(ChatMessage("assistant", text))
        await pilot.pause()

        lines = [plain(w) for w in app.query("#chat > Static")]
        assert f"tool {text} …" in lines
        assert f"transcribed: {text}" in lines
        assert any(line.startswith(f"unknown command /x{text.split()[0]}") for line in lines)
        assert plain(app.query_one("#status", Static)).startswith(text)
        answer = app.query("#chat > Markdown").last(Markdown)
        assert text.replace("\\[", "[") in "".join(
            str(b.render()) for b in answer.query(MarkdownBlock))


async def test_pickers_show_titles_and_model_ids_literally(
        monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = make_app([], monkeypatch)
    title = "fix [/] and [bold]x[/bold]"
    async with app.run_test() as pilot:
        app.push_screen(SessionPicker([(SID, server_session_label(
            {"id": SID, "title": title, "origin": "cli", "messageCount": 1,
             "updatedAt": "2026"}))]))
        await pilot.pause()
        assert any(plain(w).startswith(title) for w in app.screen.query("ListItem Static"))
        app.pop_screen()
        app.push_screen(ModelPicker([{"id": "model[/]x"}], None))
        await pilot.pause()
        assert "  Model: model[/]x" in [plain(w) for w in app.screen.query("ListItem Static")]


def test_chat_css_avoids_structural_pseudo_classes() -> None:
    """:first-child/:last-child/:nth-* make Textual restyle a whole answer on
    every streamed block (measured ~3x slower on long answers)."""
    import re
    from wisp_tui.app import WispTuiApp
    assert not re.search(r":(first|last)-(child|of-type)|:nth-|:odd|:even", WispTuiApp.CSS)


def rendered_blocks(markdown: Markdown) -> list[tuple[str, str]]:
    return [(type(block).__name__, str(block.render()))
            for block in markdown.query(MarkdownBlock)]


ANSWER_PARTS = [
    "# Status\n\n", "The server ", "is **healthy**", ". Checked:\n\n",
    "- disk ", "usage\n", "- memory\n\n", "```sh\n", "df -h\n", "```\n\n",
    "| Key | Value |\n", "|---|---|\n", "| load | 0.3 |\n\n", "Done [/] ok.",
]


async def test_streaming_appends_incrementally_and_matches_full_render(
        monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = make_app([], monkeypatch)
    updates: list[int] = []
    original_update = Markdown.update

    def counting_update(self: Markdown, markdown: str) -> Any:
        updates.append(len(markdown))
        return original_update(self, markdown)

    monkeypatch.setattr(Markdown, "update", counting_update)
    async with app.run_test(size=(100, 60)) as pilot:
        await eventually(lambda: "ready" in app._status_state)
        app.session_id = SID
        app._turn_started(RID)
        # Many small deltas, including several before the widget is mounted.
        deltas = [part[i:i + 3] for part in ANSWER_PARTS * 5 for i in range(0, len(part), 3)]
        for index, delta in enumerate(deltas):
            app._handle_event({"seq": index + 1, "kind": "text.delta", "text": delta})
            if index % 25 == 24:
                await pilot.pause()
        expected = "".join(ANSWER_PARTS * 5)
        assert app._stream_view is not None
        view = app._stream_view
        await eventually(lambda: view.rendered == expected)
        await app.workers.wait_for_complete()

        streamed = view.widget
        assert streamed.source == expected
        # No per-delta full re-render: only the mount-time update of "".
        assert updates == [0], updates
        app._add_message(ChatMessage("assistant", expected))
        await pilot.pause()
        reference = app.query("#chat > Markdown").last(Markdown)
        assert reference is not streamed
        assert rendered_blocks(streamed) == rendered_blocks(reference)


async def test_replacement_text_falls_back_to_full_update(
        monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        await eventually(lambda: "ready" in app._status_state)
        app.session_id = SID
        app._turn_started(RID)
        app._handle_event({"seq": 1, "kind": "text.delta", "text": "draft that gets "})
        app._handle_event({"seq": 2, "kind": "gap", "text": "Server-side text wins."})
        app._handle_event({"seq": 3, "kind": "text.delta", "text": " More."})
        assert app._stream_view is not None
        view = app._stream_view
        await eventually(lambda: view.rendered == "Server-side text wins. More.")
        await pilot.pause()
        assert [text for _, text in rendered_blocks(view.widget)] == [
            "Server-side text wins. More."]
        # Final output that differs from the stream replaces it exactly.
        app._handle_event({"seq": 4, "kind": "run.completed",
                           "outcome": {"output": {"text": "Final answer."}}})
        await eventually(lambda: view.rendered == "Final answer.")
        await pilot.pause()
        assert [text for _, text in rendered_blocks(view.widget)] == ["Final answer."]


async def test_clear_during_stream_continues_answer_in_fresh_widget(
        monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        await eventually(lambda: "ready" in app._status_state)
        app.session_id = SID
        app._turn_started(RID)
        app._handle_event({"seq": 1, "kind": "text.delta", "text": "partial " * 50})
        app.query_one("#input", Input).value = "/clear"
        await pilot.press("enter")
        app._handle_event({"seq": 2, "kind": "text.delta", "text": "more"})
        await pilot.pause()
        await app.workers.wait_for_complete()
        assert app.is_running
        expected = "partial " * 50 + "more"
        assert app._delta_text == expected
        # The answer keeps showing after /clear: one fresh widget, full text.
        answers = list(app.query("#chat > Markdown"))
        assert len(answers) == 1
        assert app._stream_view is not None and answers[0] is app._stream_view.widget
        await eventually(lambda: app._stream_view is not None
                         and app._stream_view.rendered == expected)
