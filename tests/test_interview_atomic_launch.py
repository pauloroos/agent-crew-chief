"""Concurrent final answers launch exactly once; expired jobs reject answers (P1-3, P2-4)."""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from crew_chief import maestro, records
from crew_chief import interview_service as svc
from crew_chief.interview import InterviewExpiredError


def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    prefs = tmp_path / "global-prefs.toml"
    prefs.write_text("", encoding="utf-8")
    monkeypatch.setenv("AGENT_CREW_CHIEF_GLOBAL_PREFS", str(prefs))
    repo = tmp_path / "project"
    repo.mkdir(parents=True, exist_ok=True)
    return repo


def test_concurrent_final_answers_launch_exactly_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Two threads answering the final question concurrently must trigger one launch."""
    repo = _isolated_env(tmp_path, monkeypatch)
    started = svc.start_interview_job(
        project=repo, request="test request", coordinator="claude:sonnet",
        executors="own", title="concurrent",
    )
    jid = started["job_id"]
    assert started["pending"] == ["end_mode"]

    calls: list[str] = []
    lock = threading.Lock()

    def fake_launch(*_args, **_kwargs):
        with lock:
            calls.append("launch")
            n = len(calls)
        # Widen the old race window: old code saves without a lock, so a slow
        # save lets both threads decide to launch. New code holds job_lock
        # across answer->decide->claim, so the second thread waits.
        time.sleep(0.05)
        return {"id": f"coord-{n}", "pid": 999999}

    real_save = records.save_job

    def slow_save(project, job):
        time.sleep(0.2)
        return real_save(project, job)

    start_barrier = threading.Barrier(2)
    errors: list[str] = []
    results: list[dict] = []

    def answer_one():
        try:
            start_barrier.wait(timeout=5)
            results.append(svc.answer_interview_job(project=repo, job_id=jid, key="end_mode", value="pr"))
        except Exception as exc:  # pragma: no cover
            errors.append(repr(exc))

    with patch.object(maestro, "launch", fake_launch), patch.object(records, "save_job", slow_save):
        threads = [threading.Thread(target=answer_one) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(15)

    assert not errors, f"answer threads raised: {errors}"
    assert len(results) == 2
    assert len(calls) == 1, f"concurrent final answers launched {len(calls)} coordinators, expected exactly 1"
    stored = records.load_job(repo, jid)
    assert stored.get("state") == "running"
    assert stored.get("coordinator_id") == "coord-1"


def test_expired_job_rejects_answers_and_never_launches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A job that transitioned to expired must stay expired and reject the final answer."""
    repo = _isolated_env(tmp_path, monkeypatch)
    started = svc.start_interview_job(
        project=repo, request="test request", coordinator="claude:sonnet",
        executors="own", title="expired",
    )
    jid = started["job_id"]

    job = records.load_job(repo, jid)
    job["interview"]["started_at"] = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    records.save_job(repo, job)

    status = svc.get_interview_status(project=repo, job_id=jid)
    assert status["state"] == "expired"

    launched: list[str] = []

    def fake_launch(*_args, **_kwargs):
        launched.append("launch")
        return {"id": "coord-x", "pid": 999999}

    with patch.object(maestro, "launch", fake_launch):
        with pytest.raises(InterviewExpiredError):
            svc.answer_interview_job(project=repo, job_id=jid, key="end_mode", value="pr")

    assert launched == [], "expired job must never launch a coordinator"
    stored = records.load_job(repo, jid)
    assert stored.get("state") == "expired"
    assert stored.get("status") == "expired"
    assert "coordinator_id" not in stored or stored.get("coordinator_id") is None


def test_status_query_does_not_rewrite_or_erase_launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A read-only status query must not blind-save and erase a launch record.

    Regression for the status/answer lost update: status read `interview`,
    a final answer launched `coord-1`, status saved its stale copy (erasing
    the launch), and a second answer launched `coord-2`. After the fix,
    status holds the job lock, re-reads fresh, and only writes when the job
    actually expired — so a status on a running job performs no write and
    preserves coordinator_id.
    """
    repo = _isolated_env(tmp_path, monkeypatch)
    started = svc.start_interview_job(
        project=repo, request="test request", coordinator="claude:sonnet",
        executors="own", title="status-no-clobber",
    )
    jid = started["job_id"]

    def fake_launch(*_args, **_kwargs):
        return {"id": "coord-1", "pid": 999999}

    with patch.object(maestro, "launch", fake_launch):
        svc.answer_interview_job(project=repo, job_id=jid, key="end_mode", value="pr")
    stored = records.load_job(repo, jid)
    assert stored.get("state") == "running"
    assert stored.get("coordinator_id") == "coord-1"

    before = (repo / "docs" / "crew-chief" / "jobs" / f"{jid}.json").read_bytes()

    real_save = records.save_job
    saves: list[str] = []

    def tracking_save(project, job):
        saves.append(str(job.get("state")))
        return real_save(project, job)

    with patch.object(records, "save_job", tracking_save):
        status = svc.get_interview_status(project=repo, job_id=jid)

    assert status["state"] == "running"
    assert saves == [], f"read-only status must not save a non-expired job, saved states: {saves}"
    after = (repo / "docs" / "crew-chief" / "jobs" / f"{jid}.json").read_bytes()
    assert after == before, "status query rewrote the job file and could erase a launch record"
    stored = records.load_job(repo, jid)
    assert stored.get("state") == "running"
    assert stored.get("coordinator_id") == "coord-1"


def test_status_racing_final_answers_launches_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Status queries racing two final answers must not cause a duplicate launch."""
    repo = _isolated_env(tmp_path, monkeypatch)
    started = svc.start_interview_job(
        project=repo, request="test request", coordinator="claude:sonnet",
        executors="own", title="status-race",
    )
    jid = started["job_id"]
    assert started["pending"] == ["end_mode"]

    calls: list[str] = []
    lock = threading.Lock()

    def fake_launch(*_args, **_kwargs):
        with lock:
            calls.append("launch")
            n = len(calls)
        time.sleep(0.05)
        return {"id": f"coord-{n}", "pid": 999999}

    real_save = records.save_job

    def slow_save(project, job):
        time.sleep(0.1)
        return real_save(project, job)

    start_barrier = threading.Barrier(3)
    errors: list[str] = []
    results: list[dict] = []
    stop = threading.Event()

    def answer_one():
        try:
            start_barrier.wait(timeout=5)
            results.append(svc.answer_interview_job(project=repo, job_id=jid, key="end_mode", value="pr"))
        except Exception as exc:  # pragma: no cover
            errors.append(repr(exc))

    def status_loop():
        try:
            start_barrier.wait(timeout=5)
            while not stop.is_set():
                try:
                    svc.get_interview_status(project=repo, job_id=jid)
                except Exception as exc:  # pragma: no cover
                    errors.append(repr(exc))
                    break
        except Exception as exc:  # pragma: no cover
            errors.append(repr(exc))

    with patch.object(maestro, "launch", fake_launch), patch.object(records, "save_job", slow_save):
        answer_threads = [threading.Thread(target=answer_one) for _ in range(2)]
        status_thread = threading.Thread(target=status_loop)
        for t in [*answer_threads, status_thread]:
            t.start()
        for t in answer_threads:
            t.join(20)
        stop.set()
        status_thread.join(20)

    assert not errors, f"threads raised: {errors}"
    assert len(results) == 2
    assert len(calls) == 1, f"status race launched {len(calls)} coordinators, expected exactly 1"
    stored = records.load_job(repo, jid)
    assert stored.get("state") == "running"
    assert stored.get("coordinator_id") == "coord-1"


def test_launch_passes_the_interview_end_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The interview's end mode reaches the maestro launch."""
    repo = _isolated_env(tmp_path, monkeypatch)
    seen: list[dict] = []

    def fake_launch(*_args, **kwargs):
        seen.append(kwargs)
        return {"id": "coord-1", "pid": 999999}

    with patch.object(maestro, "launch", fake_launch):
        started = svc.start_interview_job(
            project=repo, request="test request", coordinator="claude:sonnet",
            executors="own", end_mode="pr_review", title="end-mode",
        )
    assert started["state"] == "running"
    assert seen[0]["end_mode"] == "pr_review"
    stored = records.load_job(repo, started["job_id"])
    assert stored["runner_pid"] == 999999 and "supervisor" not in stored


def test_status_follows_the_completed_coordinator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A launched interview job reports its maestro coordinator's state and PR."""
    repo = _isolated_env(tmp_path, monkeypatch)
    with patch.object(maestro, "launch", lambda *_a, **_k: {"id": "coord-1", "pid": 999999}):
        started = svc.start_interview_job(
            project=repo, request="test request", coordinator="claude:sonnet",
            executors="own", end_mode="pr", title="status-follows",
        )
    jid = started["job_id"]
    assert svc.get_interview_status(project=repo, job_id=jid)["state"] == "running"

    folder = maestro.coordinators_dir(repo) / "coord-1"
    folder.mkdir(parents=True)
    pr = "https://github.com/example/repo/pull/7"
    (folder / "record.json").write_text(json.dumps({
        "id": "coord-1", "front": "f", "repo": str(repo),
        "worktree": str(tmp_path / "missing-worktree"), "branch": "maestro/f-coord-1",
        "coordinators": ["claude:sonnet"], "index": 0, "state": "completed", "pid": None,
        "created_at": "2026-10-29T10:00:00+00:00", "finished_at": "2026-10-29T10:30:00+00:00",
        "job_id": None, "pr_url": pr, "rounds": [], "last_error": None,
    }), encoding="utf-8")

    status = svc.get_interview_status(project=repo, job_id=jid)
    assert status["state"] == "completed" and status["status"] == "completed"
    assert status["pr_url"] == pr
