"""Exercise tool folding with real terminal keys, no runtime or private config."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from textual.widgets import Collapsible, Static

from tests.helpers import ScriptedClient
from wisp_tui.app import WispTuiApp
from wisp_tui.config import Config
from wisp_tui.editor import PromptInput
from wisp_tui.sessions import SessionStore
from wisp_tui.submissions import SubmissionStore
from wisp_tui.tool_view import ToolCallView


class ToolFixture(WispTuiApp):
    expanded_checked = False
    collapsed_checked = False

    def on_mount(self) -> None:
        super().on_mount()
        self.query_one("#input", PromptInput).value = "keep draft"
        tool = self._tool_event({"name": "scripted_tool[/]", "args": {"path": "/example/file"},
                                 "ok": True}, completed=True)
        tool.apply_history(output_text="SCRIPTED_TOOL_OUTPUT [/]")
        self.call_after_refresh(self.ready)

    def ready(self) -> None:
        self.query_one("CollapsibleTitle", Static).focus()
        self._status("TOOL_READY")

    def on_collapsible_expanded(self, event: Collapsible.Expanded) -> None:
        if isinstance(event.collapsible, ToolCallView):
            self.expanded_checked = True
            self._status("TOOL_EXPANDED")

    def on_collapsible_collapsed(self, event: Collapsible.Collapsed) -> None:
        if isinstance(event.collapsible, ToolCallView) and self.expanded_checked:
            self.collapsed_checked = True
            self._status("TOOL_COLLAPSED")
            self.set_timer(0.1, self.exit)


def main() -> None:
    with tempfile.TemporaryDirectory() as state:
        client = ScriptedClient([])
        app = ToolFixture(client, Config(client.url, client.token),
                          sessions=SessionStore(Path(state) / "sessions"),
                          submissions=SubmissionStore(client.url, Path(state)))
        app.run(inline=True)
        assert all(method == "GET" for method, _, _ in client.calls)
        print("TOOL_RESULT=" + json.dumps({
            "expanded": app.expanded_checked, "collapsed": app.collapsed_checked,
            "draft": app._composer_draft}))


if __name__ == "__main__":
    main()
