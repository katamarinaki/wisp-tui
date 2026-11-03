from __future__ import annotations

import pytest

from wisp_tui.history import HistoryError, parse_history


def checkpoint(messages: list[object]) -> dict[str, object]:
    return {"values": {"messages": messages}, "metadata": {}}


class TestParseHistory:
    def test_takes_newest_non_empty_checkpoint(self) -> None:
        payload = [
            checkpoint([{"type": "human", "content": "old", "id": "1"}]),
            checkpoint([{"type": "human", "content": "hello", "id": "2"},
                        {"type": "ai", "content": "hi there", "id": "3"}]),
            {"values": {"messages": []}, "metadata": {}},
        ]
        messages = parse_history(payload)
        assert [(m.role, m.text) for m in messages] == [
            ("user", "hello"), ("assistant", "hi there")]

    def test_role_mapping(self) -> None:
        messages = parse_history([checkpoint([
            {"type": "human", "content": "q"},
            {"type": "ai", "content": "a"},
            {"type": "system", "content": "s"},
            {"type": "tool", "content": "r"},
        ])])
        assert [m.role for m in messages] == ["user", "assistant", "system", "tool"]

    def test_content_block_lists(self) -> None:
        messages = parse_history([checkpoint([
            {"type": "ai", "content": [
                {"type": "text", "text": "part one"},
                {"type": "text", "text": "part two"},
                {"type": "image_url", "image_url": {"url": "data:x"}},
            ]},
        ])])
        assert messages[0].text == "part one\npart two\n[image]"

    def test_empty_and_blank_messages_dropped(self) -> None:
        messages = parse_history([checkpoint([
            {"type": "human", "content": "keep"},
            {"type": "ai", "content": "  "},
            {"type": "ai", "content": ""},
            {"type": "unknown-type", "content": "x"},
            "not a dict",
        ])])
        assert [(m.role, m.text) for m in messages] == [("user", "keep")]

    def test_empty_history_returns_empty(self) -> None:
        assert parse_history([]) == []
        assert parse_history([{"values": {}}]) == []

    def test_invalid_shape_raises(self) -> None:
        with pytest.raises(HistoryError):
            parse_history({"not": "a list"})
