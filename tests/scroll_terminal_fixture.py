"""Real PageUp/F4 bytes while output arrives, using only a scripted client."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from tests.helpers import ScriptedClient
from tests.test_app import eventually
from wisp_tui.app import WispTuiApp
from wisp_tui.chat_view import ChatScroll
from wisp_tui.config import Config
from wisp_tui.history import ChatMessage
from wisp_tui.sessions import SessionStore
from wisp_tui.submissions import SubmissionStore


class ScrollFixture(WispTuiApp):
    paused_checked = False
    resumed_checked = False

    def on_mount(self) -> None:
        super().on_mount()
        for i in range(20):
            self._add_message(ChatMessage("assistant", f"Reading paragraph {i}\n\nMore text."))
        self.run_worker(self.ready())

    async def ready(self) -> None:
        # Bootstrap must not overwrite the marker, and PageUp needs a scrollable chat.
        await eventually(lambda: "ready" in self._status_text)
        chat = self.query_one(ChatScroll)
        await eventually(lambda: chat.max_scroll_y > 0 and chat.scroll_y == chat.max_scroll_y)
        chat.focus()
        self._set_status("SCROLL_READY")

    def on_chat_scroll_follow_changed(self, event: ChatScroll.FollowChanged) -> None:
        super().on_chat_scroll_follow_changed(event)
        chat = self.query_one(ChatScroll)
        if not chat.following and not self.paused_checked:
            self.run_worker(self.check_paused())
        elif chat.following and self.paused_checked:
            self.run_worker(self.check_resumed())

    async def check_paused(self) -> None:
        chat = self.query_one(ChatScroll)
        # PageUp's scroll is deferred and animated; read the position once it ends.
        await chat.wait_for_refresh()
        await eventually(lambda: not self.animator.is_being_animated(chat, "scroll_y"))
        paused_y = chat.scroll_y
        old_max = chat.max_scroll_y
        self._add_message(ChatMessage("assistant", "New output\n\n" * 30))
        await eventually(lambda: chat.max_scroll_y > old_max)
        # Follow callbacks share the chat's queue, so any would have run by now.
        await chat.wait_for_refresh()
        assert not chat.following and chat.scroll_y == paused_y < chat.max_scroll_y
        self.paused_checked = True
        self._set_status("SCROLL_PAUSED_READY")

    async def check_resumed(self) -> None:
        chat = self.query_one(ChatScroll)
        await eventually(lambda: chat.scroll_y == chat.max_scroll_y)
        assert chat.following
        self.resumed_checked = True
        self.exit()


def main() -> None:
    with tempfile.TemporaryDirectory() as state:
        client = ScriptedClient([])
        app = ScrollFixture(
            client,
            Config(client.url, client.token),
            sessions=SessionStore(Path(state) / "sessions"),
            submissions=SubmissionStore(client.url, Path(state)),
        )
        app.run(inline=True)
        assert all(method == "GET" for method, _, _ in client.calls)
        print(
            "SCROLL_RESULT="
            + json.dumps({"paused": app.paused_checked, "resumed": app.resumed_checked})
        )


if __name__ == "__main__":
    main()
