"""Offline `wi setup` checks: scripted runtime responses and a temporary config path."""

from __future__ import annotations

import io
import stat
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path
from typing import cast

import pytest

from tests.helpers import FakeJsonResponse, FakeOpener
from wisp_tui import config, setup
from wisp_tui.client import WispClient

TOKEN = "t" * 43


@pytest.fixture
def config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "config/wisp-tui/config.json"
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    return path


@pytest.fixture
def opener(config_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeOpener:
    fake = FakeOpener()

    def client(url: str, token: str) -> WispClient:
        return WispClient(url, token, opener=cast(urllib.request.OpenerDirector, fake))

    monkeypatch.setattr(setup, "WispClient", client)
    return fake


def answer(monkeypatch: pytest.MonkeyPatch, url: str, token: str = TOKEN) -> None:
    monkeypatch.setattr("builtins.input", lambda _prompt: url)
    monkeypatch.setattr("getpass.getpass", lambda _prompt: token)


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "http://127.0.0.1:3008/v1/runs",
        code,
        "error",
        Message(),
        io.BytesIO(b'{"code": "unauthorized"}'),
    )


def test_setup_verifies_then_writes_owner_only_config(
    opener: FakeOpener,
    config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    opener.responses = [FakeJsonResponse({"status": "ready"}), FakeJsonResponse({"runs": []})]
    answer(monkeypatch, "")
    assert setup.setup("wi") == 0
    assert [r.path for r in opener.requests] == [
        "http://127.0.0.1:3008/v1/health",
        "http://127.0.0.1:3008/v1/runs?limit=1",
    ]
    assert opener.requests[1].headers["Authorization"] == "Bearer " + TOKEN
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(config_path.parent.stat().st_mode) == 0o700
    assert [p.name for p in config_path.parent.iterdir()] == ["config.json"]
    loaded = config.load_config(config_path)
    assert (loaded.url, loaded.token) == ("http://127.0.0.1:3008", TOKEN)
    output = capsys.readouterr()
    assert TOKEN not in output.out + output.err


@pytest.mark.parametrize(
    ("responses", "code"),
    [
        ([FakeJsonResponse({"status": "starting"})], "runtime_not_ready"),
        ([FakeJsonResponse({"status": "ready"}), http_error(401)], "runtime_http_401:unauthorized"),
        ([urllib.error.URLError("refused")], "runtime_unreachable"),
    ],
)
def test_setup_saves_nothing_when_verification_fails(
    opener: FakeOpener,
    config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    responses: list[object],
    code: str,
) -> None:
    opener.responses = list(responses)
    answer(monkeypatch, "http://127.0.0.1:3008")
    assert setup.setup("wi") == 1
    assert not config_path.exists()
    output = capsys.readouterr()
    assert f"wi: {code}. Nothing was saved." in output.err
    assert TOKEN not in output.out + output.err


def test_setup_rejects_non_loopback_url_before_asking_for_token(
    opener: FakeOpener,
    config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    answer(monkeypatch, "http://192.0.2.10:3008")
    monkeypatch.setattr("getpass.getpass", pytest.fail)
    assert setup.setup("wi") == 1
    assert "runtime_must_be_explicit_loopback_http" in capsys.readouterr().err
    assert opener.requests == [] and not config_path.exists()


def test_setup_never_overwrites_or_prompts_for_existing_config(
    opener: FakeOpener, config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path.parent.mkdir(parents=True, mode=0o700)
    config_path.write_text("keep")
    monkeypatch.setattr("builtins.input", pytest.fail)
    assert setup.setup("wi") == 1
    assert config_path.read_text() == "keep"
    with pytest.raises(setup.SetupError, match=r"^setup_config_exists$"):
        setup.write_config(config_path, "http://127.0.0.1:3008", TOKEN)
    assert config_path.read_text() == "keep"
    assert [p.name for p in config_path.parent.iterdir()] == ["config.json"]


def test_setup_write_failure_is_a_safe_code(
    opener: FakeOpener,
    config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path.parent.mkdir(parents=True, mode=0o700)
    config_path.parent.chmod(0o500)
    opener.responses = [FakeJsonResponse({"status": "ready"}), FakeJsonResponse({"runs": []})]
    answer(monkeypatch, "")
    try:
        assert setup.setup("wi") == 1
    finally:
        config_path.parent.chmod(0o700)
    assert "wi: setup_write_failed. Nothing was saved." in capsys.readouterr().err
    assert list(config_path.parent.iterdir()) == []


def test_setup_end_of_input_cancels(
    opener: FakeOpener,
    config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def eof(_prompt: str) -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    assert setup.setup("wi") == 1
    assert "setup_cancelled" in capsys.readouterr().err
    assert not config_path.exists()


# --- non-interactive path (agents and scripts) ---


def test_setup_token_stdin_writes_config_without_prompts(
    opener: FakeOpener,
    config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    opener.responses = [FakeJsonResponse({"status": "ready"}), FakeJsonResponse({"runs": []})]
    monkeypatch.setattr("sys.stdin", io.StringIO(TOKEN + "\n"))
    monkeypatch.setattr("builtins.input", pytest.fail)
    monkeypatch.setattr("getpass.getpass", pytest.fail)
    assert setup.main(["--url", "http://127.0.0.1:3008", "--token-stdin"]) == 0
    loaded = config.load_config(config_path)
    assert (loaded.url, loaded.token) == ("http://127.0.0.1:3008", TOKEN)
    output = capsys.readouterr()
    assert TOKEN not in output.out + output.err


def test_setup_token_stdin_requires_a_token(
    opener: FakeOpener,
    config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("\n"))
    assert setup.main(["--url", "http://127.0.0.1:3008", "--token-stdin"]) == 1
    assert "token_required" in capsys.readouterr().err
    assert not config_path.exists() and opener.requests == []


def test_setup_token_command_stores_the_command_not_the_secret(
    opener: FakeOpener,
    config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    opener.responses = [FakeJsonResponse({"status": "ready"}), FakeJsonResponse({"runs": []})]
    monkeypatch.setattr(config, "token_from_command", lambda command: TOKEN)
    monkeypatch.setattr("builtins.input", pytest.fail)
    monkeypatch.setattr("getpass.getpass", pytest.fail)
    argv = '["pass", "show", "wisp/wi"]'
    assert setup.main(["--url", "http://127.0.0.1:3008", "--token-command", argv]) == 0
    assert config_path.read_text() == (
        '{"url": "http://127.0.0.1:3008", "token_command": ["pass", "show", "wisp/wi"]}\n'
    )
    assert TOKEN not in config_path.read_text()
    output = capsys.readouterr()
    assert TOKEN not in output.out + output.err


def test_setup_token_command_runs_once_for_verification(
    opener: FakeOpener,
    config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opener.responses = [FakeJsonResponse({"status": "ready"}), FakeJsonResponse({"runs": []})]
    calls: list[object] = []

    def fake(command: object) -> str:
        calls.append(command)
        return TOKEN

    monkeypatch.setattr(config, "token_from_command", fake)
    monkeypatch.setattr("builtins.input", pytest.fail)
    monkeypatch.setattr("getpass.getpass", pytest.fail)
    assert setup.main(["--url", "http://127.0.0.1:3008", "--token-command", '["cmd"]']) == 0
    assert calls == [["cmd"]]
    assert opener.requests[1].headers["Authorization"] == "Bearer " + TOKEN


@pytest.mark.parametrize("argument", ["not json", '["cmd", 5]', '"cmd"'])
def test_setup_rejects_malformed_token_command(
    config_path: Path, argument: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert setup.main(["--url", "http://127.0.0.1:3008", "--token-command", argument]) == 2
    assert "invalid_token_command" in capsys.readouterr().err
    assert not config_path.exists()


def test_setup_rejects_token_stdin_with_token_command(
    config_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(TOKEN + "\n"))
    assert setup.main(["--token-stdin", "--token-command", '["cmd"]']) == 2
    assert "token_stdin_and_token_command_conflict" in capsys.readouterr().err
    assert not config_path.exists()


def test_setup_token_command_failure_is_a_safe_code(
    opener: FakeOpener,
    config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def failing(command: object) -> str:
        raise config.ConfigError("token_command_failed")

    monkeypatch.setattr(config, "token_from_command", failing)
    monkeypatch.setattr("builtins.input", pytest.fail)
    monkeypatch.setattr("getpass.getpass", pytest.fail)
    assert setup.main(["--url", "http://127.0.0.1:3008", "--token-command", '["cmd"]']) == 1
    assert "token_command_failed" in capsys.readouterr().err
    assert not config_path.exists() and opener.requests == []
