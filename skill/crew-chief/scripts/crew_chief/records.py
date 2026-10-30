"""Job records: JSON model, rendered Markdown and summaries.

A job is a plain :class:`dict`; this module owns its shape, the pieces it
contains and the two files written per job inside
``docs/crew-chief/jobs/``: a ``.json`` (the machine-readable record) and a
``.md`` (the same record rendered for a human). Both are written atomically.

Several ``crew-chief`` processes may touch the same job at once (parallel
dispatches of one job are normal), so every change to a saved job goes through
:func:`update_job`: it takes a lock file next to the record, re-reads the
JSON, applies the change and writes it back before letting go. A plain
:func:`load_job` + :func:`save_job` pair would silently drop whatever another
process wrote in between.

Nothing here imports another ``crew_chief`` module at import time: the
vocabularies live here and later tasks import them from this module. The one
exception is :func:`render_markdown`, which imports ``economy`` when it runs
(``economy`` itself imports this module).
"""

from __future__ import annotations

import json
import os
import random
import re
import tempfile
import threading
import time
import unicodedata
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

#: Fixed work-type vocabulary (see global constraints).
WORK_TYPES = (
    "code",
    "tests",
    "refactor",
    "design",
    "docs",
    "research",
    "diagnosis",
    "data",
    "review",
)

#: Work types that are hands-on execution: free executors only (see
#: ``config.DEFAULT_WORK_TYPES_BY_KIND`` and ``scoring.pick``).
EXEC_TYPES = ("code", "tests", "refactor", "docs", "data")

#: Work types that are thinking (judging, designing, diagnosing, reading up):
#: free executors first, then automatic escalation to paid ones.
THINK_TYPES = ("design", "research", "diagnosis", "review")

#: Fixed risk vocabulary (see global constraints).
RISKS = ("low", "medium", "high")

#: Roles that can report their own token spend with :func:`add_usage`.
USAGE_ROLES = ("coordinator",)

#: Valid attempt roles.
ROLES = ("exec", "review")

#: Valid team roles for team_used audit trail (US10 / FR-037).
TEAM_ROLES = ("coordinator", "executor", "reviewer")

#: Valid team assignment origins for team_used audit trail (US10 / FR-037).
TEAM_ORIGINS = (
    "caller",
    "project_team",
    "global_team",
    "project_prefs",
    "global_prefs",
    "global_fallback",
    "discovery",
)

#: Valid attempt statuses.
STATUSES = (
    "ok",
    "no_change",
    "failed",
    "timeout",
    "quota",
    "outside_write",
    "brief_refused",
    "proof_failed",
    "running",
)

#: Valid executor modes for a job (FR-038).
EXECUTOR_MODES = ("free_first", "own", "subagents")
SUPERPOWERS_VALUES = (
    "off",
    "auto",
    "test-driven-development",
    "systematic-debugging",
    "verification-before-completion",
)


def validate_superpowers(mode: str) -> str:
    """Validate that ``mode`` is a known superpowers setting."""
    norm = str(mode).strip().lower()
    if norm not in SUPERPOWERS_VALUES:
        raise ValueError(
            f"unknown superpowers mode {mode!r} (expected one of {', '.join(SUPERPOWERS_VALUES)})"
        )
    return norm

#: First work type -> conventional commit type.
_COMMIT_TYPES = {
    "code": "feat",
    "tests": "test",
    "refactor": "refactor",
    "design": "feat",
    "docs": "docs",
    "research": "docs",
    "diagnosis": "fix",
    "data": "feat",
    "review": "chore",
}

#: Path of the record, relative to the project root (used in the issue comment).
_RECORD_RELATIVE_DIR = "docs/crew-chief/jobs"


#: Seconds :func:`update_job` waits for another process's lock by default.
LOCK_TIMEOUT_S = 60.0

#: A lock file older than this (seconds) belongs to a dead process and is broken.
LOCK_STALE_S = 600.0

#: Seconds a Windows sharing violation on a record is retried (see
#: :func:`_sharing_retry`).
SHARING_RETRY_S = 2.0


class RecordError(Exception):
    """Raised when a job record file is missing or unreadable; names the file."""


class JobLockTimeout(RecordError):
    """Raised when a job's lock file stays taken for longer than the timeout."""


def crew_chief_dir(project: Path) -> Path:
    """Return ``project/docs/crew-chief``, the folder of every committed record."""
    return Path(project) / "docs" / "crew-chief"


def jobs_dir(project: Path) -> Path:
    """Return ``project/docs/crew-chief/jobs``."""
    return crew_chief_dir(project) / "jobs"


_JOB_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


def validate_job_id(job_id: str) -> str:
    """Validate that *job_id* is a safe identifier without path traversal.

    A job id must match ``^[A-Za-z0-9_-]+$`` (alphanumeric, hyphens, underscores).
    Raises :class:`RecordError` if *job_id* is empty, contains path separators,
    parent directory references, or other disallowed characters.
    """
    if not isinstance(job_id, str) or not _JOB_ID_PATTERN.fullmatch(job_id):
        raise RecordError(f"invalid job id {job_id!r}")
    return job_id


def job_path(project: Path | str, job_id: str, suffix: str = ".json") -> Path:
    """Return the contained path for a job file under ``docs/crew-chief/jobs/``.

    Validates *job_id* with :func:`validate_job_id` and verifies that the
    resolved path stays strictly inside ``docs/crew-chief/jobs``.
    Raises :class:`RecordError` on invalid job ids or path traversal attempts.
    """
    validate_job_id(job_id)
    proj = Path(project)
    directory = jobs_dir(proj)
    jobs_folder = directory.resolve(strict=False)
    candidate = (directory / f"{job_id}{suffix}").resolve(strict=False)
    if not candidate.is_relative_to(jobs_folder) or candidate.parent != jobs_folder:
        raise RecordError(f"job id {job_id!r} escapes jobs directory")
    return directory / f"{job_id}{suffix}"


def slug(title: str) -> str:
    """Slugify ``title`` for use in a job id.

    Lowercases, ASCII-folds accents, turns every non-alphanumeric character
    into ``-``, collapses repeats and strips leading/trailing ``-``. An empty
    result becomes ``"job"``.
    """
    decomposed = unicodedata.normalize("NFKD", title)
    folded = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    chars = [
        ch if ch.isascii() and ch.isalnum() else "-" for ch in folded.lower()
    ]
    text = "".join(chars)
    while "--" in text:
        text = text.replace("--", "-")
    return text.strip("-") or "job"


def _sharing_retry(action: Callable[[], object]) -> object:
    """Run ``action``, retrying a :class:`PermissionError` for a short while.

    On Windows a file that another process is reading cannot be replaced, and
    a file in the middle of being replaced cannot be opened; both surface as
    ``PermissionError`` for a few milliseconds. After
    :data:`SHARING_RETRY_S` the error propagates.
    """
    deadline = time.monotonic() + SHARING_RETRY_S
    delay = 0.005
    while True:
        try:
            return action()
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.1)


def write_atomic(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` through a temp file plus ``os.replace``.

    The text is written byte for byte (no newline translation, so CRLF and a
    BOM already in ``text`` survive), and an existing file keeps its mode.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f"{path.name}.", suffix=".tmp"
    )
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        try:
            os.chmod(temp, os.stat(path).st_mode & 0o7777)
        except OSError:  # a new file keeps the temp file's mode
            pass
        _sharing_retry(lambda: os.replace(temp, path))
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def validate_executor_mode(mode: str) -> str:
    """Return ``mode`` if it is a valid executor mode, else raise :class:`ValueError`."""
    if mode not in EXECUTOR_MODES:
        choices = ", ".join(EXECUTOR_MODES)
        raise ValueError(
            f"invalid executor mode {mode!r}; expected one of: {choices}"
        )
    return mode


def job_executors(job: dict) -> str:
    """Return the executor mode for ``job``, defaulting to ``"free_first"``."""
    return job.get("executors") or "free_first"


def new_job(
    project: Path,
    title: str,
    request: str,
    distributed: bool,
    reason: str,
    issue_ref: str = "",
    now: datetime | None = None,
    executors: str | None = None,
    job_branch: str | None = None,
    base_branch: str | None = None,
    superpowers: str | None = None,
) -> dict:
    """Create, save and return a new job record.

    The id is ``f"{now:%Y%m%d-%H%M}-{slug(title)[:40]}"`` with a ``-2``,
    ``-3``… suffix when a job with that id already exists. When ``executors``
    is given it must be one of :data:`EXECUTOR_MODES`; legacy jobs without the
    field load transparently as ``"free_first"`` via :func:`load_job`.
    """
    project = Path(project)
    now = now or datetime.now()
    base = f"{now:%Y%m%d-%H%M}-{slug(title)[:40]}"
    directory = jobs_dir(project)

    job_id = base
    counter = 2
    while (directory / f"{job_id}.json").exists():
        job_id = f"{base}-{counter}"
        counter += 1

    job = {
        "id": job_id,
        "title": title,
        "request": request,
        "created": now.isoformat(),
        "distributed": bool(distributed),
        "reason": reason,
        "issue_ref": issue_ref,
        "pieces": [],
        "executors": validate_executor_mode(executors) if executors is not None else "free_first",
        "team_used": [],
    }
    if superpowers is not None:
        job["superpowers"] = validate_superpowers(superpowers)
    if job_branch is not None:
        job["job_branch"] = job_branch
    if base_branch is not None:
        job["base_branch"] = base_branch
    save_job(project, job)
    return job


def set_executors(
    project_or_job: Path | str | dict,
    job_id_or_mode: str,
    mode_or_source: str = "cli",
    source: str = "cli",
) -> dict:
    """Set the executor mode on ``job`` atomically under lock.

    Can be called as ``set_executors(job, mode, source="cli")`` or
    ``set_executors(project, job_id, mode, source="cli")``.

    Records the switch timestamp and source. In-flight attempts and recorded
    picks are unchanged; the new mode affects only the next ``pick``. Raises
    :class:`ValueError` for invalid modes.
    """
    if isinstance(project_or_job, dict):
        job = project_or_job
        mode = job_id_or_mode
        src = mode_or_source
        validate_executor_mode(mode)
        old = job_executors(job)
        job["executors"] = mode
        history = job.setdefault("executors_history", [])
        history.append(
            {
                "mode": mode,
                "from": old,
                "to": mode,
                "at": datetime.now(timezone.utc).isoformat(),
                "source": src,
            }
        )
        return job
    else:
        project = Path(project_or_job)
        job_id = str(job_id_or_mode)
        mode = str(mode_or_source)
        src = source
        validate_executor_mode(mode)

        def apply(j: dict) -> None:
            set_executors(j, mode, src)

        update_job(project, job_id, apply)
        return load_job(project, job_id)


def set_executors_fallback(
    project: Path | None,
    job_id: str | None,
    original: str,
    fallback: str,
    source: str = "cli",
) -> None:
    """Record that ``original`` was silently replaced by ``fallback`` (FR-040).

    When a job is created with ``executors="ask"`` the coordinator would
    normally prompt interactively; at creation time we persist the
    non-interactive default instead.  Audits need to distinguish an
    explicit choice from this automatic substitution, so we append an
    entry to ``executors_history`` even though the stored mode does not
    change.  ``project`` and ``job_id`` may both be ``None`` when the
    fallback happens before the job exists; in that case the call is a
    no-op and the caller must ensure the history is recorded later.
    """
    if project is None or job_id is None:
        return
    validate_executor_mode(fallback)

    def apply(job: dict) -> None:
        history = job.setdefault("executors_history", [])
        history.append(
            {
                "mode": fallback,
                "from": original,
                "to": fallback,
                "at": datetime.now(timezone.utc).isoformat(),
                "source": source,
                "fallback": True,
            }
        )

    update_job(Path(project), job_id, apply)


def _read_job(path: Path) -> dict:
    """Read one job JSON file, converting every failure to :class:`RecordError`."""
    try:
        text = _sharing_retry(lambda: path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise RecordError(f"{path}: cannot read job record: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RecordError(
            f"{path}: invalid JSON at line {exc.lineno} column {exc.colno}: {exc.msg}"
        ) from exc
    if not isinstance(data, dict):
        raise RecordError(f"{path}: job record is not a JSON object")
    return data


def load_job(project: Path, job_id: str) -> dict:
    """Load ``docs/crew-chief/jobs/<job_id>.json``.

    Raises :class:`RecordError` naming the file when it is missing, unreadable
    or not valid JSON. Legacy records without an ``executors`` field are
    returned with ``executors`` set to ``"free_first"`` in memory only; the
    disk file is never rewritten by this function.
    """
    path = job_path(project, job_id, suffix=".json")
    job = _read_job(path)
    if "executors" not in job:
        job["executors"] = "free_first"
    if "team_used" not in job:
        job["team_used"] = []
    return job


def record_team_used(
    project_or_job: Path | str | dict,
    role: str,
    id: str,
    origin: str,
    piece: int | None = None,
    at: str | None = None,
    *,
    job_id: str | None = None,
) -> dict:
    """Record a worker assignment in the job's team_used audit trail (US10 / FR-037).

    role: coordinator | executor | reviewer
    id: identifier of the assigned coordinator or executor/reviewer
    origin: one of caller, project_team, global_team, project_prefs, global_prefs, global_fallback, discovery
    piece: optional piece number (int)
    at: optional ISO timestamp
    """
    if role not in TEAM_ROLES:
        raise ValueError(
            f"invalid team role {role!r}; expected one of: {', '.join(TEAM_ROLES)}"
        )
    if origin not in TEAM_ORIGINS:
        raise ValueError(
            f"invalid team origin {origin!r}; expected one of: {', '.join(TEAM_ORIGINS)}"
        )

    entry: dict = {
        "role": role,
        "id": id,
        "origin": origin,
        "at": at or datetime.now(timezone.utc).isoformat(),
    }
    if piece is not None:
        entry["piece"] = int(piece)

    if isinstance(project_or_job, dict):
        project_or_job.setdefault("team_used", []).append(entry)
        return entry

    project = Path(project_or_job)
    jid = str(job_id) if job_id is not None else ""
    if not jid:
        raise ValueError("job_id is required when passing project path")

    def apply(j: dict) -> None:
        j.setdefault("team_used", []).append(entry)

    update_job(project, jid, apply)
    return entry



def save_job(project: Path, job: dict) -> None:
    """Write the job's ``.json`` (indent 2, UTF-8, no escapes) and its ``.md``."""
    job_id = job.get("id")
    if not isinstance(job_id, str):
        raise RecordError(f"job record missing valid id: {job_id!r}")
    json_path = job_path(project, job_id, suffix=".json")
    md_path = job_path(project, job_id, suffix=".md")
    payload = json.dumps(job, indent=2, ensure_ascii=False) + "\n"
    write_atomic(json_path, payload)
    write_atomic(md_path, render_markdown(job))


def _lock_age_s(lock: Path) -> float | None:
    """Age of ``lock`` in seconds from its mtime; ``None`` when it is gone."""
    try:
        return time.time() - lock.stat().st_mtime
    except OSError:
        return None


@contextmanager
def file_lock(
    lock: Path,
    timeout_s: float,
    stale_s: float,
    timeout_error: Callable[[float], Exception],
) -> Iterator[Path]:
    """Hold the lock file ``lock`` for the ``with`` block.

    The lock is a file created with ``O_CREAT | O_EXCL`` (atomic on Windows
    and POSIX) holding the owner's pid. A taken lock is retried with a
    jittered exponential backoff; one whose mtime is older than ``stale_s``
    was left by a dead process and is removed. After ``timeout_s`` the
    exception ``timeout_error(timeout_s)`` is raised. Shared by
    :func:`job_lock` and the global spend log (``spend.py``).
    """
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_s
    delay = 0.01
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except (FileExistsError, PermissionError):
            # PermissionError: Windows refuses a file that is being deleted.
            age = _lock_age_s(lock)
            if age is not None and age > stale_s:
                try:
                    lock.unlink(missing_ok=True)
                    continue
                except PermissionError:
                    pass  # held open for a moment: wait and try again
            if time.monotonic() >= deadline:
                raise timeout_error(timeout_s) from None
            time.sleep(delay * (0.5 + random.random()))
            delay = min(delay * 2, 0.5)
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"{os.getpid()}\n")
        break
    try:
        yield lock
    finally:
        # An antivirus or indexer may hold the fresh file open for a moment.
        _sharing_retry(lambda: lock.unlink(missing_ok=True))


_HELD_JOB_LOCKS = threading.local()


@contextmanager
def job_lock(
    project: Path,
    job_id: str,
    timeout_s: float = LOCK_TIMEOUT_S,
    stale_s: float = LOCK_STALE_S,
) -> Iterator[Path]:
    """Hold ``docs/crew-chief/jobs/<job_id>.lock`` for the ``with`` block.

    See :func:`file_lock`; the :class:`JobLockTimeout` names the lock file.
    """
    lock = job_path(project, job_id, suffix=".lock")
    held = getattr(_HELD_JOB_LOCKS, "paths", None)
    if held is None:
        held = set()
        _HELD_JOB_LOCKS.paths = held

    resolved = str(lock.resolve())
    if resolved in held:
        yield lock
        return

    held.add(resolved)

    def timeout_error(waited: float) -> Exception:
        return JobLockTimeout(
            f"{lock}: job {job_id!r} is locked by another crew-chief "
            f"process (waited {waited:g}s); if none is running, "
            "delete the lock file"
        )

    try:
        with file_lock(lock, timeout_s, stale_s, timeout_error):
            yield lock
    finally:
        held.discard(resolved)


def update_job(
    project: Path,
    job_id: str,
    fn: Callable[[dict], object],
    timeout_s: float = LOCK_TIMEOUT_S,
) -> object:
    """Apply ``fn`` to the saved job atomically and return what ``fn`` returns.

    Under :func:`job_lock` the job is re-read from disk, ``fn(job)`` changes
    it in place and the result is saved with :func:`save_job`. When ``fn``
    raises, nothing is written and the exception propagates.
    """
    with job_lock(project, job_id, timeout_s=timeout_s):
        job = load_job(project, job_id)
        result = fn(job)
        save_job(project, job)
    return result


def _find_piece(job: dict, number: int) -> dict:
    for piece in job.get("pieces") or []:
        if piece.get("n") == number:
            return piece
    raise ValueError(f"job {job.get('id')!r} has no piece {number}")


def add_piece(
    job: dict,
    title: str,
    type: str,
    risk: str,
    ref: str = "",
    superpowers: str | None = None,
) -> int:
    """Append a piece and return its number (starting at 1).

    ``type`` must be in :data:`WORK_TYPES` and ``risk`` in :data:`RISKS`.
    A non-empty ``ref`` (a spec-kit task id such as ``T012``) is stored on the
    piece; without one the piece has no ``ref`` key.
    """
    if type not in WORK_TYPES:
        raise ValueError(
            f"unknown work type {type!r} (expected one of {', '.join(WORK_TYPES)})"
        )
    if risk not in RISKS:
        raise ValueError(
            f"unknown risk {risk!r} (expected one of {', '.join(RISKS)})"
        )

    pieces = job.setdefault("pieces", [])
    piece = {
        "n": len(pieces) + 1,
        "title": title,
        "type": type,
        "risk": risk,
        "attempts": [],
        "corrections": [],
        "escalations": [],
        "grades": {},
    }
    if superpowers is not None:
        piece["superpowers"] = validate_superpowers(superpowers)
    if ref:
        piece["ref"] = ref
    pieces.append(piece)
    return piece["n"]


def piece_for_ref(job: dict, ref: str) -> dict | None:
    """The piece bound to spec task ``ref``, ignoring stale ones; else ``None``.

    A piece turns ``stale`` when ``tasks.md`` was regenerated and its ref now
    names a different task; it keeps its history but never stands for a task.
    """
    for piece in job.get("pieces") or []:
        if piece.get("ref") == ref and not piece.get("stale"):
            return piece
    return None


def mark_accepted(job: dict, piece: int, attempt: dict) -> None:
    """Record that ``attempt``'s branch was merged for the piece.

    Stores ``{"attempt": n, "branch": ..., "at": <UTC ISO>}`` under the
    piece's ``accepted`` key; ``piece.get("accepted")`` means "accepted".
    """
    _find_piece(job, piece)["accepted"] = {
        "attempt": attempt["n"],
        "branch": attempt["branch"],
        "at": datetime.now(timezone.utc).isoformat(),
    }


def add_attempt(job: dict, piece: int, attempt: dict) -> None:
    """Append ``attempt`` to the piece.

    Whatever keys the attempt carries are stored verbatim; only ``role``
    (``exec|review``) and ``status`` (see :data:`STATUSES`) are validated.
    """
    role = attempt.get("role")
    if role not in ROLES:
        raise ValueError(
            f"unknown attempt role {role!r} (expected one of {', '.join(ROLES)})"
        )
    status = attempt.get("status")
    if status not in STATUSES:
        raise ValueError(
            f"unknown attempt status {status!r} "
            f"(expected one of {', '.join(STATUSES)})"
        )

    _find_piece(job, piece)["attempts"].append(dict(attempt))


def add_correction(
    job: dict, piece: int, reason: str, executor: str = ""
) -> None:
    """Record that the piece drew a correction, with the ISO timestamp."""
    _find_piece(job, piece)["corrections"].append(
        {
            "reason": reason,
            "executor": executor,
            "at": datetime.now(timezone.utc).isoformat(),
        }
    )


def add_escalation(job: dict, piece: int, reason: str, to: str) -> None:
    """Record that the piece escalated to another executor, with the timestamp."""
    _find_piece(job, piece)["escalations"].append(
        {"reason": reason, "to": to, "at": datetime.now(timezone.utc).isoformat()}
    )


def add_ruling(job: dict, text: str) -> int:
    """Append a ruling to ``job["rulings"]`` and return its number (1-based).

    A ruling reads ``<decision> — <why> — <cost if wrong>``; ``promoted`` holds
    the ADR number once it becomes a decision record.
    """
    rulings = job.setdefault("rulings", [])
    number = len(rulings) + 1
    rulings.append(
        {
            "n": number,
            "text": text,
            "at": datetime.now(timezone.utc).isoformat(),
            "promoted": None,
        }
    )
    return number


def _token_count(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer, got {value!r}")
    return value


def add_usage(
    job: dict,
    role: str,
    tokens_in: int,
    tokens_out: int,
    estimated: bool,
    note: str = "",
) -> None:
    """Append the coordinator's own token spend to the job.

    ``role`` must be in :data:`USAGE_ROLES` and the token counts non-negative
    integers; the entry lands in ``job["usage"]`` with ``tokens_total`` and
    the UTC timestamp.
    """
    if role not in USAGE_ROLES:
        raise ValueError(
            f"unknown usage role {role!r} (expected one of {', '.join(USAGE_ROLES)})"
        )
    tokens_in = _token_count("tokens_in", tokens_in)
    tokens_out = _token_count("tokens_out", tokens_out)
    job.setdefault("usage", []).append(
        {
            "role": role,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "tokens_total": tokens_in + tokens_out,
            "estimated": bool(estimated),
            "note": note,
            "at": datetime.now(timezone.utc).isoformat(),
        }
    )


def piece_grades(piece: dict) -> dict[str, dict]:
    """Every grade of the piece, keyed by executor id, in grading order.

    Grades live under the piece's ``grades`` map, one per executor that
    worked on it. A record written before that kept a single ``grade``; it is
    read as the grade of its own ``executor`` (``""`` when it names none), so
    old jobs keep counting. Entries that are not dicts are left out.
    """
    grades: dict[str, dict] = {}
    legacy = piece.get("grade")
    if isinstance(legacy, dict):
        grades[str(legacy.get("executor") or "")] = legacy
    stored = piece.get("grades")
    if isinstance(stored, dict):
        for executor, grade in stored.items():
            if isinstance(grade, dict):
                grades[str(executor)] = grade
    return grades


def set_grade(job: dict, piece: int, grade: dict) -> None:
    """Store ``grade`` (as given) as the grade of its ``executor`` on the piece.

    Each executor that worked on a piece keeps its own grade under the
    piece's ``grades`` map; grading the same executor again replaces only
    that executor's grade. A legacy single ``grade`` is moved into the map
    first, so it is not lost.
    """
    target = _find_piece(job, piece)
    grades = piece_grades(target)
    target.pop("grade", None)
    grades[str(grade.get("executor") or "")] = grade
    target["grades"] = grades


# --- rendering -------------------------------------------------------------


def _piece_executor(piece: dict) -> str:
    """The executor of the piece's last attempt, or ``""`` when it has none."""
    attempts = piece.get("attempts") or []
    if not attempts:
        return ""
    return str(attempts[-1].get("executor", "") or "")


def _score_of(grade: dict | None) -> object:
    """A grade's score, or ``"-"`` when there is none."""
    if not grade or grade.get("score") is None:
        return "-"
    return grade["score"]


def _lead_executor(piece: dict) -> str:
    """The executor the piece is credited to first.

    The executor of the last ``ok`` exec attempt (the one ``accept`` lands),
    else of the last attempt, else the first graded executor; ``""`` when the
    piece has neither attempts nor grades.
    """
    for attempt in reversed(piece.get("attempts") or []):
        if attempt.get("role") == "exec" and attempt.get("status") == "ok":
            executor = str(attempt.get("executor", "") or "")
            if executor:
                return executor
    executor = _piece_executor(piece)
    if executor:
        return executor
    return next((name for name in piece_grades(piece) if name), "")


def _piece_credit(piece: dict) -> str:
    """``lead, score 100; also other, score 52`` for the summaries.

    The lead executor (see :func:`_lead_executor`) comes first; a grade that
    names no executor is read as the lead's. Every other graded executor
    follows in grading order.
    """
    grades = piece_grades(piece)
    lead = _lead_executor(piece)
    lead_grade = grades.get(lead) if lead else None
    if lead_grade is None:
        lead_grade = grades.get("")
    text = f"{lead or 'unassigned'}, score {_score_of(lead_grade)}"
    others = [
        f"{executor}, score {_score_of(grade)}"
        for executor, grade in grades.items()
        if executor and executor != lead
    ]
    if others:
        text += "; also " + "; ".join(others)
    return text


#: The types ``close --type`` accepts (commit message type and changelog
#: category).
CLOSE_TYPES = ("feat", "fix", "refactor", "perf", "docs", "chore")

#: A conventional commit prefix at the start of a job title: type, the
#: optional ``(scope)`` and ``!``, then the rest of the title.
_TITLE_PREFIX = re.compile(
    r"^(feat|fix|refactor|perf|docs|chore|test)(\([^)]*\))?(!)?:\s*(.*)$", re.DOTALL
)


def split_title(title: str) -> tuple[str | None, str, str]:
    """``(type, "(scope)!", rest)`` of a conventional title; ``(None, "", title)``."""
    match = _TITLE_PREFIX.match(title or "")
    if not match:
        return None, "", title or ""
    return match.group(1), (match.group(2) or "") + (match.group(3) or ""), match.group(4)


def commit_type(job: dict, override: str | None = None) -> str:
    """Conventional commit type of the job.

    In order: ``override`` (``close --type``), the type ``close --type``
    stored in the job (``commit_type``), a conventional prefix of the title
    (``fix: ...``), then the first piece's work type; ``chore`` with no pieces.
    """
    if override:
        return override
    stored = job.get("commit_type")
    if isinstance(stored, str) and stored in CLOSE_TYPES:
        return stored
    prefix, _, _ = split_title(str(job.get("title", "")))
    if prefix:
        return prefix
    pieces = job.get("pieces") or []
    if not pieces:
        return "chore"
    return _COMMIT_TYPES.get(pieces[0].get("type"), "chore")


def _commit_message(
    job: dict, changelog_file: str | None = None, kind: str | None = None
) -> str:
    body = []
    for piece in job.get("pieces") or []:
        body.append(f"- {piece.get('title', '')} — {_piece_credit(piece)}")
    _, scope, rest = split_title(str(job.get("title", "")))
    subject = f"{commit_type(job, kind)}{scope}: {rest}"
    if body:
        body.insert(0, "")
    if changelog_file:
        body.extend(["", f"Files: {changelog_file} (changelog entry)"])
    return "\n".join([subject, *body])


def _issue_comment(job: dict) -> str:
    lines: list[str] = []
    pieces = job.get("pieces") or []
    if not pieces:
        lines.append("- no pieces")
    for piece in pieces:
        lines.append(
            f"- {piece.get('n')}. {piece.get('title', '')} — {_piece_credit(piece)}"
        )
        for escalation in piece.get("escalations") or []:
            lines.append(
                f"  - escalated: {escalation.get('reason', '')} → "
                f"{escalation.get('to', '')}"
            )
    record = f"{_RECORD_RELATIVE_DIR}/{job.get('id', '')}.md"
    return "\n".join([*lines, "", f"Record: {record}"])


def summaries(
    job: dict, changelog_file: str | None = None, kind: str | None = None
) -> tuple[str, str]:
    """Return ``(commit_message, issue_comment)`` for the job.

    ``changelog_file`` is the project CHANGELOG ``close`` just wrote an entry
    into; the commit message then names it among the files of the commit.
    ``kind`` overrides the commit type (see :func:`commit_type`).
    """
    return _commit_message(job, changelog_file, kind), _issue_comment(job)


def _attempt_line(index: int, attempt: dict) -> str:
    parts = [
        f"executor `{attempt.get('executor', '')}`",
        f"role {attempt.get('role', '')}",
        f"status {attempt.get('status', '')}",
    ]
    if attempt.get("cost"):
        parts.append(f"cost {attempt['cost']}")
    if attempt.get("duration_s") is not None:
        parts.append(f"{attempt['duration_s']}s")
    if attempt.get("steps") is not None:
        parts.append(f"{attempt['steps']} steps")
    if attempt.get("tokens_total") is not None:
        if attempt.get("tokens_estimated"):
            parts.append(f"~{attempt['tokens_total']} tokens (estimated)")
        elif (
            attempt.get("tokens_in") is not None
            and attempt.get("tokens_out") is not None
        ):
            parts.append(
                f"{attempt['tokens_total']} tokens "
                f"(in {attempt['tokens_in']} / out {attempt['tokens_out']})"
            )
        else:
            parts.append(f"{attempt['tokens_total']} tokens")
    if attempt.get("branch"):
        parts.append(f"branch `{attempt['branch']}`")
    base = attempt.get("base")
    if isinstance(base, dict) and base.get("branch"):
        start = f"from `{base['branch']}`"
        if base.get("attempt") is not None:
            start += f" (attempt {base['attempt']})"
        parts.append(start)
    if attempt.get("model_mismatch"):
        parts.append("model mismatch")
    return f"- attempt {index}: " + ", ".join(parts)


def _attempt_details(attempt: dict) -> list[str]:
    lines: list[str] = []
    if attempt.get("reported_model"):
        lines.append(f"  - reported model: `{attempt['reported_model']}`")
    if attempt.get("diffstat"):
        files = ", ".join(
            f"{entry.get('file', '')} +{entry.get('added', 0)}"
            f" -{entry.get('deleted', 0)}"
            for entry in attempt["diffstat"]
        )
        lines.append(f"  - diffstat: {files}")
    if attempt.get("destructive_suspect"):
        lines.append(
            "  - destructive suspect: "
            + ", ".join(str(name) for name in attempt["destructive_suspect"])
        )
    if attempt.get("outside_writes"):
        lines.append(
            "  - outside writes: "
            + ", ".join(str(name) for name in attempt["outside_writes"])
        )
    if attempt.get("note"):
        lines.append("  - note:")
        lines.append("    ```")
        for note_line in str(attempt["note"]).splitlines() or [""]:
            lines.append(f"    {note_line}")
        lines.append("    ```")
    return lines


def _render_piece(piece: dict) -> list[str]:
    lines: list[str] = ["", "**Attempts**", ""]
    attempts = piece.get("attempts") or []
    if not attempts:
        lines.append("- _none_")
    for index, attempt in enumerate(attempts, start=1):
        lines.append(_attempt_line(index, attempt))
        lines.extend(_attempt_details(attempt))

    lines.extend(["", "**Corrections**", ""])
    corrections = piece.get("corrections") or []
    if not corrections:
        lines.append("- _none_")
    for correction in corrections:
        executor = correction.get("executor")
        who = f" by `{executor}`" if executor else ""
        lines.append(
            f"- {correction.get('reason', '')}{who} ({correction.get('at', '')})"
        )

    lines.extend(["", "**Escalations**", ""])
    escalations = piece.get("escalations") or []
    if not escalations:
        lines.append("- _none_")
    for escalation in escalations:
        lines.append(
            f"- {escalation.get('reason', '')} → {escalation.get('to', '')} "
            f"({escalation.get('at', '')})"
        )

    lines.extend(["", "**Grades**", ""])
    grades = piece_grades(piece)
    if not grades:
        lines.append("- _not graded_")
    for executor, grade in grades.items():
        text = f"- `{executor or 'unnamed'}`: score {_score_of(grade)}"
        if grade.get("explain"):
            text += f" — {grade['explain']}"
        lines.append(text)
    return lines


def render_markdown(job: dict) -> str:
    """Render the job as the human-readable Markdown record."""
    commit_message, issue_comment = summaries(job)

    lines: list[str] = [f"# {job.get('title', '')}", ""]
    lines.append(f"- **Id:** `{job.get('id', '')}`")
    lines.append(f"- **Created:** {job.get('created', '')}")
    lines.append(
        f"- **Distributed:** {'yes' if job.get('distributed') else 'no'}"
    )
    lines.append(f"- **Executors:** {job_executors(job)}")
    if job.get("issue_ref"):
        lines.append(f"- **Issue:** {job['issue_ref']}")

    lines.extend(["", "## Request", "", str(job.get("request", "")).rstrip()])

    lines.extend(
        [
            "",
            "## Decision",
            "",
            f"- Distributed: {'yes' if job.get('distributed') else 'no'}",
            f"- Reason: {job.get('reason', '')}",
        ]
    )

    rulings = job.get("rulings") or []
    if rulings:
        lines.extend(["", "## Rulings", ""])
        for ruling in rulings:
            line = f"{ruling.get('n')}. {ruling.get('text', '')}"
            if ruling.get("promoted"):
                line += f" → ADR {ruling['promoted']}"
            lines.append(line)
    decisions = job.get("decisions") or []
    if decisions:
        lines.extend(["", "## Decisions", ""])
        lines.extend(f"- ADR {number}" for number in decisions)

    lines.extend(["", "## Pieces"])
    pieces = job.get("pieces") or []
    if not pieces:
        lines.extend(["", "_No pieces._"])
    for piece in pieces:
        lines.append("")
        lines.append(
            f"### {piece.get('n')}. {piece.get('title', '')} "
            f"({piece.get('type', '')}, risk {piece.get('risk', '')})"
        )
        lines.extend(_render_piece(piece))

    from . import economy  # lazy: economy imports this module

    if economy.has_data(job):
        # No prefs here: the alerts are printed by the CLI, not stored.
        figures = economy.job_economy(job, None)
        lines.extend(["", "## Economy", "", economy.summary_line(figures)])
        lines.extend(economy.detail_lines(figures))

    lines.extend(
        [
            "",
            "## Commit message",
            "",
            "```",
            commit_message,
            "```",
            "",
            "## Issue comment",
            "",
            issue_comment,
            "",
        ]
    )
    return "\n".join(lines)


def iter_jobs(project: Path) -> Iterator[tuple[dict | None, Path, str | None]]:
    """Yield ``(job, path, error)`` for every ``.json`` in the jobs directory.

    A corrupt or unreadable file yields ``None`` and the error text; this
    function never raises and never skips a file.
    """
    directory = jobs_dir(project)
    if not directory.is_dir():
        return
    for path in sorted(directory.glob("*.json")):
        try:
            data = _read_job(path)
        except RecordError as exc:
            yield None, path, str(exc)
            continue
        yield data, path, None


def transition_terminal_state(
    *,
    job: dict | None = None,
    project: Path | str | None = None,
    job_id: str | None = None,
    new_state: str,
    extra_fields: dict | None = None,
) -> dict:
    """Move a job to a terminal or blocked state and log the event once.

    Sets ``state`` and ``status``, copies ``pr_url`` and merges ``result``
    from ``extra_fields``, and appends one event. A job already in
    ``new_state`` with that event is left as it is. With ``job`` the dict is
    changed and saved; otherwise the record is updated under its lock.
    """
    timestamp = datetime.now(timezone.utc).isoformat()
    extra = dict(extra_fields or {})
    if job_id is None and job is not None:
        job_id = job.get("id") or job.get("job_id")

    def apply(stored: dict) -> dict:
        if stored.get("state") == new_state and stored.get("status") == new_state and any(
            isinstance(e, dict) and e.get("type") == new_state for e in stored.get("events", [])
        ):
            return stored
        stored["state"] = new_state
        stored["status"] = new_state
        if "pr_url" in extra:
            stored["pr_url"] = extra["pr_url"]
        if "result" in extra:
            current = stored.get("result")
            if isinstance(current, dict) and isinstance(extra["result"], dict):
                stored["result"] = {**current, **extra["result"]}
            else:
                stored["result"] = extra["result"]
        payload = {"type": new_state, "timestamp": timestamp, **extra}
        try:
            from .interview_service import _reconstruct_event_log

            seq = _reconstruct_event_log(stored).append(payload)
        except Exception:
            seq = len(stored.get("events", [])) + 1
        stored.setdefault("events", []).append(
            {"sequence": seq, "type": new_state, "payload": payload, **payload}
        )
        return stored

    if job is not None:
        apply(job)
        if project is not None and job_id:
            try:
                save_job(Path(project), job)
            except Exception:
                pass
        return job
    if project is not None and job_id:
        return update_job(Path(project), job_id, apply)
    raise ValueError("Either job or (project and job_id) must be provided")
