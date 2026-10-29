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
def isolated_global_prefs(tmp_path_factory, monkeypatch) -> Path:
    """Point the global preferences file at an isolated path.

    ``load_prefs`` falls back to ``default_global_path()``, so without this a
    developer's real ``~/.config/agent-crew-chief/preferences.toml`` would
    leak into every test. The file lives in its own temp dir (not the test's
    ``tmp_path``) so tests that assert on an empty ``tmp_path`` keep passing.
    """
    base = tmp_path_factory.mktemp("global-prefs")
    path = base / "preferences.toml"
    # Baseline tests must never spawn real update workers or touch the network:
    # keep the update check disabled in the default isolated global prefs.
    path.write_text("check_updates = false\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_CREW_CHIEF_GLOBAL_PREFS", str(path))
    return path


@pytest.fixture(autouse=True)
def home_dir(tmp_path_factory, monkeypatch) -> Path:
    """Redirect ``Path.home()`` (and HOME/USERPROFILE) to an empty temp dir.

    The containment snapshot compares the top level of ``Path.home()``, so on
    a developer machine another program (a background agent CLI, say) writing
    ``~/.grokbot`` mid-suite would turn an ``ok`` attempt into
    ``outside_write``. Autouse for every test, so no dispatch test can forget.
    The home lives outside ``tmp_path`` so tests that assert on the contents
    of ``tmp_path`` keep passing.
    """
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


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


@pytest.fixture(autouse=True)
def deny_external_connections(monkeypatch):
    """Deny non-loopback resolution in every test (isolated, no-network)."""
    import socket

    original = socket.getaddrinfo

    def resolve(host, *args, **kwargs):
        assert host in ("127.0.0.1", "localhost", "::1"), host
        return original(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)


@pytest.fixture(autouse=True)
def isolate_background_updates(request, monkeypatch):
    """Baseline tests can override prefs without starting real update children.

    Update proofs explicitly own their fake HTTP/process boundaries; all other
    tests use a scheduling double, including tests with custom global prefs.
    """
    if not request.node.path.name.startswith('test_update_'):
        from crew_chief import update_check
        monkeypatch.setattr(update_check, 'schedule_check', lambda *a, **kw: None)


@pytest.fixture(autouse=True)
def hide_host_rtk(monkeypatch):
    """A host rtk must not change kit tests; tests that need rtk add a fake one."""
    names = ("rtk", "rtk.exe", "rtk.cmd")
    entries = os.environ.get("PATH", "").split(os.pathsep)
    kept = [d for d in entries if not any((Path(d) / n).is_file() for n in names if d)]
    if len(kept) != len(entries):
        monkeypatch.setenv("PATH", os.pathsep.join(kept))
