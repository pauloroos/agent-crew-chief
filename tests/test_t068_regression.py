"""T068 regression tests — MUST FAIL on 962d61e, PASS after fix.

These tests assert the CORRECT invariants that the owner's reviewer
identified as broken. They are not the same as the existing tests that
were weakened to pass on the broken code.

Findings:
1. One-scope invariant: total_tokens == sum(tier_tokens.values()) ALWAYS,
   including when there is recorded coordinator/central usage.
2. Pending and recorded usage classify identically: both go to
   tier_tokens["unclassified"] so the same event has the same bucket
   before and after binding.
3. exec_tokens never goes negative (recorded usage without pending, or
   usage larger than executor events).
4. Public classify() is used instead of private _first_match.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from crew_chief.central_metrics import aggregate


def _prefs(**overrides):
    defaults = dict(
        alert_coordinator_share=0,
        alert_attempts_per_piece=0,
        alert_job_minutes=0,
        cheap_budget_usd_day=0.0,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _job(jid, worktree, pieces=None, usage=None, created="2026-01-01T00:00:00+00:00"):
    j = {"id": jid, "worktree": worktree, "created": created}
    if pieces is not None:
        j["pieces"] = pieces
    if usage is not None:
        j["usage"] = usage
    return j


# ---------------------------------------------------------------------------
# Finding 1: one-scope invariant with recorded coordinator usage
# ---------------------------------------------------------------------------
def test_one_scope_with_recorded_coordinator_usage():
    """total_tokens MUST equal sum(tier_tokens.values()) even when there is
    recorded coordinator usage. On 962d61e line 693 adds recorded_usage_tokens
    on top of sum(tier_tokens), breaking the invariant."""
    jobs = [
        _job("j1", "/tmp/a", pieces=[
            {"n": 1, "accepted": True, "attempts": [
                {"role": "exec", "status": "ok", "cost": "free",
                 "tokens_total": 100, "central_event_id": "e1"},
            ]},
        ], usage=[
            {"role": "coordinator", "tokens_total": 50, "estimated": False},
        ]),
    ]
    result = aggregate(jobs, [], _prefs(), "2026-01-01T00:01:00+00:00")
    tiers = result["tokens_by_tier"]
    # ONE-SCOPE INVARIANT: total_tokens == sum of all tier buckets.
    assert result["total_tokens"] == sum(tiers.values()), (
        f"one-scope broken: total_tokens={result['total_tokens']} != "
        f"sum(tier_tokens)={sum(tiers.values())}; tiers={tiers}"
    )


def test_one_scope_with_recorded_central_usage():
    """Same invariant with central usage instead of coordinator."""
    jobs = [
        _job("j1", "/tmp/a", pieces=[
            {"n": 1, "accepted": True, "attempts": [
                {"role": "exec", "status": "ok", "cost": "free",
                 "tokens_total": 200, "central_event_id": "e1"},
            ]},
        ], usage=[
            {"role": "central", "tokens_total": 80, "estimated": False},
        ]),
    ]
    result = aggregate(jobs, [], _prefs(), "2026-01-01T00:01:00+00:00")
    tiers = result["tokens_by_tier"]
    assert result["total_tokens"] == sum(tiers.values()), (
        f"one-scope broken: total_tokens={result['total_tokens']} != "
        f"sum(tier_tokens)={sum(tiers.values())}; tiers={tiers}"
    )


# ---------------------------------------------------------------------------
# Finding 2: pending vs recorded classified identically
# ---------------------------------------------------------------------------
def test_pending_and_recorded_classify_identically():
    """The same coordinator usage event must land in the SAME tier bucket
    whether it is pending or recorded. On 962d61e pending goes to
    tier_tokens["unclassified"] but recorded does not."""
    # Scenario A: pending only
    pending_only = [
        _job("j1", "/tmp/a", pieces=[
            {"n": 1, "accepted": True, "attempts": [
                {"role": "exec", "status": "ok", "cost": "free",
                 "tokens_total": 100, "central_event_id": "e1"},
            ]},
        ]),
    ]
    pending_usage = [
        {"event_id": "evt1", "role": "coordinator", "tokens_total": 50,
         "estimated": False},
    ]
    res_pending = aggregate(pending_only, pending_usage, _prefs(),
                            "2026-01-01T00:01:00+00:00")

    # Scenario B: recorded only (same tokens, no pending)
    recorded_only = [
        _job("j1", "/tmp/a", pieces=[
            {"n": 1, "accepted": True, "attempts": [
                {"role": "exec", "status": "ok", "cost": "free",
                 "tokens_total": 100, "central_event_id": "e1"},
            ]},
        ], usage=[
            {"role": "coordinator", "tokens_total": 50, "estimated": False},
        ]),
    ]
    res_recorded = aggregate(recorded_only, [], _prefs(),
                             "2026-01-01T00:01:00+00:00")

    # Both must have the same unclassified count for the coordinator usage.
    assert res_pending["tokens_by_tier"]["unclassified"] == \
           res_recorded["tokens_by_tier"]["unclassified"], (
        f"classification mismatch: pending unclassified="
        f"{res_pending['tokens_by_tier']['unclassified']} vs recorded "
        f"unclassified={res_recorded['tokens_by_tier']['unclassified']}"
    )
    # And total_tokens must be identical for the same underlying usage.
    assert res_pending["total_tokens"] == res_recorded["total_tokens"], (
        f"total_tokens mismatch: pending={res_pending['total_tokens']} vs "
        f"recorded={res_recorded['total_tokens']}"
    )


# ---------------------------------------------------------------------------
# Finding 3: exec_tokens never negative
# ---------------------------------------------------------------------------
def test_exec_tokens_never_negative_with_large_coordinator_usage():
    """exec_tokens = total_tokens - usage_tokens must never go negative.
    On 962d61e, when coordinator usage exceeds executor tokens, the formula
    produces a negative value."""
    jobs = [
        _job("j1", "/tmp/a", pieces=[
            {"n": 1, "accepted": True, "attempts": [
                {"role": "exec", "status": "ok", "cost": "free",
                 "tokens_total": 10, "central_event_id": "e1"},
            ]},
        ], usage=[
            {"role": "coordinator", "tokens_total": 500, "estimated": False},
        ]),
    ]
    result = aggregate(jobs, [], _prefs(), "2026-01-01T00:01:00+00:00")
    exec_tok = result["tokens_by_role"]["exec"]
    assert exec_tok >= 0, (
        f"exec_tokens went negative: {exec_tok} "
        f"(total={result['total_tokens']}, usage="
        f"{result['tokens_by_role']['usage']})"
    )


def test_exec_tokens_never_negative_recorded_without_pending():
    """Recorded usage without any executor events must not produce negative
    exec_tokens."""
    jobs = [
        _job("j1", "/tmp/a", pieces=[], usage=[
            {"role": "coordinator", "tokens_total": 200, "estimated": False},
            {"role": "central", "tokens_total": 100, "estimated": False},
        ]),
    ]
    result = aggregate(jobs, [], _prefs(), "2026-01-01T00:01:00+00:00")
    exec_tok = result["tokens_by_role"]["exec"]
    assert exec_tok >= 0, (
        f"exec_tokens went negative with no executor events: {exec_tok}"
    )


# ---------------------------------------------------------------------------
# Finding 4: public classify() used instead of private _first_match
# ---------------------------------------------------------------------------
def test_aggregate_uses_public_classify_not_private_first_match():
    """aggregate() must use the public config.classify() API which respects
    cost='forbidden' wins and cost_default, not the private _first_match."""
    import inspect
    from crew_chief import central_metrics

    source = inspect.getsource(central_metrics.aggregate)
    # The fix must remove direct calls to config._first_match inside aggregate.
    assert "config._first_match" not in source, (
        "aggregate() still calls private config._first_match directly; "
        "must use public config.classify() instead"
    )