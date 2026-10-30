"""Git delivery workflow for job branches and PRs.

T017 / US8 / FR-027: git fetch origin preflight and job branch creation.
Before creating the acc/<job-id> branch, ACC must run `git fetch origin`
to ensure the branch is rooted at the latest origin/<base_branch>.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

__all__ = ["prepare_job_branch", "push_to_origin", "create_pr", "create_pr_review_comment", "handle_git_failure"]

#: ``[text](file:///...)``: a link to a local worktree, dead for PR readers.
_FILE_MD_LINK = re.compile(r"\[([^\]]*)\]\(file://[^)]*\)")
#: The scheme of a bare ``file://`` URI (keeps a Windows drive path as ``C:/...``).
_FILE_SCHEME = re.compile(r"file:///(?=[A-Za-z]:)|file://")


def _proof_lines(repo: Path, job_id: str) -> list[str]:
    """One line per accepted attempt with a recorded proof; ``[]`` without a job."""
    from . import records
    from .cli_dispatch import _proof_text

    try:
        job = records.load_job(repo, job_id)
    except Exception:
        return []
    lines = []
    for piece in job.get("pieces") or []:
        accepted = piece.get("accepted")
        if not isinstance(accepted, dict):
            continue
        attempt = next(
            (a for a in piece.get("attempts") or [] if a.get("n") == accepted.get("attempt")),
            {},
        )
        proof = attempt.get("proof")
        if isinstance(proof, dict):
            cmd = f" `{proof['cmd']}`" if proof.get("cmd") else ""
            lines.append(f"- Piece {piece.get('n')} proof{cmd}: {_proof_text(proof)}")
    return lines


def handle_git_failure(
    repo_path: Path | str,
    job_id: str,
    exc: subprocess.CalledProcessError,
    summary: str = "",
    pr_url: str | None = None,
) -> tuple[str, str]:
    """Handle git push or gh pr create failure: transition to blocked, log command and stderr, send notice (FR-030).

    Args:
        repo_path: Path to the project repository.
        job_id: Unique job identifier.
        exc: The CalledProcessError raised by git push or gh pr create.
        summary: Optional summary of the job.
        pr_url: Optional PR URL if known.

    Returns:
        tuple[str, str]: (failed_command_str, stderr_text)
    """
    from datetime import datetime, timezone
    from . import records

    cmd_str = " ".join(exc.cmd) if isinstance(exc.cmd, list) else str(exc.cmd)
    stderr_text = exc.stderr or ""

    records.transition_terminal_state(
        project=repo_path,
        job_id=job_id,
        new_state="blocked",
        extra_fields={
            "command": cmd_str,
            "error": stderr_text,
            "failed_command": cmd_str,
            "blocked_error": stderr_text,
            "pr_url": pr_url,
            "result": {
                "failed_command": cmd_str,
                "blocked_error": stderr_text,
                "summary": summary or "",
            },
        },
    )

    return cmd_str, stderr_text



def _run_git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a git command in the given directory."""
    return subprocess.run(
        ["git"] + list(args),
        cwd=cwd,
        capture_output=True,
        text=True,
        check=check,
    )


def _may_switch_from(repo: Path, branch: str, base_branch: str) -> bool:
    """True for the base, a default branch, origin's HEAD or another ACC job branch."""
    if branch in (base_branch, "main", "master") or branch.startswith("acc/"):
        return True
    head = _run_git(repo, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD", check=False)
    return head.returncode == 0 and head.stdout.strip() == f"origin/{branch}"


def prepare_job_branch(repo_path: Path | str, job_id: str, base_branch: str = "main") -> str:
    """Fetch from origin and create a new job branch rooted at origin/<base_branch>.

    The checkout only moves when it sits on a base-like branch (where ACC
    never commits), another ``acc/`` job branch or a detached HEAD. A
    checkout already on a feature branch keeps it: that branch becomes the
    job branch, so the user's work is never switched away from under them.

    Args:
        repo_path: Path to the git repository.
        job_id: Unique job identifier (e.g., "test-job-123").
        base_branch: Name of the base branch to branch from (default "main").

    Returns:
        The name of the created job branch (e.g., "acc/test-job-123").

    Raises:
        subprocess.CalledProcessError: If any git command fails.
    """
    repo = Path(repo_path)
    branch_name = f"acc/{job_id}"

    # T017: Fetch from origin before creating the branch
    _run_git(repo, "fetch", "origin")

    current = _run_git(repo, "branch", "--show-current", check=False).stdout.strip()
    if current and not _may_switch_from(repo, current, base_branch):
        return current

    # Create and checkout the job branch from origin/<base_branch>
    _run_git(repo, "checkout", "-b", branch_name, f"origin/{base_branch}")

    return branch_name


def push_to_origin(
    repo_path: Path | str,
    branch_name: str,
    base_branch: str = "main",
) -> None:
    """Push a single job branch to origin without --force or tags.

    Args:
        repo_path: Path to the git repository.
        branch_name: Name of the branch to push (e.g., "acc/test-job-123").
        base_branch: Name of the repository base branch (default "main"). Direct
            pushes to base branches are strictly forbidden.

    Raises:
        ValueError: If branch_name is empty, contains flags, is a tag ref, or matches base_branch.
        subprocess.CalledProcessError: If the git push fails.
    """
    if not branch_name or not isinstance(branch_name, str):
        raise ValueError("Invalid branch name: must be a non-empty string")

    clean_branch = branch_name.strip()

    # Forbid flags like --force, -f, --tags, etc.
    if any(part.startswith("-") for part in clean_branch.split()):
        raise ValueError(f"Pushing flags is forbidden: '{branch_name}'")

    # Forbid tags
    if clean_branch.startswith("refs/tags/") or clean_branch.startswith("tags/"):
        raise ValueError(f"Pushing tags is forbidden: '{branch_name}'")

    # Check against base branch
    normalized_base = base_branch.strip().removeprefix("refs/heads/")
    normalized_target = clean_branch.removeprefix("refs/heads/")

    if normalized_target == normalized_base:
        raise ValueError(f"Pushing to base branch '{branch_name}' is forbidden")

    repo = Path(repo_path)

    # Push only the specified branch, no force, no tags
    _run_git(repo, "push", "origin", clean_branch)


def _verify_sdd_branch_contents(
    repo: Path,
    branch_name: str,
    base_branch: str,
    spec_dir: str | Path | None = None,
) -> None:
    """Verify that a Modern SDD job's PR branch actually contains reviewed specs and code changes.

    Raises:
        ValueError: If required spec files or implementation changes are missing from the branch.
    """
    clean_branch = branch_name.strip()
    clean_base = base_branch.strip()

    # 1. Check committed files on the branch via ls-tree
    ls_proc = _run_git(repo, "ls-tree", "-r", clean_branch, "--name-only", check=False)
    if ls_proc.returncode != 0:
        raise ValueError(f"PR branch '{clean_branch}' cannot be inspected: {ls_proc.stderr.strip()}")

    branch_files = [line.strip() for line in ls_proc.stdout.splitlines() if line.strip()]

    # Verify specification files exist committed on the branch
    if spec_dir:
        spec_prefix = str(spec_dir).replace("\\", "/").strip("/")
        if not spec_prefix.startswith("specs/"):
            spec_prefix = f"specs/{spec_prefix}"
        has_specs = any(
            f.startswith(f"{spec_prefix}/") or f == spec_prefix
            for f in branch_files
        )
    else:
        has_specs = any(
            f.startswith("specs/") and (f.endswith(".md") or "/spec.md" in f or f.endswith("spec.md"))
            for f in branch_files
        )

    if not has_specs:
        raise ValueError(
            f"Modern SDD job PR branch '{clean_branch}' does not contain required specification files under 'specs/'"
        )

    # 2. Verify implementation changes exist on the branch compared to base_branch
    diff_files: list[str] = []
    candidates = [clean_base, f"origin/{clean_base}", f"refs/heads/{clean_base}"]
    for candidate in candidates:
        diff_proc = _run_git(repo, "diff", "--name-only", f"{candidate}...{clean_branch}", check=False)
        if diff_proc.returncode == 0:
            diff_files = [line.strip() for line in diff_proc.stdout.splitlines() if line.strip()]
            break
        diff_proc2 = _run_git(repo, "diff", "--name-only", f"{candidate}..{clean_branch}", check=False)
        if diff_proc2.returncode == 0:
            diff_files = [line.strip() for line in diff_proc2.stdout.splitlines() if line.strip()]
            break

    if diff_files:
        has_code = any(
            not f.startswith("specs/") and not f.startswith(".git")
            for f in diff_files
        )
    else:
        # Fallback to inspecting all branch files in ls-tree
        has_code = any(
            not f.startswith("specs/") and not f.startswith(".git")
            for f in branch_files
        )

    if not has_code:
        raise ValueError(
            f"Modern SDD job PR branch '{clean_branch}' does not contain implementation changes"
        )


def create_pr(
    repo_path: Path | str,
    job_id: str,
    branch_name: str,
    base_branch: str = "main",
    accepted_pieces: list[dict] | None = None,
    spec_dir: str | Path | None = None,
    is_sdd: bool | None = None,
) -> str:
    """Create exactly one pull request via gh pr create with audit summary.

    Args:
        repo_path: Path to the git repository.
        job_id: Unique job identifier.
        branch_name: Name of the branch to create PR from.
        base_branch: Base branch for the PR (default "main").
        accepted_pieces: List of accepted piece dicts with keys: n, title, status,
            executor_grade, verification, and optionally type.
        spec_dir: Optional spec directory path for SDD jobs.
        is_sdd: Optional flag indicating whether Modern SDD verification applies.

    Returns:
        The PR URL (e.g., "https://github.com/test/repo/pull/1").

    Raises:
        ValueError: If Modern SDD job branch lacks required specs or implementation changes.
        subprocess.CalledProcessError: If gh pr create fails unexpectedly.
    """
    repo = Path(repo_path)
    accepted_pieces = accepted_pieces or []

    # Determine whether Modern SDD verification applies
    if is_sdd is None:
        if spec_dir:
            is_sdd = True
        elif any(p.get("type") == "spec" for p in accepted_pieces):
            is_sdd = True
        else:
            try:
                from . import records
                job = records.load_job(repo, job_id)
                if job.get("spec_dir") or job.get("spec_path") or job.get("phase") == "spec":
                    is_sdd = True
                    if not spec_dir:
                        spec_dir = job.get("spec_dir") or job.get("spec_path")
                elif (job.get("authorization") or {}).get("phase") == "spec" or (job.get("authorization") or {}).get("spec_path"):
                    is_sdd = True
                    if not spec_dir:
                        spec_dir = (job.get("authorization") or {}).get("spec_path")
                elif any(p.get("type") == "spec" for p in (job.get("pieces") or [])):
                    is_sdd = True
            except Exception:
                pass

    if is_sdd:
        _verify_sdd_branch_contents(
            repo,
            branch_name,
            base_branch=base_branch,
            spec_dir=spec_dir,
        )

    # Build audit summary body
    body_lines = [
        f"# Job {job_id} - Audit Summary",
        "",
        "## Accepted Pieces",
        "",
    ]

    for piece in accepted_pieces:
        body_lines.append(f"- **Piece {piece['n']}**: {piece['title']}")
        if piece.get("type"):
            body_lines.append(f"  - Type: {piece['type']}")
        body_lines.append(f"  - Status: {piece.get('status', 'N/A')}")
        body_lines.append(f"  - Executor Grade: {piece.get('executor_grade', 'N/A')}")
        body_lines.append(f"  - Verification: {piece.get('verification', 'N/A')}")
        body_lines.append("")

    body_lines.extend([
        "## Verification Proofs",
        "",
        *_proof_lines(repo, job_id),
        "- No force pushes used",
        "- Single PR per job enforced",
        "",
    ])

    body = "\n".join(body_lines)
    body = _FILE_SCHEME.sub("", _FILE_MD_LINK.sub(r"\1", body))

    # Check if PR already exists for this branch
    try:
        result_check = subprocess.run(
            ["gh", "pr", "list", "--head", branch_name, "--state", "open", "--json", "url"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )
        if result_check.returncode == 0 and result_check.stdout.strip():
            import json
            prs = json.loads(result_check.stdout)
            if prs:
                return prs[0]["url"]
    except Exception:
        pass

    # Create PR via gh pr create
    result = subprocess.run(
        [
            "gh",
            "pr",
            "create",
            "--base",
            base_branch,
            "--head",
            branch_name,
            "--title",
            f"Job {job_id}: Complete",
            "--body",
            body,
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )

    # Extract PR URL from output
    pr_url = result.stdout.strip()
    return pr_url


def create_pr_review_comment(
    repo_path: Path | str,
    job_id: str,
    branch_name: str,
    base_branch: str = "main",
    reviewer_model: str = "",
    coordinator_model: str = "",
    executor_models: list[str] | None = None,
    review_text: str = "",
) -> str:
    """Post a PR review comment from a reviewer model different from coordinator/executors.

    Args:
        repo_path: Path to the git repository.
        job_id: Unique job identifier.
        branch_name: Name of the branch the PR is from.
        base_branch: Base branch for the PR (default "main").
        reviewer_model: Model name of the reviewer.
        coordinator_model: Model name of the coordinator.
        executor_models: List of executor model names used in the job.
        review_text: Optional text of the review generated by the reviewer model.

    Returns:
        The PR URL.

    Raises:
        ValueError: If reviewer model matches coordinator or any executor, or if identities are missing.
        subprocess.CalledProcessError: If gh commands fail.
    """
    repo = Path(repo_path)
    executor_models = executor_models or []

    # Empty/missing identity must never bypass distinctness
    if not reviewer_model or not str(reviewer_model).strip():
        raise ValueError("Reviewer model identity must be non-empty")
    if not coordinator_model or not str(coordinator_model).strip():
        raise ValueError("Coordinator model identity must be non-empty")
    if any(e is None or not str(e).strip() for e in executor_models):
        raise ValueError("Executor model list contains empty identity; cannot verify distinctness")

    clean_reviewer = str(reviewer_model).strip()
    clean_coordinator = str(coordinator_model).strip()
    clean_executors = [str(e).strip() for e in executor_models]

    def _norm(s: str) -> str:
        if s.startswith("native:"):
            rest = s[len("native:"):]
            if "/" in rest:
                cli, model = rest.split("/", 1)
                return f"{cli}:{model}"
        return s

    def _matches_identity(a: str, b: str) -> bool:
        a = _norm(a)
        b = _norm(b)
        if a == b:
            return True
        if ":" in a and ":" not in b:
            return a.split(":", 1)[1] == b
        if ":" not in a and ":" in b:
            return a == b.split(":", 1)[1]
        return False

    # Verify reviewer is different from coordinator and all executors
    if _matches_identity(clean_reviewer, clean_coordinator):
        raise ValueError(f"Reviewer model '{clean_reviewer}' must differ from coordinator model '{clean_coordinator}'")

    for ex in clean_executors:
        if _matches_identity(clean_reviewer, ex):
            raise ValueError(f"Reviewer model '{clean_reviewer}' must differ from all executor models: {executor_models}")

    # Get PR number via gh pr list
    result_list = subprocess.run(
        ["gh", "pr", "list", "--head", branch_name, "--state", "open", "--json", "url,number"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    import json
    prs = json.loads(result_list.stdout)
    if not prs:
        raise RuntimeError(f"No open PR found for branch {branch_name}")
    pr_number = prs[0]["number"]
    pr_url = prs[0]["url"]

    # Build review comment body
    body_header = f"## PR Review\n\nReviewed by: {clean_reviewer}\n"
    if review_text and review_text.strip():
        body = f"{body_header}\n{review_text.strip()}"
    else:
        body = f"{body_header}\nThis pull request has been reviewed by an independent model different from the coordinator ({clean_coordinator}) and all executors used in this job."

    # Post review comment using gh pr review with --comment and --body (not --approve)
    subprocess.run(
        [
            "gh",
            "pr",
            "review",
            str(pr_number),
            "--comment",
            "--body",
            body,
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )

    return pr_url