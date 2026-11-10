"""Textual chat application for the local Wisp runtime.

The app is a thin, private client: your messages go to the Wisp runtime on
loopback, and its answers are rendered locally. No other model or service is
in the path. At startup Git may ask the checkout's origin for release tags.
Worker threads perform all network I/O and marshal results into the UI thread
via ``call_from_thread``.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import BindingType
from textual.containers import Container, Horizontal
from textual.widgets import Static

from . import __version__
from .app_base import ReleaseCheck
from .app_sessions import SessionLayer
from .chat_view import ChatItem, ChatScroll, JumpToLatest
from .client import ClientError
from .commands import HELP_TEXT, NAMES, mistyped_command, parse_command
from .editor import PromptInput
from .terminal_status import TabStatus
from .text import metadata_text, model_label, safe_text, title_from
from .update import check_release


class WispTuiApp(SessionLayer):
    # Free the SSH-safe Ctrl+P byte for prompt recall; keep the palette available.
    COMMAND_PALETTE_BINDING = "ctrl+shift+p"
    # Absolute, so subclasses defined elsewhere (test fixtures) still find it.
    CSS_PATH = Path(__file__).with_name("app.tcss")

    BINDINGS: ClassVar[list[BindingType]] = [
        ("f1", "help", "Help"),
        ("f2", "sessions", "Sessions"),
        ("f3", "model", "Model"),
        ("ctrl+c", "cancel", "Cancel/exit"),
    ]

    def compose(self) -> ComposeResult:
        yield self._welcome
        yield ChatScroll(id="chat")
        # Above the docked bottom, so showing them shortens the chat instead of
        # moving the composer and footer.
        yield JumpToLatest("\u2193 Jump to latest message", id="jump-latest", markup=False)
        yield Static("", id="status", markup=False)
        yield Static("", id="command-hints", markup=False)
        with Container(id="bottom"):
            yield self._prompt_input
            with Horizontal(id="location-row"):
                yield Static("", id="location", markup=False)
                yield Static("", id="session-id", markup=False)
            with Horizontal(id="summary"):
                yield Static("", id="usage-info", markup=False)
                with Horizontal(id="selection-summary"):
                    yield Static("", id="session-info", markup=False)
                    yield Static("", id="thinking-info", markup=False)
            yield Static("", id="release-notice", markup=False)
        yield Static("", id="flash", markup=False)

    def on_mount(self) -> None:
        self._activity_timer = self.set_interval(0.1, self._tick_activity, pause=True)
        self._render_status()
        self._render_metadata()
        self._restore_pending()
        self._restore_composer()
        self._prompt_input.focus()
        self.watch(self.query_one("#chat"), "scroll_y", self._update_jump_latest)
        self._run_threaded(self._bootstrap_worker)
        if self._release_check is not None:
            self.run_worker(self._check_release(self._release_check), exit_on_error=False)

    async def _check_release(self, check: ReleaseCheck) -> None:
        # A subprocess, not a thread worker: exit must never wait on the network.
        tag = await check_release(check.root, __version__)
        if tag and self._prompt_input.is_attached:
            notice = self.query_one("#release-notice", Static)
            notice.update(f"wisp-tui {tag} is available · run {check.update_command}")
            notice.display = True

    def _bootstrap_worker(self) -> None:
        try:
            health = self.client.request("GET", "/v1/health", timeout=5.0)
        except ClientError as error:
            self.call_from_thread(
                self._set_status, f"runtime unreachable: {error}", TabStatus.ERROR
            )
            self.call_from_thread(
                self._show_line, "Cannot reach the Wisp runtime. Start it and rerun wisp-tui."
            )
            return
        status = health.get("status") if isinstance(health, dict) else None
        if status != "ready":
            self.call_from_thread(self._set_status, "runtime_not_ready", TabStatus.ERROR)
            return
        self.call_from_thread(self._bootstrap_complete)

    def _bootstrap_complete(self) -> None:
        # A slow health check must not replace a session the user just opened.
        if (
            self.session_id is not None
            or self._session_change_in_progress
            or self._pending_state_error
        ):
            return
        self._set_status("")
        mode = self.startup.mode
        if mode == "session":
            self._open_session(self.startup.session_id)
        elif mode == "continue":
            latest = self.sessions.latest()
            if latest:
                self._open_session(latest)
            else:
                self._show_line("No saved sessions yet — starting a new one.")
        elif mode == "resume":
            self.action_resume(self.startup.session_id)

    def on_prompt_input_submitted(self, event: PromptInput.Submitted) -> None:
        self._capture_composer()
        if not event.value.strip():
            return
        if parse_command(event.value) is not None:
            event.input.value = ""
            self._handle_command(event.value)
            return
        near = mistyped_command(event.value)
        if near is not None:
            event.input.value = ""
            self._show_line(
                f"unknown command {event.value.strip().lower()} — did you mean {near}? "
                "Nothing was sent; a Wisp Skill with this name needs arguments."
            )
            return
        if self._session_change_in_progress:
            self._show_line("Session/history load in progress — your draft is kept.")
            return
        if self.session_id is None:
            if not self._draft_fits(event.value):
                return
            # Fresh start: create the Wisp session now, then submit. Nothing is
            # created on the server until there is something to send.
            draft = event.value
            self._new_session(
                self._next_session_title or title_from(draft),
                on_ready=lambda: self._start_submission(draft),
            )
            return
        self._start_submission(event.value)

    def action_cancel(self) -> None:
        """Ctrl+C: cancel the active run; press twice (within 3s) to leave."""
        now = time.monotonic()
        if now - self._last_ctrl_c <= 3.0:
            self.exit()
            return
        self._last_ctrl_c = now
        if self._pending_submission is not None and not self._cancel_when_acknowledged:
            self._cancel_run()
        elif self.active_run is not None and not self._cancel_requested:
            self._cancel_run()
            self._set_status("cancellation requested — press Ctrl+C again to leave")
        else:
            self._set_status("press Ctrl+C again to leave")

    def action_help(self) -> None:
        self._mount_chat(ChatItem("assistant", HELP_TEXT))

    def _handle_command(self, text: str) -> None:
        command = parse_command(text)
        if command is not None:
            name, argument = command
            COMMAND_HANDLERS[name](self, argument)

    def _status_command(self) -> None:
        if self._pending_state_error:
            submission = "state_error"
        elif self._submitting:
            submission = "sending"
        elif self._pending_submission:
            submission = "unacknowledged"
        else:
            submission = "none"
        if self._project_root:
            folder = metadata_text(self._project_root, 1000)
        elif self.session_id is not None:
            folder = "none known (a Desk Session, or a folder not saved here)"
        elif self._new_session_root:
            folder = f"{metadata_text(self._new_session_root, 1000)} (for the next session)"
        else:
            folder = "none (Wisp's own desk folder)"
        session = self.session_id[:8] if self.session_id else "new"
        thinking = self._model_selection.get("thinking")
        thinking_label = (
            ("on" if thinking else "off") if isinstance(thinking, bool) else "unavailable"
        )
        lines = [
            f"runtime: {self.client.url}",
            f"session: {self.session_id or 'none'}"
            + (f" ({self.session_title or '(untitled)'})" if self.session_id else ""),
            f"active run: {self.active_run or 'none'}",
            f"submission: {submission}",
            f"recovery view: {'loading' if self._recovery_view_token is not None else 'none'}",
            f"max turns: {self.config.max_turns}",
            f"session {session} · {model_label(self._model_selection)}",
            f"thinking: {thinking_label} · context: unavailable",
            f"last run tokens: {self._last_run_tokens or 'unavailable'}",
            f"launch dir: {metadata_text(self._launch_dir, 1000)}",
            f"Wisp project folder: {folder}",
            "Token usage is reported at run completion, not live; "
            "context occupancy is unavailable.",
        ]
        for line in lines:
            self._show_line(line, "tool")

    def on_unmount(self) -> None:
        self._clear_tab_status()
        # The exit transcript prints the stream widget's text, not the batch target.
        self._end_answer()
        self._forget_tool_history_read()
        if self._composer_timer is not None:
            self._composer_timer.stop()
        self._flush_composer()
        self.composer.close()
        self._composer_available = False
        self._recovery_view_token = None
        self._retry_after_recovery = False
        if self._activity_timer is not None:
            self._activity_timer.stop()
        self.submissions.close()
        # Stop local following only; quitting never cancels server-side work.
        if self._follower is not None:
            self._follower.stop()


COMMAND_HANDLERS: dict[str, Callable[[WispTuiApp, str], None]] = {
    "/help": lambda app, _: app.action_help(),
    "/new": lambda app, argument: app._start_fresh(safe_text(argument, 80)),
    "/resume": WispTuiApp.action_resume,
    "/sessions": lambda app, _: app.action_sessions(),
    "/model": lambda app, _: app.action_model(),
    "/thinking": lambda app, argument: app.action_thinking(argument.lower()),
    "/cancel": lambda app, _: app._cancel_run(),
    "/retry": lambda app, _: app._submit_pending(),
    "/reconnect": lambda app, _: app._start_follow(),
    "/status": lambda app, _: app._status_command(),
    "/exit": lambda app, _: app.exit(),
    "/quit": lambda app, _: app.exit(),
}
if set(COMMAND_HANDLERS) != set(NAMES):
    raise RuntimeError("commands.COMMANDS and COMMAND_HANDLERS disagree")
