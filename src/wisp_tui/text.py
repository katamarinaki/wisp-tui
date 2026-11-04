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


def safe_text(value: Any, limit: int = 4000) -> str:
    if not isinstance(value, str):
        return ""
    return terminal_text(value if len(value) <= limit else value[: limit - 1] + "…")


def metadata_text(value: Any, limit: int = 100) -> str:
    """Bound metadata and keep control characters from creating fake UI lines."""
    text = safe_text(value, limit)
    return "".join(char if char.isprintable() else "�" for char in text)


def pretty_args(args: Any, limit: int = 4000) -> str:
    try:
        rendered = json.dumps(args, indent=2, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        rendered = repr(args)
    # Also hide bidi overrides and zero-width characters: the owner reads these to approve.
    return "".join(
        char if char.isprintable() or char in "\n\t" else "�" for char in safe_text(rendered, limit)
    )


def title_from(draft: str, limit: int = 60) -> str:
    """Session title from the first non-empty line of the first prompt."""
    line = next((line.strip() for line in draft.splitlines() if line.strip()), "")
    return line if len(line) <= limit else line[: limit - 1] + "…"


def display_path(path: Path) -> str:
    home = str(Path.home())
    text = str(path)
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


def model_label(session: dict[str, Any]) -> str:
    selection = session.get("effectiveSelection")
    if isinstance(selection, dict):
        if selection.get("mode") == "model":
            return f"model: {metadata_text(selection.get('model')) or 'unavailable'}"
        mode = metadata_text(selection.get("mode"))
        if mode:
            return f"mode: {mode} (runtime-selected model)"
    return "model: unavailable (runtime default)"
