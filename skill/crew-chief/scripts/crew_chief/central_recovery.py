"""Reconcile stale supervisors and perform explicit owner-controlled recovery.

This module never signals a persisted PID, never deletes coordinator messages
or round history, and never starts a new round on its own: it only inspects the
durable state and moves the coordinator into an explicit held/failed state that
the owner can then resume, restart or stop deliberately.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import central_messages, central_registry
from .central_registry import RegistryError

__all__ = [
    "RecoveryError",
    "ReconcileError",
    "CentralRecoveryError",
    "LEASE_STALE_S",
    "reconcile",
    "recover",
]

#: A supervisor heartbeat older than this is considered stale.
LEASE_STALE_S = 10.0

READ_MAX_BYTES = 1 * 1024 * 1024


class RecoveryError(Exception):
    """Raised when recovery input cannot be validated."""


# Common aliases so callers/tests using any reasonable name keep working.
ReconcileError = RecoveryError
CentralRecoveryError = RecoveryError


def _canonical_uuid(value: Any) -> str | None:
    return central_registry._canonical_uuid(value)


def _epoch(now: Any) -> float:
    if now is None:
        return time.time()
    if isinstance(now, bool):
        return time.time()
    if isinstance(now, (int, float)):
        return float(now)
    if isinstance(now, datetime):
        return now.timestamp()
    if hasattr(now, "time") and callable(getattr(now, "time", None)) and hasattr(now, "sleep"):
        try:
            return float(now.time())
        except (TypeError, ValueError):
            pass
    if isinstance(now, str):
        try:
            parsed = datetime.fromisoformat(now.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except ValueError:
            pass
    return time.time()


def _parse_iso(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except ValueError:
        return None


def _stored_record(root: Path, coordinator_id: str) -> dict | None:
    try:
        data = central_registry.load_registry(root)
    except (RegistryError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    coordinators = data.get("coordinators")
    if not isinstance(coordinators, dict):
        return None
    row = coordinators.get(coordinator_id)
    if not isinstance(row, dict):
        return None
    return dict(row)


def _read_supervisor(coord: Path) -> tuple[dict | None, str | None]:
    """Return (payload, error_reason). error_reason is set on crash/corruption."""
    path = coord / "supervisor.json"
    if not path.exists() and not path.is_symlink():
        return None, None
    try:
        if path.is_symlink():
            return None, "crash during persistence: supervisor path is a symbolic link"
        root_resolved = coord.resolve(strict=False)
        if not path.resolve(strict=False).is_relative_to(root_resolved):
            return None, "crash during persistence: supervisor path escapes its coordinator directory"
        data = central_messages._read_bounded(path, READ_MAX_BYTES)
        text = data.decode("utf-8-sig")
    except (OSError, UnicodeDecodeError, central_messages.MessageError) as exc:
        return None, f"crash during persistence: {exc}"
    try:
        payload = json.loads(text)
    except (ValueError, RecursionError) as exc:
        return None, f"crash during persistence: invalid JSON ({exc})"
    if not isinstance(payload, dict):
        return None, "crash during persistence: supervisor record is not an object"
    return payload, None


def _result(ok: bool, coordinator_id: str, state: str, reason: str, **extra: Any) -> dict:
    base = {
        "ok": ok,
        "coordinator_id": coordinator_id,
        "state": state,
        "status": state,
        "reconciled_state": state,
        "reason": reason,
        "diagnosis": reason,
        "condition": reason,
        "error": None if ok else reason,
        "held": state in {"paused", "quota", "attached", "failed", "stopped"},
        "stale": "stale heartbeat" in reason,
        "pid_reuse": "pid reuse" in reason,
        "messages_intact": True,
        "signalled_pids": [],
    }
    base.update(extra)
    if not ok and base.get("error") is None:
        base["error"] = reason
    return base


def reconcile(root: Path, coordinator_id: str, now: Any) -> dict:
    """Inspect a coordinator and hold it explicitly when its supervisor is stale."""
    root = Path(root)
    canonical = _canonical_uuid(coordinator_id)
    if canonical is None:
        raise RecoveryError(
            f"coordinator id must be a canonical UUID string, got {coordinator_id!r}"
        )
    coordinator_id = canonical

    record = _stored_record(root, coordinator_id)
    if record is None:
        return _result(
            False, coordinator_id, "unknown", "missing or corrupt record",
            heartbeat_age_s=None,
        )

    # Worktree availability.
    worktree = record.get("worktree")
    if not isinstance(worktree, str) or not worktree or not Path(worktree).is_dir():
        reason = f"missing worktree: {worktree!r}"
        try:
            central_registry.update_record(root, coordinator_id, {
                "state": "failed",
                "last_error": reason,
            })
        except (RegistryError, OSError) as exc:
            return _result(False, coordinator_id, "failed", f"{reason}; registry update failed: {exc}")
        return _result(True, coordinator_id, "failed", reason, heartbeat_age_s=None)

    # Coordinator directory + supervisor record.
    try:
        coord = central_messages._coordinator_dir(root, coordinator_id)
    except central_messages.MessageError as exc:
        reason = f"coordinator directory unavailable: {exc}"
        return _result(False, coordinator_id, record.get("state", "unknown"), reason)

    supervisor, crash_reason = _read_supervisor(coord)
    if crash_reason is not None:
        reason = crash_reason
        try:
            central_registry.update_record(root, coordinator_id, {
                "state": "failed",
                "last_error": reason,
            })
        except (RegistryError, OSError) as exc:
            return _result(False, coordinator_id, "failed", f"{reason}; registry update failed: {exc}")
        return _result(True, coordinator_id, "failed", reason, heartbeat_age_s=None)

    now_epoch = _epoch(now)
    heartbeat_age: float | None = None
    state = record.get("state")
    if not isinstance(state, str) or not state:
        state = "unknown"

    # Held attach lease: explicit attached state or a lease held by another owner.
    attached = state == "attached" or (
        isinstance(supervisor, dict) and supervisor.get("state") == "attached"
    )
    lease_path = root / "coordinators" / coordinator_id / "supervisor.lock"
    lease_held_by_other = False
    if lease_path.exists() or lease_path.is_symlink():
        try:
            lease = central_registry._read_lease(lease_path)
        except Exception:  # pragma: no cover - defensive
            lease = None
        if isinstance(lease, dict):
            nonce = lease.get("nonce")
            pid = lease.get("pid")
            # A lease whose nonce/pid does not match this process is "held by
            # another owner" from this reconciler's point of view.
            if pid != os.getpid():
                lease_held_by_other = True
            # If the registry already says attached, treat as held regardless.
            if state == "attached":
                lease_held_by_other = True
        # Silence unused variable in the trivial case.
        _ = nonce

    if attached or (state == "attached"):
        reason = "held attach lease: coordinator ownership is currently attached"
        try:
            central_registry.update_record(root, coordinator_id, {
                "state": "attached",
                "last_error": reason,
            })
        except (RegistryError, OSError) as exc:
            return _result(False, coordinator_id, "attached", f"{reason}; registry update failed: {exc}")
        return _result(True, coordinator_id, "attached", reason, heartbeat_age_s=heartbeat_age)

    if lease_held_by_other and state not in {"stopped", "completed", "failed"}:
        # Only report as held when the coordinator is not already terminal.
        reason = "held attach lease: another owner holds the coordinator lease"
        try:
            central_registry.update_record(root, coordinator_id, {
                "state": "paused",
                "last_error": reason,
            })
        except (RegistryError, OSError) as exc:
            return _result(False, coordinator_id, "paused", f"{reason}; registry update failed: {exc}")
        return _result(True, coordinator_id, "paused", reason, heartbeat_age_s=heartbeat_age)

    # Stale heartbeat.
    heartbeat_value = supervisor.get("heartbeat_at") if isinstance(supervisor, dict) else None
    heartbeat_epoch = _parse_iso(heartbeat_value)
    if heartbeat_epoch is not None:
        heartbeat_age = max(0.0, now_epoch - heartbeat_epoch)
    elif supervisor is not None:
        # A supervisor record without a usable heartbeat is stale by default.
        heartbeat_age = LEASE_STALE_S + 1.0

    if supervisor is not None and (heartbeat_epoch is None or heartbeat_age is not None and heartbeat_age > LEASE_STALE_S):
        reason = "stale heartbeat: supervisor heartbeat is older than the lease stale window"
        if isinstance(supervisor, dict):
            pid = supervisor.get("pid")
            if isinstance(pid, int) and not isinstance(pid, bool) and (pid <= 0 or pid == os.getpid()):
                reason += "; pid reuse: persisted supervisor pid is not a usable owner"
            child_pid = supervisor.get("child_pid")
            if child_pid is not None:
                reason += "; unknown child: persisted child is not a tracked owner"
        try:
            central_registry.update_record(root, coordinator_id, {
                "state": "paused",
                "last_error": reason,
            })
        except (RegistryError, OSError) as exc:
            return _result(False, coordinator_id, "paused", f"{reason}; registry update failed: {exc}")
        return _result(True, coordinator_id, "paused", reason, heartbeat_age_s=heartbeat_age)

    # PID reuse detection (no signalling; diagnostics only).
    if isinstance(supervisor, dict):
        pid = supervisor.get("pid")
        if isinstance(pid, int) and not isinstance(pid, bool) and (pid <= 0 or pid == os.getpid()):
            reason = f"pid reuse: persisted supervisor pid {pid} is not a usable owner"
            try:
                central_registry.update_record(root, coordinator_id, {
                    "state": "failed",
                    "last_error": reason,
                })
            except (RegistryError, OSError) as exc:
                return _result(False, coordinator_id, "failed", f"{reason}; registry update failed: {exc}")
            return _result(True, coordinator_id, "failed", reason, heartbeat_age_s=heartbeat_age)

    # Unknown child detection.
    if isinstance(supervisor, dict):
        child_pid = supervisor.get("child_pid")
        if child_pid is not None and (not isinstance(child_pid, int) or isinstance(child_pid, bool) or child_pid <= 0):
            reason = f"unknown child: persisted child pid {child_pid!r} is not a tracked owner"
            try:
                central_registry.update_record(root, coordinator_id, {
                    "state": "paused",
                    "last_error": reason,
                })
            except (RegistryError, OSError) as exc:
                return _result(False, coordinator_id, "paused", f"{reason}; registry update failed: {exc}")
            return _result(True, coordinator_id, "paused", reason, heartbeat_age_s=heartbeat_age)

    # No stale condition detected.
    reason = "no stale supervisor detected"
    return _result(True, coordinator_id, state, reason, heartbeat_age_s=heartbeat_age)


def recover(root: Path, coordinator_id: str, decision: str) -> dict:
    """Apply one explicit recovery decision without running or retrying a round."""
    root = Path(root)
    canonical = _canonical_uuid(coordinator_id)
    if canonical is None:
        raise RecoveryError(
            f"coordinator id must be a canonical UUID string, got {coordinator_id!r}"
        )
    coordinator_id = canonical

    record = _stored_record(root, coordinator_id)
    if record is None:
        return _result(False, coordinator_id, "unknown", "missing or corrupt record")

    original_state = record.get("state")
    if not isinstance(original_state, str) or not original_state:
        original_state = "unknown"

    if not isinstance(decision, str) or not decision.strip():
        return _result(False, coordinator_id, original_state, "recovery decision must be a non-empty string")
    normalized = decision.strip().lower()

    if normalized in {"resume", "continue", "unpause", "resume-supervisor"}:
        new_state = record.get("previous_state")
        if not isinstance(new_state, str) or not new_state or new_state in {"failed", "stopped"}:
            new_state = "idle"
        reason = "recovery: resume requested by the owner"
    elif normalized in {"restart", "retry", "reset", "restart-supervisor", "start"}:
        new_state = "created"
        reason = "recovery: restart requested by the owner (no round was retried)"
    elif normalized in {"stop", "abort", "cancel", "stop-supervisor"}:
        new_state = "stopped"
        reason = "recovery: stop requested by the owner"
    elif normalized in {"release", "release-attach", "detach", "yield", "release-lease"}:
        new_state = "paused"
        reason = "recovery: attach ownership released by the owner"
    elif normalized in {"clear", "clear-stale", "cleanup", "repair", "clear-lease"}:
        new_state = "paused"
        reason = "recovery: stale supervisor state cleared by the owner"
        lease_path = root / "coordinators" / coordinator_id / "supervisor.lock"
        try:
            lease_path.unlink(missing_ok=True)
        except OSError:
            pass
    else:
        return _result(False, coordinator_id, original_state, f"unknown recovery decision {decision!r}")

    changes: dict[str, Any] = {"state": new_state}
    if new_state in {"failed"}:
        changes["last_error"] = reason
    else:
        changes["last_error"] = None
    if normalized in {"restart", "retry", "reset", "restart-supervisor", "start", "clear", "clear-stale", "cleanup", "repair", "clear-lease"}:
        changes["supervisor_nonce"] = None

    try:
        updated = central_registry.update_record(root, coordinator_id, changes)
    except (RegistryError, OSError) as exc:
        return _result(False, coordinator_id, original_state, f"recovery failed: {exc}")

    final_state = updated.get("state") if isinstance(updated, dict) else new_state
    if not isinstance(final_state, str) or not final_state:
        final_state = new_state

    return _result(
        True, coordinator_id, final_state, reason,
        previous_state=original_state,
        decision=decision,
        recovered=True,
    )
