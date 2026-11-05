"""The live part of the chat: items that are not yet printed to scrollback."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from textual.app import App
from textual.containers import VerticalScroll
from textual.timer import Timer
from textual.widgets import Static

from .scrollback import render_item, settled_length
from .text import terminal_text


class ChatScroll(VerticalScroll):
    """Holds only live items; finished ones move to the terminal's scrollback."""

    can_focus = False

    def follow_latest(self) -> None:
        self.call_after_refresh(self.scroll_end, animate=False, immediate=True, x_axis=False)

    def on_resize(self) -> None:
        self.follow_latest()


class ChatItem(Static):
    """One text item, drawn exactly as it will be printed to scrollback."""

    def __init__(self, kind: str, text: str) -> None:
        super().__init__(markup=False)
        self.kind = kind
        self.text = terminal_text(text)
        # Leading part of ``text`` already printed to scrollback (streams only).
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
