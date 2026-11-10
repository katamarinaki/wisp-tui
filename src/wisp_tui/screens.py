"""Modal screens: approval prompts and the session and model pickers."""

from __future__ import annotations

from typing import Any, ClassVar

from textual.app import ComposeResult
from textual.binding import BindingType
from textual.containers import Vertical, VerticalScroll
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


class ApprovalModal(ModalScreen[str | None]):
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
        with Vertical(id="approval-dialog"):
            # Approval details must be shown exactly as the runtime sent them:
            # Textual markup would hide/alter bracketed text (or crash).
            yield Static("Wisp asks for approval", id="approval-title", markup=False)
            with VerticalScroll(id="approval-details"):
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


class SessionPicker(ModalScreen[str | None]):
    """Pick a session from ``(session id, label)`` entries."""

    BINDINGS: ClassVar[list[BindingType]] = [
        ("escape", "close", "Close"),
        ("ctrl+c", "app.cancel", "Cancel/exit"),
    ]

    def __init__(
        self, entries: list[tuple[str, str]], heading: str = "Sessions (newest first)"
    ) -> None:
        super().__init__()
        self.entries = entries
        self.heading = heading

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

    BINDINGS: ClassVar[list[BindingType]] = [
        ("escape", "close", "Close"),
        ("ctrl+c", "app.cancel", "Cancel/exit"),
    ]

    def __init__(self, models: list[dict[str, Any]], current: dict[str, Any] | None) -> None:
        super().__init__()
        self.options: list[dict[str, Any]] = [
            {"mode": mode, "thinking": False} for mode in AUTO_MODES
        ]
        for model in models:
            model_id = model.get("id")
            if isinstance(model_id, str):
                reasoning = model.get("reasoning") is True
                self.options.append({"mode": "model", "model": model_id, "thinking": reasoning})
        self.current = current or {}

    def _label(self, option: dict[str, Any]) -> str:
        if option.get("mode") == "model":
            name = metadata_text(str(option.get("model")), 200)
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
            return self.current.get("mode") == "model" and self.current.get("model") == option.get(
                "model"
            )
        return self.current.get("mode") == option.get("mode") in AUTO_MODES

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        index = event.list_view.index if event.list_view.index is not None else -1
        if 0 <= index < len(self.options):
            self.dismiss(self.options[index])
        else:
            self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)
