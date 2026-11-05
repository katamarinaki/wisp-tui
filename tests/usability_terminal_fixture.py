"""Stream, resize and use all three dialogs with real terminal keys; no runtime."""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any

from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from tests.helpers import RID, SID, ScriptedClient
from tests.test_app import eventually
from wisp_tui.app import WispTuiApp
from wisp_tui.config import Config
from wisp_tui.editor import PromptInput
from wisp_tui.screens import ApprovalModal, ModelPicker, SessionPicker
from wisp_tui.sessions import SessionStore
from wisp_tui.submissions import SubmissionStore


class UsabilityFixture(WispTuiApp):
    expected = ""
    session_choice: str | None = None
    model_choice: dict[str, Any] | None = None
    approval_choice: str | None = None

    def on_mount(self) -> None:
        super().on_mount()
        self.run_worker(self.exercise())

    async def add_text(self, text: str) -> None:
        self.expected += text
        self._append_delta(text)
        view = self._stream_view
        assert view is not None
        await eventually(lambda: view.rendered == self.expected)

    async def choose(self, modal: ModalScreen[Any], title_id: str, marker: str) -> Any:
        answered = asyncio.Event()
        decisions: list[Any] = []

        def selected(value: Any) -> None:
            decisions.append(value)
            answered.set()

        await self.push_screen(modal, selected)
        # Stream under the mounted dialog before enabling its test key marker.
        await self.add_text("Output continues while the dialog is open.\n\n")
        modal.query_one(title_id, Static).update(marker)
        if isinstance(modal, ApprovalModal):
            details = modal.query_one("#approval-details", VerticalScroll)
            await eventually(
                lambda: (
                    details.max_scroll_y > 0
                    and details.scroll_y == details.max_scroll_y
                    and modal.armed
                )
            )
            modal.query_one(title_id, Static).update("APPROVAL_SCROLLED")
        await asyncio.wait_for(answered.wait(), timeout=120)
        return decisions[0]

    async def exercise(self) -> None:
        await eventually(lambda: "ready" in self._status_text)
        self.session_id = SID
        self._begin_run(RID)
        self.query_one(PromptInput).value = "keep this draft"
        for index in range(18):
            await self.add_text(
                f"## Section {index}\n\n"
                + "日本語 café **streamed text** and `code`. " * 20
                + "\n\n- first\n- second\n\n```text\n"
                + "long_code_" * 16
                + "\n```\n\n| Key | Value |\n|---|---|\n| state | ready |\n\n"
            )
            if index == 2:
                self._set_status("RESIZE_NARROW")
                await eventually(lambda: self.size.width == 40 and self.size.height == 18)
            elif index == 5:
                self.session_choice = await self.choose(
                    SessionPicker([(f"session-{i}", f"Session {i} [/] " * 4) for i in range(20)]),
                    "#picker-title",
                    "SESSION_READY",
                )
            elif index == 8:
                self.model_choice = await self.choose(
                    ModelPicker([{"id": "scripted-model", "reasoning": True}], None),
                    "#picker-title",
                    "MODEL_READY",
                )
            elif index == 11:
                self.approval_choice = await self.choose(
                    ApprovalModal("scripted_tool", {"items": [f"detail {i}" for i in range(60)]}),
                    "#approval-title",
                    "APPROVAL_READY",
                )
            elif index == 14:
                self._set_status("RESIZE_WIDE")
                await eventually(lambda: self.size.width == 100 and self.size.height == 30)
        view = self._stream_view
        assert view is not None
        self._finalize_run("run.completed", {})
        await eventually(lambda: view.rendered == self.expected)
        assert self.query_one(PromptInput).value == "keep this draft"
        self.exit()


def main() -> None:
    with tempfile.TemporaryDirectory() as state:
        client = ScriptedClient([])
        app = UsabilityFixture(
            client,
            Config(client.url, client.token),
            sessions=SessionStore(Path(state) / "sessions"),
            submissions=SubmissionStore(client.url, Path(state)),
        )
        app.run(inline=True, mouse=False)
        assert all(method == "GET" for method, _, _ in client.calls)
        print(
            "USABILITY_RESULT="
            + json.dumps(
                {
                    "session": app.session_choice,
                    "model": app.model_choice,
                    "approval": app.approval_choice,
                    "characters": len(app.expected),
                    "draft": app._composer_draft,
                }
            )
        )


if __name__ == "__main__":
    main()
