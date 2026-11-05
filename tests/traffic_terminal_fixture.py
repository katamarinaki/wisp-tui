"""Measure a quiet busy run's real terminal traffic using only scripted data."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from rich.console import Console

from tests.helpers import RID, SID, ScriptedClient
from tests.test_app import eventually
from wisp_tui.app import WispTuiApp
from wisp_tui.config import Config
from wisp_tui.editor import PromptInput
from wisp_tui.history import ChatMessage
from wisp_tui.sessions import SessionStore
from wisp_tui.submissions import SubmissionStore
from wisp_tui.terminal import InlineUpdate


class TrafficFixture(WispTuiApp):
    TICKS = 10
    measuring = False
    bytes_sent = 0
    frames = 0
    ticks = 0
    transcript_rewrites = 0

    def on_mount(self) -> None:
        super().on_mount()
        assert self._driver is not None
        write = self._driver.write

        def record(data: str) -> None:
            if self.measuring:
                self.bytes_sent += len(data.encode("utf-8"))
                self.transcript_rewrites += data.count("Unchanged transcript")
            write(data)

        self._driver.write = record  # type: ignore[method-assign]
        render = self._inline_renderer.render

        def count_frame(update: InlineUpdate, console: Console, width: int) -> str:
            if self.measuring:
                self.frames += 1
            return render(update, console, width)

        self._inline_renderer.render = count_frame  # type: ignore[method-assign]
        self.session_id = SID
        self._add_message(
            ChatMessage(
                "assistant",
                "\n\n".join(
                    f"Unchanged transcript {index}: " + "scripted text " * 10 for index in range(30)
                ),
            )
        )
        # Cursor blinking is unrelated repaint traffic; measure only the spinner.
        self.query_one(PromptInput).cursor_blink = False
        self._begin_run(RID)
        self.run_worker(self.measure())

    def _tick_activity(self) -> None:
        super()._tick_activity()
        if self.measuring:
            self.ticks += 1

    async def measure(self) -> None:
        # Start only once the whole transcript has been painted, and measure a
        # fixed number of spinner ticks rather than a wall-clock window.
        await eventually(lambda: not self._scrollback and not self._sync_queued)
        await self.screen.wait_for_refresh()
        self.measuring = True
        await eventually(lambda: self.ticks >= self.TICKS)
        self.measuring = False
        self._finish_run()
        await self.screen.wait_for_refresh()
        self.exit()


def main() -> None:
    with tempfile.TemporaryDirectory() as state:
        client = ScriptedClient([])
        app = TrafficFixture(
            client,
            Config(client.url, client.token),
            sessions=SessionStore(Path(state) / "sessions"),
            submissions=SubmissionStore(client.url, Path(state)),
        )
        app.run(inline=True, mouse=False)
        print(
            "TRAFFIC_RESULT="
            + json.dumps(
                {
                    "bytes": app.bytes_sent,
                    "frames": app.frames,
                    "ticks": app.ticks,
                    "transcript_rewrites": app.transcript_rewrites,
                }
            )
        )


if __name__ == "__main__":
    main()
