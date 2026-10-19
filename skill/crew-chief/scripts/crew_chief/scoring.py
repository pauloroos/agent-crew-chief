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
  a second chance on low-risk work. Executors tied on score are spread over
  the job (least used first, then a tool the job has used least), and an
  executor that hit its quota recently (:func:`quota_cooldown`) sits out with
  every executor of its :func:`quota_family`. Only tools whose
  ``work_types`` take the piece's type are candidates; execution types
  (:data:`EXEC_TYPES`) try ``exec_first`` tools first and never escalate to a
  paid executor unless the coordinator asks (``strong``).

:func:`free_failures` counts the free-executor failures on one piece, which is
the signal that stops free retries and escalates thinking work.
"""

from __future__ import annotations

import random
import re
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Prefs, tool_work_types
from .records import EXEC_TYPES, WORK_TYPES, iter_jobs
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
    #: Exec attempts (any status but ``brief_refused``) per piece that passed.
    attempts_per_pass: float | None = None
    #: Mean agent steps and tokens of the ``ok`` exec attempts that report them.
    mean_steps: float | None = None
    mean_tokens: float | None = None
    #: Whether any token count behind ``mean_tokens`` is a chars / 4 estimate.
    tokens_estimated: bool = False


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
    # (executor, type) -> {"runs", "passed", "steps", "tokens", "estimated"}.
    metrics: dict[tuple[str, str], dict] = {}
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
                if _is_number(attempt.get("steps")):
                    metric["steps"].append(attempt["steps"])
                if _is_number(attempt.get("tokens_total")):
                    metric["tokens"].append(attempt["tokens_total"])
                    if attempt.get("tokens_estimated"):
                        metric["estimated"] = True
                duration = attempt.get("duration_s")
                if not _is_number(duration):
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
        metric = metrics.get(key) or {
            "runs": 0, "passed": 0, "steps": [], "tokens": [], "estimated": False
        }
        stats[key] = Stat(
            executor=key[0],
            type=key[1],
            n=count,
            score=weighted / weights if weights else None,
            mean_duration_s=duration[0] / duration[1] if duration else None,
            last=newest.isoformat() if newest is not None else None,
            attempts_per_pass=(
                metric["runs"] / metric["passed"] if metric["passed"] else None
            ),
            mean_steps=_mean(metric["steps"]),
            mean_tokens=_mean(metric["tokens"]),
            tokens_estimated=metric["estimated"],
        )
    return stats, skipped


def _is_number(value: object) -> bool:
    """Whether ``value`` is an int or float (a bool is not a number here)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _mean(values: list[float]) -> float | None:
    """Arithmetic mean, or ``None`` for an empty list."""
    return sum(values) / len(values) if values else None


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
    recent ``graded_at``. ``attempts_per_pass`` divides that executor's exec
    attempts (every status but ``brief_refused``, which never ran) by the
    pieces it got an ``ok`` on; ``mean_steps`` and ``mean_tokens`` average the
    ``ok`` exec attempts that carry them, and ``tokens_estimated`` says when
    any of those token counts was an estimate. Corrupt jobs are skipped.
    """
    return _collect(Path(project), half_life_days, now)[0]


def job_usage(job: dict | None) -> tuple[dict[str, int], dict[str, int]]:
    """How much each executor and each tool is already used in ``job``.

    Returns ``(by_executor, by_tool)``. On each piece an executor counts
    ``max(picks, exec attempts)`` there, so a pick followed by its dispatch is
    one use while a dispatch made without ``pick`` still counts. The tool is
    the part of the executor id before the first ``:``.
    """
    by_executor: dict[str, int] = {}
    for piece in (job or {}).get("pieces") or []:
        if not isinstance(piece, dict):
            continue
        picks: dict[str, int] = {}
        for entry in piece.get("picks") or []:
            executor = entry.get("pick") if isinstance(entry, dict) else None
            if isinstance(executor, str) and executor:
                picks[executor] = picks.get(executor, 0) + 1
        attempts: dict[str, int] = {}
        for attempt in piece.get("attempts") or []:
            if not isinstance(attempt, dict) or attempt.get("role") != "exec":
                continue
            executor = attempt.get("executor")
            if isinstance(executor, str) and executor:
                attempts[executor] = attempts.get(executor, 0) + 1
        for executor in {*picks, *attempts}:
            by_executor[executor] = by_executor.get(executor, 0) + max(
                picks.get(executor, 0), attempts.get(executor, 0)
            )
    by_tool: dict[str, int] = {}
    for executor, count in by_executor.items():
        tool = executor.split(":", 1)[0]
        by_tool[tool] = by_tool.get(tool, 0) + count
    return by_executor, by_tool


def _rank_key(
    executor: Executor,
    stats: dict,
    type: str,
    usage: tuple[dict[str, int], dict[str, int]] = ({}, {}),
) -> tuple:
    """Sort key for a pool.

    Score desc; on a tie, the quota family (see :func:`quota_family`) least
    used in the job, then the executor least used, then the one whose tool
    the job used least; then duration asc (missing last), id asc.
    """
    stat = stats.get((executor.id, type))
    score = _UNRATED_SCORE
    if stat is not None and stat.score is not None:
        score = stat.score
    duration = float("inf")
    if stat is not None and stat.mean_duration_s is not None:
        duration = stat.mean_duration_s
    by_executor, by_tool = usage
    tool = executor.id.split(":", 1)[0]
    family = quota_family(executor.id)
    family_use = sum(
        count
        for other, count in by_executor.items()
        if quota_family(other) == family
    )
    return (
        -score,
        family_use,
        by_executor.get(executor.id, 0),
        by_tool.get(tool, 0),
        duration,
        executor.id,
    )


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
    job: dict | None = None,
    cooling: set[str] | None = None,
    strong: bool = False,
) -> tuple[Executor | None, str]:
    """Choose the next executor and return ``(choice, one-line reason)``.

    Free executors come first, ranked by decayed score descending (unrated
    get ``-1``). Ties go to the executor least used in ``job`` (picks and
    exec attempts on its pieces, see :func:`job_usage`), then to a tool the
    job has used least, then by mean duration ascending with ``None`` last,
    then by id. On ``low`` risk, ``prefs.explore_rate`` of the time an
    executor that is unrated or below the pool median is given a second
    chance and picked uniformly. With no free executor the paid ones are
    tried in ``prefs.strong_order``; with neither pool populated there is no
    choice.

    Only executors whose tool takes ``type`` (:func:`config.tool_work_types`)
    are candidates; the reason then ends with ``; not for <type>: <tools>``,
    one name per tool left out. On an execution type (:data:`EXEC_TYPES`)
    the free pool is first narrowed to the tools in ``prefs.exec_first``
    when any of them is available, and the paid pool is only used when
    ``strong`` is set: without it, an empty free pool means no choice and a
    reason telling the coordinator to split the piece or do it itself.
    Thinking types escalate to paid executors on their own.

    Executors whose :func:`quota_family` matches one in ``cooling`` (see
    :func:`quota_cooldown`) are left out like excluded ones, and the reason
    ends with ``; skipped for quota: <ids>``.
    """
    unfit = sorted(
        {
            executor.tool.get("name", executor.id.split(":", 1)[0])
            for executor in candidates
            if type in WORK_TYPES
            and type not in tool_work_types(executor.tool)
            and executor.cost != "forbidden"
        }
    )
    candidates = [
        executor
        for executor in candidates
        if executor.tool.get("name", executor.id.split(":", 1)[0]) not in unfit
    ]
    families = {quota_family(executor_id) for executor_id in cooling or ()}
    skipped = sorted(
        executor.id
        for executor in candidates
        if quota_family(executor.id) in families
        and executor.id not in exclude
        and executor.cost != "forbidden"
    )
    choice, reason = _pick(
        [executor for executor in candidates if executor.id not in skipped],
        stats,
        type,
        risk,
        prefs,
        exclude,
        rng,
        job_usage(job),
        strong,
    )
    if unfit:
        reason += f"; not for {type}: {', '.join(unfit)}"
    if skipped:
        reason += f"; skipped for quota: {', '.join(skipped)}"
    return choice, reason


def _pick(
    candidates: list[Executor],
    stats: dict,
    type: str,
    risk: str,
    prefs: Prefs,
    exclude: set[str],
    rng: random.Random,
    usage: tuple[dict[str, int], dict[str, int]],
    allow_paid_exec: bool = False,
) -> tuple[Executor | None, str]:
    """:func:`pick` once unfit and cooling executors are left out."""
    available = [
        executor for executor in candidates if executor.id not in exclude
    ]
    free = [executor for executor in available if executor.cost == "free"]
    strong = [executor for executor in available if executor.cost == "paid"]
    strong.sort(key=lambda executor: _strong_index(executor, prefs))
    execution = type in EXEC_TYPES
    if execution:
        first = [
            executor
            for executor in free
            if executor.tool.get("name") in prefs.exec_first
        ]
        if first:
            free = first
        if not allow_paid_exec:
            strong = []

    if free:
        pool = sorted(
            free, key=lambda executor: _rank_key(executor, stats, type, usage)
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

    if execution and not allow_paid_exec:
        return None, (
            f"no executor available: no free executor left for {type}; split "
            f"the piece into smaller pieces or do it yourself (paid executors "
            f"run {type} only on --strong)"
        )
    return None, "no executor available"


#: Trailing effort suffix of a model id, dropped by :func:`quota_family`.
_EFFORT_SUFFIX = re.compile(r"-(minimal|low|medium|high|xhigh|max)$")


def quota_family(executor_id: str) -> str:
    """The group of executors that share one quota with ``executor_id``.

    The model's trailing effort suffix (``-minimal`` ... ``-max``) is dropped,
    since every effort of a model draws on the same quota. Antigravity shares
    its quota across a whole vendor, so there the family is
    ``antigravity:<model up to the first '-'>`` (``antigravity:claude``,
    ``antigravity:gemini``); any other tool keeps ``<tool>:<model>``.
    """
    tool, _, model = executor_id.partition(":")
    model = _EFFORT_SUFFIX.sub("", model)
    if tool == "antigravity":
        model = model.split("-", 1)[0]
    return f"{tool}:{model}"


def quota_cooldown(
    project: Path, minutes: float, now: datetime | None = None
) -> set[str]:
    """Executors with an attempt that ended in ``quota`` in the last ``minutes``.

    Every job of the project counts, whatever the role of the attempt. An
    attempt ends at ``started + duration_s``; one without a parseable
    ``started`` cannot be aged and is ignored. ``minutes <= 0`` turns the
    cooldown off. Corrupt jobs are skipped.
    """
    if minutes <= 0:
        return set()
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    window = timedelta(minutes=minutes)

    cooling: set[str] = set()
    for job, _path, _error in iter_jobs(Path(project)):
        if job is None:
            continue
        for piece in job.get("pieces") or []:
            if not isinstance(piece, dict):
                continue
            for attempt in piece.get("attempts") or []:
                if not isinstance(attempt, dict):
                    continue
                if attempt.get("status") != "quota":
                    continue
                executor = attempt.get("executor")
                started = attempt.get("started")
                if not isinstance(executor, str) or not executor:
                    continue
                if not isinstance(started, str):
                    continue
                ended = _parse_iso(started)
                if ended is None:
                    continue
                duration = attempt.get("duration_s")
                if isinstance(duration, (int, float)) and not isinstance(
                    duration, bool
                ):
                    ended += timedelta(seconds=duration)
                if now - ended <= window:
                    cooling.add(executor)
    return cooling


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
    At 2, on thinking work, the CLI calls :func:`pick` with every free
    executor excluded, so the choice escalates; execution work stays free
    (see SKILL.md).
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


def _tokens_cell(value: float | None, estimated: bool) -> str:
    """Mean tokens as an integer, ``~`` in front when any count was estimated."""
    if value is None:
        return "—"
    return f"{'~' if estimated else ''}{round(value)}"


def render_scoreboard(project: Path, now: datetime | None = None) -> str:
    """Render the committed ``scoreboard.md`` text.

    One Markdown table per work type, types in :data:`WORK_TYPES` order and
    executors by score descending (unrated last), with the columns executor,
    score, n, attempts per pass, mean steps, mean tokens (``~`` when any count
    was estimated), mean duration and last graded date. Corrupt job files are
    listed in a ``Skipped records`` section instead of crashing the
    scoreboard.
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
        lines.append(
            "| executor | score | n | attempts/pass | mean steps | mean tokens "
            "| mean duration | last graded |"
        )
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
        for stat in entries:
            last = stat.last[:10] if stat.last else "—"
            lines.append(
                f"| {stat.executor} | {_score_cell(stat.score)} | {stat.n} "
                f"| {_score_cell(stat.attempts_per_pass)} "
                f"| {_score_cell(stat.mean_steps)} "
                f"| {_tokens_cell(stat.mean_tokens, stat.tokens_estimated)} "
                f"| {_duration_cell(stat.mean_duration_s)} | {last} |"
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
