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
    """Return a stable identity for an attempt, scoped to its job+piece.

    Uses the first available identifier field.  Returns ``None`` when no
    usable identifier exists (the caller must not dedup such attempts).
    """
    for key in (
        "central_event_id",
        "event_id",
        "id",
        "attempt_id",
        "attempt_uuid",
        "stable_id",
        "uuid",
        "key",
    ):
        value = attempt.get(key)
        if value is not None and str(value).strip() != "":
            # Scope to job+piece so identical IDs in different jobs don't collide.
            job_id = str(job.get("id", "")) if job.get("id") is not None else ""
            piece_n = str(piece.get("n", "")) if piece.get("n") is not None else ""
            return f"{job_id}:{piece_n}:{str(value).strip()}"
    return None


def _counted_attempt(attempt: dict) -> bool:
    return (
        attempt.get("role") == "exec"
        and attempt.get("status") not in _NOT_ATTEMPTS
    )


def _tier_tokens_empty() -> dict[str, int]:
    return {tier: 0 for tier in _TIERS}


def _finite_int(value: Any) -> int | None:
    """Return ``int(value)`` only when *value* is a finite number."""
    if not _number(value):
        return None
    f = float(value)
    if not math.isfinite(f):
        return None
    return int(f)


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

    # wall / execution durations — per-job wall spans, summed
    per_job_wall_seconds = 0.0
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

        # Per-job wall span accumulator
        job_wall_starts: list[datetime] = []
        job_wall_ends: list[datetime] = []

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

                # --- Single canonical token path ---
                # Prefer tokens_total when finite; fall back to in+out.
                raw_total = attempt.get("tokens_total")
                tt = _finite_int(raw_total)
                ti_raw = attempt.get("tokens_in")
                to_raw = attempt.get("tokens_out")
                ti = _finite_int(ti_raw)
                to = _finite_int(to_raw)
                if tt is None and ti is not None and to is not None:
                    tt = ti + to

                tier = attempt.get("cost")
                if tier in tier_tokens and tt is not None:
                    tier_tokens[tier] += tt

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
                if tt is not None:
                    grp["tokens_total"] += tt
                    grp["_has_total"] = True
                else:
                    grp["_missing_total"] = True
                if ti is not None:
                    grp["tokens_in"] = (grp["tokens_in"] or 0) + ti
                    grp["_has_in"] = True
                if to is not None:
                    grp["tokens_out"] = (grp["tokens_out"] or 0) + to
                    grp["_has_out"] = True
                if attempt.get("tokens_estimated") is True:
                    grp["estimated"] = True

                # exec top-level (all attempt tokens) — same canonical path
                if tt is not None:
                    exec_tokens_total += tt
                    exec_has_total = True
                if ti is not None:
                    exec_tokens_in += ti
                    exec_has_in = True
                if to is not None:
                    exec_tokens_out += to
                    exec_has_out = True

                # durations for all attempts
                started = _parse_iso(attempt.get("started"), local_tz)
                if started is not None:
                    duration = attempt.get("duration_s")
                    dur = float(duration) if _number(duration) and math.isfinite(float(duration)) else 0.0
                    if dur < 0:
                        dur = 0.0
                    end = started + timedelta(seconds=dur)
                    job_wall_starts.append(started)
                    job_wall_ends.append(end)
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

        # Per-job wall span: max(end) - min(start) within this job only
        if job_wall_starts and job_wall_ends:
            span = (max(job_wall_ends) - min(job_wall_starts)).total_seconds()
            if span > 0:
                per_job_wall_seconds += span

        # active job wall time — extend this job's span to now
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
                active_span = (now_dt - created).total_seconds()
                if active_span > 0:
                    per_job_wall_seconds += active_span

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

        for u in usage:
            role = u.get("role")
            # Only bucket known roles; unknown/non-null roles are skipped
            # from coordinator/central totals but still appear in models.
            if role not in ("coordinator", "central"):
                if role is None:
                    role = "coordinator"
                # else: keep original role string for model grouping,
                # but do NOT add to coordinator_tokens or central_tokens.

            tokens_total = u.get("tokens_total")
            tt = _finite_int(tokens_total)
            ti = _finite_int(u.get("tokens_in"))
            to = _finite_int(u.get("tokens_out"))
            estimated = u.get("estimated") is True
            model = u.get("model")
            if role == "coordinator":
                if tt is not None:
                    coordinator_tokens += tt
                if ti is not None:
                    usage_tokens_in_coord += ti
                    coord_has_in = True
                if to is not None:
                    usage_tokens_out_coord += to
                    coord_has_out = True
            elif role == "central":
                if tt is not None:
                    central_tokens += tt
                if ti is not None:
                    usage_tokens_in_central += ti
                    central_has_in = True
                if to is not None:
                    usage_tokens_out_central += to
                    central_has_out = True
            # model grouping for usage — use actual role (even if unknown)
            model_role = role if role in ("coordinator", "central") else role
            key = (model_role, model, None)
            grp = models.setdefault(
                key,
                {
                    "role": model_role,
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
            if ti is not None:
                grp["tokens_in"] = (grp["tokens_in"] or 0) + ti
                grp["_has_in"] = True
            if to is not None:
                grp["tokens_out"] = (grp["tokens_out"] or 0) + to
                grp["_has_out"] = True
            if estimated:
                grp["estimated"] = True

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
            if role is None:
                role = "coordinator"
            # else keep as-is for model grouping; don't force to coordinator

        tokens_total = pu.get("tokens_total")
        tt = _finite_int(tokens_total)
        ti = _finite_int(pu.get("tokens_in"))
        to = _finite_int(pu.get("tokens_out"))
        estimated = pu.get("estimated") is True
        model = pu.get("model")
        if role == "coordinator":
            if tt is not None:
                coordinator_tokens += tt
            if ti is not None:
                usage_tokens_in_coord += ti
                coord_has_in = True
            if to is not None:
                usage_tokens_out_coord += to
                coord_has_out = True
        elif role == "central":
            if tt is not None:
                central_tokens += tt
            if ti is not None:
                usage_tokens_in_central += ti
                central_has_in = True
            if to is not None:
                usage_tokens_out_central += to
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
        if ti is not None:
            grp["tokens_in"] = (grp["tokens_in"] or 0) + ti
            grp["_has_in"] = True
        if to is not None:
            grp["tokens_out"] = (grp["tokens_out"] or 0) + to
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
    wall_seconds = per_job_wall_seconds if per_job_wall_seconds > 0 else None

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
    if limit is not None and limit > 0 and coordinator_share is not None and coordinator_share > limit:
        alerts.append(
            f"coordinator spent {coordinator_share:.0%} of the tokens "
            f"(limit {limit:.0%})"
        )
    limit = _pref(prefs, "alert_attempts_per_piece", None)
    if limit is not None and limit > 0 and attempts_per_piece is not None and attempts_per_piece > limit:
        alerts.append(
            f"{attempts_per_piece:.1f} attempts per piece (limit {limit:.1f})"
        )
    limit = _pref(prefs, "alert_job_minutes", None)
    if limit is not None and limit > 0 and wall_seconds is not None and (wall_seconds / 60.0) > limit:
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
        },
        "free_token_share": {
            "numerator": free_tokens,
            "denominator": total_tokens,
            "value": free_token_share,
        },
        "coordinator_share": {
            "numerator": usage_tokens,
            "denominator": total_tokens,
            "value": coordinator_share,
        },
        "central_share": {
            "numerator": central_tokens,
            "denominator": total_tokens,
            "value": central_share,
        },
        "attempts_per_piece": {
            "numerator": attempts_counted,
            "denominator": attempted_pieces,
            "value": attempts_per_piece,
        },
        "mean_score": {
            "numerator": sum(scores) if scores else None,
            "denominator": len(scores),
            "value": mean_score,
        },
    }

    result = {
        "jobs": len(unique_jobs),
        "models": model_list,
        "tokens": {
            "free": free_tokens,
            "paid_exec": paid_exec_tokens,
            "coordinator": coordinator_tokens,
            "central": central_tokens,
            "usage": usage_tokens,
            "exec": exec_tokens,
            "total": total_tokens,
            "tier_tokens": tokens_by_tier,
            "tokens_by_role": tokens_by_role,
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
        "tokens_by_tier": tokens_by_tier,
        "tokens_by_role": tokens_by_role,
        "free_tokens": free_tokens,
        "paid_exec_tokens": paid_exec_tokens,
        "coordinator_tokens": coordinator_tokens,
        "central_tokens": central_tokens,
        "usage_tokens": usage_tokens,
        "exec_tokens": exec_tokens,
        "total_tokens": total_tokens,
        "pieces": pieces_total,
        "accepted_pieces": accepted_pieces,
        "accepted_free_pieces": accepted_free_pieces,
        "attempted_pieces": attempted_pieces,
        "attempts": attempts_counted,
        "all_attempts": all_attempts,
        "attempts_per_piece": attempts_per_piece,
        "free_piece_share": free_piece_share,
        "free_token_share": free_token_share,
        "coordinator_share": coordinator_share,
        "central_share": central_share,
        "mean_score": mean_score,
        "scores": scores,
        "grade_count": len(scores),
        "durations": {
            "wall_seconds": wall_seconds,
            "execution_seconds": execution_seconds,
        },
        "wall_seconds": wall_seconds,
        "execution_seconds": execution_seconds,
        "estimates": {
            "has_estimates": estimated_any,
            "estimated_models": sum(1 for m in model_list if m.get("estimated")),
        },
        "estimated": estimated_any,
        "coverage": {
            "label": coverage_label,
            "measured_models": sum(1 for m in model_list if m.get("coverage") == "measured"),
            "estimated_models": sum(1 for m in model_list if m.get("coverage") == "estimated"),
            "unknown_models": sum(1 for m in model_list if m.get("coverage") == "unknown"),
            "incomplete_models": sum(1 for m in model_list if m.get("coverage") == "incomplete"),
            "missing_usage_jobs": missing_usage_jobs,
            "jobs_with_usage": jobs_with_usage,
        },
        "coverage_label": coverage_label,
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
    }

    # clean up model entries: drop helper attempt count for non-exec
    for m in model_list:
        if m.get("role") != "exec" and "attempts" in m:
            m.pop("attempts", None)

    return result