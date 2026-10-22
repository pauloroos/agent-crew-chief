"""Detect and hold quota or stalled coordinators with durable policy (T042).

FR-027, FR-028, SC-012, US7/AC1, US7/AC3, US7/AC5.

This module owns the detection of coordinator interruption (quota from CLI
output/exit, stall from dual output+progress inactivity) and the application
of the validated ``on_coordinator_quota`` policy. It persists a continuity
object in the registry so that any central view can show stopped/reset
timestamps and route wait/handoff/stop through the phase-3 action boundary.

Public API
----------
``detect_quota(output: str | None, exit_code: int | None) -> dict | None``
    Return a quota signal dict when the CLI output or exit reports quota;
    ``None`` otherwise. Never infers quota from generic errors.

``detect_stall(last_output_at, last_progress_at, now, stall_minutes) -> bool``
    True only when BOTH output and progress have been absent for the full
    finite positive window.

``apply_policy(record, signal, policy, now) -> dict``
    Return an updated record reflecting the chosen policy action. ``ask``
    creates a durable pending question; ``wait`` holds until a known reset;
    ``stop`` uses existing ownership-safe controls; ``handoff:<cli>`` records
    the explicit target without starting it (T043 owns execution).

``continuity_payload(reason, stopped_at, quota_reset_at, policy, pending) -> dict``
    Build the registry-compatible continuity object with backwards-compatible
    defaults.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

__all__ = [
    "apply_policy",
    "continuity_payload",
    "detect_quota",
    "detect_stall",
    "DEFAULT_STALL_MINUTES",
]

#: Default stall window in minutes; finite positive per US7/AC1.
DEFAULT_STALL_MINUTES = 40.0

#: Recognized quota patterns in CLI output. Adapter-normalized text is the
#: primary source; these catch common raw forms as a safety net. Never treat
#: an arbitrary error as quota.
_QUOTA_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bquota\b", re.IGNORECASE),
    re.compile(r"\brate[\s_-]?limit", re.IGNORECASE),
    re.compile(r"\b429\b"),
    re.compile(r"\bHTTP\s+429\b", re.IGNORECASE),
)

#: Recognized quota-reset timestamp pattern (ISO 8601 with timezone).
_RESET_PATTERN = re.compile(
    r"reset[s]?\s*(?:at|after|on)?\s*([\d]{4}-[\d]{2}-[\d]{2}T[\d]{2}:[\d]{2}:[\d]{2}[^\s,;\)]*)"
    , re.IGNORECASE,
)


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _now_iso(now: datetime | None = None) -> str:
    ref = now or datetime.now(timezone.utc)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    return ref.isoformat(timespec="seconds")


def detect_quota(output: str | None, exit_code: int | None) -> dict | None:
    """Return a quota signal when CLI output or exit explicitly reports quota.

    Returns ``{"reason": "quota", "quota_reset_at": <iso>|None}`` when a
    recognized quota indicator is present. The reset timestamp is extracted
    only from explicit supported reset data; otherwise ``None`` (unknown).
    Returns ``None`` when no quota evidence exists. Never infers quota from
    an arbitrary generic error.
    """
    has_quota = False
    reset_at: str | None = None

    if isinstance(exit_code, int) and exit_code == 429:
        has_quota = True

    if isinstance(output, str) and output.strip():
        for pattern in _QUOTA_PATTERNS:
            if pattern.search(output):
                has_quota = True
                break
        match = _RESET_PATTERN.search(output)
        if match:
            parsed = _parse_iso(match.group(1))
            if parsed is not None:
                reset_at = parsed.isoformat(timespec="seconds")

    if not has_quota:
        return None
    return {"reason": "quota", "quota_reset_at": reset_at}


def detect_stall(
    last_output_at: datetime | None,
    last_progress_at: datetime | None,
    now: datetime | None = None,
    stall_minutes: float = DEFAULT_STALL_MINUTES,
) -> bool:
    """True only when BOTH output and progress have been absent for the window.

    ``stall_minutes`` must be finite and positive; non-positive values disable
    stall detection (returns False). A missing timestamp counts as absent.
    Continuing output or progress resets its respective window independently.
    """
    if not isinstance(stall_minutes, (int, float)) or stall_minutes <= 0:
        return False
    ref = now or datetime.now(timezone.utc)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    window_seconds = float(stall_minutes) * 60.0

    def _absent(ts: datetime | None) -> bool:
        if ts is None:
            return True
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (ref - ts).total_seconds() >= window_seconds

    return _absent(last_output_at) and _absent(last_progress_at)


def continuity_payload(
    reason: str,
    stopped_at: str,
    quota_reset_at: str | None,
    policy: str,
    pending: dict | None = None,
    handoff_history: list[dict] | None = None,
) -> dict:
    """Build the registry-compatible continuity object.

    Backwards-compatible defaults: ``schema_version=1``, empty lists/dicts
    for optional fields. ``quota_reset_at`` is ``null`` when unknown.
    """
    return {
        "schema_version": 1,
        "reason": reason,
        "stopped_at": stopped_at,
        "quota_reset_at": quota_reset_at,
        "policy": policy,
        "pending": pending if isinstance(pending, dict) else {},
        "handoff_history": handoff_history if isinstance(handoff_history, list) else [],
    }


def apply_policy(
    record: dict,
    signal: dict,
    policy: str,
    now: datetime | None = None,
) -> dict:
    """Apply the validated continuity policy to a coordinator record.

    Parameters
    ----------
    record : dict
        Current coordinator record (shallow-copied before mutation).
    signal : dict
        Output of :func:`detect_quota` or a stall signal
        ``{"reason": "stall"}``.
    policy : str
        One of ``"ask"``, ``"wait"``, ``"stop"``, or ``"handoff:<cli>"``.
    now : datetime | None
        Optional clock override for tests.

    Returns
    -------
    dict
        Updated record with continuity metadata and state transition.
    """
    if not isinstance(record, dict):
        raise TypeError("record must be a dict")
    if not isinstance(signal, dict) or "reason" not in signal:
        raise ValueError("signal must be a dict with 'reason'")
    if not isinstance(policy, str) or not policy.strip():
        raise ValueError("policy must be a non-empty string")

    updated = dict(record)
    reason = signal["reason"]
    stopped_at = _now_iso(now)
    quota_reset_at = signal.get("quota_reset_at")
    policy = policy.strip()

    if policy == "ask":
        pending = {
            "type": "continuity_question",
            "question": (
                f"Coordinator {reason}. Choose: wait, handoff:<cli>, or stop."
            ),
            "created_at": stopped_at,
        }
        updated["state"] = "quota" if reason == "quota" else "paused"
        updated["continuity"] = continuity_payload(
            reason, stopped_at, quota_reset_at, policy, pending
        )
    elif policy == "wait":
        updated["state"] = "quota" if reason == "quota" else "paused"
        updated["continuity"] = continuity_payload(
            reason, stopped_at, quota_reset_at, policy
        )
    elif policy == "stop":
        updated["state"] = "stopped"
        updated["continuity"] = continuity_payload(
            reason, stopped_at, quota_reset_at, policy
        )
    elif policy.startswith("handoff:"):
        target_cli = policy.split(":", 1)[1].strip()
        if not target_cli:
            raise ValueError("handoff policy must specify a target CLI")
        updated["state"] = "quota" if reason == "quota" else "paused"
        updated["continuity"] = continuity_payload(
            reason, stopped_at, quota_reset_at, policy,
            pending={"type": "handoff_pending", "target_cli": target_cli},
        )
    else:
        raise ValueError(f"unknown continuity policy {policy!r}")

    updated["updated_at"] = stopped_at
    return updated