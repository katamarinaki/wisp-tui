"""A private, crash-safe journal for the one unacknowledged submission.

An exclusive advisory lock prevents two Wi processes from retrying/replacing
this slot at once. Write + fsync + atomic replace + directory fsync precede any
POST. An acknowledged record remains until the next reservation, so a crash
never turns a failed unlink into a silently forgotten pending request.
No credentials or runtime configuration are stored, only a URL fingerprint.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import stat
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any

from . import config
from .config import ConfigError

UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
MAX_DRAFT_BYTES = 128 * 1024
MAX_FILE = 1024 * 1024
RETRY_AGE = 23 * 3600


class SubmissionError(Exception):
    """Only safe local error codes, never filesystem or conversation contents."""


@dataclass(frozen=True)
class PendingSubmission:
    session_id: str
    draft: str
    max_turns: int
    key: str
    created_at: float = field(default_factory=time.monotonic)
    created_wall: float = field(default_factory=time.time)

    def body(self) -> dict[str, Any]:
        return {
            "input": self.draft.strip(),
            "confirmationMode": "interactive",
            "wait": False,
            "maxTurns": self.max_turns,
            "idempotencyKey": self.key,
        }

    def retry_error(self) -> str:
        age = time.time() - self.created_wall
        if age < 0:
            return "pending_submission_clock_error"
        if age >= RETRY_AGE or time.monotonic() - self.created_at >= RETRY_AGE:
            return "pending_submission_expired"
        return ""


def _decode(data: dict[str, Any]) -> tuple[PendingSubmission, str | None]:
    session, draft, turns, key, created = (
        data.get("sessionId"), data.get("draft"), data.get("maxTurns"),
        data.get("key"), data.get("createdAt"))
    run_id = data.get("runId")
    if (type(data.get("version")) is not int or data.get("version") != 1
            or not isinstance(session, str) or not re.fullmatch(UUID, session)
            or not isinstance(draft, str) or not draft.strip()
            or len(draft.encode("utf-8")) > MAX_DRAFT_BYTES
            or not isinstance(turns, int) or isinstance(turns, bool) or not 1 <= turns <= 200
            or not isinstance(key, str) or not re.fullmatch(rf"wisp-tui:{UUID}", key)
            or not isinstance(created, (int, float)) or isinstance(created, bool)
            or created < 0 or created > 253402300799 or not math.isfinite(created)
            or (run_id is not None and (not isinstance(run_id, str)
                                       or not re.fullmatch(UUID, run_id)))):
        raise SubmissionError("pending_state_invalid")
    age = max(0.0, time.time() - float(created))
    return PendingSubmission(session, draft, turns, key,
                             created_at=time.monotonic() - age,
                             created_wall=float(created)), run_id


class SubmissionStore:
    def __init__(self, runtime_url: str, root: Path | None = None) -> None:
        self.root = root if root is not None else config.STATE_DIR
        self.path = self.root / "submission.json"
        self.runtime = hashlib.sha256(runtime_url.encode()).hexdigest()
        self._lock_fd: int | None = None
        self._owns_pending = False
        self._mutex = RLock()
        # Startup evidence for suppressing an already-sent composer draft.
        self.loaded_acknowledgement: PendingSubmission | None = None

    def _lock(self) -> None:
        config._private_directory(self.root)
        if self._lock_fd is not None:
            return
        fd = os.open(self.root / "submission.lock",
                     os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                     0o600)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_nlink != 1):
                raise SubmissionError("pending_state_insecure")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SubmissionError("pending_state_busy") from None
        except BaseException:
            os.close(fd)
            raise
        self._lock_fd = fd

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
        data = config._read_private_json(self.path, MAX_FILE)
        pending, run_id = _decode(data)
        # A pending request must never be replayed against another endpoint.
        if data.get("runtime") != self.runtime:
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
            if record is not None and record[1] is None:
                self._owns_pending = True
                return record[0]
            if record is not None:
                self.loaded_acknowledgement = record[0]
            self.close()
            return None
        except (ConfigError, OSError, ValueError, RecursionError, OverflowError):
            self.close()
            raise SubmissionError("pending_state_unavailable") from None
        except SubmissionError:
            self.close()
            raise

    def _write(self, pending: PendingSubmission, run_id: str | None = None) -> None:
        config._private_directory(self.root)
        data = {"version": 1, "runtime": self.runtime,
                "sessionId": pending.session_id, "draft": pending.draft,
                "maxTurns": pending.max_turns, "key": pending.key,
                "createdAt": pending.created_wall, "runId": run_id}
        _decode(data)
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        if len(payload) > MAX_FILE:
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
        return (left.session_id, left.draft, left.max_turns, left.key, left.created_wall) == (
            right.session_id, right.draft, right.max_turns, right.key, right.created_wall)

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
                elif run_id is None or previous.key == pending.key:
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
            if record is None or not self._same_request(record[0], pending):
                raise SubmissionError("pending_state_conflict")
            self._write(pending, run_id)
            self.close()
        except (ConfigError, OSError, ValueError, RecursionError, OverflowError):
            raise SubmissionError("pending_state_unavailable") from None
