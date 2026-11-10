"""Run contract and follower tests; no network, sockets, or wall-clock waits."""

from __future__ import annotations

from collections import deque
from typing import Any

import pytest

from tests.helpers import RID, SID, ScriptedClient
from wisp_tui.client import ClientError
from wisp_tui.runs import RunFollower, find_active_run, validate_run


def record(status: str, **fields: Any) -> dict[str, Any]:
    return {"runId": RID, "sessionId": SID, "status": status, **fields}


def follower(
    client: ScriptedClient, monkeypatch: pytest.MonkeyPatch
) -> tuple[RunFollower, list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    events: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    monkeypatch.setattr(RunFollower, "_wait", lambda self, seconds: None)
    return (
        RunFollower(
            client,
            RID,
            SID,
            last_seq=None,
            on_event=events.append,
            on_snapshot=records.append,
            on_error=errors.append,
        ),
        events,
        records,
        errors,
    )


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"runs": "wrong"},
        {"runs": [{"runId": "wrong"}]},
        {"runs": [record("running", sessionId=RID)]},
        {"runs": [record("unrecognized")]},
        {"runs": [], "nextCursor": "another-page"},
    ],
)
def test_invalid_listing_does_not_silently_become_idle(payload: Any) -> None:
    with pytest.raises(ClientError):
        find_active_run(payload, SID)


def test_active_discovery_uses_envelope_and_prefers_executing_run() -> None:
    queued = record("queued", runId=SID)
    assert find_active_run({"runs": [queued, record("running")], "nextCursor": None}, SID) == RID
    assert find_active_run({"runs": [queued, record("queued")], "nextCursor": None}, SID) == RID
    assert find_active_run({"runs": [], "nextCursor": None}, SID) == ""
    with pytest.raises(ClientError, match="ambiguous_active_runs"):
        find_active_run({"runs": [record("running"), record("parked")]}, SID)


@pytest.mark.parametrize(
    "prompts",
    [
        "wrong",
        [None],
        [{"id": "../wrong", "toolName": "example"}],
        [{"id": "valid", "toolName": 42}],
        [{"id": "same", "toolName": "example"}] * 2,
    ],
)
def test_invalid_confirmations_are_not_actionable(prompts: Any) -> None:
    with pytest.raises(ClientError, match="invalid_confirmations"):
        validate_run(record("parked", confirmations=prompts), RID, SID)


def test_terminal_record_finishes_without_any_sse_event(monkeypatch: pytest.MonkeyPatch) -> None:
    client = ScriptedClient([])
    client.replies[("GET", f"/v1/runs/{RID}")] = deque(
        [record("completed", output={"text": "persisted answer"})]
    )
    worker, events, records, errors = follower(client, monkeypatch)
    worker.run()
    assert records[0]["output"]["text"] == "persisted answer"
    assert not events and not errors and not client.event_requests


def test_eof_reconciles_terminal_record(monkeypatch: pytest.MonkeyPatch) -> None:
    client = ScriptedClient([])
    client.replies[("GET", f"/v1/runs/{RID}")] = deque(
        [record("running"), record("completed", output={"text": "persisted answer"})]
    )
    worker, events, records, errors = follower(client, monkeypatch)
    worker.run()
    assert not events
    assert [r["status"] for r in records] == ["running", "completed"]
    assert errors == ["event_stream_ended"]


def test_resume_preserves_cursor_and_deduplicates_events(monkeypatch: pytest.MonkeyPatch) -> None:
    client = ScriptedClient([])
    client.event_batches = deque(
        [
            [{"seq": 1, "kind": "text.delta", "text": "first"}],
            ClientError("event_stream_timeout"),
            [
                {"seq": 2, "kind": "text.delta", "text": "second"},
                {"seq": 2, "kind": "text.delta", "text": "duplicate"},
                {"seq": 3, "kind": "run.completed", "outcome": {}},
            ],
        ]
    )
    worker, events, _, errors = follower(client, monkeypatch)
    worker.run()
    assert [e["seq"] for e in events] == [1, 2, 3]
    assert client.event_requests == [None, 1, 1]
    assert worker.last_seq == 3
    # EOF is a failure; a quiet read timeout is not.
    assert errors == ["event_stream_ended"]


def test_long_silent_tool_call_is_not_a_disconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    """Many more quiet read windows than MAX_FAILURES, while the run is alive."""
    client = ScriptedClient([])
    quiet_windows = RunFollower.MAX_FAILURES * 3
    client.event_batches = deque(
        [[{"seq": 1, "kind": "tool_call.started", "name": "slow"}]]
        + [ClientError("event_stream_timeout")] * quiet_windows
        + [[{"seq": 2, "kind": "run.completed", "outcome": {}}]]
    )
    worker, events, records, errors = follower(client, monkeypatch)
    worker.run()
    assert [e["seq"] for e in events] == [1, 2]
    # Only the EOF after seq 1 is a (single) failure; silence never is.
    assert errors == ["event_stream_ended"]
    # Every quiet window re-read the run record and resumed from the cursor.
    assert len(records) == quiet_windows + 2
    assert client.event_requests == [None] + [1] * (quiet_windows + 1)


def test_quiet_windows_do_not_hide_real_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    client = ScriptedClient([])
    client.replies[("GET", f"/v1/runs/{RID}")] = deque(
        [record("running")] + [ClientError("runtime_unreachable")] * RunFollower.MAX_FAILURES
    )
    client.event_batches = deque([ClientError("event_stream_timeout")])
    worker, _, _, errors = follower(client, monkeypatch)
    worker.run()
    assert errors == ["runtime_unreachable"] * RunFollower.MAX_FAILURES + ["stream_retry_exhausted"]


def test_parked_run_is_polled_until_external_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    client = ScriptedClient([])
    prompt = {"id": "prompt-1", "toolName": "example", "args": {}}
    client.replies[("GET", f"/v1/runs/{RID}")] = deque(
        [
            record("parked", confirmations=[prompt]),
            record("parked", confirmations=[]),
            record("cancelled"),
        ]
    )
    worker, events, records, errors = follower(client, monkeypatch)
    worker.run()
    assert len(records) == 3
    assert not events and not errors and not client.event_requests


def test_restart_can_end_run_with_persisted_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    client = ScriptedClient([])
    client.replies[("GET", f"/v1/runs/{RID}")] = deque(
        [
            ClientError("runtime_unreachable"),
            record("failed", failure={"reason": "shutdown"}),
        ]
    )
    worker, _, records, errors = follower(client, monkeypatch)
    worker.last_seq = 12
    worker.run()
    assert records[-1]["failure"]["reason"] == "shutdown"
    assert errors == ["runtime_unreachable"]
    assert worker.last_seq == 12


def test_retries_exhaust_without_discarding_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    client = ScriptedClient([])
    client.replies[("GET", f"/v1/runs/{RID}")] = deque(
        [ClientError("runtime_unreachable")] * RunFollower.MAX_FAILURES
    )
    worker, events, records, errors = follower(client, monkeypatch)
    worker.last_seq = 42
    worker.run()
    assert worker.last_seq == 42
    assert errors[-1] == "stream_retry_exhausted"
    assert not events and not records


def test_stop_wakes_poll_wait_without_more_requests() -> None:
    client = ScriptedClient([])
    worker = RunFollower(
        client,
        RID,
        SID,
        last_seq=None,
        on_event=lambda e: None,
        on_snapshot=lambda r: None,
        on_error=lambda e: None,
    )
    worker.stop()
    worker.run()
    assert not client.calls
