"""First-time configuration, independent of Textual: ``wi setup``."""

from __future__ import annotations

import argparse
import contextlib
import getpass
import json
import os
import re
import sys
import tempfile
from pathlib import Path

from . import config
from .client import ClientError, WispClient, validate_runtime_url

DEFAULT_URL = "http://127.0.0.1:3008"


class SetupError(Exception):
    """A safe diagnostic code; never carries the token or raw runtime output."""


def verify(client: WispClient) -> None:
    health = client.request("GET", "/v1/health", timeout=5.0)
    if not isinstance(health, dict) or health.get("status") != "ready":
        raise SetupError("runtime_not_ready")
    # Health may be public; a small authenticated read proves the token works.
    client.request("GET", "/v1/runs?limit=1", timeout=5.0)


def write_config(path: Path, url: str, token: str) -> None:
    """Write the config atomically; never replace or follow an existing file."""
    try:
        config.ensure_private_directory(path.parent)
    except OSError:
        raise SetupError("setup_write_failed") from None
    temporary = ""
    try:
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".config-", suffix=".tmp")
        with os.fdopen(fd, "w") as target:
            json.dump({"url": url, "token": token}, target)
            target.write("\n")
            target.flush()
            os.fsync(target.fileno())
        os.link(temporary, path)
    except FileExistsError:
        raise SetupError("setup_config_exists") from None
    except OSError:
        raise SetupError("setup_write_failed") from None
    finally:
        if temporary:
            with contextlib.suppress(OSError):
                os.unlink(temporary)


def setup(prog: str) -> int:
    path = config.CONFIG_PATH
    if os.path.lexists(path):
        print(
            f"{prog}: already configured ({path}). Run `{prog} --check` to test it, "
            "or delete the file to set up again.",
            file=sys.stderr,
        )
        return 1
    try:
        url = validate_runtime_url(input(f"Runtime URL [{DEFAULT_URL}]: ").strip() or DEFAULT_URL)
        token = getpass.getpass("Wisp token (input hidden): ").strip()
        client = WispClient(url, token)
        print(f"{prog}: checking the runtime…", flush=True)
        verify(client)
        write_config(path, url, token)
    except EOFError:
        print(f"{prog}: setup_cancelled. Nothing was saved.", file=sys.stderr)
        return 1
    except (ClientError, SetupError, config.ConfigError) as error:
        print(f"{prog}: {error}. Nothing was saved.", file=sys.stderr)
        return 1
    print(f"{prog}: saved {path} (owner-only). Start with: {prog}")
    return 0


def main(argv: list[str] | None = None) -> int:
    name = os.environ.get("WISP_TUI_PROG", "")
    prog = name if re.fullmatch(r"[A-Za-z0-9._-]{1,32}", name) else "wisp-tui"
    parser = argparse.ArgumentParser(
        prog=f"{prog} setup",
        description="Ask for the runtime URL and token (hidden), verify them "
        "against the runtime, then save an owner-only config. "
        "Never overwrites an existing config.",
    )
    parser.parse_args(argv)
    try:
        return setup(prog)
    except KeyboardInterrupt:
        print(f"\n{prog}: setup_interrupted. Nothing was saved.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
