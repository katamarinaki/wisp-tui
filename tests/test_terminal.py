"""Check real ANSI output through a local PTY, without a runtime or sockets."""

from __future__ import annotations

import errno
import fcntl
import json
import os
import pty
import re
import select
import signal
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path

import pytest

from tests.editor_terminal_fixture import PASTE, PROMPT
from tests.helpers import SID
from wisp_tui import __version__
from wisp_tui.terminal_status import TabStatus, tab_status_sequence


def capture_terminal(
    module: str,
    width: int,
    input_steps: tuple[tuple[bytes, bytes], ...] = (),
    *,
    resize_steps: tuple[tuple[bytes, int, int], ...] = (),
    timeout: float = 120.0,
) -> bytes:
    # Timeouts here only catch hangs; a slow but correct run must never hit them.
    pending = list(input_steps)
    resizes = list(resize_steps)
    root = Path(__file__).resolve().parents[1]
    master, slave = pty.openpty()
    original_termios = termios.tcgetattr(master)
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, width, 0, 0))
    output = bytearray()
    try:
        with subprocess.Popen(
            [sys.executable, "-m", module],
            cwd=root,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env={
                "TERM": "xterm-256color",
                "LANG": "C.UTF-8",
                "PYTHONPATH": str(root / "src"),
                "WISP_TUI_PROG": "wi",
            },
        ) as process:
            os.close(slave)
            slave = -1
            deadline = time.monotonic() + timeout
            try:
                while True:
                    waiting = pending[0][0] if pending else b"exit"
                    assert time.monotonic() < deadline, (
                        f"terminal fixture timed out waiting for {waiting!r}; "
                        f"last output: {bytes(output[-1000:])!r}"
                    )
                    ready, _, _ = select.select([master], [], [], 0.1)
                    if not ready:
                        continue
                    try:
                        chunk = os.read(master, 65536)
                    except OSError as error:
                        if error.errno != errno.EIO:
                            raise
                        break
                    if not chunk:
                        break
                    output.extend(chunk)
                    while resizes and resizes[0][0] in output:
                        _, columns, rows = resizes.pop(0)
                        fcntl.ioctl(
                            master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0)
                        )
                        process.send_signal(signal.SIGWINCH)
                    while pending and pending[0][0] in output:
                        _, keys = pending.pop(0)
                        os.write(master, keys)
                    assert len(output) <= 2_000_000, "terminal capture exceeded limit"
                assert process.wait(timeout=60.0) == 0, "terminal fixture failed"
                assert termios.tcgetattr(master) == original_termios, "terminal modes not restored"
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=2.0)
    finally:
        os.close(master)
        if slave >= 0:
            os.close(slave)
    assert not pending, "terminal fixture exited before input checks completed"
    assert not resizes, "terminal fixture exited before resize checks completed"
    return bytes(output)


@pytest.mark.parametrize("width", [40, 100])
def test_terminal_keeps_default_background(width: int) -> None:
    capture = capture_terminal("tests.terminal_fixture", width)
    startup = capture.split(b"Show the server status", 1)[0]
    assert b"session new" not in startup
    assert b"model: runtime default" not in startup
    assert b"your first message starts it" not in startup
    assert b"T: 0" in startup
    assert f"wisp-tui v{__version__}".encode() in startup
    assert "▟█▙▟███▙".encode() in startup
    assert b"/help commands" not in startup
    hint = f"To continue: wi --resume {SID}".encode()
    assert capture.rstrip().endswith(hint), "resume hint missing after terminal exit"
    assert capture.count(hint) == 1, "resume hint printed more than once"
    statuses = re.findall(rb"\x1b\]21337;[^\x1b]*\x1b\\", capture)
    assert statuses == [
        tab_status_sequence(status).encode()
        for status in (
            TabStatus.CONNECTING,
            TabStatus.IDLE,
            TabStatus.WORKING,
            TabStatus.WAITING,
            TabStatus.IDLE,
            None,
        )
    ]
    assert capture.index(statuses[-1]) < capture.index(hint)
    assert capture.rfind(b"\x1b[?25h") < capture.index(hint), (
        "resume hint printed before the cursor was restored"
    )
    prompt_styles = re.findall(rb"\x1b\[([0-9;:]*)mShow the server status", capture)
    assert prompt_styles, "prompt was not rendered"
    # Prompt blocks: muted dark teal (xterm-256 #23) with light-grey text
    # (Rich quantizes #dadada to a 250-255 grey in 256-color mode).
    assert all(re.search(rb"(?:^|;)48;5;23(?:;|$)", codes) for codes in prompt_styles), (
        "prompt block was not filled with the dark teal"
    )
    assert all(re.search(rb"(?:^|;)38;5;25[0-5](?:;|$)", codes) for codes in prompt_styles), (
        "prompt text is not the fixed light foreground"
    )
    # Answers are printed to scrollback after a reset: only attributes (bold)
    # or explicit defaults, never a fixed foreground or background color.
    response_styles = re.findall(rb"\x1b\[([0-9;:]*)mScripted response", capture)
    assert response_styles, "scripted answer was not rendered"
    assert all(
        part in (b"0", b"1", b"2", b"39", b"49")
        for codes in response_styles
        for part in codes.split(b";")
    ), "answer did not use the terminal's default colors"
    printed = capture.index(b"Scripted response")
    assert printed < capture.index(b"Wisp asks for approval"), (
        "answer was not printed to scrollback before the approval frame"
    )
    assert b"Working" in capture
    assert b"thinking: on" in capture
    assert b"context: unavailable" not in capture
    assert "↑3".encode() in capture
    assert b"~/example" in capture, "launch directory missing from the footer"
    assert "▔".encode() not in capture and "▁".encode() not in capture, (
        "screen drew a tall top/bottom border"
    )
    borders = re.findall(rb"\x1b\[([0-9;:]*)m(?:" + "─".encode() + rb"){10,}", capture)
    assert any(b"94" in codes.split(b";") for codes in borders), (
        "input rules did not use bright blue"
    )
    footer_styles = re.findall(rb"\x1b\[([0-9;:]*)m(?:~/example|last run )", capture)
    assert footer_styles and all(b"2" not in codes.split(b";") for codes in footer_styles), (
        "footer text was dimmed"
    )
    assert b"Wisp asks for approval" in capture
    # Full screen pins the composer; the shell screen comes back on exit.
    assert b"\x1b[?1049h" in capture and b"\x1b[?1049l" in capture
    sgr = re.findall(rb"\x1b\[([0-9;:]*)m", capture)
    assert any(b"49" in codes.split(b";") for codes in sgr)
    # Only the owner-approved fills: prompts, and tool blocks (ok / failed).
    extended = [codes for codes in sgr if re.search(rb"(?:^|[;:])48(?:[;:]|$)", codes)]
    assert extended, "prompt fill missing"
    assert all(re.search(rb"(?:^|;)48;5;(?:23|236|52)(?:;|$)", codes) for codes in extended), (
        "an extended background other than the approved fills was emitted"
    )


@pytest.mark.parametrize("width", [40, 100])
def test_bracketed_paste_and_newline_bytes_require_explicit_send(width: int) -> None:

    capture = capture_terminal(
        "tests.editor_terminal_fixture",
        width,
        (
            (b"EDITOR_READY", b"\x1b[200~" + PASTE.encode() + b"\x1b[201~"),
            (b"EDITOR_PASTE_READY", b"\x1b[1;5F\ntail"),  # Ctrl+End, Ctrl+J, text
            (b"EDITOR_NEWLINE_READY", b"\r"),  # Only this physical Enter sends.
            (b"EDITOR_HISTORY_READY", b"\x10"),  # Ctrl+P recalls without sending.
            (b"EDITOR_RECALL_READY", b"\x0e"),  # Ctrl+N restores the empty draft.
        ),
    )
    assert b"\x1b[?2004h" in capture, "bracketed paste was not enabled"
    result_line = capture.split(b"EDITOR_RESULT=", 1)[1].splitlines()[0]
    result = json.loads(result_line)
    assert result == {
        "paste_checked": True,
        "newline_checked": True,
        "history_checked": True,
        "scratch_checked": True,
        "inputs": [PROMPT.strip()],
    }
    sgr = re.findall(rb"\x1b\[([0-9;:]*)m", capture)
    extended = [codes for codes in sgr if re.search(rb"(?:^|[;:])48(?:[;:]|$)", codes)]
    assert all(re.search(rb"(?:^|;)48;5;23(?:;|$)", codes) for codes in extended), (
        "editor selection/brackets painted a non-default background"
    )


@pytest.mark.parametrize("width", [40, 100])
def test_busy_spinner_does_not_retransmit_the_unchanged_chat(width: int) -> None:
    capture = capture_terminal("tests.traffic_terminal_fixture", width)
    result_line = capture.split(b"TRAFFIC_RESULT=", 1)[1].splitlines()[0]
    result = json.loads(result_line)
    assert result["ticks"] >= 10
    # At most one repaint per spinner tick (plus one in flight when counting stops).
    assert 1 <= result["frames"] <= result["ticks"] + 1, "spinner repaint rate is unbounded"
    assert result["transcript_rewrites"] == 0, "idle spinner retransmitted the chat"
    # A spinner frame repaints only the composer's three rows.
    assert result["bytes"] < 3 * 400 * result["frames"], "quiet spinner frames flooded the terminal"
    # Full screen pins the composer; the shell screen comes back on exit.
    assert b"\x1b[?1049h" in capture and b"\x1b[?1049l" in capture
    sgr = re.findall(rb"\x1b\[([0-9;:]*)m", capture)
    assert not any(re.search(rb"(?:^|[;:])48(?:[;:]|$)", codes) for codes in sgr), (
        "quiet-work repaint painted a background"
    )


def test_long_stream_resize_and_modal_keys_in_a_real_terminal() -> None:
    capture = capture_terminal(
        "tests.usability_terminal_fixture",
        100,
        (
            (b"SESSION_READY", b"\x1b[B\r"),
            (b"MODEL_READY", b"\x1b[B\x1b[B\r"),
            (b"APPROVAL_READY", b"\x1b[F"),
            (b"APPROVAL_SCROLLED", b"n"),
        ),
        resize_steps=((b"RESIZE_NARROW", 40, 18), (b"RESIZE_WIDE", 100, 30)),
    )
    result = json.loads(capture.split(b"USABILITY_RESULT=", 1)[1].splitlines()[0])
    assert result["session"] == "session-1"
    assert result["model"] == {"mode": "model", "model": "scripted-model", "thinking": True}
    assert result["approval"] == "deny"
    assert result["draft"] == "keep this draft"
    # Full screen pins the composer; the shell screen comes back on exit.
    assert b"\x1b[?1049h" in capture and b"\x1b[?1049l" in capture
    sgr = re.findall(rb"\x1b\[([0-9;:]*)m", capture)
    assert not any(re.search(rb"(?:^|[;:])48(?:[;:]|$)", codes) for codes in sgr), (
        "stream, picker or approval painted a background"
    )
    for restore in (b"\x1b[?25h", b"\x1b[?2004l"):
        assert restore in capture and capture.rfind(restore) < capture.index(b"USABILITY_RESULT=")
