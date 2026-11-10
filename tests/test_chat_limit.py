"""Long sessions stay responsive: only the newest history items are shown."""

from __future__ import annotations

from collections import deque

import pytest

from tests.helpers import SID
from tests.test_app import eventually, make_app, ready
from wisp_tui.app import WispTuiApp

LIMIT = 5


@pytest.fixture(autouse=True)
def small_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(WispTuiApp, "MAX_CHAT_ITEMS", LIMIT)


async def test_long_history_prints_only_the_tail(monkeypatch: pytest.MonkeyPatch) -> None:
    app, client = make_app([], monkeypatch)
    client.replies[("GET", f"/v1/sessions/{SID}/history")] = deque(
        [
            [
                {
                    "values": {
                        "messages": [
                            {"type": "human" if index % 2 == 0 else "ai", "content": f"h{index}"}
                            for index in range(500)
                        ]
                    }
                }
            ]
        ]
    )
    async with app.run_test() as pilot:
        await eventually(lambda: ready(app))
        app._open_session(SID)
        await eventually(lambda: app.session_id == SID)
        await pilot.pause()
        items = [text for _, text in app.transcript()]
        assert items[0].startswith("\u2191 495 earlier items")
        assert items[1:] == [f"h{index}" for index in range(495, 500)]
        assert not app._chat_items, "history stayed in the live area"
