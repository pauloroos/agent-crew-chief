"""Tests for FR-044: economy reports executor mode and flags coordinator quota."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from crew_chief import records
from crew_chief.economy import job_economy, summary_line

NOW = datetime(2026, 10, 26, 12, 0, 0, tzinfo=timezone.utc)


def _job_with_executors(executors=None, usage_tokens=0):
    """Minimal job with optional executors field and coordinator usage."""
    created = NOW - timedelta(minutes=30)
    job = {
        "id": "j",
        "title": "T",
        "request": "r",
        "distributed": True,
        "reason": "",
        "created": created.isoformat(),
        "pieces": [],
    }
    if executors is not None:
        job["executors"] = executors
    records.add_piece(job, "One", "code", "low")
    records.add_attempt(
        job,
        1,
        {
            "n": 1,
            "role": "exec",
            "status": "ok",
            "executor": "opencode:a",
            "cost": "free",
            "started": (created + timedelta(minutes=1)).isoformat(),
            "duration_s": 60.0,
            "tokens_in": 900,
            "tokens_out": 100,
            "tokens_total": 1000,
            "tokens_estimated": False,
            "branch": "b1",
        },
    )
    records.mark_accepted(job, 1, {"n": 1, "branch": "b1"})
    if usage_tokens > 0:
        records.add_usage(job, "coordinator", usage_tokens, 0, False)
    return job


def test_legacy_job_defaults_to_free_first():
    job = _job_with_executors()
    e = job_economy(job, None, now=NOW)
    assert e.executors == "free_first"
    assert e.alerts == []


def test_own_mode_is_reported_and_flags_coordinator_quota():
    job = _job_with_executors(executors="own", usage_tokens=500)
    e = job_economy(job, None, now=NOW)
    assert e.executors == "own"
    assert any("executor mode own" in a for a in e.alerts)
    assert any("500 tokens" in a for a in e.alerts)


def test_subagents_mode_is_reported_and_flags_coordinator_quota():
    job = _job_with_executors(executors="subagents", usage_tokens=200)
    e = job_economy(job, None, now=NOW)
    assert e.executors == "subagents"
    assert any("executor mode subagents" in a for a in e.alerts)


def test_free_first_mode_does_not_flag_coordinator_quota():
    job = _job_with_executors(executors="free_first", usage_tokens=500)
    e = job_economy(job, None, now=NOW)
    assert e.executors == "free_first"
    assert not any("executor mode" in a for a in e.alerts)


def test_invalid_executors_value_falls_back_to_free_first():
    job = _job_with_executors(executors="bogus", usage_tokens=100)
    e = job_economy(job, None, now=NOW)
    assert e.executors == "free_first"
    assert not any("executor mode" in a for a in e.alerts)


def test_own_mode_without_coordinator_usage_does_not_flag():
    job = _job_with_executors(executors="own", usage_tokens=0)
    e = job_economy(job, None, now=NOW)
    assert e.executors == "own"
    assert not any("executor mode" in a for a in e.alerts)


def test_summary_line_unchanged_by_executor_mode():
    """Existing summary line format stays backward-compatible."""
    job = _job_with_executors(executors="own", usage_tokens=500)
    line = summary_line(job_economy(job, None, now=NOW))
    assert "free work:" in line
    assert "coordinator:" in line