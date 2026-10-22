"""Smoke tests for central_snapshot.snapshot."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from crew_chief import central_metrics, central_registry, records
from crew_chief.central_snapshot import snapshot

SCHEMA = 1


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


def test_missing_registry_empty(tmp_path):
    root = _root(tmp_path)
    root.mkdir(parents=True, exist_ok=True)
    result = snapshot(root, "2026-01-01T00:00:00+00:00")
    assert result["schema_version"] == SCHEMA
    assert result["coordinators"] == []
    assert result["questions"] == []
    assert result["diagnostics"] == []


def test_read_only_and_stale(tmp_path):
    root = _root(tmp_path)
    (tmp_path / "repo.crew" / "worktree-a" / "docs" / "crew-chief" / "jobs").mkdir(parents=True, exist_ok=True)
    worktree = tmp_path / "repo.crew" / "worktree-a"
    record = _record(tmp_path, worktree=str(worktree))
    cid = central_registry.register(root, record)
    # create a job
    job = records.new_job(worktree, "title", "request", False, "reason")
    job["pieces"] = [{"n": 1, "title": "p", "type": "code", "accepted": False, "ref": "T1",
                      "attempts": [{"role": "exec", "status": "ok", "cost": "free", "tokens_total": 5,
                                    "started": "2026-01-01T00:00:00+00:00", "duration_s": 2}]}]
    records.save_job(worktree, job)
    central_registry.update_record(root, cid, {"active_job_id": job["id"], "job_ids": [job["id"]]})

    before = (root / "registry.json").read_bytes()
    first = snapshot(root, "2026-01-01T00:00:10+00:00")
    assert (root / "registry.json").read_bytes() == before
    assert len(first["coordinators"]) == 1
    row = first["coordinators"][0]
    assert row["worktree_available"] is True
    assert row["stale"] is False
    assert row["jobs"][0]["id"] == job["id"]

    # make worktree unavailable
    import shutil
    shutil.rmtree(worktree)
    second = snapshot(root, "2026-01-01T00:00:20+00:00", previous=first)
    assert second["coordinators"][0]["stale"] is True
    assert second["coordinators"][0]["jobs"] == row["jobs"]


def test_corrupt_job_first_read_unavailable(tmp_path):
    root = _root(tmp_path)
    worktree = tmp_path / "repo.crew" / "worktree-a"
    (worktree / "docs" / "crew-chief" / "jobs").mkdir(parents=True, exist_ok=True)
    record = _record(tmp_path, worktree=str(worktree))
    central_registry.register(root, record)
    # partially written job
    (worktree / "docs" / "crew-chief" / "jobs" / "bad.json").write_text("{corrupt", encoding="utf-8")
    result = snapshot(root, "2026-01-01T00:00:00+00:00")
    assert result["diagnostics"]
    assert result["coordinators"][0]["stale"] is True
    assert result["coordinators"][0]["worktree_available"] is True


def test_accepted_pieces_are_not_active(tmp_path):
    """records.mark_accepted stores an object, not True; both mean accepted."""
    root = _root(tmp_path)
    worktree = tmp_path / "repo.crew" / "worktree-a"
    (worktree / "docs" / "crew-chief" / "jobs").mkdir(parents=True, exist_ok=True)
    cid = central_registry.register(root, _record(tmp_path, worktree=str(worktree)))
    job = records.new_job(worktree, "title", "request", False, "reason")
    job["pieces"] = [
        {"n": 1, "title": "object", "type": "code", "ref": "T1", "attempts": []},
        {"n": 2, "title": "legacy", "type": "code", "ref": "T2", "accepted": True, "attempts": []},
        {"n": 3, "title": "open", "type": "code", "ref": "T3", "accepted": False, "attempts": []},
    ]
    records.mark_accepted(job, 1, {"n": 1, "branch": "crew/t1"})
    records.save_job(worktree, job)
    central_registry.update_record(root, cid, {"active_job_id": job["id"], "job_ids": [job["id"]]})

    row = snapshot(root, "2026-01-01T00:00:10+00:00")["coordinators"][0]

    assert [p["ref"] for p in row["active_pieces"]] == ["T3"]
