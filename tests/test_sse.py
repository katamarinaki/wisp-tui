from __future__ import annotations

import io
from typing import Iterator

import pytest

from wisp_tui.client import ClientError, _bounded_sse_lines, parse_sse


def frames(*lines: str) -> list[bytes]:
    return [line.encode() for line in lines]


class TestParseSSE:
    def test_basic_frames(self) -> None:
        events = list(parse_sse(iter(frames(
            'data: {"seq":1,"kind":"run.queued"}',
            "",
            'data: {"seq":2,"kind":"run.started"}',
            "",
        ))))
        assert [e["kind"] for e in events] == ["run.queued", "run.started"]

    def test_crlf_and_whitespace(self) -> None:
        events = list(parse_sse(iter(frames(
            'data: {"seq":1,"kind":"run.queued"}\r',
            "\r",
            'data:   {"seq":2,"kind":"run.started"}',
            "",
        ))))
        assert [e["kind"] for e in events] == ["run.queued", "run.started"]

    def test_multiline_data_joined(self) -> None:
        events = list(parse_sse(iter(frames(
            'data: {"seq":1,',
            'data: "kind":"text.delta","text":"a\\nb"}',
            "",
        ))))
        assert events == [{"seq": 1, "kind": "text.delta", "text": "a\nb"}]

    def test_comments_and_ids_ignored(self) -> None:
        events = list(parse_sse(iter(frames(
            ": keepalive",
            "id: 7",
            'data: {"seq":8,"kind":"run.parked"}',
            "",
        ))))
        assert events == [{"seq": 8, "kind": "run.parked"}]

    def test_blank_only_frames_skipped(self) -> None:
        assert list(parse_sse(iter(frames("", "", "")))) == []

    def test_trailing_frame_without_blank_not_dispatched(self) -> None:
        events = list(parse_sse(iter(frames(
            'data: {"seq":1,"kind":"run.queued"}',
            "",
            'data: {"seq":2,"kind":"run.started"}',
        ))))
        assert [e["kind"] for e in events] == ["run.queued"]

    def test_invalid_json_rejected(self) -> None:
        with pytest.raises(ClientError, match="invalid_event_json"):
            list(parse_sse(iter(frames("data: not json", ""))))

    def test_non_object_rejected(self) -> None:
        with pytest.raises(ClientError, match="invalid_event_shape"):
            list(parse_sse(iter(frames("data: [1,2]", ""))))

    def test_missing_kind_rejected(self) -> None:
        with pytest.raises(ClientError, match="invalid_event_shape"):
            list(parse_sse(iter(frames('data: {"seq":1}', ""))))

    def test_unknown_kind_passes_through(self) -> None:
        # The API contract: clients skip unknown kinds rather than fail.
        events = list(parse_sse(iter(frames(
            'data: {"seq":1,"kind":"future.event","x":1}',
            "",
        ))))
        assert events[0]["kind"] == "future.event"

    @pytest.mark.parametrize("line", [b"data: " + b"x" * 30, b": " + b"x" * 30,
                                      b"id: " + b"x" * 30])
    def test_unterminated_frame_is_bounded_incrementally(
            self, monkeypatch: pytest.MonkeyPatch, line: bytes) -> None:
        monkeypatch.setattr("wisp_tui.client.MAX_EVENT", 64)

        def lines() -> Iterator[bytes]:
            yield line + b"\n"
            yield line + b"\n"
            raise AssertionError("parser read beyond its frame budget")

        with pytest.raises(ClientError, match="event_too_large"):
            list(parse_sse(lines()))

    @pytest.mark.parametrize("as_bytes", [False, True])
    def test_limit_counts_utf8_bytes(self, monkeypatch: pytest.MonkeyPatch,
                                     as_bytes: bool) -> None:
        monkeypatch.setattr("wisp_tui.client.MAX_EVENT", 64)
        line = "data: " + "é" * 30  # 36 characters, 66 bytes
        raw = line.encode() if as_bytes else line
        with pytest.raises(ClientError, match="event_too_large"):
            list(parse_sse(iter([raw])))

    def test_budget_resets_between_frames(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("wisp_tui.client.MAX_EVENT", 64)
        line = b'data: {"kind":"run.started"}\n'
        assert len(list(parse_sse(iter([line, b"\n"] * 10)))) == 10

    def test_unterminated_line_read_is_bounded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("wisp_tui.client.MAX_EVENT", 64)
        source = io.BytesIO(b"x" * 256)
        with pytest.raises(ClientError, match="event_too_large"):
            list(_bounded_sse_lines(source))
        assert source.tell() == 65

    def test_bounded_reader_accepts_normal_frames(self) -> None:
        source = io.BytesIO(b'data: {"kind":"run.started"}\n\n')
        assert list(parse_sse(_bounded_sse_lines(source))) == [{"kind": "run.started"}]
