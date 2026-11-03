"""Private configuration and state handling, modeled on the ask-wisp proxy.

The config file holds the same kind of credential as ask-wisp and must never
live inside the (group-readable) project tree. Expected layout::

    ~/.config/wisp-tui/config.json   {"url": "http://127.0.0.1:3008", "token": "...", ...}

State (last opened session) lives under ~/.local/state/wisp-tui/.
"""
from __future__ import annotations

import json
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CONFIG_PATH = Path.home() / ".config/wisp-tui/config.json"
STATE_DIR = Path.home() / ".local/state/wisp-tui"
STATE_PATH = STATE_DIR / "state.json"
MAX_CONFIG = 16384
MAX_STATE = 4096


class ConfigError(Exception):
    """Raised when the private configuration or state is missing or unsafe."""


def _private_directory(path: Path) -> None:
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ConfigError("insecure_private_directory")


def _read_private_json(path: Path, maximum: int) -> dict[str, Any]:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size > maximum):
                raise ConfigError("insecure_or_invalid_private_file")
            raw = source.read(maximum + 1)
        if len(raw) > maximum:
            raise ConfigError("insecure_or_invalid_private_file")
        data = json.loads(raw)
    except OSError:
        raise ConfigError("private_file_unavailable") from None
    except ValueError:
        raise ConfigError("invalid_private_json") from None
    if not isinstance(data, dict):
        raise ConfigError("invalid_private_json")
    return data


def _write_private_json(path: Path, data: dict[str, Any]) -> None:
    _private_directory(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=".wisp-tui-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as target:
            os.fchmod(target.fileno(), 0o600)
            target.write((json.dumps(data, indent=2) + "\n").encode())
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@dataclass(frozen=True)
class Config:
    url: str
    token: str
    max_turns: int = 40
    event_timeout: float = 600.0


def load_config(path: Path = CONFIG_PATH) -> Config:
    _private_directory(path.parent)
    data = _read_private_json(path, MAX_CONFIG)
    url = data.get("url")
    token = data.get("token")
    if not isinstance(url, str) or not isinstance(token, str):
        raise ConfigError("url_and_token_required")
    max_turns = data.get("max_turns", 40)
    if not isinstance(max_turns, int) or not 1 <= max_turns <= 200:
        raise ConfigError("invalid_max_turns")
    event_timeout = data.get("event_timeout", 600.0)
    if not isinstance(event_timeout, (int, float)) or not 10.0 <= float(event_timeout) <= 7200.0:
        raise ConfigError("invalid_event_timeout")
    return Config(url=url, token=token, max_turns=max_turns, event_timeout=float(event_timeout))


def load_last_session(path: Path = STATE_PATH) -> str | None:
    try:
        data = _read_private_json(path, MAX_STATE)
    except ConfigError:
        return None
    session = data.get("last_session")
    return session if isinstance(session, str) and session else None


def save_last_session(session_id: str, path: Path = STATE_PATH) -> None:
    _write_private_json(path, {"last_session": session_id})
