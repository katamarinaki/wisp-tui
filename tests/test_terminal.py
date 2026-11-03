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
import struct
import subprocess
import sys
import termios
import time

import pytest

from tests.helpers import SID


def capture_terminal(module: str, width: int,
                     input_steps: tuple[tuple[bytes, bytes], ...] = ()) -> bytes:
    pending = list(input_steps)
    root = Path(__file__).resolve().parents[1]
    master, slave = pty.openpty()
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
            deadline = time.monotonic() + 10.0
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
                    while pending and pending[0][0] in output:
                        _, keys = pending.pop(0)
                        os.write(master, keys)
                    assert len(output) <= 2_000_000, "terminal capture exceeded limit"
                assert process.wait(timeout=2.0) == 0, "terminal fixture failed"
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=2.0)
    finally:
        os.close(master)
        if slave >= 0:
            os.close(slave)
    assert not pending, "terminal fixture exited before input checks completed"
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
    assert b"client:" in capture
    borders = re.findall(rb"\x1b\[([0-9;:]*)m(?:" + "─".encode() + rb"){10,}", capture)
    assert any(b"94" in codes.split(b";") for codes in borders), \
        "input rules did not use bright blue"
    footer_styles = re.findall(rb"\x1b\[([0-9;:]*)m(?:client:|last run )", capture)
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
