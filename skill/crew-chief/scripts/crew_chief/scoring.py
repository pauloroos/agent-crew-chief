"""Grading, decayed scoreboard stats and free-first picking.

Three jobs live here:

* :func:`grade` turns the outcome of one piece (adherence, corrections,
  extras and named penalties) into the score the coordinator stores for one
  executor under the piece's ``grades`` map (one grade per executor that
  worked on the piece).
* :func:`build_stats` folds every grade on disk into a per
  ``(executor, type)`` :class:`Stat`, weighting each grade by how recent it
  is (a half-life decay, so a stale 100 fades against a fresh 20).
* :func:`pick` chooses the next executor by climbing the cost ladder
  (``free``, ``included``, ``cheap``, ``paid``): a tier is left only when
  nobody is available in it, ``cheap`` only within the spend budget, and
  execution types (:data:`EXEC_TYPES`) reach ``paid`` only when the
  coordinator asks (``strong``). Inside a tier the choice is by decayed score,
  with a small exploration chance on low-risk work. Executors tied on score
  are spread over the job (least used first, then a tool the job has used
  least), an executor that hit its quota recently (:func:`quota_cooldown`)
  sits out with every executor of its :func:`quota_family`, and the lowest
  effort that suits the piece wins.

:func:`free_failures` counts the free-executor failures on one piece, which is
the signal that stops free retries and sends thinking work to the next tier.
"""

from __future__ import annotations

import random
import re
import statistics
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Prefs, tool_work_types
from .records import EXEC_TYPES, WORK_TYPES, iter_jobs, piece_grades
from .spend import PROBE_NOTE
from .tiers import TIER_ORDER
from .tools import Executor, wants_effort

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
    :func:`render_scoreboard`. Every executor graded on a piece counts (see
    :func:`records.piece_grades`, which also reads the single ``grade`` of
    older records). A grade without an executor or a parseable
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

            for grade_value in piece_grades(piece).values():
                score = grade_value.get("score")
                executor = grade_value.get("executor")
                graded_at = grade_value.get("graded_at")
                if not _is_number(score):
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
    costs: dict[str, str] | None = None,
) -> tuple:
    """Sort key for a pool.

    Score desc; on a tie, the quota family (see :func:`quota_family`) least
    used in the job, then the executor least used, then the one whose tool
    the job used least; then duration asc (missing last), id asc. ``costs``
    maps executor ids to their cost, so subscription families (``included``)
    are told apart from the plain model family.
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
    costs = costs or {}
    family = quota_family(executor.id, executor.cost)
    family_use = sum(
        count
        for other, count in by_executor.items()
        if quota_family(other, costs.get(other, "")) == family
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


#: Budget callback of :func:`pick`: ``None`` when the executor may spend money,
#: else the reason it may not (``"budget US$ 0 reached (cheap_budget_usd_day)"``).
#: Called with ``None`` it answers only for the tier as a whole (the day and
#: job ceilings). An object with ``is_probe(executor)`` (see
#: :class:`spend.BudgetCheck`) marks the one probe attempt of an executor
#: whose cost is unknown.
Budget = Callable[["Executor | None"], "str | None"]


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
    piece: int | None = None,
    budget: Budget | None = None,
    coordinator_tool: str | None = None,
    allow_own_cli: bool = False,
) -> tuple[Executor | None, str]:
    """Choose the next executor and return ``(choice, one-line reason)``.

    The choice climbs the cost ladder (:data:`tiers.TIER_ORDER`): ``free``,
    then ``included`` (covered by a subscription the user already pays),
    then ``cheap`` (pay per use, only within the spend budget), then
    ``paid``. A tier is left only when nobody is available in it (excluded,
    already tried on the piece, or paused for quota), never because a
    free executor merely failed. Execution types (:data:`EXEC_TYPES`) stop at
    ``cheap`` and reach ``paid`` only with ``strong``; thinking types may
    reach ``paid``. The reason ends with ``; tier <name>`` and, when the
    choice sits above ``free``, ``; climbed: <tier below> exhausted``.

    Inside a tier executors are ranked by decayed score descending (unrated
    get ``-1``). Ties go to the executor least used in ``job`` (picks and
    exec attempts on its pieces, see :func:`job_usage`), then to a tool the
    job has used least, then by mean duration ascending with ``None`` last,
    then by id. In the ``free`` and ``included`` tiers, on ``low`` risk,
    ``prefs.explore_rate`` of the time an executor that is unrated or below
    the pool median is given a second chance and picked uniformly. The
    ``paid`` tier is ordered by ``prefs.strong_order``.

    ``budget(executor)`` decides whether a ``cheap`` executor may be used:
    ``None`` allows it, a string is the reason it is skipped (the reason of
    the pick lists them). Without a callback the cheap tier is open only when
    ``prefs.cheap_budget_usd_day`` is above zero. With every tier empty on an
    execution type the reason tells the coordinator to split the piece or do
    it itself, and adds ``run setup to allow cheap models`` when the budget
    kept cheap executors out.

    ``coordinator_tool`` names the CLI that is coordinating: in the
    ``included`` tier its executors (same tool, or same subscription family)
    go last unless ``allow_own_cli``.

    Only executors whose tool takes ``type`` (:func:`config.tool_work_types`)
    are candidates; the reason then ends with ``; not for <type>: <tools>``,
    one name per tool left out. On an execution type each tier's pool is
    first narrowed to the tools in ``prefs.exec_first`` when any of them is
    available there.

    Executors whose :func:`quota_family` matches one in ``cooling`` (see
    :func:`quota_cooldown`) are left out like excluded ones, and the reason
    ends with ``; skipped for quota: <ids>``.

    Among the variants of one model that differ only by effort suffix, every
    tier takes the lowest :func:`effort_rank` the :func:`effort_ceiling` of
    ``piece`` allows, so effort rises after a failure on the piece before
    anything else escalates; the reason then says ``effort medium (low
    failed 1×)``.
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
    costs = {executor.id: executor.cost for executor in candidates}
    families = {
        quota_family(executor_id, costs.get(executor_id, ""))
        for executor_id in cooling or ()
    }
    skipped = sorted(
        executor.id
        for executor in candidates
        if quota_family(executor.id, executor.cost) in families
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
        job,
        piece,
        budget,
        coordinator_tool,
        allow_own_cli,
        frozenset(executor.cost for executor in candidates),
    )
    if unfit:
        reason += f"; not for {type}: {', '.join(unfit)}"
    if skipped:
        reason += f"; skipped for quota: {', '.join(skipped)}"
    return choice, reason


def _default_budget(prefs: Prefs) -> Budget:
    """The budget check when the caller gives none: only the daily ceiling."""

    def check(_executor: Executor | None) -> str | None:
        if prefs.cheap_budget_usd_day <= 0:
            return (
                f"budget US$ {prefs.cheap_budget_usd_day:g} reached "
                "(cheap_budget_usd_day)"
            )
        return None

    return check


def _blocked_note(blocked: dict[str, list[str]]) -> str:
    """``cheap skipped: <reason> [ids]`` for each reason, ids capped at 3."""
    parts = []
    for why, ids in blocked.items():
        shown = ", ".join(ids[:3])
        if len(ids) > 3:
            shown += f", +{len(ids) - 3} more"
        parts.append(f"cheap skipped: {why} [{shown}]")
    return "; ".join(parts)


def _is_own_cli(executor: Executor, coordinator_tool: str) -> bool:
    """Whether ``executor`` belongs to the coordinator's CLI or subscription."""
    tool = executor.id.split(":", 1)[0]
    return coordinator_tool in (executor.tool.get("name"), tool) or (
        quota_family(executor.id, executor.cost)
        == f"{_SUB_PREFIX}{coordinator_tool}"
    )


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
    job: dict | None = None,
    piece: int | None = None,
    budget: Budget | None = None,
    coordinator_tool: str | None = None,
    allow_own_cli: bool = False,
    known_tiers: frozenset[str] = frozenset(),
) -> tuple[Executor | None, str]:
    """:func:`pick` once unfit and cooling executors are left out."""
    available = [
        executor for executor in candidates if executor.id not in exclude
    ]
    execution = type in EXEC_TYPES
    last = "paid" if (allow_paid_exec or not execution) else "cheap"
    ladder = TIER_ORDER[: TIER_ORDER.index(last) + 1]
    costs = {executor.id: executor.cost for executor in candidates}
    check = budget or _default_budget(prefs)
    blocked: dict[str, list[str]] = {}
    seen = set(known_tiers) | {executor.cost for executor in candidates}

    for tier in ladder:
        pool = [executor for executor in available if executor.cost == tier]
        if tier == "cheap":
            allowed = []
            for executor in pool:
                why = check(executor)
                if why is None:
                    allowed.append(executor)
                else:
                    blocked.setdefault(why, []).append(executor.id)
            pool = allowed
        if not pool:
            continue

        suffix = ""
        if blocked:
            suffix += f"; {_blocked_note(blocked)}"
        if tier == "paid":
            pool = sorted(
                _lowest_effort(pool, job, piece),
                key=lambda executor: _strong_index(executor, prefs),
            )
            chosen = pool[0]
            base = f"escalated: no free executor available ({chosen.id})"
        else:
            if execution:
                first = [
                    executor
                    for executor in pool
                    if executor.tool.get("name") in prefs.exec_first
                ]
                if first:
                    pool = first
            if tier == "included" and coordinator_tool and not allow_own_cli:
                own = [e for e in pool if _is_own_cli(e, coordinator_tool)]
                others = [e for e in pool if e not in own]
                if own and others:
                    pool = others
                    suffix += f"; coordinator {coordinator_tool} kept last"
                elif own:
                    suffix += (
                        f"; coordinator {coordinator_tool} used: "
                        "no other included executor left"
                    )
            pool = sorted(
                _lowest_effort(pool, job, piece),
                key=lambda executor: _rank_key(
                    executor, stats, type, usage, costs
                ),
            )
            chosen, base = _choose_in_pool(
                pool, stats, type, risk, prefs, rng, tier
            )
        if tier == "cheap":
            probe = getattr(check, "is_probe", None)
            if probe is not None and probe(chosen):
                suffix += f"; {PROBE_NOTE}"
        note = _effort_note(chosen, job, piece)
        if note:
            suffix += f"; {note}"
        reason = f"{base}{suffix}; tier {tier}"
        if tier != ladder[0]:
            reason += f"; climbed: {_tier_below(tier, seen)} exhausted"
        return chosen, reason

    return None, _no_executor(
        type, execution, allow_paid_exec, prefs, blocked, check(None)
    )


def _tier_below(tier: str, seen: set[str]) -> str:
    """The nearest tier under ``tier`` that has executors, else ``free``."""
    for name in reversed(TIER_ORDER[: TIER_ORDER.index(tier)]):
        if name in seen:
            return name
    return TIER_ORDER[0]


def _choose_in_pool(
    pool: list[Executor],
    stats: dict,
    type: str,
    risk: str,
    prefs: Prefs,
    rng: random.Random,
    tier: str,
) -> tuple[Executor, str]:
    """The ranked ``pool``'s pick and its base reason (second chance included)."""
    # Second chance. This uses rng.random() once for the explore check and
    # once more for the uniform choice, so a test stub whose random()
    # always returns 0.0 drives both: 0.0 < explore_rate is true and the
    # uniform index is int(0.0 * len(candidates)) == 0, the first of the
    # ranked second-chance candidates. Money is never spent exploring.
    if tier != "cheap" and risk == "low" and rng.random() < prefs.explore_rate:
        candidates_for_chance = _second_chance_candidates(pool, stats, type)
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


def _no_executor(
    type: str,
    execution: bool,
    allow_paid_exec: bool,
    prefs: Prefs,
    blocked: dict[str, list[str]],
    tier_reason: str | None = None,
) -> str:
    """The reason when no tier has anyone.

    ``tier_reason`` is what the budget says about the cheap tier as a whole.
    A day ceiling that is 0 or reached adds ``run setup to allow cheap models``
    (whether or not a cheap executor existed); a reached job ceiling is said
    as it is, since ``setup`` does not change it.
    """
    if execution and not allow_paid_exec:
        text = (
            f"no executor available: no free executor left for {type}; split "
            f"the piece into smaller pieces or do it yourself (paid executors "
            f"run {type} only on --strong)"
        )
    else:
        text = "no executor available"
    if blocked:
        text += f"; {_blocked_note(blocked)}"
    if tier_reason is not None and "(cheap_budget_usd_job)" in tier_reason:
        if tier_reason not in blocked:
            text += f"; {tier_reason}"
    elif (
        prefs.cheap_budget_usd_day <= 0
        or (tier_reason or "").endswith("(cheap_budget_usd_day)")
    ):
        text += (
            "; run setup to allow cheap models (current budget US$ "
            f"{prefs.cheap_budget_usd_day:g})"
        )
    return text


def _family_failures(
    executor: Executor, job: dict | None, piece: int | None
) -> int:
    """Failures of ``executor``'s quota family on ``piece`` (see effort_ceiling)."""
    return (
        effort_ceiling(job, piece, quota_family(executor.id, executor.cost))
        - _DEFAULT_EFFORT_RANK
    )


def _effort_note(
    executor: Executor, job: dict | None, piece: int | None
) -> str:
    """``effort medium (low failed 1×)`` once the piece's family failed."""
    failures = _family_failures(executor, job, piece)
    if failures <= 0:
        return ""
    if wants_effort(executor.tool):
        level = next_effort(executor.tool, failures)
    else:
        match = _EFFORT_SUFFIX.search(executor.id)
        level = match.group(1) if match else None
    before = _EFFORT_STEPS[min(failures - 1, len(_EFFORT_STEPS) - 1)]
    if level is None or level == before:
        # Same level as the failed attempt: the effort did not rise.
        return ""
    return f"effort {level} ({before} failed {failures}×)"


def picked_effort(
    executor: Executor, job: dict | None, piece: int | None
) -> str | None:
    """The effort flag value for the next attempt of ``executor`` on ``piece``.

    ``None`` for a tool without an effort flag; otherwise :func:`next_effort`
    with the failures its quota family has on the piece.
    """
    return next_effort(executor.tool, _family_failures(executor, job, piece))


#: Trailing effort suffix of a model id, dropped by :func:`quota_family`.
_EFFORT_SUFFIX = re.compile(r"-(minimal|low|medium|high|xhigh|max)$")


#: Prefix of a subscription quota family, see :func:`quota_family`.
_SUB_PREFIX = "sub:"

#: The OpenCode route prefixes that reach the same model under one quota:
#: the canonical route and its alias, longest first.
_OPENCODE_ROUTES = ("opencode-go/", "opencode/")

#: The canonical OpenCode route: every route of the list ends up on it.
_OPENCODE_CANONICAL_ROUTE = "opencode/"

#: Effort suffix to rank; a model without suffix counts as ``low``.
_EFFORT_RANKS = {
    "minimal": 0, "low": 1, "medium": 2, "high": 3, "xhigh": 4, "max": 5,
}
_DEFAULT_EFFORT_RANK = 1

#: Attempt statuses that are not a quality failure, see :func:`_failed_executors`.
_NOT_A_FAILURE = ("ok", "quota", "brief_refused")

#: Effort flag values by failures so far, see :func:`next_effort`.
_EFFORT_STEPS = ("low", "medium", "high")


def _canonical_route(tool: str, model: str) -> str:
    """The model id with its OpenCode route prefix canonicalized.

    The same model reached through the alias route (``opencode-go/``) or
    through the canonical route (``opencode/``) draws on one quota, so both
    end up on the canonical route. Every other route keeps its own.
    """
    if tool != "opencode":
        return model
    for route in _OPENCODE_ROUTES:
        if model.startswith(route):
            return _OPENCODE_CANONICAL_ROUTE + model[len(route):]
    return model


def quota_family(executor_id: str, cost: str = "") -> str:
    """The group of executors that share one quota with ``executor_id``.

    With ``cost == "included"`` (a model covered by a subscription) the group
    is ``sub:<tool>``: every model of that CLI draws on the one plan quota.
    Otherwise the model's trailing effort suffix (``-minimal`` ... ``-max``)
    is dropped, since every effort of a model draws on the same quota.
    Antigravity shares its quota across a whole vendor, so there the family
    is ``antigravity:<model up to the first '-'>`` (``antigravity:claude``,
    ``antigravity:gemini``); any other tool keeps ``<tool>:<model>``. The two
    OpenCode routes of one model share a family too (see
    :func:`_canonical_route`). An id that already is a ``sub:<tool>`` family
    is returned as it is.
    """
    if executor_id.startswith(_SUB_PREFIX):
        return executor_id
    tool, _, model = executor_id.partition(":")
    if cost == "included":
        return f"{_SUB_PREFIX}{tool}"
    model = _EFFORT_SUFFIX.sub("", model)
    if tool == "antigravity":
        model = model.split("-", 1)[0]
    model = _canonical_route(tool, model)
    return f"{tool}:{model}"


def effort_rank(executor_id: str) -> int:
    """0 for ``-minimal`` ... 5 for ``-max``; no suffix counts as 1 (``low``)."""
    match = _EFFORT_SUFFIX.search(executor_id)
    if match is None:
        return _DEFAULT_EFFORT_RANK
    return _EFFORT_RANKS[match.group(1)]


def _failed_executors(piece: dict) -> list[str]:
    """Executors behind each failed exec attempt and each correction.

    One entry per failure, so a repeated failure counts repeatedly. An exec
    attempt that is not ``ok`` is a failure, except ``quota`` and
    ``brief_refused`` (the work never ran, so more effort would not help); so
    is a correction, which is charged to the executor that drew it.
    """
    failed = [
        attempt["executor"]
        for attempt in piece.get("attempts") or []
        if isinstance(attempt, dict)
        and attempt.get("role") == "exec"
        and attempt.get("status") not in _NOT_A_FAILURE
        and isinstance(attempt.get("executor"), str)
        and attempt.get("executor")
    ]
    failed += [
        correction["executor"]
        for correction in piece.get("corrections") or []
        if isinstance(correction, dict)
        and isinstance(correction.get("executor"), str)
        and correction.get("executor")
    ]
    return failed


def effort_ceiling(job: dict | None, piece: int | None, family: str) -> int:
    """The highest :func:`effort_rank` allowed now for ``family`` on ``piece``.

    ``1 +`` the failures of that quota family on the piece (exec attempts
    that are not ``ok``, ``quota`` or ``brief_refused``, and corrections drawn
    by its executors). Without a job or piece there is no history, so the
    ceiling is 1. The ceiling caps a preference, not a rule: it is bypassed
    when only one variant remains or none is under the ceiling.
    """
    if not job or piece is None:
        return _DEFAULT_EFFORT_RANK
    try:
        target = _find_piece(job, piece)
    except ValueError:
        return _DEFAULT_EFFORT_RANK
    costs = {
        attempt["executor"]: str(attempt.get("cost") or "")
        for attempt in target.get("attempts") or []
        if isinstance(attempt, dict) and isinstance(attempt.get("executor"), str)
    }
    failures = sum(
        1
        for executor in _failed_executors(target)
        if quota_family(executor, costs.get(executor, "")) == family
    )
    return _DEFAULT_EFFORT_RANK + failures


def next_effort(tool: dict, failures: int) -> str | None:
    """The effort flag value for the next attempt; ``None`` without effort.

    Tools that take an effort flag (:func:`tools.wants_effort`) start at
    ``low`` and rise one level per failure up to ``high``.
    """
    if not wants_effort(tool):
        return None
    return _EFFORT_STEPS[min(max(failures, 0), len(_EFFORT_STEPS) - 1)]


def _lowest_effort(
    executors: list[Executor], job: dict | None, piece: int | None
) -> list[Executor]:
    """Keep, per model, only the effort variants that suit the piece now.

    Variants of one model (the same id once the effort suffix and the
    OpenCode route prefix are dropped) compete: the lowest
    :func:`effort_rank` not above the family's :func:`effort_ceiling` wins,
    passing over variants that already failed on the piece. When every
    variant is above the ceiling the lowest one stays, so a model that only
    exists at high effort is not lost.
    """
    failed: set[str] = set()
    if job and piece is not None:
        try:
            failed = set(_failed_executors(_find_piece(job, piece)))
        except ValueError:
            failed = set()
    groups: dict[str, list[Executor]] = {}
    for executor in executors:
        tool, _, model = executor.id.partition(":")
        model = _canonical_route(tool, _EFFORT_SUFFIX.sub("", model))
        key = f"{tool}:{model}:{executor.cost}"
        groups.setdefault(key, []).append(executor)

    kept: list[Executor] = []
    for variants in groups.values():
        if len(variants) == 1:
            kept.extend(variants)
            continue
        ceiling = effort_ceiling(
            job, piece, quota_family(variants[0].id, variants[0].cost)
        )
        allowed = [v for v in variants if effort_rank(v.id) <= ceiling]
        fresh = [v for v in allowed if v.id not in failed]
        pool = fresh or allowed or variants
        lowest = min(effort_rank(v.id) for v in pool)
        kept.extend(v for v in pool if effort_rank(v.id) == lowest)
    return kept


def quota_cooldown(
    project: Path, minutes: float, now: datetime | None = None
) -> set[str]:
    """Executors with an attempt that ended in ``quota`` in the last ``minutes``.

    Every job of the project counts, whatever the role of the attempt. An
    attempt ends at ``started + duration_s``; one without a parseable
    ``started`` cannot be aged and is ignored. ``minutes <= 0`` turns the
    cooldown off. Corrupt jobs are skipped. An attempt whose cost was
    ``included`` also adds its ``sub:<tool>`` family (see :func:`quota_family`),
    so :func:`pick` pauses every model of that subscription.
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
                    if attempt.get("cost") == "included":
                        # One subscription quota: pause every model of the CLI.
                        cooling.add(quota_family(executor, "included"))
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
    executor excluded, so the choice skips to the next tier; execution work
    never climbs because of failures, only when its tier is exhausted (see
    SKILL.md).
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


def _by_type(stats: dict[tuple[str, str], Stat]) -> list[tuple[str, list[Stat]]]:
    """Work types in :data:`WORK_TYPES` order, executors by score descending (unrated last)."""
    by_type: dict[str, list[Stat]] = {}
    for stat in stats.values():
        by_type.setdefault(stat.type, []).append(stat)
    # Fixed vocabulary first, then any stray type a hand-edited record uses.
    ordered_types = [*WORK_TYPES, *sorted(set(by_type) - set(WORK_TYPES))]
    return [
        (work_type, sorted(by_type[work_type], key=lambda stat: (
            float("inf") if stat.score is None else -stat.score, stat.executor)))
        for work_type in ordered_types if by_type.get(work_type)
    ]


def scoreboard_rows(project: Path, now: datetime | None = None) -> list[dict]:
    """The rows of :func:`render_scoreboard` as data, in the same order."""
    stats, _skipped = _collect(Path(project), DEFAULT_HALF_LIFE_DAYS, now)
    return [asdict(stat) for _type, entries in _by_type(stats) for stat in entries]


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
    grouped = _by_type(stats)

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
