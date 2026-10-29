"""Scoreboard statistics and rendering.

Folds job grades on disk into per-(executor, type) Stat records,
weighted by half-life decay, and renders markdown scoreboard tables.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

#: Default half-life, in days, of a stored grade's weight in :func:`build_stats`.
DEFAULT_HALF_LIFE_DAYS = 30


@dataclass
class Stat:
    """Decayed history of one executor on one work type."""

    executor: str
    type: str
    n: int
    score: float | None
    mean_duration_s: float | None
    last: str | None
    #: Exec attempts (any status but ``brief_refused``) per piece that passed.
    attempts_per_pass: float | None = None
    #: Mean agent steps and tokens of the ``ok`` exec attempts that report them.
    mean_steps: float | None = None
    mean_tokens: float | None = None
    #: Whether any token count behind ``mean_tokens`` is a chars / 4 estimate.
    tokens_estimated: bool = False


Stat.__module__ = "crew_chief.scoring"


def _parse_iso(text: str) -> datetime | None:
    """Parse an ISO 8601 string as an aware datetime; ``None`` when invalid.

    A string without an offset is read as UTC; stored grades are always
    timezone-aware UTC, so this only guards hand-edited records.
    """
    from . import scoring
    dt = getattr(scoring, "datetime", datetime)
    tz = getattr(scoring, "timezone", timezone)
    try:
        when = dt.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=tz.utc)
    return when


def _is_number(value: object) -> bool:
    """Whether ``value`` is an int or float (a bool is not a number here)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _mean(values: list[float]) -> float | None:
    """Arithmetic mean, or ``None`` for an empty list."""
    return sum(values) / len(values) if values else None


def _collect(
    project: Path,
    half_life_days: float,
    now: datetime | None,
) -> tuple[dict[tuple[str, str], Stat], list[tuple[Path, str]]]:
    """Read every job and return ``(stats, skipped)``.

    ``skipped`` pairs the path of each corrupt job with the error text, for
    :func:`render_scoreboard`. Every executor graded on a piece counts (see
    :func:`records.piece_grades`, which also reads the single ``grade`` of
    older records). A grade without an executor or a parseable
    ``graded_at`` cannot be keyed or aged and is ignored.
    """
    from . import scoring
    dt = getattr(scoring, "datetime", datetime)
    tz = getattr(scoring, "timezone", timezone)
    path_cls = getattr(scoring, "Path", Path)

    now = now or dt.now(tz.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=tz.utc)

    # (executor, type) -> [weighted score sum, weight sum, graded pieces, newest].
    graded: dict[tuple[str, str], list] = {}
    # (executor, type) -> [duration sum, ok attempt count].
    durations: dict[tuple[str, str], list] = {}
    # (executor, type) -> {"runs", "passed", "steps", "tokens", "estimated"}.
    metrics: dict[tuple[str, str], dict] = {}
    skipped: list[tuple[Path, str]] = []

    for job, path, error in scoring.iter_jobs(path_cls(project)):
        if job is None:
            skipped.append((path, error or "unreadable record"))
            continue
        for piece in job.get("pieces") or []:
            if not isinstance(piece, dict):
                continue
            work_type = piece.get("type")
            if work_type not in scoring.WORK_TYPES:
                continue

            passed_here: set[str] = set()
            for attempt in piece.get("attempts") or []:
                if not isinstance(attempt, dict):
                    continue
                if attempt.get("role") != "exec":
                    continue
                executor = attempt.get("executor")
                if not isinstance(executor, str) or not executor:
                    continue
                status = attempt.get("status")
                if status == "brief_refused":
                    continue
                metric = metrics.setdefault(
                    (executor, work_type),
                    {"runs": 0, "passed": 0, "steps": [], "tokens": [],
                     "estimated": False},
                )
                metric["runs"] += 1
                if status != "ok":
                    continue
                if executor not in passed_here:
                    passed_here.add(executor)
                    metric["passed"] += 1
                if scoring._is_number(attempt.get("steps")):
                    metric["steps"].append(attempt["steps"])
                if scoring._is_number(attempt.get("tokens_total")):
                    metric["tokens"].append(attempt["tokens_total"])
                    if attempt.get("tokens_estimated"):
                        metric["estimated"] = True
                duration = attempt.get("duration_s")
                if not scoring._is_number(duration):
                    continue
                accumulator = durations.setdefault(
                    (executor, work_type), [0.0, 0]
                )
                accumulator[0] += duration
                accumulator[1] += 1

            for grade_value in scoring.piece_grades(piece).values():
                score = grade_value.get("score")
                executor = grade_value.get("executor")
                graded_at = grade_value.get("graded_at")
                if not scoring._is_number(score):
                    continue
                if not isinstance(executor, str) or not executor:
                    continue
                if not isinstance(graded_at, str):
                    continue
                when = scoring._parse_iso(graded_at)
                if when is None:
                    continue

                age_days = max(0.0, (now - when).total_seconds() / 86400.0)
                weight = 0.5 ** (age_days / half_life_days)
                accumulator = graded.setdefault(
                    (executor, work_type), [0.0, 0.0, 0, None]
                )
                accumulator[0] += weight * score
                accumulator[1] += weight
                accumulator[2] += 1
                if accumulator[3] is None or when > accumulator[3]:
                    accumulator[3] = when

    stats: dict[tuple[str, str], Stat] = {}
    for key, (weighted, weights, count, newest) in graded.items():
        duration = durations.get(key)
        metric = metrics.get(key) or {
            "runs": 0, "passed": 0, "steps": [], "tokens": [], "estimated": False
        }
        stats[key] = scoring.Stat(
            executor=key[0],
            type=key[1],
            n=count,
            score=weighted / weights if weights else None,
            mean_duration_s=duration[0] / duration[1] if duration else None,
            last=newest.isoformat() if newest is not None else None,
            attempts_per_pass=(
                metric["runs"] / metric["passed"] if metric["passed"] else None
            ),
            mean_steps=scoring._mean(metric["steps"]),
            mean_tokens=scoring._mean(metric["tokens"]),
            tokens_estimated=metric["estimated"],
        )
    return stats, skipped


def build_stats(
    project: Path,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
    now: datetime | None = None,
) -> dict[tuple[str, str], Stat]:
    """Decayed history per ``(executor, type)`` over every grade.

    Only grades count, one per executor graded on a piece (a piece reworked
    by a second executor after a correction gives both their own grade); a
    piece graded as ``review`` is keyed under the ``review`` type like any
    other. Each grade's weight is
    ``0.5 ** (age_days / half_life_days)`` and the score is the weighted mean.
    ``mean_duration_s`` is the mean ``duration_s`` of that executor's ``exec``
    attempts with status ``ok`` on pieces of that type; ``last`` is the most
    recent ``graded_at``. ``attempts_per_pass`` divides that executor's exec
    attempts (every status but ``brief_refused``, which never ran) by the
    pieces it got an ``ok`` on; ``mean_steps`` and ``mean_tokens`` average the
    ``ok`` exec attempts that carry them, and ``tokens_estimated`` says when
    any of those token counts was an estimate. Corrupt jobs are skipped.
    """
    from . import scoring
    path_cls = getattr(scoring, "Path", Path)
    return scoring._collect(path_cls(project), half_life_days, now)[0]


def _duration_cell(value: float | None) -> str:
    """``42s`` for a duration; an em dash when it is unknown."""
    if value is None:
        return "—"
    return f"{round(value)}s"


def _score_cell(value: float | None) -> str:
    """The score as text; an em dash when the executor has no score."""
    if value is None:
        return "—"
    from . import scoring
    return scoring._num(value)


def _tokens_cell(value: float | None, estimated: bool) -> str:
    """Mean tokens as an integer, ``~`` in front when any count was estimated."""
    if value is None:
        return "—"
    return f"{'~' if estimated else ''}{round(value)}"


def _by_type(stats: dict[tuple[str, str], Stat]) -> list[tuple[str, list[Stat]]]:
    """Work types in :data:`WORK_TYPES` order, executors by score descending (unrated last)."""
    from . import scoring
    by_type: dict[str, list[Stat]] = {}
    for stat in stats.values():
        by_type.setdefault(stat.type, []).append(stat)
    # Fixed vocabulary first, then any stray type a hand-edited record uses.
    ordered_types = [*scoring.WORK_TYPES, *sorted(set(by_type) - set(scoring.WORK_TYPES))]
    return [
        (work_type, sorted(by_type[work_type], key=lambda stat: (
            float("inf") if stat.score is None else -stat.score, stat.executor)))
        for work_type in ordered_types if by_type.get(work_type)
    ]


def scoreboard_rows(project: Path, now: datetime | None = None) -> list[dict]:
    """The rows of :func:`render_scoreboard` as data, in the same order."""
    from . import scoring
    path_cls = getattr(scoring, "Path", Path)
    half_life = getattr(scoring, "DEFAULT_HALF_LIFE_DAYS", DEFAULT_HALF_LIFE_DAYS)
    asdict_fn = getattr(scoring, "asdict", asdict)
    stats, _skipped = scoring._collect(path_cls(project), half_life, now)
    return [asdict_fn(stat) for _type, entries in scoring._by_type(stats) for stat in entries]


def render_scoreboard(project: Path, now: datetime | None = None) -> str:
    """Render the committed ``scoreboard.md`` text.

    One Markdown table per work type, types in :data:`WORK_TYPES` order and
    executors by score descending (unrated last), with the columns executor,
    score, n, attempts per pass, mean steps, mean tokens (``~`` when any count
    was estimated), mean duration and last graded date. Corrupt job files are
    listed in a ``Skipped records`` section instead of crashing the
    scoreboard.
    """
    from . import scoring
    path_cls = getattr(scoring, "Path", Path)
    half_life = getattr(scoring, "DEFAULT_HALF_LIFE_DAYS", DEFAULT_HALF_LIFE_DAYS)
    stats, skipped = scoring._collect(path_cls(project), half_life, now)
    grouped = scoring._by_type(stats)

    lines: list[str] = ["# Scoreboard", ""]
    if not grouped:
        lines.extend(["_No graded pieces yet._", ""])

    for work_type, entries in grouped:
        lines.append(f"## {work_type}")
        lines.append("")
        lines.append(
            "| executor | score | n | attempts/pass | mean steps | mean tokens "
            "| mean duration | last graded |"
        )
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
        for stat in entries:
            last = stat.last[:10] if stat.last else "—"
            lines.append(
                f"| {stat.executor} | {scoring._score_cell(stat.score)} | {stat.n} "
                f"| {scoring._score_cell(stat.attempts_per_pass)} "
                f"| {scoring._score_cell(stat.mean_steps)} "
                f"| {scoring._tokens_cell(stat.mean_tokens, stat.tokens_estimated)} "
                f"| {scoring._duration_cell(stat.mean_duration_s)} | {last} |"
            )
        lines.append("")

    if skipped:
        lines.extend(["## Skipped records", ""])
        for path, error in skipped:
            reason = (error or "unreadable record").splitlines()[0]
            prefix = f"{path}: "
            if reason.startswith(prefix):
                reason = reason[len(prefix):]
            lines.append(f"- {path.name}: {reason}")
        lines.append("")

    return "\n".join(lines)


from . import scoring  # facade for monkeypatchable dependencies
