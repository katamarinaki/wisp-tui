from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from wisp_tui.config import ConfigError, load_config


def write_config(path: Path, data: dict[str, object], mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as target:
        json.dump(data, target)


class TestLoadConfig:
    def test_valid(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        write_config(path, {"url": "http://127.0.0.1:3008", "token": "t" * 43})
        cfg = load_config(path)
        assert cfg.url == "http://127.0.0.1:3008"
        assert cfg.max_turns == 40
        assert cfg.event_timeout == 600.0

    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError):
            load_config(tmp_path / "absent.json")

    def test_group_readable_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        write_config(path, {"url": "http://127.0.0.1:3008", "token": "t" * 43}, 0o640)
        with pytest.raises(ConfigError):
            load_config(path)

    def test_missing_fields_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        write_config(path, {"url": "http://127.0.0.1:3008"})
        with pytest.raises(ConfigError):
            load_config(path)

    def test_bad_max_turns_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        write_config(path, {"url": "http://127.0.0.1:3008", "token": "t" * 43, "max_turns": 0})
        with pytest.raises(ConfigError):
            load_config(path)


URL = "http://127.0.0.1:3008"


@pytest.mark.parametrize(
    "output, token",
    [("tok12345\n", "tok12345"), ("tok12345\nuser: me\nurl: example.invalid\n", "tok12345")],
)
def test_token_command_uses_the_first_line_it_prints(
    tmp_path: Path, output: str, token: str
) -> None:
    path = tmp_path / "config.json"
    write_config(path, {"url": URL, "token_command": ["printf", output]})
    assert load_config(path).token == token


@pytest.mark.parametrize(
    "fields, code",
    [
        ({"token_command": ["false"]}, "token_command_failed"),
        ({"token_command": ["/nonexistent/token-helper"]}, "token_command_failed"),
        ({"token_command": "printf tok12345"}, "invalid_token_command"),
        ({"token_command": []}, "invalid_token_command"),
        ({"token": "t" * 43, "token_command": ["true"]}, "token_and_token_command_conflict"),
        ({"token": "t" * 43, "log_approval_args": "yes"}, "invalid_log_approval_args"),
        ({"token": "t" * 43, "session_retention_days": 0}, "invalid_session_retention_days"),
        ({"token": "t" * 43, "session_retention_days": True}, "invalid_session_retention_days"),
    ],
)
def test_invalid_token_and_privacy_settings_are_rejected(
    tmp_path: Path, fields: dict[str, object], code: str
) -> None:
    path = tmp_path / "config.json"
    write_config(path, {"url": URL, **fields})
    with pytest.raises(ConfigError, match=f"^{code}$"):
        load_config(path)


def test_token_command_runs_only_after_every_other_field_is_valid(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    path = tmp_path / "config.json"
    write_config(path, {"url": URL, "max_turns": 0, "token_command": ["touch", str(marker)]})
    with pytest.raises(ConfigError, match=r"^invalid_max_turns$"):
        load_config(path)
    assert not marker.exists()
