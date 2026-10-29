"""Executor mode badge in snapshot, terminal and web views (FR-045)."""

from __future__ import annotations

import uuid
from pathlib import Path

from crew_chief import central_registry, records
from crew_chief.central_snapshot import snapshot
from crew_chief.central_terminal import render


def _root(tmp_path: Path) -> Path:
    return tmp_path / "central"


def _record(tmp_path: Path, **overrides) -> dict:
    coordinator_id = overrides.get("id", str(uuid.uuid4()))
    base = {
        "schema_version": 1,
        "id": coordinator_id,
        "repo": str(tmp_path / "repo"),
        "worktree": str(tmp_path / "repo.crew" / "worktree-a"),
        "branch": "acc/coordinator-a",
        "base_commit": "0123456789abcdef0123456789abcdef01234567",
        "backend": "default",
        "cli": "claude",
        "cli_version": "1.0.0",
        "acc_version": "0.4.0",
        "session_id": None,
        "mode": "approved-spec",
        "authorization": {"raw": False, "request_digest": "digest", "at": "2026-01-01T00:00:00+00:00"},
        "job_ids": [],
        "active_job_id": None,
        "state": "created",
        "last_error": None,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    base.update(overrides)
    return base


def _make_job(worktree: Path, executors: str | None = None) -> dict:
    job = records.new_job(worktree, "title", "request", False, "reason")
    if executors is not None:
        job["executors"] = executors
    job["pieces"] = [{"n": 1, "title": "p", "type": "code", "ref": "T1", "attempts": []}]
    records.save_job(worktree, job)
    return job


def test_snapshot_projects_executors_on_coordinator_and_job(tmp_path):
    """Snapshot exposes executors under both coordinator and job summaries."""
    root = _root(tmp_path)
    worktree = tmp_path / "repo.crew" / "worktree-a"
    (worktree / "docs" / "crew-chief" / "jobs").mkdir(parents=True, exist_ok=True)
    record = _record(tmp_path, worktree=str(worktree))
    cid = central_registry.register(root, record)

    job_own = _make_job(worktree, "own")
    job_sub = _make_job(worktree, "subagents")
    central_registry.update_record(root, cid, {
        "active_job_id": job_own["id"],
        "job_ids": [job_own["id"], job_sub["id"]],
    })

    result = snapshot(root, "2026-01-01T00:00:10+00:00")
    row = result["coordinators"][0]

    # Coordinator-level executor mode follows the active job.
    assert row["executors"] == "own"

    # Job summaries each carry their own executors field.
    jobs_by_id = {j["id"]: j for j in row["jobs"]}
    assert jobs_by_id[job_own["id"]]["executors"] == "own"
    assert jobs_by_id[job_sub["id"]]["executors"] == "subagents"


def test_snapshot_legacy_job_defaults_to_free_first(tmp_path):
    """Legacy jobs without executors field default to free_first in snapshot."""
    root = _root(tmp_path)
    worktree = tmp_path / "repo.crew" / "worktree-a"
    (worktree / "docs" / "crew-chief" / "jobs").mkdir(parents=True, exist_ok=True)
    record = _record(tmp_path, worktree=str(worktree))
    cid = central_registry.register(root, record)

    job = _make_job(worktree, None)
    # Ensure no executors key exists on disk.
    job.pop("executors", None)
    records.save_job(worktree, job)
    central_registry.update_record(root, cid, {
        "active_job_id": job["id"],
        "job_ids": [job["id"]],
    })

    result = snapshot(root, "2026-01-01T00:00:10+00:00")
    row = result["coordinators"][0]
    assert row["executors"] == "free_first"
    assert row["jobs"][0]["executors"] == "free_first"


def test_terminal_displays_executor_mode(tmp_path):
    """Terminal output includes executors=own/subagents/free_first."""
    root = _root(tmp_path)
    worktree = tmp_path / "repo.crew" / "worktree-a"
    (worktree / "docs" / "crew-chief" / "jobs").mkdir(parents=True, exist_ok=True)
    record = _record(tmp_path, worktree=str(worktree))
    cid = central_registry.register(root, record)

    job = _make_job(worktree, "subagents")
    central_registry.update_record(root, cid, {
        "active_job_id": job["id"],
        "job_ids": [job["id"]],
    })

    snap = snapshot(root, "2026-01-01T00:00:10+00:00")
    text_out = render(snap, width=200)
    assert "executors=subagents" in text_out


def test_unavailable_row_includes_executors(tmp_path):
    """Unavailable/stale rows still expose an executors field."""
    root = _root(tmp_path)
    root.mkdir(parents=True, exist_ok=True)
    record = _record(tmp_path, worktree=str(tmp_path / "nonexistent" / "path"))
    central_registry.register(root, record)

    result = snapshot(root, "2026-01-01T00:00:10+00:00")
    row = result["coordinators"][0]
    assert "executors" in row
    assert row["executors"] == "free_first"