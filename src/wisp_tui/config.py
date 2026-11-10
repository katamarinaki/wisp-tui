"""Private configuration and state handling, modeled on the ask-wisp proxy.

The config file holds the same kind of credential as ask-wisp and must never
live inside the (group-readable) project tree. Expected layout::

    ~/.config/wisp-tui/config.json   {"url": "http://127.0.0.1:3008", "token": "...", ...}

Instead of ``token``, ``token_command`` may name an argv list (run without a
shell) that prints the token, e.g. a password manager lookup.

State (saved sessions, composer draft/history, the pending-submission journal
and its lock) lives under ~/.local/state/wisp-tui/.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CONFIG_DIR = Path.home() / ".config/wisp-tui"
CONFIG_PATH = CONFIG_DIR / "config.json"
STATE_DIR = Path.home() / ".local/state/wisp-tui"
MAX_CONFIG_BYTES = 16384
MAX_TURNS = 200


class ConfigError(Exception):
    """Raised when the private configuration or state is missing or unsafe."""


class ConfigMissingError(ConfigError):
    """The private file does not exist or cannot be opened."""


def is_private_dir(info: os.stat_result) -> bool:
    return stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077


def ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    if not is_private_dir(path.lstat()):
        raise ConfigError("insecure_private_directory")


def is_private_file(info: os.stat_result) -> bool:
    return (
        stat.S_ISREG(info.st_mode)
        and info.st_uid == os.getuid()
        and not info.st_mode & 0o077
        and info.st_nlink == 1
    )


def lock_private_file(path: Path) -> int:
    """Hold an exclusive lock on an owner-only file; ConfigError "busy" or "insecure"."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, 0o600)
    try:
        if not is_private_file(os.fstat(fd)):
            raise ConfigError("insecure")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ConfigError("busy") from None
    except BaseException:
        os.close(fd)
        raise
    return fd


def runtime_fingerprint(url: str) -> str:
    """Names local state per runtime without storing its URL."""
    return hashlib.sha256(url.encode()).hexdigest()


def read_private_json(path: Path, maximum: int) -> dict[str, Any]:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if not is_private_file(info) or info.st_size > maximum:
                raise ConfigError("insecure_or_invalid_private_file")
            raw = source.read(maximum + 1)
        if len(raw) > maximum:
            raise ConfigError("insecure_or_invalid_private_file")
        data = json.loads(raw)
    except OSError:
        raise ConfigMissingError("private_file_unavailable") from None
    except ValueError:
        raise ConfigError("invalid_private_json") from None
    if not isinstance(data, dict):
        raise ConfigError("invalid_private_json")
    return data


@dataclass(frozen=True)
class Config:
    url: str
    token: str
    max_turns: int = 40
    log_approval_args: bool = False
    session_retention_days: int | None = None
    accent_color: str = "bright_blue"


# The terminal's own 16 palette colors, so the accent follows its theme.
ACCENT_COLORS = frozenset(
    prefix + name
    for prefix in ("", "bright_")
    for name in ("black", "red", "green", "yellow", "blue", "magenta", "cyan", "white")
)


def _token_from_command(command: object) -> str:
    if (
        not isinstance(command, list)
        or not 1 <= len(command) <= 32
        or not all(isinstance(arg, str) and arg and "\0" not in arg for arg in command)
    ):
        raise ConfigError("invalid_token_command")
    try:
        # stdin and stderr stay on the terminal so the command can prompt.
        result = subprocess.run(command, stdout=subprocess.PIPE, timeout=120, check=False)
    except (OSError, subprocess.SubprocessError, ValueError):
        raise ConfigError("token_command_failed") from None
    if result.returncode != 0:
        raise ConfigError("token_command_failed")
    # Like `pass show`, the secret is the first line; any later lines are metadata.
    lines = result.stdout.decode("utf-8", "replace").strip().splitlines()
    return lines[0].strip() if lines else ""


def load_config(path: Path = CONFIG_PATH) -> Config:
    ensure_private_directory(path.parent)
    data = read_private_json(path, MAX_CONFIG_BYTES)
    url = data.get("url")
    token = data.get("token")
    command = data.get("token_command")
    if command is not None and token is not None:
        raise ConfigError("token_and_token_command_conflict")
    if not isinstance(url, str) or (command is None and not isinstance(token, str)):
        raise ConfigError("url_and_token_required")
    max_turns = data.get("max_turns", Config.max_turns)
    if not isinstance(max_turns, int) or not 1 <= max_turns <= MAX_TURNS:
        raise ConfigError("invalid_max_turns")
    log_args = data.get("log_approval_args", Config.log_approval_args)
    if not isinstance(log_args, bool):
        raise ConfigError("invalid_log_approval_args")
    retention = data.get("session_retention_days")
    if retention is not None and (
        not isinstance(retention, int) or isinstance(retention, bool) or not 1 <= retention <= 36500
    ):
        raise ConfigError("invalid_session_retention_days")
    accent = data.get("accent_color", Config.accent_color)
    if not isinstance(accent, str) or accent not in ACCENT_COLORS:
        raise ConfigError("invalid_accent_color")
    # Last, so a typo elsewhere never triggers a password prompt first.
    secret = token if isinstance(token, str) else _token_from_command(command)
    return Config(
        url=url,
        token=secret,
        max_turns=max_turns,
        log_approval_args=log_args,
        session_retention_days=retention,
        accent_color=accent,
    )
