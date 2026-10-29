"""T040 — review lifecycle, attempt identity and same-repo ids (SC-012, FR-030, US8/AC5-6).

One accepted task plus a ref-less auxiliary review with an ok graded review
attempt and a completed coordinator: no active piece, no 'pending', spec task
counts 1/1 beside the raw totals; a piece without status but with finished
attempts is not pending; two coordinators with the same repo label and CLI
are distinguishable in plain and narrow terminal output; no line is 'None'.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from crew_chief import central_registry, records
from crew_chief.central_snapshot import _active_piece, _piece_strip_state, snapshot
from crew_chief.central_terminal import render


def _record(tmp_path: Path, **overrides) -> dict:
    coordinator_id = overrides.pop("id", str(uuid.uuid4()))
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


def _setup(tmp_path: Path, worktree_name: str = "worktree-a"):
    root = tmp_path / "central"
    worktree = tmp_path / "repo.crew" / worktree_name
    (worktree / "docs" / "crew-chief" / "jobs").mkdir(parents=True, exist_ok=True)
    return root, worktree


def test_completed_coordinator_hides_finished_aux_review_and_counts_spec_tasks(tmp_path):
    root, worktree = _setup(tmp_path)
    record = _record(tmp_path, worktree=str(worktree), state="completed")
    cid = central_registry.register(root, record)
    job = records.new_job(worktree, "title", "request", False, "reason")
    job["pieces"] = [
        {
            "n": 1,
            "title": "spec task",
            "type": "code",
            "ref": "T1",
            "accepted": {"attempt": 1, "branch": "b", "at": "2026-01-01T00:00:05+00:00"},
            "attempts": [{"role": "exec", "status": "ok", "n": 1}],
        },
        {
            "n": 2,
            "title": "aux review",
            "type": "review",
            "attempts": [{"role": "review", "status": "ok", "n": 1, "executor": "rev"}],
            "grades": {"rev": {"executor": "rev", "score": 90}},
        },
    ]
    records.save_job(worktree, job)
    central_registry.update_record(root, cid, {"active_job_id": job["id"], "job_ids": [job["id"]]})

    row = snapshot(root, "2026-01-01T00:00:10+00:00")["coordinators"][0]
    assert row["active_pieces"] == []
    # spec tasks exclude the ref-less auxiliary review; raw totals keep it.
    assert row["spec_tasks_total"] == 1
    assert row["spec_tasks_accepted"] == 1
    assert row["accepted"] == 1
    assert row["total"] == 2

    out = render(snapshot(root, "2026-01-01T00:00:10+00:00"), None, 120, plain=True)
    assert "None" not in out
    assert "state=pending" not in out
    assert str(cid)[:8] in out


def test_missing_status_with_finished_attempts_is_not_pending(tmp_path):
    from datetime import datetime, timezone

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    piece = {"n": 1, "title": "p", "type": "code", "ref": "T1",
             "accepted": False, "attempts": [{"role": "exec", "status": "failed", "n": 1}]}
    active = _active_piece(piece, now)
    assert active is not None
    assert active["status"] != "pending"

    root, worktree = _setup(tmp_path)
    cid = central_registry.register(root, _record(tmp_path, worktree=str(worktree)))
    job = records.new_job(worktree, "title", "request", False, "reason")
    job["pieces"] = [dict(piece)]
    del job["pieces"][0]["ref"]
    job["pieces"][0]["status"] = None
    # keep a ref-less finished piece: snapshot ref falls back, strip stays truthful.
    records.save_job(worktree, job)
    central_registry.update_record(root, cid, {"active_job_id": job["id"], "job_ids": [job["id"]]})
    row = snapshot(root, "2026-01-01T00:00:10+00:00")["coordinators"][0]
    assert row["active_pieces"][0]["status"] != "pending"
    task = row["job_piece_strips"][0]["tasks"][0]
    assert task["state"] in ("accepted", "waiting", "running", "not_started")
    assert task["state"] != "pending"


def test_ref_falls_back_to_hash_n_and_attempt_only_from_attempts(tmp_path):
    root, worktree = _setup(tmp_path)
    cid = central_registry.register(root, _record(tmp_path, worktree=str(worktree)))
    job = records.new_job(worktree, "title", "request", False, "reason")
    job["pieces"] = [
        {"n": 3, "title": "aux", "type": "code", "accepted": False,
         "attempts": [{"role": "exec", "status": "ok", "n": 7}]},
        {"n": 1, "title": "p", "type": "code", "ref": "T1", "accepted": False,
         "attempts": [{"role": "exec", "status": "failed", "started": "2026-01-01T00:00:01+00:00"}]},
    ]
    records.save_job(worktree, job)
    central_registry.update_record(root, cid, {"active_job_id": job["id"], "job_ids": [job["id"]]})

    row = snapshot(root, "2026-01-01T00:00:10+00:00")["coordinators"][0]
    by_n = {p.get("n"): p for p in job["pieces"]}
    refs = {p["ref"] for p in row["active_pieces"]}
    assert "#3" in refs
    assert None not in refs
    aux = next(p for p in row["active_pieces"] if p["ref"] == "#3")
    assert aux["attempt"] == 7
    assert aux["attempt"] != 3
    failed_events = [e for e in row["events"] if e["kind"] == "attempt_failed"]
    assert failed_events
    assert failed_events[0]["attempt"] is None
    assert failed_events[0]["attempt"] != by_n[1]["n"] or failed_events[0]["attempt"] is None

    out = render(snapshot(root, "2026-01-01T00:00:10+00:00"), None, 120, plain=True)
    assert "None" not in out
    assert "#3" in out


def test_same_repo_same_cli_distinguishable_plain_and_narrow(tmp_path):
    root, worktree_a = _setup(tmp_path, "worktree-a")
    worktree_b = tmp_path / "repo.crew" / "worktree-b"
    (worktree_b / "docs" / "crew-chief" / "jobs").mkdir(parents=True, exist_ok=True)
    repo = str(tmp_path / "repo")
    cid_a = central_registry.register(root, _record(tmp_path, worktree=str(worktree_a), repo=repo, cli="claude"))
    cid_b = central_registry.register(root, _record(tmp_path, worktree=str(worktree_b), repo=repo, cli="claude"))
    assert str(cid_a) != str(cid_b)

    snap = snapshot(root, "2026-01-01T00:00:10+00:00")
    assert snap["coordinators"][0]["repo_label"] == snap["coordinators"][1]["repo_label"]
    for width in (120, 40):
        out = render(snap, None, width, plain=True)
        assert str(cid_a)[:8] in out
        assert str(cid_b)[:8] in out
        lines = [line for line in out.splitlines() if str(cid_a)[:8] in line or str(cid_b)[:8] in line]
        assert len(lines) >= 2
        assert "None" not in out


def test_terminal_never_prints_none(tmp_path):
    snap = {
        "coordinators": [
            {
                "id": None,
                "repo_label": None,
                "repo": None,
                "cli": None,
                "state": None,
                "accepted": None,
                "total": None,
                "active_pieces": [
                    {"ref": None, "n": 2, "title": None, "work_type": None,
                     "type": None, "executor": None, "status": None, "elapsed_s": None},
                    {"title": "no ref key"},
                ],
                "jobs": [{"id": None, "title": None, "state": None, "start": None, "end": None}],
                "metrics": {},
                "stale": False,
                "worktree_available": True,
                "last_error": None,
                "models": [{"role": None, "model": None, "tier": None}],
                "scoreboard": None,
            }
        ],
        "questions": [{"id": None, "coordinator_id": None, "body": None, "task_refs": None}],
        "alerts": [{"coordinator_id": None, "reason": None}],
        "totals": {},
        "diagnostics": [{"record": None, "message": None}, None],
    }
    for width in (120, 40, 20):
        out = render(snap, None, width, plain=True)
        assert "None" not in out


def test_reviewed_unaccepted_code_with_ref_stays_active(tmp_path):
    from datetime import datetime, timezone

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    piece = {
        "n": 1,
        "title": "spec task",
        "type": "code",
        "ref": "T001",
        "accepted": False,
        "attempts": [
            {"role": "exec", "status": "ok", "n": 1},
            {"role": "review", "status": "ok", "n": 1, "executor": "rev", "score": 90},
        ],
        "grades": {"rev": {"executor": "rev", "score": 90}},
    }
    for ended in (True, False):
        active = _active_piece(dict(piece), now, ended)
        assert active is not None
        assert active["ref"] == "T001"

    for state in ("completed", "stopped", "failed"):
        root, worktree = _setup(tmp_path, f"wt-{state}")
        record = _record(tmp_path, worktree=str(worktree), state=state)
        cid = central_registry.register(root, record)
        job = records.new_job(worktree, "title", "request", False, "reason")
        job["pieces"] = [dict(piece)]
        records.save_job(worktree, job)
        central_registry.update_record(root, cid, {"active_job_id": job["id"], "job_ids": [job["id"]]})
        row = snapshot(root, "2026-01-01T00:00:10+00:00")["coordinators"][0]
        refs = [p["ref"] for p in row["active_pieces"]]
        assert "T001" in refs


def test_no_ref_aux_review_still_complete(tmp_path):
    from datetime import datetime, timezone

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    piece = {
        "n": 2,
        "title": "aux review",
        "type": "review",
        "accepted": False,
        "attempts": [{"role": "review", "status": "ok", "n": 1, "executor": "rev"}],
        "grades": {"rev": {"executor": "rev", "score": 90}},
    }
    assert _active_piece(dict(piece), now, True) is None


def test_no_ref_code_piece_counts_without_aux_reviews_unaccepted(tmp_path):
    # T065 red: a small job with no spec refs must count the actionable
    # no-ref type=code piece (1/0) while still excluding the finished
    # no-ref auxiliary review; counts and strip must agree.
    root, worktree = _setup(tmp_path)
    record = _record(tmp_path, worktree=str(worktree), state="created")
    cid = central_registry.register(root, record)
    job = records.new_job(worktree, "title", "request", False, "reason")
    job["pieces"] = [
        {
            "n": 1,
            "title": "small code work",
            "type": "code",
            "accepted": False,
            "attempts": [{"role": "exec", "status": "ok", "n": 1}],
        },
        {
            "n": 2,
            "title": "aux review",
            "type": "review",
            "accepted": False,
            "attempts": [{"role": "review", "status": "ok", "n": 1, "executor": "rev"}],
            "grades": {"rev": {"executor": "rev", "score": 90}},
        },
    ]
    records.save_job(worktree, job)
    central_registry.update_record(root, cid, {"active_job_id": job["id"], "job_ids": [job["id"]]})

    row = snapshot(root, "2026-01-01T00:00:10+00:00")["coordinators"][0]
    assert row["spec_tasks_total"] == 1
    assert row["spec_tasks_accepted"] == 0
    strip = row["job_piece_strips"][0]
    assert {t["n"] for t in strip["tasks"]} == {1, 2}
    refs = {p["ref"] for p in row["active_pieces"]}
    assert "#1" in refs


def test_no_ref_null_code_piece_counts_when_accepted(tmp_path):
    # T065 red: the real-lab shape (ref=null, accepted type=code piece)
    # must read 1/1 instead of 0/0; the finished no-ref review stays out.
    root, worktree = _setup(tmp_path)
    record = _record(tmp_path, worktree=str(worktree), state="completed")
    cid = central_registry.register(root, record)
    job = records.new_job(worktree, "title", "request", False, "reason")
    job["pieces"] = [
        {
            "n": 1,
            "title": "small code work",
            "type": "code",
            "ref": None,
            "accepted": {"attempt": 1, "branch": "b", "at": "2026-01-01T00:00:05+00:00"},
            "attempts": [{"role": "exec", "status": "ok", "n": 1}],
        },
        {
            "n": 2,
            "title": "aux review",
            "type": "review",
            "attempts": [{"role": "review", "status": "ok", "n": 1, "executor": "rev"}],
            "grades": {"rev": {"executor": "rev", "score": 90}},
        },
    ]
    records.save_job(worktree, job)
    central_registry.update_record(root, cid, {"active_job_id": job["id"], "job_ids": [job["id"]]})

    row = snapshot(root, "2026-01-01T00:00:10+00:00")["coordinators"][0]
    assert row["spec_tasks_total"] == 1
    assert row["spec_tasks_accepted"] == 1
    assert row["active_pieces"] == []
    strip = row["job_piece_strips"][0]
    assert {t["n"] for t in strip["tasks"]} == {1, 2}


def test_ref_job_keeps_spec_ref_counts(tmp_path):
    # T065 guard: a normal ref-based spec job keeps ref-only behavior; a
    # no-ref code piece beside spec refs is not counted as a spec task.
    root, worktree = _setup(tmp_path)
    record = _record(tmp_path, worktree=str(worktree), state="created")
    cid = central_registry.register(root, record)
    job = records.new_job(worktree, "title", "request", False, "reason")
    job["pieces"] = [
        {
            "n": 1,
            "title": "spec task",
            "type": "code",
            "ref": "T1",
            "accepted": False,
            "attempts": [],
        },
        {
            "n": 2,
            "title": "side code work",
            "type": "code",
            "accepted": False,
            "attempts": [],
        },
        {
            "n": 3,
            "title": "aux review",
            "type": "review",
            "accepted": False,
            "attempts": [{"role": "review", "status": "ok", "n": 1, "executor": "rev"}],
            "grades": {"rev": {"executor": "rev", "score": 90}},
        },
    ]
    records.save_job(worktree, job)
    central_registry.update_record(root, cid, {"active_job_id": job["id"], "job_ids": [job["id"]]})

    row = snapshot(root, "2026-01-01T00:00:10+00:00")["coordinators"][0]
    assert row["spec_tasks_total"] == 1
    assert row["spec_tasks_accepted"] == 0
    strip = row["job_piece_strips"][0]
    assert {t["n"] for t in strip["tasks"]} == {1, 2, 3}


def test_running_attempt_without_status_is_running_and_matches_strip(tmp_path):
    from datetime import datetime, timezone

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    recorded = {
        "n": 1,
        "title": "p",
        "type": "code",
        "ref": "T1",
        "accepted": False,
        "attempts": [{"role": "exec", "status": "running", "n": 1}],
    }
    active = _active_piece(dict(recorded), now)
    assert active is not None
    assert active["status"] == "running"
    assert active["status"] == _piece_strip_state(recorded, [], "T1")

    reserved = {
        "n": 1,
        "title": "p",
        "type": "code",
        "ref": "T1",
        "accepted": False,
        "attempts": [],
        "next_attempt": 2,
    }
    active = _active_piece(dict(reserved), now)
    assert active is not None
    assert active["status"] == "running"
    assert active["status"] == _piece_strip_state(reserved, [], "T1")
