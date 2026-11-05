"""Exercise real bracketed-paste/key bytes against a fake runtime, never config."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from textual.widgets import TextArea
from textual.widgets.text_area import Selection

from tests.helpers import SID, ScriptedClient
from wisp_tui.app import WispTuiApp
from wisp_tui.config import Config
from wisp_tui.editor import PromptInput
from wisp_tui.sessions import SessionStore
from wisp_tui.submissions import SubmissionStore

PASTE = "/quit\r\n[example]\r\n/new other\r\n"
NORMALIZED = PASTE.replace("\r\n", "\n")
PROMPT = NORMALIZED + "\ntail"


class EditorTerminalFixture(WispTuiApp):
    client: ScriptedClient
    paste_checked = False
    newline_checked = False
    history_ready = False
    history_checked = False
    scratch_checked = False

    def on_mount(self) -> None:
        super().on_mount()
        self.session_id = SID
        self._set_status("EDITOR_READY")

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        super().on_text_area_changed(event)
        editor = self.query_one("#input", PromptInput)
        if self.history_ready:
            if editor.value == PROMPT:
                self.history_checked = True
                self._set_status("EDITOR_RECALL_READY")
            elif self.history_checked and editor.value == "":
                self.scratch_checked = True
                self.set_timer(0.1, self.exit)
        elif editor.value == NORMALIZED:
            assert all(method == "GET" for method, _, _ in self.client.calls)
            self.paste_checked = True
            # The capture must cover selected text and matching brackets, not
            # only an empty editor. Both should remain unpainted.
            editor.selection = Selection((0, 0), (1, 9))
            self._set_status("EDITOR_PASTE_READY")
        elif editor.value == PROMPT:
            assert all(method == "GET" for method, _, _ in self.client.calls)
            self.newline_checked = True
            self._set_status("EDITOR_NEWLINE_READY")

    def _finalize_run(self, kind: str, outcome: Any) -> None:
        super()._finalize_run(kind, outcome)
        self.history_ready = True
        self._set_status("EDITOR_HISTORY_READY")


def main() -> None:
    client = ScriptedClient(
        [
            {
                "seq": 1,
                "kind": "run.completed",
                "outcome": {"output": {"text": "Scripted editor response"}},
            }
        ]
    )
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        app = EditorTerminalFixture(
            client,
            Config(client.url, client.token),
            sessions=SessionStore(root / "sessions"),
            submissions=SubmissionStore(client.url, root),
        )
        app.run(inline=True)
        inputs = [
            body["input"]
            for method, path, body in client.calls
            if method == "POST" and path.endswith("/runs") and body is not None
        ]
        print(
            "EDITOR_RESULT="
            + json.dumps(
                {
                    "paste_checked": app.paste_checked,
                    "newline_checked": app.newline_checked,
                    "history_checked": app.history_checked,
                    "scratch_checked": app.scratch_checked,
                    "inputs": inputs,
                }
            )
        )


if __name__ == "__main__":
    main()
