"""Runtime text made safe and compact for the terminal.

Everything the runtime or a model sends passes through these helpers before it
reaches a widget: control characters are neutralized, lengths are bounded, and
metadata cannot fake extra UI lines.
"""

from __future__ import annotations

import json
import unicodedata
from pathlib import Path
from typing import Any


def terminal_text(text: str) -> str:
    """Replace C0/DEL/C1 controls (ESC, CSI, OSC…) so runtime text cannot drive the terminal."""
    # CR is dropped (as Textual did) so CRLF text and chunked appends stay clean.
    return "".join(
        "�" if unicodedata.category(char) == "Cc" and char not in "\n\t" else char
        for char in text.replace("\r", "")
    )


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def printable(text: str, keep: str = "") -> str:
    """Also hides bidi overrides and zero-width characters, not just controls."""
    return "".join(char if char.isprintable() or char in keep else "�" for char in text)


def safe_text(value: Any, limit: int = 4000) -> str:
    if not isinstance(value, str):
        return ""
    return terminal_text(clip(value, limit))


def metadata_text(value: Any, limit: int = 100) -> str:
    """Bound metadata and keep control characters from creating fake UI lines."""
    return printable(safe_text(value, limit))


def pretty_args(args: Any, limit: int = 4000) -> str:
    try:
        rendered = json.dumps(args, indent=2, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        rendered = repr(args)
    # The owner reads these to approve.
    return printable(safe_text(rendered, limit), "\n\t")


def title_from(draft: str, limit: int = 60) -> str:
    """Session title from the first non-empty line of the first prompt."""
    return clip(next((line.strip() for line in draft.splitlines() if line.strip()), ""), limit)


def display_path(path: Path) -> str:
    home = str(Path.home())
    text = str(path)
    if text == home:
        return "~"
    return "~" + text[len(home) :] if text.startswith(home + "/") else text


def usage_line(usage: Any) -> str:
    if not isinstance(usage, dict):
        return ""
    parts = []
    for key in ("inputTokens", "outputTokens", "totalTokens"):
        value = usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            parts.append(f"{key.replace('Tokens', '')}: {value:,}")
    return ", ".join(parts)


def compact_count(count: int) -> str:
    if count < 1000:
        return str(count)
    for limit, suffix in ((1_000_000, "k"), (1_000_000_000, "M")):
        if count < limit:
            scaled = count / (limit // 1000)
            return f"{scaled:.1f}{suffix}" if scaled < 9.95 else f"{scaled:.0f}{suffix}"
    return f"{count / 1_000_000_000:.0f}G"


def compact_usage(line: str) -> str:
    """``usage_line`` as the footer shows it: ``↑56k ↓1.8k Σ58k``."""
    marks = {"input": "↑", "output": "↓", "total": "Σ"}
    parts = (part.split(": ") for part in line.split(", ") if ": " in part)
    return " ".join(
        f"{marks.get(key, key)}{compact_count(int(value.replace(',', '')))}" for key, value in parts
    )


def model_label(selection: dict[str, Any]) -> str:
    if selection.get("mode") == "model":
        return f"model: {metadata_text(selection.get('model')) or 'unavailable'}"
    mode = metadata_text(selection.get("mode"))
    if mode:
        return f"mode: {mode} (runtime-selected model)"
    return "model: unavailable (runtime default)"
