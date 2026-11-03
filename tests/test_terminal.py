"""Check real ANSI output through a local PTY, without a runtime or sockets."""
from __future__ import annotations

import errno
import fcntl
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


@pytest.mark.parametrize("width", [40, 100])
def test_inline_terminal_keeps_default_background(width: int) -> None:
    root = Path(__file__).resolve().parents[1]
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, width, 0, 0))
    output = bytearray()
    try:
        with subprocess.Popen(
                [sys.executable, "-m", "tests.terminal_fixture"],
                cwd=root, stdin=slave, stdout=slave, stderr=slave,
                env={"TERM": "xterm-256color", "LANG": "C.UTF-8",
                     "PYTHONPATH": str(root / "src")}) as process:
            os.close(slave)
            slave = -1
            deadline = time.monotonic() + 10.0
            try:
                while True:
                    assert time.monotonic() < deadline, "terminal fixture timed out"
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
    capture = bytes(output)
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
    assert b"Wisp asks for approval" in capture
    assert b"\x1b[?1049h" not in capture, "alternate screen was enabled"
    sgr = re.findall(rb"\x1b\[([0-9;:]*)m", capture)
    assert any(b"49" in codes.split(b";") for codes in sgr)
    # The prompt fill is the only painted background (owner-approved).
    extended = [codes for codes in sgr if re.search(rb"(?:^|[;:])48(?:[;:]|$)", codes)]
    assert extended, "prompt fill missing"
    assert all(re.search(rb"(?:^|;)48;5;23(?:;|$)", codes) for codes in extended), \
        "an extended background other than the prompt fill was emitted"
