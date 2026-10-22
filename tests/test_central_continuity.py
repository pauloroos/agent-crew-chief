"""Tests for coordinator continuity detection and policy (T042).
Verification boundary (tasks.md):
Fake running-child output/progress clocks prove both-window stall detection;
quota output/exit and known/unknown resets; durable ask, exact-session
automatic wait/resume, explicit stop; registry compatibility, no idle calls.
Proof: ``python -m pytest tests/test_central_continuity.py -q``.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from crew_chief.central_continuity import (
    DEFAULT_STALL_MINUTES,
    apply_policy,
    continuity_payload,
    detect_quota,
    detect_stall,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ago(minutes: float) -> datetime:
    return _now() - timedelta(minutes=minutes)


def _ahead(minutes: float) -> str:
    return (_now() + timedelta(minutes=minutes)).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# detect_quota
# ---------------------------------------------------------------------------
class TestDetectQuota:
    def test_none_when_no_evidence(self) -> None:
        assert detect_quota(None, None) is None
        assert detect_quota("", 0) is None
        assert detect_quota("success", 0) is None

    def test_exit_429_is_quota(self) -> None:
        signal = detect_quota(None, 429)
        assert signal is not None
        assert signal["reason"] == "quota"

    def test_output_rate_limit_is_quota(self) -> None:
        signal = detect_quota("Rate limit exceeded. Please try again later.", 1)
        assert signal is not None
        assert signal["reason"] == "quota"

    def test_output_http_429_is_quota(self) -> None:
        signal = detect_quota("Upstream request failed: HTTP 429", 1)
        assert signal is not None
        assert signal["reason"] == "quota"

    def test_generic_error_is_not_quota(self) -> None:
        assert detect_quota("segfault in worker", 1) is None
        assert detect_quota("permission denied", 1) is None

    def test_known_reset_extracted(self) -> None:
        reset = _ahead(30)
        text = f"quota exceeded; resets at {reset}"
        signal = detect_quota(text, 429)
        assert signal is not None
        assert signal["quota_reset_at"] is not None

    def test_unknown_reset_when_absent(self) -> None:
        signal = detect_quota("rate limit hit", 429)
        assert signal is not None
        assert signal["quota_reset_at"] is None


# ---------------------------------------------------------------------------
# detect_stall
# ---------------------------------------------------------------------------
class TestDetectStall:
    def test_false_when_both_recent(self) -> None:
        now = _now()
        recent = now - timedelta(minutes=5)
        assert not detect_stall(recent, recent, now=now)

    def test_false_when_only_output_absent(self) -> None:
        now = _now()
        old_output = now - timedelta(minutes=DEFAULT_STALL_MINUTES + 1)
        recent_progress = now - timedelta(minutes=1)
        assert not detect_stall(old_output, recent_progress, now=now)

    def test_false_when_only_progress_absent(self) -> None:
        now = _now()
        recent_output = now - timedelta(minutes=1)
        old_progress = now - timedelta(minutes=DEFAULT_STALL_MINUTES + 1)
        assert not detect_stall(recent_output, old_progress, now=now)

    def test_true_when_both_absent(self) -> None:
        now = _now()
        old = now - timedelta(minutes=DEFAULT_STALL_MINUTES + 1)
        assert detect_stall(old, old, now=now)

    def test_true_when_never_seen(self) -> None:
        assert detect_stall(None, None, now=_now())

    def test_disabled_when_non_positive(self) -> None:
        old = _ago(DEFAULT_STALL_MINUTES + 10)
        assert not detect_stall(old, old, stall_minutes=0)
        assert not detect_stall(old, old, stall_minutes=-5)

    def test_custom_window(self) -> None:
        now = _now()
        edge = now - timedelta(minutes=6)
        assert detect_stall(edge, edge, now=now, stall_minutes=5)
        assert not detect_stall(edge, edge, now=now, stall_minutes=10)


# ---------------------------------------------------------------------------
# continuity_payload
# ---------------------------------------------------------------------------
class TestContinuityPayload:
    def test_required_fields_present(self) -> None:
        stopped = _now().isoformat(timespec="seconds")
        payload = continuity_payload("quota", stopped, None, "ask")
        assert payload["schema_version"] == 1
        assert payload["reason"] == "quota"
        assert payload["stopped_at"] == stopped
        assert payload["quota_reset_at"] is None
        assert payload["policy"] == "ask"
        assert payload["pending"] == {}
        assert payload["handoff_history"] == []

    def test_known_reset_preserved(self) -> None:
        reset = _ahead(15)
        payload = continuity_payload("quota", _now().isoformat(), reset, "wait")
        assert payload["quota_reset_at"] == reset


# ---------------------------------------------------------------------------
# apply_policy
# ---------------------------------------------------------------------------
class TestApplyPolicy:
    def test_ask_sets_pending_question(self) -> None:
        record = {"state": "running", "updated_at": "old"}
        signal = {"reason": "quota", "quota_reset_at": None}
        updated = apply_policy(record, signal, "ask")
        assert updated["state"] == "quota"
        assert updated["continuity"]["policy"] == "ask"
        assert updated["continuity"]["pending"]["type"] == "continuity_question"
        assert updated is not record

    def test_wait_holds_with_known_reset(self) -> None:
        reset = _ahead(20)
        record = {"state": "running"}
        signal = {"reason": "quota", "quota_reset_at": reset}
        updated = apply_policy(record, signal, "wait")
        assert updated["state"] == "quota"
        assert updated["continuity"]["quota_reset_at"] == reset

    def test_stop_transitions_to_stopped(self) -> None:
        record = {"state": "running"}
        signal = {"reason": "stall"}
        updated = apply_policy(record, signal, "stop")
        assert updated["state"] == "stopped"
        assert updated["continuity"]["reason"] == "stall"

    def test_handoff_records_target_cli(self) -> None:
        record = {"state": "running"}
        signal = {"reason": "quota", "quota_reset_at": None}
        updated = apply_policy(record, signal, "handoff:codex")
        assert updated["state"] == "quota"
        pending = updated["continuity"]["pending"]
        assert pending["type"] == "handoff_pending"
        assert pending["target_cli"] == "codex"

    def test_stall_reason_maps_to_paused_for_ask(self) -> None:
        record = {"state": "running"}
        signal = {"reason": "stall"}
        updated = apply_policy(record, signal, "ask")
        assert updated["state"] == "paused"

    def test_unknown_policy_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown continuity policy"):
            apply_policy({"state": "running"}, {"reason": "quota"}, "explode")

    def test_empty_handoff_target_raises(self) -> None:
        with pytest.raises(ValueError, match="handoff policy must specify"):
            apply_policy({"state": "running"}, {"reason": "quota"}, "handoff:")

    def test_invalid_signal_raises(self) -> None:
        with pytest.raises(ValueError, match="signal must be a dict"):
            apply_policy({"state": "running"}, {}, "ask")  # type: ignore[arg-type]

    def test_record_type_checked(self) -> None:
        with pytest.raises(TypeError, match="record must be a dict"):
            apply_policy("bad", {"reason": "quota"}, "ask")  # type: ignore[arg-type]