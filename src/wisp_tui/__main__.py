"""CLI entry point: ``python -m wisp_tui``."""
from __future__ import annotations

import argparse
import sys

from . import __version__
from .app import WispTuiApp
from .client import WispClient, ClientError
from .config import ConfigError, load_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="wisp-tui",
        description="Terminal chat client for the local Wisp runtime (loopback only).")
    parser.add_argument("--check", action="store_true",
                        help="Authenticated health check only; no TUI, no inference.")
    parser.add_argument("--session", metavar="UUID",
                        help="Open a specific session instead of the last one.")
    parser.add_argument("--version", action="version", version="wisp-tui " + __version__)
    args = parser.parse_args(argv)
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
    app = WispTuiApp(client, config)
    if args.session:
        from .config import save_last_session
        save_last_session(args.session)
    # Inline mode: no alternate screen — the chat stays in the terminal's
    # scrollback after quitting, like Pi.
    app.run(inline=True, inline_no_clear=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
