"""Real PageUp/F4 bytes while output arrives, using only a scripted client."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from tests.helpers import ScriptedClient
from wisp_tui.app import WispTuiApp
from wisp_tui.chat_view import ChatScroll
from wisp_tui.config import Config
from wisp_tui.history import ChatMessage
from wisp_tui.sessions import SessionStore
from wisp_tui.submissions import SubmissionStore


class ScrollFixture(WispTuiApp):
    paused_checked = False
    resumed_checked = False
    _paused_y = 0.0

    def on_mount(self) -> None:
        super().on_mount()
        for i in range(20):
            self._add_message(ChatMessage("assistant", f"Reading paragraph {i}\n\nMore text."))
        self.set_timer(0.2, self.ready)

    def ready(self) -> None:
        self.query_one(ChatScroll).focus()
        self._status("SCROLL_READY")

    def on_chat_scroll_follow_changed(self, event: ChatScroll.FollowChanged) -> None:
        super().on_chat_scroll_follow_changed(event)
        chat = self.query_one(ChatScroll)
        if not chat.following and not self.paused_checked:
            self.set_timer(0.3, self.add_output)
        elif chat.following and self.paused_checked:
            self.set_timer(0.2, self.check_resumed)

    def add_output(self) -> None:
        self._paused_y = self.query_one(ChatScroll).scroll_y
        self._add_message(ChatMessage("assistant", "New output\n\n" * 30))
        self.set_timer(0.2, self.check_paused)

    def check_paused(self) -> None:
        chat = self.query_one(ChatScroll)
        assert not chat.following and chat.scroll_y == self._paused_y < chat.max_scroll_y
        self.paused_checked = True
        self._status("SCROLL_PAUSED_READY")

    def check_resumed(self) -> None:
        chat = self.query_one(ChatScroll)
        assert chat.following and chat.scroll_y == chat.max_scroll_y
        self.resumed_checked = True
        self.exit()


def main() -> None:
    with tempfile.TemporaryDirectory() as state:
        client = ScriptedClient([])
        app = ScrollFixture(client, Config(client.url, client.token),
                            sessions=SessionStore(Path(state) / "sessions"),
                            submissions=SubmissionStore(client.url, Path(state)))
        app.run(inline=True)
        assert all(method == "GET" for method, _, _ in client.calls)
        print("SCROLL_RESULT=" + json.dumps({
            "paused": app.paused_checked, "resumed": app.resumed_checked}))


if __name__ == "__main__":
    main()
