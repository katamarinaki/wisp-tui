"""Offline updater checks: real temporary Git repos, stubbed dependency install."""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from wisp_tui import update

ROOT = Path(__file__).resolve().parents[1]


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True,
                          check=True).stdout.strip()


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


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Checkout:
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
    repo = Checkout(root, origin)
    original = update._command

    def command(root: Path, args: list[str], code: str,
                allowed: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess[str]:
        if args[0] == str(python):
            if "install" in args:
                repo.pip_calls.append(args)
            return subprocess.CompletedProcess(args, 0, "", "")
        return original(root, args, code, allowed)

    monkeypatch.setattr(update, "_command", command)
    monkeypatch.setattr(update, "_confirm", lambda: True)
    return repo


@pytest.mark.parametrize("branch", ["main", "develop", "no_local_main", "detached"])
def test_update_fetches_fresh_main_and_installs_in_project_venv(
        checkout: Checkout, branch: str, capsys: pytest.CaptureFixture[str]) -> None:
    root = checkout.root
    if branch == "detached":
        git(root, "switch", "--detach")
    elif branch != "main":
        git(root, "switch", "-c", "develop")
    if branch == "no_local_main":
        git(root, "branch", "-d", "main")
    target = commit(checkout.origin, "new main")
    assert git(root, "rev-parse", "origin/main") != target
    assert update.update(root, "wi") == 0
    assert git(root, "branch", "--show-current") == "main"
    assert git(root, "rev-parse", "HEAD") == target
    assert (root / "tracked").read_text() == "new main"
    assert checkout.pip_calls == [[str(root / ".venv/bin/python"), "-m", "pip",
                                   "install", "-r", "requirements.txt"]]
    assert "updated to main" in capsys.readouterr().out
    # Running again refreshes dependencies too (e.g. after an interrupted pip).
    assert update.update(root, "wi") == 0
    assert len(checkout.pip_calls) == 2


@pytest.mark.parametrize("kind", ["tracked", "untracked"])
def test_update_refuses_dirty_checkout(
        checkout: Checkout, kind: str, capsys: pytest.CaptureFixture[str]) -> None:
    root = checkout.root
    before = git(root, "rev-parse", "HEAD")
    (root / kind).write_text("keep this work")
    assert update.update(root, "wi") == 1
    assert "update_dirty_checkout" in capsys.readouterr().err
    assert (root / kind).read_text() == "keep this work"
    assert git(root, "rev-parse", "HEAD") == before
    assert checkout.pip_calls == []


@pytest.mark.parametrize("branch", ["main", "develop"])
def test_update_refuses_local_commits(checkout: Checkout, branch: str,
                                     capsys: pytest.CaptureFixture[str]) -> None:
    root = checkout.root
    git(root, "switch", "-c", "develop")
    if branch == "main":
        git(root, "switch", "main")
    local = commit(root, "keep local commit")
    if branch == "main":
        git(root, "switch", "develop")
    before = git(root, "rev-parse", "HEAD")
    commit(checkout.origin, "upstream commit")
    assert update.update(root, "wi") == 1
    code = ("update_main_has_local_commits" if branch == "main"
            else "update_local_commits_not_on_main")
    assert code in capsys.readouterr().err
    assert git(root, "rev-parse", "HEAD") == before
    assert git(root, "rev-parse", f"refs/heads/{branch}") == local
    assert git(root, "branch", "--show-current") == "develop"
    assert checkout.pip_calls == []


def test_update_refuses_missing_project_venv(
        checkout: Checkout, capsys: pytest.CaptureFixture[str]) -> None:
    (checkout.root / ".venv/bin/python").rename(checkout.root / ".venv/bin/missing")
    commit(checkout.origin, "new main")
    before = git(checkout.root, "rev-parse", "HEAD")
    assert update.update(checkout.root, "wi") == 1
    assert "update_missing_venv" in capsys.readouterr().err
    assert git(checkout.root, "rev-parse", "HEAD") == before
    assert checkout.pip_calls == []


def test_update_refuses_non_checkout(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert update.update(tmp_path, "wi") == 1
    assert "update_not_checkout" in capsys.readouterr().err


@pytest.mark.parametrize("marker", ["MERGE_HEAD", "rebase-merge", "CHERRY_PICK_HEAD"])
def test_update_refuses_in_progress_operation(
        checkout: Checkout, marker: str, capsys: pytest.CaptureFixture[str]) -> None:
    (checkout.root / ".git" / marker).touch()
    assert update.update(checkout.root, "wi") == 1
    assert "update_git_operation_in_progress" in capsys.readouterr().err
    assert checkout.pip_calls == []


def test_update_failure_is_safe_and_dependency_failure_is_retryable(
        checkout: Checkout, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    target = commit(checkout.origin, "new main")
    original = update._command

    def fail_pip(root: Path, args: list[str], code: str,
                 allowed: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess[str]:
        if "install" in args:
            raise update.UpdateError(code)
        return original(root, args, code, allowed)

    monkeypatch.setattr(update, "_command", fail_pip)
    assert update.update(checkout.root, "wi") == 1
    assert "update_dependencies_failed" in capsys.readouterr().err
    assert git(checkout.root, "rev-parse", "HEAD") == target
    monkeypatch.setattr(update, "_command", original)
    assert update.update(checkout.root, "wi") == 0


@pytest.mark.parametrize("answer", [False, True])
def test_update_asks_before_switching_and_decline_changes_nothing(
        checkout: Checkout, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str], answer: bool) -> None:
    root = checkout.root
    git(root, "switch", "-c", "develop")
    before = git(root, "rev-parse", "HEAD")
    target = commit(checkout.origin, "new main")
    asked: list[bool] = []

    def confirm() -> bool:
        asked.append(True)
        return answer

    monkeypatch.setattr(update, "_confirm", confirm)
    assert update.update(root, "wi") == (0 if answer else 1)
    output = capsys.readouterr()
    assert asked == [True]
    assert f"{before[:12]} → {target[:12]} (1 new commits)" in output.out
    if answer:
        assert git(root, "rev-parse", "HEAD") == target
    else:
        assert "wi: update declined. Nothing changed." in output.err
        assert "No reset or stash" not in output.err
        assert git(root, "branch", "--show-current") == "develop"
        assert git(root, "rev-parse", "HEAD") == before
        assert checkout.pip_calls == []


def test_update_does_not_ask_when_already_current(
        checkout: Checkout, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(update, "_confirm", pytest.fail)
    assert update.update(checkout.root, "wi") == 0
    assert len(checkout.pip_calls) == 1


def test_update_refuses_venv_without_pip_before_touching_the_checkout(
        checkout: Checkout, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    root = checkout.root
    before = git(root, "rev-parse", "origin/main")
    commit(checkout.origin, "new main")
    original = update._command

    def no_pip(root: Path, args: list[str], code: str,
               allowed: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess[str]:
        if "--version" in args:
            raise update.UpdateError(code)
        return original(root, args, code, allowed)

    monkeypatch.setattr(update, "_command", no_pip)
    assert update.update(root, "wi") == 1
    assert "update_venv_missing_pip, repair it with scripts/install" in capsys.readouterr().err
    assert git(root, "rev-parse", "origin/main") == before
    assert checkout.pip_calls == []


def test_failed_fetch_never_switches_branches(
        checkout: Checkout, capsys: pytest.CaptureFixture[str]) -> None:
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


@pytest.mark.parametrize("failure", ["exception", "timeout", "exit"])
def test_subprocess_diagnostics_never_echo_raw_output(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    def fail(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        if failure == "exception":
            raise OSError("private diagnostic")
        if failure == "timeout":
            raise subprocess.TimeoutExpired("private diagnostic", 300)
        return subprocess.CompletedProcess(["git"], 1, "private diagnostic",
                                           "private diagnostic")

    monkeypatch.setattr("wisp_tui.update.subprocess.run", fail)
    with pytest.raises(update.UpdateError, match="^safe_code$"):
        update._command(tmp_path, ["git"], "safe_code")


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
        tmp_path: Path, args: list[str]) -> None:
    wi = launcher_checkout(tmp_path)
    root = tmp_path / "app"
    result = subprocess.run([str(wi), "update", *args], cwd=tmp_path,
                            capture_output=True, text=True, check=False)
    assert result.returncode == (0 if args == ["--help"] else 2)
    assert "wi update" in result.stdout + result.stderr
    assert not (root / ".git").exists()


def test_launcher_never_imports_modules_from_the_launch_directory(tmp_path: Path) -> None:
    wi = launcher_checkout(tmp_path)
    launch = tmp_path / "untrusted"
    (launch / "wisp_tui").mkdir(parents=True)
    (launch / "wisp_tui/__init__.py").write_text("open('MARKER', 'w').write('cwd')\n")
    (launch / "wisp_tui/update.py").write_text("raise SystemExit(0)\n")
    result = subprocess.run([str(wi), "update", "--help"], cwd=launch,
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0 and "wi update" in result.stdout
    assert not (launch / "MARKER").exists()
