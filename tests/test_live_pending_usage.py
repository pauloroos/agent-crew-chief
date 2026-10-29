"""Pending round usage through a lock-free reader (T038, FR-028, SC-012, US8/AC3).

A failed round with known usage and no job counts once in the coordinator
row and the global totals; after it is bound to a job it is still counted
once; an unreadable round becomes a diagnostic; snapshot() and
GET /api/snapshot leave the central file tree unchanged (no locks, no writes).
Synthetic central roots and loopback HTTP only; no real home, no network.
"""

from __future__ import annotations

import json
import threading
import uuid
from http.client import HTTPConnection
from pathlib import Path

import pytest

from crew_chief import central_registry, central_usage, records
from crew_chief.central_snapshot import snapshot


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


def _setup(tmp_path: Path):
    root = tmp_path / "central"
    worktree = tmp_path / "repo.crew" / "worktree-a"
    (worktree / "docs" / "crew-chief" / "jobs").mkdir(parents=True, exist_ok=True)
    return root, worktree


def _write_round(root: Path, coordinator_id: str, *, usage: dict | None = None,
                 state: str = "failed", job_id: str | None = None) -> tuple[str, Path]:
    rounds = root / "coordinators" / coordinator_id / "rounds"
    rounds.mkdir(parents=True, exist_ok=True)
    round_id = str(uuid.uuid4())
    meta = {
        "schema_version": 1,
        "id": round_id,
        "coordinator_id": coordinator_id,
        "state": state,
        "started_at": "2026-01-01T00:00:00+00:00",
        "finished_at": "2026-01-01T00:05:00+00:00",
        "usage": usage if usage is not None else {
            "role": "coordinator",
            "tokens_in": 11,
            "tokens_out": 7,
            "tokens_total": 18,
            "estimated": False,
            "model": "fake-model",
            "quota_family": None,
        },
        "active_job_id": job_id,
        "job_ids": [job_id] if job_id else [],
        "logs": {"stdout": None, "stderr": None},
        "error": "the CLI exited 1" if state == "failed" else None,
    }
    path = rounds / f"{round_id}.json"
    path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return round_id, path


def _tree_fingerprint(root: Path) -> dict[str, tuple[int, int, int]]:
    out: dict[str, tuple[int, int, int]] = {}
    for path in [root, *sorted(root.rglob("*"))]:
        try:
            stat = path.lstat()
        except OSError:
            continue
        out[str(path.relative_to(root))] = (stat.st_mode, stat.st_size, stat.st_mtime_ns)
    return out


def test_failed_round_without_job_counts_once():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        root, worktree = _setup(tmp_path)
        record = _record(tmp_path, worktree=str(worktree))
        central_registry.register(root, record)
        round_id, _ = _write_round(root, record["id"])

        pending = central_usage.read_pending_usage(root, record)
        assert len(pending) == 1
        assert pending[0]["event_id"] == f"central:{record['id']}:{round_id}"
        assert pending[0]["tokens_total"] == 18

        result = snapshot(root, "2026-01-01T00:00:10+00:00")
        assert result["totals"]["coordinator_tokens"] == 18
        # Defect D fix: total_tokens includes recorded coordinator usage (18).
        assert result["totals"]["total_tokens"] == 18
        row = result["coordinators"][0]
        models = row["models"]
        coord_total = sum(m.get("tokens_total") or 0 for m in models
                          if m.get("role") == "coordinator")
        assert coord_total == 18


def test_pending_still_counted_once_after_binding():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        root, worktree = _setup(tmp_path)
        record = _record(tmp_path, worktree=str(worktree))
        central_registry.register(root, record)
        round_id, _ = _write_round(root, record["id"])

        before = snapshot(root, "2026-01-01T00:00:10+00:00")
        assert before["totals"]["coordinator_tokens"] == 18

        job = records.new_job(worktree, "title", "request", False, "reason")
        records.save_job(worktree, job)
        record["active_job_id"] = job["id"]
        central_usage.backfill_usage(root, record)

        pending = central_usage.read_pending_usage(root, record)
        assert pending == []

        after = snapshot(root, "2026-01-01T00:00:20+00:00")
        assert after["totals"]["coordinator_tokens"] == 18
        # Defect D fix: total_tokens includes recorded coordinator usage (18).
        assert after["totals"]["total_tokens"] == 18


def test_unreadable_round_becomes_diagnostic_and_tree_unchanged():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        root, worktree = _setup(tmp_path)
        record = _record(tmp_path, worktree=str(worktree))
        central_registry.register(root, record)
        _write_round(root, record["id"])
        bad = root / "coordinators" / record["id"] / "rounds" / f"{uuid.uuid4()}.json"
        bad.write_text("{not json", encoding="utf-8")

        before = _tree_fingerprint(root)
        result = snapshot(root, "2026-01-01T00:00:10+00:00")
        after = _tree_fingerprint(root)
        assert after == before
        assert not list(root.rglob("*.lock"))
        assert any("not json" in str(d.get("message", "")) or str(bad.name) in str(d.get("message", "")) or str(bad.name) in str(d.get("record", ""))
                   for d in result["diagnostics"])
        assert result["coordinators"][0]["stale"] is False
        assert result["totals"]["coordinator_tokens"] == 18


def test_reader_missing_rounds_dir_reads_nothing_without_mkdir_or_locks():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        root, worktree = _setup(tmp_path)
        record = _record(tmp_path, worktree=str(worktree))
        central_registry.register(root, record)
        rounds = root / "coordinators" / record["id"] / "rounds"
        assert not rounds.exists()
        assert central_usage.read_pending_usage(root, record) == []
        assert not rounds.exists()
        assert not list(root.rglob("*.lock"))


@pytest.mark.parametrize("failure", ["missing-worktree", "unreadable-job"])
def test_known_pending_usage_survives_unavailable_worktree(tmp_path, failure):
    root, worktree = _setup(tmp_path)
    record = _record(tmp_path, worktree=str(worktree))
    if failure == "missing-worktree":
        record["worktree"] = str(tmp_path / "unavailable")
    else:
        (worktree / "docs/crew-chief/jobs/broken.json").write_text("{broken", encoding="utf-8")
    central_registry.register(root, record)
    _write_round(root, record["id"])
    before = _tree_fingerprint(root)

    result = snapshot(root, "2026-01-01T00:05:10+00:00")

    assert result["totals"]["total_tokens"] == 18
    assert result["totals"]["coordinator_tokens"] == 18
    assert sum(m["tokens_total"] for m in result["coordinators"][0]["models"]) == 18
    assert result["diagnostics"]
    assert _tree_fingerprint(root) == before


def test_previous_complete_row_stays_stale_but_pending_totals_are_current(tmp_path):
    root, worktree = _setup(tmp_path)
    record = _record(tmp_path, worktree=str(worktree))
    central_registry.register(root, record)
    _write_round(root, record["id"])
    previous = snapshot(root, "2026-01-01T00:05:10+00:00")
    record["worktree"] = str(tmp_path / "unavailable")
    central_registry.update_record(root, record["id"], {"worktree": record["worktree"]})
    _write_round(root, record["id"])
    before = _tree_fingerprint(root)

    result = snapshot(root, "2026-01-01T00:06:10+00:00", previous)

    assert result["coordinators"][0]["stale"] is True
    assert result["coordinators"][0]["models"] == previous["coordinators"][0]["models"]
    assert result["totals"]["coordinator_tokens"] == 36
    assert _tree_fingerprint(root) == before
