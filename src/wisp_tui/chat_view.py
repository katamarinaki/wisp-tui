"""The chat view: finished items, then live ones that are still changing."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from rich.segment import Segment
from rich.style import Style
from textual import events
from textual.app import App
from textual.containers import VerticalScroll
from textual.geometry import Offset, Region
from textual.message import Message
from textual.strip import Strip
from textual.timer import Timer
from textual.widgets import Static

from .scrollback import render_item, settled_length
from .text import terminal_text


class ChatScroll(VerticalScroll):
    """Scrolls above the pinned composer; follows new output until scrolled up."""

    can_focus = False

    class Selected(Message):
        """A drag selection ended; ``text`` is what it covers."""

        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        # Drag selection as (anchor, focus) cells, y counted from the chat's top.
        self.selection: tuple[Offset, Offset] | None = None
        self._dragging = False

    def on_mount(self) -> None:
        self.anchor()

    def _cell(self, event: events.MouseEvent) -> Offset:
        x, y = event.screen_offset - self.region.offset
        x = min(max(x, 0), self.region.width - 1)
        y = min(max(y, 0), self.region.height - 1)
        return Offset(x, y + self.scroll_offset.y)

    def _set_selection(self, selection: tuple[Offset, Offset] | None) -> None:
        rows = [cell.y for pair in (self.selection, selection) if pair for cell in pair]
        self.selection = selection
        if rows:
            for child in self.children:
                region = child.virtual_region
                if region.y <= max(rows) and region.bottom > min(rows):
                    child.refresh()

    def clear_selection(self) -> None:
        if self.selection is not None:
            self._set_selection(None)

    def selected_columns(self, y: int) -> tuple[int, int] | None:
        """The selected cells ``[start, end)`` of content row ``y``, if any."""
        if self.selection is None or self.selection[0] == self.selection[1]:
            return None
        start, end = sorted(self.selection, key=lambda cell: (cell.y, cell.x))
        if not start.y <= y <= end.y:
            return None
        return (start.x if y == start.y else 0, end.x + 1 if y == end.y else self.size.width)

    def selected_text(self) -> str:
        if self.selection is None:
            return ""
        regions = [(child, child.virtual_region) for child in self.children if child.display]
        lines = []
        for y in range(
            min(cell.y for cell in self.selection), max(cell.y for cell in self.selection) + 1
        ):
            columns = self.selected_columns(y)
            text = ""
            for child, region in regions:
                if columns is not None and region.y <= y < region.bottom:
                    row = child.render_lines(Region(0, y - region.y, region.width, 1))[0]
                    text = row.crop(max(0, columns[0] - region.x), columns[1] - region.x).text
                    break
            lines.append(text.rstrip())
        return "\n".join(lines)

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if event.button == 1:
            cell = self._cell(event)
            self._set_selection((cell, cell))
            self._dragging = True
            self.capture_mouse()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if self._dragging and self.selection is not None:
            self._set_selection((self.selection[0], self._cell(event)))

    def on_mouse_up(self, event: events.MouseUp) -> None:
        if not self._dragging:
            return
        self._dragging = False
        self.release_mouse()
        text = self.selected_text()
        if text.strip():
            self.post_message(self.Selected(text))
        else:
            # A click clears the selection, as in Pi.
            self._set_selection(None)

    def page(self, direction: int) -> None:
        if direction < 0:
            self.scroll_page_up(animate=False)
        elif self.max_scroll_y - self.scroll_y <= self.size.height + 2:
            # The jump label takes a row from the chat while scrolled up, so a
            # page up then down falls short of the end; land on it and follow.
            self.scroll_end(animate=False)
        else:
            self.scroll_page_down(animate=False)


class JumpToLatest(Static):
    """Shown while the chat is scrolled up; a click follows new output again."""

    def on_click(self) -> None:
        self.screen.query_one(ChatScroll).scroll_end(animate=False)


# Reverse video marks the selection without painting a background.
_SELECTED = Style(reverse=True)


class SelectableStatic(Static):
    """A chat entry that shows the chat's drag selection."""

    def render_lines(self, crop: Region) -> list[Strip]:
        strips = super().render_lines(crop)
        chat = self.parent
        if not isinstance(chat, ChatScroll) or chat.selection is None:
            return strips
        region = self.virtual_region
        shown = []
        for row, strip in enumerate(strips, region.y + crop.y):
            columns = chat.selected_columns(row)
            start = end = 0
            if columns is not None:
                start = max(0, columns[0] - region.x - crop.x)
                end = min(strip.cell_length, columns[1] - region.x - crop.x)
            if start >= end:
                shown.append(strip)
                continue
            head, middle, tail = strip.divide([start, end, strip.cell_length])
            marked = Strip(Segment.apply_style(middle, post_style=_SELECTED))
            shown.append(Strip.join([head, marked, tail]))
        return shown


class ChatItem(SelectableStatic):
    """One text item, drawn exactly as it is printed on exit."""

    def __init__(self, kind: str, text: str) -> None:
        super().__init__(markup=False)
        self.kind = kind
        self.text = terminal_text(text)
        # Leading part of ``text`` already moved to a finished item (streams only).
        self.committed = 0
        self._show()

    def set_text(self, text: str) -> None:
        text = terminal_text(text)
        if not text.startswith(self.text[: self.committed]):
            # Replaced text above the printed part: show all of it again.
            self.committed = 0
        self.text = text
        self._show()

    def take_settled(self) -> str:
        """Remove and return the complete leading Markdown blocks."""
        start = self.committed
        end = start + settled_length(self.text[start:])
        self.committed = end
        if end > start:
            self._show()
        return self.text[start:end]

    def remaining(self) -> str:
        return self.text[self.committed :]

    def _show(self) -> None:
        text = self.remaining().strip("\n")
        self.update(render_item(self.kind, text) if text.strip() else "")
        self.display = bool(text.strip())


class StreamView:
    """Show one streamed answer, coalescing deltas into at most ten updates a second."""

    UPDATE_INTERVAL = 0.1

    def __init__(self, app: App[Any], widget: ChatItem, on_rendered: Callable[[], None]) -> None:
        self.app = app
        self.widget = widget
        self._on_rendered = on_rendered
        self.target = ""
        # Raw text last shown; sanitizing happens per update, not per delta.
        self.rendered = ""
        self._timer: Timer | None = None

    def set_text(self, text: str) -> None:
        self.target = text
        if self._timer is None and text != self.rendered:
            self._timer = self.app.set_timer(self.UPDATE_INTERVAL, self._apply)

    def flush(self) -> None:
        """Show the latest text now instead of at the next display batch."""
        if self._timer is not None:
            self._timer.stop()
        self._apply()

    def _apply(self) -> None:
        self._timer = None
        if self.rendered != self.target:
            self.rendered = self.target
            self.widget.set_text(self.target)
            self._on_rendered()
