"""Submitting prompts durably, and recovering a submission left unacknowledged."""

from __future__ import annotations

import uuid
from typing import Any

from .app_base import FRESH_STATUS
from .app_runs import RunLayer
from .client import ClientError, safe_code, session_path
from .composer import bounded_history
from .editor import PromptInput
from .history import ChatMessage, HistoryError, parse_history
from .runs import find_active_run
from .sessions import UUID_RE
from .submissions import PendingSubmission, SubmissionError
from .text import safe_text


class SubmissionLayer(RunLayer):
    def _restore_pending(self) -> None:
        was_error = self._pending_state_error
        try:
            pending = self.submissions.load()
        except SubmissionError as error:
            self._pending_state_error = True
            self._set_status(f"{error} · /retry to recheck")
            self._show_line(
                "Pending-request state is unavailable or in use. New submissions "
                "are blocked; /retry rechecks without starting fresh work.",
                "error",
            )
            return
        self._pending_state_error = False
        if pending is None:
            if was_error:
                self._set_status(FRESH_STATUS if self.session_id is None else "session ready")
            return
        self._pending_submission = pending
        self.session_id = pending.session_id
        self._project_root = pending.project_root
        self.session_title = "Recovered submission"
        self.title = "Wisp — recovered submission"
        editor = self.query_one("#input", PromptInput)
        if not editor.value:
            editor.value = pending.draft
            self._composer_draft = pending.draft
            self._composer_submission_key = pending.idempotency_key
            self._composer_dirty = True
            self._queue_composer_save()
        self._render_metadata()
        self._set_status("unacknowledged submission restored · /retry")
        self._show_line(
            "Recovered an unacknowledged request. Nothing was resubmitted. "
            "/retry explicitly reuses the original input, options and request ID. "
            "Session/model changes stay blocked until it is acknowledged."
        )
        self._load_pending_view(pending)

    def _load_pending_view(self, pending: PendingSubmission) -> None:
        """Inspect the original session without identifying/replaying its request."""
        token = object()
        self._recovery_view_token = token
        self._pending_view_history = None
        epoch = self._chat_epoch

        def work() -> None:
            try:
                session = self.client.request(
                    "GET", f"/v1/sessions/{pending.session_id}", timeout=5.0
                )
                if not isinstance(session, dict) or session.get("id") != pending.session_id:
                    raise ClientError("invalid_session")
                payload = self.client.request(
                    "GET", f"/v1/sessions/{pending.session_id}/history", timeout=5.0
                )
                messages = parse_history(payload)
                runs = self.client.request(
                    "GET",
                    f"/v1/runs?sessionId={pending.session_id}"
                    "&status=queued,running,parked&limit=200",
                    timeout=5.0,
                )
                active_id = find_active_run(runs, pending.session_id)
            except ClientError as error:
                code = str(error)
            except HistoryError:
                code = "unexpected_history_shape"
            except Exception:
                code = "internal_error"
            else:
                self.call_from_thread(
                    self._pending_view_loaded, token, pending, epoch, session, messages, active_id
                )
                return
            self.call_from_thread(self._pending_view_failed, token, pending, code)

        self._run_threaded(
            work, group="recovery", on_done=lambda: self._pending_view_done(token, pending)
        )

    def _pending_view_current(self, token: object, pending: PendingSubmission) -> bool:
        return (
            self._recovery_view_token is token
            and self._pending_submission is pending
            and self.session_id == pending.session_id
            and not self._submitting
            and self.active_run is None
        )

    def _pending_view_loaded(
        self,
        token: object,
        pending: PendingSubmission,
        epoch: int,
        session: dict[str, Any],
        messages: list[ChatMessage],
        active_id: str,
    ) -> None:
        if not self._pending_view_current(token, pending):
            return
        self.session_title = safe_text(session.get("title"), 80) or "(untitled)"
        self.title = f"Wisp — {self.session_title}"
        self._selection_updated(session, pending.session_id)
        # A newer view cancels history restoration, but metadata is still useful.
        if self._chat_epoch != epoch:
            return
        hidden = max(0, len(messages) - self.MAX_CHAT_ITEMS)
        self._reset_chat(hidden)
        for message in messages[hidden:]:
            self._add_message(message)
        self._pending_view_history = list(messages)
        self._show_line(
            "Recovered request remains unacknowledged. This view is read-only; "
            "only explicit /retry reuses the original request ID."
        )
        if active_id:
            self._show_line(
                f"Runtime reports active run {active_id[:8]}. "
                "It is not proof this request was accepted.",
                "tool",
            )
        else:
            self._show_line(
                "No active v1 run reported. This is not proof the request was rejected.", "tool"
            )
        self._announce_saved_draft()

    def _pending_view_failed(self, token: object, pending: PendingSubmission, code: str) -> None:
        if self._pending_view_current(token, pending):
            self._set_status(
                f"recovery view unavailable: {safe_code(code, 'history_unavailable')} · /retry"
            )

    def _pending_view_done(self, token: object, pending: PendingSubmission) -> None:
        if self._recovery_view_token is not token:
            return
        retry = self._retry_after_recovery and self._pending_view_current(token, pending)
        self._recovery_view_token = None
        self._retry_after_recovery = False
        if retry:
            self._submit_pending()

    def _start_submission(self, draft: str) -> None:
        if self._pending_state_error:
            self._show_line(
                "Pending-request state must be recovered first (/retry). Your draft is kept."
            )
            return
        if self.session_id is None:
            return
        if self._pending_submission is not None:
            self._show_line(
                "Submission pending — wait, or use /retry after an error. Your draft is kept."
            )
            return
        if self.active_run is not None:
            self._show_line(
                "A run is already active — /cancel (Ctrl+C) it first. Your draft is kept."
            )
            return
        if not self._draft_fits(draft):
            return
        self._capture_composer()
        self._pending_submission = PendingSubmission(
            self.session_id,
            draft,
            self.config.max_turns,
            "wisp-tui:" + str(uuid.uuid4()),
            self._project_root,
        )
        if self._composer_draft == draft:
            self._composer_submission_key = self._pending_submission.idempotency_key
        self._composer_dirty = True
        self._flush_composer()
        self._submit_pending()

    def _submit_pending(self) -> None:
        if self._pending_state_error:
            self._restore_pending()
            if self._pending_state_error:
                return
        pending = self._pending_submission
        if pending is None:
            self._show_line("No pending submission to retry.")
            return
        if self._submitting:
            self._show_line("Submission already in progress — please wait.")
            return
        retry_error = pending.retry_error()
        if retry_error:
            self._set_status(retry_error)
            self._show_line(
                "This request's age cannot be retried safely. Inspect the session "
                "in another client; new work remains blocked.",
                "error",
            )
            return
        if self._recovery_view_token is not None:
            self._retry_after_recovery = True
            self._show_line(
                "Loading recovered session details; your explicit /retry will continue "
                "after this read-only check."
            )
            return
        self._submitting = True
        self._set_status("submitting…")

        def work() -> None:
            try:
                run_id = self.submissions.prepare(pending)
                if run_id is None:
                    # Never contact Wisp until the original request is durable.
                    result = self.client.request(
                        "POST",
                        session_path(pending.session_id, "/runs", pending.project_root),
                        pending.body(),
                    )
                    run_id = result.get("runId") if isinstance(result, dict) else None
                if not isinstance(run_id, str) or not UUID_RE.fullmatch(run_id):
                    raise ClientError("invalid_run_reference")
                self.submissions.acknowledge(pending, run_id)
            except (ClientError, SubmissionError) as error:
                self.call_from_thread(self._submission_failed, str(error))
                return
            except Exception:
                self.call_from_thread(self._submission_failed, "internal_error")
                return
            self.call_from_thread(self._submission_accepted, pending, run_id)

        self._run_threaded(work, group="turn")

    def _submission_failed(self, code: str) -> None:
        self._submitting = False
        self._set_status(f"submission error: {code} · /retry")
        self._show_line(
            "Submission not acknowledged; it may exist server-side. "
            "Use /retry to resend the original request with the same ID. "
            "Wi will recover a durably saved request after exit. "
            "A pending_state error must be resolved before any new work.",
            "error",
        )
        editor = self.query_one("#input", PromptInput)
        if not editor.value and self._pending_submission is not None:
            editor.value = self._pending_submission.draft
            self._composer_draft = editor.value
            self._composer_submission_key = self._pending_submission.idempotency_key
            self._composer_dirty = True
            self._queue_composer_save()

    def _submission_accepted(self, pending: PendingSubmission, run_id: str) -> None:
        history = self._pending_view_history
        self._pending_view_history = None
        self._recovery_view_token = None
        self._retry_after_recovery = False
        self._submitting = False
        self._pending_submission = None
        editor = self.query_one("#input", PromptInput)
        if editor.value == pending.draft:
            editor.value = ""
        self._prompt_history = bounded_history((*self._prompt_history, pending.draft))
        editor.set_prompt_history(self._prompt_history)
        self._composer_dirty = True
        self._flush_composer()
        self._add_message(ChatMessage(role="user", text=pending.draft.strip()))
        self._record_locally(
            {"type": "message", "role": "user", "text": pending.draft.strip(), "runId": run_id},
            pending.session_id,
        )
        self._begin_run(run_id, history=history)
        self._start_follow()
        if self._cancel_when_acknowledged:
            self._cancel_when_acknowledged = False
            self._cancel_run()
