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

from rich.console import RenderableType
from textual import events
from textual._compositor import InlineUpdate
from textual.app import App
from textual.screen import Screen
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
from .scrollback import render_ansi
from .sessions import SessionStore
from .submissions import PendingSubmission, SubmissionStore
from .terminal import ChangedRowsUpdate, InlineRenderer
from .text import display_path, metadata_text, model_label, safe_text
from .tool_view import ToolCallView

SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

FRESH_STATUS = "ready · new session — your first message starts it (/resume, F2)"


class ApprovalPrompt(NamedTuple):
    """A confirmation prompt the runtime is waiting on, queued for the dialog."""

    run_id: str
    prompt_id: str
    tool_name: str
    args: Any


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
    # Textual writes this many blank rows above the inline app and moves back up
    # over them on exit, which would overwrite the last line printed to scrollback.
    INLINE_PADDING = 0
    # Printed items kept to redraw scrollback at a new width after a resize.
    REPRINT_ITEMS = 200

    def __init__(  # noqa: PLR0915 - one place declares every layer's state
        self,
        client: WispClient,
        config: Config,
        startup: Startup | None = None,
        sessions: SessionStore | None = None,
        submissions: SubmissionStore | None = None,
    ) -> None:
        # ansi_color=True disables the ANSI→truecolor filter, so
        # `background: ansi_default` reaches the terminal as SGR 49
        # ("terminal default background") instead of being rewritten to a
        # hardcoded theme color. The app then renders on the terminal's own
        # background, matching the user's terminal exactly.
        super().__init__(ansi_color=True)
        self._inline_renderer = InlineRenderer()
        self.client = client
        self.config = config
        self.startup = startup or Startup()
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
        # Live (not yet printed) items, in display order.
        self._chat_items: deque[ChatItem | ToolCallView] = deque()
        # Finished items waiting for the next inline frame to print them.
        self._scrollback: list[tuple[str, str]] = []
        self._printed: deque[tuple[str, str]] = deque(maxlen=self.REPRINT_ITEMS)
        self._printed_total = 0
        self._reprint = False
        self._sync_queued = False
        self._tool_calls: dict[str, ToolCallView] = {}
        self._saw_live_tool_events = False
        self._tool_history_token: object | None = None
        self._tool_history_targets: tuple[ToolCallView, ...] = ()
        self._tool_history_again = False
        self._status_text = "connecting…"
        self._activity_frame = 0
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
        status.display = bool(self._status_text)
        self._render_activity()

    def on_resize(self, event: events.Resize) -> None:
        # The inline driver clears the terminal on resize, even if its final
        # dimensions happen to match the previous ones.
        self._inline_renderer.invalidate()
        # That clear also erased the visible part of the printed chat.
        self._reprint = bool(self._printed)
        self.query_one("#bottom").set_class(event.size.width < 80, "narrow")

    def _display(self, screen: Screen[Any], renderable: RenderableType | None) -> None:
        if isinstance(renderable, InlineUpdate):
            renderable = ChangedRowsUpdate(
                renderable, self._inline_renderer, self.size.width, self._take_scrollback
            )
        super()._display(screen, renderable)

    def _take_scrollback(self) -> str:
        items, self._scrollback = self._scrollback, []
        prefix = ""
        if self._reprint:
            # Like Pi/Claude Code: redraw the printed chat at the new width
            # instead of leaving it half erased and wrapped at the old one.
            self._reprint = False
            prefix = "\x1b[H\x1b[2J\x1b[3J"
            items = list(self._printed)
            hidden = self._printed_total - len(items)
            if hidden:
                note = f"\u2191 {hidden} earlier item{'s' if hidden != 1 else ''} not reprinted"
                items.insert(0, ("system", note))
        if not items:
            return prefix
        return prefix + render_ansi(items, self.size.width, self.console.color_system)

    def _render_activity(self) -> None:
        editor = self._prompt_input
        # A queued tick may arrive after the screen's children are unmounted.
        if not editor.is_attached:
            if self._activity_timer is not None:
                self._activity_timer.pause()
            return
        busy = self._submitting or self.active_run is not None
        if not busy:
            editor.border_title = ""
            self._activity_frame = 0
            if self._activity_timer is not None:
                self._activity_timer.pause()
            return
        label = "Working"
        if self._disconnected:
            label = "Reconnecting"
        elif self._cancel_requested:
            label = "Cancelling"
        elif self._approval_modal is not None or self._status_text.startswith("run parked"):
            label = "Waiting for approval"
        editor.border_title = f" {SPINNER_FRAMES[self._activity_frame]} {label} "
        if self._activity_timer is not None:
            self._activity_timer.resume()

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
        session = self.session_id[:8] if self.session_id else "new"
        self.query_one("#location", Static).update(metadata_text(self._launch_dir, 1000))
        self.query_one("#session-id", Static).update(f" · session {session}")
        mode = metadata_text(self._model_selection.get("mode"))
        model = metadata_text(self._model_selection.get("model"))
        label = (
            f"model: {model or 'not reported'}"
            if mode == "model"
            else (f"mode: {mode} (auto)" if mode and mode != "model" else "model: runtime default")
        )
        self.query_one("#session-info", Static).update(label)
        thinking = self._model_selection.get("thinking")
        thinking_info = self.query_one("#thinking-info", Static)
        thinking_info.update(
            f" · thinking: {'on' if thinking else 'off'}" if isinstance(thinking, bool) else ""
        )
        thinking_info.display = isinstance(thinking, bool)
        usage = self.query_one("#usage-info", Static)
        # These are exact counts from the last finished run, never context use.
        compact = (
            self._last_run_tokens.replace("input: ", "↑")
            .replace("output: ", "↓")
            .replace("total: ", "Σ")
            .replace(", ", " ")
        )
        usage.update(f"last run {compact}" if compact else "")
        usage.display = bool(compact)

    def _selection_updated(self, session: dict[str, Any], session_id: str) -> None:
        if self.session_id != session_id:
            return
        selection = session.get("effectiveSelection")
        self._model_selection = dict(selection) if isinstance(selection, dict) else {}
        self._render_metadata()
