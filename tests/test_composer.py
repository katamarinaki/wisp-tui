"""Prompt recall, restart recovery and private storage, without a runtime."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from collections import deque
from pathlib import Path

import pytest

from tests.helpers import RID, SID
from tests.test_app import chat_text, eventually, make_app
from tests.test_submissions import COMPLETED, URL, pending
from wisp_tui.app_base import Startup
from wisp_tui.client import ClientError
from wisp_tui.composer import (
    MAX_COMPOSER_FILE_BYTES,
    MAX_HISTORY,
    MAX_HISTORY_BYTES,
    ComposerError,
    ComposerState,
    ComposerStore,
    bounded_history,
)
from wisp_tui.editor import PromptInput
from wisp_tui.screens import ApprovalModal
from wisp_tui.submissions import MAX_DRAFT_BYTES, SubmissionStore

OTHER_SID = "af06ed54-82b0-45e6-af55-7c85ab55e7ea"


def seed(root: Path, state: ComposerState) -> ComposerStore:
    store = ComposerStore(URL, root)
    store.save(state)
    store.close()
    return store


def test_private_round_trip_and_runtime_isolation(tmp_path: Path) -> None:
    store = ComposerStore(URL, tmp_path / "state")
    state = ComposerState(("one", "two\nlines"), "unsent 日本語\n[/]", SID)
    store.save(state)
    assert store.load() == state
    for path, mode in ((store.root, 0o700), (store.path, 0o600), (store.lock_path, 0o600)):
        assert stat.S_IMODE(path.stat().st_mode) == mode
    assert "token" not in store.path.read_text() and URL not in store.path.read_text()
    other = ComposerStore("http://127.0.0.1:3009", store.root)
    assert other.load() == ComposerState()
    other.close()
    store.close()
    restarted = ComposerStore(URL, store.root)
    assert restarted.load() == state
    restarted.close()


def test_history_is_bounded_by_count_and_utf8_bytes() -> None:
    prompts = tuple(f"prompt {index}" for index in range(100))
    assert bounded_history(prompts) == prompts[-MAX_HISTORY:]
    assert bounded_history(("one", "one", "", " ", "two", "one")) == ("one", "two", "one")
    large = "🐈" * (MAX_DRAFT_BYTES // 4)
    assert len(bounded_history((large, large + "x", "new"))) == 1
    kept = bounded_history((large, "different" + large[9:], "newest" + large[6:]))
    assert sum(len(prompt.encode()) for prompt in kept) <= MAX_HISTORY_BYTES


@pytest.mark.parametrize(
    "attack",
    [
        "symlink",
        "hardlink",
        "group_readable",
        "fifo",
        "oversized",
        "bad_json",
        "insecure_dir",
        "symlink_dir",
        "lock_symlink",
        "lock_hardlink",
        "lock_fifo",
    ],
)
def test_unsafe_state_is_rejected_without_modifying_it(tmp_path: Path, attack: str) -> None:
    store = seed(tmp_path / "state", ComposerState(("old",), "keep"))
    outside = tmp_path / "outside"
    outside.write_text("do not touch")
    if attack == "symlink":
        store.path.unlink()
        store.path.symlink_to(outside)
    elif attack == "hardlink":
        os.link(store.path, tmp_path / "hardlink")
    elif attack == "group_readable":
        store.path.chmod(0o640)
    elif attack == "fifo":
        store.path.unlink()
        os.mkfifo(store.path, 0o600)
    elif attack == "oversized":
        store.path.write_bytes(b"x" * (MAX_COMPOSER_FILE_BYTES + 1))
    elif attack == "bad_json":
        store.path.write_text("private invalid json")
    elif attack == "insecure_dir":
        store.root.chmod(0o750)
    elif attack == "symlink_dir":
        real = tmp_path / "real"
        store.root.rename(real)
        store.root.symlink_to(real, target_is_directory=True)
    elif attack == "lock_symlink":
        store.lock_path.unlink()
        store.lock_path.symlink_to(outside)
    elif attack == "lock_hardlink":
        os.link(store.lock_path, tmp_path / "hardlink")
    elif attack == "lock_fifo":
        store.lock_path.unlink()
        os.mkfifo(store.lock_path, 0o600)
    with pytest.raises(ComposerError, match=r"^composer_state_"):
        store.load()
    assert outside.read_text() == "do not touch"
    store.close()


@pytest.mark.parametrize(
    "changes",
    [
        {"version": True},
        {"history": [1]},
        {"history": ["x"] * (MAX_HISTORY + 1)},
        {"draft": "🐈" * (MAX_DRAFT_BYTES // 4 + 1)},
        {"session_id": "../outside"},
        {"submission_key": "bad-key"},
        {"submission_key": True},
        {"draft": "\ud800"},
    ],
)
def test_invalid_state_is_rejected(tmp_path: Path, changes: dict[str, object]) -> None:
    store = seed(tmp_path / "state", ComposerState())
    data = json.loads(store.path.read_text())
    data.update(changes)
    store.path.write_text(json.dumps(data))
    with pytest.raises(ComposerError, match=r"^composer_state_"):
        store.load()


def test_other_process_cannot_overwrite_live_draft(tmp_path: Path) -> None:
    store = ComposerStore(URL, tmp_path / "state")
    store.save(ComposerState(draft="first instance"))
    program = (
        "from pathlib import Path\n"
        "from wisp_tui.composer import ComposerStore, ComposerState, ComposerError\n"
        "import sys\n"
        "try:\n"
        "    ComposerStore('http://127.0.0.1:3008', Path(sys.argv[1])).save(ComposerState())\n"
        "except ComposerError as error:\n"
        "    print(str(error))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", program, str(store.root)],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
        env={"PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
    )
    assert result.returncode == 0 and result.stdout.strip() == "composer_state_busy"
    assert store.load().draft == "first instance"
    store.close()


def test_failed_atomic_write_keeps_previous_draft(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = ComposerStore(URL, tmp_path / "state")
    store.save(ComposerState(draft="previous"))

    def fail(fd: int) -> None:
        raise OSError("private filesystem detail")

    monkeypatch.setattr(os, "fsync", fail)
    with pytest.raises(ComposerError, match=r"^composer_state_unavailable$"):
        store.save(ComposerState(draft="new"))
    assert store.load().draft == "previous"
    assert not list(store.root.glob(".composer-*"))
    store.close()


async def test_recall_restores_scratch_and_edits_is_undoable_and_never_sends(
    monkeypatch: pytest.MonkeyPatch, private_state_dir: Path
) -> None:
    seed(private_state_dir, ComposerState(("first", "second\nline")))
    app, client = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        editor = app.query_one("#input", PromptInput)
        editor.value = "current\ndraft"
        await pilot.press("ctrl+p")
        assert editor.value == "second\nline"
        await pilot.press("!", "ctrl+p")
        assert editor.value == "first"
        await pilot.press("ctrl+p")
        assert editor.value == "first"
        await pilot.press("ctrl+n")
        assert editor.value == "second\nline!"
        await pilot.press("ctrl+n", "ctrl+n")
        assert editor.value == "current\ndraft"
        await pilot.press("ctrl+p", "ctrl+z")
        assert editor.value == "current\ndraft"
        await pilot.press("ctrl+y")
        assert editor.value == "second\nline"
        assert not any(method == "POST" for method, _, _ in client.calls)


async def test_only_acknowledged_prompts_enter_persistent_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _ = make_app(COMPLETED, monkeypatch)
    async with app.run_test() as pilot:
        await eventually(lambda: "ready" in app._status_text)
        editor = app.query_one("#input", PromptInput)
        editor.value = "/status"
        await pilot.press("enter")
        editor.value = "accepted\nprompt"
        await pilot.press("enter")
        await eventually(lambda: app._status_text == "")
        assert app.composer.load().history == ("accepted\nprompt",)
        await pilot.press("ctrl+p")
        assert editor.value == "accepted\nprompt"
    restarted, client = make_app([], monkeypatch)
    async with restarted.run_test() as pilot:
        await pilot.press("ctrl+p")
        assert restarted.query_one("#input", PromptInput).value == "accepted\nprompt"
        assert not any(method == "POST" for method, _, _ in client.calls)


async def test_fresh_draft_autosaves_and_restart_restores_without_sending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _ = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        editor = app.query_one("#input", PromptInput)
        editor.value = "unsent\n日本語 [/]"
        await eventually(app.composer.path.exists)
        assert app.composer.load().draft == editor.value
        editor.value = "last moment\ndraft"
    restarted, client = make_app([], monkeypatch)
    async with restarted.run_test() as pilot:
        await pilot.pause()
        assert restarted.query_one("#input", PromptInput).value == "last moment\ndraft"
        assert restarted.session_id is None
        assert "Recovered unsent draft" in chat_text(restarted)
        assert not any(method == "POST" for method, _, _ in client.calls)


async def test_session_draft_waits_for_explicit_resume_and_survives_view_commands(
    monkeypatch: pytest.MonkeyPatch, private_state_dir: Path
) -> None:
    seed(private_state_dir, ComposerState(draft="session draft", session_id=SID))
    app, client = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        await eventually(lambda: "ready" in app._status_text)
        editor = app.query_one("#input", PromptInput)
        assert (editor.value, app.session_id) == ("", None)
        assert "Saved unsent draft" in chat_text(app)
        editor.value = "/status"
        await pilot.press("enter")
        app._flush_composer()
        assert app.composer.load().draft == "session draft"
        editor.value = f"/resume {SID}"
        await pilot.press("enter")
        await eventually(lambda: app.session_id == SID and not app._session_change_in_progress)
        assert editor.value == "session draft"
        assert not any(method == "POST" for method, _, _ in client.calls)


async def test_skill_line_is_saved_as_a_draft_but_a_command_being_typed_is_not(
    monkeypatch: pytest.MonkeyPatch, private_state_dir: Path
) -> None:
    seed(private_state_dir, ComposerState(draft="session draft", session_id=SID))
    app, _ = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        await eventually(lambda: "ready" in app._status_text)
        editor = app.query_one("#input", PromptInput)
        for typed in ("/", "/res", f"/resume {SID[:8]}"):
            editor.value = typed
            await pilot.pause()
            app._flush_composer()
            assert app.composer.load().draft == "session draft"
        editor.value = "/deploy staging"
        await pilot.pause()
        app._flush_composer()
        assert app.composer.load().draft == "/deploy staging"


async def test_resume_does_not_overwrite_text_entered_while_loading(
    monkeypatch: pytest.MonkeyPatch, private_state_dir: Path
) -> None:
    seed(private_state_dir, ComposerState(draft="old draft", session_id=SID))
    app, _ = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        editor = app.query_one("#input", PromptInput)
        editor.value = "newer text"
        app._session_loaded({"id": SID}, [], "")
        await pilot.pause()
        assert editor.value == "newer text"


async def test_pending_restart_preserves_newer_draft_and_retry_identity(
    monkeypatch: pytest.MonkeyPatch, private_state_dir: Path
) -> None:
    original = pending()
    submissions = SubmissionStore(URL, private_state_dir)
    submissions.prepare(original)
    submissions.close()
    seed(private_state_dir, ComposerState(draft="newer\ndraft", session_id=SID))
    app, client = make_app(COMPLETED, monkeypatch)
    async with app.run_test():
        await eventually(lambda: app._recovery_view_token is None)
        editor = app.query_one("#input", PromptInput)
        assert editor.value == "newer\ndraft"
        assert app._pending_submission is not None
        assert app._pending_submission.body() == original.body()
        assert not any(method == "POST" for method, _, _ in client.calls)
        app._handle_command("/retry")
        await eventually(lambda: app._status_text == "")
        assert editor.value == "newer\ndraft"
        assert app.composer.load().draft == editor.value
        bodies = [
            body
            for method, path, body in client.calls
            if method == "POST" and path.endswith("/runs")
        ]
        assert bodies == [original.body()]


@pytest.mark.parametrize("saved_session", [None, OTHER_SID])
async def test_two_clients_preserve_unrelated_draft_through_pending_recovery(
    monkeypatch: pytest.MonkeyPatch, saved_session: str | None
) -> None:
    owner, _ = make_app([], monkeypatch)
    async with owner.run_test():
        await eventually(lambda: "ready" in owner._status_text)
        owner.session_id = saved_session
        owner.query_one("#input", PromptInput).value = "unrelated\nsaved draft"
        owner._flush_composer()
        saved = owner.composer.load()
        competitor, competitor_client = make_app([], monkeypatch)
        competitor_client.replies[("POST", f"/v1/sessions/{SID}/runs")] = deque(
            [ClientError("runtime_timeout")]
        )
        async with competitor.run_test() as pilot:
            await eventually(lambda: "ready" in competitor._status_text)
            assert "composer_state_busy" in chat_text(competitor)
            competitor.session_id = SID
            competitor.query_one("#input", PromptInput).value = "pending request"
            await pilot.press("enter")
            await eventually(lambda: "submission error" in competitor._status_text)
            original = competitor._pending_submission
            assert original is not None
            assert owner.composer.load() == saved

    # Inspection, forced autosave and shutdown must preserve the separate slot.
    inspected, inspected_client = make_app([], monkeypatch)
    async with inspected.run_test() as pilot:
        await eventually(lambda: inspected._recovery_view_token is None)
        assert inspected.query_one("#input", PromptInput).value == original.draft
        assert "Saved unsent draft" in chat_text(inspected)
        inspected._flush_composer()
        assert inspected.composer.load() == saved
        inspected.query_one("#input", PromptInput).value = "/status"
        await pilot.press("enter")
        inspected._flush_composer()
        assert inspected.composer.load() == saved
        assert not any(method == "POST" for method, _, _ in inspected_client.calls)

    resumed, resumed_client = make_app(COMPLETED, monkeypatch)
    async with resumed.run_test() as pilot:
        await eventually(lambda: resumed._recovery_view_token is None)
        assert resumed.composer.load() == saved
        resumed.query_one("#input", PromptInput).value = "/retry"
        await pilot.press("enter")
        await eventually(lambda: resumed._status_text == "")
        assert resumed.query_one("#input", PromptInput).value == ""
        persisted = resumed.composer.load()
        assert (persisted.draft, persisted.session_id, persisted.submission_key) == (
            saved.draft,
            saved.session_id,
            saved.submission_key,
        )
        assert persisted.history == (original.draft,)
        bodies = [
            body
            for method, path, body in resumed_client.calls
            if method == "POST" and path.endswith("/runs")
        ]
        assert bodies == [original.body()]

    recovered, recovered_client = make_app([], monkeypatch)
    if saved_session is not None:
        recovered.startup = Startup("session", saved_session)
        recovered_client.replies[("GET", f"/v1/sessions/{saved_session}")] = deque(
            [{"id": saved_session}]
        )
        recovered_client.replies[("GET", f"/v1/sessions/{saved_session}/history")] = deque([[]])
        recovered_client.replies[
            ("GET", f"/v1/runs?sessionId={saved_session}&status=queued,running,parked&limit=200")
        ] = deque([{"runs": [], "nextCursor": None}])
    async with recovered.run_test() as pilot:
        await eventually(lambda: recovered.query_one("#input", PromptInput).value == saved.draft)
        assert recovered.session_id == saved_session
        assert not any(method == "POST" for method, _, _ in recovered_client.calls)


async def test_explicit_new_text_replaces_unrelated_draft_during_pending_recovery(
    monkeypatch: pytest.MonkeyPatch, private_state_dir: Path
) -> None:
    seed(private_state_dir, ComposerState(draft="unrelated saved draft", session_id=OTHER_SID))
    original = pending()
    submissions = SubmissionStore(URL, private_state_dir)
    submissions.prepare(original)
    submissions.close()
    app, client = make_app([], monkeypatch)
    async with app.run_test():
        await eventually(lambda: app._recovery_view_token is None)
        assert app.composer.load().draft == "unrelated saved draft"
        editor = app.query_one("#input", PromptInput)
        editor.value = "deliberately replacing the saved draft"
        app._flush_composer()
        saved = app.composer.load()
        assert saved.draft == editor.value
        assert saved.session_id == SID
        assert app._pending_submission is not None
        assert app._pending_submission.body() == original.body()
        assert not any(method == "POST" for method, _, _ in client.calls)


@pytest.mark.parametrize("newer", [False, True])
async def test_acknowledged_crash_window_drops_only_the_already_sent_draft(
    monkeypatch: pytest.MonkeyPatch, private_state_dir: Path, newer: bool
) -> None:
    original = pending()
    submissions = SubmissionStore(URL, private_state_dir)
    submissions.prepare(original)
    submissions.acknowledge(original, RID)
    seed(
        private_state_dir,
        ComposerState(
            draft=original.draft,
            session_id=SID,
            submission_key=None if newer else original.idempotency_key,
        ),
    )
    app, client = make_app([], monkeypatch)
    app.startup = Startup("session", SID)
    async with app.run_test():
        await eventually(lambda: app.session_id == SID and not app._session_change_in_progress)
        assert app.query_one("#input", PromptInput).value == (original.draft if newer else "")
        assert app._prompt_history == (original.draft,)
        assert app._pending_submission is None
        assert not any(method == "POST" for method, _, _ in client.calls)


async def test_modal_history_keys_leave_underlying_draft_untouched(
    monkeypatch: pytest.MonkeyPatch, private_state_dir: Path
) -> None:
    seed(private_state_dir, ComposerState(("recalled",)))
    app, client = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        editor = app.query_one("#input", PromptInput)
        editor.value = "keep this"
        app.push_screen(ApprovalModal("example", {}))
        await pilot.pause()
        await pilot.press("ctrl+p", "ctrl+n", "escape")
        assert editor.value == "keep this"
        assert not any(method == "POST" for method, _, _ in client.calls)


async def test_storage_failure_is_safe_and_does_not_block_chat(
    monkeypatch: pytest.MonkeyPatch, private_state_dir: Path
) -> None:
    store = seed(private_state_dir, ComposerState())
    store.path.write_text("private invalid json")
    app, _ = make_app(COMPLETED, monkeypatch)
    async with app.run_test() as pilot:
        await eventually(lambda: "ready" in app._status_text)
        assert "composer_state_unavailable" in chat_text(app)
        assert "private invalid json" not in chat_text(app)
        editor = app.query_one("#input", PromptInput)
        editor.value = "still send"
        await pilot.press("enter")
        await eventually(lambda: app._status_text == "")
        await pilot.press("ctrl+p")
        assert editor.value == "still send"
    assert store.path.read_text() == "private invalid json"


async def test_failed_and_blocked_submissions_do_not_enter_recall_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client = make_app(COMPLETED, monkeypatch)
    client.replies[("POST", f"/v1/sessions/{SID}/runs")] = deque([ClientError("runtime_timeout")])
    async with app.run_test() as pilot:
        await eventually(lambda: "ready" in app._status_text)
        app.session_id = SID
        editor = app.query_one("#input", PromptInput)
        editor.value = "original\nprompt"
        await pilot.press("enter")
        await eventually(lambda: "submission error" in app._status_text)
        assert list(app._prompt_history) == []
        editor.value = "newer draft"
        await pilot.press("enter")
        assert list(app._prompt_history) == []
        app._handle_command("/retry")
        await eventually(lambda: app._status_text == "")
        assert app._prompt_history == ("original\nprompt",)
        assert editor.value == "newer draft"


async def test_oversized_draft_stays_in_editor_without_truncating_saved_text(
    monkeypatch: pytest.MonkeyPatch, private_state_dir: Path
) -> None:
    seed(private_state_dir, ComposerState(draft="previous draft"))
    app, _ = make_app([], monkeypatch)
    async with app.run_test() as pilot:
        editor = app.query_one("#input", PromptInput)
        oversized = "🐈" * (MAX_DRAFT_BYTES // 4 + 1)
        editor.value = oversized
        app._flush_composer()
        assert editor.value == oversized
        assert app.composer.load().draft == "previous draft"
        assert "draft_too_large_to_save" in chat_text(app)
        editor.value = "smaller draft"
        app._flush_composer()
        assert app.composer.load().draft == "smaller draft"
        await pilot.pause()
