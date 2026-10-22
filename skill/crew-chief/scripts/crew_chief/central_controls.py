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

``registered_backend(root, coordinator_id)``
    The stored row of one coordinator and the backend it was launched with,
    so every command routes by the record, never by current preferences.

The supervisor (:mod:`central_supervisor`) reads these files between rounds;
``stop`` is cooperative and never signals a process.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import central_launch, central_messages, central_registry, records

__all__ = [
    "ControlError",
    "registered_backend",
    "request_control",
]

#: Commands accepted by :func:`request_control`.
VALID_COMMANDS = frozenset({"pause", "resume", "stop"})


class ControlError(Exception):
    """Raised when a control request or application is invalid."""


def _canonical_uuid(value: Any) -> str | None:
    """Reuse the registry canonical UUID validator."""
    return central_registry._canonical_uuid(value)


def _control_dir(root: Path, coordinator_id: str, *, create: bool = False) -> Path:
    root = Path(root)
    canonical = _canonical_uuid(coordinator_id)
    if canonical is None:
        raise ControlError(
            f"coordinator id must be a canonical UUID string, got {coordinator_id!r}"
        )
    try:
        coord = central_messages._coordinator_dir(root, coordinator_id, create=create)
    except central_messages.MessageError as exc:
        raise ControlError(str(exc)) from exc
    control = coord / "control"
    root_resolved = root.resolve(strict=False)
    if control.is_symlink():
        raise ControlError(
            f"{control}: control path escapes the central root via a symbolic link"
        )
    if not control.resolve(strict=False).is_relative_to(root_resolved):
        raise ControlError(f"{control}: control path escapes the central root")
    if create:
        control.mkdir(parents=True, exist_ok=True)
    return control


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
    control_dir = _control_dir(root, coordinator_id, create=True)
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


def registered_backend(root: Path, coordinator_id: Any) -> tuple[dict | None, Any]:
    """The stored row of ``coordinator_id`` and the backend it was launched with.

    Returns ``(None, None)`` when no such coordinator is registered. The
    stored ``backend`` decides, never current preferences; an unreadable
    registry or an unknown or unusable backend raises :class:`ControlError`
    instead of falling back to another backend.
    """
    from .central_backend import BackendError

    canonical = _canonical_uuid(coordinator_id)
    try:
        rows = central_registry.load_registry(Path(root)).get("coordinators", {})
    except central_registry.RegistryError as exc:
        raise ControlError(str(exc)) from exc
    row = rows.get(canonical) if canonical is not None else None
    if not isinstance(row, dict):
        return None, None
    try:
        return row, central_launch.select_backend(Path(root), row.get("backend"))
    except BackendError as exc:
        raise ControlError(f"coordinator {canonical}: {exc}") from exc
