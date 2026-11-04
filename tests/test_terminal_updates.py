"""Inline repaint correctness against a small relative-cursor terminal model."""

from __future__ import annotations

import re

import pytest
from rich.cells import get_character_cell_size
from rich.console import Console
from rich.segment import Segment
from rich.style import Style
from textual._compositor import InlineUpdate
from textual.strip import Strip

from wisp_tui.terminal import InlineRenderer


class Terminal:
    """Model only the cursor, SGR and DSR sequences used by inline updates.

    Origin starts below a shell prompt; writes at the right/bottom edges must
    not wrap or scroll it away. LF models the PTY's ordinary ONLCR translation.
    """

    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.cells = [[" "] * width for _ in range(height)]
        self.row = 2
        self.column = 0
        self.scrolls = 0

    def write(self, output: str) -> None:
        tokens = re.findall(r"\x1b\[[0-9;]*[A-Za-z]|[^\x1b]", output)
        for token in tokens:
            if token.startswith("\x1b["):
                code = token[-1]
                value = int(token[2:-1] or 1) if code in "ABCD" else 0
                if code == "A":
                    self.row -= value
                elif code == "B":
                    self.row += value
                elif code == "C":
                    self.column += value
                elif code == "D":
                    self.column -= value
                else:
                    assert code in ("m", "n"), f"unexpected control: {token!r}"
                continue
            if token == "\r":
                self.column = 0
                continue
            if token == "\n":
                self.row += 1
                self.column = 0
                continue
            width = get_character_cell_size(token)
            if width == 0:
                continue
            if self.column + width > self.width:
                self.row += 1
                self.column = 0
            if self.row >= len(self.cells):
                self.cells.pop(0)
                self.cells.append([" "] * self.width)
                self.row -= 1
                self.scrolls += 1
            self.cells[self.row][self.column] = token
            for offset in range(1, width):
                self.cells[self.row][self.column + offset] = ""
            self.column += width


def frame(lines: list[str], width: int, style: Style | None = None) -> InlineUpdate:
    return InlineUpdate([Strip([Segment(line, style)]).adjust_cell_length(width) for line in lines])


@pytest.mark.parametrize("width", [40, 100])
def test_changed_rows_match_full_frame_without_scrolling_or_losing_origin(width: int) -> None:
    console = Console(force_terminal=True, color_system="256", width=width)
    renderer = InlineRenderer()
    terminal = Terminal(width, 7)
    # The last app row is the physical terminal's bottom row.
    initial = frame(["Unchanged answer", "before", "unchanged middle", "old", "x" * width], width)
    terminal.write(renderer.render(initial, console, width))
    assert (terminal.row, terminal.column) == (2, 0)
    latest = frame(["Unchanged answer", "日本語", "unchanged middle", "short", "end"], width)
    delta = renderer.render(latest, console, width)
    assert "Unchanged answer" not in delta and "unchanged middle" not in delta
    assert "\n" not in delta
    terminal.write(delta)
    reference = Terminal(width, 7)
    reference.write(latest.render_segments(console))
    assert terminal.cells == reference.cells
    assert terminal.scrolls == 0
    assert (terminal.row, terminal.column) == (2, 0)
    # The input cursor moves between repaints; App._display returns it to the
    # same origin before serializing the next update.
    terminal.write("\x1b[4B\x1b[5C\x1b[5D\x1b[4A")
    terminal.write(renderer.render(initial, console, width))
    restored = Terminal(width, 7)
    restored.write(initial.render_segments(console))
    assert terminal.cells == restored.cells
    assert terminal.scrolls == 0
    assert (terminal.row, terminal.column) == (2, 0)


def test_style_only_changes_are_repainted_and_identical_rows_are_omitted() -> None:
    console = Console(force_terminal=True, color_system="256", width=40)
    renderer = InlineRenderer()
    plain = frame(["same text"], 40)
    renderer.render(plain, console, 40)
    changed = renderer.render(frame(["same text"], 40, Style(color="bright_blue")), console, 40)
    assert "same text" in changed and "\x1b[94m" in changed
    unchanged = renderer.render(frame(["same text"], 40, Style(color="bright_blue")), console, 40)
    assert "same text" not in unchanged
    assert len(unchanged) < 10


@pytest.mark.parametrize("change", ["width", "height", "shrink", "invalidate"])
def test_geometry_changes_and_explicit_clear_use_textual_full_update(change: str) -> None:
    console = Console(force_terminal=True, color_system="256", width=100)
    renderer = InlineRenderer()
    old = frame(["answer", "footer"], 40)
    assert renderer.render(old, console, 40) == old.render_segments(console)
    width = 100 if change == "width" else 40
    lines = ["answer", "footer", "new line"] if change == "height" else ["answer", "footer"]
    new = frame(lines, width)
    if change == "shrink":
        new = InlineUpdate(frame(["answer"], width).strips, clear=True)
    elif change == "invalidate":
        renderer.invalidate()
    assert renderer.render(new, console, width) == new.render_segments(console)


def test_modal_overlay_and_dismissal_restore_the_underlying_rows() -> None:
    console = Console(force_terminal=True, color_system="256", width=40)
    renderer = InlineRenderer()
    terminal = Terminal(40, 7)
    underlying = frame(["answer", "code", "input", "session", "help"], 40)
    overlay = frame(["answer", "APPROVAL", "tool: example", "y approve", "help"], 40)
    terminal.write(renderer.render(underlying, console, 40))
    terminal.write(renderer.render(overlay, console, 40))
    terminal.write(renderer.render(underlying, console, 40))
    reference = Terminal(40, 7)
    reference.write(underlying.render_segments(console))
    assert terminal.cells == reference.cells
    assert (terminal.row, terminal.column) == (2, 0)
    assert terminal.scrolls == 0
