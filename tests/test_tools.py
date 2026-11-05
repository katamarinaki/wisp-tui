"""Tool ordering and literal entries; scripted data, no execution."""

from __future__ import annotations

import time
from collections import deque
from typing import Any

import pytest

from tests.helpers import RID, SID, RequestGate
from tests.test_app import eventually, make_app
from tests.test_submissions import COMPLETED
from wisp_tui.client import ClientError
from wisp_tui.editor import PromptInput
from wisp_tui.history import parse_history, tool_history_results
from wisp_tui.tool_data import MAX_DETAIL_CHARS, MAX_DETAIL_LINES, detail_text
from wisp_tui.tool_view import ToolCallView

VIEWS: list[ToolCallView] = []


@pytest.fixture(autouse=True)
def record_tool_views(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every entry: finished ones leave the live area for scrollback."""
    VIEWS.clear()
    original = ToolCallView.__init__

    def init(self: ToolCallView, *args: Any, **kwargs: Any) -> None:
        original(self, *args, **kwargs)
        VIEWS.append(self)

    monkeypatch.setattr(ToolCallView, "__init__", init)


def tool_history(call_id: str, result: str) -> list[dict[str, Any]]:
    return [
        {
            "values": {
                "messages": [
                    {
                        "type": "ai",
                        "content": "",
                        "tool_calls": [
                            {"id": call_id, "name": "read_file", "args": {"planned": "input"}}
                        ],
                    },
                    {
                        "type": "tool",
                        "content": result,
                        "tool_call_id": call_id,
                        "status": "success",
                    },
                ]
            }
        }
    ]


async def test_live_contract_does_not_accept_unverified_output_or_id_aliases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test():
        tool = app._show_tool_call(
            {
                "name": "example",
                "callId": "unsupported",
                "id": "event-1",
                "input": "not args",
                "result": "not a live field",
                "output": "not a live field",
                "args": "sanitized",
                "ok": True,
            },
            completed=True,
        )
        assert tool.call_id == "" and tool.output_text is None
        assert tool.input_text == "sanitized"


async def test_history_read_preserves_live_inputs_and_joins_parallel_calls_by_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client = make_app([], monkeypatch)
    payload = [
        {
            "values": {
                "messages": [
                    {
                        "type": "ai",
                        "content": "",
                        "tool_calls": [
                            {"id": "a", "name": "mcp_call", "args": {"planned": "A"}},
                            {"id": "b", "name": "mcp_call", "args": {"planned": "B"}},
                        ],
                    },
                    {
                        "type": "tool",
                        "tool_call_id": "b",
                        "content": "B result",
                        "status": "success",
                    },
                    {"type": "tool", "tool_call_id": "a", "content": "A result", "status": "error"},
                ]
            }
        }
    ]
    client.replies[("GET", f"/v1/sessions/{SID}/history")] = deque([payload])
    async with app.run_test() as pilot:
        app.session_id = SID
        first = app._show_tool_call(
            {
                "toolCallId": "a",
                "name": "underlying_tool",
                "args": {"executed": "A [REDACTED]"},
                "ok": False,
            },
            completed=True,
        )
        second = app._show_tool_call(
            {"toolCallId": "b", "name": "underlying_tool", "args": {"executed": "B"}, "ok": True},
            completed=True,
        )
        await pilot.pause()
        await eventually(
            lambda: first.output_text == "A result" and second.output_text == "B result"
        )
        assert '"executed"' in (first.input_text or "") and '"planned"' not in (
            first.input_text or ""
        )
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


async def test_stored_result_fills_a_running_call_and_keeps_model_input_as_planned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client = make_app([], monkeypatch)
    client.replies[("GET", f"/v1/sessions/{SID}/history")] = deque(
        [tool_history("one", "stored summary")]
    )
    async with app.run_test() as pilot:
        app.session_id = SID
        tool = app._show_tool_call({"toolCallId": "one", "name": "read_file"}, completed=False)
        await pilot.pause()
        app._request_tool_history(force_after_current=True)
        await eventually(lambda: tool.output_text == "stored summary")
        assert '"planned"' in (tool.input_text or "")


async def test_history_status_is_distinct_from_the_registry_success_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client = make_app([], monkeypatch)
    payload = tool_history("one", "domain-specific error")
    payload[0]["values"]["messages"][-1]["status"] = "error"
    client.replies[("GET", f"/v1/sessions/{SID}/history")] = deque([payload])
    async with app.run_test() as pilot:
        app.session_id = SID
        tool = app._show_tool_call(
            {"toolCallId": "one", "name": "example", "ok": True}, completed=True
        )
        await pilot.pause()
        await eventually(lambda: tool.output_text == "domain-specific error")
        # The registry's ok decides; the stored "error" status does not override it.
        assert tool.status_text == "example · ok"
        assert tool.entry()[0] == "tool-call-ok"


def test_history_preserves_json_strings_and_structured_content_blocks() -> None:
    payload = tool_history("one", '{"items":["sample"]}')
    matched, _ = tool_history_results(parse_history(payload))
    assert matched["one"].output_text == '{"items":["sample"]}'
    payload[0]["values"]["messages"][-1]["content"] = [
        {"type": "text", "text": "literal [/]"},
        {"type": "custom", "value": 0},
    ]
    matched, _ = tool_history_results(parse_history(payload))
    assert '"value": 0' in matched["one"].output_text
    assert "literal [/]" in matched["one"].output_text


@pytest.mark.parametrize("change", ["clear", "session"])
async def test_stale_tool_history_cannot_fill_a_new_widget_with_the_same_call_id(
    monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    app, client = make_app([], monkeypatch)
    route = f"/v1/sessions/{SID}/history"
    client.replies[("GET", route)] = deque([tool_history("one", "stale output")])
    gate = RequestGate("GET", route)
    client.request_hook = gate
    async with app.run_test() as pilot:
        app.session_id = SID
        original = app._show_tool_call({"toolCallId": "one", "name": "old"}, completed=True)
        await pilot.pause()
        try:
            await eventually(gate.entered.is_set)
            if change == "clear":
                app._reset_chat()
            else:
                app._session_loaded({"id": "af06ed54-82b0-45e6-af55-7c85ab55e7ea"}, [], "")
            current = app._show_tool_call({"toolCallId": "one", "name": "new"}, completed=True)
        finally:
            gate.release.set()
        # The new widget's own lookup can hit the same route; block the event loop until
        # the stale request has taken the only queued reply, or the fresh one may win it.
        deadline = time.monotonic() + 30.0
        while client.replies[("GET", route)] and time.monotonic() < deadline:
            time.sleep(0.001)
        await app.workers.wait_for_complete()
        assert current.output_text is None and original.output_text is None
        assert current is not original


async def test_history_failure_is_safe_without_submission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client = make_app([], monkeypatch)
    route = f"/v1/sessions/{SID}/history"
    client.replies[("GET", route)] = deque([ClientError("runtime_http_500:private details")])
    async with app.run_test():
        app.session_id = SID
        tool = app._show_tool_call({"toolCallId": "one", "name": "example"}, completed=True)
        await eventually(
            lambda: app._tool_history_token is None and "unavailable" in tool.result_notice
        )
        assert "private" not in tool.result_notice
        assert tool.result_checked and tool.output_text is None
        assert sum(path == route for _, path, _ in client.calls) == 1
        assert all(method == "GET" for method, _, _ in client.calls)


async def test_terminal_rechecks_history_if_an_expanded_read_started_before_results_were_saved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client = make_app([], monkeypatch)
    route = f"/v1/sessions/{SID}/history"
    client.replies[("GET", route)] = deque([[], tool_history("one", "finished output")])
    gate = RequestGate("GET", route)
    client.request_hook = gate
    async with app.run_test() as pilot:
        app.session_id = SID
        app._begin_run(RID)
        tool = app._show_tool_call({"toolCallId": "one", "name": "read_file"}, completed=True)
        await pilot.pause()
        try:
            await eventually(gate.entered.is_set)
            app._finalize_run("run.completed", {"output": {"text": "Final answer"}})
        finally:
            gate.release.set()
        await eventually(lambda: tool.output_text == "finished output")
        assert sum(path == route for _, path, _ in client.calls) == 2
        assert app.transcript()[-1] == ("assistant", "Final answer")


async def test_tool_history_lookup_does_not_block_new_input_or_submission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    app, client = make_app(COMPLETED, monkeypatch)
    route = f"/v1/sessions/{SID}/history"
    client.replies[("GET", route)] = deque([tool_history("one", "older output")])
    gate = RequestGate("GET", route)
    client.request_hook = gate
    async with app.run_test() as pilot:
        await eventually(lambda: "ready" in app._status_text)
        app.session_id = SID
        tool = app._show_tool_call({"toolCallId": "one", "name": "example"}, completed=True)
        await pilot.pause()
        try:
            await eventually(gate.entered.is_set)
            editor = app.query_one("#input", PromptInput)
            editor.value = "new prompt"
            editor.focus()
            await pilot.press("enter")
            await eventually(
                lambda: any(
                    method == "POST" and path.endswith("/runs") for method, path, _ in client.calls
                )
            )
        finally:
            gate.release.set()
        await eventually(lambda: tool.output_text == "older output")


@pytest.mark.parametrize("final_text", ["Final response.", ""])
async def test_terminal_text_replaces_multi_round_deltas_including_an_explicit_empty_answer(
    monkeypatch: pytest.MonkeyPatch, final_text: str
) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        app.session_id = SID
        app._begin_run(RID)
        app._append_delta("Checking first. ")
        app._show_tool_call({"toolCallId": "one", "name": "example", "ok": True}, completed=True)
        app._append_delta("More intermediate text.")
        view = app._stream_view
        assert view is not None
        app._finalize_run("run.completed", {"output": {"text": final_text}})
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert view.widget.text == final_text
        assert "Checking first" not in view.widget.text


async def test_terminal_only_metadata_stays_unidentified_instead_of_guessing_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        app.session_id = SID
        app._begin_run(RID)
        app._finalize_run(
            "run.completed",
            {
                "output": {"text": "Done."},
                "toolCalls": [
                    {"name": "same", "args": {"query": "beta"}, "ok": True, "durationMs": 8},
                    {"name": "same", "args": {"query": "alpha"}, "ok": True, "durationMs": 12},
                ],
            },
        )
        await app.workers.wait_for_complete()
        await pilot.pause()
        cards = list(VIEWS)
        assert len(cards) == 2
        assert all(card.call_id == "" and card.output_text is None for card in cards)
        assert not any(path.endswith("/history") for _, path, _ in client.calls)


async def test_replay_tool_outputs_need_only_one_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client = make_app([], monkeypatch)
    payload = tool_history("one", "replay result")
    payload[0]["values"]["messages"].append({"type": "ai", "content": "Done."})
    route = f"/v1/sessions/{SID}/history"
    client.replies[("GET", route)] = deque([payload])
    gate = RequestGate("GET", route)
    client.request_hook = gate
    async with app.run_test() as pilot:
        app.session_id = SID
        app._begin_run(RID, history=[])
        tool = app._show_tool_call(
            {"toolCallId": "one", "name": "read_file", "ok": True}, completed=True
        )
        app._finalize_run("run.completed", {"output": {"text": "Done."}})
        try:
            await eventually(gate.entered.is_set)
            await pilot.pause()
        finally:
            gate.release.set()
        await eventually(lambda: app._tool_history_token is None)
        assert tool.output_text == "replay result"
        assert sum(path == route for _, path, _ in client.calls) == 1
        assert all(card.output_text == "replay result" for card in VIEWS)


async def test_interleaved_tool_logs_stay_in_order_and_update_by_call_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client = make_app([], monkeypatch)
    client.replies[("GET", f"/v1/sessions/{SID}/history")] = deque(
        [tool_history("call-1", "file contents")]
    )
    async with app.run_test() as pilot:
        await eventually(lambda: "ready" in app._status_text)
        app.session_id = SID
        app._begin_run(RID)
        app._handle_event({"kind": "text.delta", "text": "I checked. "})
        app._handle_event(
            {"kind": "tool_call.started", "toolCallId": "call-1", "name": "read_file"}
        )
        app._handle_event({"kind": "text.delta", "text": "The answer."})
        app._handle_event(
            {
                "kind": "tool_call",
                "toolCallId": "call-1",
                "name": "read_file",
                "ok": True,
                "args": {"path": "/example/file"},
                "durationMs": 250,
            }
        )
        app._handle_event({"kind": "confirmation.resolved", "id": "prompt-1"})
        app._handle_event(
            {"kind": "run.completed", "outcome": {"output": {"text": "I checked. The answer."}}}
        )
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert len(VIEWS) == 1
        tool = VIEWS[0]
        assert tool.title == 'read_file {"path": "/example/file"}'
        assert tool.status_text == "read_file · ok · 0.2s"
        assert tool.input_text == '{\n  "path": "/example/file"\n}'
        await eventually(lambda: tool.output_text == "file contents")
        await eventually(lambda: not app._chat_items)
        assert app.transcript() == [
            ("assistant", "I checked. "),
            (
                "tool-call-ok",
                'read_file {"path": "/example/file"}\nfile contents\nread_file · ok · 0.2s',
            ),
            ("assistant", "The answer."),
            ("tool", "approval resolved"),
        ]


@pytest.mark.parametrize(
    "value, expected",
    [(None, "null"), (False, "false"), (0, "0"), ("", "(empty)"), ({}, "{}"), ([], "[]")],
)
async def test_empty_and_false_tool_values_are_reported_not_treated_as_missing(
    monkeypatch: pytest.MonkeyPatch, value: Any, expected: str
) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test():
        created = app._show_tool_call({"name": "example", "args": value}, completed=True)
        created.apply_history(output_text=detail_text(value))
        assert created.input_text == created.output_text == expected


async def test_missing_fields_do_not_show_unrelated_event_or_error_contents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        app._show_tool_call(
            {
                "name": "example",
                "ok": False,
                "error": "private exception details",
                "headers": {"private": "value"},
            },
            completed=True,
        )
        tool = VIEWS[0]
        await pilot.pause()
        _, shown = tool.entry()
        assert tool.status_text == "example · failed"
        assert tool.input_text is None and tool.output_text is None
        assert "private" not in shown and "headers" not in shown


async def test_parallel_same_name_calls_keep_results_with_their_explicit_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        for call_id, path in (("a", "first"), ("b", "second")):
            app._show_tool_call(
                {"toolCallId": call_id, "name": "read", "args": {"path": path}}, completed=False
            )
        for call_id, result in (("b", "second result"), ("a", "first result")):
            created = app._show_tool_call({"toolCallId": call_id, "ok": True}, completed=True)
            created.apply_history(output_text=result)
        await pilot.pause()
        first, second = VIEWS
        assert first.output_text == "first result" and '"first"' in (first.input_text or "")
        assert second.output_text == "second result" and '"second"' in (second.input_text or "")


async def test_unidentified_same_name_calls_are_never_merged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        app.session_id = SID
        app._begin_run(RID)
        app._append_delta("answer")
        for index in range(20):
            app._show_tool_call({"name": "same tool", "args": {"index": index}}, completed=True)
        await pilot.pause()
        assert len(VIEWS) == 20
        assert not app._tool_calls
        entries = [kind for kind, _ in app.transcript()]
        assert sum(kind.startswith("tool-call") for kind in entries) == 20


async def test_clear_and_new_run_do_not_reuse_old_call_widgets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        app.session_id = SID
        app._begin_run(RID)
        app._show_tool_call(
            {"toolCallId": "same", "name": "old", "args": "old args"}, completed=False
        )
        await pilot.pause()
        (old,) = VIEWS
        app._reset_chat()
        created = app._show_tool_call({"toolCallId": "same", "name": "new"}, completed=True)
        created.apply_history(output_text="new output")
        await pilot.pause()
        new = created
        assert new is not old
        assert new.input_text is None and new.output_text == "new output"
        app._begin_run("af06ed54-82b0-45e6-af55-7c85ab55e7ea")
        created = app._show_tool_call({"toolCallId": "same", "name": "next"}, completed=True)
        created.apply_history(output_text="next output")
        await pilot.pause()
        assert len(VIEWS) == 3 and created is not new
        assert new.output_text == "new output"


async def test_terminal_aggregate_does_not_duplicate_live_cards_or_invent_outputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        app.session_id = SID
        app._begin_run(RID)
        app._show_tool_call({"toolCallId": "one", "name": "read"}, completed=False)
        app._show_tool_call(
            {"toolCallId": "one", "name": "read", "args": "argument", "ok": True}, completed=True
        )
        app._append_delta("answer")
        app._finalize_run(
            "run.completed",
            {
                "output": {"text": "Final answer"},
                "toolCalls": [{"name": "read", "args": "argument", "ok": True, "durationMs": 10}],
            },
        )
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert len(VIEWS) == 1
        tool = VIEWS[0]
        assert tool.input_text == "argument" and tool.output_text is None
        assert app.transcript()[-1] == ("assistant", "Final answer")


async def test_checkpoint_tool_calls_and_results_stay_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages = parse_history(
        [
            {
                "values": {
                    "messages": [
                        {"type": "human", "content": "question"},
                        {
                            "type": "ai",
                            "content": "",
                            "tool_calls": [
                                {"id": "a", "name": "read[/]", "args": {"path": "[x]"}},
                                {"id": "b", "name": "read[/]", "args": {"path": "other"}},
                            ],
                        },
                        {"type": "tool", "content": "second result", "tool_call_id": "b"},
                        {"type": "tool", "content": "first result", "tool_call_id": "a"},
                        {"type": "ai", "content": "final answer"},
                    ]
                }
            }
        ]
    )
    app, _ = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        app._session_loaded({"id": SID}, messages, "")
        await pilot.pause()
        first, second = VIEWS
        assert first.output_text == "first result" and second.output_text == "second result"
        assert first.input_text == '{\n  "path": "[x]"\n}'
        assert first.status_text == "read[/] · finished"
        assert app.transcript()[-1] == ("assistant", "final answer")


def test_checkpoint_retains_empty_tool_results_and_validates_optional_metadata() -> None:
    messages = parse_history(
        [
            {
                "values": {
                    "messages": [
                        {
                            "type": "ai",
                            "content": "",
                            "tool_calls": [
                                {"name": "first", "id": "x" * 201, "args": 0},
                                {"name": "second", "id": ["invalid"], "args": False},
                                {"name": [], "id": "invalid"},
                            ],
                        },
                        {"type": "tool", "content": "", "tool_call_id": "x" * 201},
                        {"type": "tool", "content": None},
                    ]
                }
            }
        ]
    )
    assert len(messages) == 2
    assert [call.input_text for call in messages[0].tool_calls] == ["0", "false"]
    assert all(call.call_id == "" for call in messages[0].tool_calls)
    assert messages[1].role == "tool" and messages[1].text == ""
    assert messages[1].tool_call_id == ""


async def test_checkpoint_call_without_a_result_does_not_claim_to_be_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages = parse_history(
        [
            {
                "values": {
                    "messages": [
                        {
                            "type": "ai",
                            "content": "",
                            "tool_calls": [{"id": "one", "name": "example", "args": {}}],
                        },
                    ]
                }
            }
        ]
    )
    app, _ = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        app._session_loaded({"id": SID}, messages, "")
        await pilot.pause()
        (tool,) = VIEWS
        assert tool.status_text == "example · recorded"
        assert "running" not in tool.entry()[1]


async def test_oversized_call_ids_never_correlate_by_their_shared_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        for suffix in ("a", "b"):
            app._show_tool_call(
                {"toolCallId": "x" * 200 + suffix, "name": "same tool", "result": suffix},
                completed=True,
            )
        await pilot.pause()
        assert len(VIEWS) == 2
        assert not app._tool_calls


@pytest.mark.parametrize(
    "value",
    [
        "x" * (MAX_DETAIL_CHARS * 10),
        "line\n" * (MAX_DETAIL_LINES * 10),
        {"deep": [{"value": "x" * MAX_DETAIL_CHARS}] * 100},
    ],
)
def test_large_details_are_bounded_and_explicitly_marked(value: Any) -> None:
    text = detail_text(value)
    assert len(text) < MAX_DETAIL_CHARS + 100
    assert len(text.splitlines()) <= MAX_DETAIL_LINES + 1
    assert text.endswith("… tool detail truncated")


@pytest.mark.parametrize("duration", [True, -1, float("nan"), float("inf"), 10**1000, "100"])
async def test_invalid_duration_never_crashes_or_claims_a_time(
    monkeypatch: pytest.MonkeyPatch, duration: Any
) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test():
        app._show_tool_call({"name": "example", "durationMs": duration}, completed=True)
        assert VIEWS[0].status_text == "example · finished"


@pytest.mark.parametrize(
    ("second_read", "shown"),
    [(True, "finished output"), (False, "Result not in stored history.")],
)
async def test_active_run_reads_a_missing_result_once_more_before_printing(
    monkeypatch: pytest.MonkeyPatch, second_read: bool, shown: str
) -> None:
    app, client = make_app([], monkeypatch)
    route = f"/v1/sessions/{SID}/history"
    # Wisp may report the completion before it stores the tool result.
    later = tool_history("one", "finished output") if second_read else []
    client.replies[("GET", route)] = deque([[], later])
    async with app.run_test():
        app.session_id = SID
        app._begin_run(RID)
        tool = app._show_tool_call({"toolCallId": "one", "name": "read_file"}, completed=True)
        await eventually(lambda: sum(path == route for _, path, _ in client.calls) == 1)
        await eventually(lambda: app._tool_history_token is None)
        assert not tool.settled
        await eventually(lambda: tool.settled)
        assert shown in tool.entry()[1]
        assert sum(path == route for _, path, _ in client.calls) == 2
