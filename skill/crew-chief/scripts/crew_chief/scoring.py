"""Grading, decayed scoreboard stats and free-first picking.

Three jobs live here:

* :func:`grade` turns the outcome of one piece (adherence, corrections,
  extras and named penalties) into the score stored under the piece's
  ``grade`` key by the coordinator.
* :func:`build_stats` folds every graded piece on disk into a per
  ``(executor, type)`` :class:`Stat`, weighting each grade by how recent it
  is (a half-life decay, so a stale 100 fades against a fresh 20).
* :func:`pick` chooses the next executor, free-first, by decayed score, with
  a small exploration chance that gives an unrated or below-median executor
  a second chance on low-risk work.

:func:`free_failures` counts the free-executor failures on one piece, which is
the signal the coordinator uses to stop retrying free executors and escalate.
"""

from __future__ import annotations

import random
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import Prefs
from .records import WORK_TYPES, iter_jobs
from .tools import Executor

#: Deductions applied by :func:`grade`, in score points.
PENALTIES = {
    "destructive": 40,
    "outside_write": 60,
    "false_done": 40,
    "model_mismatch": 30,
}

#: Default half-life, in days, of a stored grade's weight in :func:`build_stats`.
DEFAULT_HALF_LIFE_DAYS = 30

#: Score used for an executor with no history when ranking a pool.
_UNRATED_SCORE = -1.0


def _num(value: float) -> str:
    """Format ``value`` for humans: no trailing ``.0``, one decimal otherwise."""
    rounded = round(float(value), 1)
    if rounded == int(rounded):
        return str(int(rounded))
    return str(rounded)


def grade(
    corrections: int,
    done: int,
    missing: int,
    extra: int,
    penalties: list[str],
) -> dict:
    """Score one piece from its adherence and its mistakes.

    The formula, with the values fixed by the plan::

        adherence = done / (done + missing)   (1.0 when done + missing == 0)
        score = 100*adherence - 15*corrections - 5*extra - sum(PENALTIES[p])
        score = clamp(round(score), 0, 100)

    ``explain`` is a one-line English breakdown of every term. An unknown
    penalty raises :class:`ValueError`.
    """
    for name in penalties:
        if name not in PENALTIES:
            raise ValueError(
                f"unknown penalty {name!r} "
                f"(expected one of {', '.join(PENALTIES)})"
            )

    total = done + missing
    adherence = done / total if total else 1.0
    raw = (
        100 * adherence
        - 15 * corrections
        - 5 * extra
        - sum(PENALTIES[name] for name in penalties)
    )
    score = max(0, min(100, round(raw)))

    terms = [f"adherence {done}/{total} → {_num(100 * adherence)}"]
    if corrections:
        word = "correction" if corrections == 1 else "corrections"
        terms.append(f"{corrections} {word} −{15 * corrections}")
    else:
        terms.append("0 corrections")
    if extra:
        terms.append(f"{extra} extra −{5 * extra}")
    else:
        terms.append("0 extra")
    if penalties:
        listed = ", ".join(f"{name} −{PENALTIES[name]}" for name in penalties)
        terms.append(f"penalties {listed}")
    else:
        terms.append("penalties none")
    explain = "; ".join(terms) + f" → {score}"

    return {
        "score": score,
        "explain": explain,
        "corrections": corrections,
        "done": done,
        "missing": missing,
        "extra": extra,
        "penalties": list(penalties),
    }


@dataclass
class Stat:
    """Decayed history of one executor on one work type."""

    executor: str
    type: str
    n: int
    score: float | None
    mean_duration_s: float | None
    last: str | None


def _parse_iso(text: str) -> datetime | None:
    """Parse an ISO 8601 string as an aware datetime; ``None`` when invalid.

    A string without an offset is read as UTC; stored grades are always
    timezone-aware UTC, so this only guards hand-edited records.
    """
    try:
        when = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when


def _collect(
    project: Path,
    half_life_days: float,
    now: datetime | None,
) -> tuple[dict[tuple[str, str], Stat], list[tuple[Path, str]]]:
    """Read every job and return ``(stats, skipped)``.

    ``skipped`` pairs the path of each corrupt job with the error text, for
    :func:`render_scoreboard`. A grade without an executor or a parseable
    ``graded_at`` cannot be keyed or aged and is ignored.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    # (executor, type) -> [weighted score sum, weight sum, graded pieces, newest].
    graded: dict[tuple[str, str], list] = {}
    # (executor, type) -> [duration sum, ok attempt count].
    durations: dict[tuple[str, str], list] = {}
    skipped: list[tuple[Path, str]] = []

    for job, path, error in iter_jobs(project):
        if job is None:
            skipped.append((path, error or "unreadable record"))
            continue
        for piece in job.get("pieces") or []:
            if not isinstance(piece, dict):
                continue
            work_type = piece.get("type")
            if work_type not in WORK_TYPES:
                continue

            for attempt in piece.get("attempts") or []:
                if not isinstance(attempt, dict):
                    continue
                if attempt.get("role") != "exec":
                    continue
                if attempt.get("status") != "ok":
                    continue
                duration = attempt.get("duration_s")
                if isinstance(duration, bool):
                    continue
                if not isinstance(duration, (int, float)):
                    continue
                executor = attempt.get("executor")
                if not isinstance(executor, str) or not executor:
                    continue
                accumulator = durations.setdefault(
                    (executor, work_type), [0.0, 0]
                )
                accumulator[0] += duration
                accumulator[1] += 1

            grade_value = piece.get("grade")
            if not isinstance(grade_value, dict):
                continue
            score = grade_value.get("score")
            executor = grade_value.get("executor")
            graded_at = grade_value.get("graded_at")
            if score is None or isinstance(score, bool):
                continue
            if not isinstance(score, (int, float)):
                continue
            if not isinstance(executor, str) or not executor:
                continue
            if not isinstance(graded_at, str):
                continue
            when = _parse_iso(graded_at)
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
        stats[key] = Stat(
            executor=key[0],
            type=key[1],
            n=count,
            score=weighted / weights if weights else None,
            mean_duration_s=duration[0] / duration[1] if duration else None,
            last=newest.isoformat() if newest is not None else None,
        )
    return stats, skipped


def build_stats(
    project: Path,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
    now: datetime | None = None,
) -> dict[tuple[str, str], Stat]:
    """Decayed history per ``(executor, type)`` over every graded piece.

    Only graded pieces count; a piece graded as ``review`` is keyed under the
    ``review`` type like any other. Each grade's weight is
    ``0.5 ** (age_days / half_life_days)`` and the score is the weighted mean.
    ``mean_duration_s`` is the mean ``duration_s`` of that executor's ``exec``
    attempts with status ``ok`` on pieces of that type; ``last`` is the most
    recent ``graded_at``. Corrupt jobs are skipped.
    """
    return _collect(Path(project), half_life_days, now)[0]


def _rank_key(executor: Executor, stats: dict, type: str) -> tuple:
    """Sort key for a pool: score desc, duration asc (missing last), id asc."""
    stat = stats.get((executor.id, type))
    score = _UNRATED_SCORE
    if stat is not None and stat.score is not None:
        score = stat.score
    duration = float("inf")
    if stat is not None and stat.mean_duration_s is not None:
        duration = stat.mean_duration_s
    return (-score, duration, executor.id)


def _second_chance_candidates(
    pool: list[Executor], stats: dict, type: str
) -> list[Executor]:
    """Pool members that are unrated or score below the pool median.

    The median is taken over the rated scores in the pool only. With no rated
    executor at all, every member is a candidate.
    """
    rated = [stats.get((executor.id, type)) for executor in pool]
    scores = [
        stat.score
        for stat in rated
        if stat is not None and stat.score is not None
    ]
    if not scores:
        return list(pool)
    median = statistics.median(scores)

    candidates: list[Executor] = []
    for executor, stat in zip(pool, rated):
        score = stat.score if stat is not None else None
        if score is None or score < median:
            candidates.append(executor)
    return candidates


def _second_chance_reason(executor: Executor, stats: dict, type: str) -> str:
    """One-line reason for an exploratory pick, for the job record."""
    stat = stats.get((executor.id, type))
    if stat is None or stat.score is None:
        return f"second chance: unrated {executor.id}"
    return (
        f"second chance: {executor.id} below median "
        f"(score {_num(stat.score)})"
    )


def _strong_index(executor: Executor, prefs: Prefs) -> int:
    """Position of the executor's tool in ``prefs.strong_order``; unlisted last."""
    name = executor.tool.get("name")
    if name in prefs.strong_order:
        return prefs.strong_order.index(name)
    return len(prefs.strong_order)


def pick(
    candidates: list[Executor],
    stats: dict,
    type: str,
    risk: str,
    prefs: Prefs,
    exclude: set[str],
    rng: random.Random,
) -> tuple[Executor | None, str]:
    """Choose the next executor and return ``(choice, one-line reason)``.

    Free executors come first, ranked by decayed score descending (unrated
    get ``-1``, ties break by mean duration ascending with ``None`` last, then
    by id). On ``low`` risk, ``prefs.explore_rate`` of the time an executor
    that is unrated or below the pool median is given a second chance and
    picked uniformly. With no free executor the paid ones are tried in
    ``prefs.strong_order``; with neither pool populated there is no choice.
    """
    available = [
        executor for executor in candidates if executor.id not in exclude
    ]
    free = [executor for executor in available if executor.cost == "free"]
    strong = [executor for executor in available if executor.cost == "paid"]
    strong.sort(key=lambda executor: _strong_index(executor, prefs))

    if free:
        pool = sorted(
            free, key=lambda executor: _rank_key(executor, stats, type)
        )

        # Second chance. This uses rng.random() once for the explore check and
        # once more for the uniform choice, so a test stub whose random()
        # always returns 0.0 drives both: 0.0 < explore_rate is true and the
        # uniform index is int(0.0 * len(candidates)) == 0, the first of the
        # ranked second-chance candidates.
        if risk == "low" and rng.random() < prefs.explore_rate:
            candidates_for_chance = _second_chance_candidates(
                pool, stats, type
            )
            if candidates_for_chance:
                chosen = candidates_for_chance[
                    int(rng.random() * len(candidates_for_chance))
                ]
                return chosen, _second_chance_reason(chosen, stats, type)

        chosen = pool[0]
        stat = stats.get((chosen.id, type))
        if stat is not None and stat.score is not None:
            return chosen, f"best score {_num(stat.score)} ({chosen.id})"
        return chosen, f"unrated, no history ({chosen.id})"

    if strong:
        chosen = strong[0]
        return chosen, f"escalated: no free executor available ({chosen.id})"

    return None, "no executor available"


def _find_piece(job: dict, piece: int) -> dict:
    """The piece numbered ``piece``; :class:`ValueError` when it is absent."""
    for candidate in job.get("pieces") or []:
        if candidate.get("n") == piece:
            return candidate
    raise ValueError(f"job {job.get('id')!r} has no piece {piece}")


def free_failures(job: dict, piece: int) -> int:
    """How many times a free executor failed on ``piece``.

    Counts attempts with role ``exec``, cost ``free`` and a status other than
    ``ok``, plus corrections drawn by the executor of such a free attempt.
    At 2 the coordinator calls :func:`pick` with every free executor excluded,
    so the choice escalates (see SKILL.md).
    """
    target = _find_piece(job, piece)
    attempts = target.get("attempts") or []
    free_executors = {
        attempt.get("executor")
        for attempt in attempts
        if attempt.get("role") == "exec"
        and attempt.get("cost") == "free"
        and isinstance(attempt.get("executor"), str)
        and attempt.get("executor")
    }

    failures = sum(
        1
        for attempt in attempts
        if attempt.get("role") == "exec"
        and attempt.get("cost") == "free"
        and attempt.get("status") != "ok"
    )
    failures += sum(
        1
        for correction in target.get("corrections") or []
        if correction.get("executor") in free_executors
    )
    return failures


def _duration_cell(value: float | None) -> str:
    """``42s`` for a duration; an em dash when it is unknown."""
    if value is None:
        return "—"
    return f"{round(value)}s"


def _score_cell(value: float | None) -> str:
    """The score as text; an em dash when the executor has no score."""
    if value is None:
        return "—"
    return _num(value)


def render_scoreboard(project: Path, now: datetime | None = None) -> str:
    """Render the committed ``scoreboard.md`` text.

    One Markdown table per work type, types in :data:`WORK_TYPES` order and
    executors by score descending (unrated last), with the columns executor,
    score, n, mean duration and last graded date. Corrupt job files are listed
    in a ``Skipped records`` section instead of crashing the scoreboard.
    """
    stats, skipped = _collect(Path(project), DEFAULT_HALF_LIFE_DAYS, now)

    by_type: dict[str, list[Stat]] = {}
    for stat in stats.values():
        by_type.setdefault(stat.type, []).append(stat)

    lines: list[str] = ["# Scoreboard", ""]
    if not by_type:
        lines.extend(["_No graded pieces yet._", ""])

    # Fixed vocabulary first, then any stray type a hand-edited record uses.
    ordered_types = [*WORK_TYPES, *sorted(set(by_type) - set(WORK_TYPES))]
    for work_type in ordered_types:
        entries = by_type.get(work_type)
        if not entries:
            continue
        entries.sort(
            key=lambda stat: (
                float("inf") if stat.score is None else -stat.score,
                stat.executor,
            )
        )
        lines.append(f"## {work_type}")
        lines.append("")
        lines.append("| executor | score | n | mean duration | last graded |")
        lines.append("| --- | --- | --- | --- | --- |")
        for stat in entries:
            last = stat.last[:10] if stat.last else "—"
            lines.append(
                f"| {stat.executor} | {_score_cell(stat.score)} | {stat.n} "
                f"| {_duration_cell(stat.mean_duration_s)} | {last} |"
            )
        lines.append("")

    if skipped:
        lines.extend(["## Skipped records", ""])
        for path, error in skipped:
            lines.append(f"- {error}")
        lines.append("")

    return "\n".join(lines)
