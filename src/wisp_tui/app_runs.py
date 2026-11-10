"""Following a run: its event stream, approval prompts, completion and cancellation."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from typing import Any

from .app_composer import ComposerLayer
from .client import safe_code, session_path
from .runs import TERMINAL_EVENTS, TERMINAL_STATUSES, Confirmation, RunFollower, confirmations
from .screens import DECISIONS, RULE_DECISIONS, ApprovalModal
from .terminal_status import TabStatus
from .text import safe_text, usage_line
from .tool_view import ToolCallView

CANCEL_REQUESTED = "cancellation requested — awaiting runtime confirmation"


class RunLayer(ComposerLayer):
    def _begin_run(self, run_id: str, *, reattached: bool = False) -> None:
        """Follow ``run_id``; a reattached run replays below the history shown."""
        self._end_answer()
        self.active_run = run_id
        self._tool_calls.clear()
        self._saw_live_tool_events = False
        self._tool_history_post_run_reads = 0
        self._replay_note_due = reattached
        self._cancel_requested = False
        self._queue_sync()
        self._disconnected = False
        self._set_status("")

    def _start_follow(self) -> None:
        run_id = self.active_run
        session_id = self.session_id
        if run_id is None or session_id is None:
            self._show_line("No active run to reconnect.")
            return
        if self._follower is not None and not self._follower.stopped.is_set():
            if self._follower.run_id == run_id:
                self._follower.wake.set()
                return
            self._follower.stop()
        follower = RunFollower(
            self.client,
            run_id,
            session_id,
            last_seq=self.last_seq.get(run_id),
            on_event=lambda event: self.call_from_thread(self._follow_event, follower, event),
            on_snapshot=lambda record: self.call_from_thread(
                self._follow_snapshot, follower, record
            ),
            on_error=lambda code: self.call_from_thread(self._follow_error, follower, code),
        )
        self._follower = follower
        self._run_threaded(follower.run, group="turn", on_done=lambda: self._follow_done(follower))

    def _follow_done(self, follower: RunFollower) -> None:
        if self._follower is follower:
            self._follower = None

    def _follow_event(self, follower: RunFollower, event: dict[str, Any]) -> None:
        if self._follower is not follower or self.active_run != follower.run_id:
            return
        self.last_seq[follower.run_id] = event["seq"]
        self._disconnected = False
        self._handle_event(event)

    def _follow_snapshot(self, follower: RunFollower, record: dict[str, Any]) -> None:
        if self._follower is not follower or self.active_run != follower.run_id:
            return
        self._disconnected = False
        status = record["status"]
        if status in TERMINAL_STATUSES:
            self._finalize_run(f"run.{status}", record)
            return
        if status == "parked":
            self._set_status("run parked — waiting for approval", TabStatus.WAITING)
        else:
            self._set_status("")
        self._sync_confirmations(confirmations(record))
        if self._cancel_requested:
            self._set_status(CANCEL_REQUESTED)

    def _follow_error(self, follower: RunFollower, code: str) -> None:
        if self._follower is follower and self.active_run == follower.run_id:
            self._stream_failed(code)

    def _stream_failed(self, code: str) -> None:
        if self.active_run is None:
            return
        if not self._disconnected:
            self._show_line(
                "Connection lost; the task may still be running. "
                "Reconnecting automatically; /reconnect retries if needed.",
                "error",
            )
        self._disconnected = True
        self._set_status(f"disconnected: {code} · /reconnect")
        self._sync_confirmations([])

    def _handle_event(self, event: dict[str, Any]) -> None:
        kind = event.get("kind")
        if kind == "text.delta":
            self._append_delta(str(event.get("text", "")))
        elif kind == "gap":
            text = event.get("text")
            self._show_stream_text(self._replace_answer(text if isinstance(text, str) else ""))
            if self._stream_view is not None:
                self._stream_view.flush()
        elif kind == "tool_call.started":
            self._show_tool_call(event, completed=False)
        elif kind == "tool_call":
            ok = event.get("ok") is True
            duration = event.get("durationMs")
            self._show_tool_call(event, completed=True)
            self._record_locally(
                {
                    "type": "tool",
                    "runId": self.active_run,
                    "name": safe_text(str(event.get("name", "?")), 200),
                    "ok": ok,
                    "durationMs": duration if isinstance(duration, (int, float)) else None,
                }
            )
        elif kind == "confirmation.resolved":
            prompt_id = event.get("id")
            if isinstance(prompt_id, str):
                self._approvals_answered.add(prompt_id)
            self._show_line("approval resolved", "tool")
        elif kind == "audio.transcribed":
            self._show_line(f"transcribed: {safe_text(event.get('text'), 120)}", "tool")
        elif kind == "run.parked":
            self._set_status("run parked — checking current approvals", TabStatus.WAITING)
        elif kind in ("run.queued", "run.started", "run.resumed"):
            self._set_status("")
        elif kind in TERMINAL_EVENTS:
            self._finalize_run(kind, event.get("outcome"))

    def _append_delta(self, text: str) -> None:
        self._answer_text += text
        self._show_stream_text(self._answer_text[self._answer_segment_start :])

    def _replace_answer(self, text: str) -> str:
        """Make ``text`` the whole answer; return the part the current segment shows."""
        earlier = self._answer_text[: self._answer_segment_start]
        self._answer_text = text
        if not text.startswith(earlier):
            # Server text replaced what is shown above; show all of it.
            self._answer_segment_start = 0
        return text[self._answer_segment_start :]

    def _sync_confirmations(self, prompts: list[Confirmation]) -> None:
        self.queued_approvals = deque(
            prompt
            for prompt in prompts
            if prompt.prompt_id not in self._approvals_sending | self._approvals_answered
        )
        ids = {queued.prompt_id for queued in self.queued_approvals}
        if self._approval_prompt_id not in ids:
            self._dismiss_approval()
        self.call_after_refresh(self._open_next_approval)

    def _dismiss_approval(self) -> None:
        modal = self._approval_modal
        self._approval_modal = None
        self._approval_prompt_id = None
        if modal is not None:
            # None is a programmatic dismissal, never an implicit denial.
            modal.dismiss(None)

    def _open_next_approval(self) -> None:
        if self._approval_modal is not None or not self.queued_approvals:
            return
        run_id, prompt_id, tool, args, note = self.queued_approvals[0]
        if self.active_run != run_id:
            return
        modal = ApprovalModal(tool, args, note, allow_rules=not self._rule_decisions_refused)
        self._approval_modal = modal
        self._approval_prompt_id = prompt_id
        self.push_screen(
            modal, lambda decision: self._approval_answered(run_id, prompt_id, decision)
        )
        self._render_activity()

    def _approval_answered(self, run_id: str, prompt_id: str, decision: str | None) -> None:
        if decision is None or self.active_run != run_id:
            return
        if self._approval_prompt_id != prompt_id:
            return
        modal = self._approval_modal
        self._approval_modal = None
        self._approval_prompt_id = None
        if decision not in DECISIONS:
            return
        if prompt_id in self._approvals_sending | self._approvals_answered:
            return
        tool, args_text = (modal.tool_name, modal.args_text) if modal is not None else ("?", "")
        entry = {
            "type": "approval",
            "runId": run_id,
            "promptId": prompt_id,
            "tool": safe_text(tool, 200),
            "decision": decision,
        }
        # Arguments often carry commands, paths or file contents: opt-in only.
        if self.config.log_approval_args:
            entry["args"] = args_text
        self._record_locally(entry)
        self.queued_approvals = deque(
            queued for queued in self.queued_approvals if queued.prompt_id != prompt_id
        )
        self._approvals_sending.add(prompt_id)
        self._set_status(f"approval {decision} · sending…")

        def work() -> Callable[[], None]:
            self.client.request(
                "POST",
                f"/v1/runs/{run_id}/confirmations/{prompt_id}",
                {"decision": decision, "wait": False},
            )
            return lambda: self._confirmation_sent(run_id, prompt_id, decision, "")

        self._run_threaded(
            work,
            group="confirm",
            on_error=lambda code: self._confirmation_sent(run_id, prompt_id, decision, code),
        )
        self.call_after_refresh(self._open_next_approval)

    def _confirmation_sent(self, run_id: str, prompt_id: str, decision: str, code: str) -> None:
        if self.active_run != run_id:
            return
        self._approvals_sending.discard(prompt_id)
        if not code:
            self._approvals_answered.add(prompt_id)
        else:
            self._record_locally(
                {"type": "approval_result", "runId": run_id, "promptId": prompt_id, "error": code}
            )
            self._show_approval_error(code, decision)
        # Re-read current prompts even after a failed/ambiguous POST. Never
        # automatically submit the decision again, and never start a second SSE.
        self._start_follow()

    def _show_approval_error(self, code: str, decision: str) -> None:
        if code.startswith("runtime_http_404"):
            self._show_line("Approval already settled or unavailable; checking run status.", "tool")
        elif code.startswith("runtime_http_403") and decision in RULE_DECISIONS:
            self._rule_decisions_refused = True
            # A dialog opened meanwhile still offers rules; reopen it without them.
            self._dismiss_approval()
            self._show_line(
                "The runtime token may not create project/global rules "
                f"({code}). Answer with approve or deny.",
                "error",
            )
        else:
            self._show_line(f"approval error: {code}", "error")

    def _finalize_run(self, kind: str, outcome: Any) -> None:
        answer = ""
        data = outcome if isinstance(outcome, dict) else {}
        if not self._saw_live_tool_events:
            self._show_summarized_tool_calls(data.get("toolCalls"))
        self._last_run_tokens = usage_line(data.get("usage"))
        self._render_metadata()
        if kind == "run.completed":
            output = data.get("output")
            text = output.get("text") if isinstance(output, dict) else None
            if isinstance(text, str):
                # Text before the last tool call is already on screen above it.
                shown = self._replace_answer(text)
                if shown or self._stream_view is not None:
                    self._show_stream_text(shown)
            answer = self._answer_text
            if answer:
                self._record_locally(
                    {
                        "type": "message",
                        "role": "assistant",
                        "text": answer,
                        "runId": self.active_run,
                    }
                )
            # End on Wisp's answer, without a completion announcement.
            self._set_status("")
        elif kind == "run.failed":
            failure = data.get("failure")
            if not isinstance(failure, dict):
                failure = {}
            reason = safe_code(failure.get("reason"), "unknown")
            self._show_line(f"run failed: {reason}", "error")
            self._record_locally(
                {"type": "run", "runId": self.active_run, "status": "failed", "reason": reason}
            )
            self._set_status("run failed", TabStatus.ERROR)
        else:
            self._show_line("run cancelled", "tool")
            self._record_locally({"type": "run", "runId": self.active_run, "status": "cancelled"})
            self._set_status("run cancelled", TabStatus.CANCELLED)
        self._finish_run()
        if self._tool_targets():
            self._request_tool_history(force_after_current=True)

    def _show_summarized_tool_calls(self, tool_calls: Any) -> None:
        """Show the outcome's tool calls when no live tool events arrived."""
        if not isinstance(tool_calls, list):
            return
        for call in tool_calls[-self.MAX_CHAT_ITEMS :]:
            if not isinstance(call, dict) or not isinstance(call.get("name"), str):
                continue
            # 0.1.8 strips call IDs and results from these aggregate records.
            # They are a fallback, never another copy of observed live calls.
            details = {
                key: call[key] for key in ("name", "args", "ok", "durationMs") if key in call
            }
            self._show_tool_call(details, completed=True, source="summary")

    def _finish_run(self) -> None:
        self._end_answer()
        for item in self._chat_items:
            if isinstance(item, ToolCallView) and not item.completed and item.source == "live":
                item.mark_unreported()
        self._queue_sync()
        if self._follower is not None:
            self._follower.stop()
        self._dismiss_approval()
        self._approvals_sending.clear()
        self._approvals_answered.clear()
        self._disconnected = False
        self._cancel_requested = False
        if self.active_run:
            self.last_seq.pop(self.active_run, None)
        self.active_run = None
        self._replay_note_due = False
        self.queued_approvals.clear()
        self._render_activity()

    def _cancel_run(self) -> None:
        if self._pending_submission is not None:
            self._cancel_when_acknowledged = True
            self._set_status("will cancel after submission is acknowledged · /retry if needed")
            return
        if self.session_id is None or self.active_run is None:
            self._show_line("No known active run to cancel.")
            return
        if self._cancel_requested:
            self._show_line("Cancellation already requested; awaiting runtime confirmation.")
            return
        self._cancel_requested = True
        path = session_path(self.session_id, "/cancel", self._project_root)
        run_id = self.active_run

        def work() -> Callable[[], None]:
            self.client.request("POST", path, {}, timeout=5.0)
            return lambda: self._cancel_sent(run_id, "")

        self._run_threaded(
            work, group="cancel", on_error=lambda code: self._cancel_sent(run_id, code)
        )

    def _cancel_sent(self, run_id: str, code: str) -> None:
        if self.active_run != run_id:
            return
        if code:
            self._cancel_requested = False
            self._set_status(f"cancel error: {code} · /cancel to retry")
        else:
            self._set_status(CANCEL_REQUESTED)
            self._start_follow()
