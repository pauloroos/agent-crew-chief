"""Long-lived coordinator worktrees.

A coordinator gets its own worktree ``<repo-name>.crew/central-<uuid>`` on the
branch ``central/<uuid>``, created from an explicit base (``HEAD`` by default).
Unlike an executor attempt these outlive the launch: nothing here merges,
pushes, deletes or cleans them, and the source checkout's dirty files are
never copied into the worktree. Callers register the returned paths in the
coordinator registry.
"""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

#: Longest stderr excerpt quoted in an error, so diagnostics stay bounded.
_STDERR_MAX = 500


class WorktreeError(Exception):
    """A coordinator worktree cannot be created; the message says why."""


def create(repo: Path, coordinator_id: str, base: str = "HEAD") -> dict[str, str]:
    """Create the coordinator's branch and worktree and describe them.

    ``coordinator_id`` must be a canonical UUID string (the registry's rule).
    The branch is ``central/<uuid>`` and the worktree lives in
    ``<repo-root>.crew/central-<uuid>`` next to the repository. ``base`` is any
    revision git can resolve to a commit; it defaults to ``HEAD``, which must
    exist. Dirty files of the source checkout stay where they are.

    Returns ``{"coordinator_id", "repo", "worktree", "branch", "base_commit"}``.
    Raises :class:`WorktreeError` when the repository or base is invalid or the
    branch or worktree path already exists; an existing path is never removed.
    """
    canonical = _canonical_uuid(coordinator_id)
    if canonical is None:
        raise WorktreeError(
            f"coordinator_id must be a canonical UUID string, got {coordinator_id!r}"
        )

    root = _repo_root(Path(repo))
    base_commit = _resolve_commit(root, base)
    branch = f"central/{canonical}"
    worktree = root.parent / f"{root.name}.crew" / f"central-{canonical}"

    if _branch_exists(root, branch):
        raise WorktreeError(f"branch already exists: {branch}")
    if worktree.exists():
        raise WorktreeError(f"worktree path already exists: {worktree}")

    worktree.parent.mkdir(parents=True, exist_ok=True)
    _git(root, "worktree", "add", "-b", branch, str(worktree), base_commit)
    return {
        "coordinator_id": canonical,
        "repo": str(root),
        "worktree": str(worktree),
        "branch": branch,
        "base_commit": base_commit,
    }


def _canonical_uuid(value: object) -> str | None:
    """``value`` in canonical UUID form, or ``None`` when it is not one."""
    if not isinstance(value, str):
        return None
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return None
    return str(parsed) if str(parsed) == value else None


def _repo_root(repo: Path) -> Path:
    """The worktree root of ``repo`` (a subfolder resolves to its root)."""
    try:
        output = _git(repo, "rev-parse", "--show-toplevel")
    except WorktreeError as exc:
        raise WorktreeError(f"not a git repository: {repo}") from exc
    root = Path(output.strip())
    if not root.is_absolute():
        raise WorktreeError(f"not a git repository: {repo}")
    return root


def _resolve_commit(root: Path, base: str) -> str:
    """The commit sha ``base`` names; raises when it cannot be resolved."""
    result = _run(
        root,
        "rev-parse",
        "--verify",
        "--quiet",
        "--end-of-options",
        f"{base}^{{commit}}",
    )
    sha = result.stdout.strip()
    if result.returncode != 0 or not sha:
        raise WorktreeError(f"base revision does not resolve to a commit: {base!r}")
    return sha


def _branch_exists(root: Path, branch: str) -> bool:
    """Whether ``branch`` is an existing local branch of ``root``."""
    return _run(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}").returncode == 0


def _git(repo: Path, *args: str) -> str:
    """Run ``git <args>`` in ``repo`` and return stdout; failure raises."""
    result = _run(repo, *args)
    if result.returncode != 0:
        raise WorktreeError(_command_error(args, result.stderr))
    return result.stdout


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run ``git <args>`` in ``repo`` with captured output and no shell."""
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _command_error(args: tuple[str, ...], stderr: str) -> str:
    """A bounded diagnostic for a failed git command."""
    detail = stderr.strip()[:_STDERR_MAX]
    return f"git {' '.join(args)} failed: {detail}" if detail else f"git {' '.join(args)} failed"
