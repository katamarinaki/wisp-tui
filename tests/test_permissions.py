"""Offline `scripts/permissions` checks: a scripted runner, no real CLI."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from wisp_tui import audit, permissions

CLI = ["wisp"]
PROFILE = audit.Profile(permissions={"fs_write": "allow", "bash_run": "ask"})


class FakeRunner:
    """Returns canned results per command tail; records every argv."""

    def __init__(self, outputs: dict[tuple[str, ...], int]) -> None:
        self.outputs = outputs
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        for tail, code in self.outputs.items():
            if tuple(argv[-len(tail) :]) == tail:
                return subprocess.CompletedProcess(argv, code, "", "")
        return subprocess.CompletedProcess(argv, 1, "", "")

    def unavailable(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        raise audit.AuditError("command_unavailable")


def test_plan_commands_are_sorted_explicit_argv() -> None:
    commands = permissions.plan_commands(CLI, PROFILE)
    assert commands == [
        ["wisp", "permissions", "set", "bash_run", "ask"],
        ["wisp", "permissions", "set", "fs_write", "allow"],
    ]


def test_apply_profile_reports_safe_codes() -> None:
    runner = FakeRunner({("bash_run", "ask"): 0, ("fs_write", "allow"): 0})
    checks = permissions.apply_profile(runner, CLI, PROFILE)
    assert [(c.status, c.detail) for c in checks] == [
        (audit.PASS, "bash_run: set"),
        (audit.PASS, "fs_write: set"),
    ]


def test_apply_profile_flags_failures_without_output() -> None:
    runner = FakeRunner({("bash_run", "ask"): 0})
    checks = permissions.apply_profile(runner, CLI, PROFILE)
    assert [(c.status, c.detail) for c in checks] == [
        (audit.PASS, "bash_run: set"),
        (audit.FAIL, "fs_write: command_failed"),
    ]


def test_apply_profile_flags_unavailable_commands() -> None:
    class UnavailableRunner:
        """Always raises AuditError, as default_runner does for a missing binary."""

        def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
            raise audit.AuditError("command_unavailable")

    checks = permissions.apply_profile(UnavailableRunner(), CLI, PROFILE)
    assert all(check.status == audit.FAIL for check in checks)
    assert "command_unavailable" in checks[0].detail


def test_apply_profile_with_empty_permissions_skips() -> None:
    checks = permissions.apply_profile(FakeRunner({}), CLI, audit.Profile())
    assert checks == [audit.Check("policy", audit.SKIP, "profile_has_no_permissions")]


# --- builtin profiles ---


@pytest.mark.parametrize("name", permissions.BUILTIN_PROFILES)
def test_builtin_profiles_load_and_validate(name: str) -> None:
    profile = audit.load_profile(permissions.builtin_profile_path(name))
    assert profile.permissions, f"profile {name} lists no permissions"


def test_builtin_profiles_cover_the_same_tools() -> None:
    pi_like = audit.load_profile(permissions.builtin_profile_path("pi-like"))
    safe = audit.load_profile(permissions.builtin_profile_path("safe"))
    assert set(pi_like.permissions) == set(safe.permissions)


def test_resolve_profile_passes_other_names_through(tmp_path: Path) -> None:
    assert permissions.resolve_profile("pi-like").name == "pi-like.json"
    custom = tmp_path / "custom.json"
    assert permissions.resolve_profile(str(custom)) == custom


# --- main ---


def test_main_defaults_to_a_plan_without_running_anything(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class RecordingRunner:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
            self.calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

    runner = RecordingRunner()
    monkeypatch.setattr(audit, "default_runner", runner)
    assert permissions.main(["--profile", "pi-like", "--wisp-cli", "wisp"]) == 0
    assert runner.calls == []
    output = capsys.readouterr().out
    assert "permissions set fs_write allow" in output
    assert "plan only" in output


def test_main_applies_and_exits_zero_on_success(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = FakeRunner(
        {("fs_write", "allow"): 0, ("fs_edit", "allow"): 0, ("bash_run", "allow"): 0}
    )
    monkeypatch.setattr(audit, "default_runner", runner)
    assert permissions.main(["--profile", "pi-like", "--wisp-cli", "wisp", "--apply"]) == 0
    output = capsys.readouterr().out
    assert "PASS  fs_write: set" in output
    assert len(runner.calls) == 3


def test_main_exits_one_when_a_rule_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = FakeRunner({("fs_write", "ask"): 0, ("fs_edit", "ask"): 0, ("bash_run", "ask"): 1})
    monkeypatch.setattr(audit, "default_runner", runner)
    assert permissions.main(["--profile", "safe", "--wisp-cli", "wisp", "--apply"]) == 1
    assert "FAIL  bash_run: command_failed" in capsys.readouterr().out


def test_main_rejects_unloadable_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(audit, "default_runner", FakeRunner({}))
    assert permissions.main(["--profile", str(tmp_path / "absent.json")]) == 2
    assert "profile_unavailable" in capsys.readouterr().err


def test_main_rejects_empty_cli(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(audit, "default_runner", FakeRunner({}))
    assert permissions.main(["--wisp-cli", "''"]) == 2
    assert "empty_wisp_cli" in capsys.readouterr().err
