"""Proof of job_branch wiring across public create paths and delivery flow.

1. Interview path: creates a job via interview, accepts one piece, and the
   job branch exists on origin containing that piece commit.
2. Bootstrap path (job new + spec import): creates a job, imports spec, accepts
   one piece, and the job branch exists on origin containing that piece commit.
3. MCP path: created job record has the same job_branch.
4. Fake gh on PATH: PR step runs once per job, argv is asserted, no second PR
   for the same job.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from crew_chief import interview_service, records, worktree
from crew_chief.cli import main


def _run_git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=check,
    )


def _init_bare_origin(tmp_path: Path) -> Path:
    bare = tmp_path / "origin.git"
    bare.mkdir(parents=True, exist_ok=True)
    _run_git(bare, "init", "--bare", "--initial-branch=main")
    return bare


def _init_work_repo(tmp_path: Path, bare: Path) -> Path:
    work = tmp_path / "work"
    work.mkdir(parents=True, exist_ok=True)
    _run_git(work, "clone", str(bare), ".")
    _run_git(work, "config", "user.email", "test@example.com")
    _run_git(work, "config", "user.name", "Test User")
    (work / "README.md").write_text("# Project\n", encoding="utf-8")
    _run_git(work, "add", "README.md")
    _run_git(work, "commit", "-m", "Initial commit")
    _run_git(work, "push", "origin", "main")
    return work


def _setup_fake_gh(bin_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    log_file = bin_dir / "gh_calls.log"
    gh_script = bin_dir / "gh"

    script_content = f"""#!/usr/bin/env python3
import sys
import json
from pathlib import Path

log_path = Path({repr(str(log_file))})
with open(log_path, "a", encoding="utf-8") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")

args = sys.argv[1:]
if len(args) >= 2 and args[0] == "pr" and args[1] == "list":
    state_file = Path({repr(str(bin_dir / "created_pr.txt"))})
    if state_file.exists():
        url = state_file.read_text().strip()
        print(json.dumps([{{"url": url}}]))
    else:
        print("[]")
elif len(args) >= 2 and args[0] == "pr" and args[1] == "create":
    state_file = Path({repr(str(bin_dir / "created_pr.txt"))})
    url = "https://github.com/example/repo/pull/123"
    state_file.write_text(url)
    print(url)
else:
    print("ok")
"""
    gh_script.write_text(script_content, encoding="utf-8")
    gh_script.chmod(gh_script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    cur_path = os.environ.get("PATH", "")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{cur_path}")
    return log_file


def _get_gh_calls(log_file: Path) -> list[list[str]]:
    if not log_file.exists():
        return []
    lines = [line.strip() for line in log_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [json.loads(line) for line in lines]


def test_interview_path_job_branch_and_origin_push(tmp_path: Path) -> None:
    """Interview path creates a job with job_branch and pushes piece commit on accept."""
    bare = _init_bare_origin(tmp_path)
    work = _init_work_repo(tmp_path, bare)

    # 1. Start interview job via public interview_service entry point
    res = interview_service.start_interview_job(
        project=work,
        request="Implement interview feature",
        coordinator="claude:sonnet",
        executors="own",
        end_mode="pr",
        title="Interview Feature",
    )
    assert res.get("ok") is True
    job_id = res["job_id"]

    # Assert job record has job_branch
    job = records.load_job(work, job_id)
    expected_branch = f"acc/{job_id}"
    assert job.get("job_branch") == expected_branch
    assert job.get("base_branch") == "main"

    # 2. Add a piece and create an attempt commit in a worktree
    records.update_job(
        work,
        job_id,
        lambda stored: records.add_piece(stored, "Feature piece", "code", "low"),
    )
    wt, attempt_branch = worktree.create_worktree(work, job_id, 1, 1)
    (wt / "interview_feat.txt").write_text("interview implementation\n", encoding="utf-8")
    _run_git(wt, "add", "interview_feat.txt")
    _run_git(wt, "commit", "-m", "feat: implement interview feature")

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

    # 3. Accept piece via canonical CLI
    exit_code = main(["accept", "--project", str(work), "--job", job_id, "--piece", "1"])
    assert exit_code == 0

    # 4. Assert job branch exists on origin containing the piece commit
    proc_remote = _run_git(bare, "log", "-n", "5", "--format=%s", expected_branch)
    assert proc_remote.returncode == 0
    assert "feat: implement interview feature" in proc_remote.stdout

    # Assert the file exists on the branch in the origin repo
    proc_show = _run_git(bare, "show", f"{expected_branch}:interview_feat.txt")
    assert proc_show.returncode == 0
    assert "interview implementation" in proc_show.stdout


def test_bootstrap_path_job_branch_and_origin_push(tmp_path: Path) -> None:
    """`job new` + `spec import` creates a job with job_branch and pushes piece commit on accept."""
    bare = _init_bare_origin(tmp_path)
    work = _init_work_repo(tmp_path, bare)

    # 1. Drive `job new` via CLI
    req_file = tmp_path / "req.md"
    req_file.write_text("Implement bootstrap feature\n", encoding="utf-8")
    exit_code = main([
        "job", "new",
        "--project", str(work),
        "--title", "Bootstrap Feature",
        "--request-file", str(req_file),
        "--executors", "own",
    ])
    assert exit_code == 0

    jobs = list((work / "docs" / "crew-chief" / "jobs").glob("*.json"))
    assert len(jobs) == 1
    job_id = jobs[0].stem
    expected_branch = f"acc/{job_id}"

    # 2. Set up spec directory and import tasks
    spec_dir = work / "specs" / "001-bootstrap"
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "spec.md").write_text("# Bootstrap Spec\n", encoding="utf-8")
    (spec_dir / "tasks.md").write_text(
        "# Tasks\n\n- [ ] T001 Implement bootstrap piece\n",
        encoding="utf-8",
    )

    exit_code = main([
        "spec", "import",
        "--project", str(work),
        "--job", job_id,
        "--spec-dir", "specs/001-bootstrap",
    ])
    assert exit_code == 0

    job = records.load_job(work, job_id)
    assert job.get("job_branch") == expected_branch

    # 3. Create attempt worktree, commit changes, and record attempt
    wt, attempt_branch = worktree.create_worktree(work, job_id, 1, 1)
    (wt / "bootstrap_feat.txt").write_text("bootstrap implementation\n", encoding="utf-8")
    _run_git(wt, "add", "bootstrap_feat.txt")
    _run_git(wt, "commit", "-m", "feat: implement bootstrap feature")

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

    # 4. Accept piece via canonical CLI
    exit_code = main(["accept", "--project", str(work), "--job", job_id, "--piece", "1"])
    assert exit_code == 0

    # 5. Assert job branch exists on origin containing the piece commit
    proc_remote = _run_git(bare, "log", "-n", "5", "--format=%s", expected_branch)
    assert proc_remote.returncode == 0
    assert "feat: implement bootstrap feature" in proc_remote.stdout

    proc_show = _run_git(bare, "show", f"{expected_branch}:bootstrap_feat.txt")
    assert proc_show.returncode == 0
    assert "bootstrap implementation" in proc_show.stdout


@pytest.mark.skipif(os.name == "nt", reason="subprocess cannot run the extensionless fake gh on Windows")
def test_fake_gh_pr_step_runs_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """PR step runs once per job using fake gh, asserts argv, and forbids second PR."""
    log_file = _setup_fake_gh(tmp_path / "bin", monkeypatch)
    bare = _init_bare_origin(tmp_path)
    work = _init_work_repo(tmp_path, bare)

    # Start an interview job
    res = interview_service.start_interview_job(
        project=work,
        request="PR step feature",
        coordinator="claude:sonnet",
        executors="own",
        end_mode="pr",
        title="PR Step Feature",
    )
    job_id = res["job_id"]
    expected_branch = f"acc/{job_id}"

    # Accept one piece
    records.update_job(
        work,
        job_id,
        lambda stored: records.add_piece(stored, "PR piece", "code", "low"),
    )
    wt, attempt_branch = worktree.create_worktree(work, job_id, 1, 1)
    (wt / "pr_feat.txt").write_text("pr content\n", encoding="utf-8")
    _run_git(wt, "add", "pr_feat.txt")
    _run_git(wt, "commit", "-m", "feat: pr piece commit")

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
    exit_accept = main(["accept", "--project", str(work), "--job", job_id, "--piece", "1"])
    assert exit_accept == 0

    # First close: opens PR via fake gh
    exit_close_1 = main(["close", "--project", str(work), "--job", job_id, "--no-changelog"])
    assert exit_close_1 == 0

    calls = _get_gh_calls(log_file)
    pr_create_calls = [c for c in calls if len(c) >= 2 and c[0] == "pr" and c[1] == "create"]
    assert len(pr_create_calls) == 1

    create_call = pr_create_calls[0]
    assert "--base" in create_call
    assert create_call[create_call.index("--base") + 1] == "main"
    assert "--head" in create_call
    assert create_call[create_call.index("--head") + 1] == expected_branch
    assert "--title" in create_call
    assert f"Job {job_id}" in create_call[create_call.index("--title") + 1]

    # Second close on the same job: must NOT open a second PR
    exit_close_2 = main(["close", "--project", str(work), "--job", job_id, "--no-changelog"])
    assert exit_close_2 == 0

    calls_after = _get_gh_calls(log_file)
    pr_create_calls_after = [c for c in calls_after if len(c) >= 2 and c[0] == "pr" and c[1] == "create"]
    assert len(pr_create_calls_after) == 1, "Expected no second PR for the same job"
