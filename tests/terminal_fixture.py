"""Scripted terminal rendering fixture. Never reads real config or contacts Wisp."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

from textual.widgets import Markdown
from textual.widgets._markdown import MarkdownBlock, MarkdownTable

from tests.helpers import RID, SID, ScriptedClient
from tests.test_app import eventually
from wisp_tui import __main__ as cli
from wisp_tui.app import WispTuiApp
from wisp_tui.config import Config
from wisp_tui.history import ChatMessage
from wisp_tui.screens import ApprovalModal
from wisp_tui.sessions import SessionStore
from wisp_tui.submissions import SubmissionStore


class TerminalFixture(WispTuiApp):
    def on_mount(self) -> None:
        # A fixed short path keeps footer assertions independent of the checkout.
        self._launch_dir = "~/example"
        super().on_mount()
        self.run_worker(self.exercise())

    async def exercise(self) -> None:
        # Each step waits until the previous state has reached the terminal.
        self.show_transcript()
        answer = self.query_one("#chat > Markdown", Markdown)
        await eventually(
            lambda: (
                bool(answer.query(MarkdownTable))
                and all(block.is_mounted for block in answer.query(MarkdownBlock))
            )
        )
        await answer.wait_for_refresh()
        await self.screen.wait_for_refresh()
        self.finish_work()
        await self.screen.wait_for_refresh()
        modal = ApprovalModal("scripted_tool", {"target": "example"})
        await self.push_screen(modal)
        await modal.wait_for_refresh()
        self.exit()

    def show_transcript(self) -> None:
        self._session_loaded(
            {
                "id": SID,
                "effectiveSelection": {
                    "mode": "model",
                    "model": "scripted-model",
                    "thinking": True,
                },
            },
            [],
            "",
            announce_empty=False,
        )
        self._begin_run(RID)
        self._add_message(ChatMessage("user", "Show the server status"))
        self._add_message(
            ChatMessage(
                "assistant",
                (
                    "# Status\n\n**Scripted response**\n\nNo runtime contacted.\n\n"
                    "- A list item with `inline code`\n\n"
                    "```text\nplain code\n```\n\n"
                    "| Key | Value |\n|---|---|\n| state | ready |"
                ),
            )
        )

    def finish_work(self) -> None:
        self._finalize_run("run.completed", {"usage": {"inputTokens": 3, "outputTokens": 4}})


def main() -> None:
    client = ScriptedClient([])
    # Local session records go to a throwaway directory, never the real state.
    with tempfile.TemporaryDirectory() as state:
        app = TerminalFixture(
            client,
            Config(url=client.url, token=client.token),
            sessions=SessionStore(Path(state) / "sessions"),
            submissions=SubmissionStore(client.url, Path(state)),
        )
        with (
            patch.object(cli, "load_config", return_value=app.config),
            patch.object(cli, "WispClient", return_value=client),
            patch.object(cli, "WispTuiApp", return_value=app),
        ):
            assert cli.main([]) == 0


if __name__ == "__main__":
    main()
