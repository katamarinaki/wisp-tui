"""CLI entry point: ``python -m wisp_tui``."""

from __future__ import annotations

import argparse
import os
import re
import sys

from . import __version__
from .app import WispTuiApp
from .app_base import Startup
from .client import ClientError, WispClient
from .config import ConfigError, load_config
from .scrollback import print_transcript
from .sessions import RESUME_QUERY_RE, UUID_RE


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
    except ConfigError as error:
        print(
            f"{parser.prog}: {error} — run `{parser.prog} setup` to create "
            "~/.config/wisp-tui/config.json",
            file=sys.stderr,
        )
        return 1
    client = WispClient(config.url, config.token)
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
    if args.session:
        startup = Startup("session", args.session)
    elif args.resume_latest:
        startup = Startup("continue")
    elif args.resume is not None:
        startup = Startup("resume", args.resume.lower())
    else:
        startup = Startup()
    app = WispTuiApp(client, config, startup)
    # Finished items are already in scrollback; clear the inline area on exit
    # and print what was still live. The mouse stays the terminal's (scroll,
    # select, copy), like Pi.
    app.run(inline=True, inline_no_clear=False, mouse=False)
    print_transcript(app.transcript())
    # Print only after Textual restores the terminal, using the final active
    # session (which may differ from the one requested at startup).
    if app.session_id and UUID_RE.fullmatch(app.session_id):
        print(f"\nTo continue: {parser.prog} --resume {app.session_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
