"""Tool ordering and literal expandable details; scripted data, no execution."""
from __future__ import annotations

from collections import deque
from typing import Any

import pytest
from textual.widgets import Markdown, Static

from tests.helpers import RID, SID, RequestGate
from tests.test_app import eventually, make_app
from wisp_tui.app import WispTuiApp
from wisp_tui.client import ClientError
from wisp_tui.editor import PromptInput
from wisp_tui.history import parse_history, tool_history_results
from wisp_tui.tool_data import MAX_DETAIL_CHARS, MAX_DETAIL_LINES, detail_text
from wisp_tui.tool_view import ToolCallView


def tool_history(call_id: str, result: str, *, pruned: bool = False) -> list[dict[str, Any]]:
    return [{"values": {"messages": [
        {"type": "ai", "content": "", "tool_calls": [
            {"id": call_id, "name": "read_file", "args": {"planned": "input"}}]},
        {"type": "tool", "content": result, "tool_call_id": call_id, "status": "success",
         "additional_kwargs": {"wisp_tool_output_pruned": pruned}},
    ]}}]


async def test_live_contract_does_not_accept_unverified_output_or_id_aliases(
        monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test():
        tool = app._tool_event({"name": "example", "callId": "unsupported", "id": "event-1",
                                "input": "not args", "result": "not a live field",
                                "output": "not a live field", "args": "sanitized", "ok": True},
                               completed=True)
        assert tool.call_id == "" and tool.output_text is None
        assert tool.input_text == "sanitized" and "sanitized" in tool.input_label


async def test_history_read_preserves_live_inputs_and_joins_parallel_calls_by_id(
        monkeypatch: pytest.MonkeyPatch) -> None:
    app, client = make_app([], monkeypatch)
    payload = [{"values": {"messages": [
        {"type": "ai", "content": "", "tool_calls": [
            {"id": "a", "name": "mcp_call", "args": {"planned": "A"}},
            {"id": "b", "name": "mcp_call", "args": {"planned": "B"}}]},
        {"type": "tool", "tool_call_id": "b", "content": "B result", "status": "success"},
        {"type": "tool", "tool_call_id": "a", "content": "A result", "status": "error"},
    ]}}]
    client.replies[("GET", f"/v1/sessions/{SID}/history")] = deque([payload])
    async with app.run_test() as pilot:
        app.session_id = SID
        first = app._tool_event({"toolCallId": "a", "name": "underlying_tool",
                                 "args": {"executed": "A [REDACTED]"}, "ok": False}, completed=True)
        second = app._tool_event({"toolCallId": "b", "name": "underlying_tool",
                                  "args": {"executed": "B"}, "ok": True}, completed=True)
        await pilot.pause()
        second.collapsed = False
        await eventually(lambda: first.output_text == "A result" and second.output_text == "B result")
        assert '"executed"' in (first.input_text or "") and '"planned"' not in (first.input_text or "")
        assert first.tool_name == second.tool_name == "underlying_tool"
        assert first.status_text == "underlying_tool · failed"
        assert second.status_text == "underlying_tool · ok"
        assert sum(path.endswith("/history") for _, path, _ in client.calls) == 1
        assert all(method == "GET" for method, _, _ in client.calls)


@pytest.mark.parametrize("duplicate", ["call", "result"])
def test_history_results_reject_ambiguous_nested_or_reused_ids(duplicate: str) -> None:
    payload = tool_history("one", "result")
    raw = payload[0]["values"]["messages"]
    if duplicate == "call":
        raw[0]["tool_calls"].append({"id": "one", "name": "nested", "args": {}})
    else:
        raw.append(dict(raw[-1]))
    matched, ambiguous = tool_history_results(parse_history(payload))
    assert matched == {} and ambiguous == {"one"}


async def test_pruned_history_is_labelled_as_a_summary_and_model_input_as_planned(
        monkeypatch: pytest.MonkeyPatch) -> None:
    app, client = make_app([], monkeypatch)
    client.replies[("GET", f"/v1/sessions/{SID}/history")] = deque([
        tool_history("one", "stored summary", pruned=True)])
    async with app.run_test() as pilot:
        app.session_id = SID
        tool = app._tool_event({"toolCallId": "one", "name": "read_file"}, completed=False)
        await pilot.pause()
        tool.collapsed = False
        await eventually(lambda: tool.output_text == "stored summary")
        assert tool.input_label == "Input (history/model request)"
        assert tool.output_label == "Output (stored summary)"


async def test_history_status_is_distinct_from_the_registry_success_flag(
        monkeypatch: pytest.MonkeyPatch) -> None:
    app, client = make_app([], monkeypatch)
    payload = tool_history("one", "domain-specific error")
    payload[0]["values"]["messages"][-1]["status"] = "error"
    client.replies[("GET", f"/v1/sessions/{SID}/history")] = deque([payload])
    async with app.run_test() as pilot:
        app.session_id = SID
        tool = app._tool_event({"toolCallId": "one", "name": "example", "ok": True}, completed=True)
        await pilot.pause()
        tool.collapsed = False
        await eventually(lambda: tool.output_text == "domain-specific error")
        assert tool.status_text == "example · ok"
        assert "History status: error" in str(tool.query_one(".tool-details", Static).render())


def test_history_preserves_json_strings_and_structured_content_blocks() -> None:
    payload = tool_history("one", '{"items":["sample"]}')
    matched, _ = tool_history_results(parse_history(payload))
    assert matched["one"].output_text == '{"items":["sample"]}'
    payload[0]["values"]["messages"][-1]["content"] = [
        {"type": "text", "text": "literal [/]"}, {"type": "custom", "value": 0}]
    matched, _ = tool_history_results(parse_history(payload))
    assert '"value": 0' in matched["one"].output_text
    assert "literal [/]" in matched["one"].output_text


@pytest.mark.parametrize("change", ["clear", "session"])
async def test_stale_tool_history_cannot_fill_a_new_widget_with_the_same_call_id(
        monkeypatch: pytest.MonkeyPatch, change: str) -> None:
    app, client = make_app([], monkeypatch)
    route = f"/v1/sessions/{SID}/history"
    client.replies[("GET", route)] = deque([tool_history("one", "stale output")])
    gate = RequestGate("GET", route)
    client.request_hook = gate
    async with app.run_test() as pilot:
        app.session_id = SID
        original = app._tool_event({"toolCallId": "one", "name": "old"}, completed=True)
        await pilot.pause()
        original.collapsed = False
        try:
            await eventually(gate.entered.is_set)
            if change == "clear":
                app._reset_chat()
            else:
                app._session_loaded({"id": "af06ed54-82b0-45e6-af55-7c85ab55e7ea"}, [], "")
            current = app._tool_event({"toolCallId": "one", "name": "new"}, completed=True)
        finally:
            gate.release.set()
        await app.workers.wait_for_complete()
        assert current.output_text is None and original.output_text is None
        assert current is not original


async def test_history_failure_is_safe_and_collapse_expand_retries_without_submission(
        monkeypatch: pytest.MonkeyPatch) -> None:
    app, client = make_app([], monkeypatch)
    route = f"/v1/sessions/{SID}/history"
    client.replies[("GET", route)] = deque([ClientError("runtime_http_500:private details"),
                                          tool_history("one", "retried output")])
    async with app.run_test() as pilot:
        app.session_id = SID
        tool = app._tool_event({"toolCallId": "one", "name": "example"}, completed=True)
        await pilot.pause()
        tool.collapsed = False
        await eventually(lambda: app._tool_history_token is None)
        await eventually(lambda: "history" in tool.result_notice or "runtime_http" in tool.result_notice)
        assert "private" not in tool.result_notice
        tool.collapsed = True
        await pilot.pause()
        tool.collapsed = False
        await eventually(lambda: tool.output_text == "retried output")
        assert sum(path == route for _, path, _ in client.calls) == 2
        assert all(method == "GET" for method, _, _ in client.calls)


async def test_terminal_rechecks_history_if_an_expanded_read_started_before_results_were_saved(
        monkeypatch: pytest.MonkeyPatch) -> None:
    app, client = make_app([], monkeypatch)
    route = f"/v1/sessions/{SID}/history"
    client.replies[("GET", route)] = deque([[], tool_history("one", "finished output")])
    gate = RequestGate("GET", route)
    client.request_hook = gate
    async with app.run_test() as pilot:
        app.session_id = SID
        app._turn_started(RID)
        tool = app._tool_event({"toolCallId": "one", "name": "read_file"}, completed=True)
        await pilot.pause()
        tool.collapsed = False
        try:
            await eventually(gate.entered.is_set)
            app._finalize_run("run.completed", {"output": {"text": "Final answer"}})
        finally:
            gate.release.set()
        await eventually(lambda: tool.output_text == "finished output")
        assert sum(path == route for _, path, _ in client.calls) == 2
        assert isinstance(list(app.query_one("#chat").children)[-1], Markdown)


async def test_tool_history_lookup_does_not_block_new_input_or_submission(
        monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.test_submissions import COMPLETED

    app, client = make_app(COMPLETED, monkeypatch)
    route = f"/v1/sessions/{SID}/history"
    client.replies[("GET", route)] = deque([tool_history("one", "older output")])
    gate = RequestGate("GET", route)
    client.request_hook = gate
    async with app.run_test() as pilot:
        await eventually(lambda: "ready" in app._status_state)
        app.session_id = SID
        tool = app._tool_event({"toolCallId": "one", "name": "example"}, completed=True)
        await pilot.pause()
        tool.collapsed = False
        try:
            await eventually(gate.entered.is_set)
            editor = app.query_one("#input", PromptInput)
            editor.value = "new prompt"
            editor.focus()
            await pilot.press("enter")
            await eventually(lambda: any(method == "POST" and path.endswith("/runs")
                                         for method, path, _ in client.calls))
        finally:
            gate.release.set()
        await eventually(lambda: tool.output_text == "older output")


@pytest.mark.parametrize("final_text", ["Final response.", ""])
async def test_terminal_text_replaces_multi_round_deltas_including_an_explicit_empty_answer(
        monkeypatch: pytest.MonkeyPatch, final_text: str) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        app.session_id = SID
        app._turn_started(RID)
        app._append_delta("Checking first. ")
        app._tool_event({"toolCallId": "one", "name": "example", "ok": True}, completed=True)
        app._append_delta("More intermediate text.")
        view = app._stream_view
        assert view is not None
        app._finalize_run("run.completed", {"output": {"text": final_text}})
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert view.widget.source == final_text
        assert "Checking first" not in view.widget.source


async def test_terminal_only_metadata_stays_unidentified_instead_of_guessing_results(
        monkeypatch: pytest.MonkeyPatch) -> None:
    app, client = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        app.session_id = SID
        app._turn_started(RID)
        app._finalize_run("run.completed", {"output": {"text": "Done."}, "toolCalls": [
            {"name": "same", "args": {"query": "beta"}, "ok": True, "durationMs": 8},
            {"name": "same", "args": {"query": "alpha"}, "ok": True, "durationMs": 12}]})
        await app.workers.wait_for_complete()
        await pilot.pause()
        cards = list(app.query(ToolCallView))
        assert len(cards) == 2
        for card in cards:
            card.collapsed = False
        await pilot.pause()
        assert all(card.call_id == "" and card.output_text is None for card in cards)
        assert all("No tool-call ID" in str(card.query_one(".tool-details", Static).render())
                   for card in cards)
        assert not any(path.endswith("/history") for _, path, _ in client.calls)


async def test_replay_history_refresh_supplies_tool_outputs_without_a_second_lookup(
        monkeypatch: pytest.MonkeyPatch) -> None:
    app, client = make_app([], monkeypatch)
    payload = tool_history("one", "replay result")
    payload[0]["values"]["messages"].append({"type": "ai", "content": "Done."})
    route = f"/v1/sessions/{SID}/history"
    client.replies[("GET", route)] = deque([payload])
    gate = RequestGate("GET", route)
    client.request_hook = gate
    async with app.run_test() as pilot:
        app.session_id = SID
        app._turn_started(RID, history=[])
        tool = app._tool_event({"toolCallId": "one", "name": "read_file", "ok": True}, completed=True)
        app._finalize_run("run.completed", {"output": {"text": "Done."}})
        try:
            await eventually(gate.entered.is_set)
            tool.collapsed = False
            await pilot.pause()
        finally:
            gate.release.set()
        await eventually(lambda: app._history_refresh_token is None)
        assert tool.output_text == "replay result"
        assert sum(path == route for _, path, _ in client.calls) == 1
        assert all(card.output_text == "replay result" for card in app.query(ToolCallView))


async def test_interleaved_tool_logs_stay_in_order_and_update_by_call_id(
        monkeypatch: pytest.MonkeyPatch) -> None:
    app, client = make_app([], monkeypatch)
    client.replies[("GET", f"/v1/sessions/{SID}/history")] = deque([tool_history("call-1", "file contents")])
    async with app.run_test() as pilot:
        await eventually(lambda: "ready" in app._status_state)
        app.session_id = SID
        app._turn_started(RID)
        app._handle_event({"kind": "text.delta", "text": "I checked. "})
        app._handle_event({"kind": "tool_call.started", "toolCallId": "call-1",
                           "name": "read_file"})
        app._handle_event({"kind": "text.delta", "text": "The answer."})
        app._handle_event({"kind": "tool_call", "toolCallId": "call-1",
                           "name": "read_file", "ok": True, "args": {"path": "/example/file"},
                           "durationMs": 250})
        app._handle_event({"kind": "confirmation.resolved", "id": "prompt-1"})
        app._handle_event({"kind": "run.completed", "outcome": {
            "output": {"text": "I checked. The answer."}}})
        await app.workers.wait_for_complete()
        await pilot.pause()
        items = list(app.query_one("#chat").children)
        tool = app.query_one(ToolCallView)
        before, answer = app.query("#chat > Markdown").results(Markdown)
        assert len(app.query(ToolCallView)) == 1
        assert tool.title == 'read_file {"path": "/example/file"}'
        assert tool.status_text == "read_file · ok · 0.2s"
        assert tool.input_text == '{\n  "path": "/example/file"\n}'
        await eventually(lambda: tool.output_text == "file contents")
        assert (before.source, answer.source) == ("I checked. ", "The answer.")
        assert items.index(before) < items.index(tool) < items.index(answer) == len(items) - 2
        assert str(items[-1].render()) == "approval resolved"
        assert list(app._chat_items) == items
        assert app.transcript() == [
            ("assistant", "I checked. "), ("tool", 'read_file {"path": "/example/file"}\nread_file · ok · 0.2s'),
            ("assistant", "The answer."), ("tool", "approval resolved")]


@pytest.mark.parametrize("width", [40, 100])
async def test_click_and_keyboard_expand_literal_details_without_sending(
        monkeypatch: pytest.MonkeyPatch, width: int) -> None:
    app, client = make_app([], monkeypatch)
    async with app.run_test(size=(width, 30)) as pilot:
        await eventually(lambda: "ready" in app._status_state)
        editor = app.query_one("#input", PromptInput)
        editor.value = "keep my draft"
        name = "tool [bold]name[/]"
        value = "literal [/]\n```sh\n/quit\n\x1b[31m"
        created = app._tool_event({"name": name, "args": {"command": "[a-z]"},
                                   "ok": True}, completed=True)
        created.apply_history(output_text=detail_text(value))
        await pilot.pause()
        tool = app.query_one(ToolCallView)
        header = tool.query_one("CollapsibleTitle", Static)
        details = tool.query_one(".tool-details", Static)
        assert bool(tool.collapsed)
        assert "$ [a-z]" in str(header.render())
        assert name in str(tool.query_one(".tool-stats", Static).render())
        await pilot.click(header)
        await pilot.pause()
        assert not bool(tool.collapsed)
        shown = str(details.render())
        assert "Input (" in shown and "Output (stored history)" in shown
        assert '"command": "[a-z]"' in shown
        assert "literal [/]\n```sh\n/quit\n�[31m" in shown
        assert "\x1b" not in shown
        header.focus()
        await pilot.press("enter")
        assert bool(tool.collapsed)
        await pilot.press("enter")
        assert not bool(tool.collapsed)
        await pilot.resize_terminal(32, 30)
        await pilot.pause()
        assert tool.region.width == 30
        assert tool.region.right == 31
        assert editor.value == "keep my draft"
        assert not any(method == "POST" for method, _, _ in client.calls)


@pytest.mark.parametrize("value, expected", [(None, "null"), (False, "false"), (0, "0"),
                                           ("", "(empty)"), ({}, "{}"), ([], "[]")])
async def test_empty_and_false_tool_values_are_reported_not_treated_as_missing(
        monkeypatch: pytest.MonkeyPatch, value: Any, expected: str) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test():
        created = app._tool_event({"name": "example", "args": value}, completed=True)
        created.apply_history(output_text=detail_text(value))
        tool = app.query_one(ToolCallView)
        assert tool.input_text == tool.output_text == expected


async def test_missing_fields_do_not_show_unrelated_event_or_error_contents(
        monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        app._tool_event({"name": "example", "ok": False,
                         "error": "private exception details", "headers": {"private": "value"}},
                        completed=True)
        tool = app.query_one(ToolCallView)
        await pilot.pause()
        shown = str(tool.query_one(".tool-details", Static).render())
        assert tool.status_text == "example · failed"
        assert shown.count("Not supplied by runtime.") == 1
        assert "No tool-call ID" in shown
        assert "private" not in shown and "headers" not in shown


@pytest.mark.parametrize("theme", ["textual-dark", "textual-light"])
async def test_expanded_tool_output_is_scrollable_bounded_and_unpainted(
        monkeypatch: pytest.MonkeyPatch, theme: str) -> None:
    app, _ = make_app([], monkeypatch)
    app.theme = theme
    async with app.run_test(size=(40, 30)) as pilot:
        app._tool_event({"toolCallId": "one", "name": "example", "args": "input"},
                        completed=False)
        await pilot.pause()
        tool = app.query_one(ToolCallView)
        tool.collapsed = False
        app._tool_event({"toolCallId": "one", "ok": True}, completed=True)
        tool.apply_history(output_text=detail_text(
            "\n".join(f"output line {index}" for index in range(300))))
        await pilot.pause()
        assert not bool(tool.collapsed)
        port = tool.query_one(".tool-detail-scroll")
        assert 0 < port.region.height <= 16
        assert port.max_scroll_y > 0
        port.focus()
        await pilot.press("end")
        await eventually(lambda: port.scroll_y == port.max_scroll_y)
        assert port.scroll_y == port.max_scroll_y
        assert (tool.output_text or "").endswith("… tool detail truncated")
        assert tool.status_text == "example · ok"
        for widget in (tool, port, tool.query_one("Contents"),
                       tool.query_one("CollapsibleTitle"), tool.query_one(".tool-details")):
            assert widget.styles.background.a == 0
            assert widget.styles.background_tint.a == 0


async def test_parallel_same_name_calls_keep_results_with_their_explicit_id(
        monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        for call_id, path in (("a", "first"), ("b", "second")):
            app._tool_event({"toolCallId": call_id, "name": "read", "args": {"path": path}},
                            completed=False)
        for call_id, result in (("b", "second result"), ("a", "first result")):
            created = app._tool_event({"toolCallId": call_id, "ok": True}, completed=True)
            created.apply_history(output_text=result)
        await pilot.pause()
        first, second = app.query(ToolCallView)
        assert isinstance(first, ToolCallView) and isinstance(second, ToolCallView)
        assert first.output_text == "first result" and '"first"' in (first.input_text or "")
        assert second.output_text == "second result" and '"second"' in (second.input_text or "")


async def test_unidentified_same_name_calls_are_never_merged_and_remain_bounded(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(WispTuiApp, "MAX_CHAT_ITEMS", 4)
    app, _ = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        app.session_id = SID
        app._turn_started(RID)
        app._append_delta("answer")
        for index in range(20):
            app._tool_event({"name": "same tool", "args": {"index": index}}, completed=True)
        await pilot.pause()
        assert len(app.query(ToolCallView)) == 4
        assert len(app._chat_items) == 4
        assert not app._tool_calls
        assert not app.query("#chat > Markdown")


@pytest.mark.parametrize("limit", [1, 4])
async def test_call_id_cache_drops_trimmed_widgets_even_with_only_a_stream_slot(
        monkeypatch: pytest.MonkeyPatch, limit: int) -> None:
    monkeypatch.setattr(WispTuiApp, "MAX_CHAT_ITEMS", limit)
    app, _ = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        app._append_delta("answer")
        for index in range(20):
            # Run-summary entries are inserted above the answer that is still shown.
            app._tool_event({"toolCallId": str(index), "name": "example"}, completed=False,
                            terminal=True)
        await pilot.pause()
        assert len(app._chat_items) == limit
        assert len(app._tool_calls) == limit - 1
        assert all(widget in app._chat_items for widget in app._tool_calls.values())


async def test_clear_and_new_run_do_not_reuse_old_call_widgets(
        monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        app.session_id = SID
        app._turn_started(RID)
        app._tool_event({"toolCallId": "same", "name": "old", "args": "old args"}, completed=False)
        await pilot.pause()
        old = app.query_one(ToolCallView)
        app._reset_chat()
        created = app._tool_event({"toolCallId": "same", "name": "new"}, completed=True)
        created.apply_history(output_text="new output")
        await pilot.pause()
        new = app.query_one(ToolCallView)
        assert new is not old
        assert new.input_text is None and new.output_text == "new output"
        app._turn_started("af06ed54-82b0-45e6-af55-7c85ab55e7ea")
        created = app._tool_event({"toolCallId": "same", "name": "next"}, completed=True)
        created.apply_history(output_text="next output")
        await pilot.pause()
        assert len(app.query(ToolCallView)) == 2
        assert new.output_text == "new output"


async def test_terminal_aggregate_does_not_duplicate_live_cards_or_invent_outputs(
        monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        app.session_id = SID
        app._turn_started(RID)
        app._tool_event({"toolCallId": "one", "name": "read"}, completed=False)
        app._tool_event({"toolCallId": "one", "name": "read", "args": "argument", "ok": True},
                        completed=True)
        app._append_delta("answer")
        app._finalize_run("run.completed", {"output": {"text": "Final answer"}, "toolCalls": [
            {"name": "read", "args": "argument", "ok": True, "durationMs": 10}]})
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert len(app.query(ToolCallView)) == 1
        tool = app.query_one(ToolCallView)
        assert tool.input_text == "argument" and tool.output_text is None
        assert isinstance(list(app.query_one("#chat").children)[-1], Markdown)


async def test_checkpoint_tool_calls_and_results_remain_expandable_in_order(
        monkeypatch: pytest.MonkeyPatch) -> None:
    messages = parse_history([{"values": {"messages": [
        {"type": "human", "content": "question"},
        {"type": "ai", "content": "", "tool_calls": [
            {"id": "a", "name": "read[/]", "args": {"path": "[x]"}},
            {"id": "b", "name": "read[/]", "args": {"path": "other"}}]},
        {"type": "tool", "content": "second result", "tool_call_id": "b"},
        {"type": "tool", "content": "first result", "tool_call_id": "a"},
        {"type": "ai", "content": "final answer"},
    ]}}])
    app, _ = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        app._session_loaded({"id": SID}, messages, "")
        await pilot.pause()
        first, second = app.query(ToolCallView)
        assert isinstance(first, ToolCallView) and isinstance(second, ToolCallView)
        assert first.output_text == "first result" and second.output_text == "second result"
        assert first.input_text == '{\n  "path": "[x]"\n}'
        assert first.status_text == "read[/] · finished"
        assert isinstance(list(app.query_one("#chat").children)[-1], Markdown)


def test_checkpoint_retains_empty_tool_results_and_validates_optional_metadata() -> None:
    messages = parse_history([{"values": {"messages": [
        {"type": "ai", "content": "", "tool_calls": [
            {"name": "first", "id": "x" * 201, "args": 0},
            {"name": "second", "id": ["invalid"], "args": False},
            {"name": [], "id": "invalid"}]},
        {"type": "tool", "content": "", "tool_call_id": "x" * 201},
        {"type": "tool", "content": None},
    ]}}])
    assert len(messages) == 2
    assert [call.input_text for call in messages[0].tool_calls] == ["0", "false"]
    assert all(call.call_id == "" for call in messages[0].tool_calls)
    assert messages[1].role == "tool" and messages[1].text == ""
    assert messages[1].tool_call_id == ""


async def test_checkpoint_call_without_a_result_does_not_claim_to_be_running(
        monkeypatch: pytest.MonkeyPatch) -> None:
    messages = parse_history([{"values": {"messages": [
        {"type": "ai", "content": "", "tool_calls": [
            {"id": "one", "name": "example", "args": {}}]},
    ]}}])
    app, _ = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        app._session_loaded({"id": SID}, messages, "")
        await pilot.pause()
        tool = app.query_one(ToolCallView)
        assert tool.status_text == "example · recorded"
        details = str(tool.query_one(".tool-details", Static).render())
        assert "No result stored in this checkpoint." in details
        assert "Waiting" not in details


async def test_oversized_call_ids_never_correlate_by_their_shared_prefix(
        monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        for suffix in ("a", "b"):
            app._tool_event({"toolCallId": "x" * 200 + suffix, "name": "same tool",
                             "result": suffix}, completed=True)
        await pilot.pause()
        assert len(app.query(ToolCallView)) == 2
        assert not app._tool_calls


async def test_cancel_from_a_focused_tool_header_still_targets_the_active_run(
        monkeypatch: pytest.MonkeyPatch) -> None:
    app, client = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        await eventually(lambda: "ready" in app._status_state)
        app.session_id = SID
        app._turn_started(RID)
        app._tool_event({"name": "example"}, completed=False)
        await pilot.pause()
        app.query_one("CollapsibleTitle", Static).focus()
        await pilot.press("ctrl+c")
        await eventually(lambda: any(path.endswith("/cancel") for _, path, _ in client.calls))
        assert [path for method, path, _ in client.calls
                if method == "POST" and path.endswith("/cancel")] == [f"/v1/sessions/{SID}/cancel"]
        assert not any("/confirmations/" in path for _, path, _ in client.calls)


@pytest.mark.parametrize("value", ["x" * (MAX_DETAIL_CHARS * 10), "line\n" * (MAX_DETAIL_LINES * 10),
                                   {"deep": [{"value": "x" * MAX_DETAIL_CHARS}] * 100}])
def test_large_details_are_bounded_and_explicitly_marked(value: Any) -> None:
    text = detail_text(value)
    assert len(text) < MAX_DETAIL_CHARS + 100
    assert len(text.splitlines()) <= MAX_DETAIL_LINES + 1
    assert text.endswith("… tool detail truncated")


@pytest.mark.parametrize("duration", [True, -1, float("nan"), float("inf"), 10 ** 1000, "100"])
async def test_invalid_duration_never_crashes_or_claims_a_time(
        monkeypatch: pytest.MonkeyPatch, duration: Any) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test():
        app._tool_event({"name": "example", "durationMs": duration}, completed=True)
        assert app.query_one(ToolCallView).status_text == "example · finished"
