"""CLI entry point: ``python -m wisp_tui``."""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

from . import __version__
from .app import WispTuiApp
from .app_base import ReleaseCheck, Startup
from .client import ClientError, WispClient, valid_project_root
from .config import CONFIG_PATH, STATE_DIR, ConfigError, load_config
from .scrollback import print_footer, print_transcript
from .sessions import RESUME_QUERY_RE, UUID_RE, SessionStore


def launch_project() -> str | None:
    """The launch folder as the Wisp project, like Pi; never the home folder or disk root."""
    try:
        cwd = Path.cwd().resolve()
        home = Path.home().resolve()
    except (OSError, RuntimeError):
        return None
    if cwd in (home, cwd.parent):
        return None
    # Wisp's file tools work in the project without asking: keep our token
    # and the saved conversations out.
    for private in (CONFIG_PATH.parent.resolve(), STATE_DIR.resolve()):
        if private.is_relative_to(cwd) or cwd.is_relative_to(private):
            return None
    return valid_project_root(str(cwd))


def program_name() -> str:
    """The name the launcher was invoked as (e.g. a `wi` symlink)."""
    name = os.environ.get("WISP_TUI_PROG", "")
    return name if re.fullmatch(r"[A-Za-z0-9._-]{1,32}", name) else "wisp-tui"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=program_name(),
        description="Terminal chat client for the local Wisp runtime (loopback only).",
        epilog=f"First-time config: {program_name()} setup. Update the checkout "
        f"from main: {program_name()} update (launcher only; see --help of each).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Authenticated health check only; no TUI, no inference.",
    )
    start = parser.add_mutually_exclusive_group()
    start.add_argument(
        "-c",
        "--continue",
        dest="resume_latest",
        action="store_true",
        help="Continue the most recent locally saved session.",
    )
    start.add_argument(
        "-r",
        "--resume",
        nargs="?",
        const="",
        default=None,
        metavar="ID",
        help="Resume a locally saved session: a picker, or the session "
        "whose ID starts with ID (e.g. the 8 characters shown).",
    )
    start.add_argument("--session", metavar="UUID", help="Open a specific Wisp session.")
    parser.add_argument("--version", action="version", version="wisp-tui " + __version__)
    args = parser.parse_args(argv)
    if args.session is not None and not UUID_RE.fullmatch(args.session):
        parser.error("--session expects a session UUID")
    if args.resume and not RESUME_QUERY_RE.fullmatch(args.resume.lower()):
        parser.error("--resume expects a session ID or prefix (4+ hex characters)")
    try:
        config = load_config()
        client = WispClient(config.url, config.token)
    except (ConfigError, ClientError) as error:
        # Setup only helps with a missing or unreadable config, not a bad value.
        hint = (
            f"run `{parser.prog} setup` to create"
            if isinstance(error, ConfigError) and not str(error).startswith("token_command")
            else "check"
        )
        print(f"{parser.prog}: {error} — {hint} ~/.config/wisp-tui/config.json", file=sys.stderr)
        return 1
    if args.check:
        try:
            health = client.request("GET", "/v1/health", timeout=5.0)
        except ClientError as error:
            print(f"{parser.prog}: {error}", file=sys.stderr)
            return 1
        status = health.get("status") if isinstance(health, dict) else None
        if status != "ready":
            print(f"{parser.prog}: runtime_not_ready", file=sys.stderr)
            return 1
        print(f"{parser.prog}: authenticated, runtime ready. No inference started.")
        return 0
    # Default: a fresh session (created by the first message), like Pi.
    project = launch_project()
    if args.session:
        startup = Startup("session", args.session, project)
    elif args.resume_latest:
        startup = Startup("continue", project_root=project)
    elif args.resume is not None:
        startup = Startup("resume", args.resume.lower(), project)
    else:
        startup = Startup(project_root=project)
    if config.session_retention_days is not None:
        SessionStore().prune(config.session_retention_days)
    release_check = ReleaseCheck(Path(__file__).resolve().parents[2], f"{parser.prog} update")
    app = WispTuiApp(client, config, startup, release_check=release_check)
    # Full screen keeps the composer and footer pinned while the wheel scrolls
    # the chat; the shown chat goes to the terminal's scrollback on exit.
    app.run(mouse=True)
    print_transcript(app.transcript())
    print_footer(app.footer_rows, config.accent_color)
    # Print only after Textual restores the terminal, using the final active
    # session (which may differ from the one requested at startup).
    if app.session_id and UUID_RE.fullmatch(app.session_id):
        print(f"\nTo continue: {parser.prog} --resume {app.session_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
