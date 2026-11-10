"""Opening, creating and switching sessions, and the session and model pickers."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .app_submission import SubmissionLayer
from .client import UUID_RE, ClientError, session_path, valid_project_root
from .runs import SessionState, load_session
from .screens import AUTO_MODES, ModelPicker, SessionPicker, server_session_label
from .sessions import RESUME_QUERY_RE
from .terminal_status import TabStatus
from .text import display_path, metadata_text, model_label, safe_text


class SessionLayer(SubmissionLayer):
    def _session_change_blocked(self) -> bool:
        if self._pending_state_error:
            self._show_line("Pending-request state must be recovered first (/retry).")
        elif self._session_change_in_progress:
            self._show_line("Session/history load already in progress — please wait.")
        elif self._pending_submission is not None:
            self._show_line(
                "Resolve the pending submission before changing sessions or models (/retry)."
            )
        elif self.active_run is not None:
            self._show_line("A run is active — /cancel before changing sessions or models.")
        else:
            return False
        return True

    def _reserve_session_change(self) -> bool:
        if self._session_change_blocked():
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
            root = self.sessions.project_root(session_id)
            try:
                state = load_session(self.client, session_id)
            except ClientError as error:
                if str(error).startswith("runtime_http_404"):
                    self.call_from_thread(self._session_missing, session_id)
                    return
                raise
            self.call_from_thread(self._session_loaded, state, root)

        self._run_threaded(work, group="load", on_done=self._release_session_change)

    def _session_missing(self, session_id: str) -> None:
        self._set_status("session not found on the runtime", TabStatus.ERROR)
        path = self.sessions.path_for(session_id)
        note = f" Your local record: {display_path(path)}" if path else ""
        self._show_line(
            f"Session {session_id[:8]} no longer exists on the Wisp runtime.{note}", "error"
        )

    def _enter_session(self, session: dict[str, Any], project_root: str | None) -> None:
        """Make a session the runtime returned current."""
        session_id = str(session["id"])
        # The runtime reports the folder only while it holds the session in memory.
        root = valid_project_root(session.get("projectRoot")) or project_root
        self._show_session(session_id, root, safe_text(session.get("title"), 80))
        self._selection_updated(session, session_id)
        self._draft_follows_session()
        self._show_selection_status()

    def _show_selection_status(self) -> None:
        if self.session_id is not None:
            self._set_status(
                f"session {self.session_id[:8]} · {model_label(self._model_selection)}"
            )

    def _session_loaded(self, state: SessionState, project_root: str | None) -> None:
        self._enter_session(state.session, project_root)
        self._show_history(state.messages)
        if not state.messages:
            title = self.session_title or "(untitled)"
            self._show_line(f"Session '{title}' is empty. Say something to start.")
        if state.active_run:
            self._begin_run(state.active_run, reattached=True)
            self._show_line("Reattaching to this session's ongoing run.", "tool")
            self._start_follow()

    def _new_session(self, title: str, on_ready: Callable[[], None]) -> None:
        """Create a Wisp session now; then run ``on_ready`` (e.g. submit)."""
        if not self._reserve_session_change():
            return
        body: dict[str, Any] = {"origin": "cli"}
        if title:
            body["title"] = title
        root = self._new_session_root
        attempts = [{**body, "projectRoot": root}, body] if root else [body]

        def work() -> Callable[[], None] | None:
            for attempt in attempts:
                try:
                    session = self.client.request("POST", "/v1/sessions", attempt)
                except ClientError as error:
                    # A folder the runtime refuses falls back to a Desk Session.
                    if attempt is not attempts[-1] and str(error).startswith("runtime_http_400"):
                        continue
                    raise
                if not (
                    isinstance(session, dict)
                    and isinstance(session.get("id"), str)
                    and UUID_RE.fullmatch(session["id"])
                ):
                    raise ClientError("invalid_session")
                root = attempt.get("projectRoot")
                return lambda: self._session_created(session, on_ready, root)
            return None

        self._run_threaded(work, group="load", on_error=self._session_create_failed)

    def _session_created(
        self, session: dict[str, Any], on_ready: Callable[[], None], project_root: str | None
    ) -> None:
        # Release before continuing, so the continuation (submit, model
        # picker) is not mistaken for a session change still in progress.
        self._release_session_change()
        self._next_session_title = ""
        if "projectRoot" in session:
            # Wisp's answer wins: a null means it made a Desk Session.
            project_root = None
        self._enter_session(session, project_root)
        self._reset_chat()
        if self._new_session_root and self._project_root is None:
            # Asked once: the rest of this launch makes Desk Sessions, and a
            # prompt written for the folder is not sent elsewhere unasked.
            self._new_session_root = None
            self._show_line(
                "Wisp refused this folder as a project; this session has no project folder. "
                "Nothing was sent — send again to continue without one.",
                "error",
            )
            return
        on_ready()

    def _session_create_failed(self, code: str) -> None:
        self._release_session_change()
        self._show_error(code)
        self._show_line(f"Could not start a Wisp session ({code}). Your draft is kept.", "error")

    def _start_fresh(self, title: str) -> None:
        """/new: leave the current session; the next message creates one."""
        if self._session_change_blocked():
            return
        self._show_session(None, None, "")
        self._draft_follows_session()
        self._next_session_title = title
        self._reset_chat()
        self._welcome.display = True
        self._set_status("")

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
            self._new_session(self._next_session_title, on_ready=self.action_model)
            return

        session_id = self.session_id

        def work() -> None:
            data = self._fetch_models()
            if data is None:
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

    def action_thinking(self, argument: str = "") -> None:
        """/thinking [on|off]: set thinking for the current mode/model; no argument toggles."""
        if argument not in ("", "on", "off"):
            self._show_line("usage: /thinking [on|off]")
            return
        if self.session_id is None:
            self._new_session(
                self._next_session_title, on_ready=lambda: self.action_thinking(argument)
            )
            return
        mode = self._model_selection.get("mode")
        model = self._model_selection.get("model")
        if mode not in (*AUTO_MODES, "model") or (mode == "model" and not isinstance(model, str)):
            self._show_line("Current selection is unknown — choose one with /model.", "error")
            return
        thinking = (
            argument == "on" if argument else self._model_selection.get("thinking") is not True
        )
        selection: dict[str, Any] = {"mode": mode, "thinking": thinking}
        if mode != "model":
            self._model_answered(selection, self.session_id)
            return
        selection["model"] = model
        if not thinking:
            self._model_answered(selection, self.session_id)
            return
        session_id = self.session_id

        # The runtime answers 400 to thinking on a model without reasoning.
        def work() -> None:
            data = self._fetch_models()
            if data is None:
                return
            if any(
                isinstance(item, dict) and item.get("id") == model and item.get("reasoning") is True
                for item in data
            ):
                self.call_from_thread(self._model_answered, selection, session_id)
            else:
                self.call_from_thread(
                    self._show_line,
                    f"{metadata_text(model, 200)} doesn't support thinking — "
                    "pick a reasoning model with /model.",
                    "error",
                )

        self._run_threaded(work, group="load")

    def _fetch_models(self) -> list[Any] | None:
        """The runtime's model list, read on a worker thread; None after reporting it invalid."""
        models = self.client.request("GET", "/v1/models")
        data = models.get("data") if isinstance(models, dict) else None
        if isinstance(data, list):
            return data
        self.call_from_thread(self._show_line, "invalid models response", "error")
        return None

    def _model_answered(self, selection: dict[str, Any] | None, session_id: str) -> None:
        if selection is None or self.session_id != session_id:
            return
        if not self._reserve_session_change():
            return
        path = session_path(session_id, "", self._project_root)

        def work() -> None:
            session = self.client.request("PATCH", path, {"selection": selection})
            if isinstance(session, dict):
                self.call_from_thread(self._selection_changed, session, session_id)
            else:
                self.call_from_thread(self._show_line, "could not set selection", "error")

        self._run_threaded(work, group="load", on_done=self._release_session_change)

    def _selection_changed(self, session: dict[str, Any], session_id: str) -> None:
        if self.session_id == session_id:
            self._selection_updated(session, session_id)
            self._show_selection_status()
