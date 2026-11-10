"""Loopback-only HTTP client for the Wisp runtime API.

Security model (mirrors the ask-wisp proxy):
- The runtime URL must be an explicit loopback http URL; no credentials in URL,
  no query/fragment, no redirects, no environment proxies.
- Only chat-relevant routes are reachable (client-side allowlist). Billing,
  tokens, updates, schedules, connectors, permission-rule writes, sandbox and
  account routes are never issued by this client. This is a scope guard for
  THIS tool, not a server-side restriction on the credential.
- Response bodies are size-bounded; error diagnostics carry status codes and
  server-provided ``code`` fields only, never raw bodies, headers or the token.
"""

from __future__ import annotations

import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from typing import Any, Protocol

MAX_JSON_BYTES = 8_388_608  # 8 MiB cap for JSON API responses
MAX_EVENT_BYTES = 1_048_576  # per SSE event
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
PROMPT_ID = r"[A-Za-z0-9._:-]{1,128}"
MAX_PROJECT_ROOT_CHARS = 4096

ALLOWED_ROUTES: tuple[tuple[str, str], ...] = (
    ("GET", r"/v1/health"),
    ("GET", r"/v1/models"),
    ("GET", r"/v1/sessions"),
    ("POST", r"/v1/sessions"),
    ("GET", rf"/v1/sessions/{UUID}"),
    ("PATCH", rf"/v1/sessions/{UUID}"),
    ("GET", rf"/v1/sessions/{UUID}/history"),
    ("POST", rf"/v1/sessions/{UUID}/runs"),
    ("POST", rf"/v1/sessions/{UUID}/cancel"),
    ("GET", r"/v1/runs"),
    ("GET", rf"/v1/runs/{UUID}"),
    ("GET", rf"/v1/runs/{UUID}/events"),
    ("POST", rf"/v1/runs/{UUID}/confirmations/{PROMPT_ID}"),
)

TERMINAL_RUN_STATUSES = ("completed", "failed", "cancelled")


class ClientError(Exception):
    """Safe, code-only client failure; never carries raw bodies or credentials."""


def safe_code(value: object, fallback: str) -> str:
    return value if isinstance(value, str) and re.fullmatch(r"[a-z0-9_]{1,80}", value) else fallback


def validate_runtime_url(value: object) -> str:
    if not isinstance(value, str):
        raise ClientError("invalid_runtime_url")
    try:
        parsed = urllib.parse.urlsplit(value)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in ("127.0.0.1", "::1")
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
            or not parsed.port
            or not 1 <= parsed.port <= 65535
        ):
            raise ClientError("runtime_must_be_explicit_loopback_http")
    except ValueError:
        raise ClientError("invalid_runtime_url") from None
    return value.rstrip("/")


def validate_token(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._~-]{8,1024}", value):
        raise ClientError("invalid_token")
    return value


def _validate_runs_query(path: str) -> dict[str, str]:
    """Parse and validate a conservative query string for GET /v1/runs."""
    query = urllib.parse.urlsplit(path).query
    if not query:
        return {}
    pairs = urllib.parse.parse_qsl(query, keep_blank_values=True)
    allowed = {"sessionId", "status", "limit", "cursor"}
    result: dict[str, str] = {}
    for key, value in pairs:
        if key not in allowed or not value or key in result:
            raise ClientError("query_parameter_not_permitted")
        if key == "sessionId" and not re.fullmatch(UUID, value):
            raise ClientError("invalid_session_id")
        if key == "status" and not re.fullmatch(r"[a-z,]{1,80}", value):
            raise ClientError("invalid_status_filter")
        if key == "limit" and not re.fullmatch(r"[1-9][0-9]{0,2}", value):
            raise ClientError("invalid_limit")
        result[key] = value
    return result


def valid_project_root(value: object) -> str | None:
    """An absolute folder path that is safe to send as ``projectRoot``, else None."""
    if (
        isinstance(value, str)
        and 1 < len(value) <= MAX_PROJECT_ROOT_CHARS
        and value.startswith("/")
        and value.isprintable()
    ):
        return value
    return None


def session_path(session_id: str, suffix: str = "", project_root: str | None = None) -> str:
    """A session route; the runtime finds a dropped Project Session only by its folder."""
    path = f"/v1/sessions/{session_id}{suffix}"
    if project_root is None:
        return path
    return (
        path
        + "?"
        + urllib.parse.urlencode({"projectRoot": project_root}, quote_via=urllib.parse.quote)
    )


def _route_permitted(method: str, path: str) -> bool:
    split = urllib.parse.urlsplit(path)
    base = split.path
    if not any(m == method and re.fullmatch(p, base) for m, p in ALLOWED_ROUTES):
        return False
    if base == "/v1/runs":
        try:
            _validate_runs_query(path)
        except ClientError:
            return False
    elif split.query:
        if not re.fullmatch(rf"/v1/sessions/{UUID}(/runs|/cancel)?", base):
            return False
        pairs = urllib.parse.parse_qsl(split.query, keep_blank_values=True)
        if (
            len(pairs) != 1
            or pairs[0][0] != "projectRoot"
            or valid_project_root(pairs[0][1]) is None
        ):
            return False
    return True


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        # Never forward the bearer credential to a different endpoint.
        return None


class _FakeResponse:
    """Minimal response interface for tests: context manager + byte lines."""

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = lines
        self.status = 200
        self.closed = False

    def __iter__(self) -> Iterator[bytes]:
        return iter(self._lines)

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        self.closed = True


class WispClient:
    def __init__(
        self, url: str, token: str, opener: urllib.request.OpenerDirector | None = None
    ) -> None:
        self.url = validate_runtime_url(url)
        self.token = validate_token(token)
        self.opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect()
        )

    # -- plain JSON requests -------------------------------------------------

    def request(
        self, method: str, path: str, body: dict[str, Any] | None = None, timeout: float = 10.0
    ) -> Any:
        if not _route_permitted(method, path):
            raise ClientError("route_not_permitted")
        payload = None if body is None else json.dumps(body, allow_nan=False).encode()
        request = urllib.request.Request(
            self.url + path,
            data=payload,
            method=method,
            headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"},
        )
        try:
            with self.opener.open(request, timeout=timeout) as response:
                if response.status == 204:
                    return {}
                raw = response.read(MAX_JSON_BYTES + 1)
            if len(raw) > MAX_JSON_BYTES:
                raise ClientError("runtime_response_too_large")
            return json.loads(raw)
        except urllib.error.HTTPError as error:
            code = ""
            try:
                data = json.loads(error.read(16384))
                if isinstance(data, dict):
                    code = safe_code(data.get("code"), "")
            except (OSError, ValueError):
                pass
            suffix = ":" + code if code else ""
            raise ClientError(f"runtime_http_{error.code}{suffix}") from None
        except (OSError, urllib.error.URLError) as error:
            if isinstance(error, socket.timeout):
                raise ClientError("runtime_timeout") from None
            raise ClientError("runtime_unreachable") from None
        except ValueError:
            raise ClientError("invalid_runtime_response") from None

    # -- SSE event stream ----------------------------------------------------

    def iter_events(
        self, run_id: str, last_seq: int | None = None, read_timeout: float = 600.0
    ) -> Iterator[dict[str, Any]]:
        """Yield the Run's events as dicts until the stream ends.

        Raises ClientError("event_stream_timeout") on a read timeout; callers
        may reopen the stream with ``last_seq`` to resume.
        """
        if not re.fullmatch(UUID, run_id):
            raise ClientError("invalid_run_id")
        path = f"/v1/runs/{run_id}/events"
        if not _route_permitted("GET", path):
            raise ClientError("route_not_permitted")
        request = urllib.request.Request(
            self.url + path,
            method="GET",
            headers={"Authorization": "Bearer " + self.token, "Accept": "text/event-stream"},
        )
        if last_seq is not None:
            if not isinstance(last_seq, int) or last_seq < 0:
                raise ClientError("invalid_last_seq")
            request.add_header("Last-Event-ID", str(last_seq))
        try:
            with self.opener.open(request, timeout=read_timeout) as response:
                if response.status != 200:
                    raise ClientError(f"runtime_http_{response.status}")
                yield from parse_sse(_bounded_sse_lines(response))
        except urllib.error.HTTPError as error:
            raise ClientError(f"runtime_http_{error.code}") from None
        except (OSError, urllib.error.URLError) as error:
            if isinstance(error, socket.timeout):
                raise ClientError("event_stream_timeout") from None
            raise ClientError("runtime_unreachable") from None


class _SSESource(Protocol):
    def readline(self, limit: int = -1) -> bytes: ...


def _bounded_sse_lines(source: _SSESource) -> Iterator[bytes]:
    """Bound allocations even if the peer never terminates a line."""
    while raw := source.readline(MAX_EVENT_BYTES + 1):
        if len(raw) > MAX_EVENT_BYTES:
            raise ClientError("event_too_large")
        yield raw


def parse_sse(lines: Iterator[bytes | str]) -> Iterator[dict[str, Any]]:
    """Parse a text/event-stream body into JSON event dicts.

    ``data:`` lines accumulate; comments and SSE IDs are ignored (the JSON
    carries ``seq``). A blank line dispatches the frame. Enforce a wire-byte
    budget incrementally, including ignored fields, not just at dispatch.
    """
    data_lines: list[str] = []
    frame_bytes = 0
    for raw in lines:
        frame_bytes += len(raw) if isinstance(raw, bytes) else len(raw.encode("utf-8"))
        if frame_bytes > MAX_EVENT_BYTES:
            raise ClientError("event_too_large")
        line = (
            raw.decode("utf-8", "replace").rstrip("\r\n")
            if isinstance(raw, bytes)
            else raw.rstrip("\r\n")
        )
        if line == "":
            frame_bytes = 0
            if data_lines:
                payload = "\n".join(data_lines)
                try:
                    event = json.loads(payload)
                except ValueError:
                    raise ClientError("invalid_event_json") from None
                if not isinstance(event, dict) or not isinstance(event.get("kind"), str):
                    raise ClientError("invalid_event_shape")
                yield event
                data_lines = []
            continue
        if line.startswith(":"):
            continue  # comment / keepalive
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip(" "))
        # ``id:`` and unknown fields are ignored; seq is carried inside the JSON.
    # A trailing frame without a blank line is not dispatched, per SSE spec.
