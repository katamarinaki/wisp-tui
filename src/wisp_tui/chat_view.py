"""The chat view: finished items, then live ones that are still changing."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from rich.cells import cell_len
from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.geometry import Offset, Region, Size
from textual.message import Message
from textual.strip import Strip
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Input, Static

from .scrollback import render_item, settled_length
from .text import terminal_text


class ChatScroll(VerticalScroll):
    """Scrolls above the pinned composer; follows new output until scrolled up."""

    can_focus = False

    # Wheel acceleration, like Pi: fast spins cover more ground, Alt+wheel 5x.
    WHEEL_WINDOW = 0.3
    WHEEL_MAX_ROWS = 10
    ALT_WHEEL_ROWS = 5

    class Selected(Message):
        """A drag selection ended; ``text`` is what it covers."""

        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    class Clicked(Message):
        """A click without a drag landed on ``item``."""

        def __init__(self, item: Widget) -> None:
            super().__init__()
            self.item = item

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        # Drag selection as (anchor, focus) cells, y counted from the chat's top.
        self.selection: tuple[Offset, Offset] | None = None
        self._dragging = False
        # The current search hit, marked like a selection (reverse video).
        self.search_mark: tuple[Offset, Offset] | None = None
        self._search_query = ""
        self._wheel_times: list[float] = []
        self._scrollbar_hover = False

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
        self._repaint_marks(rows)

    def _repaint_marks(self, rows: list[int]) -> None:
        if not rows:
            return
        for child in self.children:
            region = child.virtual_region
            if region.y <= max(rows) and region.bottom > min(rows):
                if isinstance(child, SelectableStatic):
                    child.repaint_selection()
                else:
                    child.refresh()

    def clear_selection(self) -> None:
        if self.selection is not None:
            self._set_selection(None)

    def marked_columns(self, y: int, mark: tuple[Offset, Offset] | None) -> tuple[int, int] | None:
        """The marked cells ``[start, end)`` of content row ``y``, if any."""
        if mark is None or mark[0] == mark[1]:
            return None
        start, end = sorted(mark, key=lambda cell: (cell.y, cell.x))
        if not start.y <= y <= end.y:
            return None
        return (start.x if y == start.y else 0, end.x + 1 if y == end.y else self.size.width)

    def selected_columns(self, y: int) -> tuple[int, int] | None:
        """The selected cells ``[start, end)`` of content row ``y``, if any."""
        return self.marked_columns(y, self.selection)

    def _set_search_mark(self, mark: tuple[Offset, Offset] | None) -> None:
        rows = [cell.y for pair in (self.search_mark, mark) if pair for cell in pair]
        self.search_mark = mark
        self._repaint_marks(rows)

    def clear_search(self) -> None:
        """Drop the search highlight; the search bar calls this on close."""
        self._search_query = ""
        self._set_search_mark(None)

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

    # --- wheel, hover scrollbar, search, prompt jumps ---------------------

    def _wheel_rows(self, alt: bool) -> int:
        """Rows per wheel event: 1 normally, more on a fast spin, 5x with Alt."""
        now = time.monotonic()
        self._wheel_times = [
            stamp for stamp in self._wheel_times if now - stamp < self.WHEEL_WINDOW
        ]
        self._wheel_times.append(now)
        if alt:
            return self.ALT_WHEEL_ROWS
        return min(len(self._wheel_times), self.WHEEL_MAX_ROWS)

    def wheel_scroll(self, direction: int, *, alt: bool = False) -> None:
        rows = self._wheel_rows(alt) * self.app.scroll_sensitivity_y
        if self.allow_vertical_scroll:
            self._scroll_to(y=self.scroll_target_y + direction * rows, animate=False)

    def _on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        if event.ctrl or event.shift:
            super()._on_mouse_scroll_up(event)
        elif self.allow_vertical_scroll:
            self.wheel_scroll(-1, alt=event.meta)
            event.stop()

    def _on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        if event.ctrl or event.shift:
            super()._on_mouse_scroll_down(event)
        elif self.allow_vertical_scroll:
            self.wheel_scroll(1, alt=event.meta)
            event.stop()

    def _hover_scrollbar(self, shown: bool) -> None:
        if shown == self._scrollbar_hover:
            return
        self._scrollbar_hover = shown
        for overlay in self.screen.query(ScrollOverlay).results(ScrollOverlay):
            overlay.set_shown(shown)

    def prompt_rows(self) -> list[int]:
        """Content rows of the user prompts, in display order."""
        return sorted(
            child.virtual_region.y
            for child in self.children
            if isinstance(child, ChatItem) and child.kind == "user" and child.display
        )

    def jump_prompt(self, direction: int) -> None:
        """Jump to the previous (negative) or next user prompt; down at the end follows."""
        rows = self.prompt_rows()
        if not rows:
            return
        current = self.scroll_target_y
        if direction < 0:
            above = [row for row in rows if row < current]
            self.scroll_to(y=max(above) if above else rows[0], animate=False)
        else:
            below = [row for row in rows if row > current]
            if below:
                self.scroll_to(y=min(below), animate=False)
            else:
                self.scroll_end(animate=False)

    def _content_rows(self) -> list[tuple[int, list[Strip]]]:
        """Every shown row (content row, strip), in display order."""
        shown: list[tuple[int, list[Strip]]] = []
        for child in sorted(self.children, key=lambda item: item.virtual_region.y):
            if not child.display:
                continue
            region = child.virtual_region
            shown.append((region.y, child.render_lines(Region(0, 0, region.width, region.height))))
        return shown

    def search(self, query: str, *, step: bool = False, backwards: bool = False) -> bool:
        """Mark and scroll to the next case-insensitive match; True when one is shown.

        Without ``step`` the scan starts at the top of the view (every keystroke
        of the search bar); with it, after (or before) the current mark (Enter).
        The scan wraps around once.
        """
        needle = query.strip().lower()
        if not needle:
            self.clear_search()
            return True
        flat = [(row, strip) for row, strips in self._content_rows() for strip in strips]
        if not flat:
            self._set_search_mark(None)
            return False
        start = int(self.scroll_target_y)
        if step and self.search_mark is not None and self._search_query == needle:
            mark_row = self.search_mark[0].y
            start = mark_row - 1 if backwards else mark_row + 1
        if backwards:
            order = [item for item in flat if item[0] <= start][::-1] + [
                item for item in flat if item[0] > start
            ][::-1]
        else:
            order = [item for item in flat if item[0] >= start] + [
                item for item in flat if item[0] < start
            ]
        for row, strip in order:
            line = strip.text
            position = line.lower().rfind(needle) if backwards else line.lower().find(needle)
            if position < 0:
                continue
            columns = (cell_len(line[:position]), cell_len(line[: position + len(needle)]))
            self._search_query = needle
            self._set_search_mark((Offset(columns[0], row), Offset(columns[1], row)))
            # Pi centers the hit; a third from the top leaves context above it.
            self.scroll_to(y=max(0.0, row - self.size.height / 3), animate=False)
            return True
        return False

    def on_mouse_move(self, event: events.MouseMove) -> None:
        self._hover_scrollbar(True)
        if self._dragging and self.selection is not None:
            self._set_selection((self.selection[0], self._cell(event)))

    def on_mouse_leave(self, event: events.Leave) -> None:
        self._hover_scrollbar(False)

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
            click = self.selection
            self._set_selection(None)
            if click is not None and click[0] == click[1]:
                for child in self.children:
                    if child.display and child.virtual_region.contains_point(click[0]):
                        self.post_message(self.Clicked(child))
                        break

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

    _selection_repaint: tuple[Size, object] | None = None

    def repaint_selection(self) -> None:
        """Redraw the selection marks without rendering the content again."""
        # A pending refresh (new text) still has to render: the lines are stale.
        if not self._dirty_regions:
            self._selection_repaint = (self.size, self.visual)
        self.refresh()

    def _render_content(self) -> None:
        # Rendering a long Markdown answer takes ~0.1 s; doing it on every
        # mouse move froze dragging. Reuse the lines if only the marks moved.
        repaint, self._selection_repaint = self._selection_repaint, None
        if (
            repaint is not None
            and repaint[0] == self.size == self._render_cache.size
            and repaint[1] is self.visual
        ):
            self._dirty_regions.clear()
            return
        super()._render_content()

    def render_lines(self, crop: Region) -> list[Strip]:
        strips = super().render_lines(crop)
        chat = self.parent
        if not isinstance(chat, ChatScroll):
            return strips
        marks = [mark for mark in (chat.selection, chat.search_mark) if mark is not None]
        if not marks:
            return strips
        region = self.virtual_region
        shown = []
        for row, strip in enumerate(strips, region.y + crop.y):
            segments: list[Strip] = [strip]
            for mark in marks:
                columns = chat.marked_columns(row, mark)
                if columns is None:
                    continue
                start = max(0, columns[0] - region.x - crop.x)
                end = min(strip.cell_length, columns[1] - region.x - crop.x)
                if start >= end:
                    continue
                head, middle, tail = strip.divide([start, end, strip.cell_length])
                marked = Strip(Segment.apply_style(middle, post_style=_SELECTED))
                segments = [head, marked, tail]
                break
            shown.append(Strip.join(segments) if len(segments) > 1 else segments[0])
        return shown


class ChatItem(SelectableStatic):
    """One text item, drawn exactly as it is printed on exit."""

    def __init__(self, kind: str, text: str) -> None:
        super().__init__(markup=False)
        self.kind = kind
        self.text = terminal_text(text)
        # Leading part of ``text`` already moved to a finished item (streams only).
        self.committed = 0
        # Tool entries can expand to their full output (ctrl+o).
        self.expanded = False
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

    def entry(self) -> tuple[str, str]:
        return self.kind, self.remaining()

    def set_expanded(self, expanded: bool) -> None:
        """Show (or collapse) the full output of a tool entry."""
        if self.kind.startswith("tool-call") and self.expanded != expanded:
            self.expanded = expanded
            self._show()

    def _show(self) -> None:
        text = self.remaining().strip("\n")
        if text.strip():
            expanded = self.expanded if self.kind.startswith("tool-call") else False
            self.update(render_item(self.kind, text, expanded=expanded))
        else:
            self.update("")
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


class SearchBar(Horizontal):
    """Pi-style transcript search: Enter next, Shift+Enter previous, Esc closes."""

    class Closed(Message):
        """The user closed the search bar."""

    def __init__(self) -> None:
        super().__init__(id="search-bar")

    def compose(self) -> ComposeResult:
        yield Input(
            placeholder="Find — Enter: next · Shift+Enter: previous · Esc: close",
            id="search-input",
        )
        yield Static("", id="search-note", markup=False)

    def clear(self) -> None:
        """Empty the input and drop the highlight; the caller hides the bar."""
        self.query_one("#search-input", Input).value = ""
        self.query_one("#search-note", Static).update("")
        self.chat.clear_search()

    @property
    def chat(self) -> ChatScroll:
        return self.screen.query_one(ChatScroll)

    def _search(self, *, step: bool = False, backwards: bool = False) -> None:
        value = self.query_one("#search-input", Input).value
        found = self.chat.search(value, step=step, backwards=backwards)
        note = "" if found or not value.strip() else "no matches"
        self.query_one("#search-note", Static).update(note)

    def on_input_changed(self, event: Input.Changed) -> None:
        self._search()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._search(step=True)

    def on_key(self, event: events.Key) -> None:
        if event.key == "shift+enter":
            self._search(step=True, backwards=True)
            event.stop()
        elif event.key == "escape":
            self.post_message(self.Closed())
            event.stop()
            event.prevent_default()


class ScrollOverlay(Static):
    """A hover scrollbar overlaying the chat's right edge without reflowing it."""

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__("", markup=False, id=id)

    @property
    def chat(self) -> ChatScroll:
        return self.screen.query_one(ChatScroll)

    def set_shown(self, shown: bool) -> None:
        chat = self.chat
        self.display = shown and chat.max_scroll_y > 0
        self.refresh_thumb()

    def refresh_thumb(self) -> None:
        if not self.display:
            return
        chat = self.chat
        height = chat.container_size.height
        total = chat.virtual_size.height
        if height <= 0 or total <= height:
            self.display = False
            return
        thumb = max(1, round(height * height / total))
        position = min(
            max(0, round(chat.scroll_y / (total - height) * (height - thumb))),
            height - thumb,
        )
        # Track and thumb are glyphs with colors; no background is painted.
        track = Text("│" * height, style="dim")
        track.stylize("bold", position, position + thumb)
        self.update(track)

    def _on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        # The overlay covers the chat's last column; wheel events land here.
        self.chat.wheel_scroll(-1, alt=event.meta)
        event.stop()

    def _on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        self.chat.wheel_scroll(1, alt=event.meta)
        event.stop()
