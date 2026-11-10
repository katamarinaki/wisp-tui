"""Textual chat application for the local Wisp runtime.

The app is a thin, private client: your messages go to the Wisp runtime on
loopback, and its answers are rendered locally. No other model or service is
in the path. At startup Git may ask the checkout's origin for release tags.
Worker threads perform all network I/O and marshal results into the UI thread
via ``call_from_thread``.
"""

from __future__ import annotations

import asyncio
import contextlib
import difflib
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import BindingType
from textual.containers import Container, Horizontal
from textual.widgets import Static

from . import __version__
from .app_base import FRESH_STATUS, ReleaseCheck
from .app_sessions import SessionLayer
from .chat_view import ChatItem, ChatScroll, JumpToLatest
from .client import ClientError
from .editor import PromptInput
from .help_text import HELP_TEXT, RESERVED_COMMANDS
from .sessions import UUID_RE
from .text import metadata_text, safe_text, title_from
from .update import RELEASE_QUERY, newer_release, release_query_env


class WispTuiApp(SessionLayer):
    TITLE = "wisp-tui"

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
        self.query_one("#input", PromptInput).focus()
        self.watch(self.query_one("#chat"), "scroll_y", self._update_jump_latest)
        self._run_threaded(self._bootstrap_worker)
        if self._release_check is not None:
            self.run_worker(self._check_release(self._release_check), exit_on_error=False)

    async def _check_release(self, check: ReleaseCheck) -> None:
        # A subprocess, not a thread worker: exit must never wait on the network.
        try:
            process = await asyncio.create_subprocess_exec(
                *RELEASE_QUERY,
                cwd=check.root,
                env=release_query_env(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            return
        try:
            output, _ = await asyncio.wait_for(process.communicate(), 15.0)
        except TimeoutError:
            return
        finally:
            if process.returncode is None:
                # Its own session: this also stops ssh or https helpers Git started.
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
        tag = newer_release(output.decode("utf-8", "replace"), __version__)
        if process.returncode == 0 and tag and self._prompt_input.is_attached:
            notice = self.query_one("#release-notice", Static)
            notice.update(f"wisp-tui {tag} is available · run {check.update_command}")
            notice.display = True

    def _bootstrap_worker(self) -> None:
        try:
            health = self.client.request("GET", "/v1/health", timeout=5.0)
        except ClientError as error:
            self.call_from_thread(self._set_status, f"runtime unreachable: {error}")
            self.call_from_thread(
                self._show_line, "Cannot reach the Wisp runtime. Start it and rerun wisp-tui."
            )
            return
        status = health.get("status") if isinstance(health, dict) else None
        if status != "ready":
            self.call_from_thread(self._set_status, "runtime_not_ready")
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
        self._set_status(FRESH_STATUS)
        mode = self.startup.mode
        if mode == "session" and UUID_RE.fullmatch(self.startup.session_id):
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
        text = event.value.strip()
        if not text:
            return
        word = text.split(maxsplit=1)[0].lower()
        single_line = "\n" not in event.value and "\r" not in event.value
        if single_line and word in RESERVED_COMMANDS:
            event.input.value = ""
            self._handle_command(text)
            return
        # Any other "/name args" goes to Wisp, which runs its Skill of that name,
        # except a lone word one typo away from a command: that would start a run.
        if single_line and word.startswith("/") and word == text.lower():
            near = difflib.get_close_matches(word, RESERVED_COMMANDS, 1, 0.8)
            if near:
                event.input.value = ""
                self._show_line(
                    f"unknown command {word} — did you mean {near[0]}? Nothing was sent; "
                    "a Wisp Skill with this name needs arguments."
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
                announce=False,
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
        self._show_help()

    def _show_help(self) -> None:
        self._mount_chat(ChatItem("assistant", HELP_TEXT))

    def _handle_command(self, text: str) -> None:
        parts = text.split(maxsplit=1)
        command = parts[0].lower()
        argument = parts[1].strip() if len(parts) > 1 else ""
        if command == "/help":
            self._show_help()
        elif command == "/new":
            self._start_fresh(safe_text(argument, 80))
        elif command == "/resume":
            self.action_resume(argument)
        elif command == "/sessions":
            self.action_sessions()
        elif command == "/model":
            self.action_model()
        elif command == "/thinking":
            self.action_thinking(argument.lower())
        elif command == "/cancel":
            self._cancel_run()
        elif command == "/retry":
            self._submit_pending()
        elif command == "/reconnect":
            self._start_follow()
        elif command == "/status":
            self._status_command()
        elif command in ("/exit", "/quit"):
            self.exit()

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
        elif self.startup.project_root:
            folder = f"{metadata_text(self.startup.project_root, 1000)} (for the next session)"
        else:
            folder = "none (Wisp's own desk folder)"
        lines = [
            f"runtime: {self.client.url}",
            f"session: {self.session_id or 'none'}"
            + (f" ({self.session_title})" if self.session_id else ""),
            f"active run: {self.active_run or 'none'}",
            f"submission: {submission}",
            f"recovery view: {'loading' if self._recovery_view_token is not None else 'none'}",
            f"max turns: {self.config.max_turns}",
            *self._metadata_lines(),
            f"Wisp project folder: {folder}",
            "Token usage is reported at run completion, not live; "
            "context occupancy is unavailable.",
        ]
        for line in lines:
            self._show_line(line, "tool")

    def on_unmount(self) -> None:
        self._clear_tab_status()
        # The exit transcript prints the stream widget's text, not the batch target.
        if self._stream_view is not None:
            self._stream_view.flush()
        self._tool_history_token = None
        self._tool_history_targets = ()
        self._tool_history_again = False
        if self._composer_timer is not None:
            self._composer_timer.stop()
        self._flush_composer()
        self.composer.close()
        self._composer_available = False
        self._recovery_view_token = None
        self._retry_after_recovery = False
        self._pending_view_history = None
        self._reattached_checkpoint = None
        if self._activity_timer is not None:
            self._activity_timer.stop()
        self.submissions.close()
        # Stop local following only; quitting never cancels server-side work.
        if self._follower is not None:
            self._follower.stop()
