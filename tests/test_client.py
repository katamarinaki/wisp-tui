from __future__ import annotations

import pytest

from tests.helpers import FakeJsonResponse, FakeOpener, make_client, sse_response
from wisp_tui.client import ClientError, WispClient, _route_permitted


class TestRouteAllowlist:
    def test_chat_routes_permitted(self) -> None:
        sid = "3bf1ba87-760e-4c0c-8a9d-a88a581d6bc5"
        assert _route_permitted("GET", "/v1/health")
        assert _route_permitted("GET", "/v1/models")
        assert _route_permitted("GET", "/v1/sessions")
        assert _route_permitted("POST", "/v1/sessions")
        assert _route_permitted("GET", f"/v1/sessions/{sid}")
        assert _route_permitted("PATCH", f"/v1/sessions/{sid}")
        assert _route_permitted("GET", f"/v1/sessions/{sid}/history")
        assert _route_permitted("POST", f"/v1/sessions/{sid}/runs")
        assert _route_permitted("POST", f"/v1/sessions/{sid}/cancel")
        assert _route_permitted("GET", f"/v1/runs/{sid}")
        assert _route_permitted("GET", f"/v1/runs/{sid}/events")
        assert _route_permitted("POST", f"/v1/runs/{sid}/confirmations/abc.123:_-")

    def test_privileged_routes_refused(self) -> None:
        for method, path in [
            ("GET", "/v1/tokens"),
            ("POST", "/v1/tokens"),
            ("GET", "/v1/billing/me"),
            ("POST", "/v1/billing/topups"),
            ("GET", "/v1/update"),
            ("POST", "/v1/update"),
            ("GET", "/v1/schedules"),
            ("POST", "/v1/schedules"),
            ("GET", "/v1/connectors"),
            ("POST", "/v1/permissions/rule"),
            ("POST", "/v1/permissions/bypass"),
            ("GET", "/v1/sandbox"),
            ("PUT", "/v1/sandbox"),
            ("PUT", "/v1/instructions"),
            ("POST", "/v1/auth/login"),
            ("POST", "/v1/attestation/refresh"),
            ("GET", "/v1/audit"),
        ]:
            assert not _route_permitted(method, path), f"{method} {path} should be refused"

    def test_malformed_ids_refused(self) -> None:
        assert not _route_permitted("GET", "/v1/sessions/not-a-uuid")
        assert not _route_permitted("GET", "/v1/sessions/../v1/tokens")
        assert not _route_permitted("GET", "/v1/sessions/../../v1/tokens")
        assert not _route_permitted("POST", "/v1/sessions/x/y/runs")

    def test_method_mismatch_refused(self) -> None:
        assert not _route_permitted("DELETE", "/v1/sessions")
        assert not _route_permitted("PUT", "/v1/sessions/3bf1ba87-760e-4c0c-8a9d-a88a581d6bc5")

    def test_runs_query_validation(self) -> None:
        sid = "3bf1ba87-760e-4c0c-8a9d-a88a581d6bc5"
        assert _route_permitted("GET", f"/v1/runs?sessionId={sid}&status=parked&limit=1")
        assert not _route_permitted("GET", "/v1/runs?sessionId=../../admin")
        assert not _route_permitted("GET", "/v1/runs?origin=all")
        assert not _route_permitted("GET", "/v1/runs?limit=9999")
        assert not _route_permitted("GET", "/v1/runs?status=parked&status=queued")


class TestClientConstruction:
    def test_rejects_non_loopback_url(self) -> None:
        for url in [
            "https://127.0.0.1:3008",
            "http://100.64.0.1:3008",
            "http://example.com:3008",
            "http://user:pass@127.0.0.1:3008",
            "http://127.0.0.1:3008/path",
            "http://127.0.0.1:3008?q=1",
            "http://127.0.0.1",
            "not a url",
        ]:
            with pytest.raises(ClientError):
                WispClient(url=url, token="t" * 43)

    def test_rejects_bad_token(self) -> None:
        with pytest.raises(ClientError):
            WispClient(url="http://127.0.0.1:3008", token="short")
        with pytest.raises(ClientError):
            WispClient(url="http://127.0.0.1:3008", token="has spaces in it")


class TestRequests:
    def test_request_formation(self) -> None:
        opener = FakeOpener()
        opener.responses.append(FakeJsonResponse({"ok": True}))
        client = make_client(opener)
        result = client.request("POST", "/v1/sessions", {"origin": "cli", "title": "t"})
        assert result == {"ok": True}
        recorded = opener.requests[0]
        assert recorded.method == "POST"
        assert recorded.path == "http://127.0.0.1:3008/v1/sessions"
        assert b'"origin"' in (recorded.body or b"")
        auth = {k.lower(): v for k, v in recorded.headers.items()}.get("authorization")
        assert auth == "Bearer " + "t" * 43

    def test_unpermitted_route_rejected_before_network(self) -> None:
        opener = FakeOpener()
        client = make_client(opener)
        with pytest.raises(ClientError, match="route_not_permitted"):
            client.request("POST", "/v1/tokens", {"name": "evil"})
        assert opener.requests == []

    def test_json_response(self) -> None:
        opener = FakeOpener()
        opener.responses.append(FakeJsonResponse([1, 2, 3]))
        client = make_client(opener)
        assert client.request("GET", "/v1/sessions") == [1, 2, 3]

    def test_iter_events_sets_resume_header(self) -> None:

        opener = FakeOpener()
        opener.responses.append(
            sse_response(
                [
                    {"seq": 5, "kind": "text.delta", "text": "hi"},
                    {"seq": 6, "kind": "run.completed", "outcome": {}},
                ]
            )
        )
        client = make_client(opener)
        events = list(client.iter_events("3bf1ba87-760e-4c0c-8a9d-a88a581d6bc5", last_seq=4))
        assert [e["kind"] for e in events] == ["text.delta", "run.completed"]
        headers = {k.lower(): v for k, v in opener.requests[0].headers.items()}
        assert headers.get("last-event-id") == "4"

    def test_iter_events_rejects_bad_run_id(self) -> None:
        opener = FakeOpener()
        client = make_client(opener)
        with pytest.raises(ClientError, match="invalid_run_id"):
            list(client.iter_events("nope"))
        assert opener.requests == []
