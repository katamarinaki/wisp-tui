"""Explicit checkout updater, independent of Textual and private runtime config."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys


class UpdateError(Exception):
    """A safe diagnostic, never subprocess output or exception text."""


def _command(root: Path, command: list[str], code: str,
             allowed: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(command, cwd=root, capture_output=True, text=True,
                                timeout=300, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise UpdateError(code) from None
    if result.returncode not in allowed:
        raise UpdateError(code)
    return result


def _confirm() -> bool:
    try:
        return input("Switch to it? [y/N] ").strip().lower() == "y"
    except EOFError:
        return False


def update(root: Path, prog: str) -> int:
    """Fast-forward to freshly fetched origin/main; never reset or stash work."""
    def git(*args: str, code: str = "update_git_failed",
            allowed: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess[str]:
        return _command(root, ["git", *args], code, allowed)

    try:
        top = git("rev-parse", "--show-toplevel", code="update_not_checkout")
        if Path(top.stdout.strip()).resolve() != root.resolve():
            raise UpdateError("update_not_checkout")
        git_dir = Path(git("rev-parse", "--absolute-git-dir").stdout.strip())
        if any((git_dir / name).exists() for name in (
                "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "BISECT_START",
                "rebase-merge", "rebase-apply", "sequencer")):
            raise UpdateError("update_git_operation_in_progress")
        if git("status", "--porcelain", "--untracked-files=all").stdout.strip():
            raise UpdateError("update_dirty_checkout")
        python = root / ".venv/bin/python"
        if not python.is_file():
            raise UpdateError("update_missing_venv")
        # uv-made or half-built venvs lack pip; check before the checkout moves.
        _command(root, [str(python), "-I", "-m", "pip", "--version"],
                 "update_venv_missing_pip, repair it with scripts/install")
        print(f"{prog}: fetching origin/main…", flush=True)
        git("fetch", "--no-tags", "origin",
            "+refs/heads/main:refs/remotes/origin/main", code="update_fetch_failed")
        target = git("rev-parse", "refs/remotes/origin/main").stdout.strip()
        # Refuse even when switching branches would technically be possible:
        # the user must not unknowingly leave unpublished work behind.
        git("merge-base", "--is-ancestor", "HEAD", target,
            code="update_local_commits_not_on_main")
        has_main = git("show-ref", "--verify", "--quiet", "refs/heads/main",
                       allowed=(0, 1)).returncode == 0
        if has_main:
            git("merge-base", "--is-ancestor", "refs/heads/main", target,
                code="update_main_has_local_commits")
        head = git("rev-parse", "HEAD").stdout.strip()
        if head != target:
            count = git("rev-list", "--count", f"{head}..{target}").stdout.strip()
            print(f"{prog}: {head[:12]} → {target[:12]} ({count} new commits). "
                  f"Review them with: git -C {shlex.quote(str(root))} log HEAD..origin/main",
                  flush=True)
            if not _confirm():
                print(f"{prog}: update declined. Nothing changed.", file=sys.stderr)
                return 1
        if has_main:
            git("switch", "--no-guess", "main", code="update_switch_failed")
            git("merge", "--ff-only", target, code="update_fast_forward_failed")
        else:
            git("switch", "--create", "main", "--track", "origin/main",
                code="update_switch_failed")
        print(f"{prog}: refreshing project dependencies…", flush=True)
        _command(root, [str(python), "-m", "pip", "install", "-r",
                        "requirements.txt"], "update_dependencies_failed")
    except UpdateError as error:
        print(f"{prog}: {error}. No reset or stash was performed. "
              "If the checkout already advanced, fix the problem and rerun update.",
              file=sys.stderr)
        return 1
    print(f"{prog}: updated to main ({target[:12]}). Launch {prog} again to use it.")
    return 0


def main(argv: list[str] | None = None) -> int:
    # The launcher passes its symlink name through WISP_TUI_PROG, but importing
    # __main__ would load Textual (which this command may need to repair).
    name = os.environ.get("WISP_TUI_PROG", "")
    prog = name if re.fullmatch(r"[A-Za-z0-9._-]{1,32}", name) else "wisp-tui"
    parser = argparse.ArgumentParser(
        prog=f"{prog} update",
        description="Fetch origin/main, safely switch/fast-forward to main, "
                    "and refresh the project venv. Refuses local changes or "
                    "commits not on main; never resets or stashes.")
    parser.parse_args(argv)
    try:
        return update(Path(__file__).resolve().parents[2], prog)
    except KeyboardInterrupt:
        print(f"{prog}: update_interrupted. Rerun update to finish.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
