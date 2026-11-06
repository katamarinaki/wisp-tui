"""Parse Wisp session history (LangGraph checkpoints) into chat messages."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .tool_data import ToolCallInfo, ToolHistoryInfo, command_summary, detail_text, output_text


class HistoryError(Exception):
    """Raised when the history payload has an unexpected shape."""


@dataclass(frozen=True)
class ChatMessage:
    role: str  # "user" | "assistant" | "system" | "tool"
    text: str
    # LangGraph message identity, when supplied; never inferred from the text.
    message_id: str = ""
    tool_calls: tuple[ToolCallInfo, ...] = ()
    tool_name: str = ""
    tool_call_id: str = ""
    tool_status: str = ""


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


def _parse_tool_calls(raw_calls: list[Any]) -> list[ToolCallInfo]:
    calls: list[ToolCallInfo] = []
    for call in raw_calls[:60]:
        if not isinstance(call, dict) or not isinstance(call.get("name"), str):
            continue
        call_id = call.get("id")
        name = call["name"]
        calls.append(
            ToolCallInfo(
                name if len(name) <= 200 else name[:199] + "…",
                call_id if isinstance(call_id, str) and 0 < len(call_id) <= 200 else "",
                detail_text(call["args"]) if "args" in call else None,
                command_summary(call["args"]) if "args" in call else "",
            )
        )
    return calls


def _parse_message(message: Any) -> ChatMessage | None:
    if not isinstance(message, dict):
        return None
    kind = message.get("type")
    role = _ROLE_MAP.get(kind) if isinstance(kind, str) else None
    if role is None:
        return None
    content = message.get("content")
    if role == "tool" and not isinstance(content, (str, list)):
        return None
    # Stored tool content may be structured/media blocks, not just text.
    if role == "tool" and isinstance(content, list):
        text = detail_text(content)
    else:
        text = _content_text(content)
    raw_calls = message.get("tool_calls")
    calls = (
        _parse_tool_calls(raw_calls) if role == "assistant" and isinstance(raw_calls, list) else []
    )
    if not text.strip() and not calls and role != "tool":
        return None
    message_id = message.get("id")
    tool_name = message.get("name")
    tool_call_id = message.get("tool_call_id")
    status = message.get("status")
    return ChatMessage(
        role=role,
        text=text,
        message_id=message_id if isinstance(message_id, str) else "",
        tool_calls=tuple(calls),
        tool_name=tool_name if role == "tool" and isinstance(tool_name, str) else "",
        tool_call_id=tool_call_id
        if role == "tool" and isinstance(tool_call_id, str) and 0 < len(tool_call_id) <= 200
        else "",
        tool_status=status if role == "tool" and status in ("success", "error") else "",
    )


def tool_history_results(
    messages: list[ChatMessage], call_ids: set[str] | None = None
) -> tuple[dict[str, ToolHistoryInfo], set[str]]:
    """Join only unique explicit IDs; nested/reused IDs must not guess a result."""
    calls: dict[str, ToolCallInfo] = {}
    results: dict[str, ChatMessage] = {}
    ambiguous: set[str] = set()
    for message in messages:
        for call in message.tool_calls:
            if call.call_id and (call_ids is None or call.call_id in call_ids):
                if call.call_id in calls:
                    ambiguous.add(call.call_id)
                calls[call.call_id] = call
        if (
            message.role == "tool"
            and message.tool_call_id
            and (call_ids is None or message.tool_call_id in call_ids)
        ):
            if message.tool_call_id in results:
                ambiguous.add(message.tool_call_id)
            results[message.tool_call_id] = message
    matched = {
        call_id: ToolHistoryInfo(
            calls[call_id].input_text if call_id in calls else None,
            output_text(message.text),
            message.tool_status,
            calls[call_id].summary if call_id in calls else "",
        )
        for call_id, message in results.items()
        if call_id not in ambiguous
    }
    return matched, ambiguous


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
        return [m for m in (_parse_message(item) for item in messages) if m is not None]
    return []
