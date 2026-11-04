"""Prepare failures release only unowned leases, not ambiguous saved requests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.helpers import RID
from tests.test_submissions import URL, pending
from wisp_tui.submissions import PendingSubmission, SubmissionError, SubmissionStore


@pytest.mark.parametrize("acknowledged", [False, True])
def test_pre_replace_failure_releases_unowned_slot_without_closing_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, acknowledged: bool
) -> None:
    store = SubmissionStore(URL, tmp_path / "state")
    if acknowledged:
        original = pending()
        store.prepare(original)
        store.acknowledge(original, RID)

    def fail(_: PendingSubmission, run_id: str | None = None) -> None:
        raise OSError("private disk detail")

    monkeypatch.setattr(store, "_write", fail)
    with pytest.raises(SubmissionError, match=r"^pending_state_unavailable$"):
        store.prepare(pending(idempotency_key="wisp-tui:75f8f484-cfec-460b-a2c8-12c4f8d544f4"))
    contender = SubmissionStore(URL, store.root)
    assert contender.load() is None
    assert not store._owns_pending
    assert store._lock_fd is None
    contender.close()


def test_conflict_releases_unowned_lease_but_keeps_existing_request(tmp_path: Path) -> None:
    original = pending()
    owner = SubmissionStore(URL, tmp_path / "state")
    owner.prepare(original)
    owner.close()
    contender = SubmissionStore(URL, owner.root)
    with pytest.raises(SubmissionError, match=r"^pending_state_conflict$"):
        contender.prepare(pending(idempotency_key="wisp-tui:75f8f484-cfec-460b-a2c8-12c4f8d544f4"))
    # No contender.close(): the legitimate recovery must already be possible.
    recovered = owner.load()
    assert recovered is not None and recovered.body() == original.body()
    owner.close()


def test_endpoint_mismatch_does_not_lock_out_the_original_runtime(tmp_path: Path) -> None:
    original = pending()
    owner = SubmissionStore(URL, tmp_path / "state")
    owner.prepare(original)
    owner.close()
    wrong = SubmissionStore("http://127.0.0.1:3009", owner.root)
    with pytest.raises(SubmissionError, match=r"^pending_runtime_mismatch$"):
        wrong.prepare(pending())
    recovered = owner.load()
    assert recovered is not None and recovered.body() == original.body()
    owner.close()


@pytest.mark.parametrize("restore_first", [False, True])
def test_owned_pending_keeps_lease_if_resync_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, restore_first: bool
) -> None:
    original = pending()
    store = SubmissionStore(URL, tmp_path / "state")
    store.prepare(original)
    if restore_first:
        store.close()
        restored = store.load()
        assert restored is not None
        original = restored

    def fail(fd: int) -> None:
        raise OSError("private disk detail")

    monkeypatch.setattr(os, "fsync", fail)
    with pytest.raises(SubmissionError, match=r"^pending_state_unavailable$"):
        store.prepare(original)
    contender = SubmissionStore(URL, store.root)
    with pytest.raises(SubmissionError, match=r"^pending_state_busy$"):
        contender.load()
    assert store._owns_pending
    store.close()
    restored = contender.load()
    assert restored is not None and restored.body() == original.body()
    contender.close()


def test_failed_directory_sync_after_replace_retains_new_request_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = pending()
    store = SubmissionStore(URL, tmp_path / "state")
    real_fsync = os.fsync
    count = 0

    def fail_directory(fd: int) -> None:
        nonlocal count
        count += 1
        if count == 2:
            raise OSError("private disk detail")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_directory)
    with pytest.raises(SubmissionError, match=r"^pending_state_unavailable$"):
        store.prepare(original)
    assert store._owns_pending
    contender = SubmissionStore(URL, store.root)
    with pytest.raises(SubmissionError, match=r"^pending_state_busy$"):
        contender.load()
    store.close()
    restored = contender.load()
    assert restored is not None and restored.body() == original.body()
    contender.close()
