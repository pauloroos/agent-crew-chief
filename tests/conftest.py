"""Shared pytest fixtures and import path setup for agent-crew-chief tests."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "skill" / "crew-chief" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


@pytest.fixture(autouse=True)
def isolated_global_prefs(tmp_path: Path, monkeypatch) -> Path:
    """Point the global preferences file at a non-existent path under tmp_path.

    ``load_prefs`` falls back to ``default_global_path()``, so without this a
    developer's real ``~/.config/agent-crew-chief/preferences.toml`` would
    leak into every test.
    """
    path = tmp_path / "no-global-preferences.toml"
    monkeypatch.setenv("AGENT_CREW_CHIEF_GLOBAL_PREFS", str(path))
    return path


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A temporary git repository with one commit, returned as a Path."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / "README.md").write_text("# repo\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial commit")
    return repo


@pytest.fixture
def fake_bin(tmp_path: Path, monkeypatch):
    """Factory fixture: write an executable Python stub named `name`.

    On Windows an extra `name.cmd` shim runs the stub with `sys.executable`.
    The stub directory is prepended to PATH through `monkeypatch`; the
    directory is returned.
    """

    def _make(name: str, script_text: str) -> Path:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir(exist_ok=True)
        if not script_text.startswith("#!"):
            script_text = f"#!{sys.executable}\n" + script_text
        stub = bin_dir / name
        stub.write_text(script_text, encoding="utf-8")
        if os.name == "nt":
            shim = bin_dir / f"{name}.cmd"
            shim.write_text(
                f'@echo off\r\n"{sys.executable}" "{stub}" %*\r\n',
                encoding="utf-8",
            )
        else:
            stub.chmod(0o755)
        monkeypatch.setenv(
            "PATH", os.pathsep.join([str(bin_dir), os.environ.get("PATH", "")])
        )
        return bin_dir

    return _make
