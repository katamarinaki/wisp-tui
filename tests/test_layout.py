"""Chat layout and contrast regressions, using only the scripted client."""

from __future__ import annotations

import re

import pytest

from tests.test_app import eventually, make_app
from wisp_tui.app import WispTuiApp
from wisp_tui.chat_view import ChatItem
from wisp_tui.editor import PromptInput as Input
from wisp_tui.history import ChatMessage
from wisp_tui.scrollback import render_ansi


def printed_lines(items: list[tuple[str, str]], width: int) -> list[str]:
    """Uncolored scrollback rows; unstyled trailing padding is not printed."""
    return render_ansi(items, width, None).split("\r\n")


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
        assert editor.content_region.x == 1
        assert editor.content_region.width == width - 2
        assert editor.styles.padding == (0, 0, 0, 0)
        assert editor.styles.border_top[0] == editor.styles.border_bottom[0] == "solid"
        assert editor.region.height == 3
        metadata_height = sum(
            app.query_one(f"#{name}").region.height
            for name in ("summary", "location-row", "status")
        )
        assert app.query_one("#bottom").region.height == 3 + metadata_height
        assert editor.region.y == app.query_one("#bottom").region.y
        # Everything finished went to scrollback; the live area is empty.
        assert not app.query_one("#chat").children
        lines = printed_lines(app.transcript(), width)
        # Prompt blocks span the width with a 2-column inset; answers start at
        # column 1, like the editor text, with exactly one blank row around them.
        assert lines[:9] == [
            *("", "  A short question", "", ""),
            *(" A short answer.", ""),
            *("", "  A follow-up", ""),
        ]


@pytest.mark.parametrize("width", [40, 100])
async def test_prompt_box_wraps_literal_text_and_keeps_its_fill(
    monkeypatch: pytest.MonkeyPatch, width: int
) -> None:
    prompt = "[bold]Keep these brackets[/bold]\n" + "A long task description. " * 8
    for size in (width, 30):
        lines = printed_lines([("user", prompt)], size)[:-2]
        assert lines[0] == lines[-1] == ""
        joined = " ".join(line.strip() for line in lines)
        assert joined.strip().startswith("[bold]Keep these brackets[/bold] A long task")
        assert len(lines) > 4
        assert all(line.startswith("  ") and len(line) <= size - 2 for line in lines[1:-1])
    # Muted dark teal fill (xterm-256 #23) with fixed light text, full width.
    styled = render_ansi([("user", prompt)], width, "256")
    rows = re.sub(r"\x1b\[[0-9;]*m", "", styled).split("\r\n")[:-2]
    assert all(len(row) == width for row in rows)
    codes = re.findall(r"\x1b\[([0-9;]*)m", styled)
    filled = [code for code in codes if code != "0"]
    # Rich quantizes #dadada to a 250-255 grey in 256-color mode.
    assert filled and all(re.search(r"48;5;23\b", code) for code in filled)
    assert all(re.search(r"38;5;25[0-5]\b", code) for code in filled)


@pytest.mark.parametrize("colors", ["256", "truecolor"])
def test_answers_use_terminal_foreground_without_fills(colors: str) -> None:
    answer = (
        "# Server status\n\nReadable **answer** with `inline code`.\n\n"
        "- First item\n- Second item\n\n"
        "```text\nplain code\n```\n\n"
        "| Key | Value |\n|---|---|\n| state | ready |"
    )
    styled = render_ansi([("assistant", answer)], 100, colors)
    codes = [code.split(";") for code in re.findall(r"\x1b\[([0-9;]*)m", styled)]
    # No painted background and no fixed foreground colors: default + bold/dim.
    assert all(part in ("0", "1", "2", "49", "39") for code in codes for part in code), codes
    lines = [line.rstrip() for line in printed_lines([("assistant", answer)], 100)]
    assert lines[0] == " Server status"
    assert " Readable answer with inline code." in lines
    assert " plain code" in lines


async def test_streamed_answer_keeps_the_printed_layout_after_resize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test(size=(100, 30)) as pilot:
        text = "An answer long enough to wrap across several lines in a narrow terminal."
        view = app._stream_view = app._begin_stream(text[:10])
        view.set_text(text)
        view.flush()
        streamed = view.widget
        assert isinstance(streamed, ChatItem) and streamed.text == text
        await pilot.resize_terminal(40, 30)
        expected = len(printed_lines([("assistant", text)], 40)) - 2

        await eventually(lambda: streamed.region.width == 40 and streamed.region.height > 1)
        # The live answer renders exactly as it will be printed to scrollback.
        assert streamed.region.x == 0
        assert streamed.region.height == expected


async def test_resize_reprints_the_newest_printed_items_at_the_new_width(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(WispTuiApp, "REPRINT_ITEMS", 2)
    app, _ = make_app([], monkeypatch)
    async with app.run_test(size=(100, 30)) as pilot:
        await eventually(lambda: "ready" in app._status_text)
        for index in range(3):
            app._show_line(f"item {index} " + "word " * 12)
        await eventually(lambda: not app._sync_queued)
        hidden = app._printed_total - 2
        await pilot.resize_terminal(40, 30)
        await pilot.pause()
        output = app._take_scrollback()
        assert output.startswith("\x1b[H\x1b[2J\x1b[3J")
        rows = re.sub(r"\x1b\[[0-9;]*m", "", output.removeprefix("\x1b[H\x1b[2J\x1b[3J"))
        assert f"↑ {hidden} earlier item" in rows
        assert "item 0" not in rows and "item 1" in rows and "item 2" in rows
        assert all(len(row) <= 40 for row in rows.split("\r\n"))
        assert app._take_scrollback() == ""
