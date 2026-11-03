"""Textual chat application for the local Wisp runtime.

The app is a thin, private client: your messages go to the Wisp runtime on
loopback, and its answers are rendered locally. No other model or service is
in the path. Worker threads perform all network I/O and marshal results into
the UI thread via ``call_from_thread``.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from textual.app import App, ComposeResult
from textual.containers import Container, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.widget import AwaitMount, Widget
from textual.widgets import Input, ListItem, ListView, Markdown, Static

from .client import ClientError, WispClient, TERMINAL_RUN_STATUSES, safe_code
from .config import Config
from .history import ChatMessage, parse_history
from .runs import RunFollower, find_active_run
from .sessions import SessionStore
from .submissions import MAX_DRAFT_BYTES, PendingSubmission, SubmissionError, SubmissionStore

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# A session ID or a prefix of one (as shown in pickers), for --resume / /resume.
RESUME_QUERY_RE = re.compile(r"[0-9a-f][0-9a-f-]{3,35}")

HINTS = "F1 help · F2 sessions · F3 model · Ctrl+C cancel/exit"
SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

HELP_TEXT = """**wisp-tui** — chat with the local Wisp runtime.

| Key | Action |
|---|---|
| Enter | Send the input line |
| F1 | This help |
| F2 | Session picker |
| F3 | Model / mode picker |
| Ctrl+C | Cancel the active run — press twice to leave |

**Commands:** `/new [title]`, `/resume`, `/sessions`, `/model`, `/cancel`, `/retry`, `/reconnect`, `/status`, `/clear`, `/help`, `/quit`.

**Sessions:** every launch starts fresh; your first message creates the Wisp
session. `/new` starts another one. Sessions you use here are saved locally
(`~/.local/state/wisp-tui/sessions/`): `/resume` (or `wisp-tui -r`) picks one,
`/resume 3bf1ba87` (or `wisp-tui -r 3bf1ba87`) opens it by ID prefix, and
`wisp-tui -c` continues the most recent. `/sessions` (F2) lists all Wisp
sessions, including Telegram/desktop ones.

`/retry` retries an unacknowledged submission with its original input and request
ID. It never sends your newer draft. Pending requests are saved privately before
submission and recovered after exit, without automatic resubmission. Unsent
drafts are still memory-only. A state error blocks new work until resolved.

A finished run is one answered prompt, not the end of the conversation: just
send your next message. The footer keeps the session, model/mode, thinking
setting and last finished run's reported tokens visible. Tokens update when a
run finishes, not live; context occupancy is unavailable. The launch directory
belongs to this client, not Wisp's tools. `/status` shows the full details.

While a run is active you will see tool calls as they happen. When Wisp needs
approval for a tool, a dialog appears: **y** approve, **n** deny, **p** allow
for this project, **g** allow globally, **Esc** deny. Normal questions consume
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
    meanwhile) and falls back to a full update when the text is replaced
    (``gap``/final output that is not a continuation). It waits for the
    widget's mount, so no delta can be lost to Markdown's initial update.
    """

    def __init__(self, app: App[Any], widget: Markdown, mounted: AwaitMount,
                 on_rendered: Callable[[], None]) -> None:
        self.app = app
        self.widget = widget
        self._mounted = mounted
        self._on_rendered = on_rendered
        self.target = ""
        self.rendered = ""
        self._running = False

    def set_text(self, text: str) -> None:
        self.target = text
        if not self._running and self.rendered != text:
            self._running = True
            self.app.run_worker(self._render(), group="render", exclusive=False)

    async def _render(self) -> None:
        try:
            await self._mounted
            while self.rendered != self.target and self.widget.is_attached:
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

    # The dialog has no focusable children; take focus ourselves so the
    # y/n/p/g/escape bindings are handled by this screen.
    can_focus = True

    BINDINGS = [
        ("y", "approve", "Approve"),
        ("n", "deny", "Deny"),
        ("p", "allow_project", "Allow for project"),
        ("g", "allow_global", "Allow globally"),
        ("escape", "deny", "Deny"),
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
            yield Static(f"Tool: {self.tool}", id="approval-tool", markup=False)
            if self.note:
                yield Static(self.note, id="approval-note", markup=False)
            yield Static(_pretty_args(self.args), id="approval-args", markup=False)
            yield Static("y approve · n/Esc deny · p allow for project · g allow globally",
                         id="approval-hint", markup=False)

    def on_mount(self) -> None:
        self.focus()

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

    BINDINGS = [("escape", "close", "Close")]

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

    BINDINGS = [("escape", "close", "Close")]

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
    #bottom { dock: bottom; height: auto; padding: 0 1; }
    #input {
        border-top: solid ansi_bright_black;
        border-bottom: solid ansi_bright_black;
        border-left: none;
        border-right: none;
        background: transparent;
        color: ansi_default;
        padding: 0;
    }
    #input:focus { background-tint: transparent; }
    #activity { display: none; }
    #activity, #status, #session-info, #thinking-info, #usage-info, #location {
        height: 1;
        background: transparent;
        color: ansi_default;
        text-style: dim;
        padding: 0;
    }
    #session-info, #thinking-info, #usage-info { height: auto; max-height: 2; }
    #approval-dialog {
        align: center middle; width: 80%; max-height: 80%;
        border: round $accent; padding: 1 2;
    }
    #approval-title { text-style: bold; color: $accent; margin-bottom: 1; }
    #approval-tool { text-style: bold; }
    #approval-args { margin: 1 0; }
    #approval-hint { color: $text-muted; }
    #picker-dialog {
        align: center middle; width: 90%; height: 80%;
        border: round $accent; padding: 1 2;
    }
    #picker-title { text-style: bold; margin-bottom: 1; }
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
        self.client = client
        self.config = config
        self.startup = startup or Startup()
        self.sessions = sessions or SessionStore()
        self.submissions = submissions or SubmissionStore(client.url)
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
        self._chat_items: deque[Widget] = deque()
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
        yield VerticalScroll(id="chat")
        with Container(id="bottom"):
            yield self._activity_indicator
            yield Input(placeholder="Message Wisp — /help for commands", id="input")
            yield Static("", id="status", markup=False)
            yield Static("", id="session-info", markup=False)
            yield Static("", id="thinking-info", markup=False)
            yield Static("", id="usage-info", markup=False)
            yield Static("", id="location", markup=False)

    def on_mount(self) -> None:
        self._activity_timer = self.set_interval(0.1, self._tick_activity, pause=True)
        self._render_status()
        self._render_metadata()
        self._restore_pending()
        self.query_one("#input", Input).focus()
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

    def _render_status(self) -> None:
        line = f"{self._status_state} · {HINTS}" if self._status_state else HINTS
        self.query_one("#status", Static).update(_safe_text(line, 250))
        self._render_activity()

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
        for widget_id, line in zip(("session-info", "thinking-info", "usage-info", "location"),
                                   self._metadata_lines()):
            self.query_one(f"#{widget_id}", Static).update(line)

    def _selection_updated(self, session: dict[str, Any], session_id: str) -> None:
        if self.session_id != session_id:
            return
        selection = session.get("effectiveSelection")
        self._selection = dict(selection) if isinstance(selection, dict) else {}
        self._render_metadata()

    def _scroll_down(self) -> None:
        self.query_one("#chat", VerticalScroll).scroll_end(animate=False)

    def _mount_chat(self, widget: Widget) -> AwaitMount:
        """Append a chat item, dropping the oldest beyond MAX_CHAT_ITEMS."""
        mounted = self.query_one("#chat", VerticalScroll).mount(widget)
        self._chat_items.append(widget)
        self._trim_chat()
        return mounted

    def _trim_chat(self) -> None:
        streaming = self._stream_view.widget if self._stream_view is not None else None
        removed = 0
        while len(self._chat_items) > self.MAX_CHAT_ITEMS:
            oldest = self._chat_items.popleft()
            if oldest is streaming:
                # Never drop the answer that is still being written.
                second = self._chat_items.popleft()
                self._chat_items.appendleft(oldest)
                oldest = second
            oldest.remove()
            removed += 1
        if removed:
            self._trimmed_count += removed
            self._update_trim_marker()

    def _update_trim_marker(self) -> None:
        count = self._trimmed_count
        text = (f"\u2191 {count} earlier item{'s' if count != 1 else ''} not shown "
                "\u2014 Wisp keeps the full session history.")
        if self._trim_marker is not None and self._trim_marker.is_attached:
            self._trim_marker.update(text)
            return
        marker = Static(text, id="chat-trimmed", classes="system", markup=False)
        self._trim_marker = marker
        chat = self.query_one("#chat", VerticalScroll)
        if self._chat_items:
            chat.mount(marker, before=self._chat_items[0])
        else:
            chat.mount(marker)

    def _reset_chat(self, hidden: int = 0) -> None:
        """Remove every chat item; optionally note ``hidden`` older items."""
        self.query_one("#chat", VerticalScroll).remove_children()
        self._chat_items.clear()
        # A still-running answer continues in a fresh widget (full text so far).
        self._stream_view = None
        self._trim_marker = None
        self._trimmed_count = hidden
        if hidden:
            self._update_trim_marker()

    def _sysline(self, text: str, cls: str = "system") -> None:
        self._mount_chat(Static(_safe_text(text), classes=cls, markup=False))
        self._scroll_down()

    def _add_message(self, message: ChatMessage) -> None:
        if message.role == "assistant":
            self._mount_chat(Markdown(message.text, classes="assistant"))
        else:
            self._mount_chat(Static(message.text, classes=message.role, markup=False))
        self._scroll_down()

    def _begin_stream(self, initial: str = "") -> StreamView:
        widget = Markdown(classes="assistant")
        view = StreamView(self, widget, self._mount_chat(widget), self._scroll_down)
        view.set_text(initial)
        self._scroll_down()
        return view

    def _show_stream_text(self, text: str) -> None:
        """Show ``text`` as the current answer (appending when it continues it)."""
        if self._stream_view is None:
            self._stream_view = self._begin_stream()
        self._stream_view.set_text(text)

    # -- input handling -----------------------------------------------------------

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        if text.startswith("/"):
            event.input.value = ""
            self._handle_command(text)
            return
        if self._session_busy:
            self._sysline("Session change in progress — your draft is kept.")
            return
        if self.session_id is None:
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
        editor = self.query_one("#input", Input)
        if not editor.value:
            editor.value = pending.draft
        self._render_metadata()
        self._status("unacknowledged submission restored · /retry")
        self._sysline("Recovered an unacknowledged request. Nothing was resubmitted. "
                      "/retry explicitly reuses the original input, options and request ID. "
                      "Session/model changes stay blocked until it is acknowledged.")

    def _start_submission(self, draft: str) -> None:
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
        if len(draft.encode("utf-8")) > MAX_DRAFT_BYTES:
            self._sysline("Prompt exceeds the safe recovery limit (128 KiB). Split it; your draft is kept.")
            return
        self._pending_submission = PendingSubmission(
            self.session_id, draft, self.config.max_turns,
            "wisp-tui:" + str(uuid.uuid4()))
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
        editor = self.query_one("#input", Input)
        if not editor.value and self._pending_submission is not None:
            editor.value = self._pending_submission.draft

    def _submission_accepted(self, pending: PendingSubmission, run_id: str) -> None:
        self._submitting = False
        self._pending_submission = None
        editor = self.query_one("#input", Input)
        if editor.value == pending.draft:
            editor.value = ""
        self._add_message(ChatMessage(role="user", text=pending.draft.strip()))
        self._record({"type": "message", "role": "user", "text": pending.draft.strip(),
                      "runId": run_id}, pending.session_id)
        self._turn_started(run_id)
        self._start_follow()
        if self._cancel_after_submit:
            self._cancel_after_submit = False
            self._cancel_run()

    def _turn_started(self, run_id: str) -> None:
        self.active_run = run_id
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
        elif kind == "tool_call.started":
            self._sysline(f"tool {event.get('name', '?')} …", "tool")
        elif kind == "tool_call":
            ok = event.get("ok") is True
            duration = event.get("durationMs")
            seconds = f" {duration / 1000:.1f}s" if isinstance(duration, (int, float)) else ""
            mark = "ok" if ok else "FAILED"
            self._sysline(f"tool {event.get('name', '?')} → {mark}{seconds}", "tool")
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
        data = outcome if isinstance(outcome, dict) else {}
        self._last_usage = self._usage_line(data.get("usage"))
        self._render_metadata()
        if kind == "run.completed":
            text = ""
            output = data.get("output")
            if isinstance(output, dict) and isinstance(output.get("text"), str):
                text = output["text"]
            if text:
                self._show_stream_text(text)
            answer = text or self._delta_text
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

    def _finish_run(self) -> None:
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
                parts.append(f"{key.replace('Tokens', '')}: {value}")
        return ", ".join(parts)

    # -- sessions ----------------------------------------------------------------

    def _reserve_session_change(self) -> bool:
        if self._pending_state_error:
            self._sysline("Pending-request state must be recovered first (/retry).")
            return False
        if self._session_busy:
            self._sysline("Session change already in progress — please wait.")
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
            history = self.client.request("GET", f"/v1/sessions/{session_id}/history")
            runs = self.client.request(
                "GET", f"/v1/runs?sessionId={session_id}&status=queued,running,parked&limit=200")
            active_id = find_active_run(runs, session_id)
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
            self._turn_started(active_run_id)
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
            f"max turns: {self.config.max_turns}",
            *self._metadata_lines(),
            "Wisp working directory: unavailable (not the client launch directory)",
            "Token usage is reported at run completion, not live; context occupancy is unavailable.",
        ]
        for line in lines:
            self._sysline(line, "tool")

    def on_unmount(self) -> None:
        if self._activity_timer is not None:
            self._activity_timer.stop()
        self.submissions.close()
        # Stop local following only; quitting never cancels server-side work.
        if self._follower is not None:
            self._follower.stop()
