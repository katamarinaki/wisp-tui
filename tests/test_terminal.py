"""Check real ANSI output through a local PTY, without a runtime or sockets."""
from __future__ import annotations

import errno
import fcntl
import json
import os
from pathlib import Path
import pty
import re
import select
import signal
import struct
import subprocess
import sys
import termios
import time

import pytest

from tests.helpers import SID


def capture_terminal(module: str, width: int,
                     input_steps: tuple[tuple[bytes, bytes], ...] = (), *,
                     resize_steps: tuple[tuple[bytes, int, int], ...] = (),
                     timeout: float = 10.0) -> bytes:
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
                cwd=root, stdin=slave, stdout=slave, stderr=slave,
                env={"TERM": "xterm-256color", "LANG": "C.UTF-8",
                     "PYTHONPATH": str(root / "src"),
                     "WISP_TUI_PROG": "wi"}) as process:
            os.close(slave)
            slave = -1
            deadline = time.monotonic() + timeout
            try:
                while True:
                    assert time.monotonic() < deadline, (
                        f"terminal fixture timed out waiting for {(pending[0][0] if pending else b'exit')!r}; "
                        f"last output: {bytes(output[-1000:])!r}")
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
                        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
                        process.send_signal(signal.SIGWINCH)
                    while pending and pending[0][0] in output:
                        _, keys = pending.pop(0)
                        os.write(master, keys)
                    assert len(output) <= 2_000_000, "terminal capture exceeded limit"
                assert process.wait(timeout=2.0) == 0, "terminal fixture failed"
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
def test_inline_terminal_keeps_default_background(width: int) -> None:
    capture = capture_terminal("tests.terminal_fixture", width)
    hint = f"To continue: wi --resume {SID}".encode()
    assert capture.rstrip().endswith(hint), "resume hint missing after terminal exit"
    assert capture.count(hint) == 1, "resume hint printed more than once"
    assert capture.rfind(b"\x1b[?25h") < capture.index(hint), \
        "resume hint printed before the cursor was restored"
    prompt_styles = re.findall(rb"\x1b\[([0-9;:]*)mShow the server status", capture)
    assert prompt_styles, "prompt was not rendered"
    # Prompt blocks: muted dark teal (xterm-256 #23) with light-grey text
    # (Rich quantizes #dadada to a 250-255 grey in 256-color mode).
    assert all(re.search(rb"(?:^|;)48;5;23(?:;|$)", codes) for codes in prompt_styles), \
        "prompt block was not filled with the dark teal"
    assert all(re.search(rb"(?:^|;)38;5;25[0-5](?:;|$)", codes) for codes in prompt_styles), \
        "prompt text is not the fixed light foreground"
    response_styles = re.findall(rb"\x1b\[([0-9;:]*)mScripted response", capture)
    assert response_styles, "scripted answer was not rendered"
    assert all(b"39" in codes.split(b";") for codes in response_styles), \
        "answer did not use the terminal's default foreground"
    assert all(b"49" in codes.split(b";") for codes in response_styles), \
        "answer did not use the terminal's default background"
    assert b"Working" in capture
    assert b"thinking: on" in capture
    assert b"context: unavailable" not in capture
    assert "↑3".encode() in capture
    assert b"~/example" in capture, "launch directory missing from the footer"
    assert "▔".encode() not in capture and "▁".encode() not in capture, \
        "inline screen drew its tall top/bottom border"
    borders = re.findall(rb"\x1b\[([0-9;:]*)m(?:" + "─".encode() + rb"){10,}", capture)
    assert any(b"94" in codes.split(b";") for codes in borders), \
        "input rules did not use bright blue"
    footer_styles = re.findall(rb"\x1b\[([0-9;:]*)m(?:~/example|last run )", capture)
    assert footer_styles and all(b"2" not in codes.split(b";") for codes in footer_styles), \
        "footer text was dimmed"
    assert b"Wisp asks for approval" in capture
    assert b"\x1b[?1049h" not in capture, "alternate screen was enabled"
    sgr = re.findall(rb"\x1b\[([0-9;:]*)m", capture)
    assert any(b"49" in codes.split(b";") for codes in sgr)
    # The prompt fill is the only painted background (owner-approved).
    extended = [codes for codes in sgr if re.search(rb"(?:^|[;:])48(?:[;:]|$)", codes)]
    assert extended, "prompt fill missing"
    assert all(re.search(rb"(?:^|;)48;5;23(?:;|$)", codes) for codes in extended), \
        "an extended background other than the prompt fill was emitted"


@pytest.mark.parametrize("width", [40, 100])
def test_bracketed_paste_and_newline_bytes_require_explicit_send(width: int) -> None:
    from tests.editor_terminal_fixture import PASTE, PROMPT

    capture = capture_terminal("tests.editor_terminal_fixture", width, (
        (b"EDITOR_READY", b"\x1b[200~" + PASTE.encode() + b"\x1b[201~"),
        (b"EDITOR_PASTE_READY", b"\x1b[1;5F\ntail"),  # Ctrl+End, Ctrl+J, text
        (b"EDITOR_NEWLINE_READY", b"\r"),  # Only this physical Enter sends.
        (b"EDITOR_HISTORY_READY", b"\x10"),  # Ctrl+P recalls without sending.
        (b"EDITOR_RECALL_READY", b"\x0e"),  # Ctrl+N restores the empty draft.
    ))
    assert b"\x1b[?2004h" in capture, "bracketed paste was not enabled"
    result_line = capture.split(b"EDITOR_RESULT=", 1)[1].splitlines()[0]
    result = json.loads(result_line)
    assert result == {"paste_checked": True, "newline_checked": True,
                      "history_checked": True, "scratch_checked": True,
                      "inputs": [PROMPT.strip()]}
    sgr = re.findall(rb"\x1b\[([0-9;:]*)m", capture)
    extended = [codes for codes in sgr if re.search(rb"(?:^|[;:])48(?:[;:]|$)", codes)]
    assert all(re.search(rb"(?:^|;)48;5;23(?:;|$)", codes) for codes in extended), \
        "editor selection/brackets painted a non-default background"


@pytest.mark.parametrize("width", [40, 100])
def test_busy_spinner_does_not_retransmit_the_unchanged_chat(width: int) -> None:
    capture = capture_terminal("tests.traffic_terminal_fixture", width)
    result_line = capture.split(b"TRAFFIC_RESULT=", 1)[1].splitlines()[0]
    result = json.loads(result_line)
    assert 4 <= result["frames"] <= 20, "spinner must still animate at a bounded rate"
    assert result["transcript_rewrites"] == 0, "idle spinner retransmitted the chat"
    assert result["bytes"] < 6000, "one second of quiet work flooded the terminal"
    assert b"\x1b[?1049h" not in capture, "alternate screen was enabled"
    sgr = re.findall(rb"\x1b\[([0-9;:]*)m", capture)
    assert not any(re.search(rb"(?:^|[;:])48(?:[;:]|$)", codes) for codes in sgr), \
        "quiet-work repaint painted a background"


@pytest.mark.parametrize("width", [40, 100])
def test_tool_details_fold_with_real_keys_without_painting_a_background(width: int) -> None:
    capture = capture_terminal("tests.tool_terminal_fixture", width, (
        (b"TOOL_READY", b"\r"),
        (b"TOOL_EXPANDED", b"\r"),
    ))
    result = json.loads(capture.split(b"TOOL_RESULT=", 1)[1].splitlines()[0])
    assert result == {"expanded": True, "collapsed": True, "draft": "keep draft"}
    assert b"SCRIPTED_TOOL_OUTPUT [/]" in capture
    assert b"Input" in capture and b"Output" in capture
    assert b"\x1b[?1049h" not in capture, "alternate screen was enabled"
    sgr = re.findall(rb"\x1b\[([0-9;:]*)m", capture)
    assert not any(re.search(rb"(?:^|[;:])48(?:[;:]|$)", codes) for codes in sgr), \
        "expanded/focused tool details painted a background"


@pytest.mark.parametrize("width", [40, 100])
def test_follow_pause_and_resume_with_real_keys_keep_the_default_background(width: int) -> None:
    capture = capture_terminal("tests.scroll_terminal_fixture", width, (
        (b"SCROLL_READY", b"\x1b[5~"),  # PageUp with the transcript focused.
        (b"SCROLL_PAUSED_READY", b"\x1bOS"),  # F4 resumes following.
    ))
    result = json.loads(capture.split(b"SCROLL_RESULT=", 1)[1].splitlines()[0])
    assert result == {"paused": True, "resumed": True}
    assert b"Follow paused" in capture and b"click to resume" in capture
    assert b"\x1b[?1049h" not in capture, "alternate screen was enabled"
    sgr = re.findall(rb"\x1b\[([0-9;:]*)m", capture)
    assert not any(re.search(rb"(?:^|[;:])48(?:[;:]|$)", codes) for codes in sgr), \
        "follow controls painted a background"


def test_long_stream_resize_and_modal_keys_in_a_real_inline_terminal() -> None:
    capture = capture_terminal("tests.usability_terminal_fixture", 100, (
        (b"SESSION_READY", b"\x1b[B\r"),
        (b"MODEL_READY", b"\x1b[B\x1b[B\r"),
        (b"APPROVAL_READY", b"\x1b[F"),
        (b"APPROVAL_SCROLLED", b"n"),
    ), resize_steps=((b"RESIZE_NARROW", 40, 18), (b"RESIZE_WIDE", 100, 30)), timeout=20.0)
    result = json.loads(capture.split(b"USABILITY_RESULT=", 1)[1].splitlines()[0])
    assert result["session"] == "session-1"
    assert result["model"] == {"mode": "model", "model": "scripted-model", "thinking": True}
    assert result["approval"] == "deny"
    assert result["characters"] > 12000
    assert result["draft"] == "keep this draft"
    assert b"\x1b[?1049h" not in capture, "alternate screen was enabled"
    sgr = re.findall(rb"\x1b\[([0-9;:]*)m", capture)
    assert not any(re.search(rb"(?:^|[;:])48(?:[;:]|$)", codes) for codes in sgr), \
        "stream, picker or approval painted a background"
    for restore in (b"\x1b[?25h", b"\x1b[?2004l", b"\x1b[?1000l", b"\x1b[?1006l"):
        assert restore in capture and capture.rfind(restore) < capture.index(b"USABILITY_RESULT=")
