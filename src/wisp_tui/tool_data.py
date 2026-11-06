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


def _flatten(value: Any, path: str, lines: list[str]) -> None:
    """``path: value`` lines for nested JSON, in the order the tool returned it."""
    if len(lines) > MAX_DETAIL_LINES:
        return
    if len(path) > MAX_DETAIL_CHARS:
        # Deep, long keys would otherwise copy ever-longer paths per level.
        lines.append(path[:MAX_DETAIL_CHARS])
        return
    nested = isinstance(value, list) and any(isinstance(item, (dict, list)) for item in value)
    if isinstance(value, dict) and value:
        for key, item in value.items():
            _flatten(item, f"{path}.{key}" if path else str(key), lines)
    elif nested:
        for index, item in enumerate(value):
            _flatten(item, f"{path}.{index}" if path else str(index), lines)
    else:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        lines.append(f"{path}: {text}" if path else text)


# MCP results: {"content": [{"type": "text", "text": ...}, ...], "isError": ...}.
_MCP_RESULT_KEYS = frozenset(("content", "isError", "structuredContent", "_meta"))


def _content_blocks(value: Any) -> list[Any] | None:
    if isinstance(value, dict) and "content" in value and set(value) <= _MCP_RESULT_KEYS:
        value = value["content"]
    if (
        isinstance(value, list)
        and value
        and all(isinstance(block, dict) and isinstance(block.get("type"), str) for block in value)
    ):
        return value
    return None


def _flatten_result(value: Any, lines: list[str]) -> None:
    """Text content blocks as their text; everything else as ``path: value``."""
    blocks = _content_blocks(value)
    if blocks is None:
        _flatten(value, "", lines)
        return
    for index, block in enumerate(blocks):
        if block["type"] == "text" and isinstance(block.get("text"), str):
            lines.extend(block["text"].rstrip("\n").split("\n"))
        else:
            _flatten(block, str(index), lines)
    if isinstance(value, dict) and value.get("isError") is True:
        lines.append("error")


def output_text(text: str) -> str:
    """Tool output for display: shell streams, JSON as ``path: value`` lines."""
    shown = text
    if text[:1] in ("{", "[") and len(text) <= MAX_DECODE_CHARS:
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
        elif isinstance(value, (dict, list)) and value:
            lines: list[str] = []
            try:
                _flatten_result(value, lines)
            except RecursionError:
                lines = []
            shown = "\n".join(lines) or text
    return detail_text(shown)


def command_summary(args: Any) -> str:
    """One title line: a shell command verbatim, else compact JSON arguments.

    MCP calls wrap their input as {"name", "arguments"}: show the quoted inner
    name (ToolCallView drops it when it repeats the tool name) and arguments.
    """
    prefix = ""
    if isinstance(args, dict) and set(args) == {"arguments", "name"}:
        # JSON-quoted, so it can never read as a "$ " shell title.
        try:
            prefix = _json_prefix(args["name"], MAX_SUMMARY_CHARS + 1) + " "
        except (TypeError, ValueError, RecursionError):
            return ""
        args = args["arguments"]
    elif isinstance(args, dict) and isinstance(args.get("command"), str):
        command = args["command"].strip()
        if not command:
            return ""
        args = None
        prefix = "$ " + command[: MAX_SUMMARY_CHARS + 1]
    try:
        text = "" if args is None else _json_prefix(args, MAX_SUMMARY_CHARS + 1)
    except (TypeError, ValueError, RecursionError):
        return ""
    if text in ("{}", "[]", "null", '""'):
        text = ""
    text = (prefix + text).strip()
    if not text:
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
