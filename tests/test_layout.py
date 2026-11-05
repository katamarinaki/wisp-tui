"""Chat layout and contrast regressions, using only the scripted client."""

from __future__ import annotations

import pytest
from textual.containers import VerticalScroll
from textual.widgets import Markdown

from tests.test_app import eventually, make_app
from wisp_tui.editor import PromptInput as Input
from wisp_tui.history import ChatMessage


@pytest.mark.parametrize("width", [40, 100])
async def test_chat_and_editor_share_a_compact_gutter(
    monkeypatch: pytest.MonkeyPatch, width: int
) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test(size=(width, 30)) as pilot:
        app._add_message(ChatMessage("user", "A short question"))
        app._add_message(ChatMessage("assistant", "A short answer."))
        app._add_message(ChatMessage("user", "A follow-up"))
        await pilot.pause()

        editor = app.query_one("#input", Input)
        user, follow_up = app.query("#chat > .user")
        answer = app.query_one("#chat > Markdown")
        paragraph = answer.query_one("MarkdownParagraph")
        assert editor.content_region.x == paragraph.content_region.x == 1
        assert user.region.x == 0
        assert user.region.width == width
        assert user.content_region.x == 2
        assert editor.content_region.width == answer.content_region.width == width - 2
        assert editor.styles.padding == (0, 0, 0, 0)
        assert editor.styles.border_top[0] == editor.styles.border_bottom[0] == "solid"
        assert editor.region.height == 3
        metadata_height = sum(
            app.query_one(f"#{name}").region.height
            for name in ("summary", "location-row", "status")
        )
        assert app.query_one("#bottom").region.height == 3 + metadata_height
        assert editor.region.y == app.query_one("#bottom").region.y
        # Exactly one blank row around the answer text.
        assert paragraph.region.y == user.region.bottom + 1
        assert follow_up.region.y == paragraph.region.bottom + 1


@pytest.mark.parametrize("width", [40, 100])
async def test_prompt_box_wraps_literal_text_and_resizes(
    monkeypatch: pytest.MonkeyPatch, width: int
) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test(size=(width, 30)) as pilot:
        prompt = "[bold]Keep these brackets[/bold]\n" + "A long task description. " * 8
        app._add_message(ChatMessage("user", prompt))
        user = app.query_one("#chat > .user")
        await eventually(lambda: user.region.width == width and user.content_region.height > 2)

        assert str(user.render()) == prompt
        # Muted dark teal fill with fixed light text (exact xterm-256 colors).
        assert user.styles.background.rgb == (0, 95, 95)
        assert user.styles.color.rgb == (218, 218, 218)
        assert user.rich_style.bgcolor is not None
        assert user.rich_style.bgcolor.get_truecolor().hex == "#005f5f"
        assert all(border[0] in ("", "none") for border in user.styles.border)
        assert user.styles.padding == (1, 2, 1, 2)
        assert user.content_region.width == width - 4
        assert user.region.height == user.content_region.height + 2
        assert user.content_region.height > 2

        old_height = user.region.height
        await pilot.resize_terminal(30, 30)
        await eventually(lambda: user.region.width == 30 and user.region.height > old_height)
        assert user.region.x == 0
        assert user.region.width == 30
        assert user.content_region.width == 26
        assert user.region.height > old_height
        assert str(user.render()) == prompt


@pytest.mark.parametrize("width", [40, 100])
async def test_full_width_prompts_keep_scrolling_without_a_visible_scrollbar(
    monkeypatch: pytest.MonkeyPatch, width: int
) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test(size=(width, 20)):
        for index in range(12):
            app._add_message(ChatMessage("user", f"Prompt {index}"))
            app._add_message(ChatMessage("assistant", "A short answer."))
        chat = app.query_one("#chat", VerticalScroll)
        await eventually(
            lambda: (
                chat.max_scroll_y > 0
                and all(prompt.region.width == width for prompt in app.query("#chat > .user"))
            )
        )
        assert chat.max_scroll_y > 0
        assert chat.scrollbar_size_vertical == chat.scrollbar_size_horizontal == 0
        assert chat.scrollable_content_region.width == width
        assert chat.virtual_size.width == width
        for prompt in app.query("#chat > .user"):
            assert prompt.region.x == 0
            assert prompt.region.width == width
        chat.scroll_home(animate=False)
        await eventually(lambda: chat.scroll_y == 0)
        chat.scroll_end(animate=False)
        await eventually(lambda: chat.scroll_y == chat.max_scroll_y)


@pytest.mark.parametrize("theme", ["textual-dark", "textual-light"])
async def test_answers_use_terminal_foreground_without_nested_spacing(
    monkeypatch: pytest.MonkeyPatch, theme: str
) -> None:
    app, _ = make_app([], monkeypatch)
    app.theme = theme
    async with app.run_test(size=(100, 35)) as pilot:
        app._add_message(
            ChatMessage(
                "assistant",
                (
                    "# Server status\n\nReadable **answer** with `inline code`.\n\n"
                    "- First item\n- Second item\n\n"
                    "```text\nplain code\n```\n\n"
                    "| Key | Value |\n|---|---|\n| state | ready |"
                ),
            )
        )
        await pilot.pause()

        for widget in app.query(
            "#chat Markdown, #chat MarkdownBlock, #chat MarkdownBullet, "
            "#chat MarkdownTableContent > .header"
        ):
            assert widget.styles.color.ansi == -1, type(widget).__name__
            assert widget.rich_style.color is not None
            assert widget.rich_style.color.is_default
            assert not widget.rich_style.dim

        heading = app.query_one("MarkdownH1")
        assert heading.styles.content_align == ("left", "middle")
        assert heading.region.x == 1
        assert heading.styles.margin == (0, 0, 1, 0)
        paragraph = app.query_one("Markdown > MarkdownParagraph")
        inline_color = paragraph.get_component_rich_style("code_inline").color
        assert inline_color is not None and inline_color.is_default
        for nested in app.query("#chat Markdown Label, #chat MarkdownTableCellContents"):
            assert nested.styles.margin == (0, 0, 0, 0)
        fence = app.query_one("MarkdownFence")
        assert fence.region.height == 1
        assert fence.query_one("Label").content_region.x == 1


async def test_streamed_and_saved_answers_stay_aligned_after_resize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test(size=(100, 30)) as pilot:
        text = "An answer long enough to wrap across several lines in a narrow terminal."
        app._add_message(ChatMessage("assistant", text))
        view = app._begin_stream(text[:10])
        view.set_text(text)
        await app.workers.wait_for_complete()
        streamed = view.widget
        assert streamed.source == text
        await pilot.resize_terminal(40, 30)
        saved = app.query_one("#chat > Markdown", Markdown)

        def reflowed() -> bool:
            saved_text = saved.query("MarkdownParagraph")
            streamed_text = streamed.query("MarkdownParagraph")
            return (
                saved.content_region.width == streamed.content_region.width == 38
                and saved.region.height == streamed.region.height
                and bool(saved_text)
                and bool(streamed_text)
                and saved_text.first().region.height > 1
                and streamed_text.first().region.y == saved_text.first().region.bottom + 1
            )

        await eventually(reflowed)
        assert saved.content_region.x == streamed.content_region.x == 1
        assert saved.content_region.width == streamed.content_region.width == 38
        assert saved.region.height == streamed.region.height
        saved_text = saved.query_one("MarkdownParagraph").region
        streamed_text = streamed.query_one("MarkdownParagraph").region
        assert saved_text.height > 1
        assert streamed_text.y == saved_text.bottom + 1
