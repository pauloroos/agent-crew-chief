"""Quota families, cooldown, effort ladder and failures.

Quota and effort policy for executor selection, including subscription
grouping, OpenCode canonical route normalization, effort ranking and
cooldown tracking.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .tools import Executor

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
    from . import scoring

    if tool != "opencode":
        return model
    for route in scoring._OPENCODE_ROUTES:
        if model.startswith(route):
            return scoring._OPENCODE_CANONICAL_ROUTE + model[len(route):]
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
    from . import scoring

    if executor_id.startswith(scoring._SUB_PREFIX):
        return executor_id
    tool, _, model = executor_id.partition(":")
    if cost == "included":
        return f"{scoring._SUB_PREFIX}{tool}"
    model = scoring._EFFORT_SUFFIX.sub("", model)
    if tool == "antigravity":
        model = model.split("-", 1)[0]
    model = scoring._canonical_route(tool, model)
    return f"{tool}:{model}"


def effort_rank(executor_id: str) -> int:
    """0 for ``-minimal`` ... 5 for ``-max``; no suffix counts as 1 (``low``)."""
    from . import scoring

    match = scoring._EFFORT_SUFFIX.search(executor_id)
    if match is None:
        return scoring._DEFAULT_EFFORT_RANK
    return scoring._EFFORT_RANKS[match.group(1)]


def _failed_executors(piece: dict) -> list[str]:
    """Executors behind each failed exec attempt and each correction.

    One entry per failure, so a repeated failure counts repeatedly. An exec
    attempt that is not ``ok`` is a failure, except ``quota`` and
    ``brief_refused`` (the work never ran, so more effort would not help); so
    is a correction, which is charged to the executor that drew it.
    """
    from . import scoring

    failed = [
        attempt["executor"]
        for attempt in piece.get("attempts") or []
        if isinstance(attempt, dict)
        and attempt.get("role") == "exec"
        and attempt.get("status") not in scoring._NOT_A_FAILURE
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
    from . import scoring

    if not job or piece is None:
        return scoring._DEFAULT_EFFORT_RANK
    try:
        target = scoring._find_piece(job, piece)
    except ValueError:
        return scoring._DEFAULT_EFFORT_RANK
    costs = {
        attempt["executor"]: str(attempt.get("cost") or "")
        for attempt in target.get("attempts") or []
        if isinstance(attempt, dict) and isinstance(attempt.get("executor"), str)
    }
    failures = sum(
        1
        for executor in scoring._failed_executors(target)
        if scoring.quota_family(executor, costs.get(executor, "")) == family
    )
    return scoring._DEFAULT_EFFORT_RANK + failures


def next_effort(tool: dict, failures: int) -> str | None:
    """The effort flag value for the next attempt; ``None`` without effort.

    Tools that take an effort flag (:func:`tools.wants_effort`) start at
    ``low`` and rise one level per failure up to ``high``.
    """
    from . import scoring

    if not scoring.wants_effort(tool):
        return None
    return scoring._EFFORT_STEPS[min(max(failures, 0), len(scoring._EFFORT_STEPS) - 1)]


def model_effort(tool: dict, model: str) -> str | None:
    """The effort an Antigravity model id carries (``gemini-3.8-flash-medium``).

    Antigravity lists one model id per effort and rejects an ``--effort`` that
    differs from it; ``None`` for other tools and for an id without suffix.
    """
    if tool.get("kind") != "antigravity":
        return None
    match = _EFFORT_SUFFIX.search(model)
    return match.group(1) if match else None


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
    from . import scoring

    failed: set[str] = set()
    if job and piece is not None:
        try:
            failed = set(scoring._failed_executors(scoring._find_piece(job, piece)))
        except ValueError:
            failed = set()
    groups: dict[str, list[Executor]] = {}
    for executor in executors:
        tool, _, model = executor.id.partition(":")
        model = scoring._canonical_route(tool, scoring._EFFORT_SUFFIX.sub("", model))
        key = f"{tool}:{model}:{executor.cost}"
        groups.setdefault(key, []).append(executor)

    kept: list[Executor] = []
    for variants in groups.values():
        if len(variants) == 1:
            kept.extend(variants)
            continue
        ceiling = scoring.effort_ceiling(
            job, piece, scoring.quota_family(variants[0].id, variants[0].cost)
        )
        allowed = [v for v in variants if scoring.effort_rank(v.id) <= ceiling]
        fresh = [v for v in allowed if v.id not in failed]
        pool = fresh or allowed or variants
        lowest = min(scoring.effort_rank(v.id) for v in pool)
        kept.extend(v for v in pool if scoring.effort_rank(v.id) == lowest)
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
    from . import scoring

    if minutes <= 0:
        return set()
    dt = getattr(scoring, "datetime", datetime)
    tz = getattr(scoring, "timezone", timezone)
    td = getattr(scoring, "timedelta", timedelta)
    path_cls = getattr(scoring, "Path", Path)

    now = now or dt.now(tz.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=tz.utc)
    window = td(minutes=minutes)

    cooling: set[str] = set()
    for job, _path, _error in scoring.iter_jobs(path_cls(project)):
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
                ended = scoring._parse_iso(started)
                if ended is None:
                    continue
                duration = attempt.get("duration_s")
                if isinstance(duration, (int, float)) and not isinstance(
                    duration, bool
                ):
                    ended += td(seconds=duration)
                if now - ended <= window:
                    cooling.add(executor)
                    if attempt.get("cost") == "included":
                        # One subscription quota: pause every model of the CLI.
                        cooling.add(scoring.quota_family(executor, "included"))
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
    from . import scoring

    target = scoring._find_piece(job, piece)
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


from . import scoring  # facade for monkeypatchable dependencies
