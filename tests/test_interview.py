"""Tests for the interview engine (T006, US2, FR-011)."""

from datetime import datetime, timedelta, timezone

from crew_chief.interview import (
    InterviewEngine,
    InterviewExpiredError,
    InterviewPendingError,
    check_interview_expiration,
    check_interview_gate,
)
import json
from pathlib import Path
import pytest
from crew_chief import records, spend
from crew_chief.config import Prefs
from crew_chief.dispatch import dispatch
from crew_chief.tools import Executor


def test_question_order_project_request_coordinator_executors_endmode():
    """Questions must be yielded in exact order: project, request, coordinator, executors, end_mode."""
    engine = InterviewEngine()
    keys = [q.key for q in engine.questions()]
    assert keys == ["project", "request", "coordinator", "executors", "end_mode"]


def test_skips_questions_with_project_or_global_prefs():
    """Project and global prefs skip questions; built-in defaults do not. Source is recorded."""
    project_prefs = {"coordinator": "alpha"}
    global_prefs = {"executors": "pool-x"}
    engine = InterviewEngine(
        project_prefs=project_prefs,
        global_prefs=global_prefs,
    )
    record = {}
    asked = []
    for question in engine.questions():
        if question.skip_when and question.skip_when():
            record[question.key] = {
                "value": question.resolved_value,
                "source": question.resolved_source,
            }
        else:
            asked.append(question.key)
            record[question.key] = {"value": None, "source": "user"}

    # coordinator resolved from project prefs
    assert record["coordinator"]["value"] == "alpha"
    assert record["coordinator"]["source"] == "project"
    # executors resolved from global prefs
    assert record["executors"]["value"] == "pool-x"
    assert record["executors"]["source"] == "global"
    # project and request have no pref -> must be asked
    assert "project" in asked
    assert "request" in asked
    # end_mode has a built-in default but no pref -> must still be asked
    assert "end_mode" in asked
    assert record["end_mode"]["source"] == "user"


def _tool(name: str = "stub") -> dict:
    return {
        "name": name,
        "kind": "generic",
        "bin": name,
        "run_cmd": [name, "{prompt}"],
        "models": ["m"],
    }


def _executor(name: str = "stub", cost: str = "free") -> Executor:
    return Executor(
        id=f"{name}:m", tool=_tool(name), model="m", cost=cost, trains=False
    )


def test_no_worktree_or_budget_while_unanswered(git_repo, fake_bin, tmp_path):
    """Refuse dispatch when interview questions are unanswered: no worktree, process or spend (T008, FR-013)."""
    marker = tmp_path / "process_spawned.txt"
    fake_bin("stub", f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n")

    executor = _executor("stub", cost="paid")

    job = records.new_job(git_repo, "Test interview gating", "do it", True, "testing")
    records.add_piece(job, "Piece 1", "code", "low")
    job["state"] = "interview"
    job["interview"] = {
        "answers": {
            "project": "my-project",
        }
    }
    records.save_job(git_repo, job)

    prefs = Prefs(
        tools=[],
        models=[],
        strong_order=[],
        explore_rate=0.1,
        forbid_training=False,
        sensitive_paths=[],
        issue_tracker="none",
        dispatch_timeout_s=900,
        review_default="",
    )

    with pytest.raises(InterviewPendingError, match="interview"):
        dispatch(git_repo, job["id"], 1, executor, "go\n", prefs)

    # 1. No process spawned
    assert not marker.exists(), "Process must not be spawned while questions remain unanswered"

    # 2. No worktree created
    worktree_crew_dir = git_repo.parent / f"{git_repo.name}.crew"
    assert not worktree_crew_dir.exists(), "No worktree must be created"

    # 3. No budget/tokens consumed
    spend_file = spend.spend_file()
    if spend_file.exists():
        spend_content = spend_file.read_text(encoding="utf-8").strip()
        assert job["id"] not in spend_content, "No spend must be recorded for this job"

    # 4. No attempts recorded on job
    stored = records.load_job(git_repo, job["id"])
    assert stored["pieces"][0]["attempts"] == [], "No attempts must be recorded"

    # Now provide answers for all remaining questions and complete interview
    job["interview"]["answers"].update({
        "request": "do it",
        "coordinator": "coord-a",
        "executors": "own",
        "end_mode": "pr",
    })
    job["state"] = "running"
    records.save_job(git_repo, job)

    attempt = dispatch(git_repo, job["id"], 1, executor, "go\n", prefs)
    assert marker.exists(), "Process must run after all questions are answered"
    assert attempt["worktree"] is not None
    assert worktree_crew_dir.exists()


def test_interview_engine_is_complete_and_pending_questions():
    """Engine checks completeness; built-in defaults never count as answered."""
    engine = InterviewEngine(
        project_prefs={"coordinator": "alpha"},
        global_prefs={"executors": "pool-x"},
    )
    # coordinator and executors are skipped by prefs; project, request, end_mode are pending
    pending = [q.key for q in engine.pending_questions({})]
    assert pending == ["project", "request", "end_mode"]
    assert not engine.is_complete({})

    with pytest.raises(InterviewPendingError, match="project, request, end_mode"):
        engine.check_gate({})

    # Partially answered
    partial_answers = {"project": "my-proj"}
    pending = [q.key for q in engine.pending_questions(partial_answers)]
    assert pending == ["request", "end_mode"]
    assert not engine.is_complete(partial_answers)

    # Fully answered (including end_mode explicitly answered, not defaulting)
    full_answers = {
        "project": "my-proj",
        "request": "implement feature",
        "end_mode": {"value": "pr", "source": "user"},
    }
    assert engine.pending_questions(full_answers) == []
    assert engine.is_complete(full_answers)
    engine.check_gate(full_answers)  # Must not raise


def test_dispatch_allowed_when_interview_questions_skipped_by_prefs(git_repo, fake_bin):
    """Interview allows dispatch when unanswered keys are covered by preferences."""
    fake_bin("stub", "print('ok')\n")
    executor = _executor("stub", cost="free")

    job = records.new_job(git_repo, "Test pref skipping dispatch", "do it", True, "testing")
    records.add_piece(job, "Piece 1", "code", "low")
    job["state"] = "interview"
    job["interview"] = {
        "answers": {
            "project": "my-project",
            "request": "do it",
            "end_mode": "pr",
        }
    }
    job["project_prefs"] = {"coordinator": "alpha"}
    job["global_prefs"] = {"executors": "own"}
    records.save_job(git_repo, job)

    prefs = Prefs(
        tools=[],
        models=[],
        strong_order=[],
        explore_rate=0.1,
        forbid_training=False,
        sensitive_paths=[],
        issue_tracker="none",
        dispatch_timeout_s=900,
        review_default="",
    )

    # Dispatch succeeds because coordinator and executors are skipped by preferences
    attempt = dispatch(git_repo, job["id"], 1, executor, "go\n", prefs)
    assert attempt["status"] in ("ok", "no_change", "failed")


def test_expires_after_24h_default():
    """Interview expires after default 24h timeout; state transitions and event emitted (T009, FR-014)."""
    now = datetime.now(timezone.utc)
    expired_started = (now - timedelta(hours=25)).isoformat()
    fresh_started = (now - timedelta(hours=23)).isoformat()

    # --- Expired case: 25 hours ago exceeds default 24h timeout ---
    expired_job: dict[str, Any] = {
        "state": "interview",
        "interview": {
            "started_at": expired_started,
            "answers": {
                "project": "my-project",
                "request": "do it",
            },
        },
    }

    with pytest.raises(InterviewExpiredError):
        check_interview_gate(expired_job)

    assert expired_job["state"] == "expired"
    events = expired_job.get("events")
    assert isinstance(events, list) and len(events) >= 1
    expire_events = [e for e in events if e.get("type") == "interview_expired"]
    assert expire_events, "Expected an interview_expired lifecycle event"
    assert "at" in expire_events[0]

    # --- Non-expired case: 23 hours ago must NOT expire ---
    fresh_job: dict[str, Any] = {
        "state": "interview",
        "interview": {
            "started_at": fresh_started,
            "answers": {
                "project": "my-project",
                "request": "do it",
                "coordinator": "alpha",
                "executors": "own",
                "end_mode": "pr",
            },
        },
    }

    # Should not raise InterviewExpiredError; gate passes because all questions answered
    check_interview_gate(fresh_job)
    assert fresh_job["state"] != "expired"


def test_finished_or_running_job_does_not_expire(tmp_path: Path):
    """Expiry applies only while the interview is still open; finished/running jobs never expire (P2)."""
    from crew_chief import interview_service

    now = datetime.now(timezone.utc)
    old_started = (now - timedelta(hours=25)).isoformat()

    running_job: dict[str, Any] = {
        "id": "job-running-test",
        "state": "running",
        "status": "running",
        "interview": {
            "started_at": old_started,
            "answers": {
                "project": "my-project",
                "request": "do it",
                "coordinator": "alpha",
                "executors": "own",
                "end_mode": "pr",
            },
            "pending": [],
        },
    }

    assert not check_interview_expiration(running_job)
    assert running_job["state"] == "running"
    assert running_job.get("status") == "running"


