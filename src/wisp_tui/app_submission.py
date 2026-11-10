"""Submitting prompts durably, and recovering a submission left unacknowledged."""

from __future__ import annotations

from collections.abc import Callable

from .app_runs import RunLayer
from .client import UUID_RE, ClientError, safe_code, session_path
from .composer import bounded_history
from .history import ChatMessage
from .runs import SessionState, load_session
from .submissions import PendingSubmission, SubmissionError, new_idempotency_key
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
                self._set_status("" if self.session_id is None else "session ready")
            return
        self._pending_submission = pending
        self._show_session(pending.session_id, pending.project_root, "Recovered submission")
        self._keep_pending_draft()
        self._set_status("unacknowledged submission restored · /retry")
        self._show_line(
            "Recovered an unacknowledged request. Nothing was resubmitted. "
            "/retry explicitly reuses the original input, options and request ID. "
            "Session/model changes stay blocked until it is acknowledged."
        )
        self._load_pending_view(pending)

    def _keep_pending_draft(self) -> None:
        """Show the pending request's text in an empty editor and save it as the draft."""
        pending = self._pending_submission
        if pending is not None and not self._prompt_input.value:
            self._load_draft(pending.draft, pending.idempotency_key)
            self._save_draft_soon()

    def _load_pending_view(self, pending: PendingSubmission) -> None:
        """Inspect the original session without identifying/replaying its request."""
        token = object()
        self._recovery_view_token = token
        self._pending_view_shown = False
        epoch = self._chat_epoch

        def work() -> Callable[[], None]:
            state = load_session(self.client, pending.session_id, timeout=5.0)
            return lambda: self._pending_view_loaded(token, pending, epoch, state)

        self._run_threaded(
            work,
            group="recovery",
            on_done=lambda: self._pending_view_done(token, pending),
            on_error=lambda code: self._pending_view_failed(token, pending, code),
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
        self, token: object, pending: PendingSubmission, epoch: int, state: SessionState
    ) -> None:
        if not self._pending_view_current(token, pending):
            return
        title = safe_text(state.session.get("title"), 80)
        self._show_session(pending.session_id, self._project_root, title)
        self._selection_updated(state.session, pending.session_id)
        # A newer view cancels history restoration, but metadata is still useful.
        if self._chat_epoch != epoch:
            return
        self._show_history(state.messages)
        self._pending_view_shown = True
        self._show_line(
            "Recovered request remains unacknowledged. This view is read-only; "
            "only explicit /retry reuses the original request ID."
        )
        if state.active_run:
            self._show_line(
                f"Runtime reports active run {state.active_run[:8]}. "
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
            new_idempotency_key(),
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

        def work() -> Callable[[], None]:
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
            accepted: str = run_id
            return lambda: self._submission_accepted(pending, accepted)

        self._run_threaded(work, group="turn", on_error=self._submission_failed)

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
        self._keep_pending_draft()

    def _submission_accepted(self, pending: PendingSubmission, run_id: str) -> None:
        reattached = self._pending_view_shown
        self._pending_view_shown = False
        self._recovery_view_token = None
        self._retry_after_recovery = False
        self._submitting = False
        self._pending_submission = None
        editor = self._prompt_input
        if editor.value == pending.draft:
            editor.value = ""
        self._prompt_history = bounded_history((*self._prompt_history, pending.draft))
        editor.set_prompt_history(self._prompt_history)
        self._composer_dirty = True
        self._flush_composer()
        self._add_message(ChatMessage(role="user", text=pending.draft.strip()))
        self._record_locally(
            {"type": "message", "role": "user", "text": pending.draft.strip(), "runId": run_id}
        )
        self._begin_run(run_id, reattached=reattached)
        self._start_follow()
        if self._cancel_when_acknowledged:
            self._cancel_when_acknowledged = False
            self._cancel_run()
