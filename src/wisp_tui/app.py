"""Textual chat application for the local Wisp runtime.

The app is a thin, private client: your messages go to the Wisp runtime on
loopback, and its answers are rendered locally. No other model or service is
in the path. Worker threads perform all network I/O and marshal results into
the UI thread via ``call_from_thread``.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from rich.console import RenderableType
from textual import events
from textual._compositor import InlineUpdate
from textual.app import App, ComposeResult
from textual.await_remove import AwaitRemove
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.timer import Timer
from textual.widget import AwaitMount, Widget
from textual.widgets import Collapsible, ListItem, ListView, Markdown, Static, TextArea

from .client import ClientError, WispClient, TERMINAL_RUN_STATUSES, safe_code
from .chat_view import ChatScroll, ResumeFollow
from .config import Config
from .composer import ComposerError, ComposerState, ComposerStore, bounded_history
from .editor import PromptInput
from .history import ChatMessage, HistoryError, checkpoint_covers_answer, parse_history, tool_history_results
from .runs import RunFollower, find_active_run
from .sessions import SessionStore
from .submissions import MAX_DRAFT_BYTES, PendingSubmission, SubmissionError, SubmissionStore
from .terminal import ChangedRowsUpdate, InlineRenderer
from .tool_data import detail_text
from .tool_view import ToolCallView

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# A session ID or a prefix of one (as shown in pickers), for --resume / /resume.
RESUME_QUERY_RE = re.compile(r"[0-9a-f][0-9a-f-]{3,35}")

HINTS = "Enter send · Ctrl+J newline · F1 help · F2 sessions · F3 model · Ctrl+C cancel/exit"
SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

HELP_TEXT = """**wisp-tui** — chat with the local Wisp runtime.

| Key | Action |
|---|---|
| Enter | Send the whole prompt |
| Ctrl+J | Insert a newline (SSH-safe) |
| Shift+Enter | Insert a newline if your terminal distinguishes it |
| Ctrl+P / Ctrl+N | Previous / next submitted prompt; return to your draft |
| Ctrl+Shift+P | Command palette, where the terminal distinguishes it |
| F1 | This help |
| F2 | Session picker |
| F3 | Model / mode picker |
| F4 | Resume following the latest output |
| Ctrl+C | Cancel the active run — press twice to leave |

Bracketed paste stays literal in the editor until you press Enter. Slash
commands execute only when the entire input is a single line; multiline text
starting with `/` is a prompt, not a sequence of commands. Ctrl+J works over
ordinary SSH; Shift+Enter may arrive as plain Enter in some terminals.

**Commands:** `/new [title]`, `/resume`, `/sessions`, `/model`, `/cancel`, `/retry`, `/reconnect`, `/status`, `/clear`, `/help`, `/quit`.

**Sessions:** every launch starts fresh; your first message creates the Wisp
session. `/new` starts another one. Sessions you use here are saved locally
(`~/.local/state/wisp-tui/sessions/`): `/resume` (or `wisp-tui -r`) picks one,
`/resume 3bf1ba87` (or `wisp-tui -r 3bf1ba87`) opens it by ID prefix, and
`wisp-tui -c` continues the most recent. `/sessions` (F2) lists all Wisp
sessions, including Telegram/desktop ones.

`/retry` retries an unacknowledged submission with its original input and request
ID. It never sends your newer draft. Pending requests are saved privately before
submission and recovered after exit, without automatic resubmission. A pending
request state error blocks new work until resolved.

Ctrl+P/Ctrl+N browse your last 50 acknowledged prompts, preserving the current
draft and edits made while browsing. Up/Down still move within multiline text.
One unsent draft is saved locally after a short pause and on exit, up to 128 KiB.
A fresh draft restores on a fresh launch; a session draft restores when you
resume that session. Starting new text replaces the one saved draft. Nothing
is sent automatically. Input history/recovery errors leave editing available.

A finished run is one answered prompt, not the end of the conversation: just
send your next message. The footer keeps the session, model/mode, thinking
setting and last finished run's reported tokens visible (↑ input, ↓ output,
Σ total). Missing stats are hidden; `/status` explains what is not reported.
Tokens update when a run finishes, not live; context occupancy is unavailable.
The launch directory belongs to this client, not Wisp's tools.

Tool entries stay above the current answer. Click a tool title to expand or
collapse its details, or Tab to the title and press Enter. Live inputs may be
sanitized; outputs load from stored session history when a unique call ID is
available. Long details scroll inside the expanded entry. Collapse/expand
retries a missing result. Opening details never executes or approves a tool.

Scroll up in the transcript to pause automatic following while output continues.
Click the paused-follow hint or press F4 to jump to the latest output and resume.
Tab to the transcript for Up/PageUp/Home navigation; End, or scrolling back
to the bottom, also resumes following.
Editor and expanded tool-body scrolling stay independent. Opening a tool with
Tab pauses following so new output will not pull you away from its details.

While a run is active you will see tool calls as they happen. When Wisp needs
approval for a tool, a dialog appears: **y** approve, **n** deny, **p** allow
for this project, **g** allow globally, **Esc** deny. Use Up/Down or PageUp/PageDown
to scroll long approval details; decision keys stay visible. Ctrl+C still
cancels work from approvals and pickers; press twice to leave. Normal questions consume
the Wisp account balance, exactly like Telegram/Desk sessions.
"""


def _safe_text(value: Any, limit: int = 4000) -> str:
    if not isinstance(value, str):
        return ""
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _metadata_text(value: Any, limit: int = 100) -> str:
    """Bound metadata and keep control characters from creating fake UI lines."""
    text = _safe_text(value, limit)
    return "".join(char if char.isprintable() else "�" for char in text)


def _pretty_args(args: Any) -> str:
    try:
        rendered = json.dumps(args, indent=2, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        rendered = repr(args)
    return _safe_text(rendered)


FRESH_STATUS = "ready · new session — your first message starts it (/resume, F2)"


@dataclass(frozen=True)
class Startup:
    """How the app starts: a fresh session (default) or an existing one."""
    mode: str = "new"  # "new" | "continue" | "resume" | "session"
    # "session": a full UUID. "resume": optional ID or prefix ("" = picker).
    session_id: str = ""


def _title_from(draft: str, limit: int = 60) -> str:
    """Session title from the first non-empty line of the first prompt."""
    line = next((line.strip() for line in draft.splitlines() if line.strip()), "")
    return line if len(line) <= limit else line[: limit - 1] + "…"


class StreamView:
    """Render one streamed answer incrementally, one Markdown operation at a time.

    Re-rendering the whole answer per delta is quadratic. Instead, a single
    async renderer appends only the new suffix (coalescing deltas that arrive
    meanwhile, at most ten batches per second) and falls back to a full update
    when the text is replaced (``gap``/final output that is not a continuation).
    It waits for the widget's mount, so no delta can be lost to Markdown's
    initial update.
    """

    UPDATE_INTERVAL = 0.1

    def __init__(self, app: App[Any], widget: Markdown, mounted: AwaitMount,
                 on_rendered: Callable[[], None]) -> None:
        self.app = app
        self.widget = widget
        self._mounted = mounted
        self._on_rendered = on_rendered
        self.target = ""
        self.rendered = ""
        self._running = False
        self._flush_now = asyncio.Event()

    def set_text(self, text: str) -> None:
        self.target = text
        if not self._running and self.rendered != text:
            self._running = True
            self.app.run_worker(self._render(), group="render", exclusive=False)

    def flush(self) -> None:
        """Finish the latest answer without waiting for the next display batch."""
        if self._running or self.rendered != self.target:
            self._flush_now.set()

    async def _render(self) -> None:
        try:
            await self._mounted
            while self.rendered != self.target and self.widget.is_attached:
                try:
                    await asyncio.wait_for(self._flush_now.wait(), timeout=self.UPDATE_INTERVAL)
                except TimeoutError:
                    pass
                self._flush_now.clear()
                target = self.target
                if target.startswith(self.rendered):
                    await self.widget.append(target[len(self.rendered):])
                else:
                    await self.widget.update(target)
                self.rendered = target
                self._on_rendered()
        except Exception:
            # A widget removed mid-render (/clear, session switch) must not
            # take the app down; the run state lives elsewhere.
            pass
        finally:
            self._running = False


class ApprovalModal(ModalScreen[str | None]):
    """Ask the owner to resolve a parked run's confirmation prompt."""

    # Decisions belong to this screen even when the details viewport has focus.
    can_focus = True

    BINDINGS = [
        ("y", "approve", "Approve"),
        ("n", "deny", "Deny"),
        ("p", "allow_project", "Allow for project"),
        ("g", "allow_global", "Allow globally"),
        ("escape", "deny", "Deny"),
        ("ctrl+c", "app.cancel", "Cancel/exit"),
    ]

    def __init__(self, tool: str, args: Any, note: str = "") -> None:
        super().__init__()
        self.tool = tool
        self.args = args
        self.note = note

    def compose(self) -> ComposeResult:
        with Vertical(id="approval-dialog"):
            # Approval details must be shown exactly as the runtime sent them:
            # Textual markup would hide/alter bracketed text (or crash).
            yield Static("Wisp asks for approval", id="approval-title", markup=False)
            with VerticalScroll(id="approval-details"):
                yield Static(f"Tool: {self.tool}", id="approval-tool", markup=False)
                if self.note:
                    yield Static(self.note, id="approval-note", markup=False)
                yield Static(_pretty_args(self.args), id="approval-args", markup=False)
            yield Static("↑↓/PgUp/PgDn scroll · y approve · n/Esc deny · "
                         "p allow for project · g allow globally",
                         id="approval-hint", markup=False)

    def on_mount(self) -> None:
        self.query_one("#approval-details", VerticalScroll).focus()

    def action_approve(self) -> None:
        self.dismiss("approve")

    def action_deny(self) -> None:
        self.dismiss("deny")

    def action_allow_project(self) -> None:
        self.dismiss("allow_project")

    def action_allow_global(self) -> None:
        self.dismiss("allow_global")


def server_session_label(session: dict[str, Any]) -> str:
    sid = str(session.get("id", ""))
    title = _safe_text(session.get("title"), 60) or "(untitled)"
    origin = session.get("origin", "?")
    count = session.get("messageCount", "?")
    updated = _safe_text(session.get("updatedAt"), 20).replace("T", " ")
    return f"{title}  ·  {origin}  ·  {count} msg  ·  {updated}  ·  {sid[:8]}"


class SessionPicker(ModalScreen[str | None]):
    """Pick a session from ``(session id, label)`` entries."""

    BINDINGS = [("escape", "close", "Close"), ("ctrl+c", "app.cancel", "Cancel/exit")]

    def __init__(self, entries: list[tuple[str, str]],
                 title: str = "Sessions (newest first)") -> None:
        super().__init__()
        self.entries = entries
        self.heading = title

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-dialog"):
            yield Static(self.heading, id="picker-title", markup=False)
            items = [ListItem(Static(label, markup=False)) for _, label in self.entries]
            yield ListView(*items, id="session-list")

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        index = event.list_view.index if event.list_view.index is not None else -1
        if 0 <= index < len(self.entries):
            self.dismiss(self.entries[index][0])
        else:
            self.dismiss("")

    def action_close(self) -> None:
        self.dismiss(None)


class ModelPicker(ModalScreen[dict[str, Any] | None]):
    """Pick the model selection for the current session."""

    BINDINGS = [("escape", "close", "Close"), ("ctrl+c", "app.cancel", "Cancel/exit")]

    def __init__(self, models: list[dict[str, Any]], current: dict[str, Any] | None) -> None:
        super().__init__()
        self.options: list[dict[str, Any]] = [
            {"mode": "advanced", "thinking": False},
            {"mode": "balanced", "thinking": False},
        ]
        for model in models:
            model_id = model.get("id")
            if isinstance(model_id, str):
                reasoning = model.get("reasoning") is True
                self.options.append(
                    {"mode": "model", "model": model_id, "thinking": reasoning})
        self.current = current or {}

    def _label(self, option: dict[str, Any]) -> str:
        if option.get("mode") == "model":
            name = str(option.get("model"))
            suffix = " · reasoning" if option.get("thinking") else ""
            return f"Model: {name}{suffix}"
        return f"Mode: {option.get('mode')}"

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-dialog"):
            yield Static("Model selection", id="picker-title", markup=False)
            items = []
            for option in self.options:
                marker = "● " if self._is_current(option) else "  "
                items.append(ListItem(Static(marker + self._label(option), markup=False)))
            yield ListView(*items, id="model-list")

    def _is_current(self, option: dict[str, Any]) -> bool:
        if option.get("mode") == "model":
            return (self.current.get("mode") == "model"
                    and self.current.get("model") == option.get("model"))
        return (self.current.get("mode") == option.get("mode")
                and self.current.get("mode") in ("advanced", "balanced"))

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        index = event.list_view.index if event.list_view.index is not None else -1
        if 0 <= index < len(self.options):
            self.dismiss(self.options[index])
        else:
            self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)


class WispTuiApp(App[None]):
    TITLE = "wisp-tui"
    # Free the SSH-safe Ctrl+P byte for prompt recall; keep the palette available.
    COMMAND_PALETTE_BINDING = "ctrl+shift+p"
    CSS = """
    App { background: ansi_default; }
    Screen { background: transparent; color: ansi_default; }
    ApprovalModal, SessionPicker, ModelPicker { background: transparent; }
    #chat { padding: 0; scrollbar-size: 0 0; }
    #chat > Static { margin: 0 1 1 1; }
    /* Every Markdown block already ends with a 1-row bottom margin, which
       provides the gap after an answer. Avoid structural pseudo-classes
       (first/last/nth child) here: they make Textual restyle the whole
       answer on each streamed block (see test_rendering). */
    #chat > Markdown { margin: 0 1; }
    #chat Markdown { padding: 0; color: ansi_default; }
    #chat MarkdownBlock { background: transparent; color: ansi_default; }
    #chat MarkdownHeader {
        content-align: left middle;
        text-style: bold;
        margin: 0 0 1 0;
    }
    #chat MarkdownBullet, #chat MarkdownTableContent > .header { color: ansi_default; }
    #chat MarkdownFence { margin: 1 0; }
    #chat MarkdownFence > Label { padding: 0; }
    #chat MarkdownBlock > .code_inline {
        background: transparent;
        color: ansi_default;
        text-style: bold;
    }
    /* Deliberate, owner-requested exception to the unpainted background:
       prompt blocks only use a muted dark teal (palette cyan was too bright).
       #005f5f is exactly xterm-256 entry 23, so 256-color and truecolor
       terminals show the same shade; the fixed light-grey text keeps contrast
       on light and dark themes. Everything else stays transparent. */
    #chat > .user {
        width: 100%;
        margin: 0 0 1 0;
        border: none;
        background: #005f5f;
        color: #dadada;
        text-style: bold;
        padding: 1 2;
    }
    .system, .tool { color: ansi_default; text-style: dim; }
    .error { color: $error; }
    #chat > ToolCallView {
        height: auto;
        margin: 0 1 1 1;
        padding: 0;
        border: none;
        background: transparent;
        background-tint: transparent;
        color: ansi_default;
    }
    #chat ToolCallView > Contents { padding: 0; background: transparent; }
    #chat ToolCallView CollapsibleTitle,
    #chat ToolCallView CollapsibleTitle:hover,
    #chat ToolCallView CollapsibleTitle:focus {
        padding: 0;
        max-width: 100%;
        background: transparent;
        background-tint: transparent;
        color: ansi_default;
        text-style: none;
    }
    #chat ToolCallView CollapsibleTitle:focus { text-style: underline; }
    #chat .tool-detail-scroll {
        height: auto;
        max-height: 16;
        padding: 0 0 0 2;
        background: transparent;
        scrollbar-size: 0 0;
    }
    #chat .tool-details { height: auto; background: transparent; color: ansi_default; }
    #bottom { dock: bottom; height: auto; padding: 0 1; }
    #input {
        height: auto;
        min-height: 3;
        max-height: 8;
        scrollbar-size: 0 0;
        border-top: solid ansi_bright_blue;
        border-bottom: solid ansi_bright_blue;
        border-left: none;
        border-right: none;
        background: transparent;
        color: ansi_default;
        padding: 0;
    }
    #input:focus { background-tint: transparent; }
    #activity { display: none; }
    #resume-follow {
        display: none; height: 1; background: transparent; color: ansi_default;
        text-style: underline;
    }
    #activity, #status, #session-info, #thinking-info, #usage-info, #location, #session-id {
        height: 1;
        background: transparent;
        color: ansi_default;
        text-style: none;
        padding: 0;
    }
    #location-row { height: 1; }
    #location { width: 1fr; text-wrap: nowrap; text-overflow: ellipsis; }
    #session-id, #thinking-info { width: auto; }
    #summary, #selection-summary { height: auto; }
    #selection-summary { width: 1fr; }
    #session-info, #usage-info { width: 1fr; height: auto; max-height: 2; }
    #session-info { text-align: right; }
    #usage-info { color: ansi_bright_cyan; }
    #status { height: auto; max-height: 2; }
    #bottom.narrow #summary { layout: vertical; }
    #bottom.narrow #session-info { text-align: left; }
    #approval-dialog {
        align: center middle; width: 80%; max-height: 80%;
        border: round $accent; padding: 0 1;
    }
    #approval-title { text-style: bold; color: $accent; margin-bottom: 1; }
    #approval-details {
        height: 1fr; min-height: 1; background: transparent; scrollbar-size: 0 0;
    }
    #approval-tool { text-style: bold; }
    #approval-args { margin: 1 0; }
    #approval-hint { color: $text-muted; }
    #picker-dialog {
        align: center middle; width: 90%; height: 80%;
        border: round $accent; padding: 1 2;
    }
    #picker-title { text-style: bold; margin-bottom: 1; }
    #picker-dialog ListView, #picker-dialog ListView:focus {
        height: 1fr; background: transparent; background-tint: transparent;
        color: ansi_default; scrollbar-size: 0 0;
    }
    #picker-dialog ListItem, #picker-dialog ListItem.-hovered,
    #picker-dialog ListItem.-highlight, #picker-dialog ListView:focus > ListItem.-highlight {
        background: transparent; background-tint: transparent; color: ansi_default;
        text-style: none;
    }
    #picker-dialog ListItem.-hovered { text-style: underline; }
    #picker-dialog ListItem.-highlight,
    #picker-dialog ListView:focus > ListItem.-highlight { text-style: bold underline; }
    """

    # Layout cost grows with every mounted chat widget (measured: streaming is
    # ~4x slower after 50 exchanges, ~45x after 500). Keep only the newest
    # items on screen (several screens' worth); Wisp keeps the full session
    # history server-side. 500-exchange session: load 22s -> ~1s.
    MAX_CHAT_ITEMS = 60

    BINDINGS = [
        ("f1", "help", "Help"),
        ("f2", "sessions", "Sessions"),
        ("f3", "model", "Model"),
        ("f4", "resume_follow", "Resume follow"),
        ("ctrl+c", "cancel", "Cancel/exit"),
    ]

    def __init__(self, client: WispClient, config: Config,
                 startup: Startup | None = None,
                 sessions: SessionStore | None = None,
                 submissions: SubmissionStore | None = None) -> None:
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
        self._fresh_title = ""
        self._record_error_shown = False
        self.session_id: str | None = None
        self.session_title: str = ""
        self._selection: dict[str, Any] = {}
        self._last_usage = ""
        try:
            self._launch_dir = self._display_path(Path.cwd())
        except OSError:
            self._launch_dir = "unavailable"
        self.active_run: str | None = None
        # Reserved synchronously on the UI thread, before starting any I/O.
        self._submitting = False
        self._pending_submission: PendingSubmission | None = None
        self._recovery_view_token: object | None = None
        self._retry_after_recovery = False
        self._pending_view_history: list[ChatMessage] | None = None
        self._session_busy = False
        self.last_seq: dict[str, int] = {}
        self.pending_prompts: deque[tuple[str, str, str, Any]] = deque()
        self._approval_modal: ApprovalModal | None = None
        self._approval_prompt_id: str | None = None
        self._approval_notes: dict[str, str] = {}
        self._approval_inflight: set[str] = set()
        self._answered_prompts: set[str] = set()
        self._follower: RunFollower | None = None
        self._disconnected = False
        self._cancel_after_submit = False
        self._delta_text = ""
        self._stream_view: StreamView | None = None
        self._reattach_history: list[ChatMessage] | None = None
        self._history_refresh_token: object | None = None
        self._chat_epoch = 0
        self._chat_items: deque[Widget] = deque()
        self._tool_calls: dict[str, ToolCallView] = {}
        self._saw_live_tool_events = False
        self._tool_history_token: object | None = None
        self._tool_history_targets: tuple[ToolCallView, ...] = ()
        self._tool_history_again = False
        self._trimmed_count = 0
        self._trim_marker: Static | None = None
        self._status_state = "connecting…"
        self._activity_frame = 0
        self._activity_indicator = Static("", id="activity", markup=False)
        self._activity_timer: Timer | None = None
        self._cancel_requested = False
        self._last_ctrl_c = -10.0

    # -- layout ---------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield ChatScroll(id="chat")
        with Container(id="bottom"):
            yield ResumeFollow()
            yield self._activity_indicator
            yield PromptInput()
            with Horizontal(id="location-row"):
                yield Static("", id="location", markup=False)
                yield Static("", id="session-id", markup=False)
            with Horizontal(id="summary"):
                yield Static("", id="usage-info", markup=False)
                with Horizontal(id="selection-summary"):
                    yield Static("", id="session-info", markup=False)
                    yield Static("", id="thinking-info", markup=False)
            yield Static("", id="status", markup=False)

    def on_mount(self) -> None:
        self._activity_timer = self.set_interval(0.1, self._tick_activity, pause=True)
        self._render_status()
        self._render_metadata()
        self._restore_pending()
        self._restore_composer()
        self.query_one("#input", PromptInput).focus()
        self._run_threaded(self._bootstrap_worker)

    # -- thread helpers ---------------------------------------------------------

    def _run_threaded(self, work: Callable[[], None], group: str = "net",
                      on_done: Callable[[], None] | None = None) -> None:
        def guarded() -> None:
            try:
                work()
            except ClientError as error:
                self.call_from_thread(self._status, f"error: {error}")
            except Exception:
                self.call_from_thread(self._status, "error: internal_error")
            finally:
                if on_done is not None:
                    self.call_from_thread(on_done)

        self.run_worker(guarded, thread=True, group=group, exclusive=False)

    def _bootstrap_worker(self) -> None:
        try:
            health = self.client.request("GET", "/v1/health", timeout=5.0)
        except ClientError as error:
            self.call_from_thread(self._status, f"runtime unreachable: {error}")
            self.call_from_thread(self._sysline, "Cannot reach the Wisp runtime. Start it and rerun wisp-tui.")
            return
        status = health.get("status") if isinstance(health, dict) else None
        if status != "ready":
            self.call_from_thread(self._status, "runtime_not_ready")
            return
        self.call_from_thread(self._bootstrap_complete)

    def _bootstrap_complete(self) -> None:
        # A slow health check must not replace a session the user just opened.
        if self.session_id is not None or self._session_busy or self._pending_state_error:
            return
        self._status(FRESH_STATUS)
        mode = self.startup.mode
        if mode == "session" and UUID_RE.fullmatch(self.startup.session_id):
            self._open_session(self.startup.session_id)
        elif mode == "continue":
            latest = self.sessions.latest()
            if latest:
                self._open_session(latest)
            else:
                self._sysline("No saved sessions yet — starting a new one.")
        elif mode == "resume":
            self.action_resume(self.startup.session_id)

    # -- chat rendering (main thread only) --------------------------------------

    def _status(self, text: str) -> None:
        self._status_state = _safe_text(text, 200)
        self._render_status()

    def _render_status(self, width: int | None = None) -> None:
        width = self.size.width if width is None else width
        hints = HINTS if width >= 80 else "Enter send · Ctrl+J ↵ · F1 help"
        line = f"{self._status_state} · {hints}" if self._status_state else hints
        self.query_one("#status", Static).update(_safe_text(line, 250))
        self._render_activity()

    def on_resize(self, event: events.Resize) -> None:
        # The inline driver clears the terminal on resize, even if its final
        # dimensions happen to match the previous ones.
        self._inline_renderer.invalidate()
        self.query_one("#bottom").set_class(event.size.width < 80, "narrow")
        self._render_status(event.size.width)

    def _display(self, screen: Screen[Any], renderable: RenderableType | None) -> None:
        if isinstance(renderable, InlineUpdate):
            renderable = ChangedRowsUpdate(renderable, self._inline_renderer, self.size.width)
        super()._display(screen, renderable)

    def _render_activity(self) -> None:
        indicator = self._activity_indicator
        # A queued tick may arrive after the screen's children are unmounted.
        if not indicator.is_attached:
            if self._activity_timer is not None:
                self._activity_timer.pause()
            return
        busy = self._submitting or self.active_run is not None
        indicator.display = busy
        if not busy:
            indicator.update("")
            self._activity_frame = 0
            if self._activity_timer is not None:
                self._activity_timer.pause()
            return
        label = "Working"
        if self._disconnected:
            label = "Reconnecting"
        elif self._cancel_requested:
            label = "Cancelling"
        elif self._approval_modal is not None or self._status_state.startswith("run parked"):
            label = "Waiting for approval"
        indicator.update(f"{SPINNER_FRAMES[self._activity_frame]} {label}")
        if self._activity_timer is not None:
            self._activity_timer.resume()

    def _tick_activity(self) -> None:
        self._activity_frame = (self._activity_frame + 1) % len(SPINNER_FRAMES)
        self._render_activity()

    def _metadata_lines(self) -> list[str]:
        session = self.session_id[:8] if self.session_id else "new"
        thinking = self._selection.get("thinking")
        thinking_label = ("on" if thinking else "off") if isinstance(thinking, bool) else "unavailable"
        return [
            f"session {session} · {self._model_label({'effectiveSelection': self._selection})}",
            f"thinking: {thinking_label} · context: unavailable",
            f"last run tokens: {self._last_usage or 'unavailable'}",
            f"launch dir (client only): {_metadata_text(self._launch_dir, 1000)}",
        ]

    def _render_metadata(self) -> None:
        session = self.session_id[:8] if self.session_id else "new"
        self.query_one("#location", Static).update(
            f"client: {_metadata_text(self._launch_dir, 1000)}")
        self.query_one("#session-id", Static).update(f" · session {session}")
        mode = _metadata_text(self._selection.get("mode"))
        model = _metadata_text(self._selection.get("model"))
        label = f"model: {model or 'not reported'}" if mode == "model" else (
            f"mode: {mode} (auto)" if mode and mode != "model" else "model: runtime default")
        self.query_one("#session-info", Static).update(label)
        thinking = self._selection.get("thinking")
        thinking_info = self.query_one("#thinking-info", Static)
        thinking_info.update(f" · thinking: {'on' if thinking else 'off'}"
                             if isinstance(thinking, bool) else "")
        thinking_info.display = isinstance(thinking, bool)
        usage = self.query_one("#usage-info", Static)
        # These are exact counts from the last finished run, never context use.
        compact = self._last_usage.replace("input: ", "↑").replace("output: ", "↓").replace(
            "total: ", "Σ").replace(", ", " ")
        usage.update(f"last run {compact}" if compact else "")
        usage.display = bool(compact)

    def _selection_updated(self, session: dict[str, Any], session_id: str) -> None:
        if self.session_id != session_id:
            return
        selection = session.get("effectiveSelection")
        self._selection = dict(selection) if isinstance(selection, dict) else {}
        self._render_metadata()

    def _scroll_down(self) -> None:
        self.query_one("#chat", ChatScroll).follow_latest()

    def on_chat_scroll_follow_changed(self, event: ChatScroll.FollowChanged) -> None:
        self.query_one("#resume-follow", ResumeFollow).display = not self.query_one(
            "#chat", ChatScroll).following

    def on_resume_follow_requested(self, event: ResumeFollow.Requested) -> None:
        self.action_resume_follow()

    def action_resume_follow(self) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        self.query_one("#chat", ChatScroll).resume_follow()

    def _mount_chat(self, widget: Widget, before: Widget | None = None) -> AwaitMount:
        """Mount in display order, dropping the oldest beyond MAX_CHAT_ITEMS."""
        self.query_one("#chat", ChatScroll).preserve_reading_position()
        if before is not None and before in self._chat_items:
            mounted = self.query_one("#chat", VerticalScroll).mount(widget, before=before)
            self._chat_items.insert(self._chat_items.index(before), widget)
        else:
            mounted = self.query_one("#chat", VerticalScroll).mount(widget)
            self._chat_items.append(widget)
        removed = self._trim_chat()
        self.query_one("#chat", ChatScroll).restore_after_update(mounted, *removed)
        return mounted

    def _trim_chat(self) -> list[AwaitRemove]:
        streaming = self._stream_view.widget if self._stream_view is not None else None
        removed = 0
        removals: list[AwaitRemove] = []
        while len(self._chat_items) > self.MAX_CHAT_ITEMS:
            oldest = self._chat_items.popleft()
            if oldest is streaming:
                # Never drop the answer that is still being written.
                second = self._chat_items.popleft()
                self._chat_items.appendleft(oldest)
                oldest = second
            removals.append(oldest.remove())
            self._tool_calls = {key: view for key, view in self._tool_calls.items()
                                if view is not oldest}
            removed += 1
        if removed:
            self._trimmed_count += removed
            self._update_trim_marker()
        return removals

    def _update_trim_marker(self) -> None:
        count = self._trimmed_count
        text = (f"\u2191 {count} earlier item{'s' if count != 1 else ''} not shown "
                "\u2014 Wisp keeps the full session history.")
        # Reuse a marker whose mount is still pending, too, so rapid replay
        # updates cannot create several markers before the first is mounted.
        if self._trim_marker is not None:
            self._trim_marker.update(text)
            return
        # remove_children() is asynchronous; a prior marker may still be in
        # the DOM during a reset. A class avoids colliding fixed widget IDs.
        marker = Static(text, classes="system chat-trimmed", markup=False)
        self._trim_marker = marker
        chat = self.query_one("#chat", VerticalScroll)
        if self._chat_items:
            chat.mount(marker, before=self._chat_items[0])
        else:
            chat.mount(marker)

    def _reset_chat(self, hidden: int = 0) -> None:
        """Remove every chat item; optionally note ``hidden`` older items."""
        self._chat_epoch += 1
        self._reattach_history = None
        self._pending_view_history = None
        self.query_one("#chat", ChatScroll).resume_follow()
        self.query_one("#chat", VerticalScroll).remove_children()
        self._chat_items.clear()
        self._tool_calls.clear()
        self._tool_history_token = None
        self._tool_history_targets = ()
        self._tool_history_again = False
        # A still-running answer continues in a fresh widget (full text so far).
        self._stream_view = None
        self._trim_marker = None
        self._trimmed_count = hidden
        if hidden:
            self._update_trim_marker()

    def _sysline(self, text: str, cls: str = "system") -> None:
        self._mount_chat(Static(_safe_text(text), classes=cls, markup=False))
        self._scroll_down()

    def _tool_event(self, event: dict[str, Any], *, completed: bool,
                    input_text: str | None = None, historical: bool = False,
                    terminal: bool = False) -> ToolCallView:
        call_id = event.get("toolCallId")
        key = call_id if isinstance(call_id, str) and 0 < len(call_id) <= 200 else ""
        if not historical and not terminal:
            self._saw_live_tool_events = True
        view = self._tool_calls.get(key) if key else None
        self.query_one("#chat", ChatScroll).preserve_reading_position()
        if view is not None:
            view.update_event(event, completed=completed)
            self.query_one("#chat", ChatScroll).restore_after_update()
        else:
            view = ToolCallView(event, completed=completed, historical=historical)
            view.session_id = self.session_id
            if terminal:
                view.terminal_summary = True
                view.update_event({}, completed=completed)
            before = self._stream_view.widget if self._stream_view is not None else None
            self._mount_chat(view, before=before)
            if key and view in self._chat_items:
                self._tool_calls[key] = view
        if input_text is not None:
            # Checkpoint arguments have already been bounded/formatted.
            view.apply_history(input_text=input_text)
        self._scroll_down()
        if completed and not view.collapsed:
            self._request_tool_history()
        return view

    def _add_message(self, message: ChatMessage) -> None:
        if message.role == "assistant":
            if message.text.strip():
                self._mount_chat(Markdown(message.text, classes="assistant"))
            for call in message.tool_calls:
                self._tool_event({"name": call.name, "toolCallId": call.call_id},
                                 completed=False, input_text=call.input_text, historical=True)
        elif message.role == "tool":
            view = self._tool_event({"name": message.tool_name, "toolCallId": message.tool_call_id},
                                    completed=True, historical=True)
            view.apply_history(output_text=detail_text(message.text), status=message.tool_status,
                               pruned=message.tool_output_pruned)
        else:
            if message.role == "user":
                self._tool_calls.clear()
            self._mount_chat(Static(message.text, classes=message.role, markup=False))
        self._scroll_down()

    def _tool_targets(self) -> tuple[ToolCallView, ...]:
        return tuple(view for view in self._chat_items if isinstance(view, ToolCallView)
                     and view.session_id == self.session_id and view.call_id
                     and view.output_text is None)

    def on_collapsible_expanded(self, event: Collapsible.Expanded) -> None:
        if (isinstance(event.collapsible, ToolCallView) and event.collapsible.call_id
                and event.collapsible.output_text is None):
            self._request_tool_history(force_after_current=True)

    def _apply_tool_history(self, messages: list[ChatMessage], targets: tuple[ToolCallView, ...]) -> None:
        chat = self.query_one("#chat", ChatScroll)
        if targets:
            chat.preserve_reading_position()
        matched, ambiguous = tool_history_results(messages, {view.call_id for view in targets})
        for view in targets:
            if (view not in self._chat_items or view.session_id != self.session_id
                    or view.output_text is not None):
                continue
            if view.call_id in ambiguous:
                view.set_result_notice("Stored call ID is ambiguous; result not matched.")
            elif view.call_id in matched:
                details = matched[view.call_id]
                view.apply_history(details.input_text, details.output_text,
                                   details.status, details.pruned)
            else:
                view.set_result_notice("Result not in stored history yet. Collapse and expand to retry.")
        if targets:
            chat.restore_after_update()

    def _request_tool_history(self, *, force_after_current: bool = False) -> None:
        targets = self._tool_targets()
        session_id = self.session_id
        if not targets or session_id is None or self._history_refresh_token is not None:
            return
        if self._tool_history_token is not None:
            self._tool_history_again |= force_after_current or any(
                view not in self._tool_history_targets for view in targets if not view.collapsed)
            return
        token = object()
        epoch = self._chat_epoch
        self._tool_history_token = token
        self._tool_history_targets = targets
        for view in targets:
            view.set_result_notice("Loading stored result…")

        def work() -> None:
            try:
                payload = self.client.request("GET", f"/v1/sessions/{session_id}/history", timeout=5.0)
                messages = parse_history(payload)
            except ClientError as error:
                code = safe_code(str(error), "history_unavailable")
            except HistoryError:
                code = "unexpected_history_shape"
            except Exception:
                code = "history_unavailable"
            else:
                self.call_from_thread(self._tool_history_loaded, token, session_id, epoch,
                                      targets, messages)
                return
            self.call_from_thread(self._tool_history_failed, token, session_id, epoch, targets, code)

        self._run_threaded(work, group="tool-history",
                           on_done=lambda: self._tool_history_done(token))

    def _tool_history_current(self, token: object, session_id: str, epoch: int) -> bool:
        return (self._tool_history_token is token and self.session_id == session_id
                and self._chat_epoch == epoch)

    def _tool_history_loaded(self, token: object, session_id: str, epoch: int,
                             targets: tuple[ToolCallView, ...], messages: list[ChatMessage]) -> None:
        if self._tool_history_current(token, session_id, epoch):
            self._apply_tool_history(messages, targets)

    def _tool_history_failed(self, token: object, session_id: str, epoch: int,
                             targets: tuple[ToolCallView, ...], code: str) -> None:
        if self._tool_history_current(token, session_id, epoch):
            for view in targets:
                if view in self._chat_items and view.output_text is None:
                    view.set_result_notice(f"{code} · collapse and expand to retry.")

    def _tool_history_done(self, token: object) -> None:
        if self._tool_history_token is not token:
            return
        again = self._tool_history_again
        self._tool_history_token = None
        self._tool_history_targets = ()
        self._tool_history_again = False
        if again:
            self._request_tool_history()

    def _begin_stream(self, initial: str = "") -> StreamView:
        widget = Markdown(classes="assistant")
        view = StreamView(self, widget, self._mount_chat(widget), self._scroll_down)
        view.set_text(initial)
        self._scroll_down()
        return view

    def _show_stream_text(self, text: str) -> None:
        """Show ``text`` as the current answer (appending when it continues it)."""
        if self._stream_view is None:
            if self._reattach_history is not None:
                self._sysline("Current run replay — may overlap the saved checkpoint.", "tool")
            self._stream_view = self._begin_stream()
        self._stream_view.set_text(text)

    # -- input handling -----------------------------------------------------------

    def _composer_warning(self, code: str) -> None:
        if code not in self._composer_errors:
            self._composer_errors.add(code)
            message = f"Input history/draft recovery: {code}. Current text is kept in memory."
            if self._composer_editor is not None and self._composer_editor.is_attached:
                self._sysline(message, "error")
            else:
                self.console.print(f"Input history/draft was not saved: {code}")

    def _restore_composer(self) -> None:
        editor = self.query_one("#input", PromptInput)
        self._composer_editor = editor
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
            if (state.session_id == acknowledged.session_id
                    and state.draft == acknowledged.draft
                    and state.submission_key == acknowledged.key):
                state = ComposerState(self._prompt_history)
                self._composer_dirty = True
            elif self._prompt_history != state.history:
                self._composer_dirty = True
        editor.set_prompt_history(self._prompt_history)
        self._recoverable_draft = state if state.draft else None
        pending = self._pending_submission
        if pending is not None and state.draft and state.session_id == pending.session_id:
            editor.value = state.draft
            self._recoverable_draft = None
            self._composer_submission_key = state.submission_key
        else:
            self._composer_submission_key = pending.key if pending is not None else None
        self._composer_draft = editor.value
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
            when = (" after the pending request is acknowledged"
                    if self._pending_submission is not None else "")
            self._sysline(f"Saved unsent draft for {target}. Use {command}{when} "
                          "to recover it; the saved draft was not sent.")

    def _restore_matching_draft(self) -> None:
        editor = self._composer_editor
        state = self._recoverable_draft
        if (editor is not None and state is not None and not editor.value
                and state.session_id == self.session_id):
            editor.value = state.draft
            self._composer_draft = state.draft
            self._composer_submission_key = state.submission_key
            self._recoverable_draft = None
            self._sysline("Recovered unsent draft. Nothing was sent; Enter sends it when ready.")

    def _capture_composer(self) -> None:
        editor = self._composer_editor
        if editor is not None and editor.value != self._composer_draft:
            self._composer_draft = editor.value
            self._composer_submission_key = None
            if editor.value and not self._composer_command(editor.value):
                self._recoverable_draft = None
            self._composer_dirty = True

    @staticmethod
    def _composer_command(text: str) -> bool:
        return text.lstrip().startswith("/") and "\n" not in text and "\r" not in text

    def _queue_composer_save(self) -> None:
        if self._composer_timer is not None:
            self._composer_timer.stop()
        if self._composer_available and self._composer_dirty:
            self._composer_timer = self.set_timer(0.35, self._flush_composer)

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area is self._composer_editor:
            self._capture_composer()
            self._queue_composer_save()

    def _flush_composer(self) -> None:
        self._capture_composer()
        if not self._composer_available or not self._composer_dirty:
            return
        if len(self._composer_draft.encode("utf-8")) > MAX_DRAFT_BYTES:
            self._composer_warning("draft_too_large_to_save")
            return
        try:
            draft = "" if self._composer_command(self._composer_draft) else self._composer_draft
            recovery = self._recoverable_draft
            # Unmatched saved text is independent of a restored pending request.
            # Only an explicit editor change or matching restore claims its slot.
            if recovery is not None:
                state = ComposerState(self._prompt_history, recovery.draft,
                                      recovery.session_id, recovery.submission_key)
            else:
                state = ComposerState(self._prompt_history, draft,
                                      self.session_id, self._composer_submission_key)
            self.composer.save(state)
        except ComposerError as error:
            self._composer_warning(str(error))
        else:
            self._composer_dirty = False

    def on_prompt_input_submitted(self, event: PromptInput.Submitted) -> None:
        self._capture_composer()
        text = event.value.strip()
        if not text:
            return
        if text.startswith("/") and "\n" not in event.value and "\r" not in event.value:
            event.input.value = ""
            self._handle_command(text)
            return
        if self._session_busy or self._history_refresh_token is not None:
            self._sysline("Session/history load in progress — your draft is kept.")
            return
        if self.session_id is None:
            if not self._draft_fits(event.value):
                return
            # Fresh start: create the Wisp session now, then submit. Nothing is
            # created on the server until there is something to send.
            draft = event.value
            self._new_session(self._fresh_title or _title_from(draft),
                              on_ready=lambda: self._start_submission(draft),
                              announce=False)
            return
        self._start_submission(event.value)

    def _restore_pending(self) -> None:
        was_error = self._pending_state_error
        try:
            pending = self.submissions.load()
        except SubmissionError as error:
            self._pending_state_error = True
            self._status(f"{error} · /retry to recheck")
            self._sysline("Pending-request state is unavailable or in use. New submissions "
                          "are blocked; /retry rechecks without starting fresh work.", "error")
            return
        self._pending_state_error = False
        if pending is None:
            if was_error:
                self._status(FRESH_STATUS if self.session_id is None else "session ready")
            return
        self._pending_submission = pending
        self.session_id = pending.session_id
        self.session_title = "Recovered submission"
        self.title = "Wisp — recovered submission"
        editor = self.query_one("#input", PromptInput)
        if not editor.value:
            editor.value = pending.draft
            self._composer_draft = pending.draft
            self._composer_submission_key = pending.key
            self._composer_dirty = True
            self._queue_composer_save()
        self._render_metadata()
        self._status("unacknowledged submission restored · /retry")
        self._sysline("Recovered an unacknowledged request. Nothing was resubmitted. "
                      "/retry explicitly reuses the original input, options and request ID. "
                      "Session/model changes stay blocked until it is acknowledged.")
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
                    "GET", f"/v1/sessions/{pending.session_id}", timeout=5.0)
                if not isinstance(session, dict) or session.get("id") != pending.session_id:
                    raise ClientError("invalid_session")
                payload = self.client.request(
                    "GET", f"/v1/sessions/{pending.session_id}/history", timeout=5.0)
                messages = parse_history(payload)
                runs = self.client.request(
                    "GET", f"/v1/runs?sessionId={pending.session_id}"
                    "&status=queued,running,parked&limit=200", timeout=5.0)
                active_id = find_active_run(runs, pending.session_id)
            except ClientError as error:
                code = str(error)
            except HistoryError:
                code = "unexpected_history_shape"
            except Exception:
                code = "internal_error"
            else:
                self.call_from_thread(self._pending_view_loaded, token, pending,
                                      epoch, session, messages, active_id)
                return
            self.call_from_thread(self._pending_view_failed, token, pending, code)

        self._run_threaded(work, group="recovery",
                           on_done=lambda: self._pending_view_done(token, pending))

    def _pending_view_current(self, token: object, pending: PendingSubmission) -> bool:
        return (self._recovery_view_token is token and self._pending_submission is pending
                and self.session_id == pending.session_id and not self._submitting
                and self.active_run is None)

    def _pending_view_loaded(self, token: object, pending: PendingSubmission, epoch: int,
                             session: dict[str, Any], messages: list[ChatMessage],
                             active_id: str) -> None:
        if not self._pending_view_current(token, pending):
            return
        self.session_title = _safe_text(session.get("title"), 80) or "(untitled)"
        self.title = f"Wisp — {self.session_title}"
        self._selection_updated(session, pending.session_id)
        # /clear cancels history restoration, but metadata is still useful.
        if self._chat_epoch != epoch:
            return
        hidden = max(0, len(messages) - self.MAX_CHAT_ITEMS)
        self._reset_chat(hidden)
        for message in messages[hidden:]:
            self._add_message(message)
        self._pending_view_history = list(messages)
        self._sysline("Recovered request remains unacknowledged. This view is read-only; "
                      "only explicit /retry reuses the original request ID.")
        if active_id:
            self._sysline(f"Runtime reports active run {active_id[:8]}. "
                          "It is not proof this request was accepted.", "tool")
        else:
            self._sysline("No active v1 run reported. This is not proof the request was rejected.",
                          "tool")
        self._announce_saved_draft()

    def _pending_view_failed(self, token: object, pending: PendingSubmission, code: str) -> None:
        if self._pending_view_current(token, pending):
            self._status(f"recovery view unavailable: {safe_code(code, 'history_unavailable')} · /retry")

    def _pending_view_done(self, token: object, pending: PendingSubmission) -> None:
        if self._recovery_view_token is not token:
            return
        retry = self._retry_after_recovery and self._pending_view_current(token, pending)
        self._recovery_view_token = None
        self._retry_after_recovery = False
        if retry:
            self._submit_pending()

    def _draft_fits(self, draft: str) -> bool:
        if len(draft.encode("utf-8")) > MAX_DRAFT_BYTES:
            self._sysline("Prompt exceeds the safe recovery limit (128 KiB). Split it; your draft is kept.")
            return False
        return True

    def _start_submission(self, draft: str) -> None:
        if self._history_refresh_token is not None:
            self._sysline("History refresh in progress — your draft is kept.")
            return
        if self._pending_state_error:
            self._sysline("Pending-request state must be recovered first (/retry). Your draft is kept.")
            return
        if self.session_id is None:
            return
        if self._pending_submission is not None:
            self._sysline("Submission pending — wait, or use /retry after an error. Your draft is kept.")
            return
        if self.active_run is not None:
            self._sysline("A run is already active — /cancel (Ctrl+C) it first. Your draft is kept.")
            return
        if not self._draft_fits(draft):
            return
        self._capture_composer()
        self._pending_submission = PendingSubmission(
            self.session_id, draft, self.config.max_turns,
            "wisp-tui:" + str(uuid.uuid4()))
        if self._composer_draft == draft:
            self._composer_submission_key = self._pending_submission.key
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
            self._sysline("No pending submission to retry.")
            return
        if self._submitting:
            self._sysline("Submission already in progress — please wait.")
            return
        retry_error = pending.retry_error()
        if retry_error:
            self._status(retry_error)
            self._sysline("This request's age cannot be retried safely. Inspect the session "
                          "in another client; new work remains blocked.", "error")
            return
        if self._recovery_view_token is not None:
            self._retry_after_recovery = True
            self._sysline("Loading recovered session details; your explicit /retry will continue "
                          "after this read-only check.")
            return
        self._submitting = True
        self._status("submitting…")

        def work() -> None:
            try:
                run_id = self.submissions.prepare(pending)
                if run_id is None:
                    # Never contact Wisp until the original request is durable.
                    result = self.client.request(
                        "POST", f"/v1/sessions/{pending.session_id}/runs", pending.body())
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
        self._status(f"submission error: {code} · /retry")
        self._sysline("Submission not acknowledged; it may exist server-side. "
                      "Use /retry to resend the original request with the same ID. "
                      "Wi will recover a durably saved request after exit. "
                      "A pending_state error must be resolved before any new work.", "error")
        editor = self.query_one("#input", PromptInput)
        if not editor.value and self._pending_submission is not None:
            editor.value = self._pending_submission.draft
            self._composer_draft = editor.value
            self._composer_submission_key = self._pending_submission.key
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
        self._record({"type": "message", "role": "user", "text": pending.draft.strip(),
                      "runId": run_id}, pending.session_id)
        self._turn_started(run_id, history=history)
        self._start_follow()
        if self._cancel_after_submit:
            self._cancel_after_submit = False
            self._cancel_run()

    def _turn_started(self, run_id: str, history: list[ChatMessage] | None = None) -> None:
        self.active_run = run_id
        self._tool_calls.clear()
        self._saw_live_tool_events = False
        self._reattach_history = list(history) if history is not None else None
        self._cancel_requested = False
        self._delta_text = ""
        self._stream_view = None
        self._disconnected = False
        self._status(f"run {run_id[:8]} …")

    # -- run event stream -----------------------------------------------------------

    def _start_follow(self) -> None:
        run_id = self.active_run
        session_id = self.session_id
        if run_id is None or session_id is None:
            self._sysline("No active run to reconnect.")
            return
        if self._follower is not None and not self._follower.stopped.is_set():
            if self._follower.run_id == run_id:
                self._follower.wake.set()
                return
            self._follower.stop()
        follower = RunFollower(
            self.client, run_id, session_id, self.config.event_timeout,
            self.last_seq.get(run_id),
            lambda event: self.call_from_thread(self._follow_event, follower, event),
            lambda record: self.call_from_thread(self._follow_snapshot, follower, record),
            lambda code: self.call_from_thread(self._follow_error, follower, code))
        self._follower = follower
        self._run_threaded(follower.run, group="turn",
                           on_done=lambda: self._follow_done(follower))

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
            self._status("run parked — waiting for approval")
            self._sync_confirmations(record.get("confirmations", []))
        else:
            self._sync_confirmations([])
            self._status(f"run {follower.run_id[:8]} {status}")
        if self._cancel_requested:
            self._status("cancellation requested — awaiting runtime confirmation")

    def _follow_error(self, follower: RunFollower, code: str) -> None:
        if self._follower is follower and self.active_run == follower.run_id:
            self._stream_failed(code)

    def _stream_failed(self, code: str) -> None:
        if self.active_run is None:
            return
        if not self._disconnected:
            self._sysline("Connection lost; the task may still be running. "
                          "Reconnecting automatically; /reconnect retries if needed.", "error")
        self._disconnected = True
        self._status(f"disconnected: {code} · /reconnect")
        self._sync_confirmations([])

    def _handle_event(self, event: dict[str, Any]) -> None:
        kind = event.get("kind")
        if kind == "text.delta":
            self._append_delta(str(event.get("text", "")))
        elif kind == "gap":
            text = event.get("text")
            self._delta_text = text if isinstance(text, str) else ""
            self._show_stream_text(self._delta_text)
            if self._stream_view is not None:
                self._stream_view.flush()
        elif kind == "tool_call.started":
            self._tool_event(event, completed=False)
        elif kind == "tool_call":
            ok = event.get("ok") is True
            duration = event.get("durationMs")
            self._tool_event(event, completed=True)
            self._record({"type": "tool", "runId": self.active_run,
                          "name": _safe_text(str(event.get("name", "?")), 200), "ok": ok,
                          "durationMs": duration if isinstance(duration, (int, float)) else None})
        elif kind == "confirmation.resolved":
            prompt_id = event.get("id")
            if isinstance(prompt_id, str):
                self._answered_prompts.add(prompt_id)
            self._sysline("approval resolved", "tool")
        elif kind == "audio.transcribed":
            self._sysline(f"transcribed: {_safe_text(event.get('text'), 120)}", "tool")
        elif kind in ("run.queued", "run.started", "run.resumed"):
            self._status(f"run {self.active_run[:8] if self.active_run else ''} {str(kind).split('.')[1]}")
        elif kind == "run.parked":
            self._status("run parked — checking current approvals")
        elif kind in ("run.completed", "run.failed", "run.cancelled"):
            self._finalize_run(kind, event.get("outcome"))

    def _append_delta(self, text: str) -> None:
        self._delta_text += text
        self._show_stream_text(self._delta_text)

    # -- confirmations ------------------------------------------------------------

    def _sync_confirmations(self, prompts: list[dict[str, Any]]) -> None:
        run_id = self.active_run or ""
        self.pending_prompts = deque(
            (run_id, prompt["id"], prompt["toolName"], prompt.get("args"))
            for prompt in prompts
            if prompt["id"] not in self._approval_inflight | self._answered_prompts)
        self._approval_notes = {
            prompt["id"]: " · ".join(note for applies, note in (
                (prompt.get("askedEveryTime") is True, "asked every time"),
                (prompt.get("escalatedBy") == "judge", "escalated by judge")) if applies)
            for prompt in prompts}
        ids = {prompt_id for _, prompt_id, _, _ in self.pending_prompts}
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
        if self._approval_modal is not None or not self.pending_prompts:
            return
        run_id, prompt_id, tool, args = self.pending_prompts[0]
        if self.active_run != run_id:
            return
        modal = ApprovalModal(tool, args, self._approval_notes.get(prompt_id, ""))
        self._approval_modal = modal
        self._approval_prompt_id = prompt_id
        self.push_screen(modal, lambda decision: self._approval_answered(
            run_id, prompt_id, decision))

    def _approval_answered(self, run_id: str, prompt_id: str, decision: str | None) -> None:
        if decision is None or self.active_run != run_id:
            return
        if self._approval_prompt_id != prompt_id:
            return
        self._approval_modal = None
        self._approval_prompt_id = None
        if decision not in ("approve", "deny", "allow_project", "allow_global"):
            return
        if prompt_id in self._approval_inflight | self._answered_prompts:
            return
        tool, args = next(((t, a) for _, pid, t, a in self.pending_prompts if pid == prompt_id),
                          ("?", None))
        self._record({"type": "approval", "runId": run_id, "promptId": prompt_id,
                      "tool": _safe_text(tool, 200), "args": _pretty_args(args),
                      "decision": decision})
        self.pending_prompts = deque(p for p in self.pending_prompts if p[1] != prompt_id)
        self._approval_inflight.add(prompt_id)
        self._status(f"approval {decision} · sending…")

        def work() -> None:
            code = ""
            try:
                self.client.request(
                    "POST", f"/v1/runs/{run_id}/confirmations/{prompt_id}",
                    {"decision": decision, "wait": False})
            except ClientError as error:
                code = str(error)
            except Exception:
                code = "internal_error"
            self.call_from_thread(self._confirmation_sent, run_id, prompt_id, code)

        self._run_threaded(work, group="confirm")
        self.call_after_refresh(self._open_next_approval)

    def _confirmation_sent(self, run_id: str, prompt_id: str, code: str) -> None:
        if self.active_run != run_id:
            return
        self._approval_inflight.discard(prompt_id)
        if code:
            self._record({"type": "approval_result", "runId": run_id,
                          "promptId": prompt_id, "error": code})
        if not code:
            self._answered_prompts.add(prompt_id)
        elif code.startswith("runtime_http_404"):
            self._sysline("Approval already settled or unavailable; checking run status.", "tool")
        else:
            self._sysline(f"approval error: {code}", "error")
        # Re-read current prompts even after a failed/ambiguous POST. Never
        # automatically submit the decision again, and never start a second SSE.
        self._start_follow()

    # -- run completion ------------------------------------------------------------

    def _finalize_run(self, kind: str, outcome: Any) -> None:
        saved_history = self._reattach_history
        answer = ""
        data = outcome if isinstance(outcome, dict) else {}
        tool_calls = data.get("toolCalls")
        if isinstance(tool_calls, list) and not self._saw_live_tool_events:
            for call in tool_calls[-self.MAX_CHAT_ITEMS:]:
                if not isinstance(call, dict) or not isinstance(call.get("name"), str):
                    continue
                # 0.1.8 strips call IDs and results from these aggregate records.
                # They are a fallback, never another copy of observed live calls.
                details = {key: call[key] for key in ("name", "args", "ok", "durationMs")
                           if key in call}
                self._tool_event(details, completed=True, terminal=True)
        self._last_usage = self._usage_line(data.get("usage"))
        self._render_metadata()
        if kind == "run.completed":
            text = ""
            output = data.get("output")
            has_final_text = isinstance(output, dict) and isinstance(output.get("text"), str)
            if has_final_text:
                assert isinstance(output, dict)
                text = output["text"]
                if text or self._stream_view is not None:
                    self._show_stream_text(text)
            answer = text if has_final_text else self._delta_text
            if answer:
                self._record({"type": "message", "role": "assistant", "text": answer,
                              "runId": self.active_run})
            # End on Wisp's answer, without a completion announcement.
            self._status("")
        elif kind == "run.failed":
            failure = data.get("failure")
            if not isinstance(failure, dict):
                failure = {}
            reason = safe_code(failure.get("reason"), "unknown")
            self._sysline(f"run failed: {reason}", "error")
            self._record({"type": "run", "runId": self.active_run, "status": "failed",
                          "reason": reason})
            self._status("run failed")
        else:
            self._sysline("run cancelled", "tool")
            self._record({"type": "run", "runId": self.active_run, "status": "cancelled"})
            self._status("run cancelled")
        self._finish_run()
        if kind == "run.completed" and saved_history is not None and answer:
            self._refresh_finished_history(saved_history, answer)
        elif any(not view.collapsed for view in self._tool_targets()):
            self._request_tool_history(force_after_current=True)

    def _refresh_finished_history(self, before: list[ChatMessage], answer: str) -> None:
        """Reconcile replay with one fresh checkpoint, never text-dedup old turns."""
        session_id = self.session_id
        if session_id is None:
            return
        token = object()
        self._history_refresh_token = token
        epoch = self._chat_epoch

        def work() -> None:
            try:
                payload = self.client.request(
                    "GET", f"/v1/sessions/{session_id}/history", timeout=5.0)
                messages = parse_history(payload)
            except ClientError as error:
                code = str(error)
            except HistoryError:
                code = "unexpected_history_shape"
            except Exception:
                code = "internal_error"
            else:
                self.call_from_thread(self._history_refreshed, token, session_id,
                                      epoch, before, messages, answer)
                return
            self.call_from_thread(self._history_refresh_failed, token, session_id, epoch, code)

        self._run_threaded(work, group="history",
                           on_done=lambda: self._history_refresh_done(token))

    def _history_refresh_current(self, token: object, session_id: str, epoch: int) -> bool:
        return (self._history_refresh_token is token and self.session_id == session_id
                and self._chat_epoch == epoch and self.active_run is None)

    def _history_refreshed(self, token: object, session_id: str, epoch: int,
                           before: list[ChatMessage], messages: list[ChatMessage],
                           answer: str) -> None:
        if not self._history_refresh_current(token, session_id, epoch):
            return
        self._apply_tool_history(messages, self._tool_targets())
        if not checkpoint_covers_answer(before, messages, answer):
            return
        # Replacing the entire transcript would displace a reader inspecting
        # replay. Keep that labelled view until an explicit reload instead.
        if not self.query_one("#chat", ChatScroll).following:
            return
        hidden = max(0, len(messages) - self.MAX_CHAT_ITEMS)
        self._reset_chat(hidden)
        for message in messages[hidden:]:
            self._add_message(message)

    def _history_refresh_failed(self, token: object, session_id: str, epoch: int,
                                code: str) -> None:
        if self._history_refresh_current(token, session_id, epoch):
            self._status(f"history refresh failed: {safe_code(code, 'history_unavailable')}")
            for view in self._tool_targets():
                view.set_result_notice("history_unavailable · collapse and expand to retry.")

    def _history_refresh_done(self, token: object) -> None:
        if self._history_refresh_token is token:
            self._history_refresh_token = None

    def _finish_run(self) -> None:
        if self._stream_view is not None:
            self._stream_view.flush()
        for item in self._chat_items:
            if isinstance(item, ToolCallView) and not item.completed and not item.historical:
                item.mark_unreported()
        if self._follower is not None:
            self._follower.stop()
        self._dismiss_approval()
        self._approval_inflight.clear()
        self._answered_prompts.clear()
        self._disconnected = False
        self._cancel_requested = False
        if self.active_run:
            self.last_seq.pop(self.active_run, None)
        self.active_run = None
        self._delta_text = ""
        self._stream_view = None
        self._reattach_history = None
        self.pending_prompts.clear()
        self._render_activity()

    @staticmethod
    def _usage_line(usage: Any) -> str:
        if not isinstance(usage, dict):
            return ""
        parts = []
        for key in ("inputTokens", "outputTokens", "totalTokens"):
            value = usage.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                parts.append(f"{key.replace('Tokens', '')}: {value:,}")
        return ", ".join(parts)

    # -- sessions ----------------------------------------------------------------

    def _reserve_session_change(self) -> bool:
        if self._pending_state_error:
            self._sysline("Pending-request state must be recovered first (/retry).")
            return False
        if self._session_busy or self._history_refresh_token is not None:
            self._sysline("Session/history load already in progress — please wait.")
            return False
        if self._pending_submission is not None:
            self._sysline("Resolve the pending submission before changing sessions or models (/retry).")
            return False
        if self.active_run is not None:
            self._sysline("A run is active — /cancel before changing sessions or models.")
            return False
        self._session_busy = True
        return True

    def _release_session_change(self) -> None:
        self._session_busy = False

    def _open_session(self, session_id: str) -> None:
        if not UUID_RE.fullmatch(session_id):
            self._sysline("invalid_session_id", "error")
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
                self.call_from_thread(self._sysline, "session no longer exists", "error")
                return
            # Discover first: a run that finishes while history loads still has
            # a known ID to reconcile, rather than disappearing from discovery.
            runs = self.client.request(
                "GET", f"/v1/runs?sessionId={session_id}&status=queued,running,parked&limit=200")
            active_id = find_active_run(runs, session_id)
            history = self.client.request("GET", f"/v1/sessions/{session_id}/history")
            self.call_from_thread(self._session_loaded, session,
                                  parse_history(history), active_id)

        self._run_threaded(work, group="load", on_done=self._release_session_change)

    def _session_missing(self, session_id: str) -> None:
        self._status("session not found on the runtime")
        path = self.sessions.path_for(session_id)
        note = f" Your local record: {self._display_path(path)}" if path else ""
        self._sysline(f"Session {session_id[:8]} no longer exists on the Wisp runtime.{note}",
                      "error")

    @staticmethod
    def _display_path(path: Path) -> str:
        home = str(Path.home())
        text = str(path)
        return "~" + text[len(home):] if text.startswith(home + "/") else text

    def _record(self, entry: dict[str, Any], session_id: str | None = None) -> None:
        """Append to the local session record; report a failure once."""
        target = session_id or self.session_id
        if target is None:
            return
        title = "" if self.session_title == "(untitled)" else self.session_title
        code = self.sessions.record(target, entry, title=title)
        if code and not self._record_error_shown:
            self._record_error_shown = True
            self._sysline(f"Local session record unavailable: {code}", "error")

    def _session_loaded(self, session: dict[str, Any], messages: list[ChatMessage],
                        active_run_id: str, announce_empty: bool = True) -> None:
        session_id = str(session.get("id", ""))
        if self.session_id != session_id:
            self._last_usage = ""
        self.session_id = session_id
        self._selection_updated(session, session_id)
        self._restore_matching_draft()
        if self._composer_editor is not None and self._composer_editor.value:
            self._composer_dirty = True
            self._queue_composer_save()
        self.session_title = _safe_text(session.get("title"), 80) or "(untitled)"
        self.title = f"Wisp — {self.session_title}"
        # Only mount the tail of long histories (see MAX_CHAT_ITEMS).
        hidden = max(0, len(messages) - self.MAX_CHAT_ITEMS)
        self._reset_chat(hidden)
        for message in messages[hidden:]:
            self._add_message(message)
        if not messages and announce_empty:
            self._sysline(f"Session '{self.session_title}' is empty. Say something to start.")
        self._status(f"session {self.session_id[:8]} · {self._model_label(session)}")
        if active_run_id:
            self._turn_started(active_run_id, history=messages)
            self._sysline("Reattaching to this session's ongoing run.", "tool")
            self._start_follow()

    @staticmethod
    def _model_label(session: dict[str, Any]) -> str:
        selection = session.get("effectiveSelection")
        if isinstance(selection, dict):
            if selection.get("mode") == "model":
                return f"model: {_metadata_text(selection.get('model')) or 'unavailable'}"
            mode = _metadata_text(selection.get("mode"))
            if mode:
                return f"mode: {mode} (runtime-selected model)"
        return "model: unavailable (runtime default)"

    def _new_session(self, title: str, on_ready: Callable[[], None] | None = None,
                     announce: bool = True) -> None:
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
            if (isinstance(session, dict) and isinstance(session.get("id"), str)
                    and UUID_RE.fullmatch(session["id"])):
                self.call_from_thread(self._session_created, session, on_ready, announce)
            else:
                self.call_from_thread(self._session_create_failed, "invalid_session")

        self._run_threaded(work, group="load")

    def _session_created(self, session: dict[str, Any],
                         on_ready: Callable[[], None] | None, announce: bool) -> None:
        # Release before continuing, so the continuation (submit, model
        # picker) is not mistaken for a session change still in progress.
        self._release_session_change()
        self._fresh_title = ""
        self._session_loaded(session, [], "", announce_empty=announce)
        if on_ready is not None:
            on_ready()

    def _session_create_failed(self, code: str) -> None:
        self._release_session_change()
        self._status(f"error: {code}")
        self._sysline(f"Could not start a Wisp session ({code}). Your draft is kept.", "error")

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
        self._selection = {}
        self._last_usage = ""
        self._render_metadata()
        self.title = "wisp-tui"
        self._fresh_title = title
        self._reset_chat()
        self._status(FRESH_STATUS)

    def action_resume(self, query: str = "") -> None:
        """Saved-session picker; with an ID or prefix, open the match directly."""
        if self._approval_modal is not None:
            return
        query = query.strip().lower()
        if query and not RESUME_QUERY_RE.fullmatch(query):
            self._sysline("Usage: /resume [session ID or prefix, 4+ hex characters]")
            return
        summaries = self.sessions.list(limit=10_000) if query else self.sessions.list()
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
                self._sysline(f"No saved session matches '{query}'. F2 lists all Wisp sessions.")
                return
            wanted = set(matches)
            summaries = [item for item in summaries if item.id in wanted]
        if not summaries:
            self._sysline("No saved sessions yet — sessions are saved as you chat.")
            return
        heading = (f"Saved sessions matching '{query}'" if query
                   else "Saved sessions (newest first)")
        self.push_screen(SessionPicker([(item.id, item.label()) for item in summaries],
                                       heading), self._picker_answered)

    def action_sessions(self) -> None:
        def work() -> None:
            sessions = self.client.request("GET", "/v1/sessions")
            if not isinstance(sessions, list):
                self.call_from_thread(self._sysline, "invalid sessions response", "error")
                return
            self.call_from_thread(self._show_session_picker, sessions[:100])

        self._run_threaded(work, group="load")

    def _show_session_picker(self, sessions: list[dict[str, Any]]) -> None:
        if self._approval_modal is not None:
            return
        entries = [(str(item.get("id", "")), server_session_label(item))
                   for item in sessions if isinstance(item, dict)]
        if not entries:
            self._sysline("No Wisp sessions yet — your first message starts one.")
            return
        self.push_screen(SessionPicker(entries, "Wisp sessions, all clients (newest first)"),
                         self._picker_answered)

    def _picker_answered(self, session_id: str | None) -> None:
        if session_id and UUID_RE.fullmatch(session_id):
            self._open_session(session_id)

    def action_model(self) -> None:
        if self.session_id is None:
            # The model is chosen per session: start one now, then pick.
            self._new_session(self._fresh_title, on_ready=self.action_model, announce=False)
            return

        session_id = self.session_id

        def work() -> None:
            models = self.client.request("GET", "/v1/models")
            data = models.get("data") if isinstance(models, dict) else None
            if not isinstance(data, list):
                self.call_from_thread(self._sysline, "invalid models response", "error")
                return
            session = self.client.request("GET", f"/v1/sessions/{session_id}")
            current = session.get("effectiveSelection") if isinstance(session, dict) else None
            self.call_from_thread(self._show_model_picker, data, current, session_id)

        self._run_threaded(work, group="load")

    def _show_model_picker(self, models: list[dict[str, Any]],
                           current: dict[str, Any] | None, session_id: str) -> None:
        if (self.session_id != session_id or self._session_busy
                or self._approval_modal is not None):
            return
        self.push_screen(ModelPicker(models, current),
                         lambda selection: self._model_answered(selection, session_id))

    def _model_answered(self, selection: dict[str, Any] | None, session_id: str) -> None:
        if selection is None or self.session_id != session_id:
            return
        if not self._reserve_session_change():
            return

        def work() -> None:
            session = self.client.request(
                "PATCH", f"/v1/sessions/{session_id}", {"selection": selection})
            if isinstance(session, dict):
                self.call_from_thread(self._selection_updated, session, session_id)
                self.call_from_thread(self._status,
                                      f"session {session_id[:8]} · {self._model_label(session)}")
            else:
                self.call_from_thread(self._sysline, "could not set selection", "error")

        self._run_threaded(work, group="load", on_done=self._release_session_change)

    # -- commands and actions -----------------------------------------------------

    def action_cancel(self) -> None:
        """Ctrl+C: cancel the active run; press twice (within 3s) to leave."""
        now = time.monotonic()
        if now - self._last_ctrl_c <= 3.0:
            self.exit()
            return
        self._last_ctrl_c = now
        if self._pending_submission is not None and not self._cancel_after_submit:
            self._cancel_run()
        elif self.active_run is not None and not self._cancel_requested:
            self._cancel_run()
            self._status("cancellation requested — press Ctrl+C again to leave")
        else:
            self._status("press Ctrl+C again to leave")

    def _cancel_run(self) -> None:
        if self._pending_submission is not None:
            self._cancel_after_submit = True
            self._status("will cancel after submission is acknowledged · /retry if needed")
            return
        if self.session_id is None or self.active_run is None:
            self._sysline("No known active run to cancel.")
            return
        if self._cancel_requested:
            self._sysline("Cancellation already requested; awaiting runtime confirmation.")
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
            self._status(f"cancel error: {code} · /cancel to retry")
        else:
            self._status("cancellation requested — awaiting runtime confirmation")
            self._start_follow()

    def action_help(self) -> None:
        self._show_help()

    def _show_help(self) -> None:
        self._mount_chat(Markdown(HELP_TEXT, classes="system"))
        self._scroll_down()

    def _handle_command(self, text: str) -> None:
        parts = text.split(maxsplit=1)
        command = parts[0].lower()
        argument = parts[1].strip() if len(parts) > 1 else ""
        if command == "/help":
            self._show_help()
        elif command == "/new":
            self._start_fresh(_safe_text(argument, 80))
        elif command == "/resume":
            self.action_resume(argument)
        elif command == "/sessions":
            self.action_sessions()
        elif command == "/model":
            self.action_model()
        elif command == "/cancel":
            self._cancel_run()
        elif command == "/retry":
            self._submit_pending()
        elif command == "/reconnect":
            self._start_follow()
        elif command == "/clear":
            self._reset_chat()
        elif command == "/status":
            self._status_command()
        elif command == "/quit":
            self.exit()
        else:
            self._sysline(f"unknown command {command} — /help lists them")

    def _status_command(self) -> None:
        lines = [
            f"runtime: {self.client.url}",
            f"session: {self.session_id or 'none'}"
            + (f" ({self.session_title})" if self.session_id else ""),
            f"active run: {self.active_run or 'none'}",
            f"submission: {'state_error' if self._pending_state_error else 'sending' if self._submitting else 'unacknowledged' if self._pending_submission else 'none'}",
            f"recovery view: {'loading' if self._recovery_view_token is not None else 'none'}",
            f"history refresh: {'loading' if self._history_refresh_token is not None else 'none'}",
            f"max turns: {self.config.max_turns}",
            *self._metadata_lines(),
            "Wisp working directory: unavailable (not the client launch directory)",
            "Token usage is reported at run completion, not live; context occupancy is unavailable.",
        ]
        for line in lines:
            self._sysline(line, "tool")

    def on_unmount(self) -> None:
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
        self._history_refresh_token = None
        self._reattach_history = None
        if self._activity_timer is not None:
            self._activity_timer.stop()
        self.submissions.close()
        # Stop local following only; quitting never cancels server-side work.
        if self._follower is not None:
            self._follower.stop()
