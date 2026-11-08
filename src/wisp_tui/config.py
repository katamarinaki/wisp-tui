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

import json
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CONFIG_PATH = Path.home() / ".config/wisp-tui/config.json"
STATE_DIR = Path.home() / ".local/state/wisp-tui"
MAX_CONFIG_BYTES = 16384


class ConfigError(Exception):
    """Raised when the private configuration or state is missing or unsafe."""


def ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ConfigError("insecure_private_directory")


def read_private_json(path: Path, maximum: int) -> dict[str, Any]:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
                or info.st_nlink != 1
                or info.st_size > maximum
            ):
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


@dataclass(frozen=True)
class Config:
    url: str
    token: str
    max_turns: int = 40
    event_timeout: float = 600.0
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
    max_turns = data.get("max_turns", 40)
    if not isinstance(max_turns, int) or not 1 <= max_turns <= 200:
        raise ConfigError("invalid_max_turns")
    event_timeout = data.get("event_timeout", 600.0)
    if not isinstance(event_timeout, (int, float)) or not 10.0 <= float(event_timeout) <= 7200.0:
        raise ConfigError("invalid_event_timeout")
    log_args = data.get("log_approval_args", False)
    if not isinstance(log_args, bool):
        raise ConfigError("invalid_log_approval_args")
    retention = data.get("session_retention_days")
    if retention is not None and (
        not isinstance(retention, int) or isinstance(retention, bool) or not 1 <= retention <= 36500
    ):
        raise ConfigError("invalid_session_retention_days")
    accent = data.get("accent_color", "bright_blue")
    if not isinstance(accent, str) or accent not in ACCENT_COLORS:
        raise ConfigError("invalid_accent_color")
    # Last, so a typo elsewhere never triggers a password prompt first.
    secret = token if isinstance(token, str) else _token_from_command(command)
    return Config(
        url=url,
        token=secret,
        max_turns=max_turns,
        event_timeout=float(event_timeout),
        log_approval_args=log_args,
        session_retention_days=retention,
        accent_color=accent,
    )
