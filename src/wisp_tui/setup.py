"""First-time configuration, independent of Textual: ``wi setup``."""

from __future__ import annotations

import argparse
import contextlib
import getpass
import json
import os
import sys
import tempfile
from pathlib import Path

from . import config, program_name
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


def write_config(path: Path, url: str, token: str, token_command: list[str] | None = None) -> None:
    """Write the config atomically; never replace or follow an existing file.

    With ``token_command`` the file stores the command instead of the secret;
    it is run without a shell on every load (see ``config.token_from_command``).
    """
    try:
        config.ensure_private_directory(path.parent)
    except OSError:
        raise SetupError("setup_write_failed") from None
    payload: dict[str, str | list[str]] = {"url": url, "token": token}
    if token_command is not None:
        payload = {"url": url, "token_command": token_command}
    temporary = ""
    try:
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".config-", suffix=".tmp")
        with os.fdopen(fd, "w") as target:
            json.dump(payload, target)
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


def _read_token_stdin() -> str:
    """One line from stdin, for scripted setup; the token is never echoed."""
    line = sys.stdin.readline()
    token = line.strip()
    if not token:
        raise SetupError("token_required")
    return token


def setup(
    prog: str,
    url: str | None = None,
    token: str | None = None,
    token_command: list[str] | None = None,
) -> int:
    path = config.CONFIG_PATH
    if os.path.lexists(path):
        print(
            f"{prog}: already configured ({path}). Run `{prog} --check` to test it, "
            "or delete the file to set up again.",
            file=sys.stderr,
        )
        return 1
    try:
        if url is None:
            url = input(f"Runtime URL [{DEFAULT_URL}]: ").strip() or DEFAULT_URL
        url = validate_runtime_url(url)
        # A token command is verified by running it once, but only the command
        # is stored; the secret itself never reaches the config file.
        if token_command is not None:
            token = config.token_from_command(token_command)
            write_token: str | None = None
        elif token is not None:
            write_token = token
        else:
            token = getpass.getpass("Wisp token (input hidden): ").strip()
            write_token = token
        client = WispClient(url, token)
        print(f"{prog}: checking the runtime…", flush=True)
        verify(client)
        write_config(path, url, write_token or "", token_command)
    except EOFError:
        print(f"{prog}: setup_cancelled. Nothing was saved.", file=sys.stderr)
        return 1
    except (ClientError, SetupError, config.ConfigError) as error:
        print(f"{prog}: {error}. Nothing was saved.", file=sys.stderr)
        return 1
    print(f"{prog}: saved {path} (owner-only). Start with: {prog}")
    return 0


def main(argv: list[str] | None = None) -> int:
    prog = program_name()
    parser = argparse.ArgumentParser(
        prog=f"{prog} setup",
        description="Ask for the runtime URL and token (hidden), verify them "
        "against the runtime, then save an owner-only config. "
        "Never overwrites an existing config. Non-interactive: --url with "
        "--token-stdin (one line) or --token-command (JSON argv list, stored "
        "instead of the secret).",
    )
    parser.add_argument("--url", metavar="URL", help="runtime URL (default: prompt)")
    parser.add_argument(
        "--token-stdin",
        action="store_true",
        help="read the token from the first line of stdin, for scripts",
    )
    parser.add_argument(
        "--token-command",
        metavar="JSON_ARGV",
        help="store this argv list (JSON, no shell) instead of a token, e.g. "
        '\'["pass", "show", "wisp/wi"]\'; it runs on every load',
    )
    args = parser.parse_args(argv)
    token: str | None = None
    token_command: list[str] | None = None
    if args.token_command is not None:
        try:
            parsed: object = json.loads(args.token_command)
        except ValueError:
            print(f"{prog} setup: invalid_token_command. Nothing was saved.", file=sys.stderr)
            return 2
        if not isinstance(parsed, list) or not all(
            isinstance(item, str) and item for item in parsed
        ):
            print(f"{prog} setup: invalid_token_command. Nothing was saved.", file=sys.stderr)
            return 2
        token_command = parsed
    try:
        if args.token_stdin and args.token_command:
            print(
                f"{prog} setup: token_stdin_and_token_command_conflict. Nothing was saved.",
                file=sys.stderr,
            )
            return 2
        if args.token_stdin:
            token = _read_token_stdin()
        return setup(prog, args.url, token, token_command)
    except KeyboardInterrupt:
        print(f"\n{prog}: setup_interrupted. Nothing was saved.", file=sys.stderr)
        return 1
    except SetupError as error:
        print(f"{prog}: {error}. Nothing was saved.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
