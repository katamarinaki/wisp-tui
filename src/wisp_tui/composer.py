"""Bounded prompt recall and one unsent draft, private and local to a runtime."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from . import config
from .client import UUID
from .config import ConfigError
from .submissions import MAX_DRAFT_BYTES

MAX_HISTORY = 50
MAX_HISTORY_BYTES = 256 * 1024
MAX_COMPOSER_FILE_BYTES = 4 * 1024 * 1024


class ComposerError(Exception):
    """Safe local codes only; no paths or conversation content."""


def bounded_history(prompts: Iterable[str]) -> tuple[str, ...]:
    deduplicated: list[str] = []
    for prompt in prompts:
        if prompt.strip() and (not deduplicated or deduplicated[-1] != prompt):
            deduplicated.append(prompt)
    kept: list[str] = []
    total_bytes = 0
    for prompt in reversed(deduplicated[-MAX_HISTORY:]):
        prompt_bytes = len(prompt.encode("utf-8"))
        if prompt_bytes > MAX_DRAFT_BYTES or total_bytes + prompt_bytes > MAX_HISTORY_BYTES:
            break
        kept.append(prompt)
        total_bytes += prompt_bytes
    return tuple(reversed(kept))


@dataclass(frozen=True)
class ComposerState:
    history: tuple[str, ...] = ()
    draft: str = ""
    session_id: str | None = None
    submission_key: str | None = None


def _decode(data: dict[str, Any]) -> ComposerState:
    history, draft, session, key = (
        data.get("history"),
        data.get("draft"),
        data.get("session_id"),
        data.get("submission_key"),
    )
    if (
        type(data.get("version")) is not int
        or data.get("version") != 1
        or not isinstance(history, list)
        or len(history) > MAX_HISTORY
        or not all(isinstance(prompt, str) for prompt in history)
        or not isinstance(draft, str)
        or len(draft.encode("utf-8")) > MAX_DRAFT_BYTES
        or (
            session is not None
            and (not isinstance(session, str) or not re.fullmatch(UUID, session))
        )
        or (
            key is not None
            and (not isinstance(key, str) or not re.fullmatch(rf"wisp-tui:{UUID}", key))
        )
    ):
        raise ComposerError("composer_state_invalid")
    prompts: tuple[str, ...] = tuple(history)
    if bounded_history(prompts) != prompts:
        raise ComposerError("composer_state_invalid")
    return ComposerState(prompts, draft, session, key)


class ComposerStore:
    def __init__(self, runtime_url: str, root: Path | None = None) -> None:
        self.root = root if root is not None else config.STATE_DIR
        fingerprint = hashlib.sha256(runtime_url.encode()).hexdigest()
        self.path = self.root / f"composer-{fingerprint}.json"
        self.lock_path = self.path.with_suffix(".lock")
        self._lock_fd: int | None = None

    def _lock(self) -> None:
        config.ensure_private_directory(self.root)
        if self._lock_fd is not None:
            return
        fd = os.open(
            self.lock_path,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            0o600,
        )
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
                or info.st_nlink != 1
            ):
                raise ComposerError("composer_state_insecure")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ComposerError("composer_state_busy") from None
        except BaseException:
            os.close(fd)
            raise
        self._lock_fd = fd

    def _read(self) -> ComposerState:
        try:
            self.path.lstat()
        except FileNotFoundError:
            return ComposerState()
        return _decode(config.read_private_json(self.path, MAX_COMPOSER_FILE_BYTES))

    def load(self) -> ComposerState:
        try:
            self._lock()
            return self._read()
        except (ConfigError, OSError, ValueError, RecursionError, OverflowError):
            self.close()
            raise ComposerError("composer_state_unavailable") from None
        except ComposerError:
            self.close()
            raise

    def save(self, state: ComposerState) -> None:
        try:
            self._lock()
            self._read()  # Refuse an unsafe/replaced file, even with a lease.
            data = {"version": 1, **asdict(state)}
            data["history"] = list(state.history)
            _decode(data)
            payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
            if len(payload) > MAX_COMPOSER_FILE_BYTES:
                raise ComposerError("composer_state_too_large")
            fd, name = tempfile.mkstemp(prefix=".composer-", dir=self.root)
            path = Path(name)
            try:
                with os.fdopen(fd, "wb") as target:
                    target.write(payload)
                    target.flush()
                    os.fsync(target.fileno())
                os.replace(path, self.path)
                directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                path.unlink(missing_ok=True)
        except (ConfigError, OSError, ValueError, RecursionError, OverflowError):
            raise ComposerError("composer_state_unavailable") from None

    def close(self) -> None:
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None
