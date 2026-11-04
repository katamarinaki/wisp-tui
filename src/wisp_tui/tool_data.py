"""Bounded literal tool detail formatting shared by live events and history."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

MAX_DETAIL_CHARS = 12 * 1024
MAX_DETAIL_LINES = 200
MAX_SUMMARY_CHARS = 200


def detail_text(value: Any) -> str:
    """Format only an explicitly supplied field, never a whole runtime event."""
    try:
        if isinstance(value, str):
            text = value[:MAX_DETAIL_CHARS + 1]
        else:
            chunks: list[str] = []
            remaining = MAX_DETAIL_CHARS + 1
            for chunk in json.JSONEncoder(indent=2, ensure_ascii=False).iterencode(value):
                chunks.append(chunk[:remaining])
                remaining -= len(chunks[-1])
                if remaining == 0:
                    break
            text = "".join(chunks)
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


def command_summary(args: Any) -> str:
    """One title line: a shell command verbatim, else compact JSON arguments."""
    if isinstance(args, dict) and isinstance(args.get("command"), str):
        command = args["command"].strip()
        if not command:
            return ""
        text = "$ " + command[:MAX_SUMMARY_CHARS + 1]
    else:
        try:
            chunks: list[str] = []
            remaining = MAX_SUMMARY_CHARS + 1
            for chunk in json.JSONEncoder(ensure_ascii=False).iterencode(args):
                chunks.append(chunk[:remaining])
                remaining -= len(chunks[-1])
                if remaining == 0:
                    break
        except (TypeError, ValueError, RecursionError):
            return ""
        text = "".join(chunks)
        if text in ("{}", "[]", "null", '""'):
            return ""
    lines = text.strip().splitlines() or [""]
    line = "".join(char if char.isprintable() else "�" for char in lines[0])
    if len(lines) > 1 or len(line) > MAX_SUMMARY_CHARS:
        line = line[:MAX_SUMMARY_CHARS - 1] + "…"
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
    pruned: bool = False
    summary: str = ""
