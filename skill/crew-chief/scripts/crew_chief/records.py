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

#: Work types that are hands-on execution: free executors only (see
#: ``config.DEFAULT_WORK_TYPES_BY_KIND`` and ``scoring.pick``).
EXEC_TYPES = ("code", "tests", "refactor", "docs", "data")

#: Work types that are thinking (judging, designing, diagnosing, reading up):
#: free executors first, then automatic escalation to paid ones.
THINK_TYPES = ("design", "research", "diagnosis", "review")

#: Fixed risk vocabulary (see global constraints).
RISKS = ("low", "medium", "high")

#: Valid attempt roles.
ROLES = ("exec", "review")

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
                try:
                    lock.unlink(missing_ok=True)
                    continue
                except PermissionError:
                    pass  # held open for a moment: wait and try again
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
        # An antivirus or indexer may hold the fresh file open for a moment.
        _sharing_retry(lambda: lock.unlink(missing_ok=True))


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


def add_piece(job: dict, title: str, type: str, risk: str, ref: str = "") -> int:
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


def commit_type(job: dict) -> str:
    """Conventional commit type from the first piece; ``chore`` with no pieces."""
    pieces = job.get("pieces") or []
    if not pieces:
        return "chore"
    return _COMMIT_TYPES.get(pieces[0].get("type"), "chore")


def _commit_message(job: dict, changelog_file: str | None = None) -> str:
    body = []
    for piece in job.get("pieces") or []:
        body.append(f"- {piece.get('title', '')} — {_piece_credit(piece)}")
    subject = f"{commit_type(job)}: {job.get('title', '')}"
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


def summaries(job: dict, changelog_file: str | None = None) -> tuple[str, str]:
    """Return ``(commit_message, issue_comment)`` for the job.

    ``changelog_file`` is the project CHANGELOG ``close`` just wrote an entry
    into; the commit message then names it among the files of the commit.
    """
    return _commit_message(job, changelog_file), _issue_comment(job)


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
