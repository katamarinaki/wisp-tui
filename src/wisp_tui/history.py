"""Parse Wisp session history (LangGraph checkpoints) into chat messages."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .client import ClientError
from .text import clip
from .tool_data import MAX_NAME_CHARS, call_id_of, command_summary, detail_text, output_text


@dataclass(frozen=True)
class ToolCallInfo:
    name: str
    call_id: str = ""
    summary: str = ""


@dataclass(frozen=True)
class ToolHistoryInfo:
    output_text: str
    status: str = ""
    summary: str = ""


@dataclass(frozen=True)
class ChatMessage:
    role: str  # "user" | "assistant" | "system" | "tool"
    # A tool message's text is its output, formatted for display.
    text: str
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
        calls.append(
            ToolCallInfo(
                clip(call["name"], MAX_NAME_CHARS),
                call_id_of(call.get("id")),
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
    if role != "tool":
        return ChatMessage(role=role, text=text, tool_calls=tuple(calls))
    tool_name = message.get("name")
    status = message.get("status")
    return ChatMessage(
        role=role,
        text=output_text(text),
        tool_name=tool_name if isinstance(tool_name, str) else "",
        tool_call_id=call_id_of(message.get("tool_call_id")),
        tool_status=status if status in ("success", "error") else "",
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
            message.text,
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
        raise ClientError("unexpected_history_shape")
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
