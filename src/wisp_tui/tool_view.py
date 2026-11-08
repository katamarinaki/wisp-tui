"""Literal, bounded tool entries. Display never executes a tool."""

from __future__ import annotations

import json
import math
from typing import Any

from .chat_view import SelectableStatic
from .scrollback import render_item
from .tool_data import command_summary, detail_text


class ToolCallView(SelectableStatic):
    """One tool entry; missing fields remain distinct from empty/false values."""

    def __init__(self, event: dict[str, Any], *, completed: bool, historical: bool = False) -> None:
        super().__init__(markup=False)
        self.tool_name = "?"
        self.summary = ""
        self.title = ""
        self.status_text = ""
        self.tail_text = ""
        call_id = event.get("toolCallId")
        self.call_id = call_id if isinstance(call_id, str) and 0 < len(call_id) <= 200 else ""
        self.session_id: str | None = None
        self.input_text: str | None = None
        self.output_text: str | None = None
        self.result_notice = ""
        # A stored-result read has finished (matched or not) for this entry.
        self.result_checked = False
        self.result_retried = False
        self.completed = completed
        self.historical = historical
        self.is_run_summary = False
        self.unreported = False
        self._ok: bool | None = None
        self._duration_ms: float | None = None
        self.update_event(event, completed=completed)

    @property
    def settled(self) -> bool:
        """Final: nothing further will change it, so it can go to scrollback."""
        if not (self.completed or self.historical or self.unreported):
            return False
        return (
            self.output_text is not None
            or self.result_checked
            or self.historical
            or self.is_run_summary
            or not self.call_id
        )

    def entry(self) -> tuple[str, str]:
        state = "-ok" if self._ok is True else "-failed" if self._ok is False else ""
        lines = (self.title, self.tail_text or self.result_notice, self.status_text)
        return f"tool-call{state}", "\n".join(line for line in lines if line)

    def update_event(self, event: dict[str, Any], *, completed: bool) -> None:
        name = event.get("name")
        if isinstance(name, str) and name:
            label = name if len(name) <= 200 else name[:199] + "…"
            self.tool_name = "".join(char if char.isprintable() else "�" for char in label)
        self.completed = self.completed or completed
        if "args" in event:
            self.input_text = detail_text(event["args"])
            self.summary = command_summary(event["args"]) or self.summary
        ok = event.get("ok")
        if isinstance(ok, bool):
            self._ok = ok
        duration = event.get("durationMs")
        if (
            isinstance(duration, (int, float))
            and not isinstance(duration, bool)
            and 0 <= duration <= 1e15
            and math.isfinite(duration)
        ):
            self._duration_ms = float(duration)
        self.refresh_entry()

    def refresh_entry(self) -> None:
        """Re-render status, title, colors and output tail from the current fields."""
        status = "running…"
        if self.completed:
            status = "ok" if self._ok is True else "failed" if self._ok is False else "finished"
        elif self.historical:
            status = "recorded"
        elif self.unreported:
            status = "not reported"
        parts = [self.tool_name, status]
        if self._duration_ms is not None:
            parts.append(f"{self._duration_ms / 1000:.1f}s")
        if self.is_run_summary:
            parts.append("run summary")
        self.status_text = " · ".join(parts)
        # The summary may predate the final name (history vs. live events).
        summary = self.summary.removeprefix(json.dumps(self.tool_name, ensure_ascii=False))
        self.title = (
            summary if summary.startswith("$ ") else f"{self.tool_name} {summary.strip()}".rstrip()
        )
        if self.output_text is not None:
            # Bounded by detail_text; render_item shows only its last rows.
            self.tail_text = self.output_text.rstrip("\n")
        self.update(render_item(*self.entry()))

    def apply_history(
        self,
        input_text: str | None = None,
        output_text: str | None = None,
        status: str = "",
        summary: str = "",
    ) -> None:
        if self.input_text is None and input_text is not None:
            self.input_text = input_text
        self.summary = self.summary or summary
        if output_text is not None:
            self.output_text = output_text
            self.completed = True
            if self._ok is None and status in ("success", "error"):
                self._ok = status == "success"
        self.refresh_entry()

    def mark_unreported(self) -> None:
        """The run ended without a completion event for this entry."""
        self.unreported = True
        self.refresh_entry()

    def set_result_notice(self, notice: str) -> None:
        self.result_notice = notice
        self.refresh_entry()
