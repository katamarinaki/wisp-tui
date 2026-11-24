"""Offline `wi audit` checks: a scripted command runner and temporary paths.

The runner fakes ps/ss/loginctl/wisp CLI output; no process, socket or real
service account is touched. Addresses and paths are documentation ones.
"""

from __future__ import annotations

import json
import os
import pwd
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.helpers import FakeJsonResponse, FakeOpener
from wisp_tui import audit, config, setup
from wisp_tui.client import WispClient

TOKEN = "t" * 43
SECRET = "secretrundtoken"


class FakeRunner:
    """Returns canned stdout per command tail; records every argv."""

    def __init__(self, outputs: dict[tuple[str, ...], tuple[int, str]]) -> None:
        self.outputs = outputs
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        for tail, (code, output) in self.outputs.items():
            if tuple(argv[-len(tail) :]) == tail:
                return subprocess.CompletedProcess(argv, code, output, "")
        return subprocess.CompletedProcess(argv, 1, "", "")

    def unavailable(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        raise audit.AuditError("command_unavailable")


PS_SERVE = ("wisp", "/opt/wisp/bin/wisp", "serve")
PS_TELEGRAM = ("wisp", "/opt/wisp/bin/wisp", "telegram")


def ps_lines(*rows: tuple[str, str, str]) -> str:
    lines = []
    for user, binary, sub in rows:
        lines.append(f"{user:<32} {binary} {sub}")
    return "\n".join(lines) + "\n"


def fake_passwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, shell: str = "/usr/sbin/nologin"
) -> Path:
    home = tmp_path / "home-wisp"
    home.mkdir(mode=0o700)
    record = pwd.struct_passwd(("wisp", "x", os.getuid(), os.getgid(), "", str(home), shell))
    monkeypatch.setattr(pwd, "getpwnam", lambda _name: record)
    return home


def write_config(directory: Path, url: str = "http://127.0.0.1:3008", mode: int = 0o600) -> Path:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / "config.json"
    path.write_text(json.dumps({"url": url, "token": SECRET}))
    path.chmod(mode)
    return path


@pytest.fixture
def paths(tmp_path: Path) -> tuple[Path, Path]:
    config_dir = tmp_path / "config/wisp-tui"
    state_dir = tmp_path / "state/wisp-tui"
    return config_dir / "config.json", state_dir


def run(
    options: audit.Options | None = None,
    runner: audit.CommandRunner | None = None,
    probe: audit.Probe | None = None,
    config_path: Path | None = None,
    state_dir: Path | None = None,
) -> list[audit.Check]:
    return audit.run_audit(
        options or audit.Options(),
        runner or FakeRunner({}),
        probe or (lambda: None),
        config_path or Path("/nonexistent-config"),
        state_dir or Path("/nonexistent-state"),
    )


def detail(checks: list[audit.Check], group: str) -> list[str]:
    return [check.detail for check in checks if check.group == group]


# --- client group ---


def test_client_files_pass_and_url_returned(paths: tuple[Path, Path]) -> None:
    config_path, state_dir = paths
    write_config(config_path.parent)
    state_dir.mkdir(parents=True, mode=0o700)
    checks, url = audit.check_client_files(config_path, state_dir)
    assert url == "http://127.0.0.1:3008"
    assert all(check.status == audit.PASS for check in checks)


def test_client_files_missing_config() -> None:
    checks, url = audit.check_client_files(Path("/nonexistent"), Path("/nonexistent"))
    assert url is None
    assert checks == [audit.Check("client", audit.FAIL, "config_missing (run `wi setup`)")]


def test_client_files_rejects_group_readable_config(paths: tuple[Path, Path]) -> None:
    config_path, _ = paths
    write_config(config_path.parent, mode=0o640)
    checks, _url = audit.check_client_files(config_path, Path("/nonexistent"))
    assert any(check.status == audit.FAIL for check in checks)


def test_client_files_rejects_symlink_config(paths: tuple[Path, Path]) -> None:
    config_path, _ = paths
    real = write_config(config_path.parent)
    link = config_path.parent / "link.json"
    link.symlink_to(real)
    checks, _url = audit.check_client_files(link, Path("/nonexistent"))
    assert [c.detail for c in checks if c.status == audit.FAIL] == ["config_is_symlink"]


def test_client_files_rejects_non_loopback_url(paths: tuple[Path, Path]) -> None:
    config_path, _ = paths
    write_config(config_path.parent, url="http://192.0.2.10:3008")
    checks, url = audit.check_client_files(config_path, Path("/nonexistent"))
    assert url is None
    assert "runtime_must_be_explicit_loopback_http" in detail(checks, "client")


def test_client_files_flags_open_state_dir(paths: tuple[Path, Path]) -> None:
    config_path, state_dir = paths
    write_config(config_path.parent)
    state_dir.mkdir(parents=True, mode=0o755)
    checks, _url = audit.check_client_files(config_path, state_dir)
    assert "state_dir_not_owner_only" in detail(checks, "client")


def test_audit_never_prints_the_token(
    paths: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    config_path, state_dir = paths
    write_config(config_path.parent)
    checks = run(config_path=config_path, state_dir=state_dir)
    audit.print_report(checks)
    output = capsys.readouterr().out
    assert SECRET not in output


def test_json_report_shape(paths: tuple[Path, Path], capsys: pytest.CaptureFixture[str]) -> None:
    config_path, _ = paths
    write_config(config_path.parent)
    checks = run(config_path=config_path)
    report = json.dumps([check.__dict__ for check in checks])
    assert json.loads(report)[0]["group"] == "client"


# --- runtime group ---


def test_runtime_health_passes_on_ready_probe() -> None:
    assert audit.check_runtime(lambda: None) == [
        audit.Check("runtime", audit.PASS, "runtime_ready")
    ]


def test_runtime_health_reports_safe_error_code() -> None:
    def failing() -> None:
        raise setup.SetupError("runtime_not_ready")

    assert audit.check_runtime(failing) == [audit.Check("runtime", audit.FAIL, "runtime_not_ready")]


def test_default_probe_uses_the_saved_config(
    monkeypatch: pytest.MonkeyPatch, paths: tuple[Path, Path]
) -> None:
    config_path, _ = paths
    write_config(config_path.parent)
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)
    opener = FakeOpener()
    opener.responses.append(FakeJsonResponse({"status": "ready"}))
    opener.responses.append(FakeJsonResponse({"runs": []}))

    def client(url: str, token: str) -> WispClient:
        assert token == SECRET
        return WispClient(url, token, opener=opener)  # type: ignore[arg-type]

    monkeypatch.setattr(audit, "WispClient", client)
    audit.default_probe()  # does not raise


# --- account group ---


def test_account_passes_for_nologin_service_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = fake_passwd(tmp_path, monkeypatch)
    checks = audit.check_account("wisp")
    assert [check.status for check in checks] == [audit.PASS, audit.PASS]
    assert home.name == "home-wisp"


def test_account_warns_for_login_shell(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_passwd(tmp_path, monkeypatch, shell="/bin/bash")
    checks = audit.check_account("wisp")
    assert "service_account_has_login_shell" in detail(checks, "account")


def test_account_fails_for_open_service_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = fake_passwd(tmp_path, monkeypatch)
    home.chmod(0o755)
    checks = audit.check_account("wisp")
    assert "service_home_group_world_accessible" in detail(checks, "account")


def test_account_fails_when_user_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pwd, "getpwnam", lambda _name: (_ for _ in ()).throw(KeyError("wisp")))
    checks = audit.check_account("wisp")
    assert checks == [audit.Check("account", audit.FAIL, "service_account_missing")]


PS_KEY = ("user:32,args",)
LINGER_KEY = ("Linger",)


def test_linger_states() -> None:
    yes = FakeRunner({LINGER_KEY: (0, "Linger=yes\n")})
    no = FakeRunner({LINGER_KEY: (0, "Linger=no\n")})
    failed = FakeRunner({LINGER_KEY: (1, "")})
    assert audit.check_linger(yes, "wisp")[0].status == audit.PASS
    assert audit.check_linger(no, "wisp")[0].status == audit.WARN
    assert audit.check_linger(failed, "wisp")[0].status == audit.SKIP


# --- services group ---


def test_services_running() -> None:
    runner = FakeRunner({PS_KEY: (0, ps_lines(PS_SERVE, PS_TELEGRAM))})
    checks = audit.check_services(runner, "wisp")
    assert [check.status for check in checks] == [audit.PASS, audit.PASS]


def test_services_fail_when_serve_missing() -> None:
    runner = FakeRunner({PS_KEY: (0, ps_lines(PS_TELEGRAM))})
    checks = audit.check_services(runner, "wisp")
    assert [check.status for check in checks] == [audit.FAIL, audit.PASS]


def test_services_warn_when_telegram_missing() -> None:
    runner = FakeRunner({PS_KEY: (0, ps_lines(PS_SERVE))})
    checks = audit.check_services(runner, "wisp")
    assert "telegram_not_running (optional)" in detail(checks, "services")


def test_service_subcommands_ignores_other_users() -> None:
    lines = ps_lines(("root", "/opt/wisp/bin/wisp", "serve")).splitlines()
    assert audit.service_subcommands(lines, "wisp") == set()


# --- network group ---


def test_loopback_passes_for_loopback_listeners() -> None:
    runner = FakeRunner(
        {
            ("-tlnH",): (
                0,
                "LISTEN 0 4096 127.0.0.1:3008 0.0.0.0:*\nLISTEN 0 4096 [::1]:3008 [::]:*\n",
            )
        }
    )
    checks = audit.check_loopback(runner, "http://127.0.0.1:3008")
    assert checks == [audit.Check("network", audit.PASS, "runtime_loopback_only")]


def test_loopback_fails_for_exposed_listener() -> None:
    runner = FakeRunner({("-tlnH",): (0, "LISTEN 0 4096 0.0.0.0:3008 0.0.0.0:*\n")})
    checks = audit.check_loopback(runner, "http://127.0.0.1:3008")
    assert checks[0].status == audit.FAIL
    assert checks[0].detail.startswith("runtime_listens_beyond_loopback")


def test_loopback_fails_when_not_listening() -> None:
    runner = FakeRunner({("-tlnH",): (0, "LISTEN 0 4096 0.0.0.0:9199 0.0.0.0:*\n")})
    checks = audit.check_loopback(runner, "http://127.0.0.1:3008")
    assert checks == [audit.Check("network", audit.FAIL, "runtime_not_listening")]


def test_listening_addresses_matches_exact_port_only() -> None:
    lines = ["LISTEN 0 4096 127.0.0.1:80 0.0.0.0:*"]
    assert audit.listening_addresses(lines, 80) == ["127.0.0.1:80"]
    assert audit.listening_addresses(lines, 8) == []


# --- policy group ---


def policy_runner() -> FakeRunner:
    return FakeRunner(
        {
            ("--version",): (0, "wisp 0.1.18\n"),
            ("sandbox",): (0, "sandbox\n  enabled: true\n"),
            ("auto-mode", "status"): (0, "auto-mode: on\n"),
            ("permissions", "get"): (0, "fs_write allow\nbash_run allow\nfs_edit ask\n"),
        }
    )


def test_policy_skipped_without_cli() -> None:
    checks = audit.check_policy(FakeRunner({}), None, audit.Profile())
    assert checks == [audit.Check("policy", audit.SKIP, "policy_not_checked (pass --wisp-cli)")]


def test_policy_supported_version_passes() -> None:
    runner = FakeRunner({("--version",): (0, "wisp 0.1.8\n")})
    checks = audit.check_policy(runner, ["wisp"], audit.Profile())
    assert "runtime_0.1.8_supported" in detail(checks, "policy")


def test_policy_unverified_version_warns() -> None:
    checks = audit.check_policy(policy_runner(), ["wisp"], audit.Profile())
    assert "runtime_0.1.18_unverified (see RUNTIME_CONTRACT.md)" in detail(checks, "policy")


def test_policy_unknown_version_warns() -> None:
    runner = FakeRunner({("--version",): (0, "wisp 9.9.9\n")})
    checks = audit.check_policy(runner, ["wisp"], audit.Profile())
    assert any(c.detail.startswith("runtime_9.9.9_unknown") for c in checks)


def test_policy_sandbox_and_auto_mode_match_profile() -> None:
    checks = audit.check_policy(policy_runner(), ["wisp"], audit.Profile())
    statuses = {c.detail: c.status for c in checks}
    assert statuses["sandbox_enabled"] == audit.PASS
    assert statuses["auto_mode_on"] == audit.PASS


def test_policy_sandbox_mismatch_fails() -> None:
    checks = audit.check_policy(policy_runner(), ["wisp"], audit.Profile(sandbox="disabled"))
    statuses = {c.detail: c.status for c in checks}
    assert statuses["sandbox_disabled_expected"] == audit.FAIL


def test_policy_auto_mode_mismatch_fails() -> None:
    runner = FakeRunner({("auto-mode", "status"): (0, "auto-mode: off\n")})
    checks = audit.check_policy(runner, ["wisp"], audit.Profile(auto_mode=False))
    statuses = {c.detail: c.status for c in checks}
    assert statuses["auto_mode_off"] == audit.PASS


def test_policy_permissions_diff_against_profile() -> None:
    profile = audit.Profile(
        permissions={"fs_write": "allow", "bash_run": "allow", "fs_edit": "deny"}
    )
    checks = audit.check_policy(policy_runner(), ["wisp"], profile)
    mismatch = [c for c in checks if c.detail.startswith("permission_mismatch")]
    assert mismatch and mismatch[0].status == audit.FAIL
    assert "fs_edit" in mismatch[0].detail


def test_policy_permissions_listed_without_profile() -> None:
    checks = audit.check_policy(policy_runner(), ["wisp"], audit.Profile())
    assert "permissions_listed (3 rules)" in detail(checks, "policy")


def test_policy_flags_failed_command() -> None:
    runner = FakeRunner({("permissions", "get"): (1, "")})
    checks = audit.check_policy(runner, ["wisp"], audit.Profile())
    assert "permissions_query_failed" in detail(checks, "policy")


# --- profile loading ---


def test_load_profile_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "profile.json"
    path.write_text(
        json.dumps(
            {
                "sandbox": "disabled",
                "auto_mode": False,
                "model": "balanced",
                "permissions": {"fs_write": "allow"},
            }
        )
    )
    profile = audit.load_profile(path)
    assert profile == audit.Profile(
        sandbox="disabled", auto_mode=False, model="balanced", permissions={"fs_write": "allow"}
    )


@pytest.mark.parametrize(
    "content",
    [
        '{"sandbox": "sometimes"}',
        '{"auto_mode": "yes"}',
        '{"permissions": {"fs_write": "maybe"}}',
        "[]",
        "not json",
    ],
)
def test_load_profile_rejects_invalid_files(tmp_path: Path, content: str) -> None:
    path = tmp_path / "profile.json"
    path.write_text(content)
    with pytest.raises(audit.AuditError, match="invalid_profile"):
        audit.load_profile(path)


def test_load_profile_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(audit.AuditError, match="profile_unavailable"):
        audit.load_profile(tmp_path / "absent.json")


# --- report and main ---


def test_report_prints_every_check_and_a_summary(
    capsys: pytest.CaptureFixture[str],
) -> None:
    checks = [
        audit.Check("client", audit.PASS, "config_owner_only"),
        audit.Check("policy", audit.FAIL, "sandbox_disabled"),
        audit.Check("policy", audit.WARN, "runtime_0.1.18_unverified"),
        audit.Check("network", audit.SKIP, "listener_not_checked"),
    ]
    audit.print_report(checks)
    output = capsys.readouterr().out
    assert "FAIL  policy    sandbox_disabled" in output
    assert "audit: 1 pass, 1 fail, 1 warn, 1 skip" in output


def test_exit_code_reflects_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, paths: tuple[Path, Path]
) -> None:
    config_path, _ = paths
    write_config(config_path.parent)
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)
    fake_passwd(tmp_path, monkeypatch)
    monkeypatch.setattr(audit, "default_runner", FakeRunner({PS_KEY: (0, ps_lines(PS_TELEGRAM))}))
    monkeypatch.setattr(audit, "default_probe", lambda: None)
    assert audit.main([]) == 1  # serve not running: FAIL
    assert audit.main(["--skip", "account,services,policy"]) == 0


def test_main_rejects_unknown_group(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(audit, "default_runner", FakeRunner({}))
    monkeypatch.setattr(audit, "default_probe", lambda: None)
    assert audit.main(["--skip", "nope"]) == 2


def test_main_rejects_bad_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "profile.json"
    path.write_text("not json")
    monkeypatch.setattr(audit, "default_runner", FakeRunner({}))
    monkeypatch.setattr(audit, "default_probe", lambda: None)
    assert audit.main(["--profile", str(path)]) == 2
    assert "invalid_profile" in capsys.readouterr().err


def test_main_json_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    paths: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path, _ = paths
    write_config(config_path.parent)
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)
    fake_passwd(tmp_path, monkeypatch)
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"sandbox": "enabled"}))
    runner = policy_runner()
    runner.outputs[PS_KEY] = (0, ps_lines(PS_SERVE, PS_TELEGRAM))
    monkeypatch.setattr(audit, "default_runner", runner)
    monkeypatch.setattr(audit, "default_probe", lambda: None)
    assert audit.main(["--json", "--wisp-cli", "wisp", "--profile", str(profile)]) == 0
    payload: list[dict[str, Any]] = json.loads(capsys.readouterr().out)
    assert payload
    assert all(set(item) == {"group", "status", "detail"} for item in payload)
    assert SECRET not in json.dumps(payload)
