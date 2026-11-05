"""Chat items rendered for the terminal's own scrollback (and the live area).

Finished items are printed once above the inline app, so the terminal owns
scrolling, selection and copying, and the output keeps its look after exit.
"""

from __future__ import annotations

import io
import re
import sys
from typing import ClassVar, Literal

from rich.console import (
    Console,
    ConsoleOptions,
    Group,
    RenderableType,
    RenderResult,
)
from rich.markdown import CodeBlock, Heading, Markdown
from rich.padding import Padding
from rich.segment import Segment
from rich.style import Style
from rich.syntax import Syntax
from rich.text import Text
from rich.theme import Theme

# Match app.tcss: default foreground, bold headings, no filled code spans.
THEME = Theme(
    {
        "markdown.code": "bold",
        "markdown.code_block": "none",
        "markdown.list": "none",
        "markdown.item.number": "none",
        "markdown.block_quote": "none",
        "markdown.table.border": "dim",
        "markdown.table.header": "bold",
        **{f"markdown.h{level}": "bold" for level in range(1, 7)},
    }
)
# The owner-approved prompt fill: xterm-256 #23 (48;5;23), as in the app.
USER_STYLE = Style.parse("bold #dadada on color(23)")
TOOL_COLORS = {"tool-call-ok": "cyan", "tool-call-failed": "red"}


class _LeftHeading(Heading):
    LEVEL_ALIGN: ClassVar = dict.fromkeys(("h1", "h2", "h3", "h4", "h5", "h6"), "left")


class _PlainCodeBlock(CodeBlock):
    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        yield Syntax(
            str(self.text).rstrip(),
            self.lexer_name,
            theme="ansi_dark",
            word_wrap=True,
            background_color="default",
        )


class _AnswerMarkdown(Markdown):
    elements: ClassVar = {
        **Markdown.elements,
        "heading_open": _LeftHeading,
        "fence": _PlainCodeBlock,
        "code_block": _PlainCodeBlock,
    }


class _LeftBar:
    """A tool entry's ` ▌ ` rule in front of every rendered row."""

    def __init__(self, renderable: RenderableType, style: str) -> None:
        self.renderable = renderable
        self.style = Style.parse(style)

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        width = max(1, options.max_width - 3)
        for line in console.render_lines(self.renderable, options.update_width(width), pad=False):
            yield Segment(" ")
            yield Segment("▌", self.style)
            yield Segment(" ")
            yield from line
            yield Segment.line()


def _tool_entry(kind: str, text: str) -> RenderableType:
    # Tool entries are one title line, optional output tail, one status line.
    color = TOOL_COLORS.get(kind)
    accent = color or "dim"
    lines = text.split("\n")
    parts: list[RenderableType] = [Text(lines[0], style=accent)]
    if len(lines) > 2:
        parts.append(Text("\n".join(lines[1:-1]), style="dim", no_wrap=True, overflow="ellipsis"))
    if len(lines) > 1:
        parts.append(Text(lines[-1], style=accent))
    return _LeftBar(Group(*parts), color or "bright_black")


def render_item(kind: str, text: str) -> RenderableType:
    if kind == "user":
        return Padding(Text(text), (1, 2), style=USER_STYLE, expand=True)
    if kind == "assistant":
        markdown = _AnswerMarkdown(text, code_theme="ansi_dark", hyperlinks=False)
        return Padding(markdown, (0, 1), expand=False)
    if kind.startswith("tool-call"):
        return _tool_entry(kind, text)
    style = "red" if kind == "error" else "dim"
    return Padding(Text(text, style=style), (0, 1), expand=False)


_COLOR_SYSTEMS: dict[str, Literal["standard", "256", "truecolor", "windows"]] = {
    "standard": "standard",
    "256": "256",
    "truecolor": "truecolor",
    "windows": "windows",
}
_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})")
_TRAILING_PAD = re.compile(r" +(?=\n)")


def settled_length(text: str) -> int:
    """Length of the leading complete Markdown blocks of a streamed answer.

    A block is complete once a later block has started at column 0 after a
    blank line outside a code fence. Indented text after a blank line can
    still belong to a list item or code block, so it never starts a new one.
    """
    fence = ""
    cut = blank_end = offset = 0
    for line in text.splitlines(keepends=True):
        end = offset + len(line)
        match = _FENCE.match(line)
        if fence:
            if (
                match
                and match.group(1)[0] == fence[0]
                and len(match.group(1)) >= len(fence)
                and not line[match.end() :].strip()
            ):
                fence = ""
        elif not line.strip():
            blank_end = end
        else:
            if blank_end and not line[0].isspace():
                cut = blank_end
            blank_end = 0
            if match:
                fence = match.group(1)
        offset = end
    return cut


def _print_items(console: Console, items: list[tuple[str, str]]) -> None:
    for kind, raw in items:
        text = raw.strip("\n")
        if text.strip():
            console.print(render_item(kind, text))
            console.print()


def render_ansi(items: list[tuple[str, str]], width: int, color_system: str | None) -> str:
    """Items as raw terminal output with CRLF line ends (the TTY is in raw mode)."""
    system = _COLOR_SYSTEMS.get(color_system or "")
    console = Console(
        file=io.StringIO(),
        width=max(width, 20),
        color_system=system,
        force_terminal=system is not None,
        highlight=False,
        theme=THEME,
    )
    with console.capture() as capture:
        _print_items(console, items)
    return _TRAILING_PAD.sub("", capture.get()).replace("\n", "\r\n")


def print_transcript(items: list[tuple[str, str]]) -> None:
    console = Console(highlight=False, theme=THEME)
    with console.capture() as capture:
        _print_items(console, items)
    sys.stdout.write(_TRAILING_PAD.sub("", capture.get()))
    sys.stdout.flush()
