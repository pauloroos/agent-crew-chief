"""T041 — keep raw reviewer usage counters, flag inconsistent totals (SC-012, FR-031, US8/AC7)."""
from __future__ import annotations

import json
from types import SimpleNamespace

from crew_chief.central_metrics import aggregate
from crew_chief.tools import parse_metrics


def _prefs(**overrides):
    defaults = dict(
        alert_coordinator_share=0,
        alert_attempts_per_piece=0,
        alert_job_minutes=0,
        cheap_budget_usd_day=0.0,
        models=[],
        setup_models=[],
        forbid_training=False,
        builtin_tiers=True,
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


# Sanitized shape of a real reviewer output: input 56542, output 879,
# reported total 23308, with cache and thinking on top.
_INCONSISTENT_AGY = json.dumps({
    "num_turns": 1,
    "usage": {
        "input_tokens": 56542,
        "cache_read_tokens": 12000,
        "output_tokens": 879,
        "thinking_tokens": 350,
        "total_tokens": 23308,
    },
})


def test_adapter_keeps_reported_total_as_given():
    metrics = parse_metrics({"kind": "antigravity"}, _INCONSISTENT_AGY)

    # Raw counters as given: cache folded exactly once, total never rewritten.
    assert metrics["tokens_in"] == 56542 + 12000
    assert metrics["tokens_out"] == 879 + 350
    assert metrics["tokens_total"] == 23308


def test_adapter_without_double_counting_cache():
    stdout = "\n".join([
        json.dumps({"type": "step_start", "part": {"type": "step-start"}}),
        json.dumps({"type": "step_finish", "part": {
            "type": "step-finish", "cost": 0,
            "tokens": {"input": 1000, "output": 100, "reasoning": 30,
                       "cache": {"read": 200, "write": 50}},
        }}),
    ])
    metrics = parse_metrics({"kind": "opencode"}, stdout)

    assert metrics["tokens_in"] == 1000 + 200 + 50
    assert metrics["tokens_out"] == 100 + 30
    assert metrics["tokens_total"] == 1000 + 200 + 50 + 100 + 30


def test_aggregate_flags_inconsistent_reviewer_usage():
    jobs = [
        _job("j1", "/tmp/a", pieces=[
            {"n": 1, "accepted": True, "attempts": [
                {"role": "review", "status": "ok", "cost": "free",
                 "tokens_in": 56542, "tokens_cached": 12000,
                 "tokens_out": 879, "tokens_reasoning": 350,
                 "tokens_total": 23308, "central_event_id": "rev1"},
            ]},
        ], usage=[
            {"role": "coordinator", "tokens_total": 10,
             "tokens_in": 6, "tokens_out": 4, "estimated": False},
        ]),
    ]
    result = aggregate(jobs, [], _prefs(), "2026-01-01T00:01:00+00:00")

    # Raw counters kept: the reported total is used, never rewritten to the
    # sum of the parts, and the cached split is not counted twice.
    # Defect D fix: total_tokens includes recorded coordinator usage (10).
    assert result["total_tokens"] == 23308 + 10
    assert result["usage_inconsistent"] == 1
    assert any("usage_inconsistent" in d for d in result["diagnostics"])
    assert result["coverage_label"] == "incomplete"


def test_aggregate_consistent_usage_is_not_flagged():
    jobs = [
        _job("j1", "/tmp/a", pieces=[
            {"n": 1, "accepted": True, "attempts": [
                {"role": "review", "status": "ok", "cost": "free",
                 "tokens_in": 100, "tokens_out": 20, "tokens_total": 120,
                 "central_event_id": "rev1"},
            ]},
        ], usage=[
            {"role": "coordinator", "tokens_total": 10,
             "tokens_in": 6, "tokens_out": 4, "estimated": False},
        ]),
    ]
    result = aggregate(jobs, [], _prefs(), "2026-01-01T00:01:00+00:00")

    # Defect D fix: total_tokens includes recorded coordinator usage (10).
    assert result["total_tokens"] == 130
    assert result["usage_inconsistent"] == 0
    assert not any("usage_inconsistent" in d for d in result["diagnostics"])
    assert result["coverage_label"] == "measured"


def test_unclassified_only_for_truly_unknown_models():
    """T068 — attempts without a `cost` field must be classified by the same
    tier rules `pick` uses for that executor/model (or 'coordinator' for
    coordinator rounds); 'Unclassified' remains only for truly unknown
    models."""
    jobs = [
        _job("j1", "/tmp/a", pieces=[
            # antigravity:* is free per tiers.BUILTIN_RULES
            {"n": 1, "accepted": True, "attempts": [
                {"role": "exec", "status": "ok",
                 "executor": "antigravity:gemini-3.8-flash-high",
                 "reported_model": "gemini-3.8-flash-high",
                 "tokens_total": 1000, "central_event_id": "a1"},
            ]},
            # opencode:openrouter/* is cheap per tiers.BUILTIN_RULES
            {"n": 2, "accepted": True, "attempts": [
                {"role": "exec", "status": "ok",
                 "executor": "opencode:openrouter/anthropic/claude-3-haiku",
                 "reported_model": "anthropic/claude-3-haiku",
                 "tokens_total": 500, "central_event_id": "a2"},
            ]},
            # claude:* is paid per tiers.BUILTIN_RULES
            {"n": 3, "accepted": True, "attempts": [
                {"role": "exec", "status": "ok",
                 "executor": "claude:claude-opus-5-5",
                 "reported_model": "claude-opus-5-5",
                 "tokens_total": 200, "central_event_id": "a3"},
            ]},
            # Truly unknown executor → unclassified
            {"n": 4, "accepted": True, "attempts": [
                {"role": "exec", "status": "ok",
                 "executor": "unknown-tool:unknown-model",
                 "reported_model": "unknown-model",
                 "tokens_total": 50, "central_event_id": "a4"},
            ]},
        ], usage=[
            # Coordinator usage without a known tier lives in unclassified
            # (same as pending usage), keeping total == sum(tiers).
            {"role": "coordinator", "tokens_total": 100,
             "tokens_in": 60, "tokens_out": 40, "estimated": False},
        ]),
    ]
    result = aggregate(jobs, [], _prefs(), "2026-01-01T00:01:00+00:00")
    tier_tokens = result["tokens"]["tier_tokens"]

    # Known executors must land in their tier, not unclassified
    assert tier_tokens["free"] >= 1000, f"antigravity tokens should be free, got {tier_tokens}"
    assert tier_tokens["cheap"] >= 500, f"openrouter tokens should be cheap, got {tier_tokens}"
    assert tier_tokens["paid"] >= 200, f"claude tokens should be paid, got {tier_tokens}"
    # The truly unknown executor (50) plus recorded coordinator usage (100).
    assert tier_tokens["unclassified"] == 150, f"unknown-model + coordinator usage should be unclassified, got {tier_tokens}"
    assert result["total_tokens"] == sum(tier_tokens.values())
