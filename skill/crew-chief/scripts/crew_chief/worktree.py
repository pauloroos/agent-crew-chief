"""Worktrees, containment snapshots and diff stats.

Every attempt runs in a throwaway git worktree next to the project
(``<project>.crew/<job_id>-<piece>-<attempt>``) on a branch
``crew/<job_id>-<piece>-<attempt>`` created from ``HEAD``. This module owns
that worktree's whole life cycle and the two containment checks that decide
whether an attempt is allowed to land:

* :func:`snapshot` + :func:`outside_writes` compare the main repo, the
  worktree's sibling directory and the top level of the home directory before
  and after an attempt, so a write outside the worktree is caught. Files that
  were already dirty in the main repo appear in both snapshots and are ignored.
  The sibling directory is ``<project>.crew/`` itself, so the worktrees of
  parallel attempts living there are not writes; neither are the entries agent
  CLIs keep in the home directory (:data:`AGENT_HOME_ENTRIES`).
* :func:`diffstat` reports what the attempt changed and calls out deletions
  that look destructive, so a merge can be refused before it happens.

Everything here shells out to ``git`` through the single helper :func:`_git`;
nothing else is imported from the package.
"""

from __future__ import annotations

import fnmatch
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

#: Files the coordinator writes inside the worktree; they never count as work.
EXCLUDED_FILES: tuple[str, ...] = (".crew-chief-brief.md", ".crew-chief-out.txt")

#: Top-level home entries that agent CLIs write on every run (config, auth,
#: caches, session state) plus OS noise; fnmatch patterns, matched without
#: regard to case on Windows. This is the one place the default list lives;
#: the ``ignore_home_entries`` preference adds to it.
AGENT_HOME_ENTRIES: tuple[str, ...] = (
    # Claude Code (also behind antigravity and Claude-like proxies).
    ".claude",
    ".claude.json",
    ".claude.json.*",
    # Codex.
    ".codex",
    # Gemini / Antigravity.
    ".gemini",
    ".antigravity",
    ".antigravity-ide",
    # OpenCode: .local/share/opencode, .config/opencode, .cache/opencode.
    ".opencode",
    ".local",
    ".config",
    ".cache",
    # Cursor agent.
    ".cursor",
    # Package caches the CLIs fill when they install helpers.
    ".npm",
    ".bun",
    # Windows registry hive of the user, rewritten by any process.
    "NTUSER.DAT*",
    "ntuser.dat*",
    "ntuser.ini",
)

#: Directory names :func:`create_worktree` gives attempts:
#: ``<job_id>-<piece>-<attempt>`` with a job id ``<YYYYmmdd-HHMM>-<slug>[-N]``.
_WORKTREE_NAME = re.compile(r"^\d{8}-\d{4}-[a-z0-9-]+-\d+-\d+$")

#: A deletion is called destructive when it removes many lines and mostly
#: deletes: at least this many lines gone with more than twice the added lines.
_DESTRUCTIVE_MIN_DELETED = 30
_DESTRUCTIVE_RATIO = 2


class NotAGitRepo(Exception):
    """Raised when the project to contain is not a git repository."""


def _git(args: list[str], cwd: Path, check: bool = True) -> str:
    """Run ``git <args>`` in ``cwd`` and return its stdout.

    Captured as text with utf-8. With ``check=True`` a non-zero exit raises
    :class:`RuntimeError` carrying git's stderr; with ``check=False`` the
    stdout is returned whatever the exit status.
    """
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed in {cwd}: {result.stderr.strip()}"
        )
    return result.stdout


def create_worktree(
    project: Path, job_id: str, piece: int, attempt: int
) -> tuple[Path, str]:
    """Create the worktree for one attempt and return ``(path, branch)``.

    The branch is ``crew/<job_id>-<piece>-<attempt>``, created from ``HEAD``,
    and the worktree lives in ``<project.parent>/<project.name>.crew/``. The
    two coordinator files are added to the worktree's own ``info/exclude`` so
    they never show up in ``git status``. Raises :class:`NotAGitRepo` when
    ``project`` is not a git repository, and :class:`RuntimeError` when the
    target path is already taken.
    """
    project = Path(project)
    try:
        _git(["rev-parse", "--show-toplevel"], project)
    except (RuntimeError, OSError) as exc:
        raise NotAGitRepo(f"not a git repository: {project}") from exc

    branch = f"crew/{job_id}-{piece}-{attempt}"
    worktree = project.parent / f"{project.name}.crew" / f"{job_id}-{piece}-{attempt}"
    if worktree.exists():
        raise RuntimeError(f"worktree path already exists: {worktree}")

    worktree.parent.mkdir(parents=True, exist_ok=True)
    _git(["worktree", "add", "-b", branch, str(worktree), "HEAD"], project)
    _exclude_coordinator_files(worktree)
    return worktree, branch


def snapshot(
    project: Path, worktree: Path, ignore_home: list[str] | tuple[str, ...] = ()
) -> dict:
    """A containment snapshot: main repo status, siblings and home entries.

    ``repo`` is the set of ``git status --porcelain=v1 -uall`` lines of the
    main repo; ``sibling`` and ``home`` map each top-level entry name to its
    ``st_mtime_ns``, for ``worktree.parent`` and for ``Path.home()``. Nothing
    is walked recursively.

    ``sibling`` leaves out the worktree itself and every other worktree of the
    project (registered in ``git worktree list`` or named like one
    :func:`create_worktree` makes), so parallel attempts do not see each
    other. ``home`` leaves out :data:`AGENT_HOME_ENTRIES` plus the fnmatch
    patterns in ``ignore_home``.
    """
    project = Path(project)
    worktree = Path(worktree)
    status = _git(["status", "--porcelain=v1", "-uall"], project)
    registered = _registered_worktree_names(project, worktree.parent)
    siblings = {
        name: mtime
        for name, mtime in _entry_mtimes(worktree.parent).items()
        if name != worktree.name
        and name not in registered
        and not _WORKTREE_NAME.match(name)
    }
    patterns = (*AGENT_HOME_ENTRIES, *ignore_home)
    home = {
        name: mtime
        for name, mtime in _entry_mtimes(Path.home()).items()
        if not any(fnmatch.fnmatch(name, pattern) for pattern in patterns)
    }
    return {
        "repo": set(status.splitlines()),
        "sibling": siblings,
        "home": home,
    }


def _registered_worktree_names(project: Path, directory: Path) -> set[str]:
    """Names of the project's registered worktrees that live in ``directory``."""
    target = os.path.normcase(str(Path(directory).resolve()))
    names: set[str] = set()
    listing = _git(["worktree", "list", "--porcelain"], project, check=False)
    for line in listing.splitlines():
        if not line.startswith("worktree "):
            continue
        path = Path(line[len("worktree "):])
        try:
            parent = os.path.normcase(str(path.resolve().parent))
        except OSError:
            continue
        if parent == target:
            names.add(path.name)
    return names


def outside_writes(before: dict, after: dict) -> list[str]:
    """Entries new or changed between two snapshots, sorted and labelled.

    Each entry reads ``"repo: <porcelain line>"``, ``"sibling: <name>"`` or
    ``"home: <name>"``. Status lines present in both snapshots are ignored,
    which is what keeps files that were already dirty from counting.
    """
    writes: set[str] = set()

    before_repo = set(before.get("repo", ()))
    for line in after.get("repo", ()):
        if line not in before_repo:
            writes.add(f"repo: {line}")

    for kind in ("sibling", "home"):
        before_entries = before.get(kind, {})
        for name, mtime_ns in after.get(kind, {}).items():
            if before_entries.get(name) != mtime_ns:
                writes.add(f"{kind}: {name}")

    return sorted(writes)


def diffstat(worktree: Path, base: str = "HEAD") -> tuple[list[dict], list[str]]:
    """Diff of the worktree against the merge-base with ``base``.

    Returns ``(rows, destructive_suspect)``; each row is
    ``{"file", "added", "deleted"}`` and covers tracked changes plus untracked
    files (counted as added lines). ``destructive_suspect`` lists files deleted
    outright and files with ``deleted >= 30 and deleted > 2 * added``. Nothing
    is committed; run this before :func:`commit_worktree`.
    """
    worktree = Path(worktree)
    merge_base = _git(["merge-base", "HEAD", base], worktree).strip()

    rows: list[dict] = []
    for line in _git(["diff", "--numstat", merge_base], worktree).splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        rows.append(
            {
                "file": "\t".join(parts[2:]),
                "added": _as_int(parts[0]),
                "deleted": _as_int(parts[1]),
            }
        )

    untracked = _git(["ls-files", "--others", "--exclude-standard", "-z"], worktree)
    for name in untracked.split("\0"):
        if not name:
            continue
        rows.append(
            {
                "file": name,
                "added": _count_lines(worktree / name),
                "deleted": 0,
            }
        )

    destructive = [
        row["file"]
        for row in rows
        if row["deleted"] > 0
        and (
            row["added"] == 0
            or (
                row["deleted"] >= _DESTRUCTIVE_MIN_DELETED
                and row["deleted"] > _DESTRUCTIVE_RATIO * row["added"]
            )
        )
    ]
    return rows, destructive


def commit_worktree(worktree: Path, message: str) -> str | None:
    """Stage everything in the worktree and commit it.

    Returns the new commit sha, or ``None`` when there was nothing to commit.
    """
    worktree = Path(worktree)
    _git(["add", "-A"], worktree)
    if not _git(["status", "--porcelain"], worktree).strip():
        return None
    _git(["commit", "-m", message], worktree)
    return _git(["rev-parse", "HEAD"], worktree).strip()


def accept(project: Path, worktree: Path, branch: str) -> None:
    """Merge the worktree's branch into ``project``, then remove both.

    A merge conflict aborts the merge, raises :class:`RuntimeError` and leaves
    the worktree and the branch in place for inspection.
    """
    project = Path(project)
    worktree = Path(worktree)
    try:
        _git(["merge", "--no-ff", "-m", f"merge {branch}", branch], project)
    except RuntimeError:
        _git(["merge", "--abort"], project, check=False)
        raise
    _remove_worktree(project, worktree)
    _git(["branch", "-D", branch], project)


def discard(project: Path, worktree: Path, branch: str) -> None:
    """Remove the worktree and its branch without merging anything."""
    project = Path(project)
    _remove_worktree(project, Path(worktree))
    _git(["branch", "-D", branch], project, check=False)


def _exclude_coordinator_files(worktree: Path) -> None:
    """Add :data:`EXCLUDED_FILES` to the worktree's ``info/exclude``."""
    raw = _git(["rev-parse", "--git-path", "info/exclude"], worktree).strip()
    exclude = Path(raw)
    if not exclude.is_absolute():
        exclude = worktree / exclude
    lines = (
        exclude.read_text(encoding="utf-8").splitlines() if exclude.exists() else []
    )
    for entry in EXCLUDED_FILES:
        if entry not in lines:
            lines.append(entry)
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _remove_worktree(project: Path, worktree: Path) -> None:
    """Remove the worktree, falling back to rmtree when git cannot.

    On Windows a read-only file can make ``git worktree remove`` fail; then the
    directory is emptied by hand (unlocking files as needed) and the stale
    administrative entry is pruned.
    """
    result = subprocess.run(
        ["git", "worktree", "remove", "--force", str(worktree)],
        cwd=project,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode == 0:
        return
    if worktree.exists():
        _remove_tree(worktree)
    _git(["worktree", "prune"], project, check=False)


def _remove_tree(path: Path) -> None:
    """``shutil.rmtree`` that unlocks read-only entries before retrying."""

    def unlock(func, target, _exc):  # pragma: no cover - exercised on Windows
        os.chmod(target, stat.S_IWRITE)
        func(target)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=unlock)
    else:  # pragma: no cover - Python 3.11 only
        shutil.rmtree(path, onerror=unlock)


def _entry_mtimes(directory: Path) -> dict[str, int]:
    """``name -> st_mtime_ns`` for the top-level entries of ``directory``."""
    entries: dict[str, int] = {}
    if not directory.is_dir():
        return entries
    for entry in directory.iterdir():
        try:
            entries[entry.name] = entry.stat().st_mtime_ns
        except OSError:
            continue
    return entries


def _as_int(value: str) -> int:
    """A ``--numstat`` count as int; ``-`` (binary file) counts as zero."""
    return int(value) if value.isdigit() else 0


def _count_lines(path: Path) -> int:
    """Number of lines in a file, counted on bytes so binary files are safe."""
    try:
        data = path.read_bytes()
    except OSError:
        return 0
    if not data:
        return 0
    count = data.count(b"\n")
    if not data.endswith(b"\n"):
        count += 1
    return count
