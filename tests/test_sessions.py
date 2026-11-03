"""Fresh-start sessions and the private local session record (Pi-style)."""
from __future__ import annotations

import json
import os
import stat
from collections import deque
from pathlib import Path
from typing import Any

import pytest
from wisp_tui.editor import PromptInput as Input

from tests.helpers import RID, SID, ScriptedClient
from tests.test_app import chat_text, eventually, make_app
from wisp_tui import __main__ as cli
from wisp_tui.app import ModelPicker, SessionPicker, Startup, WispTuiApp
from wisp_tui.client import ClientError
from wisp_tui.config import Config
from wisp_tui.sessions import MAX_FILE, MAX_TEXT, SessionStore

OTHER = "0b7e5a52-3c3b-4f7e-9a41-2f8d6c1e9b10"
COMPLETED: list[dict[str, Any]] = [
    {"seq": 1, "kind": "tool_call", "name": "disk_usage", "ok": True, "durationMs": 1200},
    {"seq": 2, "kind": "run.completed", "outcome": {"output": {"text": "Disk is fine."}}},
]


def entries(store: SessionStore, session_id: str) -> list[dict[str, Any]]:
    path = store.path_for(session_id)
    assert path is not None, "no local record"
    return [json.loads(line) for line in path.read_text().splitlines()]


def echo_title(client: ScriptedClient) -> None:
    """Make POST /v1/sessions return the title it was given (like the runtime)."""
    original = client.request

    def request(method: str, path: str, body: dict[str, Any] | None = None,
                timeout: float = 10.0) -> Any:
        result = original(method, path, body, timeout)
        if (method, path) == ("POST", "/v1/sessions") and body and "title" in body:
            return {**result, "title": body["title"]}
        return result

    client.request = request  # type: ignore[method-assign]


# -- storage -------------------------------------------------------------------

def test_record_creates_private_file_with_header(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    assert store.record(SID, {"type": "message", "role": "user", "text": "hi"}, "Title") == ""
    assert store.record(SID, {"type": "message", "role": "assistant", "text": "hello"}) == ""
    path = store.path_for(SID)
    assert path is not None and path.name.endswith(f"_{SID}.jsonl")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) & 0o077 == 0
    lines = entries(store, SID)
    assert lines[0]["type"] == "session" and lines[0]["id"] == SID
    assert lines[0]["title"] == "Title"
    assert [(e["role"], e["text"]) for e in lines[1:]] == [("user", "hi"), ("assistant", "hello")]
    assert all("timestamp" in e for e in lines)


def test_list_latest_and_titles(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    store.record(OTHER, {"type": "message", "role": "user", "text": "first prompt\nmore"})
    store.record(SID, {"type": "message", "role": "user", "text": "ignored"}, "Named")
    os.utime(store.path_for(OTHER) or "", (1_000, 1_000))
    assert store.latest() == SID
    summaries = store.list()
    assert [s.id for s in summaries] == [SID, OTHER]
    assert summaries[0].title == "Named"
    assert summaries[1].title == "first prompt\nmore"
    assert "first prompt more" in summaries[1].label()


@pytest.mark.parametrize("attack", ["symlink", "group_readable", "insecure_dir"])
def test_unsafe_files_are_refused(tmp_path: Path, attack: str) -> None:
    root = tmp_path / "sessions"
    store = SessionStore(root)
    assert store.record(SID, {"text": "x"}) == ""
    path = store.path_for(SID)
    assert path is not None
    if attack == "symlink":
        target = tmp_path / "elsewhere"
        target.write_text("")
        path.unlink()
        path.symlink_to(target)
        store.record(SID, {"text": "y"})
        # Never written through the link (a new regular file may be created).
        assert target.read_text() == ""
        assert path.is_symlink()
    elif attack == "group_readable":
        path.chmod(0o640)
        assert store.path_for(SID) is None
        assert store.list() == []
    else:
        root.chmod(0o755)
        assert store.list() == []
        assert store.record(OTHER, {"text": "z"}) == "local_sessions_insecure"


def test_entries_and_files_are_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = SessionStore(tmp_path / "sessions")
    store.record(SID, {"text": "x" * (MAX_TEXT + 10)})
    assert len(entries(store, SID)[1]["text"]) == MAX_TEXT + 1
    monkeypatch.setattr("wisp_tui.sessions.MAX_FILE", 1)
    assert store.record(SID, {"text": "more"}) == "local_session_full"
    assert MAX_FILE > MAX_TEXT


def test_store_never_raises_on_unusable_location(tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    store = SessionStore(blocker / "sessions")
    assert store.record(SID, {"text": "x"}) != ""
    assert store.list() == [] and store.latest() is None


# -- fresh start -----------------------------------------------------------------

async def test_launch_is_fresh_and_first_message_creates_the_session(
        monkeypatch: pytest.MonkeyPatch) -> None:
    app, client = make_app(COMPLETED, monkeypatch)
    echo_title(client)
    async with app.run_test() as pilot:
        await eventually(lambda: "new session" in app._status_state)
        assert app.session_id is None
        assert [c[:2] for c in client.calls] == [("GET", "/v1/health")]
        editor = app.query_one("#input", Input)
        editor.value = "How full is the disk?\nsecond line"
        await pilot.press("enter")
        await eventually(lambda: app._status_state == "")
        creates = [body for method, path, body in client.calls
                   if (method, path) == ("POST", "/v1/sessions")]
        assert creates == [{"origin": "cli", "title": "How full is the disk?"}]
        runs = [body for method, path, body in client.calls
                if (method, path) == ("POST", f"/v1/sessions/{SID}/runs")]
        assert runs and runs[0] is not None
        assert runs[0]["input"] == "How full is the disk?\nsecond line"
        assert editor.value == ""
        assert "is empty" not in chat_text(app)
        lines = entries(app.sessions, SID)
    assert lines[0]["title"] == "How full is the disk?"
    assert [(e["type"], e.get("role")) for e in lines[1:]] == [
        ("message", "user"), ("tool", None), ("message", "assistant")]
    assert lines[2]["name"] == "disk_usage" and lines[2]["ok"] is True
    assert lines[3]["text"] == "Disk is fine." and lines[3]["runId"] == RID


async def test_failed_session_creation_keeps_draft_and_releases(
        monkeypatch: pytest.MonkeyPatch) -> None:
    app, client = make_app(COMPLETED, monkeypatch)
    client.replies[("POST", "/v1/sessions")] = deque([ClientError("runtime_timeout")])
    async with app.run_test() as pilot:
        await eventually(lambda: "new session" in app._status_state)
        editor = app.query_one("#input", Input)
        editor.value = "keep me"
        await pilot.press("enter")
        await eventually(lambda: "runtime_timeout" in app._status_state)
        assert editor.value == "keep me"
        assert app.session_id is None and not app._session_busy
        assert not any("/runs" in path for _, path, _ in client.calls)
        await pilot.press("enter")  # retry by sending again
        await eventually(lambda: app._status_state == "")
        assert editor.value == ""


async def test_new_command_starts_fresh_without_server_calls(
        monkeypatch: pytest.MonkeyPatch) -> None:
    app, client = make_app(COMPLETED, monkeypatch)
    echo_title(client)
    async with app.run_test() as pilot:
        await eventually(lambda: "new session" in app._status_state)
        app._open_session(SID)
        await eventually(lambda: app.session_id == SID and not app._session_busy)
        calls_before = len(client.calls)
        editor = app.query_one("#input", Input)
        editor.value = "/new Backups review"
        await pilot.press("enter")
        await pilot.pause()
        assert app.session_id is None
        assert len(client.calls) == calls_before
        assert "past answer" not in chat_text(app)
        editor.value = "check the backups"
        await pilot.press("enter")
        await eventually(lambda: app._status_state == "")
        creates = [body for method, path, body in client.calls
                   if (method, path) == ("POST", "/v1/sessions")]
        assert creates == [{"origin": "cli", "title": "Backups review"}]


async def test_model_picker_without_session_creates_one_first(
        monkeypatch: pytest.MonkeyPatch) -> None:
    app, client = make_app([], monkeypatch)
    client.replies[("GET", "/v1/models")] = deque([{"data": [{"id": "model-a"}]}])
    async with app.run_test() as pilot:
        await eventually(lambda: "new session" in app._status_state)
        await pilot.press("f3")
        await eventually(lambda: isinstance(app.screen, ModelPicker))
        assert app.session_id == SID
        assert [c[:2] for c in client.calls].count(("POST", "/v1/sessions")) == 1


# -- continue / resume / explicit -------------------------------------------------

def seed(state: Path, *sessions: tuple[str, str, int]) -> SessionStore:
    store = SessionStore(state / "sessions")
    for session_id, prompt, mtime in sessions:
        store.record(session_id, {"type": "message", "role": "user", "text": prompt})
        os.utime(store.path_for(session_id) or "", (mtime, mtime))
    return store


async def test_continue_opens_most_recent_saved_session(
        monkeypatch: pytest.MonkeyPatch, private_state_dir: Path) -> None:
    seed(private_state_dir, (OTHER, "older", 1_000), (SID, "newer", 2_000))
    app, client = make_app([], monkeypatch)
    app.startup = Startup("continue")
    async with app.run_test():
        await eventually(lambda: app.session_id == SID and not app._session_busy)
        assert "past answer" in chat_text(app)
        assert not any(path.endswith(OTHER) for _, path, _ in client.calls)


async def test_continue_without_saved_sessions_starts_fresh(
        monkeypatch: pytest.MonkeyPatch) -> None:
    app, client = make_app([], monkeypatch)
    app.startup = Startup("continue")
    async with app.run_test():
        await eventually(lambda: "No saved sessions yet" in chat_text(app))
        assert app.session_id is None
        assert [c[:2] for c in client.calls] == [("GET", "/v1/health")]


async def test_resume_picker_lists_saved_sessions_newest_first(
        monkeypatch: pytest.MonkeyPatch, private_state_dir: Path) -> None:
    seed(private_state_dir, (SID, "restart jellyfin", 1_000), (OTHER, "check disks", 2_000))
    app, client = make_app([], monkeypatch)
    app.startup = Startup("resume")
    async with app.run_test() as pilot:
        await eventually(lambda: isinstance(app.screen, SessionPicker))
        picker = app.screen
        assert isinstance(picker, SessionPicker)
        assert [sid for sid, _ in picker.entries] == [OTHER, SID]
        assert picker.entries[1][1].startswith("restart jellyfin")
        await pilot.press("down", "enter")
        await eventually(lambda: app.session_id == SID and not app._session_busy)
        assert "past answer" in chat_text(app)


SIBLING = "3bf1ba87-0000-4000-8000-000000000000"  # shares SID's 8-char prefix


def test_find_matches_prefixes_newest_first(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    for session_id, mtime in ((SID, 1_000), (SIBLING, 2_000), (OTHER, 3_000)):
        store.record(session_id, {"text": "x"})
        os.utime(store.path_for(session_id) or "", (mtime, mtime))
    assert store.find("3bf1ba87") == [SIBLING, SID]
    assert store.find("3BF1BA87-760E") == [SID]
    assert store.find("ffff") == []


async def test_resume_by_unique_prefix_opens_it(
        monkeypatch: pytest.MonkeyPatch, private_state_dir: Path) -> None:
    seed(private_state_dir, (SID, "restart jellyfin", 1_000), (OTHER, "check disks", 2_000))
    app, client = make_app([], monkeypatch)
    app.startup = Startup("resume", "3bf1")
    async with app.run_test():
        await eventually(lambda: app.session_id == SID and not app._session_busy)
        assert "past answer" in chat_text(app)
        assert len(app.screen_stack) == 1


async def test_resume_ambiguous_prefix_offers_only_the_matches(
        monkeypatch: pytest.MonkeyPatch, private_state_dir: Path) -> None:
    seed(private_state_dir, (SID, "one", 1_000), (SIBLING, "two", 2_000),
         (OTHER, "unrelated", 3_000))
    app, client = make_app([], monkeypatch)
    app.startup = Startup("resume", "3bf1ba87")
    async with app.run_test() as pilot:
        await eventually(lambda: isinstance(app.screen, SessionPicker))
        picker = app.screen
        assert isinstance(picker, SessionPicker)
        assert [sid for sid, _ in picker.entries] == [SIBLING, SID]
        assert "3bf1ba87" in picker.heading
        await pilot.press("down", "enter")
        await eventually(lambda: app.session_id == SID and not app._session_busy)


async def test_resume_unknown_prefix_stays_fresh(
        monkeypatch: pytest.MonkeyPatch, private_state_dir: Path) -> None:
    seed(private_state_dir, (SID, "one", 1_000))
    app, client = make_app([], monkeypatch)
    app.startup = Startup("resume", "ffff")
    async with app.run_test():
        await eventually(lambda: "No saved session matches 'ffff'" in chat_text(app))
        assert app.session_id is None
        assert [c[:2] for c in client.calls] == [("GET", "/v1/health")]


async def test_resume_full_id_not_saved_locally_opens_wisp_session(
        monkeypatch: pytest.MonkeyPatch) -> None:
    app, client = make_app([], monkeypatch)
    app.startup = Startup("resume", SID)
    async with app.run_test():
        await eventually(lambda: app.session_id == SID and not app._session_busy)
        assert "past answer" in chat_text(app)


@pytest.mark.parametrize("command, expected", [
    ("/resume 3bf1", None),
    ("/resume 3BF1BA87", None),
    ("/resume nope!", "Usage: /resume"),
])
async def test_resume_command_accepts_a_prefix(
        monkeypatch: pytest.MonkeyPatch, private_state_dir: Path,
        command: str, expected: str | None) -> None:
    seed(private_state_dir, (SID, "one", 1_000), (OTHER, "two", 2_000))
    app, _ = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        await eventually(lambda: "new session" in app._status_state)
        app.query_one("#input", Input).value = command
        await pilot.press("enter")
        if expected is None:
            await eventually(lambda: app.session_id == SID and not app._session_busy)
        else:
            await eventually(lambda: expected in chat_text(app))
            assert app.session_id is None


async def test_resume_command_and_empty_store(monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        await eventually(lambda: "new session" in app._status_state)
        app.query_one("#input", Input).value = "/resume"
        await pilot.press("enter")
        await pilot.pause()
        assert "No saved sessions yet" in chat_text(app)


async def test_missing_server_session_points_to_local_record(
        monkeypatch: pytest.MonkeyPatch, private_state_dir: Path) -> None:
    seed(private_state_dir, (OTHER, "an old conversation", 1_000))
    app, client = make_app([], monkeypatch)
    client.replies[("GET", f"/v1/sessions/{OTHER}")] = deque([
        ClientError("runtime_http_404:session_not_found")])
    app.startup = Startup("session", OTHER)
    async with app.run_test():
        await eventually(lambda: "no longer exists" in chat_text(app))
        assert app.session_id is None and not app._session_busy
        assert f"_{OTHER}.jsonl" in chat_text(app)


def test_record_paths_are_shown_relative_to_home() -> None:
    assert WispTuiApp._display_path(Path.home() / ".local/state/x.jsonl") == "~/.local/state/x.jsonl"
    assert WispTuiApp._display_path(Path("/tmp/x.jsonl")) == "/tmp/x.jsonl"


async def test_approval_decision_is_recorded_with_what_was_approved(
        monkeypatch: pytest.MonkeyPatch) -> None:
    events = [
        {"seq": 1, "kind": "confirmation.required", "id": "prompt-1",
         "toolName": "restart_service", "args": {"name": "jellyfin"}},
        {"seq": 2, "kind": "run.parked"},
        {"seq": 3, "kind": "run.completed", "outcome": {"output": {"text": "restarted"}}},
    ]
    app, client = make_app(events, monkeypatch)
    async with app.run_test() as pilot:
        await eventually(lambda: "new session" in app._status_state)
        app.query_one("#input", Input).value = "restart jellyfin"
        await pilot.press("enter")
        await eventually(lambda: len(app.screen_stack) > 1)
        await pilot.press("y")
        await eventually(lambda: app._status_state == "")
        lines = entries(app.sessions, SID)
    approvals = [e for e in lines if e["type"] == "approval"]
    assert len(approvals) == 1
    assert approvals[0]["tool"] == "restart_service"
    assert approvals[0]["decision"] == "approve"
    assert json.loads(approvals[0]["args"]) == {"name": "jellyfin"}


async def test_record_failure_is_reported_once(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("")
    client = ScriptedClient(COMPLETED)
    app = WispTuiApp(client, Config(url=client.url, token=client.token),
                     sessions=SessionStore(blocker / "sessions"))
    async with app.run_test() as pilot:
        await eventually(lambda: "new session" in app._status_state)
        app.query_one("#input", Input).value = "hello"
        await pilot.press("enter")
        await eventually(lambda: app._status_state == "")
        assert chat_text(app).count("Local session record unavailable") == 1
        assert "Disk is fine." in chat_text(app)


# -- command line ------------------------------------------------------------------

@pytest.mark.parametrize("argv, expected", [
    ([], Startup()),
    (["-c"], Startup("continue")),
    (["--continue"], Startup("continue")),
    (["-r"], Startup("resume")),
    (["-r", "3bf1ba87"], Startup("resume", "3bf1ba87")),
    (["--resume", "3BF1BA87"], Startup("resume", "3bf1ba87")),
    (["--resume", SID], Startup("resume", SID)),
    (["--session", SID], Startup("session", SID)),
])
def test_cli_selects_startup(monkeypatch: pytest.MonkeyPatch, argv: list[str],
                             expected: Startup) -> None:
    client = ScriptedClient([])
    seen: list[Startup] = []
    monkeypatch.setattr(cli, "load_config",
                        lambda: Config(url=client.url, token=client.token))
    monkeypatch.setattr(WispTuiApp, "run",
                        lambda self, **kwargs: seen.append(self.startup))
    assert cli.main(argv) == 0
    assert seen == [expected]


@pytest.mark.parametrize("argv", [
    ["--session", "nope"], ["-c", "-r"], ["-r", "--session", SID],
    ["--resume", "zz"], ["--resume", "abc"], ["-r", "3bf1ba87", "-c"],
])
def test_cli_rejects_bad_combinations(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(argv)
    assert raised.value.code == 2


# -- launcher name -------------------------------------------------------------------

@pytest.mark.parametrize("env, expected", [
    ({}, "wisp-tui"),
    ({"WISP_TUI_PROG": "wi"}, "wi"),
    ({"WISP_TUI_PROG": "bad name; rm -rf"}, "wisp-tui"),
    ({"WISP_TUI_PROG": ""}, "wisp-tui"),
])
def test_program_name_follows_launcher(monkeypatch: pytest.MonkeyPatch,
                                       env: dict[str, str], expected: str) -> None:
    monkeypatch.delenv("WISP_TUI_PROG", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert cli.program_name() == expected


def test_launcher_symlink_uses_its_own_name(tmp_path: Path) -> None:
    """A `wi` symlink to the real launcher resolves the checkout and its name."""
    import subprocess
    root = Path(__file__).resolve().parents[1]
    link = tmp_path / "wi"
    link.symlink_to(root / "scripts" / "wisp-tui")
    result = subprocess.run([str(link), "-h"], capture_output=True, text=True,
                            timeout=30, cwd=tmp_path, env={"PATH": "/usr/bin:/bin"})
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("usage: wi ")
