"""Copyable resume hints after the terminal has been restored on normal exit."""

from __future__ import annotations

import pytest

from tests.helpers import SID, ScriptedClient
from wisp_tui import __main__ as cli
from wisp_tui import scrollback
from wisp_tui.app import WispTuiApp
from wisp_tui.config import Config

OTHER = "0b7e5a52-3c3b-4f7e-9a41-2f8d6c1e9b10"


@pytest.mark.parametrize("name", ["wi", "wisp-tui", "bad name; echo nope"])
@pytest.mark.parametrize("session_id", [SID, None])
def test_exit_hint_uses_launcher_name_and_only_an_existing_session(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
    session_id: str | None,
) -> None:
    client = ScriptedClient([])
    monkeypatch.setenv("WISP_TUI_PROG", name)
    monkeypatch.setattr(cli, "load_config", lambda: Config(url=client.url, token=client.token))
    monkeypatch.setattr(cli, "WispClient", lambda *args: client)

    def run(app: WispTuiApp, *, inline: bool, inline_no_clear: bool, mouse: bool) -> None:
        # The terminal keeps the mouse: native scrolling, selection and copy.
        assert inline and not inline_no_clear and not mouse
        assert not capsys.readouterr().out, "hint printed before terminal restoration"
        app.session_id = session_id

    monkeypatch.setattr(WispTuiApp, "run", run)
    assert cli.main([]) == 0
    output = capsys.readouterr()
    expected = name if name != "bad name; echo nope" else "wisp-tui"
    assert output.out == (f"\nTo continue: {expected} --resume {SID}\n" if session_id else "")
    assert output.err == ""
    assert client.calls == []


def test_exit_transcript_is_printed_in_order_without_empty_items(
    capsys: pytest.CaptureFixture[str],
) -> None:
    scrollback.print_transcript(
        [
            ("user", "check disk\nplease"),
            ("assistant", "\n\n"),
            ("assistant", "Let me look."),
            ("tool-call-ok", "$ df -h\n/ 40%\nbash · ok · 0.3s"),
            ("assistant", "Disk is **fine**.\n"),
        ]
    )
    # Padded rows (the filled prompt block) end in spaces; compare the text.
    lines = [line.rstrip() for line in capsys.readouterr().out.split("\n")]
    assert lines == [
        *("", "  check disk", "  please", "", ""),
        *(" Let me look.", ""),
        *(" ▌ $ df -h", " ▌ / 40%", " ▌ bash · ok · 0.3s", ""),
        *(" Disk is fine.", "", ""),
    ]


@pytest.mark.parametrize("session_id", ["not-a-session", "bad\x1b[31m\ncommand"])
def test_exit_hint_does_not_print_invalid_identifiers(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], session_id: str
) -> None:
    client = ScriptedClient([])
    monkeypatch.setattr(cli, "load_config", lambda: Config(url=client.url, token=client.token))
    monkeypatch.setattr(cli, "WispClient", lambda *args: client)

    def run(app: WispTuiApp, **kwargs: object) -> None:
        app.session_id = session_id

    monkeypatch.setattr(WispTuiApp, "run", run)
    assert cli.main([]) == 0
    assert capsys.readouterr().out == ""


def test_exit_hint_reflects_session_changed_after_launch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = ScriptedClient([])
    monkeypatch.setenv("WISP_TUI_PROG", "wi")
    monkeypatch.setattr(cli, "load_config", lambda: Config(url=client.url, token=client.token))
    monkeypatch.setattr(cli, "WispClient", lambda *args: client)

    def run(app: WispTuiApp, **kwargs: object) -> None:
        assert app.startup.session_id == SID
        app.session_id = OTHER

    monkeypatch.setattr(WispTuiApp, "run", run)
    assert cli.main(["--session", SID]) == 0
    assert capsys.readouterr().out == f"\nTo continue: wi --resume {OTHER}\n"


def test_health_check_does_not_print_resume_hint(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = ScriptedClient([])
    monkeypatch.setattr(cli, "load_config", lambda: Config(url=client.url, token=client.token))
    monkeypatch.setattr(cli, "WispClient", lambda *args: client)
    assert cli.main(["--check"]) == 0
    output = capsys.readouterr().out
    assert "runtime ready" in output
    assert "To continue" not in output


def test_invalid_token_exits_with_a_safe_code_instead_of_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("WISP_TUI_PROG", "wi")
    secret = "tok12345\nuser: me"
    monkeypatch.setattr(
        cli, "load_config", lambda: Config(url="http://127.0.0.1:3008", token=secret)
    )
    assert cli.main(["--check"]) == 1
    output = capsys.readouterr()
    assert output.err.startswith("wi: invalid_token — check ")
    assert "tok12345" not in output.err + output.out
