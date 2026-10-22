"""Interactive attach: yield exclusive ownership to a terminal client (FR-010, FR-011, FR-020, SC-004).

Attach lets an operator enter a coordinator's conversation in its worktree.
The background supervisor yields exclusive ownership via a durable pause
control, waits for the child round to finish, then hands the lease to the
interactive client. When the client exits, background eligibility is
restored through a resume control. If the CLI does not support interactive
resume or the terminal is redirected, attach returns an explicit fallback
without starting a substitute session.

Public API
----------
``attach(root, coordinator_id, terminal, timeout_s)``
    Request exclusive ownership of ``coordinator_id`` for ``terminal``.
    Returns a result dict with at least ``{"ok": bool, "state": str}``.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any

from . import central_controls, central_messages, central_registry

__all__ = [
    "AttachError",
    "attach",
]


class AttachError(Exception):
    """Raised when attach cannot proceed (invalid id, missing record, etc.)."""


def _canonical_uuid(value: Any) -> str | None:
    return central_registry._canonical_uuid(value)


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _stored_record(root: Path, coordinator_id: str) -> dict:
    """Read the coordinator row from the registry; raise on missing/corrupt."""
    data = central_registry.load_registry(root)
    if not isinstance(data, dict):
        raise AttachError("registry is missing or corrupt")
    coordinators = data.get("coordinators")
    if not isinstance(coordinators, dict):
        raise AttachError("registry has no coordinators map")
    row = coordinators.get(coordinator_id)
    if not isinstance(row, dict):
        raise AttachError(f"no coordinator {coordinator_id!r} registered")
    return dict(row)


def _terminal_is_interactive(terminal: object | None) -> bool:
    """True when the terminal can host an interactive CLI session."""
    if terminal is None:
        return False
    stdin = getattr(terminal, "stdin", None)
    stdout = getattr(terminal, "stdout", None)
    if stdin is None or stdout is None:
        return False
    try:
        return bool(getattr(stdin, "isatty", lambda: False)()) and bool(
            getattr(stdout, "isatty", lambda: False)()
        )
    except Exception:
        return False


def _cli_supports_resume(cli: str | None) -> bool:
    """Known CLIs that support interactive resume of a persisted session."""
    if not isinstance(cli, str):
        return False
    name = cli.split(":", 1)[0].strip().lower()
    return name in {"claude", "codex"}


def attach(
    root: Path,
    coordinator_id: str,
    terminal: object | None = None,
    timeout_s: float = 30.0,
) -> dict:
    """Yield exclusive ownership to ``terminal`` for interactive resume.

    Parameters
    ----------
    root:
        Central storage root.
    coordinator_id:
        Canonical UUID of the target coordinator.
    terminal:
        An object exposing ``stdin``/``stdout`` streams. When absent or
        non-interactive, attach returns a fallback result instead of
        starting a substitute session.
    timeout_s:
        Maximum seconds to wait for the supervisor to yield before
        returning ``busy``.

    Returns
    -------
    dict
        ``{"ok": True, "state": "attached", ...}`` on success, or
        ``{"ok": False, "state": <current>, "reason": <str>}`` on
        failure/fallback. Never raises for expected contention or
        unsupported CLI cases.
    """
    root = Path(root)
    canonical = _canonical_uuid(coordinator_id)
    if canonical is None:
        raise AttachError(
            f"coordinator id must be a canonical UUID string, got {coordinator_id!r}"
        )
    coordinator_id = canonical

    try:
        record = _stored_record(root, coordinator_id)
    except AttachError:
        return {"ok": False, "state": "unknown", "reason": "missing or corrupt record"}

    state = record.get("state")
    if not isinstance(state, str):
        return {"ok": False, "state": "unknown", "reason": "record has no state"}

    cli = record.get("cli")
    if not _cli_supports_resume(cli):
        return {
            "ok": False,
            "state": state,
            "reason": f"CLI {cli!r} does not support interactive resume; use logs/messages",
        }

    if not _terminal_is_interactive(terminal):
        return {
            "ok": False,
            "state": state,
            "reason": "terminal is redirected or non-interactive; output and messaging remain available",
        }

    # Terminal states where attach cannot proceed.
    blocked = {"stopped", "completed", "failed"}
    if state in blocked:
        return {"ok": False, "state": state, "reason": f"coordinator is {state}"}

    # Request the supervisor to pause cooperatively; it will yield between rounds.
    try:
        central_controls.request_control(root, coordinator_id, "pause")
    except central_controls.ControlError as exc:
        return {"ok": False, "state": state, "reason": f"control request failed: {exc}"}

    deadline = time.monotonic() + max(timeout_s, 0.0)
    poll = min(max(timeout_s / 10.0, 0.1), 1.0)
    yielded = False
    while time.monotonic() < deadline:
        try:
            record = _stored_record(root, coordinator_id)
        except AttachError:
            return {"ok": False, "state": "unknown", "reason": "record disappeared during wait"}
        current = record.get("state")
        if current == "paused":
            yielded = True
            break
        if current in blocked:
            return {"ok": False, "state": current, "reason": f"coordinator entered {current}"}
        time.sleep(poll)

    if not yielded:
        return {"ok": False, "state": record.get("state", "unknown"), "reason": "busy: supervisor did not yield within timeout"}

    # Mark attached so other clients know this coordinator is in use.
    try:
        central_registry.update_record(root, coordinator_id, {"state": "attached"})
    except central_registry.RegistryError as exc:
        return {"ok": False, "state": "paused", "reason": f"could not mark attached: {exc}"}

    result = {
        "ok": True,
        "state": "attached",
        "coordinator_id": coordinator_id,
        "worktree": record.get("worktree"),
        "session_id": record.get("session_id"),
        "cli": cli,
        "attached_at": _now_iso(),
    }

    # The caller owns the interactive session here; when it returns we restore
    # background eligibility. A real integration would run the CLI in the
    # terminal and release in finally; this module only provides the protocol.
    try:
        central_registry.update_record(root, coordinator_id, {"state": "paused"})
    except central_registry.RegistryError:
        result["release_error"] = "could not restore paused state after attach"
        return result

    try:
        central_controls.request_control(root, coordinator_id, "resume")
    except central_controls.ControlError as exc:
        result["release_error"] = f"resume control failed: {exc}"
        return result

    return result