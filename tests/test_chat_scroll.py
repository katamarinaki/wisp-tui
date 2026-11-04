"""Reading older output must survive new work, without stealing editor keys."""

from __future__ import annotations

import pytest
from textual import events
from textual.containers import VerticalScroll
from textual.widget import Widget
from textual.widgets import Static

from tests.helpers import RID, SID
from tests.test_app import eventually, make_app
from wisp_tui.app import WispTuiApp
from wisp_tui.chat_view import ChatScroll, ResumeFollow
from wisp_tui.editor import PromptInput
from wisp_tui.history import ChatMessage
from wisp_tui.screens import ApprovalModal


def wheel_up(widget: Widget) -> None:
    widget.post_message(
        events.MouseScrollUp(
            widget, x=1, y=1, delta_x=0, delta_y=0, button=0, shift=False, meta=False, ctrl=False
        )
    )


def transcript(app: WispTuiApp) -> ChatScroll:
    for index in range(15):
        app._add_message(ChatMessage("assistant", f"Paragraph {index}\n\nMore text {index}."))
    return app.query_one("#chat", ChatScroll)


@pytest.mark.parametrize("width", [40, 100])
async def test_wheel_pauses_streaming_and_f4_resumes_without_changing_draft(
    monkeypatch: pytest.MonkeyPatch, width: int
) -> None:
    app, client = make_app([], monkeypatch)
    async with app.run_test(size=(width, 30)) as pilot:
        await eventually(lambda: "ready" in app._status_text)
        chat = transcript(app)
        app.session_id = SID
        app._begin_run(RID)
        app._handle_event({"seq": 1, "kind": "text.delta", "text": "Start\n\n"})
        # Streaming is batched, and following runs after the Markdown layout.
        # A single idle pause can land between layout and its scroll callback.
        await eventually(
            lambda: app._stream_view is not None and app._stream_view.rendered == "Start\n\n"
        )
        await eventually(lambda: chat.scroll_y == chat.max_scroll_y > 0)
        editor = app.query_one(PromptInput)
        editor.value = "draft\nkeep this"
        wheel_up(chat)
        await eventually(lambda: not chat.following and app.query_one(ResumeFollow).display)
        position = chat.scroll_y
        text = "\n\n".join(f"Stream paragraph {i}" for i in range(50))
        app._handle_event({"seq": 2, "kind": "text.delta", "text": text})
        await eventually(
            lambda: app._stream_view is not None and app._stream_view.rendered.endswith(text)
        )
        # Follow callbacks share the chat's queue, so they have run by now.
        await chat.wait_for_refresh()
        assert chat.scroll_y == position < chat.max_scroll_y
        app._finalize_run("run.completed", {})
        await chat.wait_for_refresh()
        assert not bool(chat.following) and chat.scroll_y == position
        await pilot.press("f4")
        await eventually(lambda: chat.following and chat.scroll_y == chat.max_scroll_y)
        assert not app.query_one(ResumeFollow).display
        assert editor.value == "draft\nkeep this" and editor.has_focus
        assert all(method == "GET" for method, _, _ in client.calls)


@pytest.mark.parametrize("key", ["up", "pageup", "home"])
async def test_chat_navigation_pauses_and_click_or_end_resumes(
    monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        chat = transcript(app)
        await eventually(lambda: chat.max_scroll_y > 0 and chat.scroll_y == chat.max_scroll_y)
        chat.focus()
        await pilot.press(key)
        # The key's scroll is deferred and animated. Record the reading
        # position only after navigation has reached its destination.
        await eventually(lambda: not chat.following)
        await chat.wait_for_refresh()
        await eventually(lambda: not app.animator.is_being_animated(chat, "scroll_y"))
        assert chat.scroll_y < chat.max_scroll_y
        if key == "home":
            assert chat.scroll_y == 0
        position = chat.scroll_y
        app._show_line("A new status message")
        await chat.wait_for_refresh()
        assert chat.scroll_y == position
        if key == "home":
            await pilot.press("end")
        else:
            await pilot.click("#resume-follow")
        await eventually(lambda: chat.following and chat.scroll_y == chat.max_scroll_y)


async def test_queued_follow_cannot_override_a_later_scroll_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test():
        chat = transcript(app)
        await eventually(lambda: chat.max_scroll_y > 0)
        app._scroll_down()
        # The reader scrolls up while that follow callback is still queued.
        chat._pause_following()
        chat.scroll_home(animate=False, immediate=True)
        # Same message queue as the follow callback, so it has run by now.
        await chat.wait_for_refresh()
        assert not bool(chat.following) and chat.scroll_y == 0


async def test_short_chat_and_resize_keep_following_until_navigation_is_possible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test(size=(100, 30)) as pilot:
        chat = app.query_one(ChatScroll)
        app._show_line("Short transcript")
        await eventually(lambda: app.query_one("#chat > .system").region.height > 0)
        wheel_up(chat)
        await chat.wait_for_refresh()
        assert bool(chat.following)
        transcript(app)
        await pilot.resize_terminal(40, 20)
        await eventually(
            lambda: (
                chat.size.width == 40 and chat.following and chat.scroll_y == chat.max_scroll_y > 0
            )
        )


async def test_user_navigation_wins_over_pending_position_restoration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test():
        chat = transcript(app)
        await eventually(lambda: chat.max_scroll_y > 0 and chat.scroll_y == chat.max_scroll_y)
        wheel_up(chat)
        await eventually(lambda: not chat.following)
        await chat.wait_for_refresh()
        app._show_line("An asynchronous mount")
        chat.action_scroll_home()
        await eventually(lambda: chat.scroll_y == 0 and not chat._pending_layout)
        # The pending restoration is queued on the chat; it has run after this.
        await chat.wait_for_refresh()
        assert not bool(chat.following) and chat.scroll_y == 0


async def test_editor_and_tool_body_scroll_do_not_pause_the_transcript(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        chat = transcript(app)
        editor = app.query_one(PromptInput)
        editor.value = "\n".join(f"draft {i}" for i in range(20))
        await eventually(lambda: chat.max_scroll_y > 0 and editor.max_scroll_y > 0)
        await pilot.press("up", "home", "pageup")
        wheel_up(editor)
        await editor.wait_for_refresh()
        await chat.wait_for_refresh()
        assert bool(chat.following)
        tool = app._show_tool_call(
            {"name": "details", "args": "\n".join(f"line {i}" for i in range(60))}, completed=True
        )
        tool.collapsed = False
        await eventually(
            lambda: (
                bool(tool.query(VerticalScroll)) and tool.query_one(VerticalScroll).max_scroll_y > 0
            )
        )
        body = tool.query_one(VerticalScroll)
        body.scroll_end(animate=False)
        await eventually(lambda: body.scroll_y == body.max_scroll_y > 0)
        wheel_up(body)
        await eventually(lambda: body.scroll_y < body.max_scroll_y)
        # Anything the body let bubble up has reached the chat's queue by now.
        await body.wait_for_refresh()
        await chat.wait_for_refresh()
        assert bool(chat.following)
        tool.query_one("CollapsibleTitle", Static).focus()
        await eventually(lambda: not chat.following)


async def test_resize_and_approval_keep_follow_paused(monkeypatch: pytest.MonkeyPatch) -> None:
    app, client = make_app([], monkeypatch)
    async with app.run_test(size=(100, 30)) as pilot:
        chat = transcript(app)
        await eventually(lambda: chat.max_scroll_y > 0)
        wheel_up(chat)
        await eventually(lambda: not chat.following)
        await pilot.resize_terminal(40, 24)
        await eventually(lambda: chat.size.width == 40)
        await chat.wait_for_refresh()
        assert not bool(chat.following)
        position = chat.scroll_y
        app.push_screen(ApprovalModal("scripted_tool", {}))
        await pilot.press("f4")
        app._show_line("More output behind the approval")
        await chat.wait_for_refresh()
        assert isinstance(app.screen, ApprovalModal)
        assert not bool(chat.following) and chat.scroll_y == position
        await pilot.press("escape")
        await eventually(lambda: not isinstance(app.screen, ApprovalModal))
        assert not bool(chat.following)
        await pilot.press("f4")
        await eventually(lambda: chat.following and chat.scroll_y == chat.max_scroll_y)
        assert all(method == "GET" for method, _, _ in client.calls)


async def test_trimming_keeps_a_surviving_reading_item_in_place(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _ = make_app([], monkeypatch)
    monkeypatch.setattr(app, "MAX_CHAT_ITEMS", 15)
    async with app.run_test():
        chat = transcript(app)
        await eventually(lambda: chat.max_scroll_y > 0 and chat.scroll_y == chat.max_scroll_y)
        wheel_up(chat)
        await eventually(lambda: not chat.following)
        await chat.wait_for_refresh()
        item = next(
            child
            for child in chat.children
            if child.region.y >= chat.region.y and child.region.bottom <= chat.region.bottom
        )
        y = item.region.y
        for i in range(3):
            app._show_line(f"New status {i}")
        await eventually(lambda: chat._reading_position is None and not chat._pending_layout)
        await chat.wait_for_refresh()
        assert not bool(chat.following) and item.is_attached
        assert item.region.y == y
        assert len(app._chat_items) == 15


async def test_tools_inserted_above_the_answer_preserve_reading_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test():
        app.session_id = SID
        app._begin_run(RID)
        app._handle_event(
            {
                "seq": 1,
                "kind": "text.delta",
                "text": "\n\n".join(f"Paragraph {i}" for i in range(60)),
            }
        )
        await eventually(lambda: app._stream_view is not None and app._stream_view.rendered)
        chat = app.query_one(ChatScroll)
        await eventually(lambda: chat.max_scroll_y > 0 and chat.scroll_y == chat.max_scroll_y)
        wheel_up(chat)
        await eventually(lambda: not chat.following)
        await chat.wait_for_refresh()
        assert app._stream_view is not None
        answer = app._stream_view.widget
        y = answer.region.y
        # Run-summary entries are inserted above the answer.
        tool = app._show_tool_call(
            {"name": "new tool", "toolCallId": "new-call"}, completed=False, terminal=True
        )
        await eventually(lambda: chat._reading_position is None and not chat._pending_layout)
        await chat.wait_for_refresh()
        assert not bool(chat.following) and answer.region.y == y
        tool.collapsed = False
        await eventually(
            lambda: app._tool_history_token is None and "stored history" in tool.result_notice
        )
        await eventually(lambda: chat._reading_position is None and not chat._pending_layout)
        await chat.wait_for_refresh()
        # Expansion scrolls the tool into view. Return to reading the answer
        # before checking that an offscreen tool's result cannot displace it.
        chat.scroll_to(y=answer.virtual_region.y + 20, animate=False, immediate=True)
        await chat.wait_for_refresh()
        assert tool.region.bottom <= chat.region.y
        y = answer.region.y
        app._apply_tool_history(
            [ChatMessage("tool", "Result line\n" * 50, tool_call_id="new-call")], (tool,)
        )
        await eventually(lambda: chat._reading_position is None and not chat._pending_layout)
        await chat.wait_for_refresh()
        assert tool.output_text is not None
        assert not bool(chat.following) and answer.region.y == y


@pytest.mark.parametrize("command", ["/clear", "/new"])
async def test_explicit_transcript_reset_resumes_following(
    monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        chat = transcript(app)
        await eventually(lambda: chat.max_scroll_y > 0 and chat.scroll_y == chat.max_scroll_y)
        wheel_up(chat)
        await eventually(lambda: not chat.following)
        await chat.wait_for_refresh()
        app.query_one(PromptInput).value = command
        await pilot.press("enter")
        await eventually(lambda: chat.following and not app.query_one(ResumeFollow).display)


async def test_checkpoint_refresh_does_not_replace_the_paused_transcript(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test():
        chat = transcript(app)
        app.session_id = SID
        await eventually(lambda: chat.max_scroll_y > 0 and chat.scroll_y == chat.max_scroll_y)
        wheel_up(chat)
        await eventually(lambda: not chat.following)
        await chat.wait_for_refresh()
        children = list(chat.children)
        position = chat.scroll_y
        token = object()
        app._history_refresh_token = token
        question = ChatMessage("user", "question", "q1")
        answer = ChatMessage("assistant", "answer", "a1")
        app._history_refreshed(
            token, SID, app._chat_epoch, [question], [question, answer], "answer"
        )
        await chat.wait_for_refresh()
        assert list(chat.children) == children
        assert not bool(chat.following) and chat.scroll_y == position
