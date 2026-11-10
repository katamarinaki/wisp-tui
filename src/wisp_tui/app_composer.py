"""Prompt editor persistence: recall history, saved drafts and their recovery."""

from __future__ import annotations

from textual.widgets import Static, TextArea

from .app_chat import ChatLayer
from .commands import completions, is_command_text
from .composer import ComposerError, ComposerState, bounded_history
from .submissions import MAX_DRAFT_BYTES


class ComposerLayer(ChatLayer):
    def _composer_warning(self, code: str) -> None:
        if code not in self._composer_errors:
            self._composer_errors.add(code)
            message = f"Input history/draft recovery: {code}. Current text is kept in memory."
            if self._prompt_input.is_attached:
                self._show_line(message, "error")
            else:
                self.console.print(f"Input history/draft was not saved: {code}")

    def _restore_composer(self) -> None:
        editor = self._prompt_input
        try:
            state = self.composer.load()
        except ComposerError as error:
            self._composer_warning(str(error))
            return
        self._composer_available = True
        self._prompt_history = state.history
        acknowledged = self.submissions.loaded_acknowledgement
        if acknowledged is not None:
            self._prompt_history = bounded_history((*self._prompt_history, acknowledged.draft))
            # An ack may be durable before the UI clears/saves the editor.
            # Only a draft explicitly tagged with this request can be discarded.
            if (
                state.session_id == acknowledged.session_id
                and state.draft == acknowledged.draft
                and state.submission_key == acknowledged.idempotency_key
            ):
                state = ComposerState(self._prompt_history)
                self._composer_dirty = True
            elif self._prompt_history != state.history:
                self._composer_dirty = True
        editor.set_prompt_history(self._prompt_history)
        self._recoverable_draft = state if state.draft else None
        pending = self._pending_submission
        if pending is not None and state.draft and state.session_id == pending.session_id:
            self._recoverable_draft = None
            self._load_draft(state.draft, state.submission_key)
        else:
            self._composer_draft = editor.value
            self._composer_submission_key = pending.idempotency_key if pending is not None else None
        if pending is not None:
            self._composer_dirty = True
        elif self.startup.mode == "new":
            self._restore_matching_draft()
        self._announce_saved_draft()

    def _announce_saved_draft(self) -> None:
        if self._recoverable_draft is not None:
            session = self._recoverable_draft.session_id
            target = f"session {session}" if session else "a fresh session"
            command = f"/resume {session}" if session else "/new"
            when = (
                " after the pending request is acknowledged"
                if self._pending_submission is not None
                else ""
            )
            self._show_line(
                f"Saved unsent draft for {target}. Use {command}{when} "
                "to recover it; the saved draft was not sent."
            )

    def _load_draft(self, draft: str, submission_key: str | None) -> None:
        """Put ``draft`` in the editor as the draft to save, tagged with its request."""
        self._prompt_input.value = draft
        self._composer_draft = draft
        self._composer_submission_key = submission_key

    def _restore_matching_draft(self) -> None:
        state = self._recoverable_draft
        if (
            state is not None
            and not self._prompt_input.value
            and state.session_id == self.session_id
        ):
            self._recoverable_draft = None
            self._load_draft(state.draft, state.submission_key)
            self._show_line("Recovered unsent draft. Nothing was sent; Enter sends it when ready.")

    def _draft_follows_session(self) -> None:
        """The saved draft names its session: restore the new one's, or re-save under it."""
        self._restore_matching_draft()
        if self._prompt_input.value:
            self._save_draft_soon()

    def _capture_composer(self) -> None:
        editor = self._prompt_input
        if editor.value != self._composer_draft:
            self._composer_draft = editor.value
            self._composer_submission_key = None
            if editor.value and not is_command_text(editor.value):
                self._recoverable_draft = None
            self._composer_dirty = True

    def _save_draft_soon(self) -> None:
        """The editor's draft changed outside typing (load, session, submission)."""
        self._composer_dirty = True
        self._queue_composer_save()

    def _queue_composer_save(self) -> None:
        if self._composer_timer is not None:
            self._composer_timer.stop()
        if self._composer_available and self._composer_dirty:
            self._composer_timer = self.set_timer(0.35, self._flush_composer)

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area is self._prompt_input:
            self._render_command_hints(self._prompt_input.text)
            self._capture_composer()
            self._queue_composer_save()

    def _render_command_hints(self, text: str) -> None:
        matches = completions(text)
        lines = [f"{f'{name} {usage}'.rstrip():<16}{about}" for name, usage, about in matches]
        if len(matches) > 6:
            # Too many to describe without crowding the transcript: names only.
            lines = ["  ".join(command.name for command in matches)]
        # A queued change can arrive after shutdown removed the footer.
        for hints in self.query("#command-hints").results(Static):
            hints.update("\n".join(lines))
            hints.display = bool(lines)

    def _flush_composer(self) -> None:
        self._capture_composer()
        if not self._composer_available or not self._composer_dirty:
            return
        if len(self._composer_draft.encode("utf-8")) > MAX_DRAFT_BYTES:
            self._composer_warning("draft_too_large_to_save")
            return
        try:
            draft = "" if is_command_text(self._composer_draft) else self._composer_draft
            recovery = self._recoverable_draft
            # Unmatched saved text is independent of a restored pending request.
            # Only an explicit editor change or matching restore claims its slot.
            if recovery is not None:
                state = ComposerState(
                    self._prompt_history,
                    recovery.draft,
                    recovery.session_id,
                    recovery.submission_key,
                )
            else:
                state = ComposerState(
                    self._prompt_history, draft, self.session_id, self._composer_submission_key
                )
            self.composer.save(state)
        except ComposerError as error:
            self._composer_warning(str(error))
        else:
            self._composer_dirty = False

    def _draft_fits(self, draft: str) -> bool:
        if len(draft.encode("utf-8")) > MAX_DRAFT_BYTES:
            self._show_line(
                f"Prompt exceeds the safe recovery limit ({MAX_DRAFT_BYTES // 1024} KiB). "
                "Split it; your draft is kept."
            )
            return False
        return True
