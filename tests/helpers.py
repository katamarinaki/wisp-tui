"""Shared test helpers: a fake opener and fake client for the app tests."""

from __future__ import annotations

import io
import json
import urllib.request
from collections import deque
from collections.abc import Callable, Iterator
from threading import Event
from typing import Any, cast

from wisp_tui.client import WispClient


class RecordedRequest:
    def __init__(self, method: str, path: str, body: bytes | None, headers: dict[str, str]) -> None:
        self.method = method
        self.path = path
        self.body = body
        self.headers = headers


class FakeOpener:
    """Stands in for urllib's OpenerDirector; returns canned responses."""

    def __init__(self) -> None:
        self.requests: list[RecordedRequest] = []
        self.responses: list[Any] = []

    def open(self, request: Any, timeout: float = 10.0) -> Any:
        body = request.data if hasattr(request, "data") else None
        self.requests.append(
            RecordedRequest(
                request.get_method(), request.full_url, body, dict(request.header_items())
            )
        )
        if not self.responses:
            raise AssertionError("no canned response for " + request.full_url)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def sse_response(events: list[dict[str, Any]]) -> FakeSSEResponse:
    return FakeSSEResponse(events)


class FakeSSEResponse:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        lines: list[bytes] = []
        for event in events:
            lines.append(b"data: " + json.dumps(event).encode())
            lines.append(b"")
        self._lines = lines or [b""]
        self._body = io.BytesIO(b"\n".join(self._lines) + b"\n")
        self.status = 200
        self.closed = False

    def __iter__(self) -> Iterator[bytes]:
        return iter(self._lines)

    def readline(self, limit: int = -1) -> bytes:
        return self._body.readline(limit)

    def __enter__(self) -> FakeSSEResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        self.closed = True


class FakeJsonResponse:
    def __init__(self, payload: Any, status: int = 200) -> None:
        self._payload = payload
        self.status = status
        self.closed = False

    def read(self, limit: int = -1) -> bytes:
        raw = json.dumps(self._payload).encode()
        return raw if limit < 0 else raw[:limit]

    def __enter__(self) -> FakeJsonResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        self.closed = True


def make_client(opener: FakeOpener) -> WispClient:

    return WispClient(
        "http://127.0.0.1:3008", "t" * 43, opener=cast(urllib.request.OpenerDirector, opener)
    )


SID = "3bf1ba87-760e-4c0c-8a9d-a88a581d6bc5"
RID = "709cb527-5dc0-403f-8ed1-45d0eeb002c0"


class RequestGate:
    """Hold one request in flight until the test explicitly releases it."""

    def __init__(self, method: str, path: str) -> None:
        self.method = method
        self.path = path
        self.entered = Event()
        self.release = Event()

    def __call__(self, method: str, path: str) -> None:
        if (method, path) == (self.method, self.path):
            self.entered.set()
            # Hang guard only; a slow but correct run must never hit it.
            if not self.release.wait(120.0):
                raise AssertionError("scripted request was not released")


class ScriptedClient(WispClient):
    """Replays canned API responses; never opens a socket."""

    def __init__(self, events: list[dict[str, Any]]) -> None:
        client = make_client(FakeOpener())
        super().__init__(client.url, client.token, opener=client.opener)
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.events = events
        self.request_hook: Callable[[str, str], None] | None = None
        self.replies: dict[tuple[str, str], deque[Any]] = {}
        self.run_status = "running"
        self.answered_prompts: set[str] = set()
        self.event_requests: list[int | None] = []
        self.event_batches: deque[list[dict[str, Any]] | Exception] = deque()

    def request(
        self, method: str, path: str, body: dict[str, Any] | None = None, timeout: float = 10.0
    ) -> Any:
        self.calls.append((method, path, body))
        if self.request_hook is not None:
            self.request_hook(method, path)
        replies = self.replies.get((method, path))
        if replies:
            reply = replies.popleft()
            if isinstance(reply, Exception):
                raise reply
            return reply
        if (method, path) == ("GET", "/v1/health"):
            return {"status": "ready", "version": "0.1.8"}
        if (method, path) == ("GET", "/v1/sessions"):
            return []
        if (method, path) == ("GET", f"/v1/sessions/{SID}"):
            return {
                "id": SID,
                "title": "test chat",
                "origin": "cli",
                "effectiveSelection": {"mode": "balanced", "thinking": False},
            }
        if (method, path) == ("GET", f"/v1/sessions/{SID}/history"):
            return [
                {
                    "values": {
                        "messages": [
                            {"type": "human", "content": "past question"},
                            {"type": "ai", "content": "past answer"},
                        ]
                    }
                }
            ]
        if (method, path) == (
            "GET",
            f"/v1/runs?sessionId={SID}&status=queued,running,parked&limit=200",
        ):
            return {"runs": [], "nextCursor": None}
        if (method, path) == ("GET", f"/v1/runs/{RID}"):
            return {
                "runId": RID,
                "sessionId": SID,
                "status": self.run_status,
                "confirmations": self.pending_confirmations(),
            }
        if (method, path) == ("POST", f"/v1/sessions/{SID}/runs"):
            return {"runId": RID, "status": "queued"}
        if (method, path) == ("POST", "/v1/sessions"):
            return {"id": SID, "title": "wisp-tui", "origin": "cli"}
        if (method, path) == ("POST", f"/v1/sessions/{SID}/cancel"):
            self.run_status = "cancelled"
            return {}
        if method == "POST" and "/confirmations/" in path:
            self.answered_prompts.add(path.rsplit("/", 1)[1])
            self.run_status = "parked" if self.pending_confirmations() else "running"
            return {"runId": RID, "status": self.run_status}
        raise AssertionError(f"unexpected request {method} {path}")

    def pending_confirmations(self) -> list[dict[str, Any]]:
        return [
            event
            for event in self.events
            if event.get("kind") == "confirmation.required"
            and event.get("id") not in self.answered_prompts
        ]

    def iter_events(
        self, run_id: str, last_seq: int | None = None, read_timeout: float = 600.0
    ) -> Iterator[dict[str, Any]]:
        if run_id != RID:
            raise AssertionError(f"unexpected run {run_id}")
        self.event_requests.append(last_seq)
        events = self.event_batches.popleft() if self.event_batches else self.events
        if isinstance(events, Exception):
            raise events
        for event in events:
            if (
                last_seq is not None
                and isinstance(event.get("seq"), int)
                and event["seq"] <= last_seq
            ):
                continue
            kind = event.get("kind")
            waiting = self.pending_confirmations()
            if (kind == "confirmation.required" and waiting) or (
                kind == "run.parked" and (waiting or not self.answered_prompts)
            ):
                self.run_status = "parked"
            yield event
