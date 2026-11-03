"""The venv must match the pinned lock files (offline; reads package metadata only).

Upgrading Textual can silently change behavior this app depends on (default
background, markup, margins, Markdown streaming). Pins make upgrades explicit:
change requirements*.txt, then run the full suite including the PTY check.
"""
from __future__ import annotations

import re
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def pins(name: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in (ROOT / name).read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-r "):
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+-]+)", line)
        assert match, f"{name}: not an exact pin: {line!r}"
        result[match.group(1).lower()] = match.group(2)
    return result


@pytest.mark.parametrize("lock", ["requirements.txt", "requirements-dev.txt"])
def test_installed_versions_match_lock(lock: str) -> None:
    mismatches = []
    for package, pinned in pins(lock).items():
        try:
            installed = version(package)
        except PackageNotFoundError:
            installed = "missing"
        if installed != pinned:
            mismatches.append(f"{package}: installed {installed}, pinned {pinned}")
    assert not mismatches, "venv drifted from lock:\n" + "\n".join(mismatches)


def runtime_closure(root: str) -> set[str]:
    """Every distribution ``root`` needs at runtime here, following extras
    such as ``markdown-it-py[linkify]`` and evaluating environment markers."""
    from importlib.metadata import requires
    from packaging.requirements import Requirement

    visited: set[tuple[str, frozenset[str]]] = set()
    pending: list[tuple[str, frozenset[str]]] = [(root, frozenset())]
    while pending:
        name, extras = pending.pop()
        node = (name.lower().replace("_", "-"), extras)
        if node in visited:
            continue
        visited.add(node)
        for spec in requires(name) or []:
            requirement = Requirement(spec)
            if requirement.marker is None or any(
                    requirement.marker.evaluate({"extra": extra})
                    for extra in ["", *sorted(extras)]):
                pending.append((requirement.name, frozenset(requirement.extras)))
    return {name for name, _ in visited}


def test_runtime_lock_is_complete() -> None:
    """A fresh install from requirements.txt must not pull unpinned packages."""
    missing = runtime_closure("textual") - set(pins("requirements.txt"))
    assert not missing, f"unpinned runtime dependencies: {sorted(missing)}"


def test_pyproject_range_admits_locked_textual() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text()
    match = re.search(r'"textual~=(\d+)\.(\d+)\.(\d+)"', pyproject)
    assert match, "pyproject must pin textual with a compatible-release range"
    major, minor, _ = match.groups()
    locked = pins("requirements.txt")["textual"]
    assert locked.split(".")[:2] == [major, minor]
