"""Tests for delivery commands: accept, close and review."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from crew_chief import records
from crew_chief.cli import main
from crew_chief.worktree import commit_worktree, create_worktree

_STUB_PREFS = """
[[tools]]
name = "stub"
kind = "generic"
bin = "stub"
models = ["m"]
run_cmd = ["stub", "{prompt}"]

[[models]]
match = "stub:*"
cost = "free"
"""

_STUB = (
    "import os\n"
    "from pathlib import Path\n"
    "(Path(os.getcwd()) / 'out.py').write_text('x = 1\\n', encoding='utf-8')\n"
    "print('done')\n"
)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout


def _write_prefs(repo: Path, text: str) -> None:
    path = repo / "docs" / "crew-chief" / "preferences.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _stored_job(repo: Path, job_id: str) -> dict:
    path = records.jobs_dir(repo) / f"{job_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _make_readonly(path: Path) -> None:
    """Make a file read-only so git worktree remove fails on it."""
    if path.is_dir():
        return
    current = path.stat().st_mode
    path.chmod(current & ~(stat.S_IWRITE | stat.S_IWGRP | stat.S_IWOTH))


class TestAcceptWorktreeRemovalFailure:
    """Issue #3: accept must leave piece ACCEPTED when worktree removal fails,
    record cleanup pending, and retry cleanup idempotently."""

    def test_accept_succeeds_merge_but_warns_on_worktree_removal_failure(
        self, git_repo, fake_bin, monkeypatch, capsys, tmp_path
    ):
        """When worktree removal is forced to fail, accept must:
        1. Successfully merge the branch
        2. Mark the piece as accepted
        3. Warn about cleanup pending
        4. Not repeat merge/push/notices on retry
        """
        repo = git_repo
        _write_prefs(repo, _STUB_PREFS)
        fake_bin("stub", _STUB)

        # Create a job with one piece
        request = repo / "request.md"
        request.write_text("Create out.py.\n", encoding="utf-8")
        brief = repo / "brief.md"
        brief.write_text("Create out.py.\n", encoding="utf-8")

        assert main(["job", "new", "--project", str(repo), "--title", "Add out.py",
                     "--request-file", str(request)]) == 0
        job_id = capsys.readouterr().out.strip()

        assert main(["piece", "add", "--project", str(repo), "--job", job_id,
                     "--title", "Write out.py", "--type", "code", "--risk", "low"]) == 0

        assert main(["pick", "--project", str(repo), "--job", job_id,
                     "--piece", "1"]) == 0
        capsys.readouterr()

        assert main(["dispatch", "--project", str(repo), "--job", job_id,
                     "--piece", "1", "--executor", "stub:m",
                     "--brief-file", str(brief)]) == 0
        capsys.readouterr()

        assert main(["grade", "--project", str(repo), "--job", job_id,
                     "--piece", "1", "--executor", "stub:m", "--corrections", "0",
                     "--done", "3", "--missing", "0", "--extra", "0"]) == 0
        capsys.readouterr()

        # Get the worktree path from the job record
        job = _stored_job(repo, job_id)
        attempt = job["pieces"][0]["attempts"][-1]
        worktree_path = Path(attempt["worktree"])

        # Spy on merges, pushes, and notices
        merge_calls = []
        push_calls = []
        notice_calls = []

        import crew_chief.webhook as webhook_mod
        real_send_notice = webhook_mod.send_webhook_notice
        def spy_notice(*a, **kw):
            notice_calls.append((a, kw))
            return real_send_notice(*a, **kw)
        monkeypatch.setattr(webhook_mod, "send_webhook_notice", spy_notice)

        from crew_chief import git_delivery
        real_push = git_delivery.push_to_origin
        def spy_push(*a, **kw):
            push_calls.append((a, kw))
            return real_push(*a, **kw)
        monkeypatch.setattr(git_delivery, "push_to_origin", spy_push)
        from crew_chief import cli
        monkeypatch.setattr(cli, "push_to_origin", spy_push)

        real_run = subprocess.run
        def spy_run(cmd, *a, **kw):
            if isinstance(cmd, (list, tuple)) and len(cmd) > 1 and cmd[0] == "git" and cmd[1] == "merge":
                merge_calls.append(cmd)
            return real_run(cmd, *a, **kw)
        monkeypatch.setattr(subprocess, "run", spy_run)

        # Patch _remove_worktree to simulate removal failure after merge succeeds
        from unittest.mock import patch
        from crew_chief import worktree as wt_module

        original_remove_worktree = wt_module._remove_worktree

        def failing_remove_worktree(project: Path, worktree: Path) -> None:
            # Let the merge happen, but fail when trying to remove the worktree
            raise RuntimeError(f"Simulated worktree removal failure for {worktree}")

        with patch.object(wt_module, "_remove_worktree", failing_remove_worktree):
            # First accept should merge successfully but warn about cleanup
            result = main(["accept", "--project", str(repo), "--job", job_id,
                          "--piece", "1"])

        # The accept should succeed (merge worked) but warn
        assert result == 0
        output = capsys.readouterr()
        assert "cleanup pending" in output.err.lower(), f"Expected warning in stderr, got: out={output.out!r}, err={output.err!r}"

        # Verify the piece is marked as accepted
        job = _stored_job(repo, job_id)
        piece = job["pieces"][0]
        assert piece.get("accepted"), "Piece should be marked as accepted despite worktree removal failure"
        assert piece["accepted"]["attempt"] == 1

        # Verify the branch was merged (out.py should exist in repo)
        assert (repo / "out.py").exists(), "Merged file should exist in repo"
        assert worktree_path.exists(), "Worktree should still exist after cleanup failure"

        # Reset spy counters before the retry calls
        merge_calls.clear()
        push_calls.clear()
        notice_calls.clear()

        # Retry accept with failure: cleanup fails again, warns, and does not merge/push/notify
        with patch.object(wt_module, "_remove_worktree", failing_remove_worktree):
            result_retry_fail = main(["accept", "--project", str(repo), "--job", job_id,
                                      "--piece", "1"])
        assert result_retry_fail == 0
        output_fail = capsys.readouterr()
        assert "cleanup pending" in output_fail.err.lower()
        assert worktree_path.exists(), "Worktree should still exist after failed retry"
        assert len(merge_calls) == 0, f"Expected 0 git merges on failed retry, got {len(merge_calls)}"
        assert len(push_calls) == 0, f"Expected 0 git pushes on failed retry, got {len(push_calls)}"
        assert len(notice_calls) == 0, f"Expected 0 notices on failed retry, got {len(notice_calls)}"

        # Later accept/cleanup removes the worktree only - no merge, push, or notices
        result2 = main(["accept", "--project", str(repo), "--job", job_id,
                       "--piece", "1"])

        assert result2 == 0
        assert not worktree_path.exists(), "Second accept must remove the worktree"
        assert len(merge_calls) == 0, f"Expected 0 git merges on second call, got {len(merge_calls)}: {merge_calls}"
        assert len(push_calls) == 0, f"Expected 0 git pushes on second call, got {len(push_calls)}: {push_calls}"
        assert len(notice_calls) == 0, f"Expected 0 notices on second call, got {len(notice_calls)}: {notice_calls}"

        # Piece should still be marked as accepted
        job = _stored_job(repo, job_id)
        piece = job["pieces"][0]
        assert piece.get("accepted"), "Piece should remain accepted"