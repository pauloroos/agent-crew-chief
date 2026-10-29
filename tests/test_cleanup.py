"""T034 — Worktree cleanup with archive (US5, FR-026).

Unmerged attempt worktrees are bundled into a git bundle + unified diff in the
central archive before directory removal; active attempts and unpushed branches
remain untouched. Archive failure keeps the worktree/branch intact.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure the package is on the path for isolated test runs.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skill" / "crew-chief" / "scripts"))

from crew_chief import (
    central_messages,
    central_registry,
    central_supervisor,
    cleanup,
    records,
    worktree as cc_worktree,
)


class FakeClock:
    """Epoch seconds that only advance when asked."""

    def __init__(self) -> None:
        self.value = 1_700_000_000.0

    def time(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


def _git(repo: Path, *args: str) -> str:
    """Run ``git <args>`` in ``repo`` and return stdout."""
    result = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _init_bare_remote(tmp: Path) -> Path:
    """Create a bare git repo to act as a remote origin."""
    bare = tmp / "bare.git"
    bare.mkdir()
    _git(bare, "init", "--bare")
    return bare


def _init_worktree_repo(tmp: Path, bare: Path) -> Path:
    """Clone from bare, make an initial commit, push to set up main."""
    clone = tmp / "clone"
    clone.mkdir()
    _git(clone, "clone", str(bare), ".")
    _git(clone, "config", "user.email", "test@example.com")
    _git(clone, "config", "user.name", "Test User")
    (clone / "README.md").write_text("# Test\n")
    _git(clone, "add", ".")
    _git(clone, "commit", "-m", "initial commit")
    _git(clone, "branch", "-M", "main")
    _git(clone, "push", "-u", "origin", "main")
    return clone


def _create_attempt_worktree(clone: Path, branch_name: str) -> Path:
    """Create a worktree for an attempt branch with some changes."""
    safe_name = branch_name.replace("/", "-")
    worktree_path = clone.parent / f"attempt-{safe_name}"
    _git(clone, "worktree", "add", "-b", branch_name, str(worktree_path))
    (worktree_path / "feature.txt").write_text(f"Feature from {branch_name}\n")
    _git(worktree_path, "add", ".")
    _git(worktree_path, "commit", "-m", f"Work on {branch_name}")
    return worktree_path


def _push_branch(clone: Path, branch: str) -> None:
    """Push a branch to origin."""
    _git(clone, "push", "origin", branch)


def _make_temp_dir(prefix: str = "cleanup-test-") -> Path:
    """Create a temporary directory that persists for the test."""
    return Path(tempfile.mkdtemp(prefix=prefix))


def test_cleanup_with_real_records_and_archive() -> None:
    """Test cleanup using real records API, bare remote, and unmerged attempt worktrees.

    - Pushed + inactive + unmerged -> archived (.bundle + .diff) and worktree directory removed.
    - git worktree list does NOT contain removed worktree (canonical removal).
    - Branch ref is NOT deleted.
    - Unpushed branch/worktree is UNTOUCHED.
    - Active attempt is UNTOUCHED.
    - Accepted attempt is UNTOUCHED.
    - Uncommitted changes in worktree are included in diff and bundle.
    - Bundle can be verified, listed, and restored.
    - Idempotent execution.
    """
    tmp_base = _make_temp_dir()
    try:
        bare = _init_bare_remote(tmp_base)
        clone = _init_worktree_repo(tmp_base, bare)

        # Linked worktree for coordinator where .git is a FILE
        coord_wt = tmp_base / "coord_wt"
        _git(clone, "worktree", "add", "-b", "coord-branch", str(coord_wt), "main")
        assert (coord_wt / ".git").is_file(), "Linked worktree .git must be a file"

        # Create attempts
        branch_pushed_failed = "crew/job1-piece-1"
        branch_unpushed_failed = "crew/job1-piece-2"
        branch_active = "crew/job1-piece-3"
        branch_accepted = "crew/job1-piece-4"

        wt_pushed = _create_attempt_worktree(clone, branch_pushed_failed)
        wt_unpushed = _create_attempt_worktree(clone, branch_unpushed_failed)
        wt_active = _create_attempt_worktree(clone, branch_active)
        wt_accepted = _create_attempt_worktree(clone, branch_accepted)

        # Push branch_pushed_failed, branch_active, branch_accepted to origin
        _push_branch(clone, branch_pushed_failed)
        _push_branch(clone, branch_active)
        _push_branch(clone, branch_accepted)
        # wt_unpushed is deliberately NOT pushed

        # Add uncommitted work to wt_pushed to ensure it is archived in diff/bundle
        (wt_pushed / "uncommitted.txt").write_text("uncommitted work in attempt 1\n")

        # Create real job record in coord_wt using records API
        job = records.new_job(
            project=coord_wt,
            title="test-cleanup-job",
            request="implement cleanup",
            distributed=False,
            reason="testing",
        )
        p1 = records.add_piece(job, "piece 1", "code", "low")
        p2 = records.add_piece(job, "piece 2", "code", "low")
        p3 = records.add_piece(job, "piece 3", "code", "low")
        p4 = records.add_piece(job, "piece 4", "code", "low")

        att_pushed = {
            "n": 1,
            "role": "exec",
            "status": "failed",
            "branch": branch_pushed_failed,
            "worktree": str(wt_pushed),
        }
        att_unpushed = {
            "n": 1,
            "role": "exec",
            "status": "failed",
            "branch": branch_unpushed_failed,
            "worktree": str(wt_unpushed),
        }
        att_active = {
            "n": 1,
            "role": "exec",
            "status": "running",
            "branch": branch_active,
            "worktree": str(wt_active),
        }
        att_accepted = {
            "n": 1,
            "role": "exec",
            "status": "ok",
            "branch": branch_accepted,
            "worktree": str(wt_accepted),
        }
        att_missing = {
            "n": 2,
            "role": "exec",
            "status": "failed",
            "branch": "crew/job1-piece-missing",
            "worktree": str(tmp_base / "nonexistent-wt"),
        }

        records.add_attempt(job, p1, att_pushed)
        records.add_attempt(job, p2, att_unpushed)
        records.add_attempt(job, p3, att_active)
        records.add_attempt(job, p4, att_accepted)
        records.mark_accepted(job, p4, att_accepted)
        records.add_attempt(job, p1, att_missing)
        records.save_job(coord_wt, job)

        archive_dir = tmp_base / "archive"
        clock = FakeClock()

        # Execute cleanup
        result = cleanup.cleanup_completed_attempts(
            project=coord_wt,
            archive_dir=archive_dir,
            job_ids=[job["id"]],
            clock=clock,
        )

        assert result["archived"] == 1
        assert result["errors"] == 0
        assert result["skipped"] >= 3

        # 1. Check pushed failed attempt worktree is removed
        assert not wt_pushed.exists(), f"Worktree {wt_pushed} should be removed"

        # Check git worktree list no longer lists wt_pushed (canonical removal)
        wt_list = _git(clone, "worktree", "list")
        assert str(wt_pushed) not in wt_list, "wt_pushed must not remain in git worktree list"

        # Check branch ref is NOT deleted
        branch_list = _git(clone, "branch", "--list")
        assert branch_pushed_failed in branch_list, "branch must NOT be deleted with branch -D"

        # 2. Check bundle was created, verified, listed, and can be restored
        safe_branch1 = branch_pushed_failed.replace("/", "_")
        bundle_path = archive_dir / f"{safe_branch1}.bundle"
        assert bundle_path.exists(), f"Bundle {bundle_path} must exist"

        # Verify bundle
        _git(clone, "bundle", "verify", str(bundle_path))
        heads = _git(clone, "bundle", "list-heads", str(bundle_path))
        assert branch_pushed_failed in heads

        # Restore from bundle
        restore_dir = tmp_base / "restored_attempt_1"
        _git(clone, "clone", "-b", branch_pushed_failed, str(bundle_path), str(restore_dir))
        assert (restore_dir / "feature.txt").exists()
        assert (restore_dir / "uncommitted.txt").exists(), "Uncommitted work must be in bundle"

        # 3. Check diff was created and contains changes including uncommitted work
        diff_path = archive_dir / f"{safe_branch1}.diff"
        assert diff_path.exists(), f"Diff {diff_path} must exist"
        diff_content = diff_path.read_text(encoding="utf-8")
        assert f"Feature from {branch_pushed_failed}" in diff_content
        assert "uncommitted work in attempt 1" in diff_content

        # 4. Check untouched worktrees
        assert wt_unpushed.exists(), "Unpushed worktree must be untouched"
        assert wt_active.exists(), "Active worktree must be untouched"
        assert wt_accepted.exists(), "Accepted worktree must be untouched"

        # 5. Safe idempotence: running cleanup again is a no-op
        second_result = cleanup.cleanup_completed_attempts(
            project=coord_wt,
            archive_dir=archive_dir,
            job_ids=[job["id"]],
            clock=clock,
        )
        assert second_result["archived"] == 0
        assert second_result["errors"] == 0

    finally:
        shutil.rmtree(tmp_base, ignore_errors=True)


def test_archive_failure_preserves_worktree_and_cleans_partials() -> None:
    """When bundle creation or diff creation fails, worktree and branch are preserved, and partials removed."""
    tmp_base = _make_temp_dir()
    try:
        bare = _init_bare_remote(tmp_base)
        clone = _init_worktree_repo(tmp_base, bare)
        branch = "crew/job-fail-1"
        wt = _create_attempt_worktree(clone, branch)
        _push_branch(clone, branch)

        job = records.new_job(
            project=clone,
            title="job-fail",
            request="test failure",
            distributed=False,
            reason="testing",
        )
        p = records.add_piece(job, "piece 1", "code", "low")
        att = {
            "n": 1,
            "role": "exec",
            "status": "failed",
            "branch": branch,
            "worktree": str(wt),
        }
        records.add_attempt(job, p, att)
        records.save_job(clone, job)

        archive_dir = tmp_base / "archive"

        # Simulate failure during bundle creation
        with patch.object(cleanup, "_bundle_branch", side_effect=RuntimeError("bundle crash")):
            result = cleanup.cleanup_completed_attempts(
                project=clone,
                archive_dir=archive_dir,
                job_ids=[job["id"]],
                clock=FakeClock(),
            )

        assert result["errors"] == 1
        assert result["archived"] == 0
        assert wt.exists(), "Worktree must be preserved on archive failure"
        branch_list = _git(clone, "branch", "--list")
        assert branch in branch_list, "Branch must be preserved on archive failure"

        # Check partial files were not left behind
        safe_branch = branch.replace("/", "_")
        assert not (archive_dir / f"{safe_branch}.bundle").exists()
        assert not (archive_dir / f"{safe_branch}.diff").exists()

    finally:
        shutil.rmtree(tmp_base, ignore_errors=True)


def test_archive_commit_failure_preserves_worktree_and_reports_error() -> None:
    """When commit_worktree fails (e.g. hook rejects commit), the worktree is preserved and error is reported."""
    tmp_base = _make_temp_dir()
    try:
        bare = _init_bare_remote(tmp_base)
        clone = _init_worktree_repo(tmp_base, bare)
        branch = "crew/job-commit-fail-1"
        wt = _create_attempt_worktree(clone, branch)
        _push_branch(clone, branch)

        # Add uncommitted work to wt
        pending_file = wt / "pending.txt"
        pending_file.write_text("pending uncommitted work\n")

        job = records.new_job(
            project=clone,
            title="job-commit-fail",
            request="test commit failure",
            distributed=False,
            reason="testing",
        )
        p = records.add_piece(job, "piece 1", "code", "low")
        att = {
            "n": 1,
            "role": "exec",
            "status": "failed",
            "branch": branch,
            "worktree": str(wt),
        }
        records.add_attempt(job, p, att)
        records.save_job(clone, job)

        archive_dir = tmp_base / "archive"

        with patch.object(
            cleanup.worktree,
            "commit_worktree",
            side_effect=RuntimeError("git commit failed: hook rejected commit"),
        ):
            result = cleanup.cleanup_completed_attempts(
                project=clone,
                archive_dir=archive_dir,
                job_ids=[job["id"]],
                clock=FakeClock(),
            )

        assert result["errors"] == 1
        assert result["archived"] == 0
        assert wt.exists(), "Worktree must be preserved when commit_worktree fails"
        assert pending_file.exists(), "Pending uncommitted file must be preserved"
        assert pending_file.read_text() == "pending uncommitted work\n"

        # Check bundle and diff were not created
        safe_branch = branch.replace("/", "_")
        assert not (archive_dir / f"{safe_branch}.bundle").exists()
        assert not (archive_dir / f"{safe_branch}.diff").exists()

    finally:
        shutil.rmtree(tmp_base, ignore_errors=True)


def _real_subprocess_run(cmd, *args, **kwargs):
    import subprocess
    return subprocess.run(cmd, *args, **kwargs)


def test_supervise_terminal_cleanup_with_real_linked_worktree() -> None:
    """supervise() in terminal state runs cleanup on real linked git worktree and records."""
    tmp_base = _make_temp_dir()
    try:
        bare = _init_bare_remote(tmp_base)
        clone = _init_worktree_repo(tmp_base, bare)

        # Linked worktree for coordinator
        coord_wt = tmp_base / "coord_wt"
        _git(clone, "worktree", "add", "-b", "coord-branch", str(coord_wt), "main")

        # Attempt worktree
        branch = "crew/coord-attempt-1"
        wt = _create_attempt_worktree(clone, branch)
        _push_branch(clone, branch)

        # Job record in coord_wt
        job = records.new_job(
            project=coord_wt,
            title="supervisor-cleanup-job",
            request="test supervisor",
            distributed=False,
            reason="testing",
        )
        p = records.add_piece(job, "piece 1", "code", "low")
        att = {
            "n": 1,
            "role": "exec",
            "status": "failed",
            "branch": branch,
            "worktree": str(wt),
        }
        records.add_attempt(job, p, att)
        records.save_job(coord_wt, job)

        # Prepare supervisor setup
        root = tmp_base / "central"
        cid = str(uuid.uuid4())
        record = {
            "schema_version": 1,
            "id": cid,
            "repo": str(clone),
            "worktree": str(coord_wt),
            "branch": f"central/{cid}",
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
            "job_ids": [job["id"]],
            "active_job_id": None,
            "state": "idle",
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
            "last_error": None,
            "supervisor_nonce": None,
        }
        central_registry.register(root, record)
        coord_dir = central_messages._coordinator_dir(root, cid, create=True)
        (coord_dir / "request.md").write_bytes(b"Run task\n")

        clock = FakeClock()

        # Runner that immediately reports completed
        def runner(rec: dict, timeout_s: float) -> dict:
            return {
                "ok": True,
                "record": {**rec, "state": "completed"},
                "round": {
                    "checkpoint": {"state": "completed", "job_ids": [job["id"]]},
                    "result": {"questions": [], "answers": []},
                },
                "round_id": "round-1",
            }

        # Send a message to wake supervisor from idle
        central_messages.send(root, cid, {
            "schema_version": 1,
            "id": str(uuid.uuid4()),
            "coordinator_id": cid,
            "direction": "in",
            "kind": "message",
            "created_at": "2026-01-01T00:00:00+00:00",
            "reply_to": None,
            "task_refs": [],
            "body": "go",
        })

        central_supervisor.supervise(root, cid, clock, runner)

        # Check supervisor state is completed
        stored = central_registry.load_registry(root)["coordinators"][cid]
        assert stored["state"] == "completed"

        # Check archive files exist
        archive_dir = coord_dir / "archive"
        safe_branch = branch.replace("/", "_")
        assert (archive_dir / f"{safe_branch}.bundle").exists()
        assert (archive_dir / f"{safe_branch}.diff").exists()

        # Check attempt worktree removed
        assert not wt.exists()
        assert str(wt) not in _git(clone, "worktree", "list")

    finally:
        shutil.rmtree(tmp_base, ignore_errors=True)


def test_supervise_cleanup_failure_preserves_terminal_state_and_logs_note() -> None:
    """When cleanup encounters an exception in supervise(), the terminal state is preserved and a note is recorded."""
    tmp_base = _make_temp_dir()
    try:
        bare = _init_bare_remote(tmp_base)
        clone = _init_worktree_repo(tmp_base, bare)

        coord_wt = tmp_base / "coord_wt"
        _git(clone, "worktree", "add", "-b", "coord-branch", str(coord_wt), "main")

        branch = "crew/coord-attempt-fail"
        wt = _create_attempt_worktree(clone, branch)
        _push_branch(clone, branch)

        job = records.new_job(
            project=coord_wt,
            title="supervisor-cleanup-fail-job",
            request="test supervisor fail",
            distributed=False,
            reason="testing",
        )
        p = records.add_piece(job, "piece 1", "code", "low")
        att = {
            "n": 1,
            "role": "exec",
            "status": "failed",
            "branch": branch,
            "worktree": str(wt),
        }
        records.add_attempt(job, p, att)
        records.save_job(coord_wt, job)

        root = tmp_base / "central"
        cid = str(uuid.uuid4())
        record = {
            "schema_version": 1,
            "id": cid,
            "repo": str(clone),
            "worktree": str(coord_wt),
            "branch": f"central/{cid}",
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
            "job_ids": [job["id"]],
            "active_job_id": None,
            "state": "idle",
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
            "last_error": None,
            "supervisor_nonce": None,
        }
        central_registry.register(root, record)
        coord_dir = central_messages._coordinator_dir(root, cid, create=True)
        (coord_dir / "request.md").write_bytes(b"Run task\n")

        clock = FakeClock()

        def runner(rec: dict, timeout_s: float) -> dict:
            return {
                "ok": True,
                "record": {**rec, "state": "completed"},
                "round": {
                    "checkpoint": {"state": "completed", "job_ids": [job["id"]]},
                    "result": {"questions": [], "answers": []},
                },
                "round_id": "round-1",
            }

        central_messages.send(root, cid, {
            "schema_version": 1,
            "id": str(uuid.uuid4()),
            "coordinator_id": cid,
            "direction": "in",
            "kind": "message",
            "created_at": "2026-01-01T00:00:00+00:00",
            "reply_to": None,
            "task_refs": [],
            "body": "go",
        })

        with patch.object(cleanup, "cleanup_completed_attempts", side_effect=RuntimeError("disk full")):
            central_supervisor.supervise(root, cid, clock, runner)

        # Terminal state must still be completed
        stored = central_registry.load_registry(root)["coordinators"][cid]
        assert stored["state"] == "completed"

        # Supervisor status file must contain an event/note about the cleanup failure
        status_file = coord_dir / "supervisor.json"
        assert status_file.exists()
        status_data = json.loads(status_file.read_text(encoding="utf-8"))
        events = status_data.get("events", [])
        cleanup_notes = [
            e for e in events
            if "cleanup" in str(e.get("note", "")).lower() or "disk full" in str(e.get("note", "")).lower()
        ]
        assert len(cleanup_notes) > 0, f"Expected cleanup failure event in events, got: {events}"

    finally:
        shutil.rmtree(tmp_base, ignore_errors=True)
