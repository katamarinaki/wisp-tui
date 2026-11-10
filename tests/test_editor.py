"""Multiline editing and paste are local until an explicit send key."""

from __future__ import annotations

from collections import deque

import pytest
from textual import events
from textual.widgets import Static
from textual.widgets.text_area import Selection

from tests.helpers import SID, RequestGate
from tests.test_app import chat_text, eventually, make_app, ready
from tests.test_submissions import COMPLETED, pending
from wisp_tui.client import ClientError
from wisp_tui.editor import PromptInput
from wisp_tui.history import ChatMessage
from wisp_tui.screens import ApprovalModal
from wisp_tui.submissions import MAX_DRAFT_BYTES

RUNS = f"/v1/sessions/{SID}/runs"


@pytest.mark.parametrize("newline", ["ctrl+j", "shift+enter"])
async def test_newline_edits_but_enter_sends_the_whole_prompt_once(
    monkeypatch: pytest.MonkeyPatch, newline: str
) -> None:
    app, client = make_app(COMPLETED, monkeypatch)
    async with app.run_test() as pilot:
        await eventually(lambda: ready(app))
        editor = app.query_one("#input", PromptInput)
        await pilot.press("h", "i", newline, "t", "h", "e", "r", "e")
        assert editor.value == "hi\nthere"
        assert editor.cursor_location == (1, 5)
        await app.workers.wait_for_complete()
        assert not any(method == "POST" for method, _, _ in client.calls)
        await pilot.press("enter")
        await eventually(lambda: app._status_text == "")
        bodies = [body for method, path, body in client.calls if (method, path) == ("POST", RUNS)]
        assert len(bodies) == 1 and bodies[0] is not None
        assert bodies[0]["input"] == "hi\nthere"
        assert editor.value == ""
        assert "hi\nthere" in chat_text(app)


@pytest.mark.parametrize(
    "payload, expected",
    [
        ("first\nsecond\n", "first\nsecond\n"),
        ("first\r\nsecond\r\n", "first\nsecond\n"),
        ("first\rsecond\r", "first\nsecond\n"),
        ("/quit\n/new other\n/cancel\n", "/quit\n/new other\n/cancel\n"),
        ("a\n\nb\n", "a\n\nb\n"),
        ("日本語 🐈\n\tbrackets [/]", "日本語 🐈\n\tbrackets [/]"),
    ],
)
async def test_paste_is_literal_and_never_dispatches_commands_or_submits(
    monkeypatch: pytest.MonkeyPatch, payload: str, expected: str
) -> None:
    app, client = make_app(COMPLETED, monkeypatch)
    async with app.run_test() as pilot:
        await eventually(lambda: ready(app))
        editor = app.query_one("#input", PromptInput)
        app._add_message(ChatMessage("assistant", "Keep this view"))
        editor.post_message(events.Paste(payload))
        await eventually(lambda: editor.value == expected)
        assert "Keep this view" in chat_text(app)
        assert app.session_id is None
        await app.workers.wait_for_complete()
        assert not any(method == "POST" for method, _, _ in client.calls)
        await pilot.press("enter")
        await eventually(lambda: app._status_text == "")
        bodies = [body for method, path, body in client.calls if (method, path) == ("POST", RUNS)]
        assert len(bodies) == 1 and bodies[0] is not None
        assert bodies[0]["input"] == expected.strip()


async def test_paste_replaces_selection_and_can_be_undone_without_sending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        editor = app.query_one("#input", PromptInput)
        editor.value = "before replace after"
        editor.selection = Selection((0, 7), (0, 14))
        editor.post_message(events.Paste("one\ntwo"))
        await eventually(lambda: editor.value == "before one\ntwo after")
        assert editor.cursor_location == (1, 3)
        await pilot.press("ctrl+z")
        assert editor.value == "before replace after"
        await pilot.press("ctrl+y")
        assert editor.value == "before one\ntwo after"
        await app.workers.wait_for_complete()
        assert not any(method == "POST" for method, _, _ in client.calls)


async def test_local_clipboard_paste_does_not_trigger_slash_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        editor = app.query_one("#input", PromptInput)
        app.copy_to_clipboard("/quit\n/clear\n")
        await pilot.press("ctrl+v")
        assert editor.value == "/quit\n/clear\n"
        await app.workers.wait_for_complete()
        assert not any(method == "POST" for method, _, _ in client.calls)


async def test_single_line_command_requires_enter_and_still_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        editor = app.query_one("#input", PromptInput)
        app._add_message(ChatMessage("assistant", "Keep until explicit send"))
        editor.post_message(events.Paste("/help"))
        await eventually(lambda: editor.value == "/help")
        await pilot.pause()
        assert "Keep until explicit send" in chat_text(app)
        assert "chat with the local Wisp runtime" not in chat_text(app)
        await pilot.press("enter")
        await eventually(lambda: "chat with the local Wisp runtime" in chat_text(app))
        assert editor.value == ""
        await app.workers.wait_for_complete()
        assert not any(method == "POST" for method, _, _ in client.calls)


async def test_blank_multiline_input_is_not_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    app, client = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        editor = app.query_one("#input", PromptInput)
        editor.value = " \n\t\n"
        await pilot.press("enter")
        assert editor.value == " \n\t\n"
        await app.workers.wait_for_complete()
        assert not any(method == "POST" for method, _, _ in client.calls)


async def test_ctrl_c_still_cancels_instead_of_copying_selected_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        editor = app.query_one("#input", PromptInput)
        editor.value = "keep\nthis draft"
        editor.selection = Selection((0, 0), (0, 4))
        app._pending_submission = pending()
        await pilot.press("ctrl+c")
        assert app._cancel_when_acknowledged
        assert "will cancel" in app._status_text
        assert app.clipboard == ""
        assert editor.value == "keep\nthis draft"


async def test_multiline_ack_preserves_newer_paste_and_retry_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client = make_app(COMPLETED, monkeypatch)
    client.replies[("POST", RUNS)] = deque([ClientError("runtime_timeout")])
    gate = RequestGate("POST", RUNS)
    client.request_hook = gate
    async with app.run_test() as pilot:
        await eventually(lambda: ready(app))
        app.session_id = SID
        editor = app.query_one("#input", PromptInput)
        editor.value = "original\nmultiline task"
        try:
            await pilot.press("enter")
            await eventually(gate.entered.is_set)
            editor.value = "newer\ndraft"
            editor.post_message(events.Paste("\nmore text"))
            await pilot.pause()
            await pilot.press("enter")
            assert len([call for call in client.calls if call[:2] == ("POST", RUNS)]) == 1
        finally:
            gate.release.set()
        await eventually(lambda: "submission error" in app._status_text)
        app._handle_command("/retry")
        await eventually(lambda: app._status_text == "")
        bodies = [body for method, path, body in client.calls if (method, path) == ("POST", RUNS)]
        assert len(bodies) == 2 and bodies[0] == bodies[1]
        assert editor.value == "newer\ndraft\nmore text"


@pytest.mark.parametrize("width", [40, 100])
async def test_editor_grows_wraps_caps_scrolls_and_shrinks(
    monkeypatch: pytest.MonkeyPatch, width: int
) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test(size=(width, 30)) as pilot:
        editor = app.query_one("#input", PromptInput)
        assert editor.region.height == 3
        editor.value = "first\nsecond\nthird"
        await pilot.pause()
        assert editor.region.height == 5
        editor.value = "long " * 100
        await pilot.pause()
        assert 3 < editor.region.height <= 8
        editor.value = "\n".join(f"line {index}" for index in range(80))
        await pilot.pause()
        assert editor.region.height == 8
        assert editor.scroll_y > 0
        await pilot.press("ctrl+home")
        await pilot.pause()
        assert editor.cursor_location == (0, 0)
        await pilot.resize_terminal(32, 30)
        assert editor.value.endswith("line 79")
        editor.value = ""
        await pilot.pause()
        assert editor.region.height == 3


async def test_large_unicode_paste_is_kept_without_creating_a_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        await eventually(lambda: ready(app))
        editor = app.query_one("#input", PromptInput)
        payload = "🐈" * (MAX_DRAFT_BYTES // 4 + 1) + "\nend"
        editor.post_message(events.Paste(payload))
        await eventually(lambda: editor.value == payload)
        await pilot.press("enter")
        await eventually(lambda: "safe recovery limit" in chat_text(app))
        assert editor.value == payload
        assert app.session_id is None
        await app.workers.wait_for_complete()
        assert not any(method == "POST" for method, _, _ in client.calls)


@pytest.mark.parametrize("theme", ["textual-dark", "textual-light"])
async def test_editor_selection_and_matching_brackets_have_no_painted_background(
    monkeypatch: pytest.MonkeyPatch, theme: str
) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        app.theme = theme
        editor = app.query_one("#input", PromptInput)
        editor.value = "[one]\ntwo"
        editor.selection = Selection((0, 0), (1, 2))
        await pilot.pause()
        for row in range(editor.content_size.height):
            for segment in editor.render_line(row):
                if segment.style is not None and segment.style.bgcolor is not None:
                    assert segment.style.bgcolor.is_default


async def test_arrow_keys_and_backspace_edit_lines_without_submitting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        editor = app.query_one("#input", PromptInput)
        editor.value = "one\ntwo"
        await pilot.press("home", "backspace")
        assert editor.value == "onetwo"
        await pilot.press("ctrl+j", "up", "home", "delete")
        assert editor.value == "ne\ntwo"
        await app.workers.wait_for_complete()
        assert not any(method == "POST" for method, _, _ in client.calls)


async def test_approval_modal_does_not_send_underlying_editor_or_lose_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        editor = app.query_one("#input", PromptInput)
        editor.value = "keep\nmy draft"
        await pilot.press("f1")
        modal = ApprovalModal("example", {})
        app.push_screen(modal)
        await eventually(lambda: modal.is_mounted)
        await pilot.press("enter", "escape")
        await eventually(lambda: app.screen is not modal)
        assert editor.value == "keep\nmy draft"
        await app.workers.wait_for_complete()
        assert not any(method == "POST" for method, _, _ in client.calls)


async def test_slash_lists_matching_commands_and_tab_or_right_completes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        await eventually(lambda: ready(app))
        editor = app.query_one("#input", PromptInput)
        hints = app.query_one("#command-hints", Static)

        def shown() -> str:
            return str(hints.render()) if hints.display else ""

        await pilot.press("/", "r", "e")
        listed = shown()
        assert all(name in listed for name in ("/resume", "/retry", "/reconnect"))
        assert "/help" not in listed
        await pilot.press("tab")
        assert editor.value == "/resume"
        await pilot.press("space")
        assert shown() == "" and editor.suggestion == ""
        await pilot.press(*["backspace"] * 7, "h", "e", "right")
        assert editor.value == "/help"
        await pilot.press("ctrl+j", "/", "h")
        assert editor.suggestion == "" and shown() == ""
