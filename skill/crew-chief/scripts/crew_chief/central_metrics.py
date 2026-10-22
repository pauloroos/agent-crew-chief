"""Aggregate raw economy counts with coverage labels (FR-013, FR-014, FR-025, SC-006).

This module turns a list of job records (and usage that is still pending in
round metadata) into one honest aggregate.  It sums raw counts instead of
rounded percentages, keeps estimates and missing values visible, and never
invents a monetary saving.

The public entry point is :func:`aggregate`.
"""

from __future__ import annotations

import math
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from . import economy, records

__all__ = ["aggregate"]

_NOT_ATTEMPTS = economy._NOT_ATTEMPTS
_TIERS = economy._TIERS


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _int_or_none(value: Any) -> int | None:
    if not _number(value):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    return int(value)


def _pref(prefs: Any, name: str, default: Any = None) -> Any:
    if prefs is None:
        return default
    if isinstance(prefs, dict):
        return prefs.get(name, default)
    return getattr(prefs, name, default)


def _parse_iso(text: Any, local_tz: Any) -> datetime | None:
    return economy._parse_iso(text, local_tz)


def _pieces(job: dict) -> list[dict]:
    return [p for p in job.get("pieces") or [] if isinstance(p, dict)]


def _attempts(piece: dict) -> list[dict]:
    return [a for a in piece.get("attempts") or [] if isinstance(a, dict)]


def _worktree(job: dict) -> str:
    for key in (
        "worktree",
        "worktree_path",
        "repo_worktree",
        "source_worktree",
        "worktree_dir",
        "_worktree",
        "repo",
        "repository",
        "root",
    ):
        value = job.get(key)
        if value is not None and str(value).strip() != "":
            try:
                return str(Path(str(value)))
            except (OSError, ValueError):
                return str(value).strip()
    return ""


def _dedup_jobs(jobs: Iterable[Any]) -> list[dict]:
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for job in jobs or []:
        if not isinstance(job, dict):
            continue
        wt = _worktree(job)
        jid = str(job.get("id")) if job.get("id") is not None else ""
        if wt:
            key = (wt, jid)
            if key in seen:
                continue
            seen.add(key)
        out.append(job)
    return out


def _attempt_stable_id(attempt: dict, job: dict, piece: dict) -> str | None:
    for key in (
        "id",
        "attempt_id",
        "attempt_uuid",
        "central_event_id",
        "event_id",
        "stable_id",
        "uuid",
        "key",
    ):
        value = attempt.get(key)
        if value is not None and str(value).strip() != "":
            return str(value).strip()
    return None


def _counted_attempt(attempt: dict) -> bool:
    return (
        attempt.get("role") == "exec"
        and attempt.get("status") not in _NOT_ATTEMPTS
    )


def _tier_tokens_empty() -> dict[str, int]:
    return {tier: 0 for tier in _TIERS}


def _safe_total_tokens(attempt: dict) -> int | None:
    value = attempt.get("tokens_total")
    if _number(value):
        return int(value)
    ti = attempt.get("tokens_in")
    to = attempt.get("tokens_out")
    if _number(ti) and _number(to):
        return int(ti) + int(to)
    return None


def aggregate(jobs: list[dict], pending_usage: list[dict], prefs: object, now: object) -> dict:
    """Aggregate raw counts across ``jobs`` and ``pending_usage``.

    ``now`` may be a datetime or an ISO string.  ``prefs`` may be a
    :class:`Prefs` instance or a mapping.
    """
    if hasattr(now, "now") and callable(getattr(now, "now")):
        try:
            now = now.now()
        except Exception:
            pass
    if isinstance(now, str):
        try:
            now_dt = datetime.fromisoformat(now.replace("Z", "+00:00"))
        except ValueError:
            now_dt = datetime.now(timezone.utc)
    elif isinstance(now, datetime):
        now_dt = now
    elif isinstance(now, (int, float)) and not isinstance(now, bool):
        try:
            now_dt = datetime.fromtimestamp(float(now), tz=timezone.utc)
        except (OSError, ValueError, OverflowError):
            now_dt = datetime.now(timezone.utc)
    else:
        now_dt = datetime.now(timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    local_tz = now_dt.astimezone().tzinfo

    unique_jobs = _dedup_jobs(jobs)

    # --- accumulators -------------------------------------------------------
    tier_tokens = _tier_tokens_empty()
    cheap_usd = 0.0
    paid_usd = 0.0
    cost_unknown = False

    exec_tokens_in = 0
    exec_tokens_out = 0
    exec_tokens_total = 0
    exec_has_in = False
    exec_has_out = False
    exec_has_total = False

    coordinator_tokens = 0  # coordinator role only
    central_tokens = 0  # central role only
    usage_tokens_in_coord = 0
    usage_tokens_out_coord = 0
    usage_tokens_in_central = 0
    usage_tokens_out_central = 0
    coord_has_in = False
    coord_has_out = False
    central_has_in = False
    central_has_out = False

    pieces_total = 0
    accepted_pieces = 0
    accepted_free_pieces = 0
    attempted_pieces = 0
    attempts_counted = 0
    all_attempts = 0

    scores: list[float] = []

    # wall / execution durations
    wall_starts: list[datetime] = []
    wall_ends: list[datetime] = []
    execution_seconds = 0.0

    # model grouping
    models: "OrderedDict[tuple, dict]" = OrderedDict()
    seen_attempt_ids: set[str] = set()

    recorded_event_ids: set[str] = set()

    missing_usage_jobs = 0
    jobs_with_usage = 0

    for job in unique_jobs:
        if not isinstance(job, dict):
            continue
        job_usage = [u for u in job.get("usage") or [] if isinstance(u, dict)]
        recorded = [u for u in job_usage if _number(u.get("tokens_total"))]
        if recorded:
            jobs_with_usage += 1
        else:
            missing_usage_jobs += 1
        for piece in _pieces(job):
            pieces_total += 1
            attempts = _attempts(piece)
            counted = [a for a in attempts if _counted_attempt(a)]
            if counted:
                attempted_pieces += 1
                attempts_counted += len(counted)

            # tier tokens + cost (all attempts, including review/retry)
            for attempt in attempts:
                aid = _attempt_stable_id(attempt, job, piece)
                if aid is not None and aid in seen_attempt_ids:
                    continue  # shared imported history: count once
                if aid is not None:
                    seen_attempt_ids.add(aid)
                all_attempts += 1
                tier = attempt.get("cost")
                if tier in tier_tokens:
                    tokens = attempt.get("tokens_total")
                    if _number(tokens):
                        tier_tokens[tier] += int(tokens)
                if tier in ("cheap", "paid"):
                    if attempt.get("cost_known") is False:
                        cost_unknown = True
                    usd = attempt.get("cost_usd")
                    if _number(usd) and math.isfinite(usd) and usd >= 0:
                        if tier == "cheap":
                            cheap_usd += float(usd)
                        else:
                            paid_usd += float(usd)

                model = attempt.get("reported_model")
                if model is None:
                    model = attempt.get("model")
                role_var = attempt.get("role")
                if role_var not in ("exec", "review"):
                    role_var = "exec"
                # all attempts (including review/retry) are grouped by role/model/tier
                key = (role_var, model, tier if tier in _TIERS else None)
                grp = models.setdefault(
                    key,
                    {
                        "role": role_var,
                        "model": model if isinstance(model, str) else None,
                        "tier": tier if tier in _TIERS else None,
                        "tokens_in": None,
                        "tokens_out": None,
                        "tokens_total": 0,
                        "estimated": False,
                        "_has_in": False,
                        "_has_out": False,
                        "_has_total": False,
                        "_missing_total": False,
                        "attempts": 0,
                    },
                )
                grp["attempts"] += 1
                raw_total = attempt.get("tokens_total")
                tt = int(raw_total) if _number(raw_total) else None
                if tt is not None:
                    grp["tokens_total"] += tt
                    grp["_has_total"] = True
                else:
                    grp["_missing_total"] = True
                ti = attempt.get("tokens_in")
                to = attempt.get("tokens_out")
                if _number(ti):
                    grp["tokens_in"] = (grp["tokens_in"] or 0) + int(ti)
                    grp["_has_in"] = True
                if _number(to):
                    grp["tokens_out"] = (grp["tokens_out"] or 0) + int(to)
                    grp["_has_out"] = True
                if attempt.get("tokens_estimated") is True:
                    grp["estimated"] = True
                # exec top-level (all attempt tokens)
                if tt is not None:
                    exec_tokens_total += tt
                    exec_has_total = True
                if _number(ti):
                    exec_tokens_in += int(ti)
                    exec_has_in = True
                if _number(to):
                    exec_tokens_out += int(to)
                    exec_has_out = True

                # durations for all attempts
                started = _parse_iso(attempt.get("started"), local_tz)
                if started is not None:
                    duration = attempt.get("duration_s")
                    dur = float(duration) if _number(duration) else 0.0
                    end = started + timedelta(seconds=dur)
                    wall_starts.append(started)
                    wall_ends.append(end)
                    execution_seconds += dur

            # free piece share
            if piece.get("accepted"):
                accepted_pieces += 1
                last_ok = None
                for a in reversed(attempts):
                    if a.get("role") == "exec" and a.get("status") == "ok":
                        last_ok = a
                        break
                if last_ok is not None and last_ok.get("cost") == "free":
                    accepted_free_pieces += 1

            # grades
            for grade in records.piece_grades(piece).values():
                score = grade.get("score")
                if _number(score):
                    scores.append(float(score))

        # job usage entries
        usage = [u for u in job_usage if isinstance(u, dict)]
        deduped_usage: list[dict] = []
        for u in usage:
            eid = u.get("central_event_id")
            if not isinstance(eid, str):
                eid = u.get("event_id")
            if isinstance(eid, str) and eid.strip():
                if eid.strip() in recorded_event_ids:
                    continue
                recorded_event_ids.add(eid.strip())
            deduped_usage.append(u)
        usage = deduped_usage
        recorded_usage = [u for u in usage if _number(u.get("tokens_total"))]
        if recorded_usage:
            jobs_with_usage += 0  # already counted above
        if not recorded_usage:
            missing_usage_jobs += 0

        for u in usage:
            role = u.get("role")
            if role not in ("coordinator", "central"):
                # default unknown role to coordinator? keep as is but bucket coordinator
                role = "coordinator" if role is None else role
            eid = u.get("central_event_id")
            if isinstance(eid, str) and eid.strip():
                if eid.strip() in recorded_event_ids:
                    pass  # already counted in set; still count this entry once
            tokens_total = u.get("tokens_total")
            tt = int(tokens_total) if _number(tokens_total) else None
            ti = u.get("tokens_in")
            to = u.get("tokens_out")
            estimated = u.get("estimated") is True
            model = u.get("model")
            if role == "coordinator":
                if tt is not None:
                    coordinator_tokens += tt
                if _number(ti):
                    usage_tokens_in_coord += int(ti)
                    coord_has_in = True
                if _number(to):
                    usage_tokens_out_coord += int(to)
                    coord_has_out = True
            elif role == "central":
                if tt is not None:
                    central_tokens += tt
                if _number(ti):
                    usage_tokens_in_central += int(ti)
                    central_has_in = True
                if _number(to):
                    usage_tokens_out_central += int(to)
                    central_has_out = True
            # model grouping for usage
            key = (role if role in ("coordinator", "central") else "coordinator", model, None)
            grp = models.setdefault(
                key,
                {
                    "role": key[0],
                    "model": model if isinstance(model, str) else None,
                    "tier": None,
                    "tokens_in": None,
                    "tokens_out": None,
                    "tokens_total": 0,
                    "estimated": False,
                    "_has_in": False,
                    "_has_out": False,
                    "_has_total": False,
                    "_missing_total": False,
                    "attempts": 0,
                },
            )
            if tt is not None:
                grp["tokens_total"] += tt
                grp["_has_total"] = True
            else:
                grp["_missing_total"] = True
            if _number(ti):
                grp["tokens_in"] = (grp["tokens_in"] or 0) + int(ti)
                grp["_has_in"] = True
            if _number(to):
                grp["tokens_out"] = (grp["tokens_out"] or 0) + int(to)
                grp["_has_out"] = True
            if estimated:
                grp["estimated"] = True

        # active job wall time
        active = False
        if job.get("active") is True:
            active = True
        if job.get("state") in ("running", "active", "started", "in_progress"):
            active = True
        if job.get("status") in ("running", "active", "in_progress"):
            active = True
        if active:
            created = _parse_iso(job.get("created"), local_tz)
            if created is not None:
                wall_starts.append(created)
                wall_ends.append(now_dt)
            # execution time for active? leave as attempts

    # --- pending usage ------------------------------------------------------
    # dedup pending by event_id and against recorded events
    seen_pending: set[str] = set()
    pending_list: list[dict] = []
    for pu in pending_usage or []:
        if not isinstance(pu, dict):
            continue
        eid = pu.get("event_id")
        if not isinstance(eid, str):
            eid = pu.get("central_event_id")
        eid_norm = eid.strip() if isinstance(eid, str) else None
        if eid_norm is not None and eid_norm in recorded_event_ids:
            continue
        if eid_norm is not None:
            if eid_norm in seen_pending:
                continue
            seen_pending.add(eid_norm)
        pending_list.append(pu)

    for pu in pending_list:
        role = pu.get("role")
        if role not in ("coordinator", "central"):
            role = "coordinator" if role is None else str(role)
            if role not in ("coordinator", "central"):
                role = "coordinator"
        eid = pu.get("event_id")
        if not isinstance(eid, str):
            eid = pu.get("central_event_id")
        tokens_total = pu.get("tokens_total")
        tt = int(tokens_total) if _number(tokens_total) else None
        ti = pu.get("tokens_in")
        to = pu.get("tokens_out")
        estimated = pu.get("estimated") is True
        model = pu.get("model")
        if role == "coordinator":
            if tt is not None:
                coordinator_tokens += tt
            if _number(ti):
                usage_tokens_in_coord += int(ti)
                coord_has_in = True
            if _number(to):
                usage_tokens_out_coord += int(to)
                coord_has_out = True
        elif role == "central":
            if tt is not None:
                central_tokens += tt
            if _number(ti):
                usage_tokens_in_central += int(ti)
                central_has_in = True
            if _number(to):
                usage_tokens_out_central += int(to)
                central_has_out = True
        key = (role, model, None)
        grp = models.setdefault(
            key,
            {
                "role": role,
                "model": model if isinstance(model, str) else None,
                "tier": None,
                "tokens_in": None,
                "tokens_out": None,
                "tokens_total": 0,
                "estimated": False,
                "_has_in": False,
                "_has_out": False,
                "_has_total": False,
                "_missing_total": False,
                "attempts": 0,
            },
        )
        if tt is not None:
            grp["tokens_total"] += tt
            grp["_has_total"] = True
        else:
            grp["_missing_total"] = True
        if _number(ti):
            grp["tokens_in"] = (grp["tokens_in"] or 0) + int(ti)
            grp["_has_in"] = True
        if _number(to):
            grp["tokens_out"] = (grp["tokens_out"] or 0) + int(to)
            grp["_has_out"] = True
        if estimated:
            grp["estimated"] = True

    # --- finalize model entries --------------------------------------------
    model_list = []
    for grp in models.values():
        # remove internal flags
        has_in = grp.pop("_has_in", False)
        has_out = grp.pop("_has_out", False)
        has_total = grp.pop("_has_total", False)
        missing_total = grp.pop("_missing_total", False)
        if not has_in:
            grp["tokens_in"] = None
        if not has_out:
            grp["tokens_out"] = None
        if not has_total:
            grp["tokens_total"] = None
        # coverage label
        if grp["tokens_total"] is None and grp["tokens_in"] is None and grp["tokens_out"] is None:
            coverage = "unknown"
        elif missing_total:
            coverage = "incomplete"
        elif grp["tokens_total"] is None:
            coverage = "incomplete"
        elif grp["estimated"]:
            coverage = "estimated"
        else:
            coverage = "measured"
        grp["coverage"] = coverage
        # attempts for usage groups is 0; keep
        if grp.get("attempts", 0) == 0 and grp.get("role") != "exec":
            grp.pop("attempts", None)
        model_list.append(grp)

    # --- wall duration ------------------------------------------------------
    if wall_starts and wall_ends:
        wall_seconds = max(0.0, (max(wall_ends) - min(wall_starts)).total_seconds())
    else:
        wall_seconds = None

    # --- ratios -------------------------------------------------------------
    free_tokens = tier_tokens["free"]
    paid_exec_tokens = tier_tokens["paid"]
    usage_tokens = coordinator_tokens + central_tokens
    exec_tokens = sum(tier_tokens.values())
    total_tokens = exec_tokens + usage_tokens

    free_piece_share = (accepted_free_pieces / accepted_pieces) if accepted_pieces else None
    free_token_share = (free_tokens / total_tokens) if total_tokens else None
    coordinator_share = (usage_tokens / total_tokens) if total_tokens else None
    central_share = (central_tokens / total_tokens) if total_tokens else None
    attempts_per_piece = (attempts_counted / attempted_pieces) if attempted_pieces else None
    mean_score = (sum(scores) / len(scores)) if scores else None

    # exec tokens_in/out totals
    exec_tokens_in_val = exec_tokens_in if exec_has_in else None
    exec_tokens_out_val = exec_tokens_out if exec_has_out else None
    exec_tokens_total_val = exec_tokens_total if exec_has_total else None
    coord_tokens_in_val = (usage_tokens_in_coord + usage_tokens_in_central) if (coord_has_in or central_has_in) else None
    coord_tokens_out_val = (usage_tokens_out_coord + usage_tokens_out_central) if (coord_has_out or central_has_out) else None
    # but separate:
    coord_in_val = usage_tokens_in_coord if coord_has_in else None
    coord_out_val = usage_tokens_out_coord if coord_has_out else None
    central_in_val = usage_tokens_in_central if central_has_in else None
    central_out_val = usage_tokens_out_central if central_has_out else None

    # coverage label overall
    if any(m.get("coverage") in ("unknown", "incomplete") for m in model_list):
        coverage_label = "incomplete"
    elif any(m.get("coverage") == "estimated" for m in model_list):
        coverage_label = "estimated"
    elif model_list:
        coverage_label = "measured"
    else:
        coverage_label = "unknown"

    estimated_any = any(m.get("estimated") for m in model_list)

    # --- alerts -------------------------------------------------------------
    alerts: list[str] = []
    limit = _pref(prefs, "alert_coordinator_share", None)
    if limit and coordinator_share is not None and coordinator_share > limit:
        alerts.append(
            f"coordinator spent {coordinator_share:.0%} of the tokens "
            f"(limit {limit:.0%})"
        )
    limit = _pref(prefs, "alert_attempts_per_piece", None)
    if limit and attempts_per_piece is not None and attempts_per_piece > limit:
        alerts.append(
            f"{attempts_per_piece:.1f} attempts per piece (limit {limit:.1f})"
        )
    limit = _pref(prefs, "alert_job_minutes", None)
    if limit and wall_seconds is not None and (wall_seconds / 60.0) > limit:
        alerts.append(f"job took {wall_seconds / 60.0:.0f} min (limit {limit:.0f})")
    budget = _pref(prefs, "cheap_budget_usd_day", 0)
    if isinstance(budget, bool):
        budget = 0
    try:
        budget = float(budget or 0)
    except (TypeError, ValueError):
        budget = 0.0
    if budget > 0 and cheap_usd >= 0.8 * budget:
        pct = cheap_usd / budget
        alerts.append(
            f"cheap spend at {pct:.0%} of the daily budget "
            f"(US$ {cheap_usd:.2f} of {budget:.2f})"
        )

    # --- tokens by role / tier ---------------------------------------------
    tokens_by_tier = dict(tier_tokens)
    tokens_by_role = {
        "exec": exec_tokens,
        "coordinator": coordinator_tokens,
        "central": central_tokens,
        "usage": usage_tokens,
        "total": total_tokens,
    }

    metrics = {
        "free_piece_share": {
            "numerator": accepted_free_pieces,
            "denominator": accepted_pieces,
            "value": free_piece_share,
            "num": accepted_free_pieces,
            "den": accepted_pieces,
            "ratio": free_piece_share,
        },
        "free_token_share": {
            "numerator": free_tokens,
            "denominator": total_tokens,
            "value": free_token_share,
            "num": free_tokens,
            "den": total_tokens,
            "ratio": free_token_share,
        },
        "coordinator_share": {
            "numerator": usage_tokens,
            "denominator": total_tokens,
            "value": coordinator_share,
            "num": usage_tokens,
            "den": total_tokens,
            "ratio": coordinator_share,
        },
        "central_share": {
            "numerator": central_tokens,
            "denominator": total_tokens,
            "value": central_share,
            "num": central_tokens,
            "den": total_tokens,
            "ratio": central_share,
        },
        "attempts_per_piece": {
            "numerator": attempts_counted,
            "denominator": attempted_pieces,
            "value": attempts_per_piece,
            "num": attempts_counted,
            "den": attempted_pieces,
            "ratio": attempts_per_piece,
        },
        "mean_score": {
            "numerator": sum(scores) if scores else None,
            "denominator": len(scores),
            "value": mean_score,
            "num": sum(scores) if scores else None,
            "den": len(scores),
            "ratio": mean_score,
        },
    }

    result = {
        "jobs": len(unique_jobs),
        "job_count": len(unique_jobs),
        "unique_jobs": len(unique_jobs),
        "deduped_jobs": len(unique_jobs),
        "models": model_list,
        "model_totals": model_list,
        "tokens": {
            "free": free_tokens,
            "free_tokens": free_tokens,
            "paid_exec": paid_exec_tokens,
            "paid_exec_tokens": paid_exec_tokens,
            "paid": paid_exec_tokens,
            "paid_tokens": paid_exec_tokens,
            "coordinator": coordinator_tokens,
            "coordinator_tokens": coordinator_tokens,
            "central": central_tokens,
            "central_tokens": central_tokens,
            "usage": usage_tokens,
            "usage_tokens": usage_tokens,
            "exec": exec_tokens,
            "exec_tokens": exec_tokens,
            "total": total_tokens,
            "total_tokens": total_tokens,
            "tier_tokens": tokens_by_tier,
            "tiers": tokens_by_tier,
            "tokens_by_tier": tokens_by_tier,
            "by_tier": tokens_by_tier,
            "tokens_by_role": tokens_by_role,
            "by_role": tokens_by_role,
            "exec_tokens_in": exec_tokens_in_val,
            "exec_tokens_out": exec_tokens_out_val,
            "exec_tokens_total": exec_tokens_total_val,
            "coordinator_tokens_in": coord_in_val,
            "coordinator_tokens_out": coord_out_val,
            "central_tokens_in": central_in_val,
            "central_tokens_out": central_out_val,
            "cheap_usd": cheap_usd,
            "paid_usd": paid_usd,
            "cost_unknown": cost_unknown,
        },
        "tiers": tokens_by_tier,
        "tier_tokens": tokens_by_tier,
        "tokens_by_tier": tokens_by_tier,
        "tokens_by_role": tokens_by_role,
        "free_tokens": free_tokens,
        "paid_exec_tokens": paid_exec_tokens,
        "paid_tokens": paid_exec_tokens,
        "coordinator_tokens": coordinator_tokens,
        "central_tokens": central_tokens,
        "usage_tokens": usage_tokens,
        "exec_tokens": exec_tokens,
        "total_tokens": total_tokens,
        "pieces": pieces_total,
        "pieces_total": pieces_total,
        "accepted_pieces": accepted_pieces,
        "accepted_free_pieces": accepted_free_pieces,
        "pieces_with_attempts": attempted_pieces,
        "attempted_pieces": attempted_pieces,
        "attempts": attempts_counted,
        "attempts_total": attempts_counted,
        "all_attempts": all_attempts,
        "attempts_per_piece": attempts_per_piece,
        "free_piece_share": free_piece_share,
        "free_token_share": free_token_share,
        "coordinator_share": coordinator_share,
        "central_share": central_share,
        "mean_score": mean_score,
        "scores": scores,
        "grade_count": len(scores),
        "scores_count": len(scores),
        "durations": {
            "wall_seconds": wall_seconds,
            "wall_duration_s": wall_seconds,
            "wall_time_s": wall_seconds,
            "wall_minutes": (wall_seconds / 60.0) if wall_seconds is not None else None,
            "execution_seconds": execution_seconds,
            "execution_time_s": execution_seconds,
            "execution_time": execution_seconds,
            "execution_minutes": execution_seconds / 60.0,
        },
        "wall_seconds": wall_seconds,
        "wall_duration_s": wall_seconds,
        "wall_time_s": wall_seconds,
        "wall_minutes": (wall_seconds / 60.0) if wall_seconds is not None else None,
        "execution_seconds": execution_seconds,
        "execution_time_s": execution_seconds,
        "execution_time": execution_seconds,
        "execution_minutes": execution_seconds / 60.0,
        "estimates": {
            "has_estimates": estimated_any,
            "estimated": estimated_any,
            "estimated_models": sum(1 for m in model_list if m.get("estimated")),
            "estimated_any": estimated_any,
        },
        "estimated": estimated_any,
        "has_estimates": estimated_any,
        "coverage": {
            "label": coverage_label,
            "coverage_label": coverage_label,
            "measured_models": sum(1 for m in model_list if m.get("coverage") == "measured"),
            "estimated_models": sum(1 for m in model_list if m.get("coverage") == "estimated"),
            "unknown_models": sum(1 for m in model_list if m.get("coverage") == "unknown"),
            "incomplete_models": sum(1 for m in model_list if m.get("coverage") == "incomplete"),
            "missing_usage_jobs": missing_usage_jobs,
            "jobs_with_usage": jobs_with_usage,
            "unknown": any(m.get("coverage") in ("unknown", "incomplete") for m in model_list),
            "incomplete": any(m.get("coverage") in ("unknown", "incomplete") for m in model_list),
        },
        "coverage_label": coverage_label,
        "missing_usage": missing_usage_jobs,
        "missing_usage_jobs": missing_usage_jobs,
        "jobs_with_usage": jobs_with_usage,
        "alerts": alerts,
        "thresholds": {
            "alert_coordinator_share": _pref(prefs, "alert_coordinator_share", None),
            "alert_attempts_per_piece": _pref(prefs, "alert_attempts_per_piece", None),
            "alert_job_minutes": _pref(prefs, "alert_job_minutes", None),
            "cheap_budget_usd_day": _pref(prefs, "cheap_budget_usd_day", 0),
        },
        "metrics": metrics,
        "ratios": metrics,
        "totals": {
            "jobs": len(unique_jobs),
            "free_tokens": free_tokens,
            "paid_exec_tokens": paid_exec_tokens,
            "coordinator_tokens": coordinator_tokens,
            "central_tokens": central_tokens,
            "usage_tokens": usage_tokens,
            "exec_tokens": exec_tokens,
            "total_tokens": total_tokens,
            "tier_tokens": tokens_by_tier,
            "tokens_by_tier": tokens_by_tier,
            "tokens_by_role": tokens_by_role,
            "pieces": pieces_total,
            "pieces_total": pieces_total,
            "accepted_pieces": accepted_pieces,
            "accepted_free_pieces": accepted_free_pieces,
            "attempted_pieces": attempted_pieces,
            "attempts": attempts_counted,
            "attempts_total": attempts_counted,
            "all_attempts": all_attempts,
            "attempts_per_piece": attempts_per_piece,
            "free_piece_share": free_piece_share,
            "free_token_share": free_token_share,
            "coordinator_share": coordinator_share,
            "central_share": central_share,
            "mean_score": mean_score,
            "grade_count": len(scores),
            "wall_seconds": wall_seconds,
            "execution_seconds": execution_seconds,
            "cheap_usd": cheap_usd,
            "paid_usd": paid_usd,
            "cost_unknown": cost_unknown,
            "missing_usage_jobs": missing_usage_jobs,
            "jobs_with_usage": jobs_with_usage,
            "coverage_label": coverage_label,
            "estimated": estimated_any,
        },
    }

    # clean up model entries: drop helper attempt count? keep attempts for exec models
    for m in model_list:
        if m.get("role") != "exec" and "attempts" in m:
            m.pop("attempts", None)

    return result
