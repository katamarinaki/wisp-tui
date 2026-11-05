"""Opening, creating and switching sessions, and the session and model pickers."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .app_base import FRESH_STATUS
from .app_submission import SubmissionLayer
from .client import ClientError
from .history import ChatMessage, parse_history
from .runs import find_active_run
from .screens import ModelPicker, SessionPicker, server_session_label
from .sessions import RESUME_QUERY_RE, UUID_RE
from .text import display_path, model_label, safe_text


class SessionLayer(SubmissionLayer):
    def _reserve_session_change(self) -> bool:
        if self._pending_state_error:
            self._show_line("Pending-request state must be recovered first (/retry).")
            return False
        if self._session_change_in_progress:
            self._show_line("Session/history load already in progress — please wait.")
            return False
        if self._pending_submission is not None:
            self._show_line(
                "Resolve the pending submission before changing sessions or models (/retry)."
            )
            return False
        if self.active_run is not None:
            self._show_line("A run is active — /cancel before changing sessions or models.")
            return False
        self._session_change_in_progress = True
        self._render_activity()
        return True

    def _release_session_change(self) -> None:
        self._session_change_in_progress = False
        self._render_activity()

    def _open_session(self, session_id: str) -> None:
        if not UUID_RE.fullmatch(session_id):
            self._show_line("invalid_session_id", "error")
            return
        if not self._reserve_session_change():
            return

        def work() -> None:
            try:
                session = self.client.request("GET", f"/v1/sessions/{session_id}")
            except ClientError as error:
                if str(error).startswith("runtime_http_404"):
                    self.call_from_thread(self._session_missing, session_id)
                    return
                raise
            if not isinstance(session, dict) or session.get("id") != session_id:
                self.call_from_thread(self._show_line, "session no longer exists", "error")
                return
            # Discover first: a run that finishes while history loads still has
            # a known ID to reconcile, rather than disappearing from discovery.
            runs = self.client.request(
                "GET", f"/v1/runs?sessionId={session_id}&status=queued,running,parked&limit=200"
            )
            active_id = find_active_run(runs, session_id)
            history = self.client.request("GET", f"/v1/sessions/{session_id}/history")
            self.call_from_thread(self._session_loaded, session, parse_history(history), active_id)

        self._run_threaded(work, group="load", on_done=self._release_session_change)

    def _session_missing(self, session_id: str) -> None:
        self._set_status("session not found on the runtime")
        path = self.sessions.path_for(session_id)
        note = f" Your local record: {display_path(path)}" if path else ""
        self._show_line(
            f"Session {session_id[:8]} no longer exists on the Wisp runtime.{note}", "error"
        )

    def _session_loaded(
        self,
        session: dict[str, Any],
        messages: list[ChatMessage],
        active_run_id: str,
        announce_empty: bool = True,
    ) -> None:
        session_id = str(session.get("id", ""))
        if self.session_id != session_id:
            self._last_run_tokens = ""
        self.session_id = session_id
        self._selection_updated(session, session_id)
        self._restore_matching_draft()
        if self._composer_editor is not None and self._composer_editor.value:
            self._composer_dirty = True
            self._queue_composer_save()
        self.session_title = safe_text(session.get("title"), 80) or "(untitled)"
        self.title = f"Wisp — {self.session_title}"
        # Only show the tail of long histories (see MAX_CHAT_ITEMS).
        hidden = max(0, len(messages) - self.MAX_CHAT_ITEMS)
        self._reset_chat(hidden)
        for message in messages[hidden:]:
            self._add_message(message)
        if not messages and announce_empty:
            self._show_line(f"Session '{self.session_title}' is empty. Say something to start.")
        self._set_status(f"session {self.session_id[:8]} · {model_label(session)}")
        if active_run_id:
            self._begin_run(active_run_id, history=messages)
            self._show_line("Reattaching to this session's ongoing run.", "tool")
            self._start_follow()

    def _new_session(
        self, title: str, on_ready: Callable[[], None] | None = None, announce: bool = True
    ) -> None:
        """Create a Wisp session now; then run ``on_ready`` (e.g. submit)."""
        if not self._reserve_session_change():
            return
        body: dict[str, Any] = {"origin": "cli"}
        if title:
            body["title"] = title

        def work() -> None:
            try:
                session = self.client.request("POST", "/v1/sessions", body)
            except ClientError as error:
                self.call_from_thread(self._session_create_failed, str(error))
                return
            except Exception:
                self.call_from_thread(self._session_create_failed, "internal_error")
                return
            if (
                isinstance(session, dict)
                and isinstance(session.get("id"), str)
                and UUID_RE.fullmatch(session["id"])
            ):
                self.call_from_thread(self._session_created, session, on_ready, announce)
            else:
                self.call_from_thread(self._session_create_failed, "invalid_session")

        self._run_threaded(work, group="load")

    def _session_created(
        self, session: dict[str, Any], on_ready: Callable[[], None] | None, announce: bool
    ) -> None:
        # Release before continuing, so the continuation (submit, model
        # picker) is not mistaken for a session change still in progress.
        self._release_session_change()
        self._next_session_title = ""
        self._session_loaded(session, [], "", announce_empty=announce)
        if on_ready is not None:
            on_ready()

    def _session_create_failed(self, code: str) -> None:
        self._release_session_change()
        self._set_status(f"error: {code}")
        self._show_line(f"Could not start a Wisp session ({code}). Your draft is kept.", "error")

    def _start_fresh(self, title: str) -> None:
        """/new: leave the current session; the next message creates one."""
        if not self._reserve_session_change():
            return
        self._release_session_change()
        self.session_id = None
        self.session_title = ""
        self._restore_matching_draft()
        if self._composer_editor is not None and self._composer_editor.value:
            self._composer_dirty = True
            self._queue_composer_save()
        self._model_selection = {}
        self._last_run_tokens = ""
        self._render_metadata()
        self.title = "wisp-tui"
        self._next_session_title = title
        self._reset_chat()
        self._welcome.display = True
        self._set_status(FRESH_STATUS)

    def action_resume(self, query: str = "") -> None:
        """Saved-session picker; with an ID or prefix, open the match directly."""
        if self._approval_modal is not None:
            return
        query = query.strip().lower()
        if query and not RESUME_QUERY_RE.fullmatch(query):
            self._show_line("Usage: /resume [session ID or prefix, 4+ hex characters]")
            return
        summaries = self.sessions.recent(limit=10_000) if query else self.sessions.recent()
        if query:
            matches = self.sessions.find(query)
            if len(matches) == 1:
                self._open_session(matches[0])
                return
            if not matches:
                if UUID_RE.fullmatch(query):
                    # Not saved here, but a full ID may still be a Wisp session.
                    self._open_session(query)
                    return
                self._show_line(f"No saved session matches '{query}'. F2 lists all Wisp sessions.")
                return
            wanted = set(matches)
            summaries = [item for item in summaries if item.id in wanted]
        if not summaries:
            self._show_line("No saved sessions yet — sessions are saved as you chat.")
            return
        heading = f"Saved sessions matching '{query}'" if query else "Saved sessions (newest first)"
        self.push_screen(
            SessionPicker([(item.id, item.label()) for item in summaries], heading),
            self._picker_answered,
        )

    def action_sessions(self) -> None:
        def work() -> None:
            sessions = self.client.request("GET", "/v1/sessions")
            if not isinstance(sessions, list):
                self.call_from_thread(self._show_line, "invalid sessions response", "error")
                return
            self.call_from_thread(self._show_session_picker, sessions[:100])

        self._run_threaded(work, group="load")

    def _show_session_picker(self, sessions: list[dict[str, Any]]) -> None:
        if self._approval_modal is not None:
            return
        entries = [
            (str(item.get("id", "")), server_session_label(item))
            for item in sessions
            if isinstance(item, dict)
        ]
        if not entries:
            self._show_line("No Wisp sessions yet — your first message starts one.")
            return
        self.push_screen(
            SessionPicker(entries, "Wisp sessions, all clients (newest first)"),
            self._picker_answered,
        )

    def _picker_answered(self, session_id: str | None) -> None:
        if session_id and UUID_RE.fullmatch(session_id):
            self._open_session(session_id)
        elif self.session_id is None:
            self._welcome.display = True

    def action_model(self) -> None:
        if self.session_id is None:
            # The model is chosen per session: start one now, then pick.
            self._new_session(self._next_session_title, on_ready=self.action_model, announce=False)
            return

        session_id = self.session_id

        def work() -> None:
            models = self.client.request("GET", "/v1/models")
            data = models.get("data") if isinstance(models, dict) else None
            if not isinstance(data, list):
                self.call_from_thread(self._show_line, "invalid models response", "error")
                return
            session = self.client.request("GET", f"/v1/sessions/{session_id}")
            current = session.get("effectiveSelection") if isinstance(session, dict) else None
            self.call_from_thread(self._show_model_picker, data, current, session_id)

        self._run_threaded(work, group="load")

    def _show_model_picker(
        self, models: list[dict[str, Any]], current: dict[str, Any] | None, session_id: str
    ) -> None:
        if (
            self.session_id != session_id
            or self._session_change_in_progress
            or self._approval_modal is not None
        ):
            return
        self.push_screen(
            ModelPicker(models, current),
            lambda selection: self._model_answered(selection, session_id),
        )

    def _model_answered(self, selection: dict[str, Any] | None, session_id: str) -> None:
        if selection is None or self.session_id != session_id:
            return
        if not self._reserve_session_change():
            return

        def work() -> None:
            session = self.client.request(
                "PATCH", f"/v1/sessions/{session_id}", {"selection": selection}
            )
            if isinstance(session, dict):
                self.call_from_thread(self._selection_updated, session, session_id)
                self.call_from_thread(
                    self._set_status, f"session {session_id[:8]} · {model_label(session)}"
                )
            else:
                self.call_from_thread(self._show_line, "could not set selection", "error")

        self._run_threaded(work, group="load", on_done=self._release_session_change)
