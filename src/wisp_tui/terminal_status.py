"""iTerm2 tab subtitles and dots (OSC 21337), independent of terminal titles."""

from __future__ import annotations

from enum import Enum


class TabStatus(Enum):
    CONNECTING = "Connecting"
    IDLE = "Idle"
    WORKING = "Working"
    WAITING = "Waiting for approval"
    RECONNECTING = "Reconnecting"
    CANCELLING = "Cancelling"
    CANCELLED = "Cancelled"
    ERROR = "Error"
    ATTENTION = "Needs attention"


_COLORS = {
    TabStatus.CONNECTING: "#8e8e93",
    TabStatus.IDLE: "#34c759",
    TabStatus.WORKING: "#0a84ff",
    TabStatus.WAITING: "#ffcc00",
    TabStatus.RECONNECTING: "#ff9f0a",
    TabStatus.CANCELLING: "#ff9f0a",
    TabStatus.CANCELLED: "#8e8e93",
    TabStatus.ERROR: "#ff453a",
    TabStatus.ATTENTION: "#ffcc00",
}


def tab_status_sequence(status: TabStatus | None) -> str:
    """Only fixed labels/colors reach the terminal; no runtime or chat text."""
    label, color = (status.value, _COLORS[status]) if status is not None else ("", "")
    return f"\x1b]21337;status={label};indicator={color};status-color=\x1b\\"
