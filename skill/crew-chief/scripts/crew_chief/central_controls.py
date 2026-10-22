"""Cooperative pause, resume and stop controls (FR-007, FR-011, SC-004, US2/AC5).

Durable control files let an external requester (terminal, web view or CLI)
steer a running supervisor without racing it.  The live owner polls its
``control/`` directory between bounded rounds; commands never signal a bare
persisted PID and never reach across coordinator boundaries.

Public API
----------

``request_control(root, coordinator_id, action)``
    Write one validated control file into the coordinator's ``control/``
    directory.  Returns the canonical message id used as the filename stem.

``apply_controls(record, commands, owned_child)``
    Pure helper consumed by the supervisor between rounds.  Returns the
    updated record dict with the newest command applied; ``owned_child`` is
    only signalled when the coordinator itself owns that child tree.
"""

from __future__ import annotations

import json
import os
import signal
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import central_messages, central_registry, records

__all__ = [
    "ControlError",
    "apply_controls",
    "request_control",
]

#: Commands accepted by :func:`request_control`.
VALID_COMMANDS = frozenset({"pause", "resume", "stop"})


class ControlError(Exception):
    """Raised when a control request or application is invalid."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical_uuid(value: Any) -> str | None:
    """Reuse the registry canonical UUID validator."""
    return central_registry._canonical_uuid(value)


def _control_dir(root: Path, coordinator_id: str) -> Path:
    root = Path(root)
    canonical = _canonical_uuid(coordinator_id)
    if canonical is None:
        raise ControlError(
            f"coordinator id must be a canonical UUID string, got {coordinator_id!r}"
        )
    return root / "coordinators" / canonical / "control"


def request_control(
    root: Path,
    coordinator_id: str,
    action: str,
    *,
    now: datetime | None = None,
) -> str:
    """Persist one durable control command for the coordinator's live owner.

    Parameters
    ----------
    root:
        Central storage root.
    coordinator_id:
        Canonical UUID of the target coordinator.
    action:
        One of :data:`VALID_COMMANDS`.
    now:
        Optional timestamp override for tests.

    Returns
    -------
    str
        The canonical message id used as the control file stem.
    """
    if not isinstance(action, str) or action not in VALID_COMMANDS:
        raise ControlError(
            f"control action must be one of {sorted(VALID_COMMANDS)}, got {action!r}"
        )
    control_dir = _control_dir(root, coordinator_id)
    canonical = _canonical_uuid(coordinator_id)
    assert canonical is not None  # guarded above
    message_id = str(uuid.uuid4())
    payload = {
        "schema_version": 1,
        "id": message_id,
        "command": action,
        "coordinator_id": canonical,
        "requested_at": (now or datetime.now(timezone.utc)).isoformat(
            timespec="seconds"
        ),
    }
    path = control_dir / f"{message_id}.json"
    records.write_atomic(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return message_id


def _signal_owned_tree(child: object | None) -> bool:
    """Send SIGTERM to the owned child process tree when safe.

    Only the live owner may signal its own tracked child.  A persisted PID
    alone is diagnostics, never kill authority.  Returns True when a signal
    was delivered.
    """
    if child is None:
        return False
    pid = getattr(child, "pid", None)
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    return True


def apply_controls(
    record: dict,
    commands: list[dict],
    owned_child: object | None = None,
) -> dict:
    """Apply validated control commands to a coordinator record.

    Parameters
    ----------
    record:
        Current coordinator record dict (shallow-copied before mutation).
    commands:
        Ordered list of parsed control payloads (oldest first); the newest
        recognised command wins per the supervisor contract.
    owned_child:
        The live child process handle owned by this coordinator.  Only this
        handle may be signalled; persisted PIDs are ignored.

    Returns
    -------
    dict
        Updated record reflecting the winning command.  Unknown commands are
        skipped; repeated identical commands are idempotent.
    """
    if not isinstance(record, dict):
        raise ControlError("record must be a dict")
    if not isinstance(commands, list):
        raise ControlError("commands must be a list")
    updated = dict(record)
    winner: str | None = None
    for entry in commands:
        if not isinstance(entry, dict):
            continue
        command = entry.get("command")
        if command in VALID_COMMANDS:
            winner = command
    if winner is None:
        return updated
    state = updated.get("state")
    if winner == "pause":
        if state not in {"paused", "stopped", "completed", "failed"}:
            updated["state"] = "paused"
            updated["updated_at"] = _now_iso()
    elif winner == "resume":
        if state == "paused":
            previous = updated.get("previous_state") or "idle"
            updated["state"] = previous
            updated.pop("previous_state", None)
            updated["updated_at"] = _now_iso()
    elif winner == "stop":
        if state != "stopped":
            updated["state"] = "stopped"
            updated["updated_at"] = _now_iso()
            _signal_owned_tree(owned_child)
    return updated