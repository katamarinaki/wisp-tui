"""Long sessions stay responsive: only the newest chat items are mounted."""
from __future__ import annotations

from collections import deque

import pytest
from textual.widgets import Input, Markdown, Static

from tests.helpers import RID, SID
from tests.test_app import eventually, make_app
from wisp_tui.app import WispTuiApp
from wisp_tui.history import ChatMessage

LIMIT = 5


@pytest.fixture(autouse=True)
def small_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(WispTuiApp, "MAX_CHAT_ITEMS", LIMIT)


def chat_items(app: WispTuiApp) -> list[str]:
    """Visible chat in order: marker/system lines and message texts."""
    items: list[str] = []
    for child in app.query_one("#chat").children:
        if child.has_class("chat-trimmed"):
            items.append("MARKER: " + str(child.render()))
        elif isinstance(child, Markdown):
            items.append(child.source)
        elif isinstance(child, Static):
            items.append(str(child.render()))
    return items


async def test_live_chat_drops_oldest_and_counts_them(monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        for index in range(12):
            app._add_message(ChatMessage("user" if index % 2 == 0 else "assistant", f"m{index}"))
        await pilot.pause()
        items = chat_items(app)
        assert items[0] == "MARKER: \u2191 7 earlier items not shown \u2014 Wisp keeps the full session history."
        assert items[1:] == [f"m{index}" for index in range(7, 12)]
        # One more item updates the existing marker instead of adding another.
        app._sysline("note")
        await pilot.pause()
        items = chat_items(app)
        assert items[0].startswith("MARKER: \u2191 8 earlier items")
        assert sum(item.startswith("MARKER") for item in items) == 1
        assert len(items) == LIMIT + 1


async def test_streaming_answer_is_never_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        await eventually(lambda: "ready" in app._status_state)
        app.session_id = SID
        app._turn_started(RID)
        app._handle_event({"seq": 1, "kind": "text.delta", "text": "streaming answer"})
        # Flood with tool lines (newer than the answer) well past the limit.
        for seq in range(2, 2 + LIMIT * 3):
            app._handle_event({"seq": seq, "kind": "tool_call.started", "name": f"t{seq}"})
        app._handle_event({"seq": 99, "kind": "text.delta", "text": " continues"})
        await pilot.pause()
        assert app._stream_view is not None
        view = app._stream_view
        assert view.widget.is_attached
        await eventually(lambda: view.rendered == "streaming answer continues")
        items = chat_items(app)
        assert "streaming answer continues" in items
        assert len(items) == LIMIT + 1


async def test_long_history_mounts_only_the_tail(monkeypatch: pytest.MonkeyPatch) -> None:
    app, client = make_app([], monkeypatch)
    client.replies[("GET", f"/v1/sessions/{SID}/history")] = deque([[{
        "values": {"messages": [
            {"type": "human" if index % 2 == 0 else "ai", "content": f"h{index}"}
            for index in range(500)]}}]])
    async with app.run_test() as pilot:
        await eventually(lambda: "ready" in app._status_state)
        app._open_session(SID)
        await eventually(lambda: app.session_id == SID)
        await pilot.pause()
        items = chat_items(app)
        assert items[0].startswith("MARKER: \u2191 495 earlier items")
        assert items[1:] == [f"h{index}" for index in range(495, 500)]
        assert len(app.query_one("#chat").children) == LIMIT + 1


async def test_clear_resets_the_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        for index in range(LIMIT + 3):
            app._add_message(ChatMessage("user", f"m{index}"))
        app.query_one("#input", Input).value = "/clear"
        await pilot.press("enter")
        await pilot.pause()
        assert chat_items(app) == []
        for index in range(LIMIT):
            app._add_message(ChatMessage("user", f"n{index}"))
        await pilot.pause()
        assert chat_items(app) == [f"n{index}" for index in range(LIMIT)]
