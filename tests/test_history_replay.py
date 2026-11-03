"""Checkpoint/replay overlap: never use matching text alone as run identity."""
from __future__ import annotations

import json
from collections import deque
from typing import Any

import pytest
from textual.widgets import Markdown
from wisp_tui.editor import PromptInput as Input

from tests.helpers import RID, SID, RequestGate
from tests.test_app import chat_text, eventually, make_app
from tests.test_runs import record
from wisp_tui.app import WispTuiApp
from wisp_tui.client import ClientError
from wisp_tui.history import ChatMessage, checkpoint_covers_answer, parse_history
from wisp_tui.runs import RunFollower

HISTORY = f"/v1/sessions/{SID}/history"
RUN = f"/v1/runs/{RID}"
LIST = f"/v1/runs?sessionId={SID}&status=queued,running,parked&limit=200"
QUESTION = ChatMessage("user", "A question", "question-1")
PARTIAL = ChatMessage("assistant", "A partial", "answer-1")
FINAL = ChatMessage("assistant", "A partial answer", "answer-1")
OTHER = "af06ed54-82b0-45e6-af55-7c85ab55e7ea"


def history(messages: list[ChatMessage]) -> list[dict[str, Any]]:
    kinds = {"user": "human", "assistant": "ai", "tool": "tool", "system": "system"}
    return [{"values": {"messages": [
        {"type": kinds[m.role], "content": m.text, "id": m.message_id} for m in messages]}}]


def answers(app: WispTuiApp) -> list[str]:
    return [widget.source for widget in app.query("#chat > Markdown")
            if isinstance(widget, Markdown)]


@pytest.mark.parametrize("before, after, answer, covered", [
    ([QUESTION], [QUESTION, FINAL], FINAL.text, True),
    ([QUESTION, PARTIAL], [QUESTION, FINAL], FINAL.text, True),
    ([], [QUESTION, FINAL], FINAL.text, True),
    ([QUESTION, FINAL], [QUESTION, FINAL], FINAL.text, False),
    ([QUESTION, PARTIAL], [QUESTION, PARTIAL], FINAL.text, False),
    ([QUESTION, PARTIAL], [QUESTION, ChatMessage("assistant", FINAL.text)], FINAL.text, False),
    ([QUESTION, PARTIAL], [QUESTION, ChatMessage("assistant", FINAL.text, "other-id")],
     FINAL.text, False),
    ([QUESTION, FINAL], [QUESTION, FINAL, ChatMessage("tool", FINAL.text)], FINAL.text, False),
    ([QUESTION, FINAL], [QUESTION, FINAL, ChatMessage("user", "Next question")], FINAL.text, False),
    ([QUESTION], [QUESTION], FINAL.text, False),
    ([QUESTION], [], FINAL.text, False),
    ([], [QUESTION, FINAL], "", False),
    ([QUESTION, PARTIAL], [ChatMessage("user", "rewritten"), FINAL], FINAL.text, False),
    # Two genuine identical replies must survive as two messages in the checkpoint.
    ([QUESTION, FINAL], [QUESTION, FINAL, ChatMessage("user", "Again", "q2"),
                        ChatMessage("assistant", FINAL.text, "a2")], FINAL.text, True),
])
def test_only_checkpoint_progress_proves_that_a_finished_answer_is_covered(
        before: list[ChatMessage], after: list[ChatMessage], answer: str, covered: bool) -> None:
    assert checkpoint_covers_answer(before, after, answer) is covered


def test_history_retains_message_identity_without_merging_identical_turns() -> None:
    messages = [QUESTION, FINAL, ChatMessage("user", QUESTION.text, "q2"),
                ChatMessage("assistant", FINAL.text, "a2")]
    assert parse_history(history(messages)) == messages
    assert parse_history([{"values": {"messages": [
        {"type": [], "content": "invalid role"},
        {"type": "ai", "content": "kept", "id": {"invalid": "id"}},
    ]}}]) == [ChatMessage("assistant", "kept")]


def configure_reattach(app: WispTuiApp, before: list[ChatMessage],
                       after: list[ChatMessage]) -> None:
    client = app.client
    # Every caller uses the scripted client from make_app.
    from tests.helpers import ScriptedClient
    assert isinstance(client, ScriptedClient)
    client.replies[("GET", LIST)] = deque([{"runs": [record("running")], "nextCursor": None}])
    client.replies[("GET", HISTORY)] = deque([history(before), history(after)])


async def wait_refreshed(app: WispTuiApp) -> None:
    try:
        await eventually(lambda: app._status_state == "" and app.active_run is None
                         and app._history_refresh_token is None and not app._session_busy)
    except AssertionError:
        raise AssertionError(f"status={app._status_state!r}, active={app.active_run is not None}, "
                             f"refresh={app._history_refresh_token is not None}, "
                             f"loading={app._session_busy}") from None


@pytest.mark.parametrize("terminal_record", [False, True])
async def test_reattach_replaces_replay_with_one_canonical_answer(
        monkeypatch: pytest.MonkeyPatch, terminal_record: bool) -> None:
    events = [{"seq": 1, "kind": "text.delta", "text": "A partial"},
              {"seq": 2, "kind": "run.completed", "outcome": {"output": {"text": FINAL.text}}}]
    app, client = make_app(events, monkeypatch)
    configure_reattach(app, [QUESTION, PARTIAL], [QUESTION, FINAL])
    if terminal_record:
        client.replies[("GET", RUN)] = deque([record("completed", output={"text": FINAL.text})])
    async with app.run_test() as pilot:
        await eventually(lambda: "ready" in app._status_state)
        app._open_session(SID)
        await wait_refreshed(app)
        await pilot.pause()
        assert answers(app) == [FINAL.text]
        assert "Current run replay" not in chat_text(app)
        assert "Reattaching" not in chat_text(app)
        assert app._status_state == ""
        assert not str(app.query_one("#input").border_title)
        assert not any(method == "POST" for method, _, _ in client.calls)
        paths = [path for method, path, _ in client.calls]
        assert paths.index(LIST) < paths.index(HISTORY)
        path = app.sessions.path_for(SID)
        assert path is not None
        entries = [json.loads(line) for line in path.read_text().splitlines()]
        assert sum(entry.get("role") == "assistant" for entry in entries) == 1


async def test_genuine_identical_replies_to_different_questions_are_kept(
        monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = make_app([{"seq": 1, "kind": "run.completed", "outcome": {
        "output": {"text": FINAL.text}}}], monkeypatch)
    before = [QUESTION, FINAL]
    after = [*before, ChatMessage("user", "Again", "q2"),
             ChatMessage("assistant", FINAL.text, "a2")]
    configure_reattach(app, before, after)
    async with app.run_test() as pilot:
        await eventually(lambda: "ready" in app._status_state)
        app._open_session(SID)
        await wait_refreshed(app)
        await pilot.pause()
        assert answers(app) == [FINAL.text, FINAL.text]
        assert "Again" in chat_text(app)
        assert "Current run replay" not in chat_text(app)


@pytest.mark.parametrize("after", [
    [QUESTION, PARTIAL],  # Lagging checkpoint.
    [QUESTION, FINAL],  # Already saved, or a legitimate identical previous reply: unknown.
    [ChatMessage("user", "Compacted question"), FINAL],
])
async def test_ambiguous_checkpoints_keep_separately_labelled_run_output(
        monkeypatch: pytest.MonkeyPatch, after: list[ChatMessage]) -> None:
    app, _ = make_app([{"seq": 1, "kind": "run.completed", "outcome": {
        "output": {"text": FINAL.text}}}], monkeypatch)
    before = after  # Equal snapshots cannot prove which run owns this text.
    configure_reattach(app, before, after)
    async with app.run_test() as pilot:
        await eventually(lambda: "ready" in app._status_state)
        app._open_session(SID)
        await wait_refreshed(app)
        await pilot.pause()
        assert answers(app) == [before[-1].text, FINAL.text]
        assert "Current run replay — may overlap the saved checkpoint." in chat_text(app)
        assert app._status_state == ""


async def test_refresh_blocks_new_work_keeps_draft_and_clear_invalidates_callback(
        monkeypatch: pytest.MonkeyPatch) -> None:
    app, client = make_app([{"seq": 1, "kind": "run.completed", "outcome": {
        "output": {"text": FINAL.text}}}], monkeypatch)
    configure_reattach(app, [QUESTION, PARTIAL], [QUESTION, FINAL])
    gate = RequestGate("GET", HISTORY)
    reads = 0

    def hold_refresh(method: str, path: str) -> None:
        nonlocal reads
        if (method, path) == ("GET", HISTORY):
            reads += 1
            if reads == 2:
                gate(method, path)

    client.request_hook = hold_refresh
    async with app.run_test() as pilot:
        await eventually(lambda: "ready" in app._status_state)
        try:
            app._open_session(SID)
            await eventually(gate.entered.is_set)
            assert app.active_run is None
            assert app._history_refresh_token is not None
            editor = app.query_one("#input", Input)
            editor.value = "newer draft"
            await pilot.press("enter")
            app._start_submission("direct submission is blocked too")
            app._new_session("blocked")
            assert editor.value == "newer draft"
            assert not any(method == "POST" for method, _, _ in client.calls)
            app._handle_command("/clear")
        finally:
            gate.release.set()
        await eventually(lambda: app._history_refresh_token is None)
        await pilot.pause()
        assert chat_text(app) == ""
        assert editor.value == "newer draft"


@pytest.mark.parametrize("reply, code", [
    (ClientError("runtime_timeout"), "runtime_timeout"),
    ({"invalid": "history"}, "unexpected_history_shape"),
    (RuntimeError("private exception details"), "internal_error"),
])
async def test_refresh_failure_never_changes_run_outcome_or_erases_answer(
        monkeypatch: pytest.MonkeyPatch, reply: Any, code: str) -> None:
    app, client = make_app([{"seq": 1, "kind": "run.completed", "outcome": {
        "output": {"text": FINAL.text}}}], monkeypatch)
    configure_reattach(app, [QUESTION, PARTIAL], [QUESTION, FINAL])
    client.replies[("GET", HISTORY)] = deque([history([QUESTION, PARTIAL]), reply])
    async with app.run_test() as pilot:
        await eventually(lambda: "ready" in app._status_state)
        app._open_session(SID)
        await eventually(lambda: app._status_state == f"history refresh failed: {code}"
                         and app._history_refresh_token is None)
        await pilot.pause()
        assert app.active_run is None
        assert answers(app) == [PARTIAL.text, FINAL.text]
        assert "private exception details" not in chat_text(app) + app._status_state
        assert not str(app.query_one("#input").border_title)
        assert not any(method == "POST" for method, _, _ in client.calls)


async def test_clear_during_reattached_stream_does_not_reload_cleared_history(
        monkeypatch: pytest.MonkeyPatch) -> None:
    app, client = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        await eventually(lambda: "ready" in app._status_state)
        app._session_loaded({"id": SID}, [QUESTION, PARTIAL], "")
        app._turn_started(RID, history=[QUESTION, PARTIAL])
        app._handle_event({"kind": "text.delta", "text": "Live"})
        await pilot.pause()
        app._handle_command("/clear")
        app._handle_event({"kind": "text.delta", "text": " answer"})
        app._finalize_run("run.completed", {"output": {"text": "Live answer"}})
        await pilot.pause()
        assert answers(app) == ["Live answer"]
        assert "Current run replay" not in chat_text(app)
        assert not any(path == HISTORY for _, path, _ in client.calls)


@pytest.mark.parametrize("change", ["session", "run", "epoch", "token"])
async def test_stale_refresh_cannot_replace_another_view(
        monkeypatch: pytest.MonkeyPatch, change: str) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        await eventually(lambda: "ready" in app._status_state)
        app.session_id = SID
        app._add_message(ChatMessage("assistant", "Keep this view"))
        token = object()
        app._history_refresh_token = token
        epoch = app._chat_epoch
        if change == "session":
            app.session_id = OTHER
        elif change == "run":
            app.active_run = RID
        elif change == "epoch":
            app._chat_epoch += 1
        else:
            app._history_refresh_token = object()
        app._history_refreshed(token, SID, epoch, [QUESTION, PARTIAL], [QUESTION, FINAL], FINAL.text)
        app._history_refresh_failed(token, SID, epoch, "runtime_timeout")
        await pilot.pause()
        assert answers(app) == ["Keep this view"]
        assert "history refresh failed" not in app._status_state
        app._history_refresh_done(token)
        if change == "token":
            assert app._history_refresh_token is not None


async def test_unmount_invalidates_queued_refresh_callbacks(
        monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        await eventually(lambda: "ready" in app._status_state)
        app.session_id = SID
        app._add_message(ChatMessage("assistant", "Keep this view"))
        token = object()
        app._history_refresh_token = token
        epoch = app._chat_epoch
        app.on_unmount()
        app._history_refreshed(token, SID, epoch, [QUESTION], [QUESTION, FINAL], FINAL.text)
        app._history_refresh_failed(token, SID, epoch, "runtime_timeout")
        await pilot.pause()
        assert app._history_refresh_token is None
        assert answers(app) == ["Keep this view"]


async def test_canonical_long_history_still_mounts_only_the_tail(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(WispTuiApp, "MAX_CHAT_ITEMS", 5)
    older = [ChatMessage("user" if i % 2 == 0 else "assistant", f"old {i}", str(i))
             for i in range(100)]
    before = [*older, QUESTION]
    after = [*before, FINAL]
    app, _ = make_app([{"seq": 1, "kind": "run.completed", "outcome": {
        "output": {"text": FINAL.text}}}], monkeypatch)
    configure_reattach(app, before, after)
    async with app.run_test() as pilot:
        await eventually(lambda: "ready" in app._status_state)
        app._open_session(SID)
        await wait_refreshed(app)
        await pilot.pause()
        assert len(app.query("#chat > *")) == 6  # Five messages plus one marker.
        assert app._trimmed_count == len(after) - 5
        assert answers(app)[-1] == FINAL.text
        assert "Current run replay" not in chat_text(app)


async def test_reconnect_replays_cursor_once_then_reconciles_gap_and_completion(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(RunFollower, "MAX_FAILURES", 1)
    app, client = make_app([], monkeypatch)
    configure_reattach(app, [QUESTION], [QUESTION, FINAL])
    client.event_batches = deque([
        [{"seq": 1, "kind": "text.delta", "text": "A "}],
        [{"seq": 1, "kind": "text.delta", "text": "A "},
         {"seq": 2, "kind": "gap", "text": "A partial"}],
        [{"seq": 3, "kind": "text.delta", "text": " answer"},
         {"seq": 4, "kind": "run.completed", "outcome": {"output": {"text": FINAL.text}}}],
    ])
    async with app.run_test() as pilot:
        await eventually(lambda: "ready" in app._status_state)
        app._open_session(SID)
        await eventually(lambda: app._follower is None and app._disconnected)
        app._handle_command("/reconnect")
        await wait_refreshed(app)
        await pilot.pause()
        assert answers(app) == [FINAL.text]
        assert client.event_requests == [None, 1, 2]
        assert not any(method == "POST" for method, _, _ in client.calls)
