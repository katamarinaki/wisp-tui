"""Parse Wisp session history (LangGraph checkpoints) into chat messages."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class HistoryError(Exception):
    """Raised when the history payload has an unexpected shape."""


@dataclass(frozen=True)
class ChatMessage:
    role: str  # "user" | "assistant" | "system" | "tool"
    text: str
    # LangGraph message identity, when supplied; never inferred from the text.
    message_id: str = ""


_ROLE_MAP = {"human": "user", "ai": "assistant", "system": "system", "tool": "tool"}


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    parts.append(block["text"])
                elif block.get("type") == "image_url":
                    parts.append("[image]")
                elif block.get("type") == "audio":
                    parts.append("[audio]")
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(p for p in parts if p)
    return ""


def _parse_message(message: Any) -> ChatMessage | None:
    if not isinstance(message, dict):
        return None
    kind = message.get("type")
    role = _ROLE_MAP.get(kind) if isinstance(kind, str) else None
    if role is None:
        return None
    text = _content_text(message.get("content"))
    if not text.strip():
        return None
    message_id = message.get("id")
    return ChatMessage(role=role, text=text,
                       message_id=message_id if isinstance(message_id, str) else "")


def checkpoint_covers_answer(before: list[ChatMessage], after: list[ChatMessage],
                             answer: str) -> bool:
    """Whether a *progressed* checkpoint visibly contains the finished answer.

    Equal text alone is not run identity. An unchanged checkpoint might contain
    a previous, legitimately identical reply while the latest turn isn't saved
    yet. Accept an appended answer or an updated, stable-ID trailing assistant;
    ambiguous/rewritten/compacted histories retain the separate run output.
    """
    if not answer or not after or after[-1].role != "assistant" or after[-1].text != answer:
        return False
    if len(after) > len(before) and after[:len(before)] == before:
        return True
    if not before or len(after) != len(before) or after[:-1] != before[:-1]:
        return False
    previous, current = before[-1], after[-1]
    return (previous.role == "assistant" and bool(previous.message_id)
            and previous.message_id == current.message_id and previous.text != current.text)


def parse_history(payload: Any) -> list[ChatMessage]:
    """Return the messages of the newest non-empty checkpoint.

    The history endpoint returns LangGraph checkpoints oldest-first; the last
    checkpoint's ``values.messages`` holds the full conversation state.
    """
    if not isinstance(payload, list):
        raise HistoryError("unexpected_history_shape")
    for checkpoint in reversed(payload):
        if not isinstance(checkpoint, dict):
            continue
        values = checkpoint.get("values")
        if not isinstance(values, dict):
            continue
        messages = values.get("messages")
        if not isinstance(messages, list) or not messages:
            continue
        parsed = [m for m in (_parse_message(item) for item in messages) if m is not None]
        return parsed
    return []
