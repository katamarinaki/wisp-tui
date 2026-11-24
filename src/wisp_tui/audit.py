"""Read-only setup audit: ``wi audit``.

Checks the machine against the reference system setup (README "Audit the
setup"): a dedicated service account with a nologin shell and an owner-only
home, a runtime that listens on loopback only, owner-only client files, live
services, and — when a runtime CLI command is supplied with ``--wisp-cli`` —
live policy (runtime version, sandbox, auto-mode, permissions), optionally
diffed against a profile file (``--profile``).

Read-only and safe by construction: no setting is changed, no inference runs,
and check details carry safe codes and facts (service names, ports, versions),
never the token or other config contents. Commands run as explicit argv lists,
never through a shell.
"""

from __future__ import annotations

import argparse
import json
import os
import pwd
import re
import shlex
import stat
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import config, program_name, setup
from .client import ClientError, WispClient, safe_code, validate_runtime_url

PASS = "pass"
FAIL = "fail"
WARN = "warn"
SKIP = "skip"

SERVICE_USER = "wisp"
COMMAND_TIMEOUT = 15.0

# From RUNTIME_CONTRACT.md; re-check that document when the runtime changes.
RUNTIME_COMPAT: dict[str, str] = {
    "0.1.8": "supported",
    "0.1.18": "unverified",  # installed, live acceptance run still pending
}

NOLOGIN_BASENAMES = frozenset({"nologin", "false"})
PERMISSION_RULES = frozenset({"allow", "ask", "deny"})
GROUPS = ("client", "runtime", "account", "services", "network", "policy")


class AuditError(Exception):
    """A safe diagnostic code, like SetupError; never carries secrets."""


CommandRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]
Probe = Callable[[], None]


def default_runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT,
            check=False,
        )
    except FileNotFoundError:
        raise AuditError("command_unavailable") from None
    except (OSError, subprocess.SubprocessError):
        raise AuditError("command_failed") from None


def default_probe() -> None:
    """Same checks as `wi --check`: health plus one authenticated read, no inference."""
    settings = config.load_config(config.CONFIG_PATH)
    setup.verify(WispClient(settings.url, settings.token))


@dataclass(frozen=True)
class Check:
    group: str
    status: str
    detail: str


@dataclass(frozen=True)
class Profile:
    """Expected live policy; every field is optional and safe to print."""

    sandbox: str = "enabled"
    auto_mode: bool = True
    model: str | None = None
    permissions: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Options:
    service_user: str = SERVICE_USER
    wisp_cli: list[str] | None = None
    profile: Profile = field(default_factory=Profile)
    skip: frozenset[str] = frozenset()


def load_profile(path: Path) -> Profile:
    try:
        raw = path.read_bytes()
        if len(raw) > config.MAX_CONFIG_BYTES:
            raise AuditError("invalid_profile")
        data = json.loads(raw)
    except OSError:
        raise AuditError("profile_unavailable") from None
    except ValueError:
        raise AuditError("invalid_profile") from None
    if not isinstance(data, dict):
        raise AuditError("invalid_profile")
    sandbox = data.get("sandbox", Profile.sandbox)
    if sandbox not in ("enabled", "disabled"):
        raise AuditError("invalid_profile")
    auto_mode = data.get("auto_mode", Profile.auto_mode)
    if not isinstance(auto_mode, bool):
        raise AuditError("invalid_profile")
    model = data.get("model")
    if model is not None and (
        not isinstance(model, str) or not re.fullmatch(r"[\w .-]{1,64}", model)
    ):
        raise AuditError("invalid_profile")
    permissions = data.get("permissions", {})
    if not isinstance(permissions, dict) or not all(
        isinstance(tool, str) and re.fullmatch(r"[\w.-]{1,64}", tool) and rule in PERMISSION_RULES
        for tool, rule in permissions.items()
    ):
        raise AuditError("invalid_profile")
    return Profile(
        sandbox=sandbox,
        auto_mode=auto_mode,
        model=model,
        permissions=dict(permissions),
    )


# --- client group -----------------------------------------------------------


def check_client_files(
    config_path: Path | None = None, state_dir: Path | None = None
) -> tuple[list[Check], str | None]:
    """Owner-only client files and a valid, loopback-only runtime URL.

    Returns the configured URL (never printed in full) for the network group,
    or None when the config is unusable; the client group already reported it.
    Paths default to the live config locations, resolved at call time so tests
    can redirect them.
    """
    config_path = config_path if config_path is not None else config.CONFIG_PATH
    state_dir = state_dir if state_dir is not None else config.STATE_DIR
    checks: list[Check] = []
    if not os.path.lexists(config_path):
        checks.append(Check("client", FAIL, "config_missing (run `wi setup`)"))
        return checks, None
    try:
        info = config_path.lstat()
    except OSError:
        checks.append(Check("client", FAIL, "config_unavailable"))
        return checks, None
    if stat.S_ISLNK(info.st_mode):
        checks.append(Check("client", FAIL, "config_is_symlink"))
        return checks, None
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
        checks.append(Check("client", FAIL, "config_not_owner_only"))
        return checks, None
    checks.append(Check("client", PASS, "config_owner_only"))
    url: str | None = None
    try:
        data = config.read_private_json(config_path, config.MAX_CONFIG_BYTES)
        url = validate_runtime_url(data.get("url"))
        checks.append(Check("client", PASS, "config_url_loopback"))
    except config.ConfigError as error:
        checks.append(Check("client", FAIL, safe_code(str(error), "invalid_config")))
    except ClientError as error:
        checks.append(Check("client", FAIL, safe_code(str(error), "invalid_config")))
    if os.path.lexists(state_dir):
        try:
            if config.is_private_dir(state_dir.lstat()):
                checks.append(Check("client", PASS, "state_dir_owner_only"))
            else:
                checks.append(Check("client", FAIL, "state_dir_not_owner_only"))
        except OSError:
            checks.append(Check("client", WARN, "state_dir_unavailable"))
    else:
        checks.append(Check("client", SKIP, "state_dir_absent"))
    return checks, url


def check_runtime(probe: Probe) -> list[Check]:
    try:
        probe()
    except (ClientError, setup.SetupError, config.ConfigError) as error:
        return [Check("runtime", FAIL, safe_code(str(error), "runtime_check_failed"))]
    return [Check("runtime", PASS, "runtime_ready")]


# --- account group ----------------------------------------------------------


def check_account(name: str) -> list[Check]:
    try:
        entry = pwd.getpwnam(name)
    except KeyError:
        return [Check("account", FAIL, "service_account_missing")]
    checks: list[Check] = []
    shell = Path(entry.pw_shell).name
    if shell in NOLOGIN_BASENAMES:
        checks.append(Check("account", PASS, "service_account_nologin"))
    else:
        checks.append(Check("account", WARN, "service_account_has_login_shell"))
    try:
        info = os.stat(entry.pw_dir)
    except OSError:
        checks.append(Check("account", SKIP, "service_home_unavailable"))
        return checks
    if not stat.S_ISDIR(info.st_mode):
        checks.append(Check("account", FAIL, "service_home_not_a_directory"))
    elif info.st_uid != entry.pw_uid:
        checks.append(Check("account", FAIL, "service_home_wrong_owner"))
    elif info.st_mode & 0o077:
        checks.append(Check("account", FAIL, "service_home_group_world_accessible"))
    else:
        checks.append(Check("account", PASS, "service_home_owner_only"))
    return checks


def check_linger(runner: CommandRunner, name: str) -> list[Check]:
    try:
        result = runner(["loginctl", "show-user", name, "-p", "Linger"])
    except AuditError as error:
        return [Check("account", SKIP, safe_code(str(error), "linger_not_checked"))]
    if result.returncode != 0:
        return [Check("account", SKIP, "linger_query_failed")]
    output = result.stdout.lower()
    if "linger=yes" in output:
        return [Check("account", PASS, "linger_enabled")]
    if "linger=no" in output:
        return [Check("account", WARN, "linger_disabled")]
    return [Check("account", WARN, "linger_status_unknown")]


# --- services group ---------------------------------------------------------


def service_subcommands(lines: list[str], user: str) -> set[str]:
    """Wisp CLI subcommands (serve, telegram) seen running as the service user."""
    found: set[str] = set()
    for line in lines:
        parts = line.split()
        if len(parts) < 3 or parts[0] != user:
            continue
        for index, token in enumerate(parts[1:-1], start=1):
            if Path(token).name == "wisp" and parts[index + 1] in ("serve", "telegram"):
                found.add(parts[index + 1])
    return found


def check_services(runner: CommandRunner, user: str) -> list[Check]:
    try:
        result = runner(["ps", "-eo", "user:32,args"])
    except AuditError as error:
        return [Check("services", SKIP, safe_code(str(error), "services_not_checked"))]
    if result.returncode != 0:
        return [Check("services", SKIP, "services_query_failed")]
    running = service_subcommands(result.stdout.splitlines(), user)
    checks = [
        Check("services", PASS, "serve_running")
        if "serve" in running
        else Check("services", FAIL, "serve_not_running")
    ]
    checks.append(
        Check("services", PASS, "telegram_running")
        if "telegram" in running
        else Check("services", WARN, "telegram_not_running (optional)")
    )
    return checks


# --- network group ----------------------------------------------------------


def listening_addresses(lines: list[str], port: int) -> list[str]:
    """Local addresses from `ss -tlnH` output bound to the given port."""
    addresses: list[str] = []
    for line in lines:
        parts = line.split()
        if len(parts) < 4 or not parts[3].rpartition(":")[2].isdigit():
            continue
        host, _, port_text = parts[3].rpartition(":")
        if port_text == str(port) and host:
            addresses.append(parts[3])
    return addresses


def is_loopback_address(address: str) -> bool:
    host = address.rpartition(":")[0]
    return host.startswith("127.") or host in ("[::1]", "::1", "localhost")


def check_loopback(runner: CommandRunner, url: str) -> list[Check]:
    port = int(url.rpartition(":")[2])
    try:
        result = runner(["ss", "-tlnH"])
    except AuditError as error:
        return [Check("network", SKIP, safe_code(str(error), "listener_not_checked"))]
    if result.returncode != 0:
        return [Check("network", SKIP, "listener_query_failed")]
    addresses = listening_addresses(result.stdout.splitlines(), port)
    if not addresses:
        return [Check("network", FAIL, "runtime_not_listening")]
    exposed = [address for address in addresses if not is_loopback_address(address)]
    if exposed:
        return [Check("network", FAIL, f"runtime_listens_beyond_loopback ({exposed[0]})")]
    return [Check("network", PASS, "runtime_loopback_only")]


# --- policy group -----------------------------------------------------------


def _policy_output(runner: CommandRunner, cli: list[str], args: list[str]) -> str | None:
    try:
        result = runner([*cli, *args])
    except AuditError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _version_check(runner: CommandRunner, cli: list[str]) -> Check:
    version_output = _policy_output(runner, cli, ["--version"])
    match = re.search(r"\d+\.\d+\.\d+", version_output or "")
    if match is None:
        return Check("policy", WARN, "runtime_version_unparsed")
    version = match.group(0)
    state = RUNTIME_COMPAT.get(version)
    if state == "supported":
        return Check("policy", PASS, f"runtime_{version}_supported")
    if state == "unverified":
        return Check("policy", WARN, f"runtime_{version}_unverified (see RUNTIME_CONTRACT.md)")
    return Check("policy", WARN, f"runtime_{version}_unknown (see RUNTIME_CONTRACT.md)")


def _sandbox_check(runner: CommandRunner, cli: list[str], profile: Profile) -> Check:
    sandbox_output = _policy_output(runner, cli, ["sandbox"])
    enabled: bool | None = None
    for line in (sandbox_output or "").lower().splitlines():
        if "enabled: true" in line or "enabled: yes" in line:
            enabled = True
        elif "enabled: false" in line or "enabled: no" in line:
            enabled = False
    if enabled is None:
        return Check("policy", WARN, "sandbox_status_unparsed")
    if (enabled and profile.sandbox == "enabled") or (
        not enabled and profile.sandbox == "disabled"
    ):
        return Check("policy", PASS, f"sandbox_{profile.sandbox}")
    return Check("policy", FAIL, f"sandbox_{profile.sandbox}_expected")


AUTO_MODE_LINE = re.compile(r"\s*auto[- ]mode\s*[:=]?\s*(on|off)\s*$")


def _auto_mode_check(runner: CommandRunner, cli: list[str], profile: Profile) -> Check:
    auto_output = _policy_output(runner, cli, ["auto-mode", "status"])
    auto: bool | None = None
    for line in (auto_output or "").lower().splitlines():
        # Only a dedicated status line counts; help text mentioning "on"/"off"
        # must never decide the check, and anything else is a warning.
        match = AUTO_MODE_LINE.match(line)
        if match is not None and auto is None:
            auto = match.group(1) == "on"
    if auto is None:
        return Check("policy", WARN, "auto_mode_status_unparsed")
    if auto == profile.auto_mode:
        return Check("policy", PASS, f"auto_mode_{'on' if auto else 'off'}")
    return Check("policy", FAIL, f"auto_mode_{'on' if profile.auto_mode else 'off'}_expected")


def parse_permission_line(line: str) -> tuple[str, str] | None:
    """A structured (tool, rule) pair, or None for any other shape.

    `wisp permissions get` prints `TOOL RULE` lines; only that exact two-token
    shape parses, so a line like `bash allow (default ask)` stays unparsed
    instead of counting as a match for `bash: ask`.
    """
    tokens = line.split()
    if (
        len(tokens) == 2
        and tokens[1] in PERMISSION_RULES
        and re.fullmatch(r"[\w.-]{1,64}", tokens[0])
    ):
        return tokens[0], tokens[1]
    return None


def _permissions_check(runner: CommandRunner, cli: list[str], profile: Profile) -> Check:
    permissions_output = _policy_output(runner, cli, ["permissions", "get"])
    if permissions_output is None:
        return Check("policy", WARN, "permissions_query_failed")
    pairs: set[tuple[str, str]] = set()
    unparsed = 0
    for line in permissions_output.lower().splitlines():
        if not line.strip():
            continue
        pair = parse_permission_line(line)
        if pair is None:
            unparsed += 1
        else:
            pairs.add(pair)
    if not pairs:
        # Never a silent pass: output we cannot structure is a warning.
        return Check("policy", WARN, "permissions_unparsed")
    if profile.permissions:
        mismatched = [
            tool for tool, rule in sorted(profile.permissions.items()) if (tool, rule) not in pairs
        ]
        if mismatched:
            return Check("policy", FAIL, f"permission_mismatch ({', '.join(mismatched)})")
        detail = "permissions_match_profile"
    else:
        detail = f"permissions_listed ({len(pairs)} rules)"
    if unparsed:
        # Unparsable lines are never a silent pass either.
        return Check("policy", WARN, f"{detail}, {unparsed} unparsed line(s)")
    return Check("policy", PASS, detail)


def check_policy(runner: CommandRunner, cli: list[str] | None, profile: Profile) -> list[Check]:
    if not cli:
        return [Check("policy", SKIP, "policy_not_checked (pass --wisp-cli)")]
    return [
        _version_check(runner, cli),
        _sandbox_check(runner, cli, profile),
        _auto_mode_check(runner, cli, profile),
        _permissions_check(runner, cli, profile),
    ]


# --- report -----------------------------------------------------------------


def run_audit(
    options: Options,
    runner: CommandRunner,
    probe: Probe,
    config_path: Path | None = None,
    state_dir: Path | None = None,
) -> list[Check]:
    config_path = config_path if config_path is not None else config.CONFIG_PATH
    state_dir = state_dir if state_dir is not None else config.STATE_DIR
    checks: list[Check] = []
    url: str | None = None
    if "client" not in options.skip:
        client_checks, url = check_client_files(config_path, state_dir)
        checks.extend(client_checks)
    else:
        try:
            url = validate_runtime_url(config.load_config(config_path).url)
        except (config.ConfigError, ClientError):
            url = None
    if "runtime" not in options.skip:
        checks.extend(check_runtime(probe))
    if "account" not in options.skip:
        checks.extend(check_account(options.service_user))
        checks.extend(check_linger(runner, options.service_user))
    if "services" not in options.skip:
        checks.extend(check_services(runner, options.service_user))
    if "network" not in options.skip:
        if url is None:
            checks.append(Check("network", SKIP, "runtime_url_unavailable"))
        else:
            checks.extend(check_loopback(runner, url))
    if "policy" not in options.skip:
        checks.extend(check_policy(runner, options.wisp_cli, options.profile))
    return checks


def print_report(checks: list[Check]) -> None:
    for check in checks:
        print(f"{check.status.upper():<4}  {check.group:<8}  {check.detail}")
    counts: dict[str, int] = {}
    for check in checks:
        counts[check.status] = counts.get(check.status, 0) + 1
    summary = ", ".join(f"{counts.get(status, 0)} {status}" for status in (PASS, FAIL, WARN, SKIP))
    print(f"audit: {summary}")


def main(argv: list[str] | None = None) -> int:
    prog = program_name()
    parser = argparse.ArgumentParser(
        prog=f"{prog} audit",
        description="Read-only drift check of the system setup: service account, "
        "loopback-only runtime, owner-only client files, live services, and "
        "with --wisp-cli the live runtime policy. Changes nothing and runs no "
        "inference.",
    )
    parser.add_argument(
        "--wisp-cli",
        metavar="COMMAND",
        help='runtime CLI to query policy with, e.g. "sudo -u wisp -H /opt/wisp/bin/wisp"',
    )
    parser.add_argument(
        "--profile",
        metavar="FILE",
        type=Path,
        help="JSON file with the expected policy: sandbox, auto_mode, model, permissions",
    )
    parser.add_argument(
        "--user",
        metavar="NAME",
        default=SERVICE_USER,
        help=f"service account to check (default: {SERVICE_USER})",
    )
    parser.add_argument(
        "--skip",
        metavar="GROUPS",
        default="",
        help=f"comma-separated groups to skip: {', '.join(GROUPS)}",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    unknown = frozenset(group for group in args.skip.split(",") if group) - frozenset(GROUPS)
    if unknown:
        print(f"{prog} audit: unknown group(s): {', '.join(sorted(unknown))}", file=sys.stderr)
        return 2
    wisp_cli = shlex.split(args.wisp_cli) if args.wisp_cli else None
    profile = Profile()
    if args.profile is not None:
        try:
            profile = load_profile(args.profile)
        except AuditError as error:
            print(f"{prog} audit: {error}. Nothing was checked.", file=sys.stderr)
            return 2

    options = Options(
        service_user=args.user,
        wisp_cli=wisp_cli,
        profile=profile,
        skip=frozenset(group for group in args.skip.split(",") if group),
    )
    try:
        checks = run_audit(options, default_runner, default_probe)
    except AuditError as error:
        print(f"{prog} audit: {error}.", file=sys.stderr)
        return 1
    if args.json:
        print(
            json.dumps([{"group": c.group, "status": c.status, "detail": c.detail} for c in checks])
        )
    else:
        print_report(checks)
    return 1 if any(check.status == FAIL for check in checks) else 0


if __name__ == "__main__":
    raise SystemExit(main())
