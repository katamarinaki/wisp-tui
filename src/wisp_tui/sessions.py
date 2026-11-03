"""Private local record of sessions used from this TUI (Pi-style JSONL files).

The Wisp runtime stays the source of truth for conversation history; these
files are the owner's local record and drive ``--continue``/``--resume``::

    ~/.local/state/wisp-tui/sessions/<UTC time>_<session-id>.jsonl

The first line is a ``session`` header; each later line is one entry
(``message``, ``tool``, ``approval``, ``run``). Files are 0600 in a 0700
directory, opened without following symlinks and owner-checked, like the
config. Size is bounded per file and per entry. Writing never raises into
the UI: failures return a safe code instead.
"""
from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config
from .config import ConfigError

UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
FILE_RE = re.compile(rf"(\d{{8}}T\d{{6}}Z)_({UUID})\.jsonl")
MAX_FILE = 16 * 1024 * 1024
MAX_TEXT = 256 * 1024
MAX_SCAN = 1024 * 1024  # bytes read per file when listing
FORMAT_VERSION = 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _bounded(value: Any) -> Any:
    if isinstance(value, str) and len(value) > MAX_TEXT:
        return value[:MAX_TEXT] + "…"
    return value


@dataclass(frozen=True)
class SessionSummary:
    id: str
    title: str
    updated: float
    path: Path

    def label(self) -> str:
        when = datetime.fromtimestamp(self.updated).strftime("%Y-%m-%d %H:%M")
        title = self.title.replace("\n", " ").strip() or "(untitled)"
        if len(title) > 70:
            title = title[:69] + "…"
        return f"{title}  ·  {when}  ·  {self.id[:8]}"


class SessionStore:
    def __init__(self, root: Path | None = None) -> None:
        # Resolved at construction so tests can redirect config.STATE_DIR.
        self.root = root if root is not None else config.STATE_DIR / "sessions"

    # -- writing ----------------------------------------------------------------

    def record(self, session_id: str, entry: dict[str, Any], title: str = "") -> str:
        """Append ``entry``; create the file (with header) on first use.

        Returns "" on success or a safe error code. Never raises.
        """
        if not re.fullmatch(UUID, session_id):
            return "invalid_session_id"
        try:
            path = self.path_for(session_id)
            line = {"type": entry.get("type", "message"), "timestamp": _now(),
                    **{key: _bounded(value) for key, value in entry.items() if key != "type"}}
            payload = json.dumps(line, ensure_ascii=False) + "\n"
            if path is None:
                config._private_directory(self.root)
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                path = self.root / f"{stamp}_{session_id}.jsonl"
                header = {"type": "session", "version": FORMAT_VERSION, "id": session_id,
                          "timestamp": _now(), "title": _bounded(title)}
                payload = json.dumps(header, ensure_ascii=False) + "\n" + payload
            return self._append(path, payload.encode())
        except ConfigError:
            return "local_sessions_insecure"
        except (OSError, ValueError):
            return "local_sessions_unavailable"

    def _append(self, path: Path, data: bytes) -> str:
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
        fd = os.open(path, flags, 0o600)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_nlink != 1):
                return "local_sessions_insecure"
            if info.st_size + len(data) > MAX_FILE:
                return "local_session_full"
            os.write(fd, data)
            return ""
        finally:
            os.close(fd)

    # -- reading ----------------------------------------------------------------

    def _files(self) -> list[tuple[Path, str, float]]:
        try:
            info = self.root.lstat()
        except OSError:
            return []
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077):
            return []
        found: list[tuple[Path, str, float]] = []
        for path in self.root.iterdir():
            match = FILE_RE.fullmatch(path.name)
            if not match:
                continue
            try:
                file_info = path.lstat()
            except OSError:
                continue
            if (stat.S_ISREG(file_info.st_mode) and file_info.st_uid == os.getuid()
                    and not file_info.st_mode & 0o077):
                found.append((path, match.group(2), file_info.st_mtime))
        return found

    def path_for(self, session_id: str) -> Path | None:
        matches = [path for path, sid, _ in self._files() if sid == session_id]
        return min(matches) if matches else None

    def find(self, prefix: str) -> list[str]:
        """Saved session IDs starting with ``prefix`` (case-insensitive), newest first."""
        prefix = prefix.lower()
        newest: dict[str, float] = {}
        for _, sid, mtime in self._files():
            if sid.startswith(prefix):
                newest[sid] = max(mtime, newest.get(sid, mtime))
        return sorted(newest, key=lambda sid: newest[sid], reverse=True)

    def latest(self) -> str | None:
        files = self._files()
        return max(files, key=lambda item: item[2])[1] if files else None

    def list(self, limit: int = 100) -> list[SessionSummary]:
        """Newest first; title is the header title or the first prompt."""
        newest: dict[str, tuple[Path, float]] = {}
        for path, sid, mtime in self._files():
            if sid not in newest or mtime > newest[sid][1]:
                newest[sid] = (path, mtime)
        ordered = sorted(newest.items(), key=lambda item: item[1][1], reverse=True)
        return [SessionSummary(sid, self._title(path), mtime, path)
                for sid, (path, mtime) in ordered[:limit]]

    def _title(self, path: Path) -> str:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            with os.fdopen(fd, "rb") as source:
                raw = source.read(MAX_SCAN)
        except OSError:
            return ""
        title = ""
        for line in raw.splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            if entry.get("type") == "session" and isinstance(entry.get("title"), str):
                title = entry["title"]
            elif (entry.get("type") == "message" and entry.get("role") == "user"
                    and isinstance(entry.get("text"), str)):
                return title or entry["text"]
        return title
