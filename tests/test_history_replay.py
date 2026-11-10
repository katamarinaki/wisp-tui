"""Reattached runs: printed replay stays labelled; matching text is never run identity."""

from __future__ import annotations

from collections import deque
from typing import Any

import pytest

from tests.helpers import RID, SID, ScriptedClient
from tests.test_app import chat_text, eventually, make_app, ready
from tests.test_runs import record
from wisp_tui.app import WispTuiApp
from wisp_tui.history import ChatMessage, parse_history
from wisp_tui.runs import RunFollower

HISTORY = f"/v1/sessions/{SID}/history"
RUN = f"/v1/runs/{RID}"
LIST = f"/v1/runs?sessionId={SID}&status=queued,running,parked&limit=200"
QUESTION = ChatMessage("user", "A question")
PARTIAL = ChatMessage("assistant", "A partial")
FINAL = ChatMessage("assistant", "A partial answer")
OTHER = "af06ed54-82b0-45e6-af55-7c85ab55e7ea"


def history(messages: list[ChatMessage]) -> list[dict[str, Any]]:
    kinds = {"user": "human", "assistant": "ai", "tool": "tool", "system": "system"}
    return [
        {"values": {"messages": [{"type": kinds[m.role], "content": m.text} for m in messages]}}
    ]


def answers(app: WispTuiApp) -> list[str]:
    return [text for kind, text in app.transcript() if kind == "assistant"]


def test_history_keeps_identical_turns_apart() -> None:
    messages = [QUESTION, FINAL, QUESTION, FINAL]
    assert parse_history(history(messages)) == messages
    assert parse_history(
        [
            {
                "values": {
                    "messages": [
                        {"type": [], "content": "invalid role"},
                        {"type": "ai", "content": "kept", "id": {"invalid": "id"}},
                    ]
                }
            }
        ]
    ) == [ChatMessage("assistant", "kept")]


def configure_reattach(
    app: WispTuiApp, before: list[ChatMessage], after: list[ChatMessage]
) -> None:
    client = app.client
    # Every caller uses the scripted client from make_app.

    assert isinstance(client, ScriptedClient)
    client.replies[("GET", LIST)] = deque([{"runs": [record("running")], "nextCursor": None}])
    client.replies[("GET", HISTORY)] = deque([history(before), history(after)])


async def wait_finished(app: WispTuiApp) -> None:
    try:
        await eventually(
            lambda: (
                app._status_text == ""
                and app.active_run is None
                and not app._session_change_in_progress
            )
        )
    except AssertionError:
        raise AssertionError(
            f"status={app._status_text!r}, active={app.active_run is not None}, "
            f"loading={app._session_change_in_progress}"
        ) from None


@pytest.mark.parametrize(
    "after",
    [
        [QUESTION, PARTIAL],  # Lagging checkpoint.
        [QUESTION, FINAL],  # Already saved, or a legitimate identical previous reply: unknown.
        [ChatMessage("user", "Compacted question"), FINAL],
    ],
)
async def test_reattached_run_output_stays_separately_labelled(
    monkeypatch: pytest.MonkeyPatch, after: list[ChatMessage]
) -> None:
    app, _ = make_app(
        [{"seq": 1, "kind": "run.completed", "outcome": {"output": {"text": FINAL.text}}}],
        monkeypatch,
    )
    before = after
    configure_reattach(app, before, after)
    async with app.run_test():
        await eventually(lambda: ready(app))
        app._open_session(SID)
        await wait_finished(app)
        await eventually(lambda: answers(app) == [before[-1].text, FINAL.text])
        assert "Current run replay — may overlap the saved checkpoint." in chat_text(app)
        assert app._status_text == ""


async def test_reattached_long_history_prints_only_the_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(WispTuiApp, "MAX_CHAT_ITEMS", 5)
    older = [ChatMessage("user" if i % 2 == 0 else "assistant", f"old {i}") for i in range(100)]
    before = [*older, QUESTION]
    after = [*before, FINAL]
    app, _ = make_app(
        [{"seq": 1, "kind": "run.completed", "outcome": {"output": {"text": FINAL.text}}}],
        monkeypatch,
    )
    configure_reattach(app, before, after)
    async with app.run_test() as pilot:
        await eventually(lambda: ready(app))
        app._open_session(SID)
        await wait_finished(app)
        await pilot.pause()
        items = [text for _, text in app.transcript()]
        assert items[0].startswith(f"\u2191 {len(before) - 5} earlier items not shown")
        assert items[1:6] == [message.text for message in before[-5:]]
        assert answers(app)[-1] == FINAL.text
        assert "Current run replay" in chat_text(app)


async def test_reconnect_replays_cursor_once_then_reconciles_gap_and_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(RunFollower, "MAX_FAILURES", 1)
    app, client = make_app([], monkeypatch)
    configure_reattach(app, [QUESTION], [QUESTION, FINAL])
    client.event_batches = deque(
        [
            [{"seq": 1, "kind": "text.delta", "text": "A "}],
            [
                {"seq": 1, "kind": "text.delta", "text": "A "},
                {"seq": 2, "kind": "gap", "text": "A partial"},
            ],
            [
                {"seq": 3, "kind": "text.delta", "text": " answer"},
                {"seq": 4, "kind": "run.completed", "outcome": {"output": {"text": FINAL.text}}},
            ],
        ]
    )
    async with app.run_test() as pilot:
        await eventually(lambda: ready(app))
        app._open_session(SID)
        await eventually(lambda: app._follower is None and app._disconnected)
        app._handle_command("/reconnect")
        await wait_finished(app)
        await pilot.pause()
        assert answers(app) == [FINAL.text]
        assert client.event_requests == [None, 1, 2]
        assert not any(method == "POST" for method, _, _ in client.calls)
