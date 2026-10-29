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
import math
import os
from dataclasses import dataclass, field
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


def _valid_cost(cost: object) -> bool:
    """Whether ``cost`` is a finite, non-negative number (a bool is not one)."""
    return (
        isinstance(cost, (int, float))
        and not isinstance(cost, bool)
        and math.isfinite(cost)
        and cost >= 0
    )


def _clean(entry: dict) -> dict:
    """``entry`` reduced to :data:`FIELDS`, with ``at`` and the cost filled in."""
    at = entry.get("at")
    if not isinstance(at, str) or not at:
        at = datetime.now(timezone.utc).isoformat()
    try:
        cost = float(entry.get("cost_usd") or 0.0)
    except (TypeError, ValueError):
        cost = 0.0
    known = bool(entry.get("known", False))
    if not _valid_cost(cost):
        # A malformed CLI event (NaN, infinity, negative) must not poison the
        # daily total; it counts as an unknown zero.
        cost, known = 0.0, False
    values = dict(entry)
    values["at"] = at
    values["cost_usd"] = round(cost, 6)
    values["known"] = known
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


def _rows():
    """Every readable spend line as ``(row, day)``; damaged lines are skipped.

    A line is complete once its newline is written under the lock, so
    reading needs no lock. A missing or unreadable file gives nothing.
    """
    try:
        text = spend_file().read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        day = _day_of(row.get("at"))
        if day is not None:
            yield row, day


def spent(day: date, job: str | None = None) -> float:
    """US$ spent on ``day`` (UTC) across all projects, optionally on one ``job``.

    Damaged lines are skipped, and so is a cost that is not a finite,
    non-negative number.
    """
    total = 0.0
    for row, row_day in _rows():
        if row_day != day:
            continue
        if job is not None and row.get("job") != job:
            continue
        cost = row.get("cost_usd")
        if _valid_cost(cost):
            total += float(cost)
    return total


@dataclass
class Ledger:
    """One read of the spend log, for the budget checks of ``pick``."""

    #: US$ spent on the day, all projects.
    day_total: float = 0.0
    #: US$ per job id, over every day.
    by_job: dict[str, float] = field(default_factory=dict)
    #: US$ spent on the day per executor id.
    executor_day: dict[str, float] = field(default_factory=dict)
    #: Executors with any line today (an attempt was made, priced or not).
    attempted_day: set[str] = field(default_factory=set)
    #: Executors with at least one line whose cost was known and above zero.
    #: OpenCode prints cost 0 for unpriced models, so a zero is never proof.
    priced: set[str] = field(default_factory=set)


def ledger(day: date) -> Ledger:
    """Totals of the spend log for ``day`` (UTC), read once."""
    result = Ledger()
    for row, row_day in _rows():
        executor = row.get("executor")
        if row_day == day and isinstance(executor, str) and executor:
            result.attempted_day.add(executor)
        cost = row.get("cost_usd")
        if not _valid_cost(cost):
            continue
        cost = float(cost)
        job = row.get("job")
        if isinstance(job, str) and job:
            result.by_job[job] = result.by_job.get(job, 0.0) + cost
        if isinstance(executor, str) and executor:
            if row.get("known") is True and cost > 0:
                result.priced.add(executor)
            if row_day == day:
                result.executor_day[executor] = (
                    result.executor_day.get(executor, 0.0) + cost
                )
        if row_day == day:
            result.day_total += cost
    return result


#: Reason texts of :class:`BudgetCheck`.
UNKNOWN_COST = "unknown cost and no cap_usd_day"
PROBE_NOTE = "cheap probe: unknown cost, one attempt today"


class BudgetCheck:
    """May a ``cheap`` executor spend money now? Shared by ``pick`` and ``dispatch``.

    Reads the spend log once, for today (UTC). ``check(executor)`` returns
    ``None`` when the executor may be used, else the reason it may not:

    * the day ceiling ``cheap_budget_usd_day`` (``0`` closes the tier) or
      today's spend of all projects has reached it;
    * the job ceiling ``cheap_budget_usd_job`` (when above zero) has been
      reached by the job's spend;
    * the executor's own ``cap_usd_day`` has been reached (a cap lets an
      executor with an unknown cost in, up to the cap);
    * its cost is unknown (no line with a known cost above zero: OpenCode
      prints ``0`` for an unpriced model, which proves nothing) and it has no
      cap. Such an executor gets ONE probe attempt per UTC day while the
      budget is open (:meth:`is_probe`); once it has been tried today and is
      still unpriced it stays closed until it has a cap.

    ``check(None)`` asks only about the tier as a whole (day and job ceilings).
    """

    def __init__(self, prefs, job_id: str, day: date | None = None) -> None:
        self.prefs = prefs
        self.job_id = job_id
        self.ledger = ledger(day or datetime.now(timezone.utc).date())

    def tier_reason(self) -> str | None:
        day_cap = self.prefs.cheap_budget_usd_day
        job_cap = self.prefs.cheap_budget_usd_job
        if day_cap <= 0 or self.ledger.day_total >= day_cap:
            return f"budget US$ {day_cap:g} reached (cheap_budget_usd_day)"
        if job_cap > 0 and self.ledger.by_job.get(self.job_id, 0.0) >= job_cap:
            return (
                f"job budget US$ {job_cap:g} reached (cheap_budget_usd_job)"
            )
        return None

    def __call__(self, executor) -> str | None:
        why = self.tier_reason()
        if why is not None or executor is None:
            return why
        cap = config.model_cap(executor.id, self.prefs)
        if cap is not None:
            if self.ledger.executor_day.get(executor.id, 0.0) >= cap:
                return f"cap US$ {cap:g} reached (cap_usd_day)"
            return None
        if executor.id in self.ledger.priced:
            return None
        if executor.id in self.ledger.attempted_day:
            return UNKNOWN_COST
        return None

    def is_probe(self, executor) -> bool:
        """Whether using ``executor`` now is its one probe of the day."""
        return (
            executor.id not in self.ledger.priced
            and config.model_cap(executor.id, self.prefs) is None
            and self(executor) is None
        )
