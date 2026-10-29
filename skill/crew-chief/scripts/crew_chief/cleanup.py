"""Worktree cleanup with archive (T034, US5, FR-026).

Unmerged attempt worktrees are bundled into a git bundle + unified diff in the
central archive before directory removal; active attempts and unpushed branches
remain untouched. Archive failure keeps the worktree/branch intact.
"""

from __future__ import annotations

import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import records, worktree

__all__ = [
    "cleanup_completed_attempts",
]

_INACTIVE_STATUSES = frozenset(
    (
        "ok",
        "no_change",
        "failed",
        "timeout",
        "quota",
        "outside_write",
        "brief_refused",
        "proof_failed",
    )
)


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run git in repo, returning the CompletedProcess."""
    result = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result


def _is_branch_pushed(repo: Path, branch: str) -> bool:
    """Check if the branch exists on origin and has no unpushed local commits."""
    # Check if origin remote exists
    remotes = _git(repo, "remote", check=False).stdout.split()
    if "origin" not in remotes:
        return False

    # Check if origin tracking ref exists
    origin_ref = f"refs/remotes/origin/{branch}"
    check_ref = _git(repo, "rev-parse", "--verify", origin_ref, check=False)
    if check_ref.returncode != 0:
        return False

    # Check for unpushed commits on branch
    unpushed = _git(repo, "rev-list", f"{origin_ref}..{branch}", check=False)
    if unpushed.returncode != 0 or unpushed.stdout.strip():
        return False

    return True


def _find_base(repo: Path, job: dict, attempt: dict, branch: str) -> str:
    """Find a common ancestor base for diffing branch."""
    start_sha = attempt.get("start_sha")
    if isinstance(start_sha, str) and start_sha.strip():
        verify = _git(repo, "rev-parse", "--verify", start_sha.strip(), check=False)
        if verify.returncode == 0:
            mb = _git(repo, "merge-base", branch, start_sha.strip(), check=False)
            if mb.returncode == 0 and mb.stdout.strip():
                return mb.stdout.strip()
            return start_sha.strip()

    for candidate in (
        job.get("base_branch"),
        job.get("job_branch"),
        "origin/main",
        "main",
        "origin/master",
        "master",
        "HEAD",
    ):
        if not candidate or not isinstance(candidate, str):
            continue
        mb = _git(repo, "merge-base", candidate, branch, check=False)
        if mb.returncode == 0 and mb.stdout.strip():
            return mb.stdout.strip()

    # Fallback to root commit
    root = _git(repo, "rev-list", "--max-parents=0", "HEAD", check=False)
    if root.returncode == 0 and root.stdout.strip():
        return root.stdout.strip().split("\n")[-1]
    return ""


def _bundle_branch(repo: Path, branch: str, bundle_path: Path) -> None:
    """Create and verify a git bundle for a single branch."""
    _git(repo, "bundle", "create", str(bundle_path), branch, "--")
    _git(repo, "bundle", "verify", str(bundle_path))
    _git(repo, "bundle", "list-heads", str(bundle_path))


def _create_diff(repo: Path, job: dict, attempt: dict, branch: str, diff_path: Path) -> None:
    """Create a unified diff for branch and write to diff_path."""
    base = _find_base(repo, job, attempt, branch)
    if base:
        result = _git(repo, "diff", f"{base}..{branch}", check=False)
    else:
        result = _git(repo, "diff", branch, check=False)
    diff_path.write_text(result.stdout, encoding="utf-8")


def cleanup_completed_attempts(
    project: Path | None = None,
    archive_dir: Path | None = None,
    job_ids: list[str] | tuple[str, ...] | None = None,
    clock: Any = None,
    *,
    clone: Path | None = None,
    coord_dir: Path | None = None,
) -> dict:
    """Archive and remove completed (non-active, non-accepted) attempt worktrees.

    Args:
        project: Path to the git project / worktree
        archive_dir: Directory to store bundles and diffs
        job_ids: Optional list of job ids to inspect
        clock: Optional clock object with time() method
        clone: Backwards-compatible alias for project
        coord_dir: Backwards-compatible coordinator directory

    Returns:
        Dict with keys: archived, skipped, errors, timestamp
    """
    if project is None and clone is not None:
        project = clone
    if project is None:
        raise ValueError("project must be provided")
    project = Path(project)

    if archive_dir is None:
        if coord_dir is not None:
            archive_dir = coord_dir / "archive"
        else:
            raise ValueError("archive_dir must be provided")
    archive_dir = Path(archive_dir)

    if clock is None:
        clock = type("Clock", (), {"time": lambda self: time.time()})()

    jobs_directory = records.jobs_dir(project)
    if not jobs_directory.is_dir():
        return {
            "archived": 0,
            "skipped": 0,
            "errors": 0,
            "timestamp": _iso(clock.time()),
        }

    jobs: list[dict] = []
    if job_ids:
        for jid in job_ids:
            try:
                jobs.append(records.load_job(project, jid))
            except (records.RecordError, OSError):
                continue
    else:
        for j, _, err in records.iter_jobs(project):
            if j is not None and not err:
                jobs.append(j)

    archived = 0
    skipped = 0
    errors = 0

    candidates: list[tuple[dict, dict, dict, Path, str]] = []

    for job in jobs:
        for piece in job.get("pieces") or []:
            if not isinstance(piece, dict):
                continue
            accepted_info = piece.get("accepted")
            acc_attempt = accepted_info.get("attempt") if isinstance(accepted_info, dict) else None
            acc_branch = accepted_info.get("branch") if isinstance(accepted_info, dict) else None

            for attempt in piece.get("attempts") or []:
                if not isinstance(attempt, dict):
                    continue

                # Check if accepted
                if accepted_info and (
                    attempt.get("n") == acc_attempt or attempt.get("branch") == acc_branch
                ):
                    skipped += 1
                    continue
                if attempt.get("accepted") is True:
                    skipped += 1
                    continue

                # Check status: only known inactive attempts are eligible
                status = attempt.get("status")
                if status in ("running", "queued") or attempt.get("active") is True:
                    skipped += 1
                    continue
                if status not in _INACTIVE_STATUSES:
                    skipped += 1
                    continue

                branch = attempt.get("branch")
                worktree_str = attempt.get("worktree")
                if not branch or not isinstance(branch, str):
                    skipped += 1
                    continue
                if not worktree_str or not isinstance(worktree_str, str):
                    skipped += 1
                    continue

                worktree_path = Path(worktree_str)
                if not worktree_path.exists():
                    skipped += 1
                    continue

                candidates.append((job, piece, attempt, worktree_path, branch))

    if not candidates:
        return {
            "archived": 0,
            "skipped": skipped,
            "errors": 0,
            "timestamp": _iso(clock.time()),
        }

    archive_dir.mkdir(parents=True, exist_ok=True)

    for job, piece, attempt, worktree_path, branch in candidates:
        # Check if unpushed
        if not _is_branch_pushed(project, branch):
            skipped += 1
            continue

        safe_branch = branch.replace("/", "_").replace("\\", "_")
        bundle_path = archive_dir / f"{safe_branch}.bundle"
        diff_path = archive_dir / f"{safe_branch}.diff"

        created_bundle = False
        created_diff = False

        try:
            # Commit uncommitted work in the worktree before archiving/pruning
            worktree.commit_worktree(
                worktree_path, f"archive uncommitted work for {branch}"
            )

            # Create unified diff
            _create_diff(project, job, attempt, branch, diff_path)
            created_diff = True

            # Create bundle
            _bundle_branch(project, branch, bundle_path)
            created_bundle = True

            # Canonical worktree removal (removes directory and git worktree registry entry)
            worktree._remove_worktree(project, worktree_path)

            archived += 1

        except Exception:
            errors += 1
            if created_bundle and bundle_path.exists():
                try:
                    bundle_path.unlink()
                except OSError:
                    pass
            if created_diff and diff_path.exists():
                try:
                    diff_path.unlink()
                except OSError:
                    pass

    return {
        "archived": archived,
        "skipped": skipped,
        "errors": errors,
        "timestamp": _iso(clock.time()),
    }
