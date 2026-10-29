"""Tests for pit-wall snapshot strips and launch ordering (T011)."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from crew_chief import central_messages, central_registry, records
from crew_chief.central_snapshot import _build_row, snapshot


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


def _setup(tmp_path: Path):
    root = _root(tmp_path)
    worktree = tmp_path / "repo.crew" / "worktree-a"
    (worktree / "docs" / "crew-chief" / "jobs").mkdir(parents=True, exist_ok=True)
    return root, worktree


def test_task_order_out_of_order(tmp_path):
    root, worktree = _setup(tmp_path)
    cid = central_registry.register(root, _record(tmp_path, worktree=str(worktree)))
    job = records.new_job(worktree, "title", "request", False, "reason")
    job["pieces"] = [
        {"n": 3, "title": "c", "type": "code", "ref": "T3", "accepted": False, "attempts": []},
        {"n": 1, "title": "a", "type": "code", "ref": "T1", "accepted": False, "attempts": []},
        {"n": 2, "title": "b", "type": "code", "ref": "T2", "accepted": False, "attempts": []},
    ]
    records.save_job(worktree, job)
    central_registry.update_record(root, cid, {"active_job_id": job["id"], "job_ids": [job["id"]]})

    row = snapshot(root, "2026-01-01T00:00:10+00:00")["coordinators"][0]
    strip = row["job_piece_strips"][0]
    assert [t["n"] for t in strip["tasks"]] == [1, 2, 3]


def test_accepted_keeps_failed_tick(tmp_path):
    root, worktree = _setup(tmp_path)
    cid = central_registry.register(root, _record(tmp_path, worktree=str(worktree)))
    job = records.new_job(worktree, "title", "request", False, "reason")
    job["pieces"] = [
        {
            "n": 1,
            "title": "p",
            "type": "code",
            "ref": "T1",
            "accepted": {"attempt": 2, "branch": "b", "at": "2026-01-01T00:00:00+00:00"},
            "attempts": [
                {"role": "exec", "status": "failed", "n": 1},
                {"role": "exec", "status": "ok", "n": 2},
            ],
        }
    ]
    records.save_job(worktree, job)
    central_registry.update_record(root, cid, {"active_job_id": job["id"], "job_ids": [job["id"]]})

    task = snapshot(root, "2026-01-01T00:00:10+00:00")["coordinators"][0]["job_piece_strips"][0]["tasks"][0]
    assert task["state"] == "accepted"
    assert task["failed_attempt"] is True


def test_waiting_via_open_question(tmp_path):
    root, worktree = _setup(tmp_path)
    cid = str(uuid.uuid4())
    central_registry.register(root, _record(tmp_path, id=cid, worktree=str(worktree)))
    job = records.new_job(worktree, "title", "request", False, "reason")
    job["pieces"] = [
        {"n": 1, "title": "p", "type": "code", "ref": "T2", "accepted": False, "attempts": []}
    ]
    records.save_job(worktree, job)
    central_registry.update_record(root, cid, {"active_job_id": job["id"], "job_ids": [job["id"]]})

    qid = str(uuid.uuid4())
    central_messages.send(
        root,
        cid,
        {
            "schema_version": 1,
            "id": qid,
            "coordinator_id": cid,
            "direction": "out",
            "kind": "question",
            "created_at": "2026-01-01T00:00:00+00:00",
            "reply_to": None,
            "task_refs": ["T2"],
            "body": "choose",
        },
    )

    task = snapshot(root, "2026-01-01T00:00:10+00:00")["coordinators"][0]["job_piece_strips"][0]["tasks"][0]
    assert task["state"] == "waiting"

    # answered question -> not waiting
    aid = str(uuid.uuid4())
    central_messages.send(
        root,
        cid,
        {
            "schema_version": 1,
            "id": aid,
            "coordinator_id": cid,
            "direction": "in",
            "kind": "answer",
            "created_at": "2026-01-01T00:00:00+00:00",
            "reply_to": qid,
            "task_refs": ["T2"],
            "body": "use X",
        },
    )
    task2 = snapshot(root, "2026-01-01T00:00:10+00:00")["coordinators"][0]["job_piece_strips"][0]["tasks"][0]
    assert task2["state"] == "not_started"


def test_running_via_reservation(tmp_path):
    root, worktree = _setup(tmp_path)
    cid = central_registry.register(root, _record(tmp_path, worktree=str(worktree)))
    job = records.new_job(worktree, "title", "request", False, "reason")
    job["pieces"] = [
        {"n": 1, "title": "p", "type": "code", "ref": "T1", "accepted": False, "attempts": [], "next_attempt": 2}
    ]
    records.save_job(worktree, job)
    central_registry.update_record(root, cid, {"active_job_id": job["id"], "job_ids": [job["id"]]})

    task = snapshot(root, "2026-01-01T00:00:10+00:00")["coordinators"][0]["job_piece_strips"][0]["tasks"][0]
    assert task["state"] == "running"


def test_latest_exec_ok_running(tmp_path):
    root, worktree = _setup(tmp_path)
    cid = central_registry.register(root, _record(tmp_path, worktree=str(worktree)))
    job = records.new_job(worktree, "title", "request", False, "reason")
    job["pieces"] = [
        {
            "n": 1,
            "title": "p",
            "type": "code",
            "ref": "T1",
            "accepted": False,
            "attempts": [{"role": "exec", "status": "ok", "n": 1}],
        }
    ]
    records.save_job(worktree, job)
    central_registry.update_record(root, cid, {"active_job_id": job["id"], "job_ids": [job["id"]]})

    task = snapshot(root, "2026-01-01T00:00:10+00:00")["coordinators"][0]["job_piece_strips"][0]["tasks"][0]
    assert task["state"] == "running"


def test_quota_brief_refused_review_not_ticking(tmp_path):
    root, worktree = _setup(tmp_path)
    cid = central_registry.register(root, _record(tmp_path, worktree=str(worktree)))
    job = records.new_job(worktree, "title", "request", False, "reason")
    job["pieces"] = [
        {
            "n": 1,
            "title": "p",
            "type": "code",
            "ref": "T1",
            "accepted": False,
            "attempts": [
                {"role": "exec", "status": "quota", "n": 1},
                {"role": "exec", "status": "brief_refused", "n": 2},
                {"role": "review", "status": "failed", "n": 3},
            ],
        }
    ]
    records.save_job(worktree, job)
    central_registry.update_record(root, cid, {"active_job_id": job["id"], "job_ids": [job["id"]]})

    task = snapshot(root, "2026-01-01T00:00:10+00:00")["coordinators"][0]["job_piece_strips"][0]["tasks"][0]
    assert task["failed_attempt"] is False
    assert task["state"] == "not_started"


def test_failed_attempt_statuses_tick(tmp_path):
    root, worktree = _setup(tmp_path)
    cid = central_registry.register(root, _record(tmp_path, worktree=str(worktree)))
    job = records.new_job(worktree, "title", "request", False, "reason")
    job["pieces"] = [
        {
            "n": 1,
            "title": "p",
            "type": "code",
            "ref": "T1",
            "accepted": False,
            "attempts": [{"role": "exec", "status": "timeout", "n": 1}],
        }
    ]
    records.save_job(worktree, job)
    central_registry.update_record(root, cid, {"active_job_id": job["id"], "job_ids": [job["id"]]})

    task = snapshot(root, "2026-01-01T00:00:10+00:00")["coordinators"][0]["job_piece_strips"][0]["tasks"][0]
    assert task["failed_attempt"] is True


def test_duplicate_refs_two_cells(tmp_path):
    root, worktree = _setup(tmp_path)
    cid = central_registry.register(root, _record(tmp_path, worktree=str(worktree)))
    job = records.new_job(worktree, "title", "request", False, "reason")
    job["pieces"] = [
        {"n": 1, "title": "a", "type": "code", "ref": "T1", "accepted": False, "attempts": []},
        {"n": 2, "title": "b", "type": "code", "ref": "T1", "accepted": False, "attempts": []},
    ]
    records.save_job(worktree, job)
    central_registry.update_record(root, cid, {"active_job_id": job["id"], "job_ids": [job["id"]]})

    tasks = snapshot(root, "2026-01-01T00:00:10+00:00")["coordinators"][0]["job_piece_strips"][0]["tasks"]
    assert len(tasks) == 2
    assert [t["n"] for t in tasks] == [1, 2]
    assert all(t["ref"] == "T1" for t in tasks)


def test_two_jobs_chronology_order(tmp_path):
    root, worktree = _setup(tmp_path)
    cid = central_registry.register(root, _record(tmp_path, worktree=str(worktree)))
    job1 = records.new_job(worktree, "first", "request", False, "reason", now=datetime(2026, 1, 2, tzinfo=timezone.utc))
    job1["pieces"] = [{"n": 1, "title": "a", "type": "code", "ref": "T1", "accepted": False, "attempts": []}]
    records.save_job(worktree, job1)
    job2 = records.new_job(worktree, "second", "request", False, "reason", now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    job2["pieces"] = [{"n": 1, "title": "b", "type": "code", "ref": "T2", "accepted": False, "attempts": []}]
    records.save_job(worktree, job2)
    central_registry.update_record(root, cid, {"active_job_id": job1["id"], "job_ids": [job1["id"], job2["id"]]})

    strips = snapshot(root, "2026-01-01T00:00:10+00:00")["coordinators"][0]["job_piece_strips"]
    assert [s["job_id"] for s in strips] == [job2["id"], job1["id"]]


def test_corrupt_job_stale_keeps_previous_fields(tmp_path):
    root, worktree = _setup(tmp_path)
    cid = central_registry.register(root, _record(tmp_path, worktree=str(worktree)))
    job = records.new_job(worktree, "title", "request", False, "reason")
    job["pieces"] = [{"n": 1, "title": "p", "type": "code", "ref": "T1", "accepted": False, "attempts": []}]
    records.save_job(worktree, job)
    central_registry.update_record(root, cid, {"active_job_id": job["id"], "job_ids": [job["id"]]})

    first = snapshot(root, "2026-01-01T00:00:10+00:00")
    prev_strips = first["coordinators"][0]["job_piece_strips"]
    # corrupt job file
    bad = worktree / "docs" / "crew-chief" / "jobs" / f"{job['id']}.json"
    bad.write_text("{corrupt", encoding="utf-8")
    second = snapshot(root, "2026-01-01T00:00:20+00:00", previous=first)
    assert second["coordinators"][0]["stale"] is True
    assert second["coordinators"][0]["job_piece_strips"] == prev_strips


def test_oversized_strip_truncated(tmp_path):
    root, worktree = _setup(tmp_path)
    cid = central_registry.register(root, _record(tmp_path, worktree=str(worktree)))
    job = records.new_job(worktree, "title", "request", False, "reason")
    job["pieces"] = [
        {"n": i, "title": f"p{i}", "type": "code", "ref": f"T{i}", "accepted": False, "attempts": []}
        for i in range(1, 258)
    ]
    records.save_job(worktree, job)
    central_registry.update_record(root, cid, {"active_job_id": job["id"], "job_ids": [job["id"]]})

    strip = snapshot(root, "2026-01-01T00:00:10+00:00")["coordinators"][0]["job_piece_strips"][0]
    assert len(strip["tasks"]) == 256
    assert strip["truncated"] is True


def test_unavailable_row_empty_strips(tmp_path):
    root, worktree = _setup(tmp_path)
    record = _record(tmp_path, worktree=str(worktree))
    central_registry.register(root, record)
    (worktree / "docs" / "crew-chief" / "jobs" / "bad.json").write_text("{corrupt", encoding="utf-8")
    row = snapshot(root, "2026-01-01T00:00:00+00:00")["coordinators"][0]
    assert row["stale"] is True
    assert row["job_piece_strips"] == []


def test_missing_created_at_none(tmp_path):
    root, worktree = _setup(tmp_path)
    rec = _record(tmp_path, worktree=str(worktree))
    rec["created_at"] = 123  # not a string
    rec["active_job_id"] = 456  # not a string
    now_dt = datetime(2026, 1, 1, tzinfo=timezone.utc)
    row, _diags, _jobs = _build_row(root, rec, now_dt, None)
    assert row["launched_at"] is None
    assert row["active_job_id"] is None


def test_no_pieces_empty_tasks(tmp_path):
    root, worktree = _setup(tmp_path)
    cid = central_registry.register(root, _record(tmp_path, worktree=str(worktree)))
    job = records.new_job(worktree, "title", "request", False, "reason")
    job["pieces"] = []
    records.save_job(worktree, job)
    central_registry.update_record(root, cid, {"active_job_id": job["id"], "job_ids": [job["id"]]})

    strip = snapshot(root, "2026-01-01T00:00:10+00:00")["coordinators"][0]["job_piece_strips"][0]
    assert strip["tasks"] == []
    assert strip["truncated"] is False


def test_unnumbered_pieces_fallback(tmp_path):
    root, worktree = _setup(tmp_path)
    cid = central_registry.register(root, _record(tmp_path, worktree=str(worktree)))
    job = records.new_job(worktree, "title", "request", False, "reason")
    job["pieces"] = [
        {"title": "no n", "type": "code", "ref": "T0", "accepted": False, "attempts": []},
        {"n": 1, "title": "a", "type": "code", "ref": "T1", "accepted": False, "attempts": []},
    ]
    records.save_job(worktree, job)
    central_registry.update_record(root, cid, {"active_job_id": job["id"], "job_ids": [job["id"]]})

    tasks = snapshot(root, "2026-01-01T00:00:10+00:00")["coordinators"][0]["job_piece_strips"][0]["tasks"]
    assert [t["n"] for t in tasks] == [1, None]


def test_launched_at_from_created_at(tmp_path):
    root, worktree = _setup(tmp_path)
    rec = _record(tmp_path, worktree=str(worktree), created_at="2026-02-02T00:00:00+00:00")
    cid = central_registry.register(root, rec)
    row = snapshot(root, "2026-01-01T00:00:10+00:00")["coordinators"][0]
    assert row["launched_at"] == "2026-02-02T00:00:00+00:00"
    assert row["active_job_id"] is None


# --- appended T011 events tests ---


def test_events_accepted_and_failed_attempt(tmp_path):
    root, worktree = _setup(tmp_path)
    cid = central_registry.register(root, _record(tmp_path, worktree=str(worktree)))
    job = records.new_job(worktree, "title", "request", False, "reason")
    job["pieces"] = [
        {
            "n": 1,
            "title": "p",
            "type": "code",
            "ref": "T1",
            "accepted": {"attempt": 2, "branch": "b", "at": "2026-01-01T00:00:05+00:00"},
            "attempts": [
                {"role": "exec", "status": "failed", "n": 1, "started": "2026-01-01T00:00:01+00:00", "duration_s": 2},
            ],
        }
    ]
    records.save_job(worktree, job)
    central_registry.update_record(root, cid, {"active_job_id": job["id"], "job_ids": [job["id"]]})

    row = snapshot(root, "2026-01-01T00:00:10+00:00")["coordinators"][0]
    events = row["events"]
    assert len(events) == 2
    assert set(events[0].keys()) == {"at", "kind", "job_id", "n", "ref", "attempt"}
    assert events[0]["kind"] == "attempt_failed"
    assert events[0]["at"] == "2026-01-01T00:00:03+00:00"
    assert events[0]["job_id"] == job["id"]
    assert events[0]["n"] == 1
    assert events[0]["ref"] == "T1"
    assert events[0]["attempt"] == 1
    assert set(events[1].keys()) == {"at", "kind", "job_id", "n", "ref"}
    assert events[1]["kind"] == "accepted"
    assert events[1]["at"] == "2026-01-01T00:00:05+00:00"
    assert row["events_truncated"] is False


def test_events_ordering_across_two_jobs(tmp_path):
    root, worktree = _setup(tmp_path)
    cid = central_registry.register(root, _record(tmp_path, worktree=str(worktree)))
    job1 = records.new_job(worktree, "first", "request", False, "reason", now=datetime(2026, 1, 2, tzinfo=timezone.utc))
    job1["pieces"] = [{"n": 1, "title": "a", "type": "code", "ref": "T1", "accepted": {"at": "2026-01-01T00:00:02+00:00"}, "attempts": []}]
    records.save_job(worktree, job1)
    job2 = records.new_job(worktree, "second", "request", False, "reason", now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    job2["pieces"] = [{"n": 1, "title": "b", "type": "code", "ref": "T2", "accepted": {"at": "2026-01-01T00:00:01+00:00"}, "attempts": []}]
    records.save_job(worktree, job2)
    central_registry.update_record(root, cid, {"active_job_id": job1["id"], "job_ids": [job1["id"], job2["id"]]})

    events = snapshot(root, "2026-01-01T00:00:10+00:00")["coordinators"][0]["events"]
    assert [e["ref"] for e in events] == ["T2", "T1"]


def test_events_review_quota_brief_refused_no_event(tmp_path):
    root, worktree = _setup(tmp_path)
    cid = central_registry.register(root, _record(tmp_path, worktree=str(worktree)))
    job = records.new_job(worktree, "title", "request", False, "reason")
    job["pieces"] = [
        {
            "n": 1,
            "title": "p",
            "type": "code",
            "ref": "T1",
            "accepted": False,
            "attempts": [
                {"role": "review", "status": "failed", "n": 1, "started": "2026-01-01T00:00:01+00:00"},
                {"role": "exec", "status": "quota", "n": 2, "started": "2026-01-01T00:00:02+00:00"},
                {"role": "exec", "status": "brief_refused", "n": 3, "started": "2026-01-01T00:00:03+00:00"},
            ],
        }
    ]
    records.save_job(worktree, job)
    central_registry.update_record(root, cid, {"active_job_id": job["id"], "job_ids": [job["id"]]})

    row = snapshot(root, "2026-01-01T00:00:10+00:00")["coordinators"][0]
    assert row["events"] == []


def test_events_invalid_times_skipped_naive_utc(tmp_path):
    root, worktree = _setup(tmp_path)
    cid = central_registry.register(root, _record(tmp_path, worktree=str(worktree)))
    job = records.new_job(worktree, "title", "request", False, "reason")
    job["pieces"] = [
        {"n": 1, "title": "a", "type": "code", "ref": "T1", "accepted": {"at": "2026-01-01T00:00:00"}, "attempts": []},
        {"n": 2, "title": "b", "type": "code", "ref": "T2", "accepted": {"at": 123}, "attempts": []},
        {"n": 3, "title": "c", "type": "code", "ref": "T3", "accepted": {"at": "not-a-date"}, "attempts": []},
        {
            "n": 4,
            "title": "d",
            "type": "code",
            "ref": "T4",
            "accepted": False,
            "attempts": [
                {"role": "exec", "status": "failed", "n": 1, "started": None},
                {"role": "exec", "status": "failed", "n": 2, "started": "garbage"},
                {"role": "exec", "status": "failed", "n": 3, "started": 999},
            ],
        },
    ]
    records.save_job(worktree, job)
    central_registry.update_record(root, cid, {"active_job_id": job["id"], "job_ids": [job["id"]]})

    events = snapshot(root, "2026-01-01T00:00:10+00:00")["coordinators"][0]["events"]
    assert len(events) == 1
    assert events[0]["kind"] == "accepted"
    assert events[0]["at"] == "2026-01-01T00:00:00+00:00"


def test_events_duration_added(tmp_path):
    root, worktree = _setup(tmp_path)
    cid = central_registry.register(root, _record(tmp_path, worktree=str(worktree)))
    job = records.new_job(worktree, "title", "request", False, "reason")
    job["pieces"] = [
        {
            "n": 1,
            "title": "p",
            "type": "code",
            "ref": "T1",
            "accepted": False,
            "attempts": [
                {"role": "exec", "status": "timeout", "n": 1, "started": "2026-01-01T00:00:01+00:00", "duration_s": 5},
            ],
        }
    ]
    records.save_job(worktree, job)
    central_registry.update_record(root, cid, {"active_job_id": job["id"], "job_ids": [job["id"]]})

    events = snapshot(root, "2026-01-01T00:00:10+00:00")["coordinators"][0]["events"]
    assert len(events) == 1
    assert events[0]["at"] == "2026-01-01T00:00:06+00:00"


def test_events_truncated_201(tmp_path):
    from datetime import timedelta

    root, worktree = _setup(tmp_path)
    cid = central_registry.register(root, _record(tmp_path, worktree=str(worktree)))
    job = records.new_job(worktree, "title", "request", False, "reason")
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    pieces = []
    for i in range(1, 202):
        at = (base + timedelta(seconds=i)).isoformat()
        pieces.append({"n": i, "title": f"p{i}", "type": "code", "ref": f"T{i}", "accepted": {"at": at}, "attempts": []})
    job["pieces"] = pieces
    records.save_job(worktree, job)
    central_registry.update_record(root, cid, {"active_job_id": job["id"], "job_ids": [job["id"]]})

    row = snapshot(root, "2026-01-01T00:00:10+00:00")["coordinators"][0]
    assert len(row["events"]) == 200
    assert row["events_truncated"] is True
    assert row["events"][0]["n"] == 2


def test_events_not_truncated_200(tmp_path):
    from datetime import timedelta

    root, worktree = _setup(tmp_path)
    cid = central_registry.register(root, _record(tmp_path, worktree=str(worktree)))
    job = records.new_job(worktree, "title", "request", False, "reason")
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    pieces = []
    for i in range(1, 201):
        at = (base + timedelta(seconds=i)).isoformat()
        pieces.append({"n": i, "title": f"p{i}", "type": "code", "ref": f"T{i}", "accepted": {"at": at}, "attempts": []})
    job["pieces"] = pieces
    records.save_job(worktree, job)
    central_registry.update_record(root, cid, {"active_job_id": job["id"], "job_ids": [job["id"]]})

    row = snapshot(root, "2026-01-01T00:00:10+00:00")["coordinators"][0]
    assert len(row["events"]) == 200
    assert row["events_truncated"] is False


def test_events_unavailable_row_empty(tmp_path):
    root, worktree = _setup(tmp_path)
    record = _record(tmp_path, worktree=str(worktree))
    central_registry.register(root, record)
    (worktree / "docs" / "crew-chief" / "jobs" / "bad.json").write_text("{corrupt", encoding="utf-8")
    row = snapshot(root, "2026-01-01T00:00:00+00:00")["coordinators"][0]
    assert row["events"] == []
    assert row["events_truncated"] is False


def test_stale_pieces_excluded_from_strips_and_events(tmp_path):
    root, worktree = _setup(tmp_path)
    cid = central_registry.register(root, _record(tmp_path, worktree=str(worktree)))
    job = records.new_job(worktree, "title", "request", False, "reason")
    job["pieces"] = [
        {"n": 1, "title": "a", "type": "code", "ref": "T1", "accepted": {"at": "2026-01-01T00:00:01+00:00"}, "attempts": []},
        {"n": 2, "title": "b", "type": "code", "ref": "T2", "stale": True, "accepted": {"at": "2026-01-01T00:00:02+00:00"}, "attempts": []},
    ]
    records.save_job(worktree, job)
    central_registry.update_record(root, cid, {"active_job_id": job["id"], "job_ids": [job["id"]]})

    row = snapshot(root, "2026-01-01T00:00:10+00:00")["coordinators"][0]
    strip = row["job_piece_strips"][0]
    assert [t["n"] for t in strip["tasks"]] == [1]
    assert [e["ref"] for e in row["events"]] == ["T1"]


def test_snapshot_does_not_write_job_folder(tmp_path):
    import hashlib

    root, worktree = _setup(tmp_path)
    cid = central_registry.register(root, _record(tmp_path, worktree=str(worktree)))
    job = records.new_job(worktree, "title", "request", False, "reason")
    job["pieces"] = [{"n": 1, "title": "p", "type": "code", "ref": "T1", "accepted": False, "attempts": []}]
    records.save_job(worktree, job)
    central_registry.update_record(root, cid, {"active_job_id": job["id"], "job_ids": [job["id"]]})

    jobs_dir = worktree / "docs" / "crew-chief" / "jobs"

    def folder_hash():
        h = hashlib.sha256()
        for path in sorted(jobs_dir.rglob("*")):
            if path.is_file():
                h.update(path.read_bytes())
        return h.hexdigest()

    before = folder_hash()
    snapshot(root, "2026-01-01T00:00:10+00:00")
    after = folder_hash()
    assert before == after
