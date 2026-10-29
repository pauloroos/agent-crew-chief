"""Comprehensive tests for central_metrics.aggregate (SC-006).

Covers: retries/reviews, mixed tiers/models, duplicate job refs, overlapping
time, estimates, missing usage, each threshold with exact expected totals,
NaN guards, unknown roles, stable-id dedup across jobs and within jobs.
"""

from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from crew_chief.central_metrics import aggregate


def _prefs(**overrides):
    defaults = dict(
        alert_coordinator_share=0.5,
        alert_attempts_per_piece=2.0,
        alert_job_minutes=10.0,
        cheap_budget_usd_day=0.0,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _job(jid, worktree, pieces=None, usage=None, created="2026-01-01T00:00:00+00:00", **extra):
    j = {"id": jid, "worktree": worktree, "created": created}
    if pieces is not None:
        j["pieces"] = pieces
    if usage is not None:
        j["usage"] = usage
    j.update(extra)
    return j


def _piece(n, attempts=None, accepted=False):
    p = {"n": n, "accepted": accepted}
    if attempts is not None:
        p["attempts"] = attempts
    return p


def _attempt(
    role="exec",
    status="ok",
    cost="free",
    tokens_total=None,
    tokens_in=None,
    tokens_out=None,
    tokens_estimated=False,
    started="2026-01-01T00:00:00+00:00",
    duration_s=5,
    reported_model=None,
    event_id=None,
    cost_usd=None,
    cost_known=True,
):
    a = {
        "role": role,
        "status": status,
        "cost": cost,
        "tokens_estimated": tokens_estimated,
        "started": started,
        "duration_s": duration_s,
    }
    if tokens_total is not None:
        a["tokens_total"] = tokens_total
    if tokens_in is not None:
        a["tokens_in"] = tokens_in
    if tokens_out is not None:
        a["tokens_out"] = tokens_out
    if reported_model is not None:
        a["reported_model"] = reported_model
    if event_id is not None:
        a["central_event_id"] = event_id
    if cost_usd is not None:
        a["cost_usd"] = cost_usd
    if cost_known is not True:
        a["cost_known"] = cost_known
    return a


# --- SC-006: smoke -----------------------------------------------------------

def test_aggregate_smoke():
    jobs = [
        _job("job-1", "/tmp/repo", pieces=[
            _piece(1, attempts=[
                _attempt(tokens_total=10, tokens_in=4, tokens_out=6),
            ], accepted=True),
        ], usage=[
            {"role": "coordinator", "tokens_total": 3, "tokens_in": 1, "tokens_out": 2, "estimated": False},
        ]),
    ]
    result = aggregate(jobs, [], _prefs(), "2026-01-01T00:00:10+00:00")
    assert result["jobs"] == 1
    assert result["free_tokens"] == 10
    # Defect D fix: recorded coordinator/central usage IS included in total_tokens
    # so the tokens card shows live usage from running coordinators.
    assert result["total_tokens"] == 13
    assert result["attempts_per_piece"] == 1.0
    assert result["wall_seconds"] == 5.0
    assert result["coverage_label"] == "measured"


# --- SC-006: retries and reviews included in tier/model totals ---------------

def test_retries_and_reviews_counted():
    jobs = [
        _job("j1", "/tmp/a", pieces=[
            _piece(1, attempts=[
                _attempt(cost="free", tokens_total=10, event_id="e1"),
                _attempt(cost="free", tokens_total=8, event_id="e2"),  # retry
                _attempt(role="review", cost="free", tokens_total=5, event_id="e3"),  # review
            ], accepted=True),
        ]),
    ]
    result = aggregate(jobs, [], _prefs(), "2026-01-01T00:01:00+00:00")
    # All three attempts counted in free tier
    assert result["tokens_by_tier"]["free"] == 23
    assert result["all_attempts"] == 3
    # exec attempts only for attempts_per_piece
    assert result["attempts"] == 2  # two exec attempts
    assert result["attempts_per_piece"] == 2.0


# --- SC-006: mixed tiers and models -----------------------------------------

def test_mixed_tiers_and_models():
    jobs = [
        _job("j1", "/tmp/a", pieces=[
            _piece(1, attempts=[
                _attempt(cost="free", tokens_total=10, reported_model="model-a", event_id="e1"),
                _attempt(cost="cheap", tokens_total=20, reported_model="model-b", event_id="e2", cost_usd=0.05),
                _attempt(cost="paid", tokens_total=30, reported_model="model-a", event_id="e3", cost_usd=0.10),
            ], accepted=True),
        ]),
    ]
    result = aggregate(jobs, [], _prefs(), "2026-01-01T00:01:00+00:00")
    assert result["tokens_by_tier"]["free"] == 10
    assert result["tokens_by_tier"]["cheap"] == 20
    assert result["tokens_by_tier"]["paid"] == 30
    assert result["total_tokens"] == 60
    # Models grouped by (role, model, tier)
    model_keys = [(m["role"], m["model"], m["tier"]) for m in result["models"]]
    assert ("exec", "model-a", "free") in model_keys
    assert ("exec", "model-b", "cheap") in model_keys
    assert ("exec", "model-a", "paid") in model_keys


# --- SC-006: duplicate job refs deduped --------------------------------------

def test_duplicate_job_refs_deduped():
    job = _job("j1", "/tmp/a", pieces=[
        _piece(1, attempts=[
            _attempt(tokens_total=10, event_id="e1"),
        ], accepted=True),
    ])
    # Same job appears twice in the list
    result = aggregate([job, job], [], _prefs(), "2026-01-01T00:01:00+00:00")
    assert result["jobs"] == 1
    assert result["free_tokens"] == 10
    # Defect D fix: recorded usage included in total; deduped job still counts once.
    assert result["total_tokens"] == 10  # no coordinator usage in this fixture


# --- SC-006: overlapping time — wall is per-job sum, not global span ---------

def test_overlapping_jobs_wall_per_job():
    jobs = [
        _job("j1", "/tmp/a", created="2026-01-01T00:00:00+00:00", pieces=[
            _piece(1, attempts=[
                _attempt(tokens_total=10, started="2026-01-01T00:00:00+00:00", duration_s=60, event_id="e1"),
            ]),
        ]),
        _job("j2", "/tmp/b", created="2026-01-01T00:00:30+00:00", pieces=[
            _piece(1, attempts=[
                _attempt(tokens_total=10, started="2026-01-01T00:00:30+00:00", duration_s=60, event_id="e2"),
            ]),
        ]),
    ]
    result = aggregate(jobs, [], _prefs(), "2026-01-01T00:02:00+00:00")
    # Each job has wall=60s; total wall = 60+60 = 120, NOT 90 (global span)
    assert result["wall_seconds"] == 120.0
    assert result["execution_seconds"] == 120.0


# --- SC-006: estimates labelled correctly ------------------------------------

def test_estimates_labelled():
    jobs = [
        _job("j1", "/tmp/a", pieces=[
            _piece(1, attempts=[
                _attempt(tokens_total=10, tokens_estimated=True, event_id="e1"),
            ], accepted=True),
        ]),
    ]
    result = aggregate(jobs, [], _prefs(), "2026-01-01T00:01:00+00:00")
    assert result["estimated"] is True
    assert result["estimated_any"] is True
    assert result["estimated_tokens"] == 10
    # Missing job usage makes coverage partial while the estimate marker stays.
    assert result["missing_usage_jobs"] == 1
    assert result["coverage_label"] == "incomplete"
    measured = [m for m in result["models"] if m.get("coverage") == "estimated"]
    assert len(measured) >= 1


# --- SC-006: missing usage → unknown coverage --------------------------------

def test_missing_usage_unknown_coverage():
    jobs = [
        _job("j1", "/tmp/a", pieces=[
            _piece(1, attempts=[
                _attempt(tokens_total=10, event_id="e1"),
            ], accepted=True),
        ]),
        # No usage at all
    ]
    result = aggregate(jobs, [], _prefs(), "2026-01-01T00:01:00+00:00")
    assert result["missing_usage_jobs"] == 1
    assert result["jobs_with_usage"] == 0


# --- SC-006: NaN guard — tokens_total=NaN must not crash ---------------------

def test_nan_tokens_total_no_crash():
    jobs = [
        _job("j1", "/tmp/a", pieces=[
            _piece(1, attempts=[
                _attempt(tokens_total=float("nan"), tokens_in=4, tokens_out=6, event_id="e1"),
            ], accepted=True),
        ]),
    ]
    result = aggregate(jobs, [], _prefs(), "2026-01-01T00:01:00+00:00")
    # NaN total ignored; falls back to in+out
    assert result["free_tokens"] == 10
    assert result["total_tokens"] == 10


def test_inf_tokens_total_no_crash():
    jobs = [
        _job("j1", "/tmp/a", pieces=[
            _piece(1, attempts=[
                _attempt(tokens_total=float("inf"), tokens_in=4, tokens_out=6, event_id="e1"),
            ], accepted=True),
        ]),
    ]
    result = aggregate(jobs, [], _prefs(), "2026-01-01T00:01:00+00:00")
    assert result["free_tokens"] == 10


def test_negative_tokens_total_treated_as_absent():
    """T068: a negative tokens_total is invalid and must be treated as absent.

    The attempt reports -10 but its parts say 5 in + 5 out, so the canonical
    contribution is 10 and no aggregate may go negative.
    """
    jobs = [
        _job("j1", "/tmp/a", pieces=[
            _piece(1, attempts=[
                _attempt(tokens_total=-10, tokens_in=5, tokens_out=5, event_id="e1"),
            ], accepted=True),
        ]),
    ]
    result = aggregate(jobs, [], _prefs(), "2026-01-01T00:01:00+00:00")
    assert result["free_tokens"] == 10
    assert result["total_tokens"] == 10
    assert result["exec_tokens"] == 10
    assert result["exec_tokens"] >= 0


# --- SC-006: intra-job duplicate event_id deduped ----------------------------

def test_intra_job_duplicate_event_id_deduped():
    jobs = [
        _job("j1", "/tmp/a", pieces=[
            _piece(1, attempts=[
                _attempt(tokens_total=10, event_id="same-id"),
                _attempt(tokens_total=10, event_id="same-id"),  # duplicate
            ], accepted=True),
        ]),
    ]
    result = aggregate(jobs, [], _prefs(), "2026-01-01T00:01:00+00:00")
    # Only one attempt counted
    assert result["free_tokens"] == 10
    assert result["all_attempts"] == 1


# --- SC-006: cross-job same event_id NOT deduped (scoped to job+piece) -------

def test_cross_job_same_event_id_not_deduped():
    jobs = [
        _job("j1", "/tmp/a", pieces=[
            _piece(1, attempts=[
                _attempt(tokens_total=10, event_id="shared-id"),
            ], accepted=True),
        ]),
        _job("j2", "/tmp/b", pieces=[
            _piece(1, attempts=[
                _attempt(tokens_total=20, event_id="shared-id"),
            ], accepted=True),
        ]),
    ]
    result = aggregate(jobs, [], _prefs(), "2026-01-01T00:01:00+00:00")
    # Different jobs → both counted
    assert result["free_tokens"] == 30
    assert result["all_attempts"] == 2


# --- SC-006: unknown role in usage not silently bucketed to coordinator ------

def test_unknown_usage_role_not_bucketed_to_coordinator():
    jobs = [
        _job("j1", "/tmp/a", pieces=[
            _piece(1, attempts=[
                _attempt(tokens_total=10, event_id="e1"),
            ], accepted=True),
        ], usage=[
            {"role": "weird", "tokens_total": 99, "estimated": False},
        ]),
    ]
    result = aggregate(jobs, [], _prefs(), "2026-01-01T00:01:00+00:00")
    # Unknown role should NOT appear in coordinator_tokens
    assert result["coordinator_tokens"] == 0
    # Unknown roles are excluded from coordinator/central totals and from
    # tier_tokens; only exec tokens count.
    assert result["total_tokens"] == 10


# --- SC-006: threshold alerts fire correctly ---------------------------------

def test_alert_coordinator_share_fires():
    jobs = [
        _job("j1", "/tmp/a", pieces=[
            _piece(1, attempts=[
                _attempt(tokens_total=10, event_id="e1"),
            ], accepted=True),
        ], usage=[
            {"role": "coordinator", "tokens_total": 20, "estimated": False},
        ]),
    ]
    # coordinator_share = 20/30 = 66.7% > 50% limit
    result = aggregate(jobs, [], _prefs(alert_coordinator_share=0.5), "2026-01-01T00:01:00+00:00")
    assert any("coordinator spent" in a for a in result["alerts"])


def test_alert_coordinator_share_zero_disables():
    jobs = [
        _job("j1", "/tmp/a", pieces=[
            _piece(1, attempts=[
                _attempt(tokens_total=10, event_id="e1"),
            ], accepted=True),
        ], usage=[
            {"role": "coordinator", "tokens_total": 999, "estimated": False},
        ]),
    ]
    result = aggregate(jobs, [], _prefs(alert_coordinator_share=0), "2026-01-01T00:01:00+00:00")
    assert not any("coordinator spent" in a for a in result["alerts"])


def test_alert_attempts_per_piece_fires():
    jobs = [
        _job("j1", "/tmp/a", pieces=[
            _piece(1, attempts=[
                _attempt(tokens_total=5, event_id="e1"),
                _attempt(tokens_total=5, event_id="e2"),
                _attempt(tokens_total=5, event_id="e3"),
            ], accepted=True),
        ]),
    ]
    # 3 attempts / 1 piece = 3.0 > 2.0 limit
    result = aggregate(jobs, [], _prefs(alert_attempts_per_piece=2.0), "2026-01-01T00:01:00+00:00")
    assert any("attempts per piece" in a for a in result["alerts"])


def test_alert_job_minutes_fires():
    jobs = [
        _job("j1", "/tmp/a", created="2026-01-01T00:00:00+00:00", pieces=[
            _piece(1, attempts=[
                _attempt(tokens_total=10, started="2026-01-01T00:00:00+00:00", duration_s=900, event_id="e1"),
            ]),
        ]),
    ]
    # wall = 900s = 15 min > 10 min limit
    result = aggregate(jobs, [], _prefs(alert_job_minutes=10.0), "2026-01-01T00:15:00+00:00")
    assert any("job took" in a for a in result["alerts"])


def test_alert_cheap_budget_fires():
    jobs = [
        _job("j1", "/tmp/a", pieces=[
            _piece(1, attempts=[
                _attempt(cost="cheap", tokens_total=100, cost_usd=0.85, event_id="e1"),
            ], accepted=True),
        ]),
    ]
    # 0.85 >= 0.8 * 1.0 budget
    result = aggregate(jobs, [], _prefs(cheap_budget_usd_day=1.0), "2026-01-01T00:01:00+00:00")
    assert any("cheap spend" in a for a in result["alerts"])


# --- SC-006: pending usage dedup against recorded ---------------------------

def test_pending_usage_dedup_against_recorded():
    jobs = [
        _job("j1", "/tmp/a", pieces=[
            _piece(1, attempts=[
                _attempt(tokens_total=10, event_id="e1"),
            ], accepted=True),
        ], usage=[
            {"role": "coordinator", "tokens_total": 5, "central_event_id": "u1", "estimated": False},
        ]),
    ]
    pending = [
        {"role": "coordinator", "tokens_total": 5, "event_id": "u1"},  # same as recorded
        {"role": "coordinator", "tokens_total": 7, "event_id": "u2"},  # new
    ]
    result = aggregate(jobs, pending, _prefs(), "2026-01-01T00:01:00+00:00")
    # u1 deduped, u2 added: coordinator = 5 + 7 = 12
    assert result["coordinator_tokens"] == 12


def test_empty_central_event_id_falls_back_to_event_id():
    """T068: an empty central_event_id must not bypass recorded-event dedup.

    The recorded usage entry carries central_event_id="" but event_id="e";
    it must register "e" so the pending entry with the same event_id is
    deduped. Before the fix the empty string fell through and both counted.
    """
    jobs = [
        _job("j1", "/tmp/a", pieces=[
            _piece(1, attempts=[
                _attempt(tokens_total=0, event_id="attempt-1"),
            ], accepted=True),
        ], usage=[
            {"role": "coordinator", "tokens_total": 50,
             "central_event_id": "", "event_id": "e", "estimated": False},
        ]),
    ]
    pending = [
        {"role": "coordinator", "tokens_total": 50, "event_id": "e"},
    ]
    result = aggregate(jobs, pending, _prefs(), "2026-01-01T00:01:00+00:00")
    assert result["total_tokens"] == 50
    assert result["coordinator_tokens"] == 50


# --- SC-006: tokens_in/out fallback when tokens_total missing ----------------

def test_tokens_in_out_fallback():
    jobs = [
        _job("j1", "/tmp/a", pieces=[
            _piece(1, attempts=[
                _attempt(tokens_in=4, tokens_out=6, event_id="e1"),  # no tokens_total
            ], accepted=True),
        ]),
    ]
    result = aggregate(jobs, [], _prefs(), "2026-01-01T00:01:00+00:00")
    assert result["free_tokens"] == 10
    assert result["total_tokens"] == 10


# --- SC-006: ratios use summed numerators/denominators -----------------------

def test_ratios_summed_correctly():
    jobs = [
        _job("j1", "/tmp/a", pieces=[
            _piece(1, attempts=[
                _attempt(cost="free", tokens_total=10, event_id="e1"),
            ], accepted=True),
            _piece(2, attempts=[
                _attempt(cost="paid", tokens_total=30, event_id="e2", cost_usd=0.10),
            ], accepted=True),
        ], usage=[
            {"role": "coordinator", "tokens_total": 10, "estimated": False},
        ]),
    ]
    result = aggregate(jobs, [], _prefs(), "2026-01-01T00:01:00+00:00")
    # Defect D fix: total_tokens now includes recorded coordinator usage (10),
    # so free_token_share = 10 / (10+30+10) = 10/50 = 0.2
    assert result["free_token_share"] == pytest.approx(0.2)
    # Defect D fix: coordinator_share now uses total_tokens (including
    # recorded coordinator usage) as denominator: 10/(10+30+10) = 0.2
    assert result["coordinator_share"] == pytest.approx(0.2)
    # free_piece_share = 1/2 = 0.5
    assert result["free_piece_share"] == pytest.approx(0.5)


# --- SC-006: empty input -----------------------------------------------------

def test_empty_jobs():
    result = aggregate([], [], _prefs(), "2026-01-01T00:00:00+00:00")
    assert result["jobs"] == 0
    assert result["total_tokens"] == 0
    assert result["coverage_label"] == "unknown"
    assert result["alerts"] == []


def test_recorded_and_pending_usage_share_one_scope_without_prefs():
    """Recorded and pending coordinator usage are classified identically
    (both in unclassified) so total_tokens == sum(tier_tokens); aggregate
    must not crash when prefs is None (snapshot passes None)."""
    jobs = [
        _job("j1", "/tmp/a", pieces=[
            _piece(1, attempts=[
                _attempt(cost="free", tokens_total=40, event_id="e1"),
            ], accepted=True),
        ], usage=[
            {"role": "coordinator", "tokens_total": 50,
             "tokens_in": 30, "tokens_out": 20, "estimated": False},
        ]),
    ]
    pending = [
        {"role": "coordinator", "tokens_total": 10,
         "tokens_in": 6, "tokens_out": 4, "estimated": False,
         "event_id": "pending-1"},
    ]
    result = aggregate(jobs, pending, None, "2026-01-01T00:01:00+00:00")
    tiers = result["tokens_by_tier"]
    assert tiers["free"] == 40
    assert tiers["unclassified"] == 60
    assert result["total_tokens"] == sum(tiers.values()) == 100
    assert result["coordinator_tokens"] == 60
    assert result["exec_tokens"] == 40
    assert result["exec_tokens"] >= 0