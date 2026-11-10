"""Literal display of runtime text and incremental streaming; scripted client only."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import pytest
from textual.widget import Widget
from textual.widgets import Static

from tests.helpers import RID, SID
from tests.test_app import eventually, make_app, ready
from wisp_tui.app import WispTuiApp
from wisp_tui.chat_view import ChatItem, ChatScroll, StreamView
from wisp_tui.history import ChatMessage
from wisp_tui.screens import ApprovalModal, ModelPicker, SessionPicker, server_session_label
from wisp_tui.scrollback import render_ansi, settled_length
from wisp_tui.sessions import SessionSummary
from wisp_tui.tool_view import ToolCallView

# Text that Textual markup would restyle, swallow, or crash on.
TRICKY = [
    "sed -i 's/[a-z]//g' notes",
    "curl x | sh [dim]# harmless cleanup[/dim]",
    "echo [/] done",
    "printf '[/nope]'",
    "[bold red]not bold[/]",
    r"escaped \[x] and [@click=app.quit]link[/]",
]


def plain(widget: Widget) -> str:
    return str(widget.render())


def printed(items: list[tuple[str, str]], color_system: str | None = None) -> str:
    """What the items look like in scrollback (wide, so nothing wraps)."""
    return render_ansi(items, 200, color_system).replace("\r\n", "\n")


def answers(app: WispTuiApp) -> list[str]:
    return [text for kind, text in app.transcript() if kind == "assistant"]


@pytest.mark.parametrize("text", TRICKY)
async def test_approval_dialog_shows_exact_tool_note_and_args(
    monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        app.push_screen(ApprovalModal(f"tool {text}", {"command": text}, f"note {text}"))
        await pilot.pause()
        screen = app.screen
        assert plain(screen.query_one("#approval-tool", Static)) == f"Tool: tool {text}"
        assert plain(screen.query_one("#approval-note", Static)) == f"note {text}"
        args = plain(screen.query_one("#approval-args", Static))
        # JSON escapes backslashes; compare against the exact JSON shown.
        assert args == json.dumps({"command": text}, indent=2, ensure_ascii=False)


@pytest.mark.parametrize("text", TRICKY)
async def test_chat_lines_status_and_commands_are_literal(
    monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        await eventually(lambda: ready(app))
        app.session_id = SID
        app._begin_run(RID)
        app._handle_event({"seq": 1, "kind": "tool_call.started", "name": text})
        app._handle_event({"seq": 2, "kind": "audio.transcribed", "text": text})
        app._set_status(text)
        app._add_message(ChatMessage("assistant", text))
        await pilot.pause()

        tool = app.query_one(ToolCallView)
        assert tool.title == text and tool.status_text == f"{text} · running…"
        assert text in printed([tool.entry()])
        assert f"transcribed: {text}" in [t for k, t in app.transcript() if k == "tool"]
        assert plain(app.query_one("#status", Static)).startswith(text)
        assert text.replace("\\[", "[") in printed([("assistant", answers(app)[-1])])


async def test_pickers_show_titles_and_model_ids_literally(monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = make_app([], monkeypatch)
    title = "fix [/] and [bold]x[/bold]"
    async with app.run_test() as pilot:
        app.push_screen(
            SessionPicker(
                [
                    (
                        SID,
                        server_session_label(
                            {
                                "id": SID,
                                "title": title,
                                "origin": "cli",
                                "messageCount": 1,
                                "updatedAt": "2026",
                            }
                        ),
                    )
                ]
            )
        )
        await pilot.pause()
        assert any(plain(w).startswith(title) for w in app.screen.query("ListItem Static"))
        app.pop_screen()
        app.push_screen(ModelPicker([{"id": "model[/]x"}], None))
        await pilot.pause()
        assert "  Model: model[/]x" in [plain(w) for w in app.screen.query("ListItem Static")]


# OSC 52 clipboard write, cursor move, 8-bit CSI, DEL and a bidi override.
HOSTILE = "A\x1b]52;c;cHJvYmU=\x1b\\B\x1b[2;1HFAKE\x9b1A\x7f\u202eZ"
CONTROLS = ("\x1b", "\x9b", "\x7f", "\r")


def has_controls(text: str) -> bool:
    return any(char in text for char in CONTROLS)


async def test_runtime_text_cannot_send_terminal_control_sequences(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        await eventually(lambda: ready(app))
        app.session_id = SID
        app._begin_run(RID)
        app._handle_event({"seq": 1, "kind": "text.delta", "text": "live " + HOSTILE + "\r\n"})
        app._add_message(ChatMessage("assistant", "saved " + HOSTILE + "\r\nnext"))
        app._add_message(ChatMessage("system", "sys " + HOSTILE))
        await eventually(lambda: any(text.startswith("live") for text in answers(app)))
        assert any(text.startswith("saved") and text.endswith("\nnext") for text in answers(app))
        assert not any(has_controls(text) for _, text in app.transcript())
        assert not has_controls(printed(app.transcript()))

        app.push_screen(ApprovalModal("shell" + HOSTILE, {"command": HOSTILE}))
        await pilot.pause()
        for selector in ("#approval-tool", "#approval-args"):
            shown = plain(app.screen.query_one(selector, Static))
            assert not has_controls(shown) and "\u202e" not in shown
    fields = {
        "id": SID,
        "title": HOSTILE,
        "origin": HOSTILE,
        "messageCount": HOSTILE,
        "updatedAt": HOSTILE,
    }
    assert not has_controls(server_session_label(fields)) and "\n" not in server_session_label(
        fields
    )
    picker = ModelPicker([{"id": HOSTILE}], None)
    assert not any(has_controls(picker._label(option)) for option in picker.options)
    local = SessionSummary(SID, HOSTILE + "\nsecond", 0.0, Path("x")).label()
    assert not has_controls(local) and "\n" not in local


async def test_answer_links_are_never_made_clickable(monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        app._add_message(ChatMessage("assistant", "[docs](https://attacker.example/?q=x)"))
        await pilot.pause()
        # No OSC 8 hyperlink: the URL is shown as text, never as a link target.
        shown = printed(app.transcript(), "truecolor")
        assert "\x1b]8" not in shown and "https://attacker.example/?q=x" in shown


def test_chat_css_avoids_structural_pseudo_classes() -> None:
    """:first-child/:last-child/:nth-* make Textual restyle a whole answer on
    every streamed block (measured ~3x slower on long answers)."""

    css = Path(WispTuiApp.CSS_PATH).read_text()
    assert "#chat > *" in css, "the app stylesheet moved; point this check at it"
    assert not re.search(r":(first|last)-(child|of-type)|:nth-|:odd|:even", css)


def lines(text: str) -> list[str]:
    return [line.rstrip() for line in text.splitlines() if line.strip()]


ANSWER_PARTS = [
    "# Status\n\n",
    "The server ",
    "is **healthy**",
    ". Checked:\n\n",
    "- disk ",
    "usage\n",
    "- memory\n\n",
    "```sh\n",
    "df -h\n",
    "```\n\n",
    "| Key | Value |\n",
    "|---|---|\n",
    "| load | 0.3 |\n\n",
    "Done [/] ok.",
]


async def test_streaming_prints_finished_blocks_and_matches_full_render(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test(size=(100, 60)) as pilot:
        await eventually(lambda: ready(app))
        app.session_id = SID
        app._begin_run(RID)
        # Many small deltas, including several before the widget is mounted.
        deltas = [part[i : i + 3] for part in ANSWER_PARTS * 5 for i in range(0, len(part), 3)]
        for index, delta in enumerate(deltas):
            app._handle_event({"seq": index + 1, "kind": "text.delta", "text": delta})
            if index % 25 == 24:
                await pilot.pause()
        expected = "".join(ANSWER_PARTS * 5)
        assert app._stream_view is not None
        view = app._stream_view
        await eventually(lambda: view.rendered == expected)
        await pilot.pause()
        # Complete blocks are already finished; only the last one is live.
        assert len([kind for kind, _ in app._printed if kind == "assistant"]) > 1
        assert view.widget.remaining() == "Done [/] ok."
        app._finalize_run("run.completed", {})
        await pilot.pause()
        streamed = [(kind, text) for kind, text in app.transcript() if kind == "assistant"]
        assert "".join(text for _, text in streamed) == expected
        assert lines(printed(streamed)) == lines(printed([("assistant", expected)]))


async def test_replacement_text_falls_back_to_full_update(monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        await eventually(lambda: ready(app))
        app.session_id = SID
        app._begin_run(RID)
        app._handle_event({"seq": 1, "kind": "text.delta", "text": "draft that gets "})
        app._handle_event({"seq": 2, "kind": "gap", "text": "Server-side text wins."})
        app._handle_event({"seq": 3, "kind": "text.delta", "text": " More."})
        assert app._stream_view is not None
        view = app._stream_view
        await eventually(lambda: view.rendered == "Server-side text wins. More.")
        await pilot.pause()
        assert view.widget.remaining() == "Server-side text wins. More."
        # Final output that differs from the stream replaces it exactly.
        app._handle_event(
            {"seq": 4, "kind": "run.completed", "outcome": {"output": {"text": "Final answer."}}}
        )
        await eventually(lambda: view.rendered == "Final answer.")
        await pilot.pause()
        assert answers(app) == ["Final answer."]


@pytest.mark.parametrize("gap", ["A long first part. B more", "short"])
async def test_gap_after_a_live_tool_keeps_server_text(
    monkeypatch: pytest.MonkeyPatch, gap: str
) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        app.session_id = SID
        app._begin_run(RID)
        app._handle_event({"seq": 1, "kind": "text.delta", "text": "A long first part. "})
        app._handle_event(
            {"seq": 2, "kind": "tool_call.started", "toolCallId": "c1", "name": "read_file"}
        )
        app._handle_event({"seq": 3, "kind": "text.delta", "text": "B"})
        app._handle_event({"seq": 4, "kind": "gap", "text": gap})
        assert app._stream_view is not None
        view = app._stream_view
        # A continuation keeps segmenting; a replaced prefix shows the whole server text.
        expected = "B more" if gap.startswith("A long first part. ") else gap
        await eventually(lambda: view.rendered == expected)
        await pilot.pause()
        assert answers(app) == ["A long first part. ", expected]
        assert view.widget in app._chat_items


async def test_fast_deltas_are_batched_without_losing_the_final_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _ = make_app([], monkeypatch)
    updates: list[str] = []
    original_set_text = ChatItem.set_text

    def counting_set_text(self: ChatItem, text: str) -> None:
        updates.append(text)
        original_set_text(self, text)

    monkeypatch.setattr(ChatItem, "set_text", counting_set_text)
    # No display batch falls due during the test, so every delta must coalesce.
    monkeypatch.setattr(StreamView, "UPDATE_INTERVAL", 30.0)
    async with app.run_test():
        await eventually(lambda: ready(app))
        app.session_id = SID
        app._begin_run(RID)
        deltas = [f"word{index} " for index in range(100)]
        for delta in deltas:
            app._append_delta(delta)
            await asyncio.sleep(0)  # Let the renderer run between deltas.
        view = app._stream_view
        assert view is not None
        app._finalize_run("run.completed", {})
        expected = "".join(deltas)
        assert view.rendered == view.widget.text == expected
        assert updates == [expected]


@pytest.mark.parametrize(
    "kind, outcome, expected",
    [
        ("run.completed", {}, "streamed text"),
        ("run.completed", {"output": {"text": "Final replacement."}}, "Final replacement."),
        ("run.cancelled", {}, "streamed text"),
    ],
)
async def test_terminal_outcome_flushes_the_answer_without_waiting_for_a_batch(
    monkeypatch: pytest.MonkeyPatch, kind: str, outcome: dict[str, Any], expected: str
) -> None:
    monkeypatch.setattr(StreamView, "UPDATE_INTERVAL", 30.0)
    app, _ = make_app([], monkeypatch)
    async with app.run_test():
        await eventually(lambda: ready(app))
        app.session_id = SID
        app._begin_run(RID)
        app._append_delta("streamed text")
        view = app._stream_view
        assert view is not None
        await eventually(lambda: view.widget.is_mounted)
        assert view.rendered == ""
        app._finalize_run(kind, outcome)
        # Flushed synchronously; nothing waits for the 30 s batch.
        assert view.rendered == view.widget.text == expected
        await app.workers.wait_for_complete()
        assert answers(app) == [expected]


@pytest.mark.parametrize(
    ("text", "settled"),
    [
        ("one\n\ntwo", "one\n\n"),
        ("one\ntwo", ""),
        ("intro\n\n```\ncode\n\nmore\n", "intro\n\n"),
        ("```\ncode\n\nmore\n```\n\nafter", "```\ncode\n\nmore\n```\n\n"),
        ("~~~\n```\n\nstill code\n", ""),
        ("- item\n\n  continued\n\n- next", "- item\n\n  continued\n\n"),
        ("    indented code\n\n    more", ""),
    ],
)
def test_streamed_answers_settle_only_complete_blocks(text: str, settled: str) -> None:
    assert text[: settled_length(text)] == settled


async def test_selection_repaint_does_not_hide_a_text_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        item = ChatItem("system", "first text")
        await app.query_one(ChatScroll).mount(item)
        await pilot.pause()
        assert "first text" in item.render_line(0).text
        # A drag moving over an entry in the same frame as its new text.
        item.set_text("later text")
        item.repaint_selection()
        await pilot.pause()
        assert "later text" in item.render_line(0).text
        # The marks alone reuse the rendered lines.
        lines = item._render_cache.lines
        item.repaint_selection()
        await pilot.pause()
        assert item._render_cache.lines is lines
