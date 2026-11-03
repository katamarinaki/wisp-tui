"""CLI entry point: ``python -m wisp_tui``."""
from __future__ import annotations

import argparse
import sys

from . import __version__
from .app import RESUME_QUERY_RE, Startup, WispTuiApp, UUID_RE
from .client import WispClient, ClientError
from .config import ConfigError, load_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="wisp-tui",
        description="Terminal chat client for the local Wisp runtime (loopback only).")
    parser.add_argument("--check", action="store_true",
                        help="Authenticated health check only; no TUI, no inference.")
    start = parser.add_mutually_exclusive_group()
    start.add_argument("-c", "--continue", dest="resume_latest", action="store_true",
                       help="Continue the most recent locally saved session.")
    start.add_argument("-r", "--resume", nargs="?", const="", default=None, metavar="ID",
                       help="Resume a locally saved session: a picker, or the session "
                            "whose ID starts with ID (e.g. the 8 characters shown).")
    start.add_argument("--session", metavar="UUID",
                       help="Open a specific Wisp session.")
    parser.add_argument("--version", action="version", version="wisp-tui " + __version__)
    args = parser.parse_args(argv)
    if args.session is not None and not UUID_RE.fullmatch(args.session):
        parser.error("--session expects a session UUID")
    if args.resume and not RESUME_QUERY_RE.fullmatch(args.resume.lower()):
        parser.error("--resume expects a session ID or prefix (4+ hex characters)")
    try:
        config = load_config()
    except ConfigError as error:
        print(f"wisp-tui: {error} — create ~/.config/wisp-tui/config.json "
              "(see README; never store it inside the project tree)", file=sys.stderr)
        return 1
    client = WispClient(config.url, config.token)
    if args.check:
        try:
            health = client.request("GET", "/v1/health", timeout=5.0)
        except ClientError as error:
            print(f"wisp-tui: {error}", file=sys.stderr)
            return 1
        status = health.get("status") if isinstance(health, dict) else None
        if status != "ready":
            print("wisp-tui: runtime_not_ready", file=sys.stderr)
            return 1
        print("wisp-tui: authenticated, runtime ready. No inference started.")
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
    # Inline mode: no alternate screen — the chat stays in the terminal's
    # scrollback after quitting, like Pi.
    app.run(inline=True, inline_no_clear=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
