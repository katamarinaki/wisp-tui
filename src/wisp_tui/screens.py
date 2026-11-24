"""Modal screens: approval prompts and the session and model pickers.

Like Pi's selectors, each is drawn in the editor's place, as tall as its
content, so the chat stays visible above it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, ClassVar, Generic, TypeVar

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import BindingType
from textual.containers import Vertical, VerticalScroll
from textual.geometry import Region
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.widgets import ListItem, ListView, Static

from .text import metadata_text, pretty_args

APPROVAL_ARGS_LIMIT = 200_000
# Rule decisions outlive the run, so they take a second, re-armed key press.
RULE_DECISIONS = {"allow_project": ("p", "this project"), "allow_global": ("g", "every project")}
DECISIONS = ("approve", "deny", *RULE_DECISIONS)
# Selections where Wisp picks the model.
AUTO_MODES = ("advanced", "balanced")


ResultT = TypeVar("ResultT")
ValueT = TypeVar("ValueT")


class BottomPanel(ModalScreen[ResultT]):
    """A modal docked over the editor; its ``.panel-scroll`` child takes the overflow."""

    # The editor's region on the screen underneath, which the panel covers; set by the app.
    placement: Callable[[], Region | None] | None = None
    # The app's status line, which the panel covers on the screen underneath.
    status_text = ""

    def on_mount(self) -> None:
        status = Static(self.status_text, id="panel-status", markup=False)
        status.display = bool(self.status_text)
        self.query_one(".panel").mount(status, before=0)
        self.call_after_refresh(self.fit)

    def show_status(self, text: str) -> None:
        self.status_text = text
        # Absent before mount and while the panel is being dismissed.
        status = self.query_one_optional("#panel-status", Static)
        if status is not None:
            status.update(text)
            status.display = bool(text)
            self.call_after_refresh(self.fit)

    def on_resize(self) -> None:
        self.call_after_refresh(self.fit)

    def fit(self) -> None:
        panel = self.query_one_optional(".panel")
        scroller = self.query_one_optional(".panel-scroll")
        if panel is None or scroller is None:
            return
        height = self.size.height
        editor = (self.placement() if self.placement else None) or Region(0, height, 0, 0)
        below = min(max(0, height - editor.bottom), height - 3)
        available = height - below
        panel.styles.margin = (0, 0, below, editor.x)
        if editor.width:
            panel.styles.width = editor.width
        panel.styles.min_height = min(editor.height, available)
        panel.styles.max_height = available
        chrome = panel.styles.gutter.height + sum(
            child.outer_size.height
            for child in panel.children
            if child is not scroller and child.display
        )
        scroller.styles.max_height = max(1, available - chrome)


class ApprovalModal(BottomPanel[str | None]):
    """Ask the owner to resolve a parked run's confirmation prompt."""

    # Decisions belong to this screen even when the details viewport has focus.
    can_focus = True

    BINDINGS: ClassVar[list[BindingType]] = [
        ("y", "decide('approve')", "Approve"),
        ("n", "decide('deny')", "Deny"),
        ("p", "decide('allow_project')", "Allow for project"),
        ("g", "decide('allow_global')", "Allow globally"),
        ("escape", "deny_now", "Deny"),
        ("ctrl+c", "app.cancel", "Cancel/exit"),
    ]

    # Letter keys stay inert until the dialog has been on screen with no decision
    # key pressed for this long, so type-ahead or key repeat cannot answer it.
    ARM_DELAY = 0.5

    def __init__(self, tool_name: str, args: Any, note: str = "", allow_rules: bool = True) -> None:
        super().__init__()
        self.tool_name = tool_name
        self.args = args
        self.note = note
        self.allow_rules = allow_rules
        self.armed = False
        self._arm_timer: Timer | None = None
        self.pending_rule: str | None = None
        args_text = pretty_args(args, APPROVAL_ARGS_LIMIT + 1)
        # A decision must never cover arguments the owner could not read.
        self.reviewable = len(args_text) <= APPROVAL_ARGS_LIMIT
        self.args_text = (
            args_text
            if self.reviewable
            else (args_text[:4000] + "\n… arguments too large to review; only deny is available")
        )

    def compose(self) -> ComposeResult:
        with Vertical(id="approval-dialog", classes="panel"):
            # Approval details must be shown exactly as the runtime sent them:
            # Textual markup would hide/alter bracketed text (or crash).
            yield Static("Wisp asks for approval", id="approval-title", markup=False)
            with VerticalScroll(id="approval-details", classes="panel-scroll"):
                yield Static(
                    f"Tool: {metadata_text(self.tool_name, 200)}", id="approval-tool", markup=False
                )
                if self.note:
                    yield Static(self.note, id="approval-note", markup=False)
                yield Static(self.args_text, id="approval-args", markup=False)
            yield Static(self._hint(), id="approval-hint", markup=False)

    def _hint(self) -> str:
        if not self.reviewable:
            return "↑↓/PgUp/PgDn scroll · n/Esc deny (arguments too large to approve)"
        if self.pending_rule is not None:
            key, scope = RULE_DECISIONS[self.pending_rule]
            return (
                f"Wisp will save a rule that allows {metadata_text(self.tool_name, 80)} "
                f"in {scope}, for future runs too. {key} again to confirm · "
                "y approve once · n/Esc deny"
            )
        rules = " · p allow for project · g allow globally" if self.allow_rules else ""
        return f"↑↓/PgUp/PgDn scroll · y approve · n/Esc deny{rules}"

    def on_mount(self) -> None:
        self.query_one("#approval-details", VerticalScroll).focus()
        self._start_arming()

    def _start_arming(self) -> None:
        self.armed = False
        if self._arm_timer is not None:
            self._arm_timer.stop()
        self._arm_timer = self.set_timer(self.ARM_DELAY, self._arm)

    def _arm(self) -> None:
        self.armed = True

    def action_decide(self, decision: str) -> None:
        if not self.armed:
            if self._arm_timer is not None:
                self._arm_timer.reset()
            return
        if decision != "deny" and not self.reviewable:
            return
        if decision in RULE_DECISIONS:
            if not self.allow_rules:
                return
            if self.pending_rule != decision:
                self.pending_rule = decision
                self.query_one("#approval-hint", Static).update(self._hint())
                self.call_after_refresh(self.fit)
                self._start_arming()
                return
        self.dismiss(decision)

    def action_deny_now(self) -> None:
        self.dismiss("deny")


def server_session_label(session: dict[str, Any]) -> str:
    sid = str(session.get("id", ""))
    title = metadata_text(session.get("title"), 60) or "(untitled)"
    origin = metadata_text(str(session.get("origin", "?")), 40)
    count = metadata_text(str(session.get("messageCount", "?")), 12)
    updated = metadata_text(session.get("updatedAt"), 20).replace("T", " ")
    return f"{title}  ·  {origin}  ·  {count} msg  ·  {updated}  ·  {sid[:8]}"


class _ListPicker(BottomPanel[ValueT | None], Generic[ValueT]):
    """Pick one of ``values``: type to filter, arrows to move, Enter to choose."""

    BINDINGS: ClassVar[list[BindingType]] = [
        ("escape", "close", "Close"),
        ("ctrl+c", "app.cancel", "Cancel/exit"),
    ]
    HINT = "↑↓ move · type to filter · enter select · esc cancel"

    def __init__(
        self, heading: str, values: list[ValueT], labels: list[str], current: int | None = None
    ) -> None:
        super().__init__()
        self.heading = heading
        self.values = values
        self.labels = labels
        self.current = current
        self.query_text = ""

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-dialog", classes="panel"):
            yield Static(self.heading, id="picker-title", markup=False)
            yield Static("", id="picker-filter", markup=False)
            items = [ListItem(Static(markup=False)) for _ in self.labels]
            yield ListView(*items, classes="panel-scroll", initial_index=self.current or 0)
            yield Static("", id="picker-hint", markup=False)

    def on_mount(self) -> None:
        self._show()

    def on_key(self, event: events.Key) -> None:
        if event.key == "backspace":
            self.query_text = self.query_text[:-1]
        elif event.key == "ctrl+u":
            self.query_text = ""
        elif event.is_printable and event.character:
            self.query_text += event.character
        else:
            return
        event.stop()
        self._apply_filter()

    def _apply_filter(self) -> None:
        words = self.query_text.lower().split()
        listing = self.query_one(ListView)
        for item, label in zip(listing.children, self.labels, strict=True):
            shown = all(word in label.lower() for word in words)
            item.display = shown
            item.disabled = not shown
        if listing.index is None or listing.children[listing.index].disabled:
            listing.index = next(
                (i for i, item in enumerate(listing.children) if not item.disabled), None
            )
        self._show()
        # Rows above the cursor may have come back: scroll once the new layout exists.
        self.refresh(layout=True)
        self.call_after_refresh(self._scroll_to_cursor)

    def _scroll_to_cursor(self) -> None:
        listing = self.query_one(ListView)
        if listing.highlighted_child is not None:
            listing.scroll_to_widget(listing.highlighted_child, animate=False)

    def on_list_view_highlighted(self) -> None:
        self._show()

    def _show(self) -> None:
        listing = self.query_one(ListView)
        shown = [i for i, item in enumerate(listing.children) if not item.disabled]
        for index, item in enumerate(listing.children):
            cursor = "→" if index == listing.index else " "
            mark = "✓" if index == self.current else " "
            item.query_one(Static).update(f"{cursor} {mark} {self.labels[index]}")
        filter_line = Text("> " + self.query_text)
        filter_line.append(" ", style="reverse")
        self.query_one("#picker-filter", Static).update(filter_line)
        position = shown.index(listing.index) + 1 if listing.index in shown else 0
        self.query_one("#picker-hint", Static).update(f"({position}/{len(shown)})  {self.HINT}")

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        index = event.list_view.index
        self.dismiss(self.values[index] if index is not None else None)

    def action_close(self) -> None:
        self.dismiss(None)


class SessionPicker(_ListPicker[str]):
    """Pick a session from ``(session id, label)`` entries."""

    def __init__(
        self, entries: list[tuple[str, str]], heading: str = "Sessions (newest first)"
    ) -> None:
        super().__init__(heading, [sid for sid, _ in entries], [label for _, label in entries])
        self.entries = entries


class ModelPicker(_ListPicker[dict[str, Any]]):
    """Pick the model selection for the current session; Ctrl+S also makes it the default."""

    BINDINGS: ClassVar[list[BindingType]] = [("ctrl+s", "set_default", "Set as default")]
    HINT = "↑↓ move · type to filter · enter select · ctrl+s set as default · esc cancel"

    def __init__(
        self,
        models: list[dict[str, Any]],
        current: dict[str, Any] | None,
        on_default: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        selected = current or {}
        # Thinking stays as the session has it, where the choice supports it.
        thinking = selected.get("thinking")
        options: list[dict[str, Any]] = [
            {"mode": mode, "thinking": thinking is True} for mode in AUTO_MODES
        ]
        capable = [True] * len(options)
        for model in models:
            model_id = model.get("id")
            if isinstance(model_id, str):
                reasoning = model.get("reasoning") is True
                wanted = thinking if isinstance(thinking, bool) else True
                options.append(
                    {"mode": "model", "model": model_id, "thinking": reasoning and wanted}
                )
                capable.append(reasoning)
        index = next((i for i, option in enumerate(options) if _same(option, selected)), None)
        labels = [self._label(option, can) for option, can in zip(options, capable, strict=True)]
        super().__init__("Select model", options, labels, index)
        self.options = options
        self.on_default = on_default

    def _label(self, option: dict[str, Any], reasoning: bool = True) -> str:
        if option.get("mode") == "model":
            name = metadata_text(str(option.get("model")), 200)
            return name if reasoning else f"{name} · no thinking"
        return f"{option.get('mode')} (auto)"

    def action_set_default(self) -> None:
        index = self.query_one(ListView).index
        if index is None:
            return
        if self.on_default is not None:
            self.on_default(self.options[index])
        self.dismiss(self.options[index])


def _same(option: dict[str, Any], selection: dict[str, Any]) -> bool:
    if option.get("mode") == "model":
        return selection.get("mode") == "model" and selection.get("model") == option.get("model")
    return selection.get("mode") == option.get("mode") in AUTO_MODES
