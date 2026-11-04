"""Literal, bounded, collapsible tool details. Display never executes a tool."""

from __future__ import annotations

import math
from typing import Any

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.content import Content
from textual.widgets import Collapsible, Static

from .tool_data import command_summary, detail_text

TAIL_LINES = 5


class ToolCallView(Collapsible):
    """One tool entry; missing fields remain distinct from empty/false values."""

    def __init__(self, event: dict[str, Any], *, completed: bool, historical: bool = False) -> None:
        self.tool_name = "?"
        self.summary = ""
        self.status_text = ""
        call_id = event.get("toolCallId")
        self.call_id = call_id if isinstance(call_id, str) and 0 < len(call_id) <= 200 else ""
        self.session_id: str | None = None
        self.input_text: str | None = None
        self.output_text: str | None = None
        self.input_label = "Input (runtime, sanitized)"
        self.output_label = "Output (stored history)"
        self.history_status = ""
        self.result_notice = ""
        self.completed = completed
        self.historical = historical
        self.is_run_summary = False
        self.unreported = False
        self._ok: bool | None = None
        self._duration_ms: float | None = None
        self._details = Static("", markup=False, classes="tool-details")
        self._tail = Static("", markup=False, classes="tool-tail")
        self._stats = Static("", markup=False, classes="tool-stats")
        super().__init__(
            VerticalScroll(self._details, classes="tool-detail-scroll"),
            title="tool",
            collapsed=True,
            classes="tool-call",
        )
        self.update_event(event, completed=completed)

    def compose(self) -> ComposeResult:
        yield from super().compose()
        yield self._tail
        yield self._stats

    def _watch_title(self, title: str) -> None:
        # CollapsibleTitle otherwise parses string labels as Textual markup.
        self._title.label = Content(title)

    def update_event(self, event: dict[str, Any], *, completed: bool) -> None:
        name = event.get("name")
        if isinstance(name, str) and name:
            label = name if len(name) <= 200 else name[:199] + "…"
            self.tool_name = "".join(char if char.isprintable() else "�" for char in label)
        self.completed = self.completed or completed
        if "args" in event:
            self.input_text = detail_text(event["args"])
            self.input_label = "Input (runtime, sanitized)"
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
        """Re-render status, title, colors and details from the current fields."""
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
        self._stats.update(self.status_text)
        self.title = (
            self.summary
            if self.summary.startswith("$ ")
            else f"{self.tool_name} {self.summary}".rstrip()
        )
        self.set_class(self._ok is True, "-ok")
        self.set_class(self._ok is False, "-failed")
        self._render_details()

    def apply_history(
        self,
        input_text: str | None = None,
        output_text: str | None = None,
        status: str = "",
        pruned: bool = False,
        summary: str = "",
    ) -> None:
        if self.input_text is None and input_text is not None:
            self.input_text = input_text
            self.input_label = "Input (history/model request)"
        self.summary = self.summary or summary
        if output_text is not None:
            self.output_text = output_text
            self.output_label = "Output (stored summary)" if pruned else "Output (stored history)"
            self.completed = True
            self.history_status = status if status in ("success", "error") else ""
            if self._ok is None and status in ("success", "error"):
                self._ok = status == "success"
        self.refresh_entry()

    def mark_unreported(self) -> None:
        """The run ended without a completion event for this entry."""
        self.unreported = True
        self.refresh_entry()

    def set_result_notice(self, notice: str) -> None:
        self.result_notice = notice
        self._render_details()

    def _render_details(self) -> None:
        if self.output_text is not None:
            lines = self.output_text.rstrip("\n").split("\n")
            tail = lines[-TAIL_LINES:]
            if len(lines) > TAIL_LINES:
                tail.insert(0, f"… ({len(lines) - TAIL_LINES} earlier lines, expand for all)")
            self._tail.update("\n".join(tail))
        self.set_class(self.output_text is not None, "-has-tail")
        if self.output_text is not None:
            output = self.output_text
        elif self.result_notice:
            output = self.result_notice
        elif self.historical:
            output = "No result stored in this checkpoint."
        elif self.call_id:
            output = "Expand to load the stored result."
        else:
            output = (
                "No tool-call ID; cannot match a stored result. "
                "Reopen the session to inspect history."
            )
        history_status = f"\nHistory status: {self.history_status}" if self.history_status else ""
        if self.input_text is not None:
            input_part = f"{self.input_label}\n{self.input_text}"
        elif self.historical:
            input_part = "Input\nNot in loaded history."
        else:
            input_part = f"{self.input_label}\nNot supplied by runtime."
        self._details.update(f"{input_part}\n\n{self.output_label}{history_status}\n{output}")
