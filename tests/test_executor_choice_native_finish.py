"""Tests for FR-049 / SC-025: native piece finish records diff, runs proof, refuses outside work.

`piece finish --job <id> --piece <n>` must:
- Record the attempt's diff against its base (the commit at `piece start`).
- Run the piece's proof command (reusing the dispatch proof runner) and set the
  attempt status to `ok`, `no_change` or `failed`/`proof_failed`.
- Refuse (non-zero exit, clear diagnostic, attempt marked refused/failed) work
  committed outside that worktree/branch: commits on the job branch or main
  checkout since `piece start`, and uncommitted changes left in main.
"""
from __future__ import annotations

import json
import subprocess
import textwrap
from datetime import datetime
from pathlib import Path

import pytest

from crew_chief.cli import main
from crew_chief.records import add_attempt, add_piece, load_job, mark_accepted, new_job, update_job

NOW = datetime(2026, 10, 25, 12, 0)


def _git(args: list[str], cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def _init_repo(root: Path) -> Path:
    """Create a minimal git repository with one commit and return its path."""
    project = root / "project"
    project.mkdir(parents=True)
    _git(["init", "-b", "main"], project)
    _git(["config", "user.email", "test@example.com"], project)
    _git(["config", "user.name", "Test User"], project)
    (project / "README.md").write_text("# Test\n", encoding="utf-8")
    _git(["add", "."], project)
    _git(["commit", "-m", "initial"], project)
    return project


def _new_job(project: Path, **overrides) -> dict:
    kwargs = dict(
        title="Native finish test",
        request="Test native finish.",
        distributed=True,
        reason="test",
        now=NOW,
        executors="subagents",
    )
    kwargs.update(overrides)
    return new_job(project, **kwargs)


def _add_piece(project: Path, job_id: str) -> int:
    """Add a code piece to the job via the CLI and return its number."""
    exit_code = main([
        "piece", "add",
        "--project", str(project),
        "--job", job_id,
        "--title", "Implement feature",
        "--type", "code",
        "--risk", "low",
    ])
    assert exit_code == 0
    job = load_job(project, job_id)
    return job["pieces"][-1]["n"]


def _start_native(project: Path, job_id: str, piece: int) -> tuple[Path, str]:
    """Run `piece start --native` and return (worktree, branch)."""
    exit_code = main([
        "piece", "start",
        "--project", str(project),
        "--job", job_id,
        "--piece", str(piece),
        "--native",
        "--coordinator", "claude",
        "--model", "sonnet",
    ])
    assert exit_code == 0
    job = load_job(project, job_id)
    piece_rec = next(p for p in job["pieces"] if p["n"] == piece)
    attempt = piece_rec["attempts"][-1]
    return Path(attempt["worktree"]), attempt["branch"]


def _load_attempt(project: Path, job_id: str, piece: int) -> dict:
    job = load_job(project, job_id)
    for p in job.get("pieces") or []:
        if p.get("n") == piece:
            attempts = p.get("attempts") or []
            return attempts[-1]
    raise AssertionError(f"no attempts for piece {piece}")


def _approve_specs(project: Path, job_id: str, spec_dir_name: str) -> None:
    """Record a valid spec approval so SDD gate allows piece start."""
    def _do_approve(stored: dict) -> dict:
        stored["coordinator"] = "claude:sonnet"
        p_num = add_piece(stored, f"Review {spec_dir_name}/tasks.md", "docs", "low")
        att = {
            "n": 1,
            "executor": "stub:m",
            "role": "review",
            "status": "ok",
            "duration_s": 1.0,
            "branch": "crew/spec-approval",
        }
        add_attempt(stored, p_num, att)
        mark_accepted(stored, p_num, att)
        return stored

    update_job(project, job_id, _do_approve)


def test_piece_finish_records_diff_and_status_ok(tmp_path: Path) -> None:
    """A clean finish with changes records diffstat and sets status ok."""
    project = _init_repo(tmp_path)
    job = _new_job(project)
    piece_n = _add_piece(project, job["id"])
    wt, branch = _start_native(project, job["id"], piece_n)

    # Make a change inside the worktree and commit it there.
    (wt / "foo.txt").write_text("hello\n", encoding="utf-8")
    _git(["add", "."], wt)
    _git(["commit", "-m", "work"], wt)

    exit_code = main([
        "piece", "finish",
        "--project", str(project),
        "--job", job["id"],
        "--piece", str(piece_n),
    ])
    assert exit_code == 0

    attempt = _load_attempt(project, job["id"], piece_n)
    assert attempt["status"] == "ok"
    assert attempt.get("diffstat"), "expected non-empty diffstat"
    files = [r["file"] for r in attempt["diffstat"]]
    assert "foo.txt" in files
    assert attempt.get("end_sha")

    # Verify attempt was updated in place, not duplicated.
    loaded = load_job(project, job["id"])
    piece_rec = next(p for p in loaded["pieces"] if p["n"] == piece_n)
    assert len(piece_rec["attempts"]) == 1


def test_piece_finish_no_change_when_worktree_clean(tmp_path: Path) -> None:
    """A finish with no changes in the worktree records no_change."""
    project = _init_repo(tmp_path)
    job = _new_job(project)
    piece_n = _add_piece(project, job["id"])
    _start_native(project, job["id"], piece_n)

    exit_code = main([
        "piece", "finish",
        "--project", str(project),
        "--job", job["id"],
        "--piece", str(piece_n),
    ])
    assert exit_code == 0

    attempt = _load_attempt(project, job["id"], piece_n)
    assert attempt["status"] == "no_change"


def test_piece_finish_refuses_commit_on_main_since_start(tmp_path: Path) -> None:
    """A commit on main after piece start is refused."""
    project = _init_repo(tmp_path)
    job = _new_job(project)
    piece_n = _add_piece(project, job["id"])
    _start_native(project, job["id"], piece_n)

    # Commit on main AFTER piece start.
    (project / "leak.txt").write_text("bad\n", encoding="utf-8")
    _git(["add", "."], project)
    _git(["commit", "-m", "outside"], project)

    exit_code = main([
        "piece", "finish",
        "--project", str(project),
        "--job", job["id"],
        "--piece", str(piece_n),
    ])
    assert exit_code != 0

    attempt = _load_attempt(project, job["id"], piece_n)
    assert attempt["status"] in ("failed", "refused")


def test_piece_finish_refuses_uncommitted_main_changes(tmp_path: Path) -> None:
    """Uncommitted changes in main since piece start are refused."""
    project = _init_repo(tmp_path)
    job = _new_job(project)
    piece_n = _add_piece(project, job["id"])
    _start_native(project, job["id"], piece_n)

    # Dirty main without committing.
    (project / "dirty.txt").write_text("oops\n", encoding="utf-8")

    exit_code = main([
        "piece", "finish",
        "--project", str(project),
        "--job", job["id"],
        "--piece", str(piece_n),
    ])
    assert exit_code != 0

    attempt = _load_attempt(project, job["id"], piece_n)
    assert attempt["status"] in ("failed", "refused")


def test_piece_finish_runs_proof_and_marks_failed(tmp_path: Path) -> None:
    """When the piece has a proof command, finish runs it and marks proof_failed on failure."""
    project = _init_repo(tmp_path)

    # Create a spec dir with a task that has a failing proof.
    spec_dir = project / "specs" / "001-test"
    spec_dir.mkdir(parents=True)
    (spec_dir / "tasks.md").write_text(
        "- [ ] T001 impl\n",
        encoding="utf-8",
    )
    (spec_dir / "crew.toml").write_text(
        textwrap.dedent("""\
            [tasks.T001]
            type = "code"
            risk = "low"
            proof = "exit 1"
        """),
        encoding="utf-8",
    )

    job = _new_job(project)

    # Import the spec task as piece 1.
    exit_code = main([
        "spec", "import",
        "--project", str(project),
        "--job", job["id"],
        "--spec-dir", str(spec_dir),
    ])
    assert exit_code == 0

    # Record a valid spec approval so SDD gate allows piece start.
    _approve_specs(project, job["id"], "specs/001-test")

    # Commit all ACC-generated records so piece finish's isolation check
    # does not flag them as uncommitted outside work.
    _git(["add", "."], project)
    _git(["commit", "-m", "records"], project)

    wt, _ = _start_native(project, job["id"], 1)
    (wt / "x.txt").write_text("y\n", encoding="utf-8")
    _git(["add", "."], wt)
    _git(["commit", "-m", "w"], wt)

    exit_code = main([
        "piece", "finish",
        "--project", str(project),
        "--job", job["id"],
        "--piece", "1",
    ])
    assert exit_code != 0

    attempt = _load_attempt(project, job["id"], 1)
    assert attempt["status"] == "proof_failed"
    assert attempt.get("proof") is not None
    assert attempt["proof"].get("passed") is False


def test_piece_finish_refuses_commit_on_job_branch(tmp_path: Path) -> None:
    """A commit on the job branch (not the native worktree branch) is refused."""
    project = _init_repo(tmp_path)
    job = _new_job(project)
    piece_n = _add_piece(project, job["id"])
    _start_native(project, job["id"], piece_n)

    # Committing on main which IS the job branch target here.
    (project / "job-leak.txt").write_text("nope\n", encoding="utf-8")
    _git(["add", "."], project)
    _git(["commit", "-m", "on-job-branch"], project)

    exit_code = main([
        "piece", "finish",
        "--project", str(project),
        "--job", job["id"],
        "--piece", str(piece_n),
    ])
    assert exit_code != 0

    attempt = _load_attempt(project, job["id"], piece_n)
    assert attempt["status"] in ("failed", "refused")


def test_piece_finish_commits_uncommitted_changes_in_worktree(tmp_path: Path) -> None:
    """Uncommitted changes left in the worktree are committed by finish."""
    project = _init_repo(tmp_path)
    job = _new_job(project)
    piece_n = _add_piece(project, job["id"])
    wt, branch = _start_native(project, job["id"], piece_n)

    # Leave uncommitted changes inside the worktree.
    (wt / "uncommitted.txt").write_text("auto-commit me\n", encoding="utf-8")

    exit_code = main([
        "piece", "finish",
        "--project", str(project),
        "--job", job["id"],
        "--piece", str(piece_n),
    ])
    assert exit_code == 0

    attempt = _load_attempt(project, job["id"], piece_n)
    assert attempt["status"] == "ok"
    assert attempt.get("end_sha")
    # Verify the worktree is now clean on that branch and has the commit.
    status = _git(["status", "--porcelain"], wt)
    assert status.strip() == ""
    head_wt = _git(["rev-parse", "HEAD"], wt).strip()
    assert head_wt == attempt["end_sha"]


def test_piece_finish_runs_proof_and_marks_ok(tmp_path: Path) -> None:
    """When the piece has a passing proof command, finish marks ok with proof passed."""
    project = _init_repo(tmp_path)

    spec_dir = project / "specs" / "001-test"
    spec_dir.mkdir(parents=True)
    (spec_dir / "tasks.md").write_text(
        "- [ ] T001 impl\n",
        encoding="utf-8",
    )
    (spec_dir / "crew.toml").write_text(
        textwrap.dedent("""\
            [tasks.T001]
            type = "code"
            risk = "low"
            proof = "exit 0"
        """),
        encoding="utf-8",
    )

    job = _new_job(project)

    exit_code = main([
        "spec", "import",
        "--project", str(project),
        "--job", job["id"],
        "--spec-dir", str(spec_dir),
    ])
    assert exit_code == 0

    # Record a valid spec approval so SDD gate allows piece start.
    _approve_specs(project, job["id"], "specs/001-test")

    _git(["add", "."], project)
    _git(["commit", "-m", "records"], project)

    wt, _ = _start_native(project, job["id"], 1)
    (wt / "valid.txt").write_text("good\n", encoding="utf-8")
    _git(["add", "."], wt)
    _git(["commit", "-m", "good work"], wt)

    exit_code = main([
        "piece", "finish",
        "--project", str(project),
        "--job", job["id"],
        "--piece", "1",
    ])
    assert exit_code == 0

    attempt = _load_attempt(project, job["id"], 1)
    assert attempt["status"] == "ok"
    assert attempt.get("proof") is not None
    assert attempt["proof"].get("passed") is True


def test_piece_finish_no_running_attempt_fails(tmp_path: Path) -> None:
    """Attempting to finish when no attempt is running exits non-zero."""
    project = _init_repo(tmp_path)
    job = _new_job(project)
    piece_n = _add_piece(project, job["id"])

    exit_code = main([
        "piece", "finish",
        "--project", str(project),
        "--job", job["id"],
        "--piece", str(piece_n),
    ])
    assert exit_code != 0


def test_piece_finish_json_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """--json returns structured finish payload."""
    project = _init_repo(tmp_path)
    job = _new_job(project)
    piece_n = _add_piece(project, job["id"])
    wt, _ = _start_native(project, job["id"], piece_n)
    (wt / "test.txt").write_text("content\n", encoding="utf-8")
    _git(["add", "."], wt)
    _git(["commit", "-m", "test"], wt)
    capsys.readouterr()  # Flush previous output

    exit_code = main([
        "piece", "finish",
        "--project", str(project),
        "--job", job["id"],
        "--piece", str(piece_n),
        "--json",
    ])
    assert exit_code == 0

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["piece"] == piece_n
    assert payload["status"] == "ok"
    assert payload["attempt"] == 1


def test_piece_finish_commits_with_sensitive_paths_configured(tmp_path: Path) -> None:
    """Non-sensitive worktree changes are committed even when sensitive_paths is set."""
    project = _init_repo(tmp_path)

    # Configure sensitive_paths in project preferences.
    prefs_dir = project / "docs" / "crew-chief"
    prefs_dir.mkdir(parents=True, exist_ok=True)
    (prefs_dir / "preferences.toml").write_text(
        'sensitive_paths = ["secrets/"]\n', encoding="utf-8",
    )
    secret_dir = project / "secrets"
    secret_dir.mkdir(parents=True, exist_ok=True)
    (secret_dir / "token.txt").write_text("super-secret\n", encoding="utf-8")
    _git(["add", "."], project)
    _git(["commit", "-m", "add secret"], project)

    job = _new_job(project)
    piece_n = _add_piece(project, job["id"])
    wt, _ = _start_native(project, job["id"], piece_n)

    start_sha = _git(["rev-parse", "HEAD"], wt).strip()

    # Add a non-sensitive file inside the worktree (leave uncommitted).
    (wt / "feature.txt").write_text("non-sensitive work\n", encoding="utf-8")

    exit_code = main([
        "piece", "finish",
        "--project", str(project),
        "--job", job["id"],
        "--piece", str(piece_n),
    ])
    assert exit_code == 0

    attempt = _load_attempt(project, job["id"], piece_n)
    assert attempt["status"] == "ok"
    assert attempt.get("end_sha")
    assert attempt["end_sha"] != start_sha

    # The non-sensitive file must be tracked in the worktree commit.
    tracked = _git(["ls-tree", "-r", "--name-only", "HEAD"], wt).strip().splitlines()
    assert "feature.txt" in tracked


def test_piece_finish_refuses_branch_mismatch(tmp_path: Path) -> None:
    """Finish refuses when the worktree HEAD is on a different branch than recorded."""
    project = _init_repo(tmp_path)
    job = _new_job(project)
    piece_n = _add_piece(project, job["id"])
    wt, recorded_branch = _start_native(project, job["id"], piece_n)

    # Create and switch to a different branch inside the worktree, then commit.
    other_branch = "wrong-branch"
    _git(["checkout", "-b", other_branch], wt)
    (wt / "stray.txt").write_text("on wrong branch\n", encoding="utf-8")
    _git(["add", "."], wt)
    _git(["commit", "-m", "stray work"], wt)

    exit_code = main([
        "piece", "finish",
        "--project", str(project),
        "--job", job["id"],
        "--piece", str(piece_n),
    ])
    assert exit_code != 0

    attempt = _load_attempt(project, job["id"], piece_n)
    assert attempt["status"] == "failed"
    note = attempt.get("note", "")
    assert recorded_branch in note
    assert other_branch in note

def _finish(project: Path, job_id: str, piece: int) -> int:
    return main([
        "piece", "finish",
        "--project", str(project),
        "--job", job_id,
        "--piece", str(piece),
    ])


def test_native_correction_starts_from_the_corrected_attempt(tmp_path: Path) -> None:
    """After a correction, the next native attempt keeps the earlier work and
    its diff counts only the adjustment (as dispatch does)."""
    from crew_chief.records import add_correction, update_job

    project = _init_repo(tmp_path)
    job = _new_job(project)
    piece_n = _add_piece(project, job["id"])
    wt1, branch1 = _start_native(project, job["id"], piece_n)
    (wt1 / "first.txt").write_text("from attempt 1\n", encoding="utf-8")
    assert _finish(project, job["id"], piece_n) == 0

    update_job(project, job["id"], lambda j: add_correction(j, piece_n, "adjust it"))
    wt2, _ = _start_native(project, job["id"], piece_n)
    assert (wt2 / "first.txt").read_text(encoding="utf-8") == "from attempt 1\n"
    attempt = _load_attempt(project, job["id"], piece_n)
    assert attempt["base"] == {"attempt": 1, "branch": branch1}

    (wt2 / "second.txt").write_text("from attempt 2\n", encoding="utf-8")
    assert _finish(project, job["id"], piece_n) == 0
    files = [r["file"] for r in _load_attempt(project, job["id"], piece_n)["diffstat"]]
    assert files == ["second.txt"]


def test_native_fresh_and_explicit_base(tmp_path: Path) -> None:
    project = _init_repo(tmp_path)
    job = _new_job(project)
    piece_n = _add_piece(project, job["id"])
    wt1, _ = _start_native(project, job["id"], piece_n)
    (wt1 / "first.txt").write_text("one\n", encoding="utf-8")
    assert _finish(project, job["id"], piece_n) == 0

    base = ["piece", "start", "--project", str(project), "--job", job["id"],
            "--piece", str(piece_n), "--native", "--coordinator", "claude", "--model", "sonnet"]
    assert main([*base, "--base", "1"]) == 0
    assert (Path(_load_attempt(project, job["id"], piece_n)["worktree"]) / "first.txt").exists()
    assert main([*base, "--fresh"]) == 0
    fresh = _load_attempt(project, job["id"], piece_n)
    assert fresh["base"] is None
    assert not (Path(fresh["worktree"]) / "first.txt").exists()
