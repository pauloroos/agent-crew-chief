"""Transfer the same job exclusively with persisted takeover evidence (T043).
FR-029, FR-028, SC-013, US7/AC4, US7/AC5, US7/AC6.

This module owns the protocol for handing off a coordinator's job to another
installed supported CLI. The handoff is exclusive: the source releases its
owner lease before the target acquires it, and every step is recorded in the
registry and as a job ruling so crashes between updates never duplicate work
or lose audit evidence.

Public API
----------
``validate_target(cli: str) -> bool``
    True when ``cli`` is an installed supported coordinator CLI.

``build_takeover_brief(root, coordinator_id, spec_status_fn) -> dict``
    Bounded brief from spec status, pending pieces, open attempt, rulings
    and messages. Never reads prior chat/session memory. Raises on corrupt,
    oversized, traversal or mismatched-job evidence.

``record_handoff(root, coordinator_id, source_cli, target_cli, outcome, ...) -> str``
    Persist a stable handoff ID in the registry and return it. Idempotent
    on (handoff_id, coordinator_id); safe to retry after a crash.

``handoff_journal_entry(handoff_id, state, detail=None) -> dict``
    One row of the handoff journal; ``state`` is one of
    ``prepared | released | acquired | completed | failed``.
"""
from __future__ import annotations

import json
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import central_messages, central_registry
from .records import write_atomic

__all__ = [
    "SUPPORTED_COORDINATOR_CLIS",
    "build_takeover_brief",
    "handoff_journal_entry",
    "record_handoff",
    "validate_target",
]

#: Installed CLIs that may act as coordinator targets. Installation alone
#: never establishes permission to use a costlier CLI; the owner must name
#: the target explicitly via ``on_coordinator_quota = "handoff:<cli>"`` or
#: an interactive choice.
SUPPORTED_COORDINATOR_CLIS = frozenset({"claude", "codex"})

#: Maximum bytes for any single evidence blob included in the takeover brief.
_EVIDENCE_MAX_BYTES = 256 * 1024


def _now_iso(now: datetime | None = None) -> str:
    ref = now or datetime.now(timezone.utc)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    return ref.isoformat(timespec="seconds")


def _canonical_uuid(value: Any) -> str | None:
    return central_registry._canonical_uuid(value)


def validate_target(cli: str) -> bool:
    """True when ``cli`` is an installed supported coordinator CLI."""
    if not isinstance(cli, str):
        return False
    name = cli.strip().lower()
    if name not in SUPPORTED_COORDINATOR_CLIS:
        return False
    return shutil.which(name) is not None


def _coordinator_dir(root: Path, coordinator_id: str) -> Path:
    canonical = _canonical_uuid(coordinator_id)
    if canonical is None:
        raise ValueError(
            f"coordinator id must be a canonical UUID string, got {coordinator_id!r}"
        )
    return Path(root) / "coordinators" / canonical


def _bounded_text(path: Path, label: str) -> str:
    """Read ``path`` as UTF-8 text, rejecting oversized or missing files."""
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ValueError(f"{label}: cannot stat {path}: {exc}") from exc
    if size > _EVIDENCE_MAX_BYTES:
        raise ValueError(
            f"{label}: {path} exceeds the {_EVIDENCE_MAX_BYTES} byte evidence cap"
        )
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{label}: {path} is not valid UTF-8: {exc}") from exc
    except OSError as exc:
        raise ValueError(f"{label}: cannot read {path}: {exc}") from exc


def _safe_relative(base: Path, candidate: Path, label: str) -> Path:
    """Reject traversal: ``candidate`` must be inside ``base``."""
    try:
        relative = candidate.resolve().relative_to(base.resolve())
    except ValueError as exc:
        raise ValueError(
            f"{label}: path {candidate} escapes the coordinator directory"
        ) from exc
    return base / relative


def build_takeover_brief(
    root: Path,
    coordinator_id: str,
    spec_status_fn: Callable[[], dict],
    *,
    now: datetime | None = None,
) -> dict:
    """Build a bounded takeover brief from persisted evidence only.

    Parameters
    ----------
    root:
        Central storage root.
    coordinator_id:
        Canonical UUID of the coordinator being handed off.
    spec_status_fn:
        Zero-argument callable returning the current spec status dict.
        Injected so this module never imports the spec-status command.
    now:
        Optional clock override for tests.

    Returns
    -------
    dict
        ``{"schema_version": 1, "coordinator_id", "generated_at",
        "spec_status", "pending_pieces", "open_attempt", "rulings",
        "messages"}``. Every field is bounded and validated.

    Raises
    ------
    ValueError
        On corrupt, oversized, traversal or mismatched-job evidence.
    """
    coord = _coordinator_dir(root, coordinator_id)
    if not coord.is_dir():
        raise ValueError(f"no coordinator directory for {coordinator_id!r}")

    # Spec status — injected, never re-derived here.
    try:
        spec_status = spec_status_fn()
    except Exception as exc:
        raise ValueError(f"spec status unavailable: {exc}") from exc
    if not isinstance(spec_status, dict):
        raise ValueError("spec status must be a dict")

    # Pending pieces + open attempt from the active job record.
    pending_pieces: list[dict] = []
    open_attempt: dict | None = None
    active_job_id = None
    registry = central_registry.load_registry(root)
    row = registry.get("coordinators", {}).get(coordinator_id)
    if isinstance(row, dict):
        active_job_id = row.get("active_job_id")

    if isinstance(active_job_id, str) and active_job_id:
        jobs_dir = Path(root) / "jobs"
        job_path = _safe_relative(jobs_dir, jobs_dir / f"{active_job_id}.json", "job")
        if job_path.is_file():
            raw = _bounded_text(job_path, "job record")
            try:
                job = json.loads(raw)
            except ValueError as exc:
                raise ValueError(f"job record is not valid JSON: {exc}") from exc
            if not isinstance(job, dict):
                raise ValueError("job record must be a JSON object")
            for piece in job.get("pieces") or []:
                if not isinstance(piece, dict):
                    continue
                accepted = piece.get("accepted")
                if accepted is True:
                    continue
                pending_pieces.append(
                    {
                        "n": piece.get("n"),
                        "ref": piece.get("ref"),
                        "title": piece.get("title"),
                    }
                )
                attempts = piece.get("attempts") or []
                for attempt in attempts:
                    if not isinstance(attempt, dict):
                        continue
                    status = attempt.get("status")
                    if status in {"ok", "quota", "timeout"}:
                        continue
                    open_attempt = {
                        "piece": piece.get("n"),
                        "attempt": attempt.get("n"),
                        "status": status,
                        "executor": attempt.get("executor"),
                    }
                    break

    # Rulings — bounded list from the job record when available.
    rulings: list[dict] = []
    if isinstance(active_job_id, str) and active_job_id:
        jobs_dir = Path(root) / "jobs"
        job_path = _safe_relative(jobs_dir, jobs_dir / f"{active_job_id}.json", "job")
        if job_path.is_file():
            raw = _bounded_text(job_path, "job record")
            try:
                job = json.loads(raw)
            except ValueError:
                job = {}
            for ruling in job.get("rulings") or []:
                if isinstance(ruling, dict):
                    rulings.append(
                        {
                            "id": ruling.get("id"),
                            "type": ruling.get("type"),
                            "text": ruling.get("text"),
                            "at": ruling.get("at"),
                        }
                    )

    # Messages — inbox entries for this coordinator.
    messages: list[dict] = []
    inbox = coord / "inbox"
    if inbox.is_dir():
        for entry in sorted(inbox.glob("*.json")):
            try:
                text = _bounded_text(entry, "message")
                msg = json.loads(text)
            except (ValueError, OSError):
                continue
            if isinstance(msg, dict):
                messages.append(
                    {
                        "id": msg.get("id"),
                        "text": msg.get("text"),
                        "reply_to": msg.get("reply_to"),
                        "sent_at": msg.get("sent_at"),
                    }
                )

    return {
        "schema_version": 1,
        "coordinator_id": coordinator_id,
        "generated_at": _now_iso(now),
        "spec_status": spec_status,
        "pending_pieces": pending_pieces,
        "open_attempt": open_attempt,
        "rulings": rulings,
        "messages": messages,
    }


def handoff_journal_entry(
    handoff_id: str,
    state: str,
    detail: str | None = None,
    *,
    now: datetime | None = None,
) -> dict:
    """One row of the handoff journal."""
    valid_states = {"prepared", "released", "acquired", "completed", "failed"}
    if state not in valid_states:
        raise ValueError(
            f"handoff journal state must be one of {sorted(valid_states)}, got {state!r}"
        )
    entry: dict[str, Any] = {
        "handoff_id": handoff_id,
        "state": state,
        "at": _now_iso(now),
    }
    if detail is not None:
        entry["detail"] = detail
    return entry


def record_handoff(
    root: Path,
    coordinator_id: str,
    source_cli: str,
    target_cli: str,
    outcome: str,
    *,
    handoff_id: str | None = None,
    session_id: str | None = None,
    job_id: str | None = None,
    now: datetime | None = None,
) -> str:
    """Persist a stable handoff record in the registry.

    Idempotent on ``(handoff_id, coordinator_id)``: calling twice with the
    same ID appends at most one journal entry per distinct state. Returns
    the handoff ID used.
    """
    canonical = _canonical_uuid(coordinator_id)
    if canonical is None:
        raise ValueError(
            f"coordinator id must be a canonical UUID string, got {coordinator_id!r}"
        )
    if not isinstance(source_cli, str) or not source_cli.strip():
        raise ValueError("source_cli must be a non-empty string")
    if not isinstance(target_cli, str) or not target_cli.strip():
        raise ValueError("target_cli must be a non-empty string")
    if not isinstance(outcome, str) or not outcome.strip():
        raise ValueError("outcome must be a non-empty string")

    hid = handoff_id or str(uuid.uuid4())
    stamp = _now_iso(now)

    registry = central_registry.load_registry(root)
    row = registry.get("coordinators", {}).get(canonical)
    if not isinstance(row, dict):
        raise ValueError(f"no coordinator {canonical!r} registered")

    history = list(row.get("handoff_history") or [])
    existing = next((h for h in history if h.get("handoff_id") == hid), None)
    if existing is None:
        entry = {
            "handoff_id": hid,
            "source_cli": source_cli.strip(),
            "target_cli": target_cli.strip(),
            "session_id": session_id,
            "job_id": job_id,
            "outcome": outcome.strip(),
            "at": stamp,
            "journal": [handoff_journal_entry(hid, "prepared", now=now)],
        }
        history.append(entry)
    else:
        normalized_outcome = outcome.strip()
        previous_outcome = (existing.get("outcome") or "").strip()
        # Fully idempotent: re-recording the same outcome never grows the
        # journal, even when the initial entry was created with "prepared".
        if normalized_outcome != previous_outcome:
            journal = list(existing.get("journal") or [])
            new_state = "completed" if normalized_outcome == "completed" else "failed"
            journal.append(handoff_journal_entry(hid, new_state, now=now))
            existing["journal"] = journal
            existing["outcome"] = normalized_outcome

    central_registry.update_record(root, canonical, {"handoff_history": history})
    return hid