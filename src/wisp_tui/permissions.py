"""Apply a reviewed permission profile: ``scripts/permissions`` (``wi permissions``).

A profile is the JSON file format ``wi audit --profile`` consumes (see
``profiles/`` in the repo). This tool turns its ``permissions`` map into
explicit ``wisp permissions set TOOL RULE`` commands and — with ``--apply`` —
runs them through the runtime CLI as an argv list, never a shell. Without
``--apply`` it only prints the plan. Details are safe codes and tool names;
nothing about the token or config is touched. Applying a profile is an
owner-run write action: nothing here auto-applies, and there is no
auto-approval path in the chat client.
"""

from __future__ import annotations

import argparse
import shlex
import sys
from pathlib import Path

from . import audit, program_name

DEFAULT_CLI = ["sudo", "-u", audit.SERVICE_USER, "-H", f"/home/{audit.SERVICE_USER}/.wisp/bin/wisp"]
BUILTIN_PROFILES = ("pi-like", "safe")


class PermissionsError(Exception):
    """A safe diagnostic code; never carries secrets or raw command output."""


def builtin_profile_path(name: str) -> Path:
    """Profiles shipped in the checkout: ``<repo>/profiles/<name>.json``."""
    return Path(__file__).resolve().parents[2] / "profiles" / f"{name}.json"


def resolve_profile(name: str) -> Path:
    if name in BUILTIN_PROFILES:
        return builtin_profile_path(name)
    return Path(name)


def plan_commands(cli: list[str], profile: audit.Profile) -> list[list[str]]:
    return [
        [*cli, "permissions", "set", tool, rule]
        for tool, rule in sorted(profile.permissions.items())
    ]


def apply_profile(
    runner: audit.CommandRunner, cli: list[str], profile: audit.Profile
) -> list[audit.Check]:
    commands = plan_commands(cli, profile)
    if not commands:
        return [audit.Check("policy", audit.SKIP, "profile_has_no_permissions")]
    checks: list[audit.Check] = []
    for command in commands:
        tool = command[-2]
        try:
            result = runner(command)
        except audit.AuditError as error:
            checks.append(audit.Check("policy", audit.FAIL, f"{tool}: {error}"))
            continue
        if result.returncode != 0:
            checks.append(audit.Check("policy", audit.FAIL, f"{tool}: command_failed"))
        else:
            checks.append(audit.Check("policy", audit.PASS, f"{tool}: set"))
    return checks


def main(argv: list[str] | None = None) -> int:
    prog = program_name()
    command = f"{prog} permissions" if prog != "permissions" else "permissions"
    parser = argparse.ArgumentParser(
        prog=command,
        description="Print (or with --apply, run) the `wisp permissions set` commands "
        "for a profile: a builtin name ("
        + ", ".join(BUILTIN_PROFILES)
        + ") or a JSON file path. The profile format is shared with "
        "`wi audit --profile`; only its permissions map is applied here.",
    )
    parser.add_argument(
        "--profile",
        metavar="NAME|FILE",
        default="safe",
        help=f"builtin ({', '.join(BUILTIN_PROFILES)}) or path to a profile JSON",
    )
    parser.add_argument(
        "--wisp-cli",
        metavar="COMMAND",
        default=shlex.join(DEFAULT_CLI),
        help="runtime CLI to apply rules with (default: the reference setup's "
        "sudo -u wisp -H /home/wisp/.wisp/bin/wisp)",
    )
    parser.add_argument(
        "--apply", action="store_true", help="run the commands (default: print the plan only)"
    )
    args = parser.parse_args(argv)

    cli = shlex.split(args.wisp_cli)
    if not cli or not all(cli):
        print(f"{prog} permissions: empty_wisp_cli.", file=sys.stderr)
        return 2
    try:
        profile = audit.load_profile(resolve_profile(args.profile))
    except audit.AuditError as error:
        print(f"{prog} permissions: {error}. Nothing was applied.", file=sys.stderr)
        return 2

    if not args.apply:
        for planned in plan_commands(cli, profile):
            print(shlex.join(planned))
        print(f"{prog} permissions: plan only; rerun with --apply to execute.")
        return 0

    checks = apply_profile(audit.default_runner, cli, profile)
    for check in checks:
        print(f"{check.status.upper():<4}  {check.detail}")
    failed = any(check.status == audit.FAIL for check in checks)
    if failed:
        print(
            f"{prog} permissions: some rules failed; rerun `wi audit --wisp-cli "
            f"{shlex.join(cli)}` to see the live state.",
            file=sys.stderr,
        )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
