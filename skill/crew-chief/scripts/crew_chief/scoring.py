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
from . import scoring_stats
from . import scoring_effort

#: Deductions applied by :func:`grade`, in score points.
PENALTIES = {
    "destructive": 40,
    "outside_write": 60,
    "false_done": 40,
    "model_mismatch": 30,
}

#: Default half-life, in days, of a stored grade's weight in :func:`build_stats`.
DEFAULT_HALF_LIFE_DAYS = scoring_stats.DEFAULT_HALF_LIFE_DAYS

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


Stat = scoring_stats.Stat
_parse_iso = scoring_stats._parse_iso
_collect = scoring_stats._collect
_is_number = scoring_stats._is_number
_mean = scoring_stats._mean
build_stats = scoring_stats.build_stats


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
            count = max(picks.get(executor, 0), attempts.get(executor, 0))
            by_executor[executor] = by_executor.get(executor, 0) + count
            if executor.startswith("native:"):
                rest = executor[len("native:") :]
                if "/" in rest:
                    cli, model = rest.split("/", 1)
                    by_executor[f"{cli}:{model}"] = (
                        by_executor.get(f"{cli}:{model}", 0) + count
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
    executor_mode: str = "free_first",
    review_for_executor: str | None = None,
    allowlist: set[str] | None = None,
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

    ``executor_mode`` controls which executors are eligible:

    * ``"free_first"`` (default): the existing ladder unchanged.
    * ``"own"``: only the coordinator's own CLI and its included family are
      eligible; free executors are excluded. If no own-family executor is
      available, ``pick`` fails with ``"no executor available in own CLI
      family"`` instead of falling back to free executors.
    * ``"subagents"``: ``pick`` answers ``native:<cli>/<model>`` for the
      coordinator's CLI and model instead of selecting a CLI to spawn.

    ``--allow-own-cli`` is accepted alongside ``--executors``: in
    ``free_first`` it continues to permit the coordinator's own CLI after
    free/included tiers as today; in ``own`` it is redundant (own CLI
    already first) and accepted without error; it never changes the
    resolved executor mode.

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

    # Normalize exclude set so native:cli/model and cli:model match each other.
    if exclude:
        exclude = set(exclude)
        for ex_id in list(exclude):
            if ex_id.startswith("native:"):
                rest = ex_id[len("native:") :]
                if "/" in rest:
                    cli, model = rest.split("/", 1)
                    exclude.add(f"{cli}:{model}")
            elif ":" in ex_id:
                cli, model = ex_id.split(":", 1)
                exclude.add(f"native:{cli}/{model}")

    # --- T040: restrict candidates to project team allowlist (US10, FR-037) --
    if allowlist is not None:
        allowed = set(allowlist)
        for entry in list(allowed):
            if entry.startswith("native:"):
                rest = entry[len("native:") :]
                if "/" in rest:
                    cli, model = rest.split("/", 1)
                    allowed.add(f"{cli}:{model}")
            elif ":" in entry:
                cli, model = entry.split(":", 1)
                allowed.add(f"native:{cli}/{model}")

        def _is_allowed(executor: Executor) -> bool:
            if executor.id in allowed:
                return True
            tool_name = executor.tool.get("name") if executor.tool else None
            if tool_name and executor.model:
                if f"{tool_name}:{executor.model}" in allowed:
                    return True
            return False

        filtered = [e for e in candidates if _is_allowed(e)]
        if not filtered:
            excluded_ids = sorted({e.id for e in candidates})
            role_label = (
                "reviewer"
                if (type == "review" or review_for_executor is not None)
                else "executor"
            )
            reason = (
                f"no {role_label} available: all candidates are outside the allowlist "
                f"({', '.join(sorted(allowlist))}); saw {', '.join(excluded_ids)}"
            )
            return None, reason
        candidates = filtered

    # --- FR-042: review selection under own and subagents modes ------------
    is_review = type == "review" or review_for_executor is not None
    if executor_mode in ("own", "subagents") and is_review:
        exec_model = (
            _model_of(review_for_executor)
            if review_for_executor is not None
            else None
        )

        def _shares_exec_model(cand_model: str) -> bool:
            if exec_model is None:
                return False
            if cand_model == exec_model:
                return True
            return (
                _EFFORT_SUFFIX.sub("", cand_model)
                == _EFFORT_SUFFIX.sub("", exec_model)
            )

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
        pool = [e for e in candidates if e.id not in skipped]

        # Candidates are considered in order:
        # (1) the coordinator's own CLI with a different model
        # (2) included executors
        # (3) free executors
        tier1 = [
            e
            for e in pool
            if coordinator_tool
            and _is_own_cli(e, coordinator_tool)
            and not _shares_exec_model(e.model)
        ]
        tier2 = [
            e
            for e in pool
            if not (coordinator_tool and _is_own_cli(e, coordinator_tool))
            and e.cost == "included"
            and not _shares_exec_model(e.model)
        ]
        tier3 = [
            e
            for e in pool
            if not (coordinator_tool and _is_own_cli(e, coordinator_tool))
            and e.cost == "free"
            and not _shares_exec_model(e.model)
        ]
        tier4 = [
            e
            for e in pool
            if not (coordinator_tool and _is_own_cli(e, coordinator_tool))
            and e.cost in ("cheap", "paid")
            and not _shares_exec_model(e.model)
        ]

        ordered_tiers = [
            ("own", tier1),
            ("included", tier2),
            ("free", tier3),
        ]
        if strong:
            ordered_tiers.append(("escalated", tier4))

        for tier_name, tier_pool in ordered_tiers:
            if not tier_pool:
                continue
            choice, reason = _pick(
                tier_pool,
                stats,
                type,
                risk,
                prefs,
                exclude,
                rng,
                job_usage(job),
                allow_paid_exec=True,
                job=job,
                piece=piece,
                budget=budget,
                coordinator_tool=coordinator_tool,
                allow_own_cli=True,
                known_tiers=frozenset(executor.cost for executor in candidates),
            )
            if choice is not None:
                # A review is always a dispatchable executor, never native:
                # ``dispatch --review`` runs it, in every mode (FR-043).
                if unfit:
                    reason += f"; not for {type}: {', '.join(unfit)}"
                if skipped:
                    reason += f"; skipped for quota: {', '.join(skipped)}"
                return choice, reason

        # If no candidate is available, the review is not dispatched and the
        # reason is reported (never the executor's own model).
        if exec_model is not None:
            reason = (
                f"no reviewer available: all candidates share the executor's model ({exec_model})"
                if candidates
                and all(_shares_exec_model(e.model) for e in candidates)
                else f"no reviewer available: cannot use the executor's model ({exec_model})"
            )
        else:
            reason = "no reviewer available"

        if unfit:
            reason += f"; not for {type}: {', '.join(unfit)}"
        if skipped:
            reason += f"; skipped for quota: {', '.join(skipped)}"
        return None, reason

    # --- executor mode: own and subagents restrict to coordinator's CLI family
    if executor_mode in ("own", "subagents"):
        if not coordinator_tool:
            return None, "no executor available in own CLI family"
        own_pool = [e for e in candidates if _is_own_cli(e, coordinator_tool)]
        if not own_pool:
            return None, "no executor available in own CLI family"
        candidates = own_pool

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
    if executor_mode in ("own", "subagents") and choice is None:
        return None, "no executor available in own CLI family"
    if choice is not None and executor_mode == "subagents":
        native_id = f"native:{coordinator_tool}/{choice.model}"
        choice = Executor(
            id=native_id,
            tool=choice.tool,
            model=choice.model,
            cost=choice.cost,
            trains=choice.trains,
        )
        reason = f"subagents mode: native {coordinator_tool} ({choice.model})"
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


def _model_of(executor_id: str) -> str:
    """Extract model name from an executor id (including native:<cli>/<model>)."""
    if executor_id.startswith("native:"):
        rest = executor_id[len("native:"):]
        if "/" in rest:
            return rest.split("/", 1)[1]
        if ":" in rest:
            return rest.split(":", 1)[1]
        return rest
    if ":" in executor_id:
        return executor_id.split(":", 1)[1]
    return executor_id


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


_EFFORT_SUFFIX = scoring_effort._EFFORT_SUFFIX
_SUB_PREFIX = scoring_effort._SUB_PREFIX
_OPENCODE_ROUTES = scoring_effort._OPENCODE_ROUTES
_OPENCODE_CANONICAL_ROUTE = scoring_effort._OPENCODE_CANONICAL_ROUTE
_EFFORT_RANKS = scoring_effort._EFFORT_RANKS
_DEFAULT_EFFORT_RANK = scoring_effort._DEFAULT_EFFORT_RANK
_NOT_A_FAILURE = scoring_effort._NOT_A_FAILURE
_EFFORT_STEPS = scoring_effort._EFFORT_STEPS

_canonical_route = scoring_effort._canonical_route
quota_family = scoring_effort.quota_family
effort_rank = scoring_effort.effort_rank
_failed_executors = scoring_effort._failed_executors
effort_ceiling = scoring_effort.effort_ceiling
next_effort = scoring_effort.next_effort
_lowest_effort = scoring_effort._lowest_effort
quota_cooldown = scoring_effort.quota_cooldown
_find_piece = scoring_effort._find_piece
free_failures = scoring_effort.free_failures


_duration_cell = scoring_stats._duration_cell
_score_cell = scoring_stats._score_cell
_tokens_cell = scoring_stats._tokens_cell
_by_type = scoring_stats._by_type
scoreboard_rows = scoring_stats.scoreboard_rows
render_scoreboard = scoring_stats.render_scoreboard
