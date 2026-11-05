"""Changed-row updates for Textual's pinned inline compositor.

Textual 8.2 writes the entire inline viewport on every repaint. In particular,
a spinner redraws a long, otherwise idle transcript ten times per second. Keep
its full-frame path for resize/height changes, and otherwise write only rows
whose rendered bytes changed, using movements relative to the inline origin.

Finished chat items are printed at the inline origin before a full frame, so
they scroll into the terminal's own scrollback above the app.
"""

from __future__ import annotations

from collections.abc import Callable

from rich.console import Console
from textual._compositor import InlineUpdate


class InlineRenderer:
    def __init__(self) -> None:
        self._rows: tuple[str, ...] = ()
        self._width = 0

    def invalidate(self) -> None:
        self._rows = ()
        self._width = 0

    def render(self, update: InlineUpdate, console: Console, width: int) -> str:
        rows = tuple(strip.render(console) for strip in update.strips)
        previous = self._rows
        if update.clear or width != self._width or len(rows) != len(previous):
            output = update.render_segments(console)
        else:
            # App._display has moved from the input cursor to the inline origin.
            # Never use absolute screen coordinates or newlines: they can scroll
            # the primary screen and invalidate that origin at the bottom row.
            parts: list[str] = []
            cursor_row = 0
            for row, (before, after) in enumerate(zip(previous, rows, strict=True)):
                if before == after:
                    continue
                if row > cursor_row:
                    parts.append(f"\x1b[{row - cursor_row}B")
                parts.append("\r")
                parts.append(after)
                cursor_row = row
            if cursor_row:
                parts.append(f"\x1b[{cursor_row}A")
            parts.append("\r\x1b[6n")
            output = "".join(parts)
        self._rows = rows
        self._width = width
        return output


class ChangedRowsUpdate(InlineUpdate):
    """Preserve App._display's cursor/sync handling while reducing its payload."""

    def __init__(
        self,
        update: InlineUpdate,
        renderer: InlineRenderer,
        width: int,
        take_scrollback: Callable[[], str],
    ) -> None:
        super().__init__(update.strips, clear=update.clear)
        self._update = update
        self._renderer = renderer
        self._width = width
        self._take_scrollback = take_scrollback

    def render_segments(self, console: Console) -> str:
        # Update the cache (and take scrollback) only when Textual actually
        # serializes a frame, not when App._display skips one.
        printed = self._take_scrollback()
        if not printed:
            return self._renderer.render(self._update, console, self._width)
        # From the origin: clear the old frame, print (scrolling the terminal
        # as needed), then draw a full frame at the new origin below.
        self._renderer.invalidate()
        return "\x1b[J" + printed + self._renderer.render(self._update, console, self._width)
