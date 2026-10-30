"""Worktrees, containment snapshots and diff stats.

Every attempt runs in a throwaway git worktree next to the project
(``<project>.crew/<job_id>-<piece>-<attempt>``) on a branch
``crew/<job_id>-<piece>-<attempt>`` created from ``HEAD`` (or, for a
correction, from the branch of an earlier attempt). This module owns
that worktree's whole life cycle and the two containment checks that decide
whether an attempt is allowed to land:

* :func:`snapshot` + :func:`outside_writes` compare the main repo, the
  worktree's sibling directory and the top level of the home directory before
  and after an attempt, so a write outside the worktree is caught. Files that
  were already dirty in the main repo appear in both snapshots and are ignored.
  The sibling directory is ``<project>.crew/`` itself, so the worktrees of
  parallel attempts living there are not writes; neither are the entries agent
  CLIs keep in the home directory (:data:`AGENT_HOME_ENTRIES`), nor the
  records parallel crew-chief commands rewrite in the main repo
  (:data:`COORDINATOR_RECORDS`).
* :func:`hide_sensitive` and :func:`sensitive_present` keep the project's
  ``sensitive_paths`` out of an attempt's worktree: the tracked ones are
  removed right after checkout (and marked ``skip-worktree`` so the removal
  never shows as a deletion), and whatever sensitive path exists in the
  worktree after the run is found so the caller can delete it.
* :func:`diffstat` reports what the attempt changed and calls out deletions
  that look destructive, so a merge can be refused before it happens.

Everything here shells out to ``git`` through the single helper :func:`_git`;
the only import from the package is the path matcher of :mod:`briefcheck`.
"""

from __future__ import annotations

import fnmatch
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

from .briefcheck import path_is_sensitive

#: Files the coordinator writes inside the worktree; they never count as work.
EXCLUDED_FILES: tuple[str, ...] = (".crew-chief-brief.md", ".crew-chief-out.txt")

#: Paths in the main repo that crew-chief commands themselves rewrite (a
#: trailing ``/`` covers a folder): parallel dispatches of the same project
#: update the job records, their lock files and the registries while an
#: attempt runs, so the containment check never counts them.
COORDINATOR_RECORDS: tuple[str, ...] = (
    "docs/crew-chief/jobs/",
    "docs/crew-chief/models-seen.json",
    "docs/crew-chief/scoreboard.md",
)

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
    # Docker Desktop: it updates its own home entry (contexts, build
    # metadata) independently of any attempt.
    ".docker",
    # Package caches the CLIs fill when they install helpers, and the build
    # caches of the toolchains an attempt runs (cargo fills ~/.cargo on any
    # Rust build).
    ".npm",
    ".bun",
    ".yarn",
    ".pnpm-store",
    ".cargo",
    ".rustup",
    ".gradle",
    ".m2",
    ".nuget",
    # ACC state (executor leases, daemon caches, etc.)
    ".acc",
    # Windows registry hive of the user, rewritten by any process.
    "NTUSER.DAT*",
    "ntuser.dat*",
    "ntuser.ini",
    # Windows shell folders the system updates on its own (recent files,
    # app data of every program).
    "Recent",
    "AppData",
)

#: Directory names :func:`create_worktree` gives attempts:
#: ``<job_id>-<piece>-<attempt>`` with a job id ``<YYYYmmdd-HHMM>-<slug>[-N]``.
_WORKTREE_NAME = re.compile(r"^\d{8}-\d{4}-[a-z0-9-]+-\d+-\d+$")

#: Seconds a transient failure caused by a parallel attempt is retried.
_TRANSIENT_RETRY_S = 2.0

#: A deletion is called destructive when it removes many lines and mostly
#: deletes: at least this many lines gone with more than twice the added lines.
_DESTRUCTIVE_MIN_DELETED = 30
_DESTRUCTIVE_RATIO = 2

#: Paths per ``git update-index`` call, to stay under command-line limits.
_UPDATE_INDEX_BATCH = 200


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
    project: Path, job_id: str, piece: int, attempt: int, start: str = "HEAD"
) -> tuple[Path, str]:
    """Create the worktree for one attempt and return ``(path, branch)``.

    The branch is ``crew/<job_id>-<piece>-<attempt>``, created from ``start``
    (``HEAD`` by default; a correction starts from the branch of the attempt
    it corrects), and the worktree lives in
    ``<project.parent>/<project.name>.crew/``. The
    two coordinator files are added to the worktree's own ``info/exclude`` so
    they never show up in ``git status``. Raises :class:`NotAGitRepo` when
    ``project`` is not a git repository, and :class:`RuntimeError` when the
    target path is a registered worktree already.

    An orphan folder at the target path (left by a removal that stopped half
    way, typically on Windows, and not in ``git worktree list``) is removed
    after a ``git worktree prune``, then the worktree is created.
    """
    project = Path(project)
    try:
        _git(["rev-parse", "--show-toplevel"], project)
    except (RuntimeError, OSError) as exc:
        raise NotAGitRepo(f"not a git repository: {project}") from exc

    branch = f"crew/{job_id}-{piece}-{attempt}"
    worktree = _worktree_path(project, job_id, piece, attempt)
    _git(["worktree", "prune"], project, check=False)
    if worktree.exists():
        if worktree.name in _registered_worktree_names(project, worktree.parent):
            raise RuntimeError(f"worktree path already exists: {worktree}")
        _remove_tree(worktree)

    worktree.parent.mkdir(parents=True, exist_ok=True)
    _git(["worktree", "add", "-b", branch, str(worktree), start], project)
    _exclude_coordinator_files(worktree)
    return worktree, branch


def _worktree_path(project: Path, job_id: str, piece: int, attempt: int) -> Path:
    """``<project.parent>/<project.name>.crew/<job_id>-<piece>-<attempt>``."""
    return project.parent / f"{project.name}.crew" / f"{job_id}-{piece}-{attempt}"


def branch_exists(project: Path, branch: str) -> bool:
    """Whether ``branch`` is a local branch of ``project``."""
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=Path(project),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.returncode == 0


def next_free_attempt(project: Path, job_id: str, piece: int, start: int) -> int:
    """The first attempt number ``>= start`` whose worktree and branch are free.

    A number is taken when its branch ``crew/<job_id>-<piece>-<n>`` exists or
    its worktree path is a registered worktree (an attempt whose record was
    lost still holds both). An orphan folder does not take a number:
    :func:`create_worktree` clears it.
    """
    project = Path(project)
    crew_dir = project.parent / f"{project.name}.crew"
    registered = _registered_worktree_names(project, crew_dir)
    prefix = f"crew/{job_id}-{piece}-"
    branches = {
        line.strip().lstrip("*+ ").strip()
        for line in _git(
            ["branch", "--list", f"{prefix}*"], project, check=False
        ).splitlines()
    }
    number = start
    while (
        f"{prefix}{number}" in branches
        or f"{job_id}-{piece}-{number}" in registered
    ):
        number += 1
    return number


def snapshot(
    project: Path, worktree: Path, ignore_home: list[str] | tuple[str, ...] = ()
) -> dict:
    """A containment snapshot: main repo status, siblings and home entries.

    ``repo`` is the set of ``git status --porcelain=v1 -uall`` lines of the
    main repo, minus :data:`COORDINATOR_RECORDS` and untracked Python bytecode
    caches; ``sibling`` and ``home`` map each top-level entry name to its
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
    # A parallel attempt may be replacing the shared info/exclude right now.
    status = _retry_transient(
        lambda: _git(["status", "--porcelain=v1", "-uall"], project), RuntimeError
    )
    registered =_registered_worktree_names(project, worktree.parent)
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
        "repo": {
            line
            for line in status.splitlines()
            if not _is_coordinator_record(line) and not _is_untracked_cache(line)
        },
        "sibling": siblings,
        "home": home,
    }


def _is_coordinator_record(line: str) -> bool:
    """Whether a porcelain status line is a record crew-chief itself writes.

    Every path the line names (both sides of a rename) must be one of
    :data:`COORDINATOR_RECORDS`.
    """
    paths = [part.strip().strip('"') for part in line[3:].split(" -> ")]

    def is_record(path: str) -> bool:
        return any(
            path.startswith(entry) if entry.endswith("/") else path == entry
            for entry in COORDINATOR_RECORDS
        )

    return all(is_record(path) for path in paths)


def _is_untracked_cache(line: str) -> bool:
    """Whether a porcelain status line is an untracked Python bytecode cache.

    Interpreters (a test run the executor starts in the project, say) leave
    ``__pycache__/*.pyc`` behind; the project does not need a ``.gitignore``
    for that not to read as an outside write. Only ``??`` lines qualify: a
    tracked file under ``__pycache__`` that the attempt changes or deletes
    still counts as a write.
    """
    if not line.startswith("?? "):
        return False
    path = line[3:].strip().strip('"').replace("\\", "/")
    return path.endswith(".pyc") or "__pycache__" in path.split("/")[:-1]


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


def hide_sensitive(worktree: Path, patterns: list[str]) -> list[str]:
    """Remove the tracked files matching ``patterns`` from the worktree.

    ``patterns`` are ``sensitive_paths`` entries, matched like a brief's
    (:func:`briefcheck.path_is_sensitive`). Each tracked match is marked
    ``git update-index --skip-worktree`` and deleted from disk, so it is not
    shown as deleted by ``git status``, the diff stats or the attempt's commit,
    and a merge of the branch leaves the project's copy alone. Folders left
    empty are removed too. Returns the hidden paths, sorted; nothing happens
    without patterns.
    """
    worktree = Path(worktree)
    if not patterns:
        return []
    hidden = sorted(
        name for name in _tracked_files(worktree) if path_is_sensitive(name, patterns)
    )
    for start in range(0, len(hidden), _UPDATE_INDEX_BATCH):
        batch = hidden[start : start + _UPDATE_INDEX_BATCH]
        _git(["update-index", "--skip-worktree", "--", *batch], worktree)
    for name in hidden:
        _remove_path(worktree / name)
        _prune_empty_parents(worktree, worktree / name)
    return hidden


def sensitive_present(worktree: Path, patterns: list[str]) -> list[str]:
    """Paths in the worktree that match ``patterns`` and exist on disk.

    Tracked, untracked and git-ignored paths all count (the real incident was
    a folder git knew nothing about). Each match is reported as its top-most
    matching path, ``/``-separated and relative to the worktree, so a copied
    folder is one entry, ``referencia``, not one per file. Sorted; empty
    without patterns.
    """
    worktree = Path(worktree)
    if not patterns:
        return []
    found: set[str] = set()
    for name in (*_tracked_files(worktree), *_untracked_files(worktree)):
        if not (worktree / name).exists():
            continue
        parts = name.rstrip("/").split("/")
        for end in range(1, len(parts) + 1):
            prefix = "/".join(parts[:end])
            if path_is_sensitive(prefix, patterns):
                found.add(prefix)
                break
    return sorted(found)


def remove_paths(worktree: Path, paths: list[str]) -> None:
    """Delete ``paths`` (relative to ``worktree``), files and folders alike."""
    worktree = Path(worktree)
    for name in paths:
        _remove_path(worktree / name)


def _tracked_files(worktree: Path) -> list[str]:
    """``git ls-files`` of the worktree: every path in the index."""
    return [
        name
        for name in _git(["ls-files", "-z"], worktree).split("\0")
        if name
    ]


def _untracked_files(worktree: Path) -> list[str]:
    """Untracked and ignored paths; an ignored folder is one ``dir/`` entry."""
    names: list[str] = []
    for extra in ([], ["--ignored", "--directory"]):
        listing = _git(
            ["ls-files", "--others", "--exclude-standard", "-z", *extra], worktree
        )
        names.extend(name for name in listing.split("\0") if name)
    return names


def _remove_path(path: Path) -> None:
    """Delete a file or a whole folder; a missing path is not an error."""
    if path.is_dir() and not path.is_symlink():
        _remove_tree(path)
    else:
        try:
            path.unlink(missing_ok=True)
        except PermissionError:  # pragma: no cover - read-only file on Windows
            os.chmod(path, stat.S_IWRITE)
            path.unlink(missing_ok=True)


def _prune_empty_parents(root: Path, path: Path) -> None:
    """Remove the folders above ``path`` that became empty, up to ``root``."""
    parent = path.parent
    while parent != root and root in parent.parents:
        try:
            parent.rmdir()
        except OSError:
            return
        parent = parent.parent


def head_commit(worktree: Path) -> str:
    """The sha ``HEAD`` points at in ``worktree``."""
    return _git(["rev-parse", "HEAD"], Path(worktree)).strip()


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


def short_sha(repo: Path, rev: str) -> str:
    """``git rev-parse --short <rev>`` in ``repo``; raises :class:`RuntimeError`."""
    return _git(["rev-parse", "--short", rev], Path(repo)).strip()


def merge_base(repo: Path, first: str, second: str) -> str:
    """The best common ancestor of two revisions; raises :class:`RuntimeError`."""
    return _git(["merge-base", first, second], Path(repo)).strip()


def path_is_clean(project: Path, path: str) -> bool:
    """Whether ``path`` is tracked and identical to ``HEAD`` (index and disk).

    Anything git cannot answer (no repository, no ``HEAD``) counts as not
    clean.
    """
    project = Path(project)

    def ok(args: list[str]) -> bool:
        result = subprocess.run(
            ["git", *args], cwd=project, capture_output=True, text=True,
            encoding="utf-8",
        )
        return result.returncode == 0

    return ok(["ls-files", "--error-unmatch", "--", path]) and ok(
        ["diff", "--quiet", "HEAD", "--", path]
    )


def commit_paths(project: Path, paths: list[str], message: str) -> None:
    """Commit ``paths`` of ``project`` (and nothing else) on its current branch.

    Other staged or modified files stay out of the commit. Raises
    :class:`RuntimeError` when git refuses (for instance a hook, or an ignored
    path).
    """
    project = Path(project)
    _git(["add", "--", *paths], project)
    _git(["commit", "-m", message, "--", *paths], project)


class WorktreeRemovalError(RuntimeError):
    """Raised when a worktree cannot be removed after branch merge."""


def remove_worktree(project: Path, worktree: Path, branch: str | None = None) -> None:
    """Remove a worktree directory and prune administrative entries.

    If branch is given, delete the branch if it exists.
    Raises :class:`WorktreeRemovalError` if the worktree could not be removed.
    """
    project = Path(project)
    worktree = Path(worktree)
    if worktree.exists():
        try:
            _remove_worktree(project, worktree)
        except Exception as exc:
            raise WorktreeRemovalError(str(exc)) from exc
    if branch:
        _git(["branch", "-D", branch], project, check=False)


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
    remove_worktree(project, worktree, branch)


def discard(project: Path, worktree: Path, branch: str) -> None:
    """Remove the worktree and its branch without merging anything."""
    project = Path(project)
    _remove_worktree(project, Path(worktree))
    _git(["branch", "-D", branch], project, check=False)


def _exclude_coordinator_files(worktree: Path) -> None:
    """Add :data:`EXCLUDED_FILES` to the worktree's ``info/exclude``.

    That file is shared by every worktree of the repository, and parallel
    attempts run ``git`` against it at any moment; on Windows a ``git`` that
    opens it mid-write dies. So it is written only when an entry is missing,
    and then through a temp file and an atomic replace.
    """
    raw = _git(["rev-parse", "--git-path", "info/exclude"], worktree).strip()
    exclude = Path(raw)
    if not exclude.is_absolute():
        exclude = worktree / exclude
    lines = (
        exclude.read_text(encoding="utf-8").splitlines() if exclude.exists() else []
    )
    missing = [entry for entry in EXCLUDED_FILES if entry not in lines]
    if not missing:
        return
    exclude.parent.mkdir(parents=True, exist_ok=True)
    temp = exclude.with_name(f"{exclude.name}.{os.getpid()}.tmp")
    temp.write_text("\n".join([*lines, *missing]) + "\n", encoding="utf-8")
    try:
        _retry_transient(lambda: os.replace(temp, exclude), PermissionError)
    finally:
        temp.unlink(missing_ok=True)


def _retry_transient(action, errors, seconds: float = _TRANSIENT_RETRY_S):
    """Run ``action``, retrying ``errors`` for up to ``seconds``.

    For the brief failures parallel attempts cause on Windows: a file being
    replaced cannot be opened, and one that is open cannot be replaced.
    """
    deadline = time.monotonic() + seconds
    delay = 0.01
    while True:
        try:
            return action()
        except errors:
            if time.monotonic() >= deadline:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.2)


def _remove_worktree(project: Path, worktree: Path) -> None:
    """Remove the worktree, falling back to rmtree when git cannot.

    On Windows a read-only file can make ``git worktree remove`` fail; then the
    directory is emptied by hand (unlocking files as needed) and the stale
    administrative entry is pruned. Raises :class:`RuntimeError` when the
    worktree still exists after all removal attempts.
    """
    result = subprocess.run(
        # longpaths: a node_modules deep inside the worktree passes Windows' 260 limit
        ["git", "-c", "core.longpaths=true", "worktree", "remove", "--force", str(worktree)],
        cwd=project,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode == 0:
        return
    if worktree.exists():
        try:
            _remove_tree(worktree)
        except OSError as exc:
            raise WorktreeRemovalError(
                f"cannot remove worktree {worktree}: {exc}"
            ) from exc
    _git(["worktree", "prune"], project, check=False)
    if worktree.exists():
        raise WorktreeRemovalError(
            f"worktree {worktree} still exists after removal attempt"
        )


def _remove_tree(path: Path) -> None:
    """``shutil.rmtree`` that unlocks read-only entries before retrying."""

    def unlock(func, target, _exc):  # pragma: no cover - exercised on Windows
        os.chmod(target, stat.S_IWRITE)
        func(target)

    if os.name == "nt":
        # the extended-length prefix lifts the 260-character path limit
        full = str(Path(path).resolve())
        if full.startswith("\\\\?\\"):
            pass
        elif full.startswith("\\\\"):
            full = "\\\\?\\UNC\\" + full[2:]
        else:
            full = "\\\\?\\" + full
        path = Path(full)
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
