"""A small multiline composer: explicit send, literal paste, terminal-default colors."""
from __future__ import annotations

from dataclasses import dataclass

from rich.style import Style
from textual import events
from textual.binding import Binding
from textual.message import Message
from textual.widgets import TextArea
from textual.widgets.text_area import TextAreaTheme


class PromptInput(TextArea):
    """Enter sends; Ctrl+J (also Shift+Enter when supported) inserts a newline.

    TextArea inserts bracketed paste as one document edit, never as key presses.
    No paste handler dispatches commands or submits text. ``value`` keeps the
    app's draft/acknowledgement interface independent of the editor document.
    """

    BINDINGS = [
        Binding("enter", "submit", show=False),
        Binding("ctrl+j,shift+enter", "newline", show=False),
        Binding("ctrl+home", "document_start", show=False),
        Binding("ctrl+end", "document_end", show=False),
        # TextArea normally copies here; Wi reserves Ctrl+C for cancel/exit.
        Binding("ctrl+c", "app.cancel", show=False),
    ]

    @dataclass
    class Submitted(Message):
        input: PromptInput
        value: str

        @property
        def control(self) -> PromptInput:
            return self.input

    def __init__(self, *, id: str = "input") -> None:
        super().__init__(id=id, soft_wrap=True, highlight_cursor_line=False,
                         placeholder="Message Wisp — Enter send · Ctrl+J newline · /help")
        # Explicit non-painting component styles also cover selections and
        # matching brackets; TextArea's default styles can emit filled cells.
        self.register_theme(TextAreaTheme(
            "terminal", cursor_style=Style(reverse=True), selection_style=Style(reverse=True),
            cursor_line_style=Style(), cursor_line_gutter_style=Style(),
            bracket_matching_style=Style(underline=True)))
        self.theme = "terminal"

    @property
    def value(self) -> str:
        return self.text

    @value.setter
    def value(self, text: str) -> None:
        self.load_text(text)
        self.move_cursor(self.document.end)

    async def _on_key(self, event: events.Key) -> None:
        # Consume these before TextArea's default Enter-to-newline handler.
        # Textual dispatches unhandled keys to the inherited editor handlers.
        actions = {
            "enter": self.action_submit,
            "ctrl+j": self.action_newline,
            "shift+enter": self.action_newline,
            "ctrl+home": self.action_document_start,
            "ctrl+end": self.action_document_end,
        }
        if action := actions.get(event.key):
            event.stop()
            event.prevent_default()
            action()

    async def _on_paste(self, event: events.Paste) -> None:
        # Consume the whole paste once; never bubble it back for forwarding.
        event.stop()
        event.prevent_default()
        await super()._on_paste(event)

    def action_document_start(self) -> None:
        self.move_cursor((0, 0))

    def action_document_end(self) -> None:
        self.move_cursor(self.document.end)

    def action_submit(self) -> None:
        if not self.read_only:
            self.post_message(self.Submitted(self, self.text))

    def action_newline(self) -> None:
        if not self.read_only:
            self.replace("\n", *self.selection, maintain_selection_offset=False)
