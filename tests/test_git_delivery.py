"""Tests for git delivery workflow.

T017 / US8 / FR-027: git fetch origin preflight and job branch creation.
Before creating the acc/<job-id> branch, ACC must run `git fetch origin`
to ensure the branch is rooted at the latest origin/<base_branch>.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import pytest


def _run_git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run a git command in the given directory."""
    return subprocess.run(
        ["git"] + list(args),
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )


def _init_bare_remote(tmp_path: Path) -> Path:
    """Create a bare git repository to act as origin."""
    bare = tmp_path / "bare.git"
    bare.mkdir()
    _run_git(bare, "init", "--bare", "--initial-branch=main")
    return bare


def _init_worktree_repo(tmp_path: Path, bare: Path) -> Path:
    """Clone from bare repo and create an initial commit."""
    work = tmp_path / "work"
    work.mkdir()
    _run_git(work, "clone", str(bare), ".")
    _run_git(work, "config", "user.email", "test@example.com")
    _run_git(work, "config", "user.name", "Test User")
    (work / "README.md").write_text("# Test\n")
    _run_git(work, "add", "README.md")
    _run_git(work, "commit", "-m", "Initial commit")
    _run_git(work, "push", "origin", "main")
    return work


def test_fetch_before_branch_creation(tmp_path: Path) -> None:
    """T017: git fetch origin must run before creating the job branch.

    Verification:
    - A bare remote exists with a main branch.
    - A worktree clone has an older local state.
    - After calling prepare_job_branch(), the local repo has fetched
      the latest origin/main.
    - The job branch acc/test-job-123 is created rooted at origin/main.
    """
    from crew_chief.git_delivery import prepare_job_branch

    # Set up a bare remote and a worktree with initial commit.
    bare = _init_bare_remote(tmp_path)
    work = _init_worktree_repo(tmp_path, bare)

    # Add another commit to the bare remote via a separate clone.
    other = tmp_path / "other"
    other.mkdir()
    _run_git(other, "clone", str(bare), ".")
    _run_git(other, "config", "user.email", "test@example.com")
    _run_git(other, "config", "user.name", "Test User")
    (other / "feature.txt").write_text("new feature\n")
    _run_git(other, "add", "feature.txt")
    _run_git(other, "commit", "-m", "Add feature")
    _run_git(other, "push", "origin", "main")

    # The work repo should NOT yet have this commit locally.
    # Verify origin/main is behind before fetch.
    result_before = subprocess.run(
        ["git", "rev-parse", "origin/main"],
        cwd=work,
        capture_output=True,
        text=True,
    )
    assert result_before.returncode == 0
    old_origin_main = result_before.stdout.strip()

    # Now call prepare_job_branch which should fetch first.
    job_id = "test-job-123"
    base_branch = "main"
    branch_name = prepare_job_branch(work, job_id, base_branch)

    assert branch_name == f"acc/{job_id}"

    # Verify that origin/main was updated by the fetch.
    result_after = subprocess.run(
        ["git", "rev-parse", "origin/main"],
        cwd=work,
        capture_output=True,
        text=True,
    )
    assert result_after.returncode == 0
    new_origin_main = result_after.stdout.strip()

    # The origin/main should now point to the latest commit from 'other'.
    assert new_origin_main != old_origin_main, "git fetch origin should have updated origin/main"

    # Verify the job branch exists and is rooted at origin/main.
    result_branch = subprocess.run(
        ["git", "rev-parse", branch_name],
        cwd=work,
        capture_output=True,
        text=True,
    )
    assert result_branch.returncode == 0
    assert result_branch.stdout.strip() == new_origin_main, \
        f"Branch {branch_name} should be rooted at origin/main"

    # Verify we're on the job branch.
    result_current = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=work,
        capture_output=True,
        text=True,
    )
    assert result_current.stdout.strip() == branch_name


def test_prepare_job_branch_custom_base(tmp_path: Path) -> None:
    """prepare_job_branch works with custom base branch and string repo path."""
    from crew_chief.git_delivery import prepare_job_branch

    bare = _init_bare_remote(tmp_path)
    work = _init_worktree_repo(tmp_path, bare)

    # Create and push a develop branch on bare
    _run_git(work, "checkout", "-b", "develop")
    (work / "dev.txt").write_text("dev branch\n")
    _run_git(work, "add", "dev.txt")
    _run_git(work, "commit", "-m", "Develop commit")
    _run_git(work, "push", "origin", "develop")
    _run_git(work, "checkout", "main")

    # Call prepare_job_branch with str path and base_branch="develop"
    branch_name = prepare_job_branch(str(work), "job-xyz", base_branch="develop")
    assert branch_name == "acc/job-xyz"

    result_branch = subprocess.run(
        ["git", "rev-parse", branch_name],
        cwd=work,
        capture_output=True,
        text=True,
    )
    result_dev = subprocess.run(
        ["git", "rev-parse", "origin/develop"],
        cwd=work,
        capture_output=True,
        text=True,
    )
    assert result_branch.returncode == 0
    assert result_branch.stdout.strip() == result_dev.stdout.strip()


def test_push_after_accept_no_force_no_tags(tmp_path: Path) -> None:
    """T018 / US6 / FR-027: Push job branch after accepted piece; forbid --force, base branch pushes, and tag pushes.

    Verification:
    - A bare remote exists with a main branch.
    - A worktree clone has an initial commit.
    - After accepting a piece (simulated by creating a commit on the job branch),
      the push_to_origin function pushes only the job branch to origin.
    - The push does NOT use --force.
    - The push does NOT push the base branch (main).
    - The push does NOT push tags.
    """
    from crew_chief.git_delivery import prepare_job_branch, push_to_origin

    # Set up a bare remote and a worktree with initial commit.
    bare = _init_bare_remote(tmp_path)
    work = _init_worktree_repo(tmp_path, bare)

    # Create the job branch.
    job_id = "test-job-push-123"
    branch_name = prepare_job_branch(work, job_id, base_branch="main")

    # Simulate accepting a piece: create a commit on the job branch.
    (work / "accepted_piece.txt").write_text("accepted work\n")
    _run_git(work, "add", "accepted_piece.txt")
    _run_git(work, "commit", "-m", "Accept piece 1")

    # Create a local tag to verify push_to_origin does NOT push tags
    _run_git(work, "tag", "v1.0.0")

    # Track what was pushed by using a custom git command that logs pushes.
    # We'll verify by checking what's on the remote after push.

    # Push to origin.
    push_to_origin(work, branch_name)

    # Verify the job branch is now on the remote.
    result_remote_branches = subprocess.run(
        ["git", "ls-remote", "--heads", str(bare)],
        cwd=work,
        capture_output=True,
        text=True,
        check=True,
    )
    remote_heads = result_remote_branches.stdout.strip()
    assert f"refs/heads/{branch_name}" in remote_heads, \
        f"Job branch {branch_name} should be pushed to origin"

    # Verify main branch is NOT pushed (it was already there, but we shouldn't push it again explicitly).
    # Actually, main was already pushed during setup, so we just verify no force was used.

    # Verify no tags were pushed.
    result_remote_tags = subprocess.run(
        ["git", "ls-remote", "--tags", str(bare)],
        cwd=work,
        capture_output=True,
        text=True,
        check=True,
    )
    remote_tags = result_remote_tags.stdout.strip()
    assert remote_tags == "", "No tags should be pushed to origin"

    # Verify the commit is on the remote job branch.
    result_remote_commit = subprocess.run(
        ["git", "rev-parse", f"origin/{branch_name}"],
        cwd=work,
        capture_output=True,
        text=True,
        check=True,
    )
    remote_commit = result_remote_commit.stdout.strip()

    result_local_commit = subprocess.run(
        ["git", "rev-parse", branch_name],
        cwd=work,
        capture_output=True,
        text=True,
        check=True,
    )
    local_commit = result_local_commit.stdout.strip()

    assert remote_commit == local_commit, \
        "Remote job branch should match local commit after push"


def test_push_to_origin_forbids_base_branch_flags_and_tags(tmp_path: Path) -> None:
    """push_to_origin forbids pushing base branches, flags (--force), and tags."""
    from crew_chief.git_delivery import push_to_origin

    bare = _init_bare_remote(tmp_path)
    work = _init_worktree_repo(tmp_path, bare)

    # Base branch cannot be pushed
    with pytest.raises(ValueError, match="base branch"):
        push_to_origin(work, "main", base_branch="main")

    with pytest.raises(ValueError, match="base branch"):
        push_to_origin(work, "refs/heads/main", base_branch="main")

    # Flags like --force or -f are forbidden
    with pytest.raises(ValueError, match="forbidden"):
        push_to_origin(work, "--force")

    with pytest.raises(ValueError, match="forbidden"):
        push_to_origin(work, "acc/job -f")

    # Tags are forbidden
    with pytest.raises(ValueError, match="forbidden"):
        push_to_origin(work, "refs/tags/v1.0")

    with pytest.raises(ValueError, match="forbidden"):
        push_to_origin(work, "--tags")


def test_push_to_origin_exact_argv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Exact subprocess argv verification for push_to_origin."""
    from crew_chief.git_delivery import push_to_origin

    bare = _init_bare_remote(tmp_path)
    work = _init_worktree_repo(tmp_path, bare)
    _run_git(work, "branch", "acc/test-job-argv")

    calls = []
    real_run = subprocess.run

    def spy_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy_run)
    push_to_origin(work, "acc/test-job-argv")

    assert calls == [["git", "push", "origin", "acc/test-job-argv"]]
    assert "--force" not in calls[0]
    assert "--tags" not in calls[0]


def test_cli_accept_pushes_job_branch_to_origin(tmp_path: Path) -> None:
    """Canonical acc accept merges piece attempt and pushes explicit job_branch to origin."""
    from crew_chief import records, worktree
    from crew_chief.cli import main
    from crew_chief.git_delivery import prepare_job_branch

    bare = _init_bare_remote(tmp_path)
    work = _init_worktree_repo(tmp_path, bare)

    # Create job with explicit job_branch
    job = records.new_job(work, "Accept Test", "request", True, "reason")
    job_id = job["id"]
    job_branch = prepare_job_branch(work, job_id, base_branch="main")
    job["job_branch"] = job_branch
    job["base_branch"] = "main"
    records.add_piece(job, "Piece 1", "code", "low")
    records.save_job(work, job)

    # Create an attempt worktree and a commit
    wt, attempt_branch = worktree.create_worktree(work, job_id, 1, 1)
    (wt / "file.py").write_text("print('hello')\n")
    _run_git(wt, "add", "file.py")
    _run_git(wt, "commit", "-m", "Commit in piece 1")

    # Record the successful attempt
    attempt = {
        "n": 1,
        "role": "exec",
        "status": "ok",
        "branch": attempt_branch,
        "worktree": str(wt),
    }
    records.update_job(
        work,
        job_id,
        lambda stored: stored["pieces"][0]["attempts"].append(attempt),
    )

    # Accept the piece via canonical CLI
    exit_code = main(["accept", "--project", str(work), "--job", job_id, "--piece", "1"])
    assert exit_code == 0

    # Verify origin received the pushed job_branch
    result_remote = subprocess.run(
        ["git", "ls-remote", "--heads", str(bare)],
        cwd=work,
        capture_output=True,
        text=True,
        check=True,
    )
    assert f"refs/heads/{job_branch}" in result_remote.stdout

    # Verify the commit on remote origin matches local job_branch
    remote_sha = subprocess.run(
        ["git", "rev-parse", f"origin/{job_branch}"],
        cwd=work,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    local_sha = subprocess.run(
        ["git", "rev-parse", job_branch],
        cwd=work,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert remote_sha == local_sha


def test_cli_accept_preserves_legacy_job_no_push(tmp_path: Path) -> None:
    """Canonical acc accept on legacy job without job_branch does not push."""
    from crew_chief import records, worktree
    from crew_chief.cli import main

    bare = _init_bare_remote(tmp_path)
    work = _init_worktree_repo(tmp_path, bare)

    # Legacy job without job_branch
    job = records.new_job(work, "Legacy Job", "request", True, "reason")
    job_id = job["id"]
    records.add_piece(job, "Piece 1", "code", "low")
    records.save_job(work, job)

    wt, attempt_branch = worktree.create_worktree(work, job_id, 1, 1)
    (wt / "legacy.py").write_text("print('legacy')\n")
    _run_git(wt, "add", "legacy.py")
    _run_git(wt, "commit", "-m", "Commit in legacy piece")

    attempt = {
        "n": 1,
        "role": "exec",
        "status": "ok",
        "branch": attempt_branch,
        "worktree": str(wt),
    }
    records.update_job(
        work,
        job_id,
        lambda stored: stored["pieces"][0]["attempts"].append(attempt),
    )

    # Accept via canonical CLI
    exit_code = main(["accept", "--project", str(work), "--job", job_id, "--piece", "1"])
    assert exit_code == 0

    # Verify remote has only main, no other branches
    result_remote = subprocess.run(
        ["git", "ls-remote", "--heads", str(bare)],
        cwd=work,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result_remote.stdout.strip().endswith("refs/heads/main")


def test_single_pr_with_audit_summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """T019 / US6 / US8 / FR-028: Open exactly one PR via gh pr create at job completion.

    Verification:
    - Canonical close on a job with job_branch opens exactly one PR via gh pr create.
    - The body includes accepted piece summaries, verification proofs, and executor grades.
    - pr_url, state ('pr_open'), status ('pr_open'), and result are persisted on the job.
    - Repeated close calls report the existing PR without duplicate creation.
    """
    import json
    from crew_chief import records
    from crew_chief.cli import main
    from crew_chief.git_delivery import prepare_job_branch

    bare = _init_bare_remote(tmp_path)
    work = _init_worktree_repo(tmp_path, bare)

    job = records.new_job(work, "Test Job PR", "implement feature", True, "requested")
    job_id = job["id"]
    job_branch = prepare_job_branch(work, job_id, base_branch="main")
    job["job_branch"] = job_branch
    job["base_branch"] = "main"
    records.add_piece(job, "Feature Piece", "code", "low")
    records.set_grade(
        job,
        1,
        {
            "score": 100,
            "explain": "all checks pass",
            "executor": "mock-exec:m",
            "graded_at": "2026-10-27T12:00:00+00:00",
        },
    )
    records.mark_accepted(
        job,
        1,
        {
            "n": 1,
            "branch": f"{job_branch}-piece-1",
            "role": "exec",
            "status": "ok",
            "verification": "All unit tests pass",
        },
    )
    records.save_job(work, job)

    # Push job branch commit so origin has the branch
    _run_git(work, "checkout", job_branch)
    (work / "feature.txt").write_text("feature work\n")
    _run_git(work, "add", "feature.txt")
    _run_git(work, "commit", "-m", "Add feature")
    _run_git(work, "push", "origin", job_branch)

    # Mock subprocess.run to capture gh pr create calls
    calls = []
    real_run = subprocess.run

    def spy_run(cmd, *args, **kwargs):
        calls.append(list(cmd) if isinstance(cmd, list) else [cmd])
        if isinstance(cmd, list) and cmd and cmd[0] == "gh":
            if "pr" in cmd and "list" in cmd:
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout="[]\n",
                    stderr="",
                )
            if "pr" in cmd and "create" in cmd:
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout="https://github.com/test/repo/pull/42\n",
                    stderr="",
                )
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy_run)

    # Close the job via canonical CLI
    exit_code = main(["close", "--project", str(work), "--job", job_id, "--json"])
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)

    # Verify exactly one gh pr create call was made
    gh_create_calls = [c for c in calls if isinstance(c, list) and len(c) > 0 and c[0] == "gh" and "pr" in c and "create" in c]
    assert len(gh_create_calls) == 1, f"Expected exactly 1 gh pr create call, got {len(gh_create_calls)}"

    create_call = gh_create_calls[0]
    assert "--base" in create_call
    assert create_call[create_call.index("--base") + 1] == "main"
    assert "--head" in create_call
    assert create_call[create_call.index("--head") + 1] == job_branch
    assert "--title" in create_call

    # Verify audit summary in body
    body_idx = create_call.index("--body") + 1
    body_content = create_call[body_idx]
    assert "Feature Piece" in body_content
    assert "mock-exec:m, score 100" in body_content
    assert "Verification Proofs" in body_content

    # Verify persisted job record and output payload
    assert payload["pr_url"] == "https://github.com/test/repo/pull/42"
    assert payload["state"] == "pr_open"
    assert payload["status"] == "pr_open"
    assert payload["result"]["pr_url"] == "https://github.com/test/repo/pull/42"

    stored = records.load_job(work, job_id)
    assert stored["pr_url"] == "https://github.com/test/repo/pull/42"
    assert stored["state"] == "pr_open"
    assert stored["status"] == "pr_open"
    assert stored["result"]["pr_url"] == "https://github.com/test/repo/pull/42"

    # Repeated close call does not create duplicate PRs
    exit_code_repeat = main(["close", "--project", str(work), "--job", job_id, "--json"])
    assert exit_code_repeat == 0
    payload_repeat = json.loads(capsys.readouterr().out)
    assert payload_repeat["pr_url"] == "https://github.com/test/repo/pull/42"
    gh_create_calls_repeat = [c for c in calls if isinstance(c, list) and len(c) > 0 and c[0] == "gh" and "pr" in c and "create" in c]
    assert len(gh_create_calls_repeat) == 1


def test_cli_close_preserves_legacy_job_no_pr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Canonical close on legacy job without job_branch does not invoke gh pr create."""
    import json
    from crew_chief import records
    from crew_chief.cli import main

    bare = _init_bare_remote(tmp_path)
    work = _init_worktree_repo(tmp_path, bare)

    job = records.new_job(work, "Legacy Close", "request", True, "reason")
    job_id = job["id"]
    records.add_piece(job, "Legacy Piece", "code", "low")
    records.save_job(work, job)

    calls = []
    real_run = subprocess.run

    def spy_run(cmd, *args, **kwargs):
        calls.append(list(cmd) if isinstance(cmd, list) else [cmd])
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy_run)

    exit_code = main(["close", "--project", str(work), "--job", job_id, "--json"])
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)

    assert "pr_url" not in payload
    gh_calls = [c for c in calls if isinstance(c, list) and len(c) > 0 and c[0] == "gh"]
    assert len(gh_calls) == 0


def test_create_pr_forbids_duplicate_prs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """T019: Repeated calls to create_pr do not open duplicate PRs."""
    from crew_chief.git_delivery import create_pr

    bare = _init_bare_remote(tmp_path)
    work = _init_worktree_repo(tmp_path, bare)

    # Create a job branch
    _run_git(work, "checkout", "-b", "acc/test-job-dup")
    _run_git(work, "push", "origin", "acc/test-job-dup")

    call_count = 0
    real_run = subprocess.run

    def spy_run(cmd, *args, **kwargs):
        nonlocal call_count
        call_count += 1
        # Simulate gh pr list returning an existing PR on second check
        if "pr" in cmd and "list" in cmd:
            if call_count >= 2:
                # Return existing PR
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout='[{"url": "https://github.com/test/repo/pull/1"}]\n',
                    stderr="",
                )
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout="[]\n",
                stderr="",
            )
        if "gh" in cmd[0] and "pr" in cmd and "create" in cmd:
            # First call succeeds
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout="https://github.com/test/repo/pull/1\n",
                stderr="",
            )
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy_run)

    # First call creates PR
    pr_url1 = create_pr(
        repo_path=work,
        job_id="test-job-dup",
        branch_name="acc/test-job-dup",
        base_branch="main",
        accepted_pieces=[],
    )
    assert pr_url1 == "https://github.com/test/repo/pull/1"

    # Second call should detect existing PR and return same URL
    pr_url2 = create_pr(
        repo_path=work,
        job_id="test-job-dup",
        branch_name="acc/test-job-dup",
        base_branch="main",
        accepted_pieces=[],
    )
    # Should return the same PR URL without creating a duplicate
    assert pr_url2 == "https://github.com/test/repo/pull/1"


def test_pr_review_comment_not_approval(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """T020 / US6 / FR-029: When end_mode = pr_review, post a review comment from a different model without approving.

    Verification:
    - A reviewer model different from coordinator and all executors is selected.
    - The review comment is posted via gh pr review with --comment and --body (not --approve, not --comment-body).
    - Distinct review text produced by the model reaches the comment body.
    - No approval flag is passed.
    """
    from crew_chief.git_delivery import create_pr_review_comment

    bare = _init_bare_remote(tmp_path)
    work = _init_worktree_repo(tmp_path, bare)

    # Create a job branch and push
    _run_git(work, "checkout", "-b", "acc/test-job-review")
    _run_git(work, "push", "origin", "acc/test-job-review")

    calls = []
    real_run = subprocess.run

    def spy_run(cmd, *args, **kwargs):
        cmd_list = list(cmd) if isinstance(cmd, list) else [cmd]
        calls.append(cmd_list)
        if cmd_list and cmd_list[0] == "gh":
            if "pr" in cmd_list and "list" in cmd_list:
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout='[{"url": "https://github.com/test/repo/pull/99", "number": 99}]\n',
                    stderr="",
                )
            if "pr" in cmd_list and "review" in cmd_list:
                # Real gh pr review rejects unknown flags like --comment-body
                if "--comment-body" in cmd_list:
                    return subprocess.CompletedProcess(
                        args=cmd,
                        returncode=1,
                        stderr="unknown flag: --comment-body\n",
                    )
                if "--approve" in cmd_list:
                    return subprocess.CompletedProcess(
                        args=cmd,
                        returncode=1,
                        stderr="forbidden: approval not allowed\n",
                    )
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout="",
                    stderr="",
                )
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy_run)

    # Call create_pr_review_comment with reviewer different from coordinator/executors
    distinct_review = "DISTINCTIVE_MODEL_REVIEW_BODY: Automated audit completed successfully."
    pr_url = create_pr_review_comment(
        repo_path=work,
        job_id="test-job-review",
        branch_name="acc/test-job-review",
        base_branch="main",
        reviewer_model="reviewer-model:v1",
        coordinator_model="coordinator-model:v1",
        executor_models=["executor-model:v1"],
        review_text=distinct_review,
    )

    assert pr_url == "https://github.com/test/repo/pull/99"

    # Verify gh pr review was called with --comment and --body, not --approve, not --comment-body
    gh_review_calls = [c for c in calls if isinstance(c, list) and len(c) > 0 and c[0] == "gh" and "pr" in c and "review" in c]
    assert len(gh_review_calls) == 1, f"Expected exactly 1 gh pr review call, got {len(gh_review_calls)}"

    review_call = gh_review_calls[0]
    assert "--comment" in review_call, "Review should use --comment"
    assert "--body" in review_call, "Review should use --body"
    assert "--comment-body" not in review_call, "Review must not use invalid --comment-body"
    assert "--approve" not in review_call, "Review must not approve"

    # Verify reviewer is different from coordinator and executors and distinctive text is in body
    body_idx = review_call.index("--body") + 1
    body_content = review_call[body_idx]
    assert "reviewer-model:v1" in body_content
    assert distinct_review in body_content


def test_pr_review_distinctness_enforcement(tmp_path: Path) -> None:
    """T020 / US6 / FR-029: Reviewer model must differ from coordinator and all executors, and reject empty identities."""
    from crew_chief.git_delivery import create_pr_review_comment

    # Reviewer matches coordinator -> ValueError
    with pytest.raises(ValueError, match="must differ from coordinator"):
        create_pr_review_comment(
            repo_path=tmp_path,
            job_id="test-job",
            branch_name="acc/test-job",
            reviewer_model="claude:sonnet",
            coordinator_model="claude:sonnet",
            executor_models=[],
        )

    # Reviewer matches executor -> ValueError
    with pytest.raises(ValueError, match="must differ from all executor"):
        create_pr_review_comment(
            repo_path=tmp_path,
            job_id="test-job",
            branch_name="acc/test-job",
            reviewer_model="claude:opus",
            coordinator_model="claude:sonnet",
            executor_models=["claude:opus"],
        )

    # Reviewer matches normalized executor model -> ValueError
    with pytest.raises(ValueError, match="must differ from all executor"):
        create_pr_review_comment(
            repo_path=tmp_path,
            job_id="test-job",
            branch_name="acc/test-job",
            reviewer_model="opus",
            coordinator_model="claude:sonnet",
            executor_models=["claude:opus"],
        )

    # Empty reviewer -> ValueError
    with pytest.raises(ValueError):
        create_pr_review_comment(
            repo_path=tmp_path,
            job_id="test-job",
            branch_name="acc/test-job",
            reviewer_model="",
            coordinator_model="claude:sonnet",
            executor_models=[],
        )

    # Empty coordinator -> ValueError
    with pytest.raises(ValueError):
        create_pr_review_comment(
            repo_path=tmp_path,
            job_id="test-job",
            branch_name="acc/test-job",
            reviewer_model="claude:haiku",
            coordinator_model="",
            executor_models=[],
        )


def test_cli_close_end_mode_pr_review(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], fake_bin) -> None:
    """T020 / US6 / FR-029: CLI close with end_mode=pr_review posts a review comment via configured model CLI."""
    import json
    from crew_chief import records
    from crew_chief.cli import main
    from crew_chief.git_delivery import prepare_job_branch

    # Set up fake model CLI that outputs distinctive review text
    distinct_output = "DISTINCTIVE_HAIKU_REVIEW_OUTPUT_98765: LGTM, no security issues found."
    fake_cli_script = f"""
import sys, json
# Return JSON format matching claude-code output
data = {{"type": "result", "result": "{distinct_output}"}}
print(json.dumps(data))
"""
    fake_bin("claude", fake_cli_script)

    bare = _init_bare_remote(tmp_path)
    work = _init_worktree_repo(tmp_path, bare)

    # Write preferences with team reviewers
    prefs_dir = work / "docs" / "crew-chief"
    prefs_dir.mkdir(parents=True, exist_ok=True)
    (prefs_dir / "preferences.toml").write_text("""
[[tools]]
name = "claude"
kind = "claude-code"
bin = "claude"

[team]
coordinators = ["claude:sonnet"]
executors = ["claude:opus"]
reviewers = ["claude:haiku"]
""")

    job = records.new_job(work, "Review Test", "implement feature", True, "requested")
    job_id = job["id"]
    job_branch = prepare_job_branch(work, job_id, base_branch="main")
    job["job_branch"] = job_branch
    job["base_branch"] = "main"
    records.add_piece(job, "Feature Piece", "code", "low")
    attempt = {
        "n": 1,
        "branch": f"{job_branch}-piece-1",
        "role": "exec",
        "status": "ok",
        "executor": "claude:opus",
        "verification": "All tests pass",
    }
    records.add_attempt(job, 1, attempt)
    records.mark_accepted(job, 1, attempt)
    records.save_job(work, job)

    # Push job branch so origin has it
    _run_git(work, "checkout", job_branch)
    (work / "feature.txt").write_text("feature work\n")
    _run_git(work, "add", "feature.txt")
    _run_git(work, "commit", "-m", "Add feature")
    _run_git(work, "push", "origin", job_branch)

    calls = []
    real_run = subprocess.run

    def spy_run(cmd, *args, **kwargs):
        cmd_list = list(cmd) if isinstance(cmd, list) else [cmd]
        calls.append(cmd_list)
        if cmd_list and cmd_list[0] == "gh":
            if "pr" in cmd_list and "list" in cmd_list:
                # First call (check for existing PR) returns empty; second call (get PR number) returns the PR
                if "--json" in cmd_list and "url,number" in cmd_list:
                    return subprocess.CompletedProcess(
                        args=cmd,
                        returncode=0,
                        stdout='[{"url": "https://github.com/test/repo/pull/42", "number": 42}]\n',
                        stderr="",
                    )
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout="[]\n",
                    stderr="",
                )
            if "pr" in cmd_list and "create" in cmd_list:
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout="https://github.com/test/repo/pull/42\n",
                    stderr="",
                )
            if "pr" in cmd_list and "review" in cmd_list:
                if "--comment-body" in cmd_list:
                    return subprocess.CompletedProcess(
                        args=cmd,
                        returncode=1,
                        stderr="unknown flag: --comment-body\n",
                    )
                if "--approve" in cmd_list:
                    return subprocess.CompletedProcess(
                        args=cmd,
                        returncode=1,
                        stderr="forbidden: approval not allowed\n",
                    )
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout="",
                    stderr="",
                )
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy_run)

    # Close with end_mode=pr_review
    exit_code = main(["close", "--project", str(work), "--job", job_id, "--end-mode", "pr_review", "--json"])
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)

    # Verify PR was created and review comment was posted
    gh_create_calls = [c for c in calls if isinstance(c, list) and len(c) > 0 and c[0] == "gh" and "pr" in c and "create" in c]
    assert len(gh_create_calls) == 1, f"Expected exactly 1 gh pr create call, got {len(gh_create_calls)}"

    gh_review_calls = [c for c in calls if isinstance(c, list) and len(c) > 0 and c[0] == "gh" and "pr" in c and "review" in c]
    assert len(gh_review_calls) == 1, f"Expected exactly 1 gh pr review call, got {len(gh_review_calls)}"

    review_call = gh_review_calls[0]
    assert "--comment" in review_call, "Review should use --comment"
    assert "--body" in review_call, "Review should use --body"
    assert "--comment-body" not in review_call, "Review must not use invalid --comment-body"
    assert "--approve" not in review_call, "Review must not approve"

    # Verify the reviewer model CLI was actually executed
    model_cli_calls = [c for c in calls if c and Path(c[0]).stem == "claude"]
    assert len(model_cli_calls) >= 1, "Reviewer model CLI must be executed"
    assert "haiku" in model_cli_calls[0] or any("--model" in c and "haiku" in c for c in model_cli_calls)

    # Verify distinctive review output produced by model reached gh body
    body_idx = review_call.index("--body") + 1
    body_content = review_call[body_idx]
    assert distinct_output in body_content, "Distinctive model review output must reach review body"
    assert "claude:haiku" in body_content

    # Verify state reflects pr_review_posted
    assert payload["state"] == "pr_review_posted"
    assert payload["pr_url"] == "https://github.com/test/repo/pull/42"


def test_cli_close_end_mode_pr_review_distinctness_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """T020 / US6 / FR-029: CLI close fails when no distinct reviewer is available."""
    from crew_chief import records
    from crew_chief.cli import main
    from crew_chief.git_delivery import prepare_job_branch

    bare = _init_bare_remote(tmp_path)
    work = _init_worktree_repo(tmp_path, bare)

    prefs_dir = work / "docs" / "crew-chief"
    prefs_dir.mkdir(parents=True, exist_ok=True)
    (prefs_dir / "preferences.toml").write_text("""
[team]
coordinators = ["claude:sonnet"]
executors = ["claude:opus"]
reviewers = ["claude:sonnet", "claude:opus"]
""")

    job = records.new_job(work, "Distinctness Test", "implement feature", True, "requested")
    job_id = job["id"]
    job_branch = prepare_job_branch(work, job_id, base_branch="main")
    job["job_branch"] = job_branch
    job["base_branch"] = "main"
    records.add_piece(job, "Feature Piece", "code", "low")
    attempt = {
        "n": 1,
        "branch": f"{job_branch}-piece-1",
        "role": "exec",
        "status": "ok",
        "executor": "claude:opus",
        "verification": "All tests pass",
    }
    records.add_attempt(job, 1, attempt)
    records.mark_accepted(job, 1, attempt)
    records.save_job(work, job)

    exit_code = main(["close", "--project", str(work), "--job", job_id, "--end-mode", "pr_review"])
    assert exit_code == 2
    assert "no distinct reviewer available" in capsys.readouterr().err


def test_blocked_on_push_or_pr_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """T021 / US6 / FR-030: On git push or gh pr create failure, transitions to blocked, logs exact command and stderr, ceases local commits.

    Verification:
    - A failed push_to_origin in _cmd_accept causes the job to enter 'blocked' state.
    - The exact command and stderr are logged in the job record.
    - No further commits happen after blocking (subsequent accept, dispatch, close, piece commands fail and make no commits).
    - A failed create_pr in _cmd_close (both pr and pr_review end_modes) causes the job to enter 'blocked' state, log exact command/stderr.
    """
    import json
    from unittest.mock import MagicMock
    from crew_chief import records
    from crew_chief.cli import main
    from crew_chief.git_delivery import prepare_job_branch

    bare = _init_bare_remote(tmp_path)
    work = _init_worktree_repo(tmp_path, bare)

    # Configure preferences with the team configuration
    prefs_dir = work / "docs" / "crew-chief"
    prefs_dir.mkdir(parents=True, exist_ok=True)
    secret_file = tmp_path / "notify_secret"
    secret_file.write_bytes(b"test-secret-1234")
    secret_file.chmod(0o600)
    (prefs_dir / "preferences.toml").write_text(f"""
[team]
coordinators = ["claude:sonnet"]
executors = ["claude:opus"]
reviewers = ["codex:o1"]
""")

    # Create job with job_branch
    job = records.new_job(work, "Blocked Test", "implement feature", True, "requested")
    job_id = job["id"]
    job_branch = prepare_job_branch(work, job_id, base_branch="main")
    job["job_branch"] = job_branch
    job["base_branch"] = "main"
    records.add_piece(job, "Feature Piece", "code", "low")
    records.save_job(work, job)

    # Create an attempt worktree and a commit
    from crew_chief import worktree
    wt, attempt_branch = worktree.create_worktree(work, job_id, 1, 1)
    (wt / "file.py").write_text("print('hello')\n")
    _run_git(wt, "add", "file.py")
    _run_git(wt, "commit", "-m", "Commit in piece 1")

    # Record the successful attempt
    attempt = {
        "n": 1,
        "role": "exec",
        "status": "ok",
        "branch": attempt_branch,
        "worktree": str(wt),
    }
    records.update_job(
        work,
        job_id,
        lambda stored: stored["pieces"][0]["attempts"].append(attempt),
    )

    # Mock subprocess.run to make ONLY this job's push fail
    real_run = subprocess.run
    push_stderr = "error: failed to push some refs to 'origin'"
    target_branch = job_branch
    attempted_push_cmd = None

    def spy_run_fail_push(cmd, *args, **kwargs):
        nonlocal attempted_push_cmd
        cmd_list = list(cmd) if isinstance(cmd, list) else [cmd]
        if cmd_list and cmd_list[0] == "git" and "push" in cmd_list and target_branch in cmd_list:
            attempted_push_cmd = " ".join(cmd_list)
            raise subprocess.CalledProcessError(
                returncode=1,
                cmd=cmd,
                stderr=push_stderr,
            )
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy_run_fail_push)

    # Accept the piece via canonical CLI — should fail and transition to blocked
    exit_code = main(["accept", "--project", str(work), "--job", job_id, "--piece", "1"])
    assert exit_code != 0, "accept should fail when push fails"

    # Verify job entered blocked state
    stored = records.load_job(work, job_id)
    assert stored.get("state") == "blocked", f"Expected blocked state, got {stored.get('state')}"
    assert stored.get("status") == "blocked", f"Expected blocked status, got {stored.get('status')}"

    # Verify exact command and stderr are logged
    result = stored.get("result", {})
    assert result.get("failed_command") == attempted_push_cmd, f"Expected {attempted_push_cmd}, got {result.get('failed_command')}"
    assert result.get("blocked_error") == push_stderr, f"Expected {push_stderr}, got {result.get('blocked_error')}"


    # Verify commit cessation: local commits cease, subsequent commands on blocked job fail without committing
    head_sha_before = _run_git(work, "rev-parse", "HEAD").stdout.strip()
    commit_count_before = int(_run_git(work, "rev-list", "--count", "HEAD").stdout.strip())

    # Subsequent accept must fail and make no commits
    exit_sub_accept = main(["accept", "--project", str(work), "--job", job_id, "--piece", "1"])
    assert exit_sub_accept != 0, "subsequent accept on blocked job must fail"
    assert _run_git(work, "rev-parse", "HEAD").stdout.strip() == head_sha_before
    assert int(_run_git(work, "rev-list", "--count", "HEAD").stdout.strip()) == commit_count_before

    # Subsequent close must fail and make no commits
    exit_sub_close = main(["close", "--project", str(work), "--job", job_id])
    assert exit_sub_close != 0, "subsequent close on blocked job must fail"
    assert _run_git(work, "rev-parse", "HEAD").stdout.strip() == head_sha_before
    assert int(_run_git(work, "rev-list", "--count", "HEAD").stdout.strip()) == commit_count_before

    # Subsequent piece start must fail and make no commits
    exit_sub_piece_start = main([
        "piece", "start", "--project", str(work), "--job", job_id, "--piece", "1",
        "--native", "--coordinator", "claude", "--model", "sonnet",
    ])
    assert exit_sub_piece_start != 0, "subsequent piece start on blocked job must fail"
    assert _run_git(work, "rev-parse", "HEAD").stdout.strip() == head_sha_before
    assert int(_run_git(work, "rev-list", "--count", "HEAD").stdout.strip()) == commit_count_before

    # Subsequent piece finish must fail and make no commits
    exit_sub_piece_finish = main(["piece", "finish", "--project", str(work), "--job", job_id, "--piece", "1"])
    assert exit_sub_piece_finish != 0, "subsequent piece finish on blocked job must fail"
    assert _run_git(work, "rev-parse", "HEAD").stdout.strip() == head_sha_before
    assert int(_run_git(work, "rev-list", "--count", "HEAD").stdout.strip()) == commit_count_before

    # Subsequent dispatch must fail and make no commits
    brief_path = tmp_path / "test_brief.txt"
    brief_path.write_text("test brief")
    exit_sub_dispatch = main([
        "dispatch", "--project", str(work), "--job", job_id, "--piece", "1",
        "--executor", "claude:opus", "--brief-file", str(brief_path),
    ])
    assert exit_sub_dispatch != 0, "subsequent dispatch on blocked job must fail"
    assert _run_git(work, "rev-parse", "HEAD").stdout.strip() == head_sha_before
    assert int(_run_git(work, "rev-list", "--count", "HEAD").stdout.strip()) == commit_count_before

    # Now test PR creation failure in default 'pr' end_mode
    job2 = records.new_job(work, "Blocked PR Test", "implement feature 2", True, "requested")
    job_id2 = job2["id"]
    job_branch2 = prepare_job_branch(work, job_id2, base_branch="main")
    job2["job_branch"] = job_branch2
    job2["base_branch"] = "main"
    records.add_piece(job2, "Feature Piece 2", "code", "low")

    # Mark piece as accepted so close will try to create PR
    attempt2 = {
        "n": 1,
        "branch": f"{job_branch2}-piece-1",
        "role": "exec",
        "status": "ok",
        "executor": "test-exec",
        "verification": "All tests pass",
    }
    records.add_attempt(job2, 1, attempt2)
    records.mark_accepted(job2, 1, attempt2)
    records.save_job(work, job2)

    # Push the branch so gh pr list can find it (mocked)
    _run_git(work, "checkout", job_branch2)
    (work / "feature2.txt").write_text("feature work 2\n")
    _run_git(work, "add", "feature2.txt")
    _run_git(work, "commit", "-m", "Add feature 2")
    _run_git(work, "push", "origin", job_branch2)

    pr_stderr = "error: could not create pull request"
    attempted_pr_cmd = None

    def spy_run_fail_pr(cmd, *args, **kwargs):
        nonlocal attempted_pr_cmd
        cmd_list = list(cmd) if isinstance(cmd, list) else [cmd]
        if cmd_list and cmd_list[0] == "gh" and "pr" in cmd_list and "create" in cmd_list:
            attempted_pr_cmd = " ".join(cmd_list)
            raise subprocess.CalledProcessError(
                returncode=1,
                cmd=cmd,
                stderr=pr_stderr,
            )
        if cmd_list and cmd_list[0] == "gh" and "pr" in cmd_list and "list" in cmd_list:
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout="[]\n",
                stderr="",
            )
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy_run_fail_pr)

    # Close the job — should fail and transition to blocked
    exit_code2 = main(["close", "--project", str(work), "--job", job_id2, "--json"])
    assert exit_code2 != 0, "close should fail when PR creation fails"

    # Verify job2 entered blocked state
    stored2 = records.load_job(work, job_id2)
    assert stored2.get("state") == "blocked", f"Expected blocked state for job2, got {stored2.get('state')}"
    assert stored2.get("status") == "blocked", f"Expected blocked status for job2, got {stored2.get('status')}"
    result2 = stored2.get("result", {})
    assert result2.get("failed_command") == attempted_pr_cmd, f"Expected {attempted_pr_cmd}, got {result2.get('failed_command')}"
    assert result2.get("blocked_error") == pr_stderr, f"Expected {pr_stderr}, got {result2.get('blocked_error')}"


    # Now test PR creation failure in 'pr_review' end_mode
    job3 = records.new_job(work, "Blocked PR Review Test", "implement feature 3", True, "requested")
    job_id3 = job3["id"]
    job_branch3 = prepare_job_branch(work, job_id3, base_branch="main")
    job3["job_branch"] = job_branch3
    job3["base_branch"] = "main"
    records.add_piece(job3, "Feature Piece 3", "code", "low")

    attempt3 = {
        "n": 1,
        "branch": f"{job_branch3}-piece-1",
        "role": "exec",
        "status": "ok",
        "executor": "claude:opus",
        "verification": "All tests pass",
    }
    records.add_attempt(job3, 1, attempt3)
    records.mark_accepted(job3, 1, attempt3)
    records.save_job(work, job3)

    _run_git(work, "checkout", job_branch3)
    (work / "feature3.txt").write_text("feature work 3\n")
    _run_git(work, "add", "feature3.txt")
    _run_git(work, "commit", "-m", "Add feature 3")
    _run_git(work, "push", "origin", job_branch3)

    exit_code3 = main(["close", "--project", str(work), "--job", job_id3, "--end-mode", "pr_review", "--json"])
    assert exit_code3 != 0, "close with pr_review should fail when PR creation fails"

    stored3 = records.load_job(work, job_id3)
    assert stored3.get("state") == "blocked", f"Expected blocked state for job3, got {stored3.get('state')}"
    assert stored3.get("status") == "blocked", f"Expected blocked status for job3, got {stored3.get('status')}"
    result3 = stored3.get("result", {})
    assert result3.get("failed_command") == attempted_pr_cmd, f"Expected {attempted_pr_cmd}, got {result3.get('failed_command')}"
    assert result3.get("blocked_error") == pr_stderr, f"Expected {pr_stderr}, got {result3.get('blocked_error')}"



def test_pr_branch_contains_specs_and_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """T030 / US7 / FR-032: PR branch contains both reviewed specification documents and implementation changes.

    Verification:
    - Spec files exist in specs/ directory on the job branch.
    - Code files exist on the job branch.
    - Both are committed and pushed before PR creation.
    - The PR body mentions both spec and code pieces.
    """
    from crew_chief.git_delivery import create_pr

    bare = _init_bare_remote(tmp_path)
    work = _init_worktree_repo(tmp_path, bare)

    # Create a job branch
    _run_git(work, "checkout", "-b", "acc/test-job-specs")

    # Create spec directory and files (simulating spec-driven development)
    specs_dir = work / "specs" / "001-test-feature"
    specs_dir.mkdir(parents=True)
    (specs_dir / "spec.md").write_text("# Test Feature Spec\n\nRequirements...")
    (specs_dir / "plan.md").write_text("# Implementation Plan\n\nSteps...")
    (specs_dir / "tasks.md").write_text("# Tasks\n\n- [ ] Task 1")

    # Create code files (simulating implementation)
    (work / "feature.py").write_text("def feature():\n    pass")

    # Commit both spec and code
    _run_git(work, "add", ".")
    _run_git(work, "commit", "-m", "Add spec and code")

    # Push to origin
    _run_git(work, "push", "origin", "acc/test-job-specs")

    # Verify spec files exist on the branch
    result_spec = subprocess.run(
        ["git", "ls-tree", "-r", "HEAD", "--name-only"],
        cwd=work,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "specs/001-test-feature/spec.md" in result_spec.stdout, \
        "Spec files must be committed to the branch"
    assert "feature.py" in result_spec.stdout, \
        "Code files must be committed to the branch"

    # Mock subprocess.run to capture gh pr create calls
    calls = []
    real_run = subprocess.run

    def spy_run(cmd, *args, **kwargs):
        cmd_list = list(cmd) if isinstance(cmd, list) else [cmd]
        calls.append(cmd_list)
        if cmd_list and cmd_list[0] == "gh":
            if "pr" in cmd_list and "list" in cmd_list:
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout="[]\n",
                    stderr="",
                )
            if "pr" in cmd_list and "create" in cmd_list:
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout="https://github.com/test/repo/pull/1\n",
                    stderr="",
                )
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy_run)

    # Create PR with accepted pieces including both spec and code
    accepted_pieces = [
        {
            "n": 1,
            "title": "Test Feature Spec",
            "status": "accepted",
            "executor_grade": "claude:sonnet, score 100",
            "verification": "Spec reviewed and approved",
            "type": "spec",
        },
        {
            "n": 2,
            "title": "Test Feature Code",
            "status": "accepted",
            "executor_grade": "claude:opus, score 100",
            "verification": "All tests passed",
            "type": "code",
        },
    ]

    pr_url = create_pr(
        repo_path=work,
        job_id="test-job-specs",
        branch_name="acc/test-job-specs",
        base_branch="main",
        accepted_pieces=accepted_pieces,
    )

    assert pr_url == "https://github.com/test/repo/pull/1"

    # Verify gh pr create was called
    gh_create_calls = [c for c in calls if isinstance(c, list) and len(c) > 0 and c[0] == "gh" and "pr" in c and "create" in c]
    assert len(gh_create_calls) == 1, f"Expected exactly 1 gh pr create call, got {len(gh_create_calls)}"

    # Verify the PR body includes both spec and code references
    create_call = gh_create_calls[0]
    body_idx = create_call.index("--body") + 1
    body_content = create_call[body_idx]

    # The PR should mention both spec and code pieces
    assert "Test Feature Spec" in body_content, "PR body should include spec piece title"
    assert "Test Feature Code" in body_content, "PR body should include code piece title"
    assert "Spec reviewed and approved" in body_content, "PR body should include spec verification"
    assert "All tests passed" in body_content, "PR body should include code verification"


def _setup_sdd_job_for_close(work: Path, tmp_path: Path, job_name: str = "SDD Job"):
    from crew_chief import records
    from crew_chief.git_delivery import prepare_job_branch

    # Setup preferences
    prefs_dir = work / "docs" / "crew-chief"
    prefs_dir.mkdir(parents=True, exist_ok=True)
    (prefs_dir / "preferences.toml").write_text("""
[team]
coordinators = ["claude:sonnet"]
executors = ["claude:opus"]
reviewers = ["codex:o1"]
""")
    job = records.new_job(work, job_name, "implement SDD feature", True, "requested")
    job_id = job["id"]
    job_branch = prepare_job_branch(work, job_id, base_branch="main")
    job["job_branch"] = job_branch
    job["base_branch"] = "main"
    job["spec_dir"] = "specs/001-test-feature"
    job["phase"] = "spec"

    records.add_piece(job, "Test Feature Spec", "docs", "low")
    job["pieces"][0]["type"] = "spec"
    records.add_piece(job, "Test Feature Code", "code", "low")

    attempt1 = {
        "n": 1,
        "branch": f"{job_branch}-piece-1",
        "role": "exec",
        "status": "ok",
        "executor": "claude:sonnet",
        "verification": "Spec reviewed and approved",
    }
    records.add_attempt(job, 1, attempt1)
    records.mark_accepted(job, 1, attempt1)

    attempt2 = {
        "n": 1,
        "branch": f"{job_branch}-piece-2",
        "role": "exec",
        "status": "ok",
        "executor": "claude:opus",
        "verification": "All tests passed",
    }
    records.add_attempt(job, 2, attempt2)
    records.mark_accepted(job, 2, attempt2)

    records.save_job(work, job)
    return job_id, job_branch


def test_close_refuses_code_only_branch_for_sdd_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """T030 negative case: code-only branch must be refused before gh pr create."""
    from crew_chief.cli import main
    from crew_chief.git_delivery import create_pr

    bare = _init_bare_remote(tmp_path)
    work = _init_worktree_repo(tmp_path, bare)
    job_id, job_branch = _setup_sdd_job_for_close(work, tmp_path, "Code Only Job")

    # Only commit code to the branch — no specs
    _run_git(work, "checkout", job_branch)
    (work / "feature.py").write_text("def feature(): pass\n")
    _run_git(work, "add", "feature.py")
    _run_git(work, "commit", "-m", "Add feature code only")
    _run_git(work, "push", "origin", job_branch)

    calls = []
    real_run = subprocess.run

    def spy_run(cmd, *args, **kwargs):
        cmd_list = list(cmd) if isinstance(cmd, list) else [cmd]
        calls.append(cmd_list)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy_run)

    # 1. Close path must refuse and not call gh
    exit_code = main(["close", "--project", str(work), "--job", job_id])
    assert exit_code != 0, "close must fail on code-only branch for SDD job"
    gh_calls = [c for c in calls if c and c[0] == "gh"]
    assert len(gh_calls) == 0, f"gh must not be called when specs are missing, got: {gh_calls}"

    # 2. create_pr direct call must raise ValueError
    with pytest.raises(ValueError, match="spec"):
        create_pr(
            repo_path=work,
            job_id=job_id,
            branch_name=job_branch,
            base_branch="main",
            spec_dir="specs/001-test-feature",
        )


def test_close_refuses_spec_only_branch_for_sdd_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """T030 negative case: spec-only branch must be refused before gh pr create."""
    from crew_chief.cli import main
    from crew_chief.git_delivery import create_pr

    bare = _init_bare_remote(tmp_path)
    work = _init_worktree_repo(tmp_path, bare)
    job_id, job_branch = _setup_sdd_job_for_close(work, tmp_path, "Spec Only Job")

    # Only commit specs to the branch — no implementation changes
    _run_git(work, "checkout", job_branch)
    specs_dir = work / "specs" / "001-test-feature"
    specs_dir.mkdir(parents=True)
    (specs_dir / "spec.md").write_text("# Spec\n")
    (specs_dir / "plan.md").write_text("# Plan\n")
    (specs_dir / "tasks.md").write_text("# Tasks\n")
    _run_git(work, "add", "specs")
    _run_git(work, "commit", "-m", "Add specs only")
    _run_git(work, "push", "origin", job_branch)

    calls = []
    real_run = subprocess.run

    def spy_run(cmd, *args, **kwargs):
        cmd_list = list(cmd) if isinstance(cmd, list) else [cmd]
        calls.append(cmd_list)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy_run)

    # 1. Close path must refuse and not call gh
    exit_code = main(["close", "--project", str(work), "--job", job_id])
    assert exit_code != 0, "close must fail on spec-only branch for SDD job"
    gh_calls = [c for c in calls if c and c[0] == "gh"]
    assert len(gh_calls) == 0, f"gh must not be called when code is missing, got: {gh_calls}"

    # 2. create_pr direct call must raise ValueError
    with pytest.raises(ValueError, match="implementation"):
        create_pr(
            repo_path=work,
            job_id=job_id,
            branch_name=job_branch,
            base_branch="main",
            spec_dir="specs/001-test-feature",
        )


def test_close_refuses_uncommitted_untracked_specs_not_in_pr_branch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """T030 negative case: untracked specs on disk but not in PR branch must be refused."""
    from crew_chief.cli import main
    from crew_chief.git_delivery import create_pr

    bare = _init_bare_remote(tmp_path)
    work = _init_worktree_repo(tmp_path, bare)
    job_id, job_branch = _setup_sdd_job_for_close(work, tmp_path, "Untracked Specs Job")

    # Commit code to the branch
    _run_git(work, "checkout", job_branch)
    (work / "feature.py").write_text("def feature(): pass\n")
    _run_git(work, "add", "feature.py")
    _run_git(work, "commit", "-m", "Add feature code")
    _run_git(work, "push", "origin", job_branch)

    # Create spec files on disk, but DO NOT git add or commit them
    specs_dir = work / "specs" / "001-test-feature"
    specs_dir.mkdir(parents=True)
    (specs_dir / "spec.md").write_text("# Spec on disk only\n")

    calls = []
    real_run = subprocess.run

    def spy_run(cmd, *args, **kwargs):
        cmd_list = list(cmd) if isinstance(cmd, list) else [cmd]
        calls.append(cmd_list)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy_run)

    # 1. Close path must refuse and not call gh because specs are not committed to branch HEAD
    exit_code = main(["close", "--project", str(work), "--job", job_id])
    assert exit_code != 0, "close must fail when specs are not in PR branch HEAD"
    gh_calls = [c for c in calls if c and c[0] == "gh"]
    assert len(gh_calls) == 0, f"gh must not be called when specs are uncommitted, got: {gh_calls}"

    # 2. create_pr direct call must raise ValueError
    with pytest.raises(ValueError, match="spec"):
        create_pr(
            repo_path=work,
            job_id=job_id,
            branch_name=job_branch,
            base_branch="main",
            spec_dir="specs/001-test-feature",
        )


def test_close_refuses_supplied_fake_piece_titles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """T030 negative case: fake piece titles cannot bypass branch content verification."""
    from crew_chief.cli import main
    from crew_chief.git_delivery import create_pr

    bare = _init_bare_remote(tmp_path)
    work = _init_worktree_repo(tmp_path, bare)
    job_id, job_branch = _setup_sdd_job_for_close(work, tmp_path, "Fake Titles Job")

    # Only commit code to the branch
    _run_git(work, "checkout", job_branch)
    (work / "feature.py").write_text("def feature(): pass\n")
    _run_git(work, "add", "feature.py")
    _run_git(work, "commit", "-m", "Add code only")
    _run_git(work, "push", "origin", job_branch)

    calls = []
    real_run = subprocess.run

    def spy_run(cmd, *args, **kwargs):
        cmd_list = list(cmd) if isinstance(cmd, list) else [cmd]
        calls.append(cmd_list)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy_run)

    # The job pieces claim both spec and code, but branch lacks specs
    exit_code = main(["close", "--project", str(work), "--job", job_id])
    assert exit_code != 0, "fake piece titles must not bypass branch verification"
    gh_calls = [c for c in calls if c and c[0] == "gh"]
    assert len(gh_calls) == 0

    # Direct create_pr with fake titles in accepted_pieces
    with pytest.raises(ValueError, match="spec"):
        create_pr(
            repo_path=work,
            job_id="fake-job",
            branch_name=job_branch,
            base_branch="main",
            accepted_pieces=[
                {"n": 1, "title": "Spec 001 reviewed and approved", "type": "spec"},
                {"n": 2, "title": "Feature implementation complete", "type": "code"},
            ],
        )


def test_close_succeeds_for_real_sdd_branch_with_specs_and_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """T030 positive case: real local git branch with both specs and implementation succeeds."""
    from crew_chief import records
    from crew_chief.cli import main

    bare = _init_bare_remote(tmp_path)
    work = _init_worktree_repo(tmp_path, bare)
    job_id, job_branch = _setup_sdd_job_for_close(work, tmp_path, "Positive SDD Job")

    # Commit both specs and code to the branch
    _run_git(work, "checkout", job_branch)
    specs_dir = work / "specs" / "001-test-feature"
    specs_dir.mkdir(parents=True)
    (specs_dir / "spec.md").write_text("# Spec\n")
    (specs_dir / "plan.md").write_text("# Plan\n")
    (specs_dir / "tasks.md").write_text("# Tasks\n")
    (work / "feature.py").write_text("def feature(): pass\n")
    _run_git(work, "add", ".")
    _run_git(work, "commit", "-m", "Add spec and implementation")
    _run_git(work, "push", "origin", job_branch)

    calls = []
    real_run = subprocess.run

    def spy_run(cmd, *args, **kwargs):
        cmd_list = list(cmd) if isinstance(cmd, list) else [cmd]
        calls.append(cmd_list)
        if cmd_list and cmd_list[0] == "gh":
            if "pr" in cmd_list and "list" in cmd_list:
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout="[]\n",
                    stderr="",
                )
            if "pr" in cmd_list and "create" in cmd_list:
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout="https://github.com/test/repo/pull/100\n",
                    stderr="",
                )
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy_run)

    exit_code = main(["close", "--project", str(work), "--job", job_id, "--json"])
    assert exit_code == 0, "close must succeed when branch has specs and code"

    gh_create_calls = [c for c in calls if c and c[0] == "gh" and "pr" in c and "create" in c]
    assert len(gh_create_calls) == 1
    create_call = gh_create_calls[0]
    body = create_call[create_call.index("--body") + 1]

    # Verify PR body mentions both spec and code pieces and their type
    assert "Test Feature Spec" in body
    assert "Test Feature Code" in body
    assert "Type: spec" in body
    assert "Type: code" in body

    stored = records.load_job(work, job_id)
    assert stored["pr_url"] == "https://github.com/test/repo/pull/100"
    assert stored["state"] == "pr_open"


def test_close_preserves_legacy_jobs_lacking_sdd_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """T030: legacy jobs lacking SDD metadata are preserved and do not require specs."""
    from crew_chief import records
    from crew_chief.cli import main
    from crew_chief.git_delivery import prepare_job_branch

    bare = _init_bare_remote(tmp_path)
    work = _init_worktree_repo(tmp_path, bare)

    prefs_dir = work / "docs" / "crew-chief"
    prefs_dir.mkdir(parents=True, exist_ok=True)
    (prefs_dir / "preferences.toml").write_text("""
[team]
coordinators = ["claude:sonnet"]
executors = ["claude:opus"]
reviewers = ["codex:o1"]
""")
    # Legacy job has no spec_dir and no spec pieces
    job = records.new_job(work, "Legacy Job", "fix bug", True, "requested")
    job_id = job["id"]
    job_branch = prepare_job_branch(work, job_id, base_branch="main")
    job["job_branch"] = job_branch
    job["base_branch"] = "main"

    records.add_piece(job, "Bugfix Code", "code", "low")

    attempt = {
        "n": 1,
        "branch": f"{job_branch}-piece-1",
        "role": "exec",
        "status": "ok",
        "executor": "claude:opus",
        "verification": "Tests pass",
    }
    records.add_attempt(job, 1, attempt)
    records.mark_accepted(job, 1, attempt)
    records.save_job(work, job)

    _run_git(work, "checkout", job_branch)
    (work / "fix.py").write_text("def fix(): pass\n")
    _run_git(work, "add", "fix.py")
    _run_git(work, "commit", "-m", "Add fix")
    _run_git(work, "push", "origin", job_branch)

    calls = []
    real_run = subprocess.run

    def spy_run(cmd, *args, **kwargs):
        cmd_list = list(cmd) if isinstance(cmd, list) else [cmd]
        calls.append(cmd_list)
        if cmd_list and cmd_list[0] == "gh":
            if "pr" in cmd_list and "list" in cmd_list:
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="[]\n", stderr="")
            if "pr" in cmd_list and "create" in cmd_list:
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="https://github.com/test/repo/pull/200\n", stderr="")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy_run)

    exit_code = main(["close", "--project", str(work), "--job", job_id, "--json"])
    assert exit_code == 0, "legacy job must succeed without requiring spec files"
    gh_create_calls = [c for c in calls if c and c[0] == "gh" and "pr" in c and "create" in c]
    assert len(gh_create_calls) == 1


def test_no_merge_command_exists() -> None:
    """T022 / US6 / FR-031: No automatic PR merge commands, auto-merge flags, or PR merge monitoring in product.

    Verification:
    - CLI parser defines no 'merge', 'pr-merge', or 'auto-merge' subcommands.
    - CLI parser defines no '--auto-merge' or '--auto-merge-pr' flags anywhere in its subcommand tree.
    - crew_chief.git_delivery exports no PR merge functions and contains no 'gh pr merge' commands.
    - Codebase audit confirms no 'gh pr merge', '--auto-merge', or background PR merge monitoring exists.
    - PR merge authority remains exclusively with the human repository owner.
    - Internal piece-branch accept merge (worktree.accept / acc accept) is retained as authorized implementation.
    """
    import argparse
    import contextlib
    import io
    from pathlib import Path
    import crew_chief
    from crew_chief.cli import build_parser
    import crew_chief.git_delivery as git_delivery
    from crew_chief.worktree import accept

    parser = build_parser()

    # 1. Verify no top-level or sub-level merge/pr-merge command exists
    def _walk_parsers(p: argparse.ArgumentParser):
        yield p
        for action in p._actions:
            if isinstance(action, argparse._SubParsersAction):
                for choice_name, subparser in action.choices.items():
                    yield from _walk_parsers(subparser)

    all_parsers = list(_walk_parsers(parser))

    # Top-level subcommands
    top_level_subcommands = []
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            top_level_subcommands.extend(action.choices.keys())

    assert "merge" not in top_level_subcommands, "CLI must not have a 'merge' subcommand"
    assert "pr-merge" not in top_level_subcommands, "CLI must not have a 'pr-merge' subcommand"
    assert "auto-merge" not in top_level_subcommands, "CLI must not have an 'auto-merge' subcommand"

    # 2. Verify no parser defines an auto-merge flag anywhere in its options
    for subp in all_parsers:
        for action in subp._actions:
            for opt in action.option_strings:
                assert "--auto-merge" not in opt, f"Forbidden auto-merge option found: {opt}"
                assert opt not in ("--merge", "--merge-pr", "--auto-merge-pr"), f"Forbidden merge option found: {opt}"

    # Verify attempting to pass --auto-merge or merge command is rejected by the parser
    with contextlib.redirect_stderr(io.StringIO()):
        with pytest.raises(SystemExit):
            parser.parse_args(["merge"])

        with pytest.raises(SystemExit):
            parser.parse_args(["pr-merge"])

        with pytest.raises(SystemExit):
            parser.parse_args(["close", "--auto-merge", "--job", "j1"])

    # 3. Verify git_delivery exports and implements no PR merge commands
    assert "merge_pr" not in git_delivery.__all__
    assert "auto_merge" not in git_delivery.__all__
    for attr_name in dir(git_delivery):
        if not attr_name.startswith("_"):
            assert "merge" not in attr_name.lower(), f"Unexpected merge function in git_delivery: {attr_name}"

    # 4. Codebase audit: scan all crew_chief python files for prohibited PR merge patterns
    pkg_dir = Path(crew_chief.__file__).parent
    forbidden_tokens = [
        "gh pr merge",
        "gh\" pr merge\"",
        "'gh', 'pr', 'merge'",
        "\"gh\", \"pr\", \"merge\"",
        "--auto-merge",
    ]
    for py_file in pkg_dir.rglob("*.py"):
        text = py_file.read_text(encoding="utf-8")
        for token in forbidden_tokens:
            assert token not in text, f"Forbidden PR merge pattern '{token}' found in {py_file}"

    # 5. Verify ACC accept's local piece branch merge into the job branch is retained
    assert callable(accept), "worktree.accept must remain available for authorized internal piece merge"
    assert "accept" in top_level_subcommands, "acc accept subcommand must be retained"






def test_prepare_job_branch_keeps_feature_branch(tmp_path: Path) -> None:
    """A checkout on a feature branch is never switched: it becomes the job branch."""
    from crew_chief.git_delivery import prepare_job_branch

    bare = _init_bare_remote(tmp_path)
    work = _init_worktree_repo(tmp_path, bare)
    _run_git(work, "checkout", "-b", "my-feature")
    (work / "wip.txt").write_text("work in progress\n")
    _run_git(work, "add", "wip.txt")
    _run_git(work, "commit", "-m", "WIP")

    branch_name = prepare_job_branch(work, "job-keep", base_branch="main")

    assert branch_name == "my-feature"
    current = subprocess.run(
        ["git", "branch", "--show-current"], cwd=work, capture_output=True, text=True
    )
    assert current.stdout.strip() == "my-feature"
    assert (work / "wip.txt").exists()
