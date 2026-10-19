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

Nothing here imports another ``crew_chief`` module: the vocabularies live
here and later tasks import them from this module.
"""

from __future__ import annotations

import json
import os
import random
import tempfile
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

#: Fixed risk vocabulary (see global constraints).
RISKS = ("low", "medium", "high")

#: Valid attempt roles.
ROLES = ("exec", "review")

#: Valid attempt statuses.
STATUSES = (
    "ok",
    "failed",
    "timeout",
    "quota",
    "outside_write",
    "brief_refused",
)

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
    """Write ``text`` to ``path`` through a temp file plus ``os.replace``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f"{path.name}.", suffix=".tmp"
    )
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        _sharing_retry(lambda: os.replace(temp, path))
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def new_job(
    project: Path,
    title: str,
    request: str,
    distributed: bool,
    reason: str,
    issue_ref: str = "",
    now: datetime | None = None,
) -> dict:
    """Create, save and return a new job record.

    The id is ``f"{now:%Y%m%d-%H%M}-{slug(title)[:40]}"`` with a ``-2``,
    ``-3``… suffix when a job with that id already exists.
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
    }
    save_job(project, job)
    return job


def _read_job(path: Path) -> dict:
    """Read one job JSON file, converting every failure to :class:`RecordError`."""
    try:
        text = _sharing_retry(lambda: path.read_text(encoding="utf-8"))
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
    or not valid JSON.
    """
    path = jobs_dir(project) / f"{job_id}.json"
    return _read_job(path)


def save_job(project: Path, job: dict) -> None:
    """Write the job's ``.json`` (indent 2, UTF-8, no escapes) and its ``.md``."""
    directory = jobs_dir(project)
    job_id = job["id"]
    payload = json.dumps(job, indent=2, ensure_ascii=False) + "\n"
    write_atomic(directory / f"{job_id}.json", payload)
    write_atomic(directory / f"{job_id}.md", render_markdown(job))


def _lock_age_s(lock: Path) -> float | None:
    """Age of ``lock`` in seconds from its mtime; ``None`` when it is gone."""
    try:
        return time.time() - lock.stat().st_mtime
    except OSError:
        return None


@contextmanager
def job_lock(
    project: Path,
    job_id: str,
    timeout_s: float = LOCK_TIMEOUT_S,
    stale_s: float = LOCK_STALE_S,
) -> Iterator[Path]:
    """Hold ``docs/crew-chief/jobs/<job_id>.lock`` for the ``with`` block.

    The lock is a file created with ``O_CREAT | O_EXCL`` (atomic on Windows
    and POSIX) holding the owner's pid. A taken lock is retried with a
    jittered exponential backoff; one whose mtime is older than ``stale_s``
    was left by a dead process and is removed. After ``timeout_s`` a
    :class:`JobLockTimeout` names the lock file.
    """
    lock = jobs_dir(project) / f"{job_id}.lock"
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
                lock.unlink(missing_ok=True)
                continue
            if time.monotonic() >= deadline:
                raise JobLockTimeout(
                    f"{lock}: job {job_id!r} is locked by another crew-chief "
                    f"process (waited {timeout_s:g}s); if none is running, "
                    "delete the lock file"
                ) from None
            time.sleep(delay * (0.5 + random.random()))
            delay = min(delay * 2, 0.5)
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"{os.getpid()}\n")
        break
    try:
        yield lock
    finally:
        lock.unlink(missing_ok=True)


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


def add_piece(job: dict, title: str, type: str, risk: str) -> int:
    """Append a piece and return its number (starting at 1).

    ``type`` must be in :data:`WORK_TYPES` and ``risk`` in :data:`RISKS`.
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
        "grade": None,
    }
    pieces.append(piece)
    return piece["n"]


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


def set_grade(job: dict, piece: int, grade: dict) -> None:
    """Store ``grade`` (as given) under the piece's ``grade`` key."""
    _find_piece(job, piece)["grade"] = grade


# --- rendering -------------------------------------------------------------


def _piece_executor(piece: dict) -> str:
    """The executor of the piece's last attempt, or ``""`` when it has none."""
    attempts = piece.get("attempts") or []
    if not attempts:
        return ""
    return str(attempts[-1].get("executor", "") or "")


def _piece_score(piece: dict) -> object:
    """The piece's grade score, or ``"-"`` when the piece is not graded."""
    grade = piece.get("grade")
    if not grade or grade.get("score") is None:
        return "-"
    return grade["score"]


def _commit_type(job: dict) -> str:
    """Conventional commit type from the first piece; ``chore`` with no pieces."""
    pieces = job.get("pieces") or []
    if not pieces:
        return "chore"
    return _COMMIT_TYPES.get(pieces[0].get("type"), "chore")


def _commit_message(job: dict) -> str:
    body = []
    for piece in job.get("pieces") or []:
        executor = _piece_executor(piece) or "unassigned"
        body.append(
            f"- {piece.get('title', '')} — {executor}, score {_piece_score(piece)}"
        )
    subject = f"{_commit_type(job)}: {job.get('title', '')}"
    if not body:
        return subject
    return "\n".join([subject, "", *body])


def _issue_comment(job: dict) -> str:
    lines: list[str] = []
    pieces = job.get("pieces") or []
    if not pieces:
        lines.append("- no pieces")
    for piece in pieces:
        executor = _piece_executor(piece) or "unassigned"
        lines.append(
            f"- {piece.get('n')}. {piece.get('title', '')} — {executor}, "
            f"score {_piece_score(piece)}"
        )
        for escalation in piece.get("escalations") or []:
            lines.append(
                f"  - escalated: {escalation.get('reason', '')} → "
                f"{escalation.get('to', '')}"
            )
    record = f"{_RECORD_RELATIVE_DIR}/{job.get('id', '')}.md"
    return "\n".join([*lines, "", f"Record: {record}"])


def summaries(job: dict) -> tuple[str, str]:
    """Return ``(commit_message, issue_comment)`` for the job."""
    return _commit_message(job), _issue_comment(job)


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
    if attempt.get("branch"):
        parts.append(f"branch `{attempt['branch']}`")
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

    lines.append("")
    grade = piece.get("grade")
    if grade:
        score = grade.get("score")
        text = "**Grade**" if score is None else f"**Grade**: score {score}"
        if grade.get("explain"):
            text += f" — {grade['explain']}"
        lines.append(text)
    else:
        lines.append("**Grade**: _not graded_")
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
