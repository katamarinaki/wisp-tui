"""Following a run: its event stream, approval prompts, completion and cancellation."""

from __future__ import annotations

from collections import deque
from typing import Any

from .app_base import ApprovalPrompt
from .app_composer import ComposerLayer
from .client import TERMINAL_RUN_STATUSES, ClientError, safe_code
from .history import ChatMessage
from .runs import RunFollower
from .screens import ApprovalModal
from .text import safe_text, usage_line
from .tool_view import ToolCallView

RUN_PROGRESS_STATUS = {
    "run.queued": "",
    "run.started": "",
    "run.resumed": "",
    "run.parked": "run parked — checking current approvals",
}


class RunLayer(ComposerLayer):
    def _begin_run(self, run_id: str, history: list[ChatMessage] | None = None) -> None:
        if self._stream_view is not None:
            self._stream_view.flush()
        self.active_run = run_id
        self._tool_calls.clear()
        self._saw_live_tool_events = False
        self._reattached_checkpoint = list(history) if history is not None else None
        self._cancel_requested = False
        self._answer_text = ""
        self._stream_view = None
        self._queue_sync()
        self._disconnected = False
        self._answer_segment_start = 0
        self._replay_label_shown = False
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
            event_timeout=self.config.event_timeout,
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
        if status in TERMINAL_RUN_STATUSES:
            self._finalize_run(f"run.{status}", record)
            return
        if status == "parked":
            self._set_status("run parked — waiting for approval")
            self._sync_confirmations(record.get("confirmations", []))
        else:
            self._sync_confirmations([])
            self._set_status("")
        if self._cancel_requested:
            self._set_status("cancellation requested — awaiting runtime confirmation")

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
            earlier = self._answer_text[: self._answer_segment_start]
            self._answer_text = text if isinstance(text, str) else ""
            if not self._answer_text.startswith(earlier):
                # Server text replaced what is shown above; show all of it.
                self._answer_segment_start = 0
            self._show_stream_text(self._answer_text[self._answer_segment_start :])
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
        elif kind in RUN_PROGRESS_STATUS:
            self._set_status(RUN_PROGRESS_STATUS[kind])
        elif kind in ("run.completed", "run.failed", "run.cancelled"):
            self._finalize_run(kind, event.get("outcome"))

    def _append_delta(self, text: str) -> None:
        self._answer_text += text
        self._show_stream_text(self._answer_text[self._answer_segment_start :])

    def _sync_confirmations(self, prompts: list[dict[str, Any]]) -> None:
        run_id = self.active_run or ""
        self.queued_approvals = deque(
            ApprovalPrompt(run_id, prompt["id"], prompt["toolName"], prompt.get("args"))
            for prompt in prompts
            if prompt["id"] not in self._approvals_sending | self._approvals_answered
        )
        self._approval_notes = {
            prompt["id"]: " · ".join(
                note
                for applies, note in (
                    (prompt.get("askedEveryTime") is True, "asked every time"),
                    (prompt.get("escalatedBy") == "judge", "escalated by judge"),
                )
                if applies
            )
            for prompt in prompts
        }
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
        run_id, prompt_id, tool, args = self.queued_approvals[0]
        if self.active_run != run_id:
            return
        modal = ApprovalModal(
            tool,
            args,
            self._approval_notes.get(prompt_id, ""),
            allow_rules=not self._rule_decisions_refused,
        )
        self._approval_modal = modal
        self._approval_prompt_id = prompt_id
        self.push_screen(
            modal, lambda decision: self._approval_answered(run_id, prompt_id, decision)
        )

    def _approval_answered(self, run_id: str, prompt_id: str, decision: str | None) -> None:
        if decision is None or self.active_run != run_id:
            return
        if self._approval_prompt_id != prompt_id:
            return
        modal = self._approval_modal
        self._approval_modal = None
        self._approval_prompt_id = None
        if decision not in ("approve", "deny", "allow_project", "allow_global"):
            return
        if prompt_id in self._approvals_sending | self._approvals_answered:
            return
        # Record exactly what the owner was shown when deciding.
        tool, args_text = (modal.tool_name, modal.args_text) if modal is not None else ("?", "")
        self._record_locally(
            {
                "type": "approval",
                "runId": run_id,
                "promptId": prompt_id,
                "tool": safe_text(tool, 200),
                "args": args_text,
                "decision": decision,
            }
        )
        self.queued_approvals = deque(
            queued for queued in self.queued_approvals if queued.prompt_id != prompt_id
        )
        self._approvals_sending.add(prompt_id)
        self._set_status(f"approval {decision} · sending…")

        def work() -> None:
            code = ""
            try:
                self.client.request(
                    "POST",
                    f"/v1/runs/{run_id}/confirmations/{prompt_id}",
                    {"decision": decision, "wait": False},
                )
            except ClientError as error:
                code = str(error)
            except Exception:
                code = "internal_error"
            self.call_from_thread(self._confirmation_sent, run_id, prompt_id, decision, code)

        self._run_threaded(work, group="confirm")
        self.call_after_refresh(self._open_next_approval)

    def _confirmation_sent(self, run_id: str, prompt_id: str, decision: str, code: str) -> None:
        if self.active_run != run_id:
            return
        self._approvals_sending.discard(prompt_id)
        if code:
            self._record_locally(
                {"type": "approval_result", "runId": run_id, "promptId": prompt_id, "error": code}
            )
        if not code:
            self._approvals_answered.add(prompt_id)
        elif code.startswith("runtime_http_404"):
            self._show_line("Approval already settled or unavailable; checking run status.", "tool")
        elif code.startswith("runtime_http_403") and decision in ("allow_project", "allow_global"):
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
        # Re-read current prompts even after a failed/ambiguous POST. Never
        # automatically submit the decision again, and never start a second SSE.
        self._start_follow()

    def _finalize_run(self, kind: str, outcome: Any) -> None:
        answer = ""
        data = outcome if isinstance(outcome, dict) else {}
        if not self._saw_live_tool_events:
            self._show_summarized_tool_calls(data.get("toolCalls"))
        self._last_run_tokens = usage_line(data.get("usage"))
        self._render_metadata()
        if kind == "run.completed":
            text = ""
            output = data.get("output")
            has_final_text = isinstance(output, dict) and isinstance(output.get("text"), str)
            if has_final_text:
                assert isinstance(output, dict)
                text = output["text"]
                shown = text
                # Text before the last tool call is already on screen above it.
                earlier = self._answer_text[: self._answer_segment_start]
                if earlier and shown.startswith(earlier):
                    shown = shown[len(earlier) :]
                if shown or self._stream_view is not None:
                    self._show_stream_text(shown)
            answer = text if has_final_text else self._answer_text
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
            self._set_status("run failed")
        else:
            self._show_line("run cancelled", "tool")
            self._record_locally({"type": "run", "runId": self.active_run, "status": "cancelled"})
            self._set_status("run cancelled")
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
            self._show_tool_call(details, completed=True, terminal=True)

    def _finish_run(self) -> None:
        if self._stream_view is not None:
            self._stream_view.flush()
        for item in self._chat_items:
            if isinstance(item, ToolCallView) and not item.completed and not item.historical:
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
        self._answer_text = ""
        self._answer_segment_start = 0
        self._stream_view = None
        self._reattached_checkpoint = None
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
        session_id = self.session_id
        run_id = self.active_run

        def work() -> None:
            code = ""
            try:
                self.client.request("POST", f"/v1/sessions/{session_id}/cancel", {}, timeout=5.0)
            except ClientError as error:
                code = str(error)
            except Exception:
                code = "internal_error"
            self.call_from_thread(self._cancel_sent, run_id, code)

        self._run_threaded(work, group="cancel")

    def _cancel_sent(self, run_id: str, code: str) -> None:
        if self.active_run != run_id:
            return
        if code:
            self._cancel_requested = False
            self._set_status(f"cancel error: {code} · /cancel to retry")
        else:
            self._set_status("cancellation requested — awaiting runtime confirmation")
            self._start_follow()
