"""Reading older output must survive new work, without stealing editor keys."""
from __future__ import annotations

import pytest
from textual import events
from textual.containers import VerticalScroll
from textual.widget import Widget
from textual.widgets import Static

from tests.helpers import RID, SID
from tests.test_app import eventually, make_app
from wisp_tui.app import ApprovalModal, WispTuiApp
from wisp_tui.chat_view import ChatScroll, ResumeFollow
from wisp_tui.editor import PromptInput
from wisp_tui.history import ChatMessage


def wheel_up(widget: Widget) -> None:
    widget.post_message(events.MouseScrollUp(
        widget, x=1, y=1, delta_x=0, delta_y=0, button=0,
        shift=False, meta=False, ctrl=False))


def transcript(app: WispTuiApp) -> ChatScroll:
    for index in range(15):
        app._add_message(ChatMessage("assistant", f"Paragraph {index}\n\nMore text {index}."))
    return app.query_one("#chat", ChatScroll)


@pytest.mark.parametrize("width", [40, 100])
async def test_wheel_pauses_streaming_and_f4_resumes_without_changing_draft(
        monkeypatch: pytest.MonkeyPatch, width: int) -> None:
    app, client = make_app([], monkeypatch)
    async with app.run_test(size=(width, 30)) as pilot:
        await eventually(lambda: "ready" in app._status_state)
        chat = transcript(app)
        app.session_id = SID
        app._turn_started(RID)
        app._handle_event({"seq": 1, "kind": "text.delta", "text": "Start\n\n"})
        await pilot.pause()
        assert chat.scroll_y == chat.max_scroll_y > 0
        editor = app.query_one(PromptInput)
        editor.value = "draft\nkeep this"
        wheel_up(chat)
        await pilot.pause()
        assert not bool(chat.following)
        assert app.query_one(ResumeFollow).display
        position = chat.scroll_y
        text = "\n\n".join(f"Stream paragraph {i}" for i in range(50))
        app._handle_event({"seq": 2, "kind": "text.delta", "text": text})
        await eventually(lambda: app._stream_view is not None
                         and app._stream_view.rendered.endswith(text))
        await pilot.pause()
        assert chat.scroll_y == position < chat.max_scroll_y
        app._finalize_run("run.completed", {})
        await pilot.pause()
        assert not bool(chat.following) and chat.scroll_y == position
        await pilot.press("f4")
        await pilot.pause()
        assert bool(chat.following) and chat.scroll_y == chat.max_scroll_y
        assert not app.query_one(ResumeFollow).display
        assert editor.value == "draft\nkeep this" and editor.has_focus
        assert all(method == "GET" for method, _, _ in client.calls)


@pytest.mark.parametrize("key", ["up", "pageup", "home"])
async def test_chat_navigation_pauses_and_click_or_end_resumes(
        monkeypatch: pytest.MonkeyPatch, key: str) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        chat = transcript(app)
        await pilot.pause()
        chat.focus()
        await pilot.press(key)
        await pilot.pause()
        assert not bool(chat.following) and chat.scroll_y < chat.max_scroll_y
        position = chat.scroll_y
        app._sysline("A new status message")
        await pilot.pause()
        assert chat.scroll_y == position
        if key == "home":
            await pilot.press("end")
        else:
            await pilot.click("#resume-follow")
        await pilot.pause()
        assert bool(chat.following) and chat.scroll_y == chat.max_scroll_y


async def test_queued_follow_cannot_override_a_later_scroll_up(
        monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        chat = transcript(app)
        await pilot.pause()
        app._scroll_down()
        chat.action_page_up()
        await pilot.pause()
        assert not bool(chat.following) and chat.scroll_y < chat.max_scroll_y


async def test_short_chat_and_resize_keep_following_until_navigation_is_possible(
        monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test(size=(100, 30)) as pilot:
        chat = app.query_one(ChatScroll)
        app._sysline("Short transcript")
        await pilot.pause()
        wheel_up(chat)
        await pilot.pause()
        assert bool(chat.following)
        transcript(app)
        await pilot.pause()
        await pilot.resize_terminal(40, 20)
        await pilot.pause()
        assert bool(chat.following) and chat.scroll_y == chat.max_scroll_y > 0


async def test_user_navigation_wins_over_pending_position_restoration(
        monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        chat = transcript(app)
        await pilot.pause()
        wheel_up(chat)
        await pilot.pause()
        app._sysline("An asynchronous mount")
        chat.action_scroll_home()
        await eventually(lambda: chat.scroll_y == 0)
        await pilot.pause()
        assert not bool(chat.following) and chat.scroll_y == 0


async def test_editor_and_tool_body_scroll_do_not_pause_the_transcript(
        monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        chat = transcript(app)
        editor = app.query_one(PromptInput)
        editor.value = "\n".join(f"draft {i}" for i in range(20))
        await pilot.pause()
        await pilot.press("up", "home", "pageup")
        wheel_up(editor)
        await pilot.pause()
        assert bool(chat.following)
        tool = app._tool_event({"name": "details", "args": "\n".join(
            f"line {i}" for i in range(60))}, completed=True)
        tool.collapsed = False
        await pilot.pause()
        body = tool.query_one(VerticalScroll)
        body.scroll_end(animate=False)
        await pilot.pause()
        assert body.scroll_y > 0
        wheel_up(body)
        await pilot.pause()
        assert body.scroll_y < body.max_scroll_y
        assert bool(chat.following)
        tool.query_one("CollapsibleTitle", Static).focus()
        await pilot.pause()
        assert not bool(chat.following)


async def test_resize_and_approval_keep_follow_paused(monkeypatch: pytest.MonkeyPatch) -> None:
    app, client = make_app([], monkeypatch)
    async with app.run_test(size=(100, 30)) as pilot:
        chat = transcript(app)
        await pilot.pause()
        wheel_up(chat)
        await pilot.pause()
        await pilot.resize_terminal(40, 24)
        assert not bool(chat.following)
        position = chat.scroll_y
        app.push_screen(ApprovalModal("scripted_tool", {}))
        await pilot.pause()
        await pilot.press("f4")
        app._sysline("More output behind the approval")
        await pilot.pause()
        assert isinstance(app.screen, ApprovalModal)
        assert not bool(chat.following) and chat.scroll_y == position
        await pilot.press("escape")
        await pilot.pause()
        assert not bool(chat.following)
        await pilot.press("f4")
        await pilot.pause()
        assert bool(chat.following) and chat.scroll_y == chat.max_scroll_y
        assert all(method == "GET" for method, _, _ in client.calls)


async def test_trimming_keeps_a_surviving_reading_item_in_place(
        monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = make_app([], monkeypatch)
    monkeypatch.setattr(app, "MAX_CHAT_ITEMS", 15)
    async with app.run_test() as pilot:
        chat = transcript(app)
        await pilot.pause()
        wheel_up(chat)
        await pilot.pause()
        item = next(child for child in chat.children if child.region.y >= chat.region.y
                    and child.region.bottom <= chat.region.bottom)
        y = item.region.y
        for i in range(3):
            app._sysline(f"New status {i}")
        await eventually(lambda: chat._reading_position is None and not chat._pending_layout)
        await pilot.pause()
        assert not bool(chat.following) and item.is_attached
        assert item.region.y == y
        assert len(app._chat_items) == 15


async def test_tools_inserted_above_the_answer_preserve_reading_position(
        monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        app.session_id = SID
        app._turn_started(RID)
        app._handle_event({"seq": 1, "kind": "text.delta", "text": "\n\n".join(
            f"Paragraph {i}" for i in range(60))})
        await eventually(lambda: app._stream_view is not None and app._stream_view.rendered)
        await pilot.pause()
        chat = app.query_one(ChatScroll)
        wheel_up(chat)
        await pilot.pause()
        assert app._stream_view is not None
        answer = app._stream_view.widget
        y = answer.region.y
        tool = app._tool_event({"name": "new tool", "toolCallId": "new-call"}, completed=False)
        await eventually(lambda: chat._reading_position is None and not chat._pending_layout)
        await pilot.pause()
        assert not bool(chat.following) and answer.region.y == y
        tool.collapsed = False
        await pilot.pause()
        y = answer.region.y
        app._apply_tool_history([ChatMessage("tool", "Result line\n" * 50,
                                            tool_call_id="new-call")], (tool,))
        await eventually(lambda: chat._reading_position is None and not chat._pending_layout)
        await pilot.pause()
        assert tool.output_text is not None
        assert not bool(chat.following) and answer.region.y == y


@pytest.mark.parametrize("command", ["/clear", "/new"])
async def test_explicit_transcript_reset_resumes_following(
        monkeypatch: pytest.MonkeyPatch, command: str) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        chat = transcript(app)
        await pilot.pause()
        wheel_up(chat)
        await pilot.pause()
        app.query_one(PromptInput).value = command
        await pilot.press("enter")
        await pilot.pause()
        assert bool(chat.following) and not app.query_one(ResumeFollow).display


async def test_checkpoint_refresh_does_not_replace_the_paused_transcript(
        monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        chat = transcript(app)
        app.session_id = SID
        await pilot.pause()
        wheel_up(chat)
        await pilot.pause()
        children = list(chat.children)
        position = chat.scroll_y
        token = object()
        app._history_refresh_token = token
        question = ChatMessage("user", "question", "q1")
        answer = ChatMessage("assistant", "answer", "a1")
        app._history_refreshed(token, SID, app._chat_epoch, [question],
                               [question, answer], "answer")
        await pilot.pause()
        assert list(chat.children) == children
        assert not bool(chat.following) and chat.scroll_y == position
