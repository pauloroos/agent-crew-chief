"""Economy metrics of a job: did the free work really save paid tokens?

Three anti-goals guard the whole tool: spending more paid tokens than the
free work saved, more rework, and slower work. :func:`job_economy` turns a job
record into the figures that show each one, and into alerts when a figure
crosses a limit the owner set (``alert_*`` preferences, ``0`` turns one off).

Definitions (a division by zero gives ``None``, never an exception):

* ``free_tokens`` / ``paid_exec_tokens``: the ``tokens_total`` of every attempt
  whose ``cost`` is ``free`` / ``paid``.
* ``tier_tokens``: the same ``tokens_total`` split by every cost tier
  (``free``, ``included``, ``cheap``, ``paid``; ``0`` for a tier with none).
* ``cheap_usd`` / ``paid_usd``: the sum of the finite, non-negative
  ``cost_usd`` of the attempts whose ``cost`` is ``cheap`` / ``paid``;
  ``cost_unknown`` says whether any of those attempts had ``cost_known``
  false.
* ``coordinator_tokens``: the job's ``usage`` entries (``coordinator`` role,
  see ``records.add_usage``).
* ``usage_recorded``: whether the job has at least one valid ``usage``
  entry. Without one the coordinator's share is unknown, not zero: the
  summary says ``not recorded`` instead of a measured ``0%``.
* ``free_piece_share``: accepted pieces whose last ``ok`` exec attempt ran on
  a free executor, over accepted pieces.
* ``free_token_share`` / ``coordinator_share``: ``free`` / ``coordinator``
  tokens over all four token counts plus the coordinator.
* ``attempts_per_piece``: exec attempts over pieces with at least one exec
  attempt; ``brief_refused`` (never ran) and ``quota`` (no work done) attempts
  count on neither side, so review and converge pieces do not dilute it.
* ``mean_score``: mean of every grade's score.
* ``minutes``: from the job's ``created`` to the end of its last attempt
  (``started`` + ``duration_s``).
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from . import records

if TYPE_CHECKING:  # config imports records; keep this module cheap to import
    from .config import Prefs

_DASH = "—"

#: Exec statuses left out of ``attempts_per_piece``: the executor did no work.
_NOT_ATTEMPTS = ("brief_refused", "quota")

#: The cost tiers of an attempt, in the order of ``Economy.tier_tokens``.
_TIERS = ("free", "included", "cheap", "paid")


def _empty_tiers() -> dict[str, int]:
    return {tier: 0 for tier in _TIERS}


@dataclass
class Economy:
    """The economy figures of one job; ``None`` means "nothing to divide"."""

    free_tokens: int
    paid_exec_tokens: int
    coordinator_tokens: int
    coordinator_estimated: bool
    free_piece_share: float | None
    free_token_share: float | None
    coordinator_share: float | None
    attempts_per_piece: float | None
    mean_score: float | None
    minutes: float | None
    tier_tokens: dict[str, int] = field(default_factory=_empty_tiers)
    cheap_usd: float = 0.0
    paid_usd: float = 0.0
    cost_unknown: bool = False
    alerts: list[str] = field(default_factory=list)
    #: Whether the job holds a valid ``usage`` entry. It comes last so the
    #: positional construction of older callers keeps working.
    usage_recorded: bool = False
    #: The active executor mode (``own``, ``subagents`` or ``free_first``).
    #: Legacy jobs without the field are reported as ``free_first``.
    executors: str = "free_first"

    def as_dict(self) -> dict:
        return asdict(self)


def _number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _usd(value: object) -> float | None:
    """``value`` as a finite, non-negative amount, or ``None``."""
    if not _number(value) or not math.isfinite(value) or value < 0:
        return None
    return float(value)


def _total_tokens(e: Economy) -> int:
    """Every token the job spent: all four tiers plus the coordinator."""
    return sum(e.tier_tokens.values()) + e.coordinator_tokens


def _ratio(part: float, whole: float) -> float | None:
    return part / whole if whole else None


def _parse_iso(text: object, local_tz) -> datetime | None:
    """An ISO timestamp as an aware datetime; naive ones are local time."""
    if not isinstance(text, str):
        return None
    try:
        when = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=local_tz)


def _pieces(job: dict) -> list[dict]:
    return [p for p in job.get("pieces") or [] if isinstance(p, dict)]


def _attempts(piece: dict) -> list[dict]:
    return [a for a in piece.get("attempts") or [] if isinstance(a, dict)]


def has_data(job: dict) -> bool:
    """Whether the job has at least one attempt or usage entry to show."""
    return bool(job.get("usage")) or any(_attempts(p) for p in _pieces(job))


def _minutes(job: dict, local_tz) -> float | None:
    created = _parse_iso(job.get("created"), local_tz)
    if created is None:
        return None
    ends = []
    for piece in _pieces(job):
        for attempt in _attempts(piece):
            started = _parse_iso(attempt.get("started"), local_tz)
            if started is None:
                continue
            duration = attempt.get("duration_s")
            ends.append(
                started + timedelta(seconds=duration if _number(duration) else 0)
            )
    if not ends:
        return None
    return max(0.0, (max(ends) - created).total_seconds() / 60)


def job_economy(
    job: dict,
    prefs: Prefs | None,
    now: datetime | None = None,
    spent_today: float | None = None,
) -> Economy:
    """Compute the :class:`Economy` of ``job``.

    ``prefs`` supplies the alert limits; ``None`` means no alerts.
    ``spent_today`` is what the day's spend log already holds (see
    ``spend.spent``): with it the cheap daily budget can raise an alert.
    Naive timestamps (the job's ``created`` is local time) are read in the
    local zone of ``now`` (default: the current local time).
    """
    now = now or datetime.now(timezone.utc)
    local_tz = now.astimezone().tzinfo

    tier_tokens = _empty_tiers()
    cheap_usd = paid_usd = 0.0
    cost_unknown = False
    exec_attempts = tried = 0
    accepted = accepted_free = 0
    scores: list[float] = []
    for piece in _pieces(job):
        attempts = _attempts(piece)
        counted = [
            a
            for a in attempts
            if a.get("role") == "exec"
            and a.get("status") not in _NOT_ATTEMPTS
        ]
        if counted:
            tried += 1
            exec_attempts += len(counted)
        for attempt in attempts:
            tier = attempt.get("cost")
            if tier not in tier_tokens:
                continue
            tokens = attempt.get("tokens_total")
            if _number(tokens):
                tier_tokens[tier] += tokens
            if tier not in ("cheap", "paid"):
                continue
            if attempt.get("cost_known") is False:
                cost_unknown = True
            usd = _usd(attempt.get("cost_usd"))
            if usd is not None:
                if tier == "cheap":
                    cheap_usd += usd
                else:
                    paid_usd += usd
        if piece.get("accepted"):
            accepted += 1
            last_ok = next(
                (
                    a
                    for a in reversed(attempts)
                    if a.get("role") == "exec" and a.get("status") == "ok"
                ),
                None,
            )
            if last_ok is not None and last_ok.get("cost") == "free":
                accepted_free += 1
        scores.extend(
            grade["score"]
            for grade in records.piece_grades(piece).values()
            if _number(grade.get("score"))
        )

    usage = [u for u in job.get("usage") or [] if isinstance(u, dict)]
    # a valid entry carries a token count; the sum and the flag read the
    # same entries, so the flag never disagrees with the numbers
    recorded = [u for u in usage if _number(u.get("tokens_total"))]
    coordinator = sum(u["tokens_total"] for u in recorded)
    free = tier_tokens["free"]
    paid = tier_tokens["paid"]
    total = sum(tier_tokens.values()) + coordinator

    executors = job.get("executors") or "free_first"
    if executors not in ("own", "subagents", "free_first"):
        executors = "free_first"

    economy = Economy(
        free_tokens=free,
        paid_exec_tokens=paid,
        coordinator_tokens=coordinator,
        coordinator_estimated=any(u.get("estimated") for u in recorded),
        free_piece_share=_ratio(accepted_free, accepted),
        free_token_share=_ratio(free, total),
        coordinator_share=_ratio(coordinator, total),
        attempts_per_piece=_ratio(exec_attempts, tried),
        mean_score=_ratio(sum(scores), len(scores)),
        minutes=_minutes(job, local_tz),
        tier_tokens=tier_tokens,
        cheap_usd=cheap_usd,
        paid_usd=paid_usd,
        cost_unknown=cost_unknown,
        usage_recorded=bool(recorded),
        executors=executors,
    )
    if prefs is not None:
        economy.alerts = _alerts(economy, prefs, spent_today)
    if executors in ("own", "subagents") and coordinator > 0:
        economy.alerts.append(
            f"executor mode {executors}: coordinator consumed {coordinator} tokens"
        )
    return economy


def _alerts(
    e: Economy, prefs: Prefs, spent_today: float | None = None
) -> list[str]:
    alerts = []
    limit = prefs.alert_coordinator_share
    if limit and e.coordinator_share is not None and e.coordinator_share > limit:
        alerts.append(
            f"coordinator spent {e.coordinator_share:.0%} of the tokens "
            f"(limit {limit:.0%})"
        )
    limit = prefs.alert_attempts_per_piece
    if limit and e.attempts_per_piece is not None and e.attempts_per_piece > limit:
        alerts.append(
            f"{e.attempts_per_piece:.1f} attempts per piece (limit {limit:.1f})"
        )
    limit = prefs.alert_job_minutes
    if limit and e.minutes is not None and e.minutes > limit:
        alerts.append(f"job took {e.minutes:.0f} min (limit {limit:.0f})")
    budget = prefs.cheap_budget_usd_day
    if budget > 0 and spent_today is not None and spent_today >= 0.8 * budget:
        pct = spent_today / budget
        alerts.append(
            f"cheap spend at {pct:.0%} of the daily budget "
            f"(US$ {spent_today:.2f} of {budget:.2f})"
        )
    return alerts


def _fmt(value: float | None, spec: str) -> str:
    return _DASH if value is None else format(value, spec)


def summary_line(e: Economy) -> str:
    """The one-line summary printed by ``economy`` and ``close``.

    Without a recorded usage entry the coordinator's share is unknown, not
    zero: the line says ``not recorded`` and the free share is over the
    recorded tokens only.
    """
    if e.usage_recorded:
        estimated = " (estimated)" if e.coordinator_estimated else ""
        coordinator = _fmt(e.coordinator_share, ".0%")
        scope = "of tokens"
    else:
        estimated = ""
        coordinator = "not recorded"
        scope = "of recorded tokens"
    line = (
        f"free work: {_fmt(e.free_piece_share, '.0%')} of pieces, "
        f"{_fmt(e.free_token_share, '.0%')} {scope} · "
        f"coordinator: {coordinator}{estimated} · "
        f"{_fmt(e.attempts_per_piece, '.1f')} attempts/piece · "
        f"mean score {_fmt(e.mean_score, '.0f')}"
    )
    included = e.tier_tokens.get("included", 0)
    total = _total_tokens(e)
    if included > 0:
        line += f" · included {included / total:.0%}"
    if e.cheap_usd > 0:
        line += f" · cheap US$ {e.cheap_usd:.2f}"
        if e.cost_unknown:
            line += " (cost unknown)"
    return line


def detail_lines(e: Economy) -> list[str]:
    """Markdown bullets under the summary line of the job record."""
    estimated = " (estimated)" if e.coordinator_estimated else ""
    lines = [
        "",
        f"- Tokens: free {e.free_tokens}, paid exec {e.paid_exec_tokens}, "
        f"coordinator {e.coordinator_tokens}{estimated}",
    ]
    if e.minutes is not None:
        lines.append(f"- Time: {e.minutes:.0f} min")
    return lines


def report_lines(e: Economy) -> list[str]:
    """The summary line followed by one ``alert: ...`` line per alert."""
    return [summary_line(e), *(f"alert: {alert}" for alert in e.alerts)]
