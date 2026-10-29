"""Tests for FR-048 / SC-024: piece start --native creates isolated worktree."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from crew_chief.cli import main
from crew_chief.records import load_job, new_job

NOW = datetime(2026, 10, 25, 12, 0)


def _git(args: list[str], cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def _init_repo(root: Path) -> Path:
    """Create a minimal git repository with one commit and return its path."""
    project = root / "project"
    project.mkdir(parents=True)
    _git(["init", "-b", "main"], project)
    _git(["config", "user.email", "test@example.com"], project)
    _git(["config", "user.name", "Test User"], project)
    (project / "README.md").write_text("# Test\n", encoding="utf-8")
    _git(["add", "."], project)
    _git(["commit", "-m", "initial"], project)
    return project


def _new_job(project: Path, **overrides) -> dict:
    kwargs = dict(
        title="Native piece start",
        request="Test native isolation.",
        distributed=True,
        reason="test",
        now=NOW,
        executors="subagents",
    )
    kwargs.update(overrides)
    return new_job(project, **kwargs)


def _add_piece(project: Path, job_id: str) -> int:
    """Add a code piece to the job via the CLI and return its number."""
    exit_code = main([
        "piece", "add",
        "--project", str(project),
        "--job", job_id,
        "--title", "Implement feature",
        "--type", "code",
        "--risk", "low",
    ])
    assert exit_code == 0
    job = load_job(project, job_id)
    return job["pieces"][-1]["n"]


def test_piece_start_native_creates_worktree_and_records_attempt(tmp_path):
    """piece start --native creates worktree, branch and records running attempt."""
    project = _init_repo(tmp_path)
    job = _new_job(project)
    piece_n = _add_piece(project, job["id"])

    exit_code = main([
        "piece", "start",
        "--project", str(project),
        "--job", job["id"],
        "--piece", str(piece_n),
        "--native",
        "--coordinator", "claude",
        "--model", "sonnet",
    ])
    assert exit_code == 0

    # Verify attempt is recorded in the job record.
    loaded = load_job(project, job["id"])
    piece = next(p for p in loaded["pieces"] if p["n"] == piece_n)
    assert len(piece["attempts"]) == 1
    attempt = piece["attempts"][0]
    assert attempt["executor"] == "native:claude/sonnet"
    assert attempt["status"] == "running"
    assert attempt["role"] == "exec"
    assert attempt["branch"].startswith("crew/")
    assert attempt["worktree"]

    # Verify worktree exists on disk.
    worktree_path = Path(attempt["worktree"])
    assert worktree_path.is_dir()

    # Verify branch exists in the repository.
    branches = _git(["branch", "--list"], project)
    assert attempt["branch"] in branches


def test_piece_start_native_prints_worktree_and_branch(tmp_path, capsys):
    """piece start --native prints worktree path and branch to stdout."""
    project = _init_repo(tmp_path)
    job = _new_job(project)
    piece_n = _add_piece(project, job["id"])

    exit_code = main([
        "piece", "start",
        "--project", str(project),
        "--job", job["id"],
        "--piece", str(piece_n),
        "--native",
        "--coordinator", "codex",
        "--model", "o3",
    ])
    assert exit_code == 0

    captured = capsys.readouterr()
    assert "worktree:" in captured.out
    assert "branch:" in captured.out
    assert "crew/" in captured.out


def test_piece_start_native_uses_coordinator_env(tmp_path, monkeypatch):
    """--coordinator falls back to CREW_CHIEF_COORDINATOR env var."""
    project = _init_repo(tmp_path)
    job = _new_job(project)
    piece_n = _add_piece(project, job["id"])

    monkeypatch.setenv("CREW_CHIEF_COORDINATOR", "gemini")
    exit_code = main([
        "piece", "start",
        "--project", str(project),
        "--job", job["id"],
        "--piece", str(piece_n),
        "--native",
        "--model", "flash",
    ])
    assert exit_code == 0

    loaded = load_job(project, job["id"])
    piece = next(p for p in loaded["pieces"] if p["n"] == piece_n)
    attempt = piece["attempts"][0]
    assert attempt["executor"] == "native:gemini/flash"


def test_piece_start_native_requires_model(tmp_path):
    """piece start --native fails without --model."""
    project = _init_repo(tmp_path)
    job = _new_job(project)
    piece_n = _add_piece(project, job["id"])

    exit_code = main([
        "piece", "start",
        "--project", str(project),
        "--job", job["id"],
        "--piece", str(piece_n),
        "--native",
        "--coordinator", "claude",
    ])
    assert exit_code == 2


def test_piece_start_native_requires_native_flag(tmp_path):
    """piece start without --native is rejected."""
    project = _init_repo(tmp_path)
    job = _new_job(project)
    piece_n = _add_piece(project, job["id"])

    exit_code = main([
        "piece", "start",
        "--project", str(project),
        "--job", job["id"],
        "--piece", str(piece_n),
        "--coordinator", "claude",
        "--model", "sonnet",
    ])
    assert exit_code == 2


def test_piece_start_native_json_output(tmp_path, capsys):
    """--json returns structured payload with worktree, branch and executor."""
    project = _init_repo(tmp_path)
    job = _new_job(project)
    piece_n = _add_piece(project, job["id"])
    capsys.readouterr()  # Flush stdout from piece add

    exit_code = main([
        "piece", "start",
        "--project", str(project),
        "--job", job["id"],
        "--piece", str(piece_n),
        "--native",
        "--coordinator", "claude",
        "--model", "opus",
        "--json",
    ])
    assert exit_code == 0

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["piece"] == piece_n
    assert payload["attempt"] == 1
    assert payload["executor"] == "native:claude/opus"
    assert payload["branch"].startswith("crew/")
    assert Path(payload["worktree"]).is_dir()

    loaded = load_job(project, job["id"])
    piece = next(p for p in loaded["pieces"] if p["n"] == piece_n)
    attempt = piece["attempts"][0]
    assert attempt["executor"] == "native:claude/opus"
    assert attempt["status"] == "running"
    assert Path(attempt["worktree"]).is_dir()


def test_piece_start_native_requires_coordinator(tmp_path, monkeypatch):
    """piece start --native fails without coordinator."""
    project = _init_repo(tmp_path)
    job = _new_job(project)
    piece_n = _add_piece(project, job["id"])

    monkeypatch.delenv("CREW_CHIEF_COORDINATOR", raising=False)
    exit_code = main([
        "piece", "start",
        "--project", str(project),
        "--job", job["id"],
        "--piece", str(piece_n),
        "--native",
        "--model", "sonnet",
    ])
    assert exit_code == 2


def test_piece_start_native_consecutive_attempts(tmp_path):
    """Multiple native piece starts allocate incrementing attempt numbers."""
    project = _init_repo(tmp_path)
    job = _new_job(project)
    piece_n = _add_piece(project, job["id"])

    # First attempt
    code1 = main([
        "piece", "start",
        "--project", str(project),
        "--job", job["id"],
        "--piece", str(piece_n),
        "--native",
        "--coordinator", "claude",
        "--model", "sonnet",
    ])
    assert code1 == 0

    # Second attempt
    code2 = main([
        "piece", "start",
        "--project", str(project),
        "--job", job["id"],
        "--piece", str(piece_n),
        "--native",
        "--coordinator", "claude",
        "--model", "sonnet",
    ])
    assert code2 == 0

    loaded = load_job(project, job["id"])
    piece = next(p for p in loaded["pieces"] if p["n"] == piece_n)
    assert len(piece["attempts"]) == 2
    a1, a2 = piece["attempts"][0], piece["attempts"][1]
    assert a1["n"] == 1
    assert a2["n"] == 2
    assert a1["branch"] != a2["branch"]
    assert a1["worktree"] != a2["worktree"]
    assert Path(a1["worktree"]).is_dir()
    assert Path(a2["worktree"]).is_dir()
    branches = _git(["branch", "--list"], project)
    assert a1["branch"] in branches
    assert a2["branch"] in branches


def test_piece_start_native_invalid_piece(tmp_path):
    """piece start --native fails when piece does not exist in the job."""
    project = _init_repo(tmp_path)
    job = _new_job(project)

    exit_code = main([
        "piece", "start",
        "--project", str(project),
        "--job", job["id"],
        "--piece", "999",
        "--native",
        "--coordinator", "claude",
        "--model", "sonnet",
    ])
    assert exit_code == 2


def test_piece_start_native_hides_sensitive_paths(tmp_path):
    """piece start --native respects sensitive paths from project preferences."""
    project = _init_repo(tmp_path)
    # Write sensitive preferences and file
    prefs_dir = project / "docs" / "crew-chief"
    prefs_dir.mkdir(parents=True, exist_ok=True)
    (prefs_dir / "preferences.toml").write_text(
        'sensitive_paths = ["secrets/"]\n', encoding="utf-8"
    )
    secret_dir = project / "secrets"
    secret_dir.mkdir(parents=True, exist_ok=True)
    secret_file = secret_dir / "token.txt"
    secret_file.write_text("super-secret\n", encoding="utf-8")
    _git(["add", "."], project)
    _git(["commit", "-m", "add secret"], project)

    job = _new_job(project)
    piece_n = _add_piece(project, job["id"])

    exit_code = main([
        "piece", "start",
        "--project", str(project),
        "--job", job["id"],
        "--piece", str(piece_n),
        "--native",
        "--coordinator", "claude",
        "--model", "sonnet",
    ])
    assert exit_code == 0

    loaded = load_job(project, job["id"])
    piece = next(p for p in loaded["pieces"] if p["n"] == piece_n)
    wt_path = Path(piece["attempts"][0]["worktree"])
    assert wt_path.is_dir()
    # Sensitive file must be hidden in the isolated worktree
    assert not (wt_path / "secrets" / "token.txt").exists()
    # Main project still has the secret
    assert (project / "secrets" / "token.txt").exists()
