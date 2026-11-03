"""Suite-wide isolation: no test may touch the owner's real state directory."""
from __future__ import annotations

from pathlib import Path

import pytest

from wisp_tui import config


@pytest.fixture(autouse=True)
def private_state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    monkeypatch.setattr(config, "STATE_DIR", state)
    return state
