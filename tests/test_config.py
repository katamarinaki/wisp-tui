from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from wisp_tui import config as config_mod
from wisp_tui.config import ConfigError, load_config, load_last_session, save_last_session


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
        write_config(path, {"url": "http://127.0.0.1:3008", "token": "t" * 43,
                            "max_turns": 0})
        with pytest.raises(ConfigError):
            load_config(path)


class TestState:
    def test_roundtrip(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        assert load_last_session(path) is None
        save_last_session("3bf1ba87-760e-4c0c-8a9d-a88a581d6bc5", path)
        assert load_last_session(path) == "3bf1ba87-760e-4c0c-8a9d-a88a581d6bc5"
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode & 0o077 == 0

    def test_corrupt_state_returns_none(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json")
        assert load_last_session(path) is None
