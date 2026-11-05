"""Offline updater checks: real temporary Git repos, stubbed dependency install."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from textual.widgets import Static

from tests.test_app import eventually, make_app
from wisp_tui import update
from wisp_tui.app_base import ReleaseCheck

ROOT = Path(__file__).resolve().parents[1]


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True
    ).stdout.strip()


def commit(root: Path, text: str) -> str:
    (root / "tracked").write_text(text)
    git(root, "add", "tracked")
    git(root, "commit", "-m", "Test change")
    return git(root, "rev-parse", "HEAD")


@dataclass
class Checkout:
    root: Path
    origin: Path
    pip_calls: list[list[str]] = field(default_factory=list)


@dataclass(frozen=True)
class ReleaseKey:
    private: Path
    public: str


def make_key(private: Path) -> ReleaseKey:
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(private)],
        check=True,
        capture_output=True,
    )
    return ReleaseKey(private, " ".join(private.with_suffix(".pub").read_text().split()[:2]))


@pytest.fixture(scope="session")
def release_key(tmp_path_factory: pytest.TempPathFactory) -> ReleaseKey:
    return make_key(tmp_path_factory.mktemp("release-key") / "key")


def release(origin: Path, key: ReleaseKey, name: str) -> str:
    """Tag origin's HEAD as a signed release; returns the tagged commit."""
    git(
        origin,
        "-c",
        "gpg.format=ssh",
        "-c",
        f"user.signingkey={key.private}",
        "tag",
        "-s",
        name,
        "-m",
        name,
    )
    return git(origin, "rev-parse", "HEAD")


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, release_key: ReleaseKey) -> Checkout:
    # Ignore the owner's Git identity/config/hooks, and use a local-path remote:
    # no network, private config, runtime calls or actual pip invocation.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test User")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test User")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.invalid")
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "--initial-branch=main")
    (origin / ".gitignore").write_text(".venv/\n")
    (origin / "requirements.txt").write_text("# test placeholder\n")
    git(origin, "add", ".")
    commit(origin, "initial")
    root = tmp_path / "checkout"
    git(tmp_path, "clone", str(origin), str(root))
    python = root / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.touch()
    # What scripts/install pins: an owner-only allowed-signers file.
    signers = tmp_path / "release-signers"
    signers.write_text(f'wisp-tui-release namespaces="git" {release_key.public}\n')
    signers.chmod(0o600)
    monkeypatch.setattr(update, "RELEASE_SIGNERS", signers)
    repo = Checkout(root, origin)
    original = update._run_checked

    def command(
        root: Path,
        args: list[str],
        code: str,
        allowed: tuple[int, ...] = (0,),
    ) -> subprocess.CompletedProcess[str]:
        if args[0] == str(python):
            if "install" in args:
                repo.pip_calls.append(args)
            return subprocess.CompletedProcess(args, 0, "", "")
        return original(root, args, code, allowed)

    monkeypatch.setattr(update, "_run_checked", command)
    monkeypatch.setattr(update, "_confirm", lambda: True)
    return repo


@pytest.mark.parametrize("branch", ["main", "develop", "no_local_main", "detached"])
def test_update_installs_the_newest_signed_release_in_project_venv(
    checkout: Checkout, release_key: ReleaseKey, branch: str, capsys: pytest.CaptureFixture[str]
) -> None:
    root = checkout.root
    if branch == "detached":
        git(root, "switch", "--detach")
    elif branch != "main":
        git(root, "switch", "-c", "develop")
    if branch == "no_local_main":
        git(root, "branch", "-d", "main")
    release(checkout.origin, release_key, "v0.1.9")
    commit(checkout.origin, "new main")
    target = release(checkout.origin, release_key, "v0.2.0")
    commit(checkout.origin, "unreleased")
    assert update.update(root, "wi") == 0
    assert git(root, "branch", "--show-current") == "main"
    assert git(root, "rev-parse", "HEAD") == target
    assert (root / "tracked").read_text() == "new main"
    assert checkout.pip_calls == [
        [str(root / ".venv/bin/python"), "-m", "pip", "install", "-r", "requirements.txt"]
    ]
    assert "updated to v0.2.0" in capsys.readouterr().out
    # Running again refreshes dependencies too (e.g. after an interrupted pip).
    assert update.update(root, "wi") == 0
    assert len(checkout.pip_calls) == 2


@pytest.mark.parametrize("kind", ["tracked", "untracked"])
def test_update_refuses_dirty_checkout(
    checkout: Checkout, kind: str, capsys: pytest.CaptureFixture[str]
) -> None:
    root = checkout.root
    before = git(root, "rev-parse", "HEAD")
    (root / kind).write_text("keep this work")
    assert update.update(root, "wi") == 1
    assert "update_dirty_checkout" in capsys.readouterr().err
    assert (root / kind).read_text() == "keep this work"
    assert git(root, "rev-parse", "HEAD") == before
    assert checkout.pip_calls == []


@pytest.mark.parametrize("branch", ["main", "develop"])
def test_update_refuses_local_commits(
    checkout: Checkout, release_key: ReleaseKey, branch: str, capsys: pytest.CaptureFixture[str]
) -> None:
    root = checkout.root
    git(root, "switch", "-c", "develop")
    if branch == "main":
        git(root, "switch", "main")
    local = commit(root, "keep local commit")
    if branch == "main":
        git(root, "switch", "develop")
    before = git(root, "rev-parse", "HEAD")
    commit(checkout.origin, "upstream commit")
    release(checkout.origin, release_key, "v0.2.0")
    assert update.update(root, "wi") == 1
    code = (
        "update_main_has_local_commits"
        if branch == "main"
        else "update_local_commits_not_in_release"
    )
    assert code in capsys.readouterr().err
    assert git(root, "rev-parse", "HEAD") == before
    assert git(root, "rev-parse", f"refs/heads/{branch}") == local
    assert git(root, "branch", "--show-current") == "develop"
    assert checkout.pip_calls == []


def test_update_refuses_missing_project_venv(
    checkout: Checkout, capsys: pytest.CaptureFixture[str]
) -> None:
    (checkout.root / ".venv/bin/python").rename(checkout.root / ".venv/bin/missing")
    commit(checkout.origin, "new main")
    before = git(checkout.root, "rev-parse", "HEAD")
    assert update.update(checkout.root, "wi") == 1
    assert "update_missing_venv" in capsys.readouterr().err
    assert git(checkout.root, "rev-parse", "HEAD") == before
    assert checkout.pip_calls == []


def test_update_refuses_non_checkout(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert update.update(tmp_path, "wi") == 1
    assert "update_not_checkout" in capsys.readouterr().err


@pytest.mark.parametrize("marker", ["MERGE_HEAD", "rebase-merge", "CHERRY_PICK_HEAD"])
def test_update_refuses_in_progress_operation(
    checkout: Checkout, marker: str, capsys: pytest.CaptureFixture[str]
) -> None:
    (checkout.root / ".git" / marker).touch()
    assert update.update(checkout.root, "wi") == 1
    assert "update_git_operation_in_progress" in capsys.readouterr().err
    assert checkout.pip_calls == []


def test_update_failure_is_safe_and_dependency_failure_is_retryable(
    checkout: Checkout,
    release_key: ReleaseKey,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    commit(checkout.origin, "new main")
    target = release(checkout.origin, release_key, "v0.2.0")
    original = update._run_checked

    def fail_pip(
        root: Path,
        args: list[str],
        code: str,
        allowed: tuple[int, ...] = (0,),
    ) -> subprocess.CompletedProcess[str]:
        if "install" in args:
            raise update.UpdateError(code)
        return original(root, args, code, allowed)

    monkeypatch.setattr(update, "_run_checked", fail_pip)
    assert update.update(checkout.root, "wi") == 1
    assert "update_dependencies_failed" in capsys.readouterr().err
    assert git(checkout.root, "rev-parse", "HEAD") == target
    monkeypatch.setattr(update, "_run_checked", original)
    assert update.update(checkout.root, "wi") == 0


@pytest.mark.parametrize("answer", [False, True])
def test_update_asks_before_switching_and_decline_changes_nothing(
    checkout: Checkout,
    release_key: ReleaseKey,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    answer: bool,
) -> None:
    root = checkout.root
    git(root, "switch", "-c", "develop")
    before = git(root, "rev-parse", "HEAD")
    commit(checkout.origin, "new main")
    target = release(checkout.origin, release_key, "v0.2.0")
    asked: list[bool] = []

    def confirm() -> bool:
        asked.append(True)
        return answer

    monkeypatch.setattr(update, "_confirm", confirm)
    assert update.update(root, "wi") == (0 if answer else 1)
    output = capsys.readouterr()
    assert asked == [True]
    assert f"{before[:12]} → v0.2.0 {target[:12]} (1 new commits)" in output.out
    if answer:
        assert git(root, "rev-parse", "HEAD") == target
    else:
        assert "wi: update declined. Nothing changed." in output.err
        assert "No reset or stash" not in output.err
        assert git(root, "branch", "--show-current") == "develop"
        assert git(root, "rev-parse", "HEAD") == before
        assert checkout.pip_calls == []


def test_update_does_not_ask_when_already_current(
    checkout: Checkout, release_key: ReleaseKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    release(checkout.origin, release_key, "v0.1.0")
    monkeypatch.setattr(update, "_confirm", pytest.fail)
    assert update.update(checkout.root, "wi") == 0
    assert len(checkout.pip_calls) == 1


def test_update_refuses_venv_without_pip_before_touching_the_checkout(
    checkout: Checkout,
    release_key: ReleaseKey,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = checkout.root
    before = git(root, "rev-parse", "HEAD")
    commit(checkout.origin, "new main")
    release(checkout.origin, release_key, "v0.2.0")
    original = update._run_checked

    def no_pip(
        root: Path,
        args: list[str],
        code: str,
        allowed: tuple[int, ...] = (0,),
    ) -> subprocess.CompletedProcess[str]:
        if "--version" in args:
            raise update.UpdateError(code)
        return original(root, args, code, allowed)

    monkeypatch.setattr(update, "_run_checked", no_pip)
    assert update.update(root, "wi") == 1
    assert "update_venv_missing_pip, repair it with scripts/install" in capsys.readouterr().err
    assert git(root, "rev-parse", "HEAD") == before
    assert git(root, "for-each-ref", update.RELEASE_REFS) == ""
    assert checkout.pip_calls == []


def test_failed_fetch_never_switches_branches(
    checkout: Checkout, capsys: pytest.CaptureFixture[str]
) -> None:
    root = checkout.root
    git(root, "switch", "-c", "develop")
    before = git(root, "rev-parse", "HEAD")
    # A missing local directory fails without any network access.
    git(root, "remote", "set-url", "origin", str(root / "missing-remote"))
    assert update.update(root, "wi") == 1
    output = capsys.readouterr()
    assert "update_fetch_failed" in output.err
    assert "missing-remote" not in output.err + output.out
    assert git(root, "branch", "--show-current") == "develop"
    assert git(root, "rev-parse", "HEAD") == before
    assert checkout.pip_calls == []


def unlisted_key(origin: Path, key: ReleaseKey) -> None:
    release(origin, make_key(origin.parent / "other-key"), "v0.2.0")


def unsigned_annotated(origin: Path, key: ReleaseKey) -> None:
    git(origin, "tag", "-a", "v0.2.0", "-m", "v0.2.0")


def lightweight(origin: Path, key: ReleaseKey) -> None:
    git(origin, "tag", "v0.2.0")


def replayed_under_newer_name(origin: Path, key: ReleaseKey) -> None:
    # A genuinely signed tag object, published again under a higher version.
    release(origin, key, "v0.1.5")
    git(origin, "update-ref", "refs/tags/v0.2.0", git(origin, "rev-parse", "v0.1.5"))


@pytest.mark.parametrize(
    "publish, code",
    [
        (unlisted_key, "update_release_signature_invalid"),
        (unsigned_annotated, "update_release_not_signed"),
        (lightweight, "update_release_not_signed"),
        (replayed_under_newer_name, "update_release_not_signed"),
    ],
)
def test_update_refuses_releases_not_signed_by_a_pinned_key(
    checkout: Checkout,
    release_key: ReleaseKey,
    capsys: pytest.CaptureFixture[str],
    publish: Callable[[Path, ReleaseKey], None],
    code: str,
) -> None:
    before = git(checkout.root, "rev-parse", "HEAD")
    commit(checkout.origin, "untrusted")
    publish(checkout.origin, release_key)
    assert update.update(checkout.root, "wi") == 1
    assert code in capsys.readouterr().err
    assert git(checkout.root, "rev-parse", "HEAD") == before
    assert checkout.pip_calls == []


@pytest.mark.parametrize("problem", ["missing", "group_readable", "symlink"])
def test_update_refuses_an_unpinned_or_unsafe_signers_file(
    checkout: Checkout,
    release_key: ReleaseKey,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    problem: str,
) -> None:
    signers = update.RELEASE_SIGNERS
    if problem == "missing":
        signers.unlink()
    elif problem == "group_readable":
        signers.chmod(0o640)
    else:
        link = signers.with_name("linked-signers")
        link.symlink_to(signers)
        monkeypatch.setattr(update, "RELEASE_SIGNERS", link)
    commit(checkout.origin, "new main")
    release(checkout.origin, release_key, "v0.2.0")
    before = git(checkout.root, "rev-parse", "HEAD")
    assert update.update(checkout.root, "wi") == 1
    code = (
        "update_release_key_not_pinned"
        if problem == "missing"
        else "update_release_signers_insecure"
    )
    assert code in capsys.readouterr().err
    assert git(checkout.root, "rev-parse", "HEAD") == before
    assert git(checkout.root, "for-each-ref", update.RELEASE_REFS) == ""


def test_update_never_moves_a_checkout_back_to_an_older_release(
    checkout: Checkout, release_key: ReleaseKey, capsys: pytest.CaptureFixture[str]
) -> None:
    release(checkout.origin, release_key, "v0.1.0")
    commit(checkout.origin, "after the release")
    git(checkout.root, "pull", "--ff-only")
    before = git(checkout.root, "rev-parse", "HEAD")
    assert update.update(checkout.root, "wi") == 0
    assert "already ahead of v0.1.0" in capsys.readouterr().out
    assert git(checkout.root, "rev-parse", "HEAD") == before
    assert checkout.pip_calls == []


def test_newer_release_picks_the_highest_strict_version_above_current() -> None:
    listing = "\n".join(
        f"{index}\trefs/tags/{name}"
        for index, name in enumerate(
            ["v0.1.0", "v0.9.0", "v0.10.0", "v1.0.0-rc1", "v01.2.0", "latest"]
        )
    )
    assert update.newer_release(listing, "0.1.0") == "v0.10.0"
    assert update.newer_release(listing, "0.10.0") is None
    assert update.newer_release(listing, "dev") is None


@pytest.mark.parametrize("newest, notice", [("v9.0.0", True), ("v0.1.0", False)])
async def test_startup_notice_names_a_newer_release_tag_from_origin(
    checkout: Checkout,
    release_key: ReleaseKey,
    monkeypatch: pytest.MonkeyPatch,
    newest: str,
    notice: bool,
) -> None:
    release(checkout.origin, release_key, newest)
    app, _ = make_app([], monkeypatch)
    app._release_check = ReleaseCheck(checkout.root, "wi update")
    async with app.run_test() as pilot:
        line = app.query_one("#release-notice", Static)
        if notice:
            await eventually(lambda: line.display)
            assert str(line.render()) == f"wisp-tui {newest} is available · run wi update"
        else:
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert not line.display


@pytest.mark.parametrize("failure", ["exception", "timeout", "exit"])
def test_subprocess_diagnostics_never_echo_raw_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    def fail(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        if failure == "exception":
            raise OSError("private diagnostic")
        if failure == "timeout":
            raise subprocess.TimeoutExpired("private diagnostic", 300)
        return subprocess.CompletedProcess(["git"], 1, "private diagnostic", "private diagnostic")

    monkeypatch.setattr("wisp_tui.update.subprocess.run", fail)
    with pytest.raises(update.UpdateError, match=r"^safe_code$"):
        update._run_checked(tmp_path, ["git"], "safe_code")


def launcher_checkout(tmp_path: Path) -> Path:
    """A minimal checkout with only the launcher and updater; returns a `wi` symlink."""
    root = tmp_path / "app"
    (root / "scripts").mkdir(parents=True)
    (root / "src/wisp_tui").mkdir(parents=True)
    (root / ".venv/bin").mkdir(parents=True)
    shutil.copy2(ROOT / "scripts/wisp-tui", root / "scripts/wisp-tui")
    shutil.copy2(ROOT / "src/wisp_tui/update.py", root / "src/wisp_tui/update.py")
    (root / "src/wisp_tui/__init__.py").touch()
    # No __main__, app, config or Textual modules are provided in this checkout.
    (root / ".venv/bin/python").symlink_to(sys.executable)
    wi = tmp_path / "wi"
    wi.symlink_to(root / "scripts/wisp-tui")
    return wi


@pytest.mark.parametrize("args", [["--help"], ["--check"]])
def test_symlink_launcher_dispatches_update_without_textual_or_config(
    tmp_path: Path, args: list[str]
) -> None:
    wi = launcher_checkout(tmp_path)
    root = tmp_path / "app"
    result = subprocess.run(
        [str(wi), "update", *args], cwd=tmp_path, capture_output=True, text=True, check=False
    )
    assert result.returncode == (0 if args == ["--help"] else 2)
    assert "wi update" in result.stdout + result.stderr
    assert not (root / ".git").exists()


def test_launcher_never_imports_modules_from_the_launch_directory(tmp_path: Path) -> None:
    wi = launcher_checkout(tmp_path)
    launch = tmp_path / "untrusted"
    (launch / "wisp_tui").mkdir(parents=True)
    (launch / "wisp_tui/__init__.py").write_text("open('MARKER', 'w').write('cwd')\n")
    (launch / "wisp_tui/update.py").write_text("raise SystemExit(0)\n")
    result = subprocess.run(
        [str(wi), "update", "--help"], cwd=launch, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0 and "wi update" in result.stdout
    assert not (launch / "MARKER").exists()
