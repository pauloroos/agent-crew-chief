from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from crew_chief import central_messages, central_registry
from crew_chief.central_recovery import RecoveryError, reconcile, recover


def _record_for(tmp_path: Path, **overrides) -> dict:
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    stamp = "2026-01-01T00:00:00+00:00"
    record = {
        "schema_version": 1,
        "id": str(uuid.uuid4()),
        "repo": str(repo),
        "worktree": str(worktree),
        "branch": f"central/{uuid.uuid4()}",
        "base_commit": "0" * 40,
        "backend": "default",
        "cli": "claude",
        "cli_version": "",
        "acc_version": "",
        "session_id": None,
        "mode": "raw-request",
        "authorization": {
            "mode": "raw-request",
            "approved": False,
            "authorize_raw": True,
            "settings": {"coordinator_unrestricted": True},
        },
        "job_ids": [],
        "active_job_id": None,
        "state": "running",
        "created_at": stamp,
        "updated_at": stamp,
        "last_error": None,
        "supervisor_nonce": None,
    }
    record.update(overrides)
    return record


def _prepared(tmp_path: Path, **overrides):
    root = tmp_path / "central"
    record = _record_for(tmp_path, **overrides)
    central_registry.register(root, record)
    coord = central_messages._coordinator_dir(root, record["id"], create=True)
    return root, record, coord


def test_invalid_id_raises(tmp_path: Path):
    with pytest.raises(RecoveryError):
        reconcile(tmp_path, "not-a-uuid", 1.0)


def test_missing_record_returns_unknown(tmp_path: Path):
    result = reconcile(tmp_path, str(uuid.uuid4()), 1.0)
    assert result["ok"] is False
    assert result["state"] == "unknown"


def test_stale_heartbeat_holds_coordinator(tmp_path: Path):
    root, record, coord = _prepared(tmp_path, state="running")
    import json
    (coord / "supervisor.json").write_text(
        json.dumps({"pid": 12345, "heartbeat_at": "2020-01-01T00:00:00+00:00", "state": "running"}),
        encoding="utf-8",
    )
    result = reconcile(root, record["id"], 1_700_000_000.0)
    assert result["ok"] is True
    assert result["state"] == "paused"
    assert "stale heartbeat" in result["reason"]
    assert central_registry.load_registry(root)["coordinators"][record["id"]]["state"] == "paused"


def test_missing_worktree_fails(tmp_path: Path):
    root, record, coord = _prepared(tmp_path, state="running")
    import shutil
    shutil.rmtree(tmp_path / "worktree")
    result = reconcile(root, record["id"], 1.0)
    assert result["state"] == "failed"
    assert "missing worktree" in result["reason"]


def test_recover_resume(tmp_path: Path):
    root, record, coord = _prepared(tmp_path, state="paused")
    result = recover(root, record["id"], "resume")
    assert result["ok"] is True
    assert result["state"] == "idle"


def test_recover_does_not_signal(tmp_path: Path, monkeypatch):
    root, record, coord = _prepared(tmp_path, state="running")
    signalled = []
    monkeypatch.setattr("os.kill", lambda *a, **k: signalled.append(a))
    recover(root, record["id"], "stop")
    assert signalled == []
