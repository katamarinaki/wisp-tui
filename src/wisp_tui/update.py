"""Explicit checkout updater, independent of Textual and private runtime config.

Releases are SSH-signed ``vX.Y.Z`` tags. A tag is accepted only when it verifies
against the owner-only allowed-signers file that ``scripts/install`` pinned,
never against a key fetched during the update.
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import stat
import subprocess
import sys
from pathlib import Path

RELEASE_TAG = re.compile(r"v(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})")
# Fetched release tags live here, so a fetch never moves the user's own tags.
RELEASE_REFS = "refs/wisp-tui/releases/"
RELEASE_SIGNERS = Path.home() / ".config/wisp-tui/release-signers"
# Files Git leaves in the git dir while a merge, rebase, bisect or similar runs.
GIT_OPERATION_MARKERS = (
    "MERGE_HEAD",
    "CHERRY_PICK_HEAD",
    "REVERT_HEAD",
    "BISECT_START",
    "rebase-merge",
    "rebase-apply",
    "sequencer",
)


class UpdateError(Exception):
    """A safe diagnostic, never subprocess output or exception text."""


def _run_checked(
    root: Path,
    command: list[str],
    error_code: str,
    allowed_returncodes: tuple[int, ...] = (0,),
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command, cwd=root, capture_output=True, text=True, timeout=300, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        raise UpdateError(error_code) from None
    if result.returncode not in allowed_returncodes:
        raise UpdateError(error_code)
    return result


def release_version(name: str) -> tuple[int, int, int] | None:
    match = RELEASE_TAG.fullmatch(name)
    return None if match is None else (int(match[1]), int(match[2]), int(match[3]))


# Lists origin's release tags. Run detached from the terminal (no stdin, new
# session) so neither Git nor ssh can prompt over the TUI.
RELEASE_QUERY = ("git", "ls-remote", "--tags", "--refs", "origin", "refs/tags/v*")


def release_query_env() -> dict[str, str]:
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0", "SSH_ASKPASS_REQUIRE": "never"}


def newer_release(ls_remote: str, current: str) -> str | None:
    """The newest ``vX.Y.Z`` tag above ``current``; a hint, never verified here."""
    installed = release_version("v" + current)
    if installed is None:
        return None
    best: tuple[tuple[int, int, int], str] | None = None
    for line in ls_remote.splitlines():
        name = line.partition("\t")[2].removeprefix("refs/tags/")
        version = release_version(name)
        if version is not None and version > installed and (best is None or version > best[0]):
            best = (version, name)
    return None if best is None else best[1]


def _pinned_signers() -> Path:
    try:
        info = RELEASE_SIGNERS.lstat()
    except OSError:
        raise UpdateError("update_release_key_not_pinned, run scripts/install") from None
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
        or info.st_nlink != 1
    ):
        raise UpdateError("update_release_signers_insecure")
    return RELEASE_SIGNERS


def _verify_release(root: Path, ref: str, name: str, signers: Path) -> str:
    """Return the release commit if ``ref`` is a tag object signed by a pinned key."""

    def git(*args: str) -> str:
        return _run_checked(root, ["git", *args], "update_release_not_signed").stdout

    if git("cat-file", "-t", ref).strip() != "tag":
        raise UpdateError("update_release_not_signed")
    header, _, body = git("cat-file", "tag", ref).partition("\n\n")
    # A validly signed older tag must not be replayed under a newer name.
    if f"tag {name}" not in header.splitlines() or "type commit" not in header.splitlines():
        raise UpdateError("update_release_not_signed")
    # Only SSH signatures: a PGP one would be checked against the user's keyring.
    if "-----BEGIN SSH SIGNATURE-----" not in body:
        raise UpdateError("update_release_not_signed")
    output = _run_checked(
        root,
        [
            "git",
            "-c",
            f"gpg.ssh.allowedSignersFile={signers}",
            "-c",
            "gpg.minTrustLevel=fully",
            "verify-tag",
            "--raw",
            ref,
        ],
        "update_release_signature_invalid",
    )
    lines = (output.stdout + output.stderr).splitlines()
    # An unlisted key still prints "Good ... signature with", never "... for <principal>".
    if not any(line.startswith('Good "git" signature for ') for line in lines):
        raise UpdateError("update_release_signature_invalid")
    return git("rev-parse", f"{ref}^{{commit}}").strip()


def _latest_release(root: Path) -> str:
    refs = _run_checked(
        root,
        ["git", "for-each-ref", "--format=%(refname)", RELEASE_REFS],
        "update_git_failed",
    ).stdout.split()
    versions = [
        (version, ref.removeprefix(RELEASE_REFS))
        for ref in refs
        if (version := release_version(ref.removeprefix(RELEASE_REFS))) is not None
    ]
    if not versions:
        raise UpdateError("update_no_release")
    return max(versions)[1]


def _confirm() -> bool:
    try:
        return input("Switch to it? [y/N] ").strip().lower() == "y"
    except EOFError:
        return False


def update(root: Path, prog: str) -> int:
    """Fast-forward to the newest signed release; never reset or stash work."""

    def git(
        *args: str,
        error_code: str = "update_git_failed",
        allowed_returncodes: tuple[int, ...] = (0,),
    ) -> subprocess.CompletedProcess[str]:
        return _run_checked(root, ["git", *args], error_code, allowed_returncodes)

    try:
        toplevel = git("rev-parse", "--show-toplevel", error_code="update_not_checkout")
        if Path(toplevel.stdout.strip()).resolve() != root.resolve():
            raise UpdateError("update_not_checkout")
        git_dir = Path(git("rev-parse", "--absolute-git-dir").stdout.strip())
        if any((git_dir / name).exists() for name in GIT_OPERATION_MARKERS):
            raise UpdateError("update_git_operation_in_progress")
        if git("status", "--porcelain", "--untracked-files=all").stdout.strip():
            raise UpdateError("update_dirty_checkout")
        python = root / ".venv/bin/python"
        if not python.is_file():
            raise UpdateError("update_missing_venv")
        # uv-made or half-built venvs lack pip; check before the checkout moves.
        _run_checked(
            root,
            [str(python), "-I", "-m", "pip", "--version"],
            "update_venv_missing_pip, repair it with scripts/install",
        )
        signers = _pinned_signers()
        print(f"{prog}: fetching releases…", flush=True)
        git(
            "fetch",
            "--no-tags",
            "--prune",
            "origin",
            f"+refs/tags/v*:{RELEASE_REFS}v*",
            error_code="update_fetch_failed",
        )
        release = _latest_release(root)
        target = _verify_release(root, RELEASE_REFS + release, release, signers)
        head = git("rev-parse", "HEAD").stdout.strip()
        if (
            head != target
            and git(
                "merge-base", "--is-ancestor", target, "HEAD", allowed_returncodes=(0, 1)
            ).returncode
            == 0
        ):
            print(f"{prog}: this checkout is already ahead of {release}. Nothing changed.")
            return 0
        # Refuse even when switching branches would technically be possible:
        # the user must not unknowingly leave unpublished work behind.
        git(
            "merge-base",
            "--is-ancestor",
            "HEAD",
            target,
            error_code="update_local_commits_not_in_release",
        )
        has_main = (
            git(
                "show-ref", "--verify", "--quiet", "refs/heads/main", allowed_returncodes=(0, 1)
            ).returncode
            == 0
        )
        if has_main:
            git(
                "merge-base",
                "--is-ancestor",
                "refs/heads/main",
                target,
                error_code="update_main_has_local_commits",
            )
        if head != target:
            count = git("rev-list", "--count", f"{head}..{target}").stdout.strip()
            print(
                f"{prog}: {head[:12]} → {release} {target[:12]} ({count} new commits). "
                f"Review them with: git -C {shlex.quote(str(root))} log HEAD..{RELEASE_REFS}"
                f"{release}",
                flush=True,
            )
            if not _confirm():
                print(f"{prog}: update declined. Nothing changed.", file=sys.stderr)
                return 1
        if has_main:
            git("switch", "--no-guess", "main", error_code="update_switch_failed")
            git("merge", "--ff-only", target, error_code="update_fast_forward_failed")
        else:
            git("switch", "--create", "main", target, error_code="update_switch_failed")
        print(f"{prog}: refreshing project dependencies…", flush=True)
        _run_checked(
            root,
            [str(python), "-m", "pip", "install", "-r", "requirements.txt"],
            "update_dependencies_failed",
        )
    except UpdateError as error:
        print(
            f"{prog}: {error}. No reset or stash was performed. "
            "If the checkout already advanced, fix the problem and rerun update.",
            file=sys.stderr,
        )
        return 1
    print(f"{prog}: updated to {release} ({target[:12]}). Launch {prog} again to use it.")
    return 0


def main(argv: list[str] | None = None) -> int:
    # The launcher passes its symlink name through WISP_TUI_PROG, but importing
    # __main__ would load Textual (which this command may need to repair).
    name = os.environ.get("WISP_TUI_PROG", "")
    prog = name if re.fullmatch(r"[A-Za-z0-9._-]{1,32}", name) else "wisp-tui"
    parser = argparse.ArgumentParser(
        prog=f"{prog} update",
        description="Fetch release tags, verify the newest against the pinned "
        "release key, fast-forward main to it and refresh the project venv. "
        "Refuses local changes or commits not in the release; never resets or stashes.",
    )
    parser.parse_args(argv)
    try:
        return update(Path(__file__).resolve().parents[2], prog)
    except KeyboardInterrupt:
        print(f"{prog}: update_interrupted. Rerun update to finish.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
