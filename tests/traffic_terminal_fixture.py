"""Measure a quiet busy run's real terminal traffic using only scripted data."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from tests.helpers import RID, SID, ScriptedClient
from wisp_tui.app import WispTuiApp
from wisp_tui.config import Config
from wisp_tui.history import ChatMessage
from wisp_tui.sessions import SessionStore
from wisp_tui.submissions import SubmissionStore


class TrafficFixture(WispTuiApp):
    measuring = False
    bytes_sent = 0
    frames = 0
    transcript_rewrites = 0

    def on_mount(self) -> None:
        super().on_mount()
        assert self._driver is not None
        write = self._driver.write

        def record(data: str) -> None:
            if self.measuring:
                self.bytes_sent += len(data.encode("utf-8"))
                self.frames += data.count("\x1b[6n")
                self.transcript_rewrites += data.count("Unchanged transcript")
            write(data)

        self._driver.write = record  # type: ignore[method-assign]
        self.session_id = SID
        self._add_message(ChatMessage("assistant", "\n\n".join(
            f"Unchanged transcript {index}: " + "scripted text " * 10 for index in range(30))))
        self._turn_started(RID)
        self.set_timer(0.4, self.start_measurement)
        self.set_timer(1.4, self.stop_measurement)

    def start_measurement(self) -> None:
        self.measuring = True

    def stop_measurement(self) -> None:
        self.measuring = False
        self._finish_run()
        self.set_timer(0.1, self.exit)


def main() -> None:
    with tempfile.TemporaryDirectory() as state:
        client = ScriptedClient([])
        app = TrafficFixture(client, Config(client.url, client.token),
                             sessions=SessionStore(Path(state) / "sessions"),
                             submissions=SubmissionStore(client.url, Path(state)))
        app.run(inline=True)
        print("TRAFFIC_RESULT=" + json.dumps({
            "bytes": app.bytes_sent, "frames": app.frames,
            "transcript_rewrites": app.transcript_rewrites}))


if __name__ == "__main__":
    main()
