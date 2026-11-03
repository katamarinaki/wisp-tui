"""Combined terminal workflows, entirely offline with the scripted runtime."""
from __future__ import annotations

from typing import Any

import pytest
from textual import events
from textual.containers import VerticalScroll
from textual.widgets import ListItem, ListView, Markdown, Static

from tests.helpers import RID, SID
from tests.test_app import eventually, make_app
from tests.test_rendering import rendered_blocks
from wisp_tui.app import ApprovalModal, ModelPicker, SessionPicker
from wisp_tui.editor import PromptInput


@pytest.mark.parametrize("width", [40, 100])
@pytest.mark.parametrize("key, decision", [
    ("y", "approve"), ("n", "deny"), ("p", "allow_project"),
    ("g", "allow_global"), ("escape", "deny"),
])
async def test_long_approval_keeps_keys_visible_and_details_scrollable(
        monkeypatch: pytest.MonkeyPatch, width: int, key: str, decision: str) -> None:
    app, client = make_app([], monkeypatch)
    async with app.run_test(size=(width, 24)) as pilot:
        editor = app.query_one(PromptInput)
        editor.value = "keep\nmy draft"
        decisions: list[str | None] = []
        modal = ApprovalModal("scripted tool " * 12,
                              {"items": [f"detail {i} [/]" for i in range(70)]},
                              "Approval note " * 10)
        app.push_screen(modal, decisions.append)
        await pilot.pause()
        for new_width, height in [(width, 24), (40, 18), (32, 16), (100, 30)]:
            await pilot.resize_terminal(new_width, height)
            await pilot.pause()
            dialog = modal.query_one("#approval-dialog")
            hint = modal.query_one("#approval-hint")
            assert dialog.content_region.contains_region(hint.region)
            assert modal.region.contains_region(dialog.region)
        details = modal.query_one("#approval-details", VerticalScroll)
        assert details.max_scroll_y > 0
        await pilot.press("end")
        await eventually(lambda: details.scroll_y == details.max_scroll_y)
        assert details.scroll_y > 0
        assert "detail 69 [/]" in str(modal.query_one("#approval-args", Static).render())
        # Enter and editor shortcuts must not decide, submit, or recall a draft.
        await pilot.press("enter", "ctrl+j", "ctrl+p", "ctrl+n", "f1", "f2", "f3", "f4")
        modal.post_message(events.Paste("y\np\n/quit"))
        await pilot.pause()
        assert app.screen is modal and not decisions
        assert editor.value == "keep\nmy draft"
        await pilot.press(key)
        await pilot.pause()
        assert decisions == [decision]
        assert editor.has_focus and editor.value == "keep\nmy draft"
        assert all(method == "GET" for method, _, _ in client.calls)


@pytest.mark.parametrize("picker_kind", ["session", "model"])
@pytest.mark.parametrize("theme", ["textual-dark", "textual-light"])
async def test_picker_navigation_resize_and_highlight_keep_default_background(
        monkeypatch: pytest.MonkeyPatch, picker_kind: str, theme: str) -> None:
    app, client = make_app([], monkeypatch)
    app.theme = theme
    async with app.run_test(size=(100, 30)) as pilot:
        editor = app.query_one(PromptInput)
        editor.value = "unsent\ndraft"
        choices: list[Any] = []
        modal: SessionPicker | ModelPicker
        if picker_kind == "session":
            modal = SessionPicker([(f"session-{i}", f"Conversation {i} [/] " * 8)
                                   for i in range(30)])
        else:
            modal = ModelPicker([{"id": f"model-{i}[/]", "reasoning": True}
                                 for i in range(30)], None)
        app.push_screen(modal, choices.append)
        await pilot.pause()
        await pilot.resize_terminal(40, 18)
        await pilot.press(*(["down"] * 12))
        await pilot.pause()
        listing = modal.query_one(ListView)
        assert listing.index == 12 and listing.scroll_y > 0
        item = listing.children[12]
        await pilot.hover(item)
        await pilot.pause()
        for widget in [listing, *listing.query(ListItem)]:
            assert widget.styles.background.a == 0
            assert widget.styles.background_tint.a == 0
            assert widget.rich_style.bgcolor is None or widget.rich_style.bgcolor.is_default
        assert item.styles.text_style.bold and item.styles.text_style.underline
        await pilot.press("ctrl+j", "ctrl+p", "ctrl+n", "f1", "f2", "f3", "f4")
        modal.post_message(events.Paste("/new\ny"))
        await pilot.pause()
        assert app.screen is modal and not choices
        assert editor.value == "unsent\ndraft"
        await pilot.press("enter")
        await pilot.pause()
        expected: Any = "session-12" if picker_kind == "session" else {
            "mode": "model", "model": "model-10[/]", "thinking": True}
        assert choices == [expected]
        assert editor.has_focus
        app.push_screen(SessionPicker([(SID, "Another session")]), choices.append)
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert choices == [expected, None]
        assert editor.value == "unsent\ndraft"
        assert all(method == "GET" for method, _, _ in client.calls)


@pytest.mark.parametrize("modal_kind", ["approval", "session", "model"])
async def test_ctrl_c_cancels_active_work_from_every_modal_without_approving(
        monkeypatch: pytest.MonkeyPatch, modal_kind: str) -> None:
    app, client = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        await eventually(lambda: "ready" in app._status_state)
        app.session_id = SID
        app._turn_started(RID)
        if modal_kind == "approval":
            app._sync_confirmations([{"id": "prompt-1", "toolName": "scripted", "args": {}}])
            await eventually(lambda: isinstance(app.screen, ApprovalModal))
        elif modal_kind == "session":
            app.push_screen(SessionPicker([(SID, "Example session")]))
        else:
            app.push_screen(ModelPicker([], None))
        await pilot.pause()
        await pilot.press("ctrl+c")
        await eventually(lambda: any(path.endswith("/cancel") for _, path, _ in client.calls))
        assert not any("/confirmations/" in path for _, path, _ in client.calls)
        assert app.is_running
        await pilot.press("ctrl+c")
        await pilot.pause()
        assert not app.is_running


@pytest.mark.parametrize("theme", ["textual-dark", "textual-light"])
async def test_long_mixed_stream_resizes_with_modal_and_preserves_exact_render(
        monkeypatch: pytest.MonkeyPatch, theme: str) -> None:
    app, _ = make_app([], monkeypatch)
    app.theme = theme
    parts = [f"## Section {i}\n\n" + "Unicode 日本語 café and **bold text**. " * 12
             + "\n\n- first\n- second\n\n```text\n" + "long_code_" * 16
             + "\n```\n\n| Key | Value |\n|---|---|\n| state | ready |\n\n"
             for i in range(24)]
    expected = "".join(parts)
    assert len(expected) > 12000
    async with app.run_test(size=(100, 30)) as pilot:
        await eventually(lambda: "ready" in app._status_state)
        editor = app.query_one(PromptInput)
        editor.value = "draft stays editable"
        app.session_id = SID
        app._turn_started(RID)
        for index, part in enumerate(parts):
            app._append_delta(part)
            if index in (3, 11, 19):
                width, height = (40, 18) if index != 11 else (100, 30)
                await pilot.resize_terminal(width, height)
                await pilot.press("!")
                modal = ModelPicker([{"id": "example-model"}], None)
                app.push_screen(modal)
                await pilot.pause()
                app._append_delta("Still streaming.\n\n")
                expected = expected.replace(part, part + "Still streaming.\n\n", 1)
                await pilot.press("escape")
        view = app._stream_view
        assert view is not None
        app._finalize_run("run.completed", {})
        await eventually(lambda: view.rendered == expected, timeout=15)
        await pilot.pause()
        assert editor.value == "draft stays editable!!!" and editor.has_focus
        assert view.widget.source == expected
        assert app.query_one("#chat").region.height > 0
        assert editor.region.bottom <= app.size.height
        reference = Markdown(expected)
        await app.query_one("#chat").mount(reference)
        await pilot.pause()
        assert rendered_blocks(view.widget) == rendered_blocks(reference)
        assert view.widget.region.height == reference.region.height
