"""Chat UX additions: search, prompt jumps, tool expand, wheel acceleration."""

from __future__ import annotations

import io
import time
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from rich.console import Console
from textual.pilot import Pilot
from textual.widgets import Input

from tests.test_app import eventually, make_app, ready
from wisp_tui.app import WispTuiApp
from wisp_tui.chat_view import ChatItem, ChatScroll, ScrollOverlay, SearchBar
from wisp_tui.scrollback import render_item


def paragraph_rows(count: int) -> str:
    """Tall content: Markdown merges single newlines into one paragraph."""
    return "\n\n".join(f"row {i}" for i in range(count))


def tool_text(rows: int = 12) -> str:
    return "bash_run echo hi\n" + "\n".join(f"line {i}" for i in range(rows)) + "\nok · 1.0s"


def test_tool_tail_collapsed_note_and_expansion() -> None:
    console = Console(file=io.StringIO(), width=80)
    with console.capture() as capture:
        console.print(render_item("tool-call-ok", tool_text()))
    collapsed = capture.get()
    assert "ctrl+o to expand" in collapsed
    assert "line 0" not in collapsed

    with console.capture() as capture:
        console.print(render_item("tool-call-ok", tool_text(), expanded=True))
    expanded = capture.get()
    assert "ctrl+o" not in expanded
    assert "line 0" in expanded
    assert "line 11" in expanded


def test_short_tool_output_has_no_expand_note() -> None:
    console = Console(file=io.StringIO(), width=80)
    with console.capture() as capture:
        console.print(render_item("tool-call-ok", tool_text(rows=2)))
    assert "ctrl+o" not in capture.get()


# --- wheel acceleration (unit; no app needed) ---


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        self.now += 0.01
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(time, "monotonic", fake.monotonic)
    return fake


def test_wheel_rows_grow_with_fast_spins_and_cap(clock: FakeClock) -> None:
    chat = ChatScroll()
    assert chat._wheel_rows(False) == 1
    for expected in range(2, chat.WHEEL_MAX_ROWS + 1):
        assert chat._wheel_rows(False) == expected
    assert chat._wheel_rows(False) == chat.WHEEL_MAX_ROWS


def test_wheel_rows_reset_after_a_pause(clock: FakeClock) -> None:
    chat = ChatScroll()
    for _ in range(5):
        chat._wheel_rows(False)
    clock.now += chat.WHEEL_WINDOW + 1
    assert chat._wheel_rows(False) == 1


def test_wheel_rows_alt_is_a_fixed_multiplier(clock: FakeClock) -> None:
    chat = ChatScroll()
    assert chat._wheel_rows(True) == chat.ALT_WHEEL_ROWS


# --- app-level behavior ---


@pytest.fixture
async def app_pilot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> AsyncGenerator[tuple[WispTuiApp, Pilot[None]]]:
    launch = tmp_path / "project"
    launch.mkdir(parents=True)
    monkeypatch.chdir(launch)
    app, _client = make_app([], monkeypatch)
    async with app.run_test(size=(80, 24)) as pilot:
        await eventually(lambda: ready(app))
        await pilot.pause()
        yield app, pilot


async def test_wheel_scroll_moves_and_alt_multiplies(
    app_pilot: tuple[WispTuiApp, Pilot[None]],
) -> None:
    app, pilot = app_pilot
    chat = app.query_one("#chat", ChatScroll)
    app._mount_chat(ChatItem("assistant", paragraph_rows(60)))
    await pilot.pause()
    await pilot.pause()
    assert chat.max_scroll_y > 0
    chat.scroll_end(animate=False)
    await pilot.pause()
    top = chat.scroll_target_y
    chat.wheel_scroll(-1)
    small = top - chat.scroll_target_y
    assert small > 0
    chat.wheel_scroll(-1, alt=True)
    large = chat.scroll_target_y
    assert large < top - small  # one Alt turn covers at least the small step


async def test_jump_prompt_walks_user_prompts(
    app_pilot: tuple[WispTuiApp, Pilot[None]],
) -> None:
    app, pilot = app_pilot
    chat = app.query_one("#chat", ChatScroll)
    app._mount_chat(ChatItem("user", "first prompt"))
    app._mount_chat(ChatItem("assistant", paragraph_rows(30)))
    app._mount_chat(ChatItem("user", "second prompt"))
    app._mount_chat(ChatItem("assistant", paragraph_rows(30)))
    await pilot.pause()
    await pilot.pause()
    rows = chat.prompt_rows()
    assert len(rows) == 2
    assert chat.max_scroll_y > rows[1]
    chat.scroll_end(animate=False)
    await pilot.pause()
    chat.jump_prompt(-1)
    await pilot.pause()
    assert chat.scroll_target_y == rows[1]
    chat.jump_prompt(-1)
    await pilot.pause()
    assert chat.scroll_target_y == rows[0]
    chat.jump_prompt(1)
    await pilot.pause()
    assert chat.scroll_target_y == rows[1]
    # Past the last prompt, down follows new output again.
    chat.jump_prompt(1)
    await pilot.pause()
    assert chat.scroll_target_y == chat.max_scroll_y


async def test_search_marks_steps_and_wraps(
    app_pilot: tuple[WispTuiApp, Pilot[None]],
) -> None:
    app, pilot = app_pilot
    chat = app.query_one("#chat", ChatScroll)
    app._mount_chat(ChatItem("assistant", "the needle is here"))
    app._mount_chat(ChatItem("assistant", "nothing relevant"))
    app._mount_chat(ChatItem("assistant", "another Needle appears"))
    await pilot.pause()
    await pilot.pause()
    assert chat.search("needle") is True
    first = chat.search_mark
    assert first is not None and first[0].x > 0
    assert chat.search("needle", step=True) is True
    assert chat.search_mark is not None and chat.search_mark[0].y > first[0].y
    assert chat.search("needle", step=True, backwards=True) is True
    assert chat.search_mark == first
    # Stepping again wraps around to the first hit.
    assert chat.search("needle", step=True) is True
    assert chat.search_mark is not None and chat.search_mark[0].y > first[0].y
    assert chat.search("needle", step=True) is True
    assert chat.search_mark == first
    assert chat.search("absent") is False
    assert chat.search("") is True
    assert chat.search_mark is None


async def test_search_bar_open_typing_and_close(
    app_pilot: tuple[WispTuiApp, Pilot[None]],
) -> None:
    app, pilot = app_pilot
    chat = app.query_one("#chat", ChatScroll)
    app._mount_chat(ChatItem("assistant", "a needle to find"))
    await pilot.pause()
    await pilot.pause()
    app.action_chat_search()
    await pilot.pause()
    bar = app.query_one("#search-bar", SearchBar)
    assert bar.display
    input_widget = app.query_one("#search-input", Input)
    focused: object = app.focused
    assert focused is input_widget
    for character in "needle":
        await pilot.press(character)
    await pilot.pause()
    assert chat.search_mark is not None
    assert str(app.query_one("#search-note").render()) == ""
    input_widget.value = "absent"
    await pilot.pause()
    assert str(app.query_one("#search-note").render()) == "no matches"
    input_widget.value = "needle"
    await pilot.pause()
    await pilot.press("escape")
    await pilot.pause()
    assert not bool(bar.display)
    mark: object = chat.search_mark
    assert mark is None
    assert app.focused is app._prompt_input


async def test_expand_tool_toggles_the_newest_entry(
    app_pilot: tuple[WispTuiApp, Pilot[None]],
) -> None:
    app, pilot = app_pilot
    app._mount_chat(ChatItem("tool-call-ok", tool_text()))
    app._mount_chat(ChatItem("assistant", "after the tool"))
    await pilot.pause()
    await pilot.pause()
    app.action_chat_expand_tool()
    await pilot.pause()
    tool = next(
        child
        for child in reversed(app.query_one("#chat", ChatScroll).children)
        if isinstance(child, ChatItem) and child.kind.startswith("tool-call")
    )
    assert tool.expanded is True
    app.action_chat_expand_tool()
    await pilot.pause()
    assert tool.expanded is False


async def test_scrollbar_overlay_only_when_scrollable(
    app_pilot: tuple[WispTuiApp, Pilot[None]],
) -> None:
    app, pilot = app_pilot
    overlay = app.query_one("#chat-scrollbar", ScrollOverlay)
    overlay.set_shown(True)
    await pilot.pause()
    assert not bool(overlay.display)  # content fits: no scrollbar
    app._mount_chat(ChatItem("assistant", paragraph_rows(60)))
    await pilot.pause()
    await pilot.pause()
    overlay.set_shown(True)
    await pilot.pause()
    assert overlay.display
    rendered = str(overlay.render())
    assert "│" in rendered
    overlay.set_shown(False)
    await pilot.pause()
    assert not bool(overlay.display)
