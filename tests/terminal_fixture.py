"""Scripted terminal rendering fixture. Never reads real config or contacts Wisp."""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

from tests.helpers import RID, SID, ScriptedClient
from wisp_tui import __main__ as cli
from wisp_tui.app import ApprovalModal, WispTuiApp
from wisp_tui.config import Config
from wisp_tui.history import ChatMessage
from wisp_tui.sessions import SessionStore
from wisp_tui.submissions import SubmissionStore


class TerminalFixture(WispTuiApp):
    def on_mount(self) -> None:
        # A fixed short path keeps footer assertions independent of the checkout.
        self._launch_dir = "~/example"
        super().on_mount()
        self.set_timer(0.1, self.show_transcript)
        self.set_timer(0.3, self.finish_work)
        self.set_timer(0.45, self.show_approval)
        self.set_timer(0.8, self.exit)

    def show_transcript(self) -> None:
        self._session_loaded({"id": SID, "effectiveSelection": {
            "mode": "model", "model": "scripted-model", "thinking": True}},
            [], "", announce_empty=False)
        self._turn_started(RID)
        self._add_message(ChatMessage("user", "Show the server status"))
        self._add_message(ChatMessage("assistant", (
            "# Status\n\n**Scripted response**\n\nNo runtime contacted.\n\n"
            "- A list item with `inline code`\n\n"
            "```text\nplain code\n```\n\n"
            "| Key | Value |\n|---|---|\n| state | ready |")))

    def finish_work(self) -> None:
        self._finalize_run("run.completed", {
            "usage": {"inputTokens": 3, "outputTokens": 4}})

    def show_approval(self) -> None:
        self.push_screen(ApprovalModal("scripted_tool", {"target": "example"}))


def main() -> None:
    client = ScriptedClient([])
    # Local session records go to a throwaway directory, never the real state.
    with tempfile.TemporaryDirectory() as state:
        app = TerminalFixture(client, Config(url=client.url, token=client.token),
                              sessions=SessionStore(Path(state) / "sessions"),
                              submissions=SubmissionStore(client.url, Path(state)))
        with patch.object(cli, "load_config", return_value=app.config), \
                patch.object(cli, "WispClient", return_value=client), \
                patch.object(cli, "WispTuiApp", return_value=app):
            assert cli.main([]) == 0


if __name__ == "__main__":
    main()
