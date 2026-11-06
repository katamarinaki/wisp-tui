"""Shared state of the chat app, its worker helper and footer rendering.

The app is split into layers that inherit from each other in this order:
base -> chat -> composer -> runs -> submission -> sessions -> app. A layer
only calls methods of itself or of layers below it; all state lives here so
every layer sees the same attributes.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

from textual import events
from textual.app import App
from textual.timer import Timer
from textual.widgets import Static

from .chat_view import ChatItem, StreamView
from .client import ClientError, WispClient
from .composer import ComposerState, ComposerStore
from .config import Config
from .editor import PromptInput
from .help_text import COMMANDS
from .history import ChatMessage
from .runs import RunFollower
from .screens import ApprovalModal
from .sessions import SessionStore
from .submissions import PendingSubmission, SubmissionStore
from .terminal_status import TabStatus, tab_status_sequence
from .text import display_path, metadata_text, model_label, safe_text
from .tool_view import ToolCallView
from .welcome import Welcome

SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

FRESH_STATUS = "ready · new session"


class ApprovalPrompt(NamedTuple):
    """A confirmation prompt the runtime is waiting on, queued for the dialog."""

    run_id: str
    prompt_id: str
    tool_name: str
    args: Any


class ReleaseCheck(NamedTuple):
    """The checkout to ask its origin for newer release tags, and the update command."""

    root: Path
    update_command: str


@dataclass(frozen=True)
class Startup:
    """How the app starts: a fresh session (default) or an existing one."""

    mode: str = "new"  # "new" | "continue" | "resume" | "session"
    # "session": a full UUID. "resume": optional ID or prefix ("" = picker).
    session_id: str = ""


class AppBase(App[None]):
    # A loaded history prints only its newest items (several screens' worth);
    # Wisp keeps the full session history server-side.
    MAX_CHAT_ITEMS = 60
    # Finished items kept in the chat view (and printed again on exit).
    SHOWN_ITEMS = 200
    # Textual's own selection paints a background and needs Ctrl+C; the
    # terminal's selection (Option/Shift-drag) still works with the mouse on.
    ALLOW_SELECT = False

    def __init__(  # noqa: PLR0915 - one place declares every layer's state
        self,
        client: WispClient,
        config: Config,
        startup: Startup | None = None,
        sessions: SessionStore | None = None,
        submissions: SubmissionStore | None = None,
        release_check: ReleaseCheck | None = None,
    ) -> None:
        # ansi_color=True disables the ANSI→truecolor filter, so
        # `background: ansi_default` reaches the terminal as SGR 49
        # ("terminal default background") instead of being rewritten to a
        # hardcoded theme color. The app then renders on the terminal's own
        # background, matching the user's terminal exactly.
        super().__init__(ansi_color=True)
        self.client = client
        self.config = config
        self.startup = startup or Startup()
        # None in tests: the only non-loopback request, made at startup.
        self._release_check = release_check
        self._welcome = Welcome(id="welcome")
        self._welcome.display = self.startup.mode == "new"
        self.sessions = sessions or SessionStore()
        self.submissions = submissions or SubmissionStore(client.url)
        self.composer = ComposerStore(client.url, self.submissions.root)
        self._prompt_history: tuple[str, ...] = ()
        self._composer_editor: PromptInput | None = None
        self._composer_available = False
        self._composer_dirty = False
        self._composer_draft = ""
        self._composer_submission_key: str | None = None
        self._composer_timer: Timer | None = None
        self._composer_errors: set[str] = set()
        self._recoverable_draft: ComposerState | None = None
        self._pending_state_error = False
        # Title for the session that the next first message will create.
        self._next_session_title = ""
        self._record_error_shown = False
        self.session_id: str | None = None
        self.session_title: str = ""
        self._model_selection: dict[str, Any] = {}
        self._last_run_tokens = ""
        try:
            self._launch_dir = display_path(Path.cwd())
        except OSError:
            self._launch_dir = "unavailable"
        self.active_run: str | None = None
        # Reserved synchronously on the UI thread, before starting any I/O.
        self._submitting = False
        self._pending_submission: PendingSubmission | None = None
        self._recovery_view_token: object | None = None
        self._retry_after_recovery = False
        self._pending_view_history: list[ChatMessage] | None = None
        self._session_change_in_progress = False
        self.last_seq: dict[str, int] = {}
        self.queued_approvals: deque[ApprovalPrompt] = deque()
        self._approval_modal: ApprovalModal | None = None
        self._approval_prompt_id: str | None = None
        self._approval_notes: dict[str, str] = {}
        self._approvals_sending: set[str] = set()
        self._approvals_answered: set[str] = set()
        # The token's capabilities do not change while this process runs.
        self._rule_decisions_refused = False
        self._follower: RunFollower | None = None
        self._disconnected = False
        self._cancel_when_acknowledged = False
        self._answer_text = ""
        # Offset in _answer_text where the current answer widget starts.
        self._answer_segment_start = 0
        self._replay_label_shown = False
        self._stream_view: StreamView | None = None
        self._reattached_checkpoint: list[ChatMessage] | None = None
        self._chat_epoch = 0
        # Live (not yet finished) items, in display order.
        self._chat_items: deque[ChatItem | ToolCallView] = deque()
        # Finished items, oldest first, and the widgets that show them.
        self._printed: deque[tuple[str, str]] = deque(maxlen=self.SHOWN_ITEMS)
        self._finished: deque[ChatItem] = deque()
        # Finished items dropped past SHOWN_ITEMS, and the note that counts them.
        self._dropped = 0
        self._dropped_note: ChatItem | None = None
        self._sync_queued = False
        self._tool_calls: dict[str, ToolCallView] = {}
        self._saw_live_tool_events = False
        self._tool_history_token: object | None = None
        self._tool_history_targets: tuple[ToolCallView, ...] = ()
        self._tool_history_again = False
        self._tool_history_post_run_reads = 0
        self._status_text = "connecting…"
        self._tab_status: TabStatus | None = None
        self._tab_status_closed = False
        self._activity_frame = 0
        # The footer's (left, right) rows, printed again below the chat on exit.
        self.footer_rows: list[tuple[str, str]] = []
        self._prompt_input = PromptInput(commands=tuple(name for name, _, _ in COMMANDS))
        self._activity_timer: Timer | None = None
        self._cancel_requested = False
        self._last_ctrl_c = -10.0

    def _run_threaded(
        self,
        work: Callable[[], None],
        group: str = "net",
        on_done: Callable[[], None] | None = None,
    ) -> None:
        def guarded() -> None:
            try:
                work()
            except ClientError as error:
                self.call_from_thread(self._set_status, f"error: {error}")
            except Exception:
                self.call_from_thread(self._set_status, "error: internal_error")
            finally:
                if on_done is not None:
                    self.call_from_thread(on_done)

        self.run_worker(guarded, thread=True, group=group, exclusive=False)

    def _set_status(self, text: str) -> None:
        self._status_text = safe_text(text, 200)
        self._render_status()

    def _render_status(self) -> None:
        # A worker's error callback can land after shutdown removed the footer.
        if not self._prompt_input.is_attached:
            return
        status = self.query_one("#status", Static)
        status.update(self._status_text)
        status.display = bool(self._status_text) and self._status_text != FRESH_STATUS
        self._render_activity()

    def on_resize(self, event: events.Resize) -> None:
        self.query_one("#bottom").set_class(event.size.width < 80, "narrow")

    def _update_tab_status(self, status: TabStatus | None) -> None:
        if self._tab_status_closed or status == self._tab_status:
            return
        self._tab_status = status
        driver = self._driver
        if driver is None or driver.is_headless:
            return
        # Do not gate on TERM_PROGRAM: SSH need not forward the Mac's environment.
        # Unsupported terminals ignore this OSC; it neither moves the cursor nor
        # paints the terminal. Use Textual's writer, never redirected stdout.
        try:
            driver.write(tab_status_sequence(status))
            driver.flush()
        except (OSError, ValueError):
            pass  # A closed terminal must not prevent local shutdown/recovery.

    def _clear_tab_status(self) -> None:
        self._update_tab_status(None)
        self._tab_status_closed = True

    def _idle_tab_status(self) -> TabStatus:
        if self._session_change_in_progress:
            return TabStatus.WORKING
        if self._pending_state_error or self._pending_submission is not None:
            return TabStatus.ATTENTION
        if self._status_text == "connecting…":
            return TabStatus.CONNECTING
        if self._status_text.startswith(
            (
                "runtime unreachable:",
                "runtime_not_ready",
                "error:",
                "run failed",
                "session not found",
            )
        ):
            return TabStatus.ERROR
        if self._status_text == "run cancelled":
            return TabStatus.CANCELLED
        return TabStatus.IDLE

    def _render_activity(self) -> None:
        editor = self._prompt_input
        # A queued tick may arrive after the screen's children are unmounted.
        if not editor.is_attached:
            if self._activity_timer is not None:
                self._activity_timer.pause()
            return
        busy = self._submitting or self.active_run is not None
        if not busy:
            self._update_tab_status(self._idle_tab_status())
            editor.border_title = ""
            self._activity_frame = 0
            if self._activity_timer is not None:
                self._activity_timer.pause()
            return
        label = "Working"
        tab_status = TabStatus.WORKING
        if self._disconnected:
            label = "Reconnecting"
            tab_status = TabStatus.RECONNECTING
        elif self._cancel_requested:
            label = "Cancelling"
            tab_status = TabStatus.CANCELLING
        elif self._approval_modal is not None or self._status_text.startswith("run parked"):
            label = "Waiting for approval"
            tab_status = TabStatus.WAITING
        self._update_tab_status(tab_status)
        editor.border_title = f" {SPINNER_FRAMES[self._activity_frame]} {label} "
        if self._activity_timer is not None:
            # Hold the frame while the terminal tab is in the background: ten
            # repaints a second there only slow the terminal's other tabs.
            if self.app_focus:
                self._activity_timer.resume()
            else:
                self._activity_timer.pause()

    def watch_app_focus(self) -> None:
        self._render_activity()

    def _tick_activity(self) -> None:
        self._activity_frame = (self._activity_frame + 1) % len(SPINNER_FRAMES)
        self._render_activity()

    def _metadata_lines(self) -> list[str]:
        session = self.session_id[:8] if self.session_id else "new"
        thinking = self._model_selection.get("thinking")
        thinking_label = (
            ("on" if thinking else "off") if isinstance(thinking, bool) else "unavailable"
        )
        return [
            f"session {session} · {model_label({'effectiveSelection': self._model_selection})}",
            f"thinking: {thinking_label} · context: unavailable",
            f"last run tokens: {self._last_run_tokens or 'unavailable'}",
            f"launch dir (client only): {metadata_text(self._launch_dir, 1000)}",
        ]

    def _render_metadata(self) -> None:
        if self.session_id is not None:
            self._welcome.display = False
        self.query_one("#session-id").display = self.session_id is not None
        session = self.session_id[:8] if self.session_id else "new"
        self.query_one("#location", Static).update(metadata_text(self._launch_dir, 1000))
        session_text = f" · session {session}"
        self.query_one("#session-id", Static).update(session_text)
        mode = metadata_text(self._model_selection.get("mode"))
        model = metadata_text(self._model_selection.get("model"))
        label = (
            f"model: {model}"
            if model
            else (f"mode: {mode} (auto)" if mode and mode != "model" else "")
        )
        model_info = self.query_one("#session-info", Static)
        model_info.update(label)
        model_info.display = bool(label)
        thinking = self._model_selection.get("thinking")
        thinking_info = self.query_one("#thinking-info", Static)
        separator = " · " if label else ""
        thinking_text = (
            f"{separator}thinking: {'on' if thinking else 'off'}"
            if isinstance(thinking, bool)
            else ""
        )
        thinking_info.update(thinking_text)
        thinking_info.display = isinstance(thinking, bool)
        self.query_one("#selection-summary").display = bool(label) or thinking_info.display
        usage = self.query_one("#usage-info", Static)
        # These are exact counts from the last finished run, never context use.
        compact = (
            self._last_run_tokens.replace("input: ", "↑")
            .replace("output: ", "↓")
            .replace("total: ", "Σ")
            .replace(", ", " ")
        )
        fallback = "T: 0" if self.session_id is None else "T: unavailable"
        usage_text = f"last run {compact}" if compact else fallback
        usage.update(usage_text)
        self.footer_rows = [
            (metadata_text(self._launch_dir, 1000), session_text if self.session_id else ""),
            (usage_text, label + thinking_text),
        ]

    def _selection_updated(self, session: dict[str, Any], session_id: str) -> None:
        if self.session_id != session_id:
            return
        selection = session.get("effectiveSelection")
        self._model_selection = dict(selection) if isinstance(selection, dict) else {}
        self._render_metadata()
