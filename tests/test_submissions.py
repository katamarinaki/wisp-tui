"""Crash/restart submission recovery, private storage and cross-process leases."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from collections import deque
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Input

from tests.helpers import RID, SID
from tests.test_app import chat_text, eventually, make_app
from wisp_tui.app import Startup
from wisp_tui.client import ClientError
from wisp_tui.submissions import (
    MAX_DRAFT_BYTES, MAX_FILE, RETRY_AGE, PendingSubmission, SubmissionError, SubmissionStore,
)

URL = "http://127.0.0.1:3008"
KEY = "wisp-tui:9a576438-11c6-4ce5-baf1-539f46fda639"
RUN_PATH = f"/v1/sessions/{SID}/runs"
COMPLETED: list[dict[str, Any]] = [
    {"seq": 1, "kind": "run.completed", "outcome": {"output": {"text": "done"}}},
]


def pending(**changes: Any) -> PendingSubmission:
    return replace(PendingSubmission(SID, " original task\nsecond line ", 17, KEY), **changes)


def test_round_trip_is_private_bounded_and_keeps_original_identity(tmp_path: Path) -> None:
    store = SubmissionStore(URL, tmp_path / "state")
    original = pending(created_wall=time.time() - 60)
    assert store.prepare(original) is None
    for path, mode in ((store.root, 0o700), (store.path, 0o600),
                       (store.root / "submission.lock", 0o600)):
        assert stat.S_IMODE(path.stat().st_mode) == mode
    data = json.loads(store.path.read_text())
    assert "token" not in data and "url" not in data
    assert data["runtime"] != URL
    assert store.path.stat().st_size < MAX_FILE
    store.close()
    resumed = SubmissionStore(URL, store.root)
    loaded = resumed.load()
    assert loaded is not None
    assert loaded.body() == original.body()
    assert loaded.draft == original.draft and loaded.created_wall == original.created_wall
    assert time.monotonic() - loaded.created_at >= 60
    resumed.close()


def test_acknowledged_slot_can_be_reused_but_same_request_needs_no_post(tmp_path: Path) -> None:
    store = SubmissionStore(URL, tmp_path / "state")
    original = pending()
    store.prepare(original)
    store.acknowledge(original, RID)
    assert store.load() is None
    assert store.prepare(original) == RID
    store.close()
    newer = pending(key="wisp-tui:75f8f484-cfec-460b-a2c8-12c4f8d544f4")
    assert store.prepare(newer) is None
    store.close()
    loaded = store.load()
    assert loaded is not None and loaded.key == newer.key
    store.close()


def test_other_process_cannot_take_a_live_lease(tmp_path: Path) -> None:
    store = SubmissionStore(URL, tmp_path / "state")
    store.prepare(pending())
    program = (
        "from pathlib import Path\n"
        "from wisp_tui.submissions import SubmissionStore, SubmissionError\n"
        "import sys\n"
        "try:\n"
        "    SubmissionStore('http://127.0.0.1:3008', Path(sys.argv[1])).load()\n"
        "except SubmissionError as error:\n"
        "    print(str(error))\n"
    )
    result = subprocess.run([sys.executable, "-c", program, str(store.root)],
                            capture_output=True, text=True, timeout=10,
                            env={"PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")})
    assert result.returncode == 0
    assert result.stdout.strip() == "pending_state_busy"
    store.close()


def test_unacknowledged_record_is_not_overwritten_even_after_lock_release(tmp_path: Path) -> None:
    store = SubmissionStore(URL, tmp_path / "state")
    original = pending()
    store.prepare(original)
    store.close()
    contender = SubmissionStore(URL, store.root)
    with pytest.raises(SubmissionError, match="^pending_state_conflict$"):
        contender.prepare(pending(key="wisp-tui:75f8f484-cfec-460b-a2c8-12c4f8d544f4"))
    contender.close()
    assert json.loads(store.path.read_text())["key"] == original.key


@pytest.mark.parametrize("attack", [
    "symlink", "hardlink", "group_readable", "fifo", "oversized", "bad_json",
    "insecure_dir", "symlink_dir", "lock_symlink", "lock_hardlink", "lock_fifo",
])
def test_unsafe_journals_and_leases_fail_closed(tmp_path: Path, attack: str) -> None:
    store = SubmissionStore(URL, tmp_path / "state")
    store.prepare(pending())
    store.close()
    outside = tmp_path / "outside"
    outside.write_text("do not touch")
    if attack == "symlink":
        store.path.unlink()
        store.path.symlink_to(outside)
    elif attack == "hardlink":
        os.link(store.path, tmp_path / "hardlink")
    elif attack == "group_readable":
        store.path.chmod(0o640)
    elif attack == "fifo":
        store.path.unlink()
        os.mkfifo(store.path, 0o600)
    elif attack == "oversized":
        store.path.write_bytes(b"x" * (MAX_FILE + 1))
    elif attack == "bad_json":
        store.path.write_text("private invalid json")
    elif attack == "insecure_dir":
        store.root.chmod(0o755)
    elif attack == "symlink_dir":
        real = tmp_path / "real"
        store.root.rename(real)
        store.root.symlink_to(real, target_is_directory=True)
    else:
        lock = store.root / "submission.lock"
        if attack == "lock_hardlink":
            os.link(lock, tmp_path / "linked-lock")
        else:
            lock.unlink()
            if attack == "lock_symlink":
                lock.symlink_to(outside)
            else:
                os.mkfifo(lock, 0o600)
    with pytest.raises(SubmissionError):
        store.load()
    assert outside.read_text() == "do not touch"
    store.close()


@pytest.mark.parametrize("field, value", [
    ("version", True), ("sessionId", "not-an-id"), ("draft", 4), ("maxTurns", True),
    ("maxTurns", 201), ("key", "new-key"), ("createdAt", float("nan")),
    ("createdAt", -1), ("createdAt", 10 ** 1000), ("runId", "not-an-id"),
])
def test_invalid_records_never_enable_a_fresh_submission(
        tmp_path: Path, field: str, value: Any) -> None:
    store = SubmissionStore(URL, tmp_path / "state")
    store.prepare(pending())
    store.close()
    data = json.loads(store.path.read_text())
    data[field] = value
    store.path.write_text(json.dumps(data))
    with pytest.raises(SubmissionError, match="^pending_state_invalid$"):
        store.load()


@pytest.mark.parametrize("failed_sync", [1, 2])
def test_failed_fsync_never_claims_a_durable_reservation(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed_sync: int) -> None:
    store = SubmissionStore(URL, tmp_path / "state")
    original = pending()
    fsync = os.fsync
    calls = 0

    def fail_once(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == failed_sync:
            raise OSError("private filesystem detail")
        fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_once)
    with pytest.raises(SubmissionError, match="^pending_state_unavailable$"):
        store.prepare(original)
    assert not list(store.root.glob(".submission-*"))
    # Retrying the original request safely re-syncs a possibly replaced file.
    assert store.prepare(original) is None
    store.close()
    loaded = store.load()
    assert loaded is not None and loaded.key == original.key
    store.close()


async def test_restart_restores_original_request_but_never_auto_posts(
        monkeypatch: pytest.MonkeyPatch, private_state_dir: Path) -> None:
    app, client = make_app([], monkeypatch)
    client.replies[("POST", RUN_PATH)] = deque([ClientError("runtime_timeout")])
    async with app.run_test() as pilot:
        await eventually(lambda: "ready" in app._status_state)
        app.session_id = SID
        editor = app.query_one("#input", Input)
        editor.value = "original task\nsecond line"
        await pilot.press("enter")
        await eventually(lambda: "submission error" in app._status_state)
        first_body = next(body for method, path, body in client.calls if path == RUN_PATH)
        editor.value = "new unsent draft"
    # A new app/client simulates process exit; state lives only in the temp dir.
    resumed, resumed_client = make_app(COMPLETED, monkeypatch)
    resumed.config = replace(resumed.config, max_turns=1)
    resumed.startup = Startup("session", RID)  # Can't sidestep recovery with another session.
    async with resumed.run_test() as pilot:
        await eventually(lambda: "restored" in resumed._status_state)
        assert resumed.session_id == SID
        assert resumed.query_one("#input", Input).value == "original task\nsecond line"
        await eventually(lambda: resumed._recovery_view_token is None)
        assert not any(method == "POST" for method, _, _ in resumed_client.calls)
        assert {path for _, path, _ in resumed_client.calls} == {
            "/v1/health", f"/v1/sessions/{SID}", f"/v1/sessions/{SID}/history",
            f"/v1/runs?sessionId={SID}&status=queued,running,parked&limit=200"}
        assert resumed._selection.get("mode") == "balanced"
        assert "past answer" in chat_text(resumed)
        resumed._handle_command("/new")
        resumed.query_one("#input", Input).value = "do not send this new draft"
        await pilot.press("enter")
        assert not any(method == "POST" for method, _, _ in resumed_client.calls)
        resumed._handle_command("/retry")
        await eventually(lambda: resumed._status_state == "")
        retried = next(body for method, path, body in resumed_client.calls if path == RUN_PATH)
        assert retried == first_body
        assert resumed.query_one("#input", Input).value == "do not send this new draft"
        assert "done" in chat_text(resumed)
    assert json.loads((private_state_dir / "submission.json").read_text())["runId"] == RID


async def test_pending_is_fsynced_before_first_network_post(
        monkeypatch: pytest.MonkeyPatch, private_state_dir: Path) -> None:
    app, client = make_app(COMPLETED, monkeypatch)
    syncs: list[int] = []
    fsync = os.fsync

    def record_sync(fd: int) -> None:
        syncs.append(fd)
        fsync(fd)

    def check_request(method: str, path: str) -> None:
        if (method, path) == ("POST", RUN_PATH):
            assert len(syncs) >= 2  # File and directory barriers precede POST.
            record = json.loads((private_state_dir / "submission.json").read_text())
            assert record["draft"] == "durable first" and record["runId"] is None

    monkeypatch.setattr(os, "fsync", record_sync)
    client.request_hook = check_request
    async with app.run_test() as pilot:
        await eventually(lambda: "ready" in app._status_state)
        app.session_id = SID
        app.query_one("#input", Input).value = "durable first"
        await pilot.press("enter")
        await eventually(lambda: app._status_state == "")


@pytest.mark.parametrize("cause", ["write_failure", "too_large"])
async def test_no_post_without_durable_storage(
        monkeypatch: pytest.MonkeyPatch, cause: str) -> None:
    app, client = make_app([], monkeypatch)

    def fail(_: PendingSubmission) -> str | None:
        raise SubmissionError("pending_state_unavailable")

    if cause == "write_failure":
        monkeypatch.setattr(app.submissions, "prepare", fail)
    async with app.run_test() as pilot:
        await eventually(lambda: "ready" in app._status_state)
        app.session_id = SID
        editor = app.query_one("#input", Input)
        draft = "kept" if cause == "write_failure" else "x" * (MAX_DRAFT_BYTES + 1)
        editor.value = draft
        await pilot.press("enter")
        if cause == "write_failure":
            await eventually(lambda: "pending_state_unavailable" in app._status_state)
        else:
            assert "safe recovery limit" in chat_text(app)
        assert editor.value == draft
        assert not any(method == "POST" for method, _, _ in client.calls)


@pytest.mark.parametrize("age", [RETRY_AGE + 1, -3600])
async def test_expired_or_clock_reversed_recovery_stays_blocked(
        monkeypatch: pytest.MonkeyPatch, private_state_dir: Path, age: int) -> None:
    store = SubmissionStore(URL, private_state_dir)
    store.prepare(pending(created_wall=time.time() - age))
    store.close()
    app, client = make_app([], monkeypatch)
    async with app.run_test():
        await eventually(lambda: "restored" in app._status_state)
        app._handle_command("/retry")
        assert app._status_state in ("pending_submission_expired", "pending_submission_clock_error")
        app._handle_command("/new")
        assert app.session_id == SID
        assert not any(method == "POST" for method, _, _ in client.calls)
        assert (private_state_dir / "submission.json").exists()


async def test_bad_state_blocks_all_new_work_and_shows_no_raw_contents(
        monkeypatch: pytest.MonkeyPatch, private_state_dir: Path) -> None:
    path = private_state_dir / "submission.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    with os.fdopen(fd, "w") as target:
        target.write("private raw file content")
    app, client = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        await eventually(lambda: app._pending_state_error)
        app.query_one("#input", Input).value = "do not send"
        await pilot.press("enter")
        app._handle_command("/new")
        app.action_model()
        assert app.query_one("#input", Input).value == "do not send"
        assert not any(method in ("POST", "PATCH") for method, _, _ in client.calls)
        assert "private raw file content" not in app._status_state + chat_text(app)


async def test_live_other_client_is_rechecked_only_on_explicit_retry(
        monkeypatch: pytest.MonkeyPatch, private_state_dir: Path) -> None:
    owner = SubmissionStore(URL, private_state_dir)
    original = pending()
    owner.prepare(original)
    app, client = make_app(COMPLETED, monkeypatch)
    async with app.run_test():
        await eventually(lambda: app._pending_state_error)
        assert "pending_state_busy" in app._status_state
        assert not any(method == "POST" for method, _, _ in client.calls)
        owner.close()
        app._handle_command("/retry")
        await eventually(lambda: app._status_state == "")
        posted = next(body for method, path, body in client.calls if path == RUN_PATH)
        assert posted == original.body()


async def test_failed_ack_write_retains_original_key_for_restart_retry(
        monkeypatch: pytest.MonkeyPatch) -> None:
    app, client = make_app([], monkeypatch)

    def fail_ack(_: PendingSubmission, __: str) -> None:
        raise SubmissionError("pending_state_unavailable")

    monkeypatch.setattr(app.submissions, "acknowledge", fail_ack)
    async with app.run_test() as pilot:
        await eventually(lambda: "ready" in app._status_state)
        app.session_id = SID
        app.query_one("#input", Input).value = "ack was lost"
        await pilot.press("enter")
        await eventually(lambda: "submission error" in app._status_state)
        posted = next(body for method, path, body in client.calls if path == RUN_PATH)
        assert app._pending_submission is not None
    resumed, resumed_client = make_app(COMPLETED, monkeypatch)
    async with resumed.run_test():
        await eventually(lambda: "restored" in resumed._status_state)
        resumed._handle_command("/retry")
        await eventually(lambda: resumed._status_state == "")
        retried = next(body for method, path, body in resumed_client.calls if path == RUN_PATH)
        assert retried == posted


@pytest.mark.parametrize("acknowledged", [False, True])
def test_endpoint_binding_only_prevents_replaying_pending_work(
        tmp_path: Path, acknowledged: bool) -> None:
    store = SubmissionStore(URL, tmp_path / "state")
    original = pending()
    store.prepare(original)
    if acknowledged:
        store.acknowledge(original, RID)
    store.close()
    other = SubmissionStore("http://127.0.0.1:3009", store.root)
    if acknowledged:
        assert other.load() is None
    else:
        with pytest.raises(SubmissionError, match="^pending_runtime_mismatch$"):
            other.load()
    other.close()


def test_excessively_nested_json_is_a_safe_state_error(tmp_path: Path) -> None:
    store = SubmissionStore(URL, tmp_path / "state")
    store.prepare(pending())
    store.close()
    store.path.write_text('{"value":' + '[' * 3000 + '0' + ']' * 3000 + '}')
    with pytest.raises(SubmissionError, match="^pending_state_(invalid|unavailable)$"):
        store.load()


def test_ack_directory_sync_failure_keeps_known_id_without_another_post(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = SubmissionStore(URL, tmp_path / "state")
    original = pending()
    store.prepare(original)
    fsync = os.fsync
    count = 0

    def fail_directory_once(fd: int) -> None:
        nonlocal count
        count += 1
        if count == 2:
            raise OSError("private detail")
        fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_directory_once)
    with pytest.raises(SubmissionError, match="^pending_state_unavailable$"):
        store.acknowledge(original, RID)
    assert store.prepare(original) == RID
    store.acknowledge(original, RID)
    assert store.load() is None


def test_late_ack_cannot_overwrite_a_newer_reservation(tmp_path: Path) -> None:
    store = SubmissionStore(URL, tmp_path / "state")
    original = pending()
    store.prepare(original)
    store.acknowledge(original, RID)
    newer = pending(key="wisp-tui:75f8f484-cfec-460b-a2c8-12c4f8d544f4")
    store.prepare(newer)
    store.close()
    with pytest.raises(SubmissionError, match="^pending_state_conflict$"):
        store.acknowledge(original, RID)
    assert json.loads(store.path.read_text())["key"] == newer.key
    store.close()


async def test_recheck_after_other_client_acknowledges_unblocks_without_posting(
        monkeypatch: pytest.MonkeyPatch, private_state_dir: Path) -> None:
    owner = SubmissionStore(URL, private_state_dir)
    original = pending()
    owner.prepare(original)
    app, client = make_app([], monkeypatch)
    async with app.run_test():
        await eventually(lambda: app._pending_state_error)
        owner.acknowledge(original, RID)
        app._handle_command("/retry")
        assert not bool(app._pending_state_error)
        assert "new session" in app._status_state
        assert not any(method == "POST" for method, _, _ in client.calls)
