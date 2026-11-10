"""A private, crash-safe journal for the one unacknowledged submission.

An exclusive advisory lock prevents two Wi processes from retrying/replacing
this slot at once. Write + fsync + atomic replace + directory fsync precede any
POST. An acknowledged record remains until the next reservation, so a crash
never turns a failed unlink into a silently forgotten pending request.
No credentials or runtime configuration are stored, only a URL fingerprint.
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any

from . import config
from .client import UUID, valid_project_root
from .config import ConfigError

MAX_DRAFT_BYTES = 128 * 1024
MAX_SUBMISSION_FILE_BYTES = 1024 * 1024
MAX_RETRY_AGE_SECONDS = 23 * 3600
IDEMPOTENCY_KEY_RE = re.compile(rf"wisp-tui:{UUID}")


def new_idempotency_key() -> str:
    return f"wisp-tui:{uuid.uuid4()}"


class SubmissionError(Exception):
    """Only safe local error codes, never filesystem or conversation contents."""


@dataclass(frozen=True)
class PendingSubmission:
    session_id: str
    draft: str
    max_turns: int
    idempotency_key: str
    project_root: str | None = None
    created_monotonic: float = field(default_factory=time.monotonic)
    created_wall: float = field(default_factory=time.time)

    def body(self) -> dict[str, Any]:
        return {
            "input": self.draft.strip(),
            "confirmationMode": "interactive",
            "wait": False,
            "maxTurns": self.max_turns,
            "idempotencyKey": self.idempotency_key,
        }

    def retry_error(self) -> str:
        age = time.time() - self.created_wall
        if age < 0:
            return "pending_submission_clock_error"
        if (
            age >= MAX_RETRY_AGE_SECONDS
            or time.monotonic() - self.created_monotonic >= MAX_RETRY_AGE_SECONDS
        ):
            return "pending_submission_expired"
        return ""


def _decode(data: dict[str, Any]) -> tuple[PendingSubmission, str | None]:
    session, draft, turns, key, created = (
        data.get("sessionId"),
        data.get("draft"),
        data.get("maxTurns"),
        data.get("key"),
        data.get("createdAt"),
    )
    run_id = data.get("runId")
    # Version 2 adds the session's Project folder; version 1 requests have none.
    root = data.get("projectRoot")
    if (
        type(data.get("version")) is not int
        or data.get("version") not in (1, 2)
        or (root is not None and (data["version"] == 1 or valid_project_root(root) is None))
        or not isinstance(session, str)
        or not re.fullmatch(UUID, session)
        or not isinstance(draft, str)
        or not draft.strip()
        or len(draft.encode("utf-8")) > MAX_DRAFT_BYTES
        or not isinstance(turns, int)
        or isinstance(turns, bool)
        or not 1 <= turns <= config.MAX_TURNS
        or not isinstance(key, str)
        or not IDEMPOTENCY_KEY_RE.fullmatch(key)
        or not isinstance(created, (int, float))
        or isinstance(created, bool)
        or created < 0
        or created > 253402300799
        or not math.isfinite(created)
        or (run_id is not None and (not isinstance(run_id, str) or not re.fullmatch(UUID, run_id)))
    ):
        raise SubmissionError("pending_state_invalid")
    age = max(0.0, time.time() - float(created))
    return PendingSubmission(
        session,
        draft,
        turns,
        key,
        root,
        created_monotonic=time.monotonic() - age,
        created_wall=float(created),
    ), run_id


class SubmissionStore:
    def __init__(self, runtime_url: str, root: Path | None = None) -> None:
        self.root = root if root is not None else config.STATE_DIR
        self.path = self.root / "submission.json"
        self.runtime_fingerprint = config.runtime_fingerprint(runtime_url)
        self._lock_fd: int | None = None
        self._owns_pending = False
        self._mutex = RLock()
        # Startup evidence for suppressing an already-sent composer draft.
        self.loaded_acknowledgement: PendingSubmission | None = None

    def _lock(self) -> None:
        config.ensure_private_directory(self.root)
        if self._lock_fd is None:
            try:
                self._lock_fd = config.lock_private_file(self.root / "submission.lock")
            except ConfigError as error:
                raise SubmissionError(f"pending_state_{error}") from None

    def close(self) -> None:
        """Release the lease, but never discard a pending request on exit."""
        with self._mutex:
            if self._lock_fd is not None:
                os.close(self._lock_fd)
                self._lock_fd = None
            self._owns_pending = False

    def _read(self) -> tuple[PendingSubmission, str | None] | None:
        try:
            self.path.lstat()
        except FileNotFoundError:
            return None
        data = config.read_private_json(self.path, MAX_SUBMISSION_FILE_BYTES)
        pending, run_id = _decode(data)
        # A pending request must never be replayed against another endpoint.
        if data.get("runtime") != self.runtime_fingerprint:
            if run_id is not None:
                return None  # An acknowledged slot does not bind future work.
            raise SubmissionError("pending_runtime_mismatch")
        return pending, run_id

    def load(self) -> PendingSubmission | None:
        """Reserve an orphaned pending request; acknowledged slots are reusable."""
        with self._mutex:
            return self._load()

    def _load(self) -> PendingSubmission | None:
        self.loaded_acknowledgement = None
        try:
            self._lock()
            record = self._read()
            if record is None:
                self.close()
                return None
            pending, run_id = record
            if run_id is None:
                self._owns_pending = True
                return pending
            self.loaded_acknowledgement = pending
            self.close()
            return None
        except (ConfigError, OSError, ValueError, RecursionError, OverflowError):
            self.close()
            raise SubmissionError("pending_state_unavailable") from None
        except SubmissionError:
            self.close()
            raise

    def _write(self, pending: PendingSubmission, run_id: str | None = None) -> None:
        config.ensure_private_directory(self.root)
        data = {
            "version": 2,
            "runtime": self.runtime_fingerprint,
            "sessionId": pending.session_id,
            "draft": pending.draft,
            "maxTurns": pending.max_turns,
            "key": pending.idempotency_key,
            "projectRoot": pending.project_root,
            "createdAt": pending.created_wall,
            "runId": run_id,
        }
        _decode(data)
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        if len(payload) > MAX_SUBMISSION_FILE_BYTES:
            raise SubmissionError("pending_state_too_large")
        fd, name = tempfile.mkstemp(prefix=".submission-", dir=self.root)
        path = Path(name)
        try:
            with os.fdopen(fd, "wb") as target:
                target.write(payload)
                target.flush()
                os.fsync(target.fileno())
            os.replace(path, self.path)
            if run_id is None:
                # Replacement may have happened even if directory fsync fails.
                self._owns_pending = True
            directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            path.unlink(missing_ok=True)

    @staticmethod
    def _same_request(left: PendingSubmission, right: PendingSubmission) -> bool:
        def identity(
            request: PendingSubmission,
        ) -> tuple[str, str, int, str, str | None, float]:
            return (
                request.session_id,
                request.draft,
                request.max_turns,
                request.idempotency_key,
                request.project_root,
                request.created_wall,
            )

        return identity(left) == identity(right)

    def prepare(self, pending: PendingSubmission) -> str | None:
        """Durably reserve before POST. A previously saved ack needs no POST."""
        with self._mutex:
            try:
                return self._prepare(pending)
            except BaseException:
                # Failed validation, conflicts and pre-replacement I/O errors
                # must not monopolize a slot this instance never reserved.
                if not self._owns_pending:
                    self.close()
                raise

    def _prepare(self, pending: PendingSubmission) -> str | None:
        try:
            self._lock()
            record = self._read()
            if record is not None:
                previous, run_id = record
                if self._same_request(previous, pending):
                    if run_id is not None:
                        return run_id
                    self._owns_pending = True
                elif run_id is None or previous.idempotency_key == pending.idempotency_key:
                    raise SubmissionError("pending_state_conflict")
            # Re-sync even an existing pending record after a previous I/O error.
            self._write(pending)
            return None
        except (ConfigError, OSError, ValueError, RecursionError, OverflowError):
            raise SubmissionError("pending_state_unavailable") from None

    def acknowledge(self, pending: PendingSubmission, run_id: str) -> None:
        with self._mutex:
            self._acknowledge(pending, run_id)

    def _acknowledge(self, pending: PendingSubmission, run_id: str) -> None:
        try:
            self._lock()
            record = self._read()
            if record is None:
                raise SubmissionError("pending_state_conflict")
            previous, _ = record
            if not self._same_request(previous, pending):
                raise SubmissionError("pending_state_conflict")
            self._write(pending, run_id)
            self.close()
        except (ConfigError, OSError, ValueError, RecursionError, OverflowError):
            raise SubmissionError("pending_state_unavailable") from None
