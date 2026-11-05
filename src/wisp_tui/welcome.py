"""A transient welcome for an empty chat, never part of the transcript."""

from __future__ import annotations

from rich.console import RenderableType
from rich.table import Table
from rich.text import Text
from textual.widgets import Static

from . import __version__

WISP = """          ▄
      ▄  ▟█▙
     ▟█▙▟███▙▖
 ▄  ▟█████████▙
▟█ ▟███████████
▜██████████████
 ▜█████▙▀▟████▛
  ▀██████████▙
    ▜████▀ ▜██▙
     ▀███▄▄█▛▝▀"""


def wisp_art() -> Text:
    """Foreground-only pixels: a waving flame, blue eyes, and a little smile."""
    art = Text(WISP)
    shades = (
        "#bad7f8",
        "#b2d2f7",
        "#aaccf5",
        "#a2c7f4",
        "#9ac1f2",
        "#92bcf0",
        "#8bb7ee",
        "#83b2ec",
        "#7cadea",
        "#75a8e8",
    )
    # The one-row smile is cut into the foreground blocks, below a clear cheek row.
    face = {4: (6, 10)}
    offset = 0
    for row, line in enumerate(WISP.splitlines()):
        art.stylize(shades[row], offset, offset + len(line))
        for column in face.get(row, ()):
            art.stylize("#285cc4", offset + column, offset + column + 1)
        offset += len(line) + 1
    return art


class Welcome(Static):
    """A small text mascot and version that fit a narrow SSH terminal."""

    def render(self) -> RenderableType:
        title = Text("Wisp\n", style="bold")
        title.append(f"wisp-tui v{__version__}", style="not bold dim")
        header = Table.grid(padding=(0, 3), expand=True)
        header.add_column(width=16, no_wrap=True)
        header.add_column(ratio=1, vertical="middle")
        header.add_row(wisp_art(), title)
        return header

    def on_resize(self) -> None:
        self.refresh(layout=True)
