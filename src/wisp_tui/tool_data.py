"""Bounded literal tool detail formatting shared by live events and history."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

MAX_DETAIL_CHARS = 12 * 1024
MAX_DETAIL_LINES = 200
MAX_SUMMARY_CHARS = 200


def _json_prefix(value: Any, limit: int, indent: int | None = None) -> str:
    """The first ``limit`` characters of ``value`` as JSON, without encoding the rest."""
    chunks: list[str] = []
    remaining = limit
    for chunk in json.JSONEncoder(indent=indent, ensure_ascii=False).iterencode(value):
        chunks.append(chunk[:remaining])
        remaining -= len(chunks[-1])
        if remaining == 0:
            break
    return "".join(chunks)


def detail_text(value: Any) -> str:
    """Format only an explicitly supplied field, never a whole runtime event."""
    try:
        if isinstance(value, str):
            text = value[: MAX_DETAIL_CHARS + 1]
        else:
            text = _json_prefix(value, MAX_DETAIL_CHARS + 1, indent=2)
    except (TypeError, ValueError, RecursionError):
        return "unsupported_tool_detail"
    truncated = len(text) > MAX_DETAIL_CHARS
    text = text[:MAX_DETAIL_CHARS].replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(char if char.isprintable() or char in "\n\t" else "�" for char in text)
    lines = text.split("\n")
    bounded = "\n".join(lines[:MAX_DETAIL_LINES])
    if truncated or len(lines) > MAX_DETAIL_LINES:
        bounded += "\n… tool detail truncated"
    return bounded or "(empty)"


# Wisp's shell tool result: {"exitCode", "stdout", "stderr", "timedOut"}.
_SHELL_RESULT_KEYS = frozenset(("exitCode", "stdout", "stderr", "timedOut"))
MAX_DECODE_CHARS = 1024 * 1024


def output_text(text: str) -> str:
    """Tool output for display; a shell result shows its streams, not the JSON."""
    shown = text
    if text.startswith("{") and len(text) <= MAX_DECODE_CHARS:
        try:
            value = json.loads(text)
        except (ValueError, RecursionError):
            value = None
        if (
            isinstance(value, dict)
            and "stdout" in value
            and set(value) <= _SHELL_RESULT_KEYS
            and all(isinstance(value.get(key, ""), str) for key in ("stdout", "stderr"))
        ):
            code = value.get("exitCode")
            notes = []
            if isinstance(code, int) and not isinstance(code, bool) and code != 0:
                notes.append(f"exit code {code}")
            if value.get("timedOut") is True:
                notes.append("timed out")
            streams = (value["stdout"], value.get("stderr", ""), " · ".join(notes))
            shown = "\n".join(part.rstrip("\n") for part in streams if part.strip())
    return detail_text(shown)


def command_summary(args: Any) -> str:
    """One title line: a shell command verbatim, else compact JSON arguments."""
    if isinstance(args, dict) and isinstance(args.get("command"), str):
        command = args["command"].strip()
        if not command:
            return ""
        text = "$ " + command[: MAX_SUMMARY_CHARS + 1]
    else:
        try:
            text = _json_prefix(args, MAX_SUMMARY_CHARS + 1)
        except (TypeError, ValueError, RecursionError):
            return ""
        if text in ("{}", "[]", "null", '""'):
            return ""
    lines = text.strip().splitlines() or [""]
    line = "".join(char if char.isprintable() else "�" for char in lines[0])
    if len(lines) > 1 or len(line) > MAX_SUMMARY_CHARS:
        line = line[: MAX_SUMMARY_CHARS - 1] + "…"
    return line


@dataclass(frozen=True)
class ToolCallInfo:
    name: str
    call_id: str = ""
    input_text: str | None = None
    summary: str = ""


@dataclass(frozen=True)
class ToolHistoryInfo:
    input_text: str | None
    output_text: str
    status: str = ""
    summary: str = ""
