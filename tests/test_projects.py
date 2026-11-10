"""Project sessions, /thinking and Skill lines; scripted client only."""

from __future__ import annotations

from collections import deque
from pathlib import Path
from typing import Any

import pytest

from tests.helpers import RID, SID
from tests.test_app import chat_text, eventually, make_app, ready
from wisp_tui import __main__ as cli
from wisp_tui import config
from wisp_tui.app import WispTuiApp
from wisp_tui.app_base import Startup
from wisp_tui.client import ClientError, session_path
from wisp_tui.editor import PromptInput as Input
from wisp_tui.runs import SessionState

ROOT = "/work/my project"
RUNS = f"/v1/sessions/{SID}/runs"
COMPLETED: list[dict[str, Any]] = [
    {"seq": 1, "kind": "run.completed", "outcome": {"output": {"text": "Done."}}},
]


def bodies(app_calls: list[tuple[str, str, dict[str, Any] | None]], method: str, path: str) -> Any:
    return [body for called, target, body in app_calls if (called, target) == (method, path)]


async def enter(app: WispTuiApp, pilot: Any, text: str) -> None:
    app.query_one("#input", Input).value = text
    await pilot.press("enter")
    await pilot.pause()


@pytest.mark.parametrize(
    "where, project",
    [
        ("home/code/app", True),
        ("home", False),
        ("/", False),
        # Holds the home folder, so the token too.
        ("", False),
        ("home/.config/wisp-tui", False),
        ("home/.local/state/wisp-tui/sessions", False),
    ],
)
def test_launch_folder_is_the_project_unless_it_exposes_private_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, where: str, project: bool
) -> None:
    home = tmp_path / "home"
    folder = tmp_path / where
    for path in (folder, home / ".config/wisp-tui", home / ".local/state/wisp-tui"):
        path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(config, "CONFIG_DIR", home / ".config/wisp-tui")
    monkeypatch.setattr(config, "STATE_DIR", home / ".local/state/wisp-tui")
    monkeypatch.chdir(folder)
    assert cli.launch_project() == (str(folder.resolve()) if project else None)


async def test_new_session_is_created_saved_and_addressed_with_its_folder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client = make_app(COMPLETED, monkeypatch, Startup(project_root=ROOT))
    runs, patch = session_path(SID, "/runs", ROOT), session_path(SID, "", ROOT)
    selection = {"mode": "advanced", "thinking": False}
    client.replies[("POST", runs)] = deque([{"runId": RID, "status": "queued"}])
    client.replies[("PATCH", patch)] = deque([{"id": SID, "effectiveSelection": selection}])
    async with app.run_test() as pilot:
        await eventually(lambda: ready(app))
        await enter(app, pilot, "hello")
        await eventually(lambda: "Done." in chat_text(app))
        await eventually(lambda: app.active_run is None and app._pending_submission is None)
        assert bodies(client.calls, "POST", "/v1/sessions") == [
            {"origin": "cli", "title": "hello", "projectRoot": ROOT}
        ]
        assert len(bodies(client.calls, "POST", runs)) == 1
        assert app.sessions.project_root(SID) == ROOT
        app._model_answered(selection, SID)
        await eventually(lambda: app._model_selection == selection)
        app._handle_command("/status")
        assert f"Wisp project folder: {ROOT}" in chat_text(app)


@pytest.mark.parametrize("reported, expected", [(None, ROOT), ("/other", "/other")])
async def test_resumed_session_keeps_its_saved_folder_unless_wisp_reports_one(
    monkeypatch: pytest.MonkeyPatch, reported: str | None, expected: str
) -> None:
    app, client = make_app([], monkeypatch, Startup("session", SID, "/launched/elsewhere"))
    app.sessions.record(SID, {"type": "message", "role": "user", "text": "hi"}, project_root=ROOT)
    client.replies[("GET", f"/v1/sessions/{SID}")] = deque([{"id": SID, "projectRoot": reported}])
    async with app.run_test():
        await eventually(lambda: app.session_id == SID and not app._session_change_in_progress)
        assert app._project_root == expected


async def test_refused_folder_becomes_a_desk_session_and_sends_nothing_until_repeated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client = make_app(COMPLETED, monkeypatch, Startup(project_root=ROOT))
    client.replies[("POST", "/v1/sessions")] = deque(
        [ClientError("runtime_http_400:invalid_project_root"), {"id": SID}]
    )
    async with app.run_test() as pilot:
        await eventually(lambda: ready(app))
        await enter(app, pilot, "hello")
        await eventually(lambda: "refused this folder" in chat_text(app))
        assert bodies(client.calls, "POST", "/v1/sessions") == [
            {"origin": "cli", "title": "hello", "projectRoot": ROOT},
            {"origin": "cli", "title": "hello"},
        ]
        assert app.query_one("#input", Input).value == "hello"
        assert not bodies(client.calls, "POST", RUNS)
        await pilot.press("enter")
        await eventually(lambda: "Done." in chat_text(app))
        assert len(bodies(client.calls, "POST", RUNS)) == 1


async def test_other_runtime_errors_are_not_retried_as_a_desk_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client = make_app([], monkeypatch, Startup(project_root=ROOT))
    client.replies[("POST", "/v1/sessions")] = deque([ClientError("runtime_http_500")])
    async with app.run_test() as pilot:
        await eventually(lambda: ready(app))
        await enter(app, pilot, "hello")
        await eventually(lambda: "Could not start a Wisp session" in chat_text(app))
        assert len(bodies(client.calls, "POST", "/v1/sessions")) == 1
        assert app.session_id is None


async def test_thinking_command_toggles_and_refuses_a_model_without_reasoning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client = make_app([], monkeypatch)
    path = f"/v1/sessions/{SID}"
    on = {"mode": "balanced", "thinking": True}
    plain = {"mode": "model", "model": "plain", "thinking": False}
    client.replies[("PATCH", path)] = deque([{"id": SID, "effectiveSelection": on}])
    client.replies[("GET", "/v1/models")] = deque([{"data": [{"id": "plain", "reasoning": False}]}])
    async with app.run_test() as pilot:
        await eventually(lambda: ready(app))
        app._session_loaded(
            SessionState(
                {"id": SID, "effectiveSelection": {"mode": "balanced", "thinking": False}}, [], ""
            ),
            None,
        )
        await enter(app, pilot, "/thinking")
        await eventually(lambda: app._model_selection == on)
        await eventually(lambda: not app._session_change_in_progress)
        assert bodies(client.calls, "PATCH", path) == [{"selection": on}]
        app._session_loaded(SessionState({"id": SID, "effectiveSelection": plain}, [], ""), None)
        await enter(app, pilot, "/thinking on")
        await eventually(lambda: "plain doesn't support thinking" in chat_text(app))
        await enter(app, pilot, "/thinking maybe")
        assert "usage: /thinking [on|off]" in chat_text(app)
        assert len(bodies(client.calls, "PATCH", path)) == 1


async def test_other_slash_lines_go_to_wisp_but_a_mistyped_command_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client = make_app(COMPLETED, monkeypatch)
    async with app.run_test() as pilot:
        await eventually(lambda: ready(app))
        app.session_id = SID
        await enter(app, pilot, "/stauts")
        assert "unknown command /stauts — did you mean /status?" in chat_text(app)
        assert app.query_one("#input", Input).value == ""
        assert not any(method == "POST" for method, _, _ in client.calls)
        await enter(app, pilot, "/deploy staging")
        await eventually(lambda: "Done." in chat_text(app))
        assert [body["input"] for body in bodies(client.calls, "POST", RUNS)] == ["/deploy staging"]
