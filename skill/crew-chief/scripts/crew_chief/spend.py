"""The global spend log: one JSON line per paid attempt, across all projects.

``spend.jsonl`` lives next to the global preferences file (so the
``AGENT_CREW_CHIEF_GLOBAL_PREFS`` override moves both). Dispatches of
different projects run in parallel processes and all append to the same
file, so every append happens under ``spend.lock``, the same cross-process
lock file the job records use (:func:`records.file_lock`): a line is never
lost or torn, and the daily ceiling can add up what every project spent.

A line carries only the fields in :data:`FIELDS`; nothing from the
environment (no key, no token) is ever written.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone
from pathlib import Path

from . import config
from . import records
from .records import RecordError, file_lock

#: Seconds :func:`record` waits for another process's ``spend.lock``.
LOCK_TIMEOUT_S = records.LOCK_TIMEOUT_S

#: The only keys a spend line may hold, in the order they are written.
FIELDS = (
    "at", "project", "job", "piece", "executor", "tier", "cost_usd", "known",
)


class SpendLockTimeout(RecordError):
    """Raised when ``spend.lock`` stays taken for longer than the timeout."""


def spend_file() -> Path:
    """``spend.jsonl`` in the folder of the global preferences file."""
    return config.default_global_path().parent / "spend.jsonl"


def __getattr__(name: str) -> object:
    # ``spend.SPEND_FILE`` is resolved on access, not at import: the global
    # preferences path can be overridden through the environment later.
    if name == "SPEND_FILE":
        return spend_file()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _clean(entry: dict) -> dict:
    """``entry`` reduced to :data:`FIELDS`, with ``at`` and the cost filled in."""
    at = entry.get("at")
    if not isinstance(at, str) or not at:
        at = datetime.now(timezone.utc).isoformat()
    try:
        cost = float(entry.get("cost_usd") or 0.0)
    except (TypeError, ValueError):
        cost = 0.0
    values = dict(entry)
    values["at"] = at
    values["cost_usd"] = round(cost, 6)
    values["known"] = bool(entry.get("known", False))
    return {key: values.get(key) for key in FIELDS}


def record(entry: dict) -> None:
    """Append one line for ``entry`` to the spend log, under ``spend.lock``.

    ``entry`` holds the :data:`FIELDS`; ``at`` defaults to now (UTC) and any
    other key is dropped. Raises :class:`SpendLockTimeout` when another
    process keeps the lock past :data:`LOCK_TIMEOUT_S`.
    """
    path = spend_file()
    lock = path.with_name("spend.lock")
    line = json.dumps(_clean(entry), ensure_ascii=False) + "\n"

    def timeout_error(waited: float) -> Exception:
        return SpendLockTimeout(
            f"{lock}: the spend log is locked by another crew-chief process "
            f"(waited {waited:g}s); if none is running, delete the lock file"
        )

    with file_lock(lock, LOCK_TIMEOUT_S, records.LOCK_STALE_S, timeout_error):
        with open(path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())


def _day_of(at: object) -> date | None:
    """The UTC date of an ISO timestamp; ``None`` when it does not parse."""
    if not isinstance(at, str):
        return None
    try:
        moment = datetime.fromisoformat(at)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).date()


def spent(day: date, job: str | None = None) -> float:
    """US$ spent on ``day`` (UTC) across all projects, optionally on one ``job``.

    Damaged lines are skipped. A line is complete once its newline is
    written under the lock, so reading needs no lock.
    """
    path = spend_file()
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0.0
    total = 0.0
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict) or _day_of(row.get("at")) != day:
            continue
        if job is not None and row.get("job") != job:
            continue
        cost = row.get("cost_usd")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            total += float(cost)
    return total
