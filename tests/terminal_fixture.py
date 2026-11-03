"""Scripted terminal rendering fixture. Never reads real config or contacts Wisp."""
from __future__ import annotations

import tempfile
from pathlib import Path

from tests.helpers import ScriptedClient
from wisp_tui.app import ApprovalModal, WispTuiApp
from wisp_tui.config import Config
from wisp_tui.history import ChatMessage
from wisp_tui.sessions import SessionStore


class TerminalFixture(WispTuiApp):
    def on_mount(self) -> None:
        super().on_mount()
        self.set_timer(0.1, self.show_transcript)
        self.set_timer(0.3, self.show_approval)
        self.set_timer(0.6, self.exit)

    def show_transcript(self) -> None:
        self._add_message(ChatMessage("user", "Show the server status"))
        self._add_message(ChatMessage("assistant", (
            "# Status\n\n**Scripted response**\n\nNo runtime contacted.\n\n"
            "- A list item with `inline code`\n\n"
            "```text\nplain code\n```\n\n"
            "| Key | Value |\n|---|---|\n| state | ready |")))

    def show_approval(self) -> None:
        self.push_screen(ApprovalModal("scripted_tool", {"target": "example"}))


def main() -> None:
    client = ScriptedClient([])
    # Local session records go to a throwaway directory, never the real state.
    with tempfile.TemporaryDirectory() as state:
        app = TerminalFixture(client, Config(url=client.url, token=client.token),
                              sessions=SessionStore(Path(state) / "sessions"))
        app.run(inline=True, inline_no_clear=True)


if __name__ == "__main__":
    main()
