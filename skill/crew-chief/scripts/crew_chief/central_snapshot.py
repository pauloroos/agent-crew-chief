"""Read-only shared snapshot over registry metadata and live worktree jobs.

This module owns the single snapshot object consumed by the terminal, the
JSON endpoint and the web view. It never writes derived data back to disk:
registry and job records are read, aggregated in memory and returned as one
deterministic dict.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from . import central_messages, central_metrics, central_registry, records
from .records import RecordError, iter_jobs

__all__ = ["snapshot"]

_SCHEMA_VERSION = 1


def _normalize_now(now: Any) -> datetime:
    if hasattr(now, "now") and callable(getattr(now, "now")):
        try:
            now = now.now()
        except Exception:
            pass
    if isinstance(now, datetime):
        value = now
    elif isinstance(now, str):
        text = now.strip()
        try:
            value = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            value = datetime.now(timezone.utc)
    elif isinstance(now, (int, float)) and not isinstance(now, bool):
        try:
            value = datetime.fromtimestamp(float(now), tz=timezone.utc)
        except (OSError, ValueError, OverflowError):
            value = datetime.now(timezone.utc)
    else:
        value = datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def _generated_at(now: Any, now_dt: datetime) -> str:
    if isinstance(now, str) and now.strip():
        return now.strip()
    return now_dt.isoformat()


def _canonical_uuid(value: Any) -> str | None:
    import uuid

    if not isinstance(value, str):
        return None
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return None
    return str(parsed) if str(parsed) == value else None


def _first(mapping: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in mapping and mapping.get(key) is not None:
            return mapping.get(key)
    # Preserve an explicit null when none of the candidate keys exist.
    for key in keys:
        if key in mapping:
            return mapping.get(key)
    return None


def _parse_iso(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OSError, ValueError, OverflowError):
            return None
    return None


def _piece_elapsed(piece: dict[str, Any], now_dt: datetime) -> float | None:
    if not isinstance(piece, dict):
        return None
    if "elapsed_s" in piece:
        value = piece.get("elapsed_s")
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                pass
    candidates = (
        piece.get("started_at"),
        piece.get("started"),
        piece.get("created_at"),
        piece.get("created"),
    )
    start = None
    for candidate in candidates:
        parsed = _parse_iso(candidate)
        if parsed is not None:
            start = parsed
            break
    if start is None:
        attempts = piece.get("attempts")
        if isinstance(attempts, list) and attempts:
            last = attempts[-1]
            if isinstance(last, dict):
                parsed = _parse_iso(last.get("started") or last.get("started_at"))
                if parsed is not None:
                    start = parsed
    if start is None:
        return None
    delta = (now_dt - start).total_seconds()
    if delta < 0:
        delta = 0.0
    return delta


def _lead_executor(piece: dict[str, Any]) -> str:
    try:
        from .records import _lead_executor as records_lead

        return str(records_lead(piece) or "")
    except Exception:
        pass
    if not isinstance(piece, dict):
        return ""
    attempts = piece.get("attempts")
    if isinstance(attempts, list) and attempts:
        last = attempts[-1]
        if isinstance(last, dict):
            value = last.get("executor")
            if isinstance(value, str):
                return value
    value = piece.get("executor")
    return value if isinstance(value, str) else ""


def _job_row(job: dict[str, Any], active_job_id: Any) -> dict[str, Any]:
    job = job if isinstance(job, dict) else {}
    return {
        "id": job.get("id"),
        "title": job.get("title"),
        "state": job.get("state"),
        "start": _first(job, ("started_at", "started", "created_at", "created")),
        "end": _first(job, ("finished_at", "ended_at", "completed_at", "finished", "end")),
    }


def _active_piece(piece: dict[str, Any], now_dt: datetime) -> dict[str, Any] | None:
    if not isinstance(piece, dict):
        return None
    # records.mark_accepted stores an object; legacy records store True.
    if piece.get("accepted"):
        return None
    work_type = piece.get("type")
    if work_type is None:
        work_type = piece.get("work_type")
    status = piece.get("status")
    if status is None:
        status = "pending"
    return {
        "ref": piece.get("ref"),
        "title": piece.get("title"),
        "work_type": work_type,
        "executor": _lead_executor(piece),
        "status": status,
        "elapsed_s": _piece_elapsed(piece, now_dt),
    }


def _previous_rows(previous: Any) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    if isinstance(previous, list):
        for value in previous:
            if isinstance(value, dict) and "id" in value:
                result[str(value.get("id"))] = dict(value)
        return result
    if not isinstance(previous, dict):
        return result
    coordinators = previous.get("coordinators")
    if isinstance(coordinators, dict):
        for key, value in coordinators.items():
            if isinstance(value, dict):
                result[str(key)] = dict(value)
        return result
    if isinstance(coordinators, list):
        for value in coordinators:
            if isinstance(value, dict) and "id" in value:
                result[str(value.get("id"))] = dict(value)
    # Some callers may pass the coordinator list directly.
    if not result and isinstance(previous.get("rows"), list):
        for value in previous["rows"]:
            if isinstance(value, dict) and "id" in value:
                result[str(value.get("id"))] = dict(value)
    return result


def _unavailable_row(record: dict[str, Any], now_dt: datetime, error: str, worktree_available: bool = False) -> dict[str, Any]:
    coord_id = record.get("id")
    repo = record.get("repo")
    if not isinstance(repo, str) or not repo:
        repo = record.get("worktree")
    repo_label = Path(repo).name if isinstance(repo, str) and repo else ""
    metrics = central_metrics.aggregate([], [], None, now_dt)
    continuity = record.get("continuity")
    if not isinstance(continuity, dict):
        continuity = None
    return {
        "id": coord_id,
        "repo_label": repo_label,
        "cli": record.get("cli"),
        "backend": record.get("backend"),
        "state": record.get("state"),
        "worktree_available": worktree_available,
        "stale": True,
        "accepted": 0,
        "total": 0,
        "active_pieces": [],
        "jobs": [],
        "metrics": metrics.get("metrics", {}),
        "models": [],
        "scoreboard": None,
        "last_error": error if error else record.get("last_error"),
        "continuity": continuity,
    }


def _build_row(
    root: Path,
    record: dict[str, Any],
    now_dt: datetime,
    previous_row: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    diagnostics: list[dict[str, Any]] = []
    coord_id = record.get("id")

    repo = record.get("repo")
    if not isinstance(repo, str) or not repo:
        repo = record.get("worktree")
    repo_label = Path(repo).name if isinstance(repo, str) and repo else ""

    worktree_value = record.get("worktree")
    worktree_path: Path | None = None
    if isinstance(worktree_value, str) and worktree_value:
        worktree_path = Path(worktree_value)

    worktree_available = bool(
        worktree_path is not None
        and not worktree_path.is_symlink()
        and worktree_path.is_dir()
    )

    # Missing worktree: row-level failure.
    if not worktree_available:
        error = f"missing worktree: {worktree_value!r}"
        diagnostics.append({"record": str(coord_id), "message": error})
        if isinstance(previous_row, dict):
            stale = dict(previous_row)
            stale["stale"] = True
            stale["worktree_available"] = False
            if stale.get("last_error") is None:
                stale["last_error"] = error
            return stale, diagnostics, []
        return _unavailable_row(record, now_dt, error), diagnostics, []

    # Read live jobs.
    valid_jobs: list[dict[str, Any]] = []
    job_errors: list[dict[str, Any]] = []
    try:
        for job, path, error in iter_jobs(worktree_path):
            if error is not None or not isinstance(job, dict):
                message = error if error is not None else "job record is not a JSON object"
                job_errors.append({"record": str(path), "message": message})
                continue
            valid_jobs.append(job)
    except Exception as exc:  # pragma: no cover - iter_jobs is defensive
        job_errors.append({"record": str(worktree_path), "message": str(exc)})

    diagnostics.extend(job_errors)

    # A partially unreadable worktree makes the row stale/unavailable rather
    # than partially reported, matching the "last complete row" policy.
    if job_errors:
        if isinstance(previous_row, dict):
            stale = dict(previous_row)
            stale["stale"] = True
            stale["worktree_available"] = worktree_available
            if stale.get("last_error") is None:
                stale["last_error"] = job_errors[0]["message"]
            return stale, diagnostics, []
        return _unavailable_row(record, now_dt, job_errors[0]["message"], worktree_available=True), diagnostics, []

    active_job_id = record.get("active_job_id")
    job_rows = [_job_row(job, active_job_id) for job in sorted(valid_jobs, key=lambda j: str(j.get("id") or ""))]
    active_pieces = []
    for job in valid_jobs:
        for piece in job.get("pieces") or []:
            if not isinstance(piece, dict):
                continue
            row_piece = _active_piece(piece, now_dt)
            if row_piece is not None:
                active_pieces.append(row_piece)

    try:
        metrics_result = central_metrics.aggregate(valid_jobs, [], None, now_dt)
    except Exception as exc:  # pragma: no cover - defensive
        metrics_result = central_metrics.aggregate([], [], None, now_dt)
        diagnostics.append({"record": str(coord_id), "message": str(exc)})

    scoreboard = None
    try:
        from . import scoring

        scoreboard = scoring.render_scoreboard(worktree_path, now=now_dt)
    except Exception:
        scoreboard = None

    continuity = record.get("continuity")
    if not isinstance(continuity, dict):
        continuity = None

    row = {
        "id": coord_id,
        "repo_label": repo_label,
        "cli": record.get("cli"),
        "backend": record.get("backend"),
        "state": record.get("state"),
        "worktree_available": worktree_available,
        "stale": False,
        "accepted": metrics_result.get("accepted_pieces", 0),
        "total": metrics_result.get("pieces", 0),
        "active_pieces": active_pieces,
        "jobs": job_rows,
        "metrics": metrics_result.get("metrics", {}),
        "models": metrics_result.get("models", []),
        "scoreboard": scoreboard,
        "last_error": record.get("last_error"),
        "continuity": continuity,
    }
    return row, diagnostics, valid_jobs


def snapshot(root: Path, now: Any, previous: dict | None = None) -> dict[str, Any]:
    """Build the shared read-only snapshot."""
    root = Path(root)
    now_dt = _normalize_now(now)
    generated_at = _generated_at(now, now_dt)

    diagnostics: list[dict[str, Any]] = []
    coordinators: list[dict[str, Any]] = []
    all_jobs: list[dict[str, Any]] = []
    previous_map = _previous_rows(previous)

    # Registry load (bounded; structural failures are reported, not raised).
    registry: dict[str, Any] | None = None
    try:
        registry = central_registry.load_registry(root)
    except Exception as exc:
        registry = {"schema_version": _SCHEMA_VERSION, "coordinators": {}, "errors": []}
        diagnostics.append({"record": "<registry>", "message": str(exc)})

    if isinstance(registry, dict):
        for err in registry.get("errors") or []:
            if isinstance(err, dict):
                diagnostics.append(dict(err))
            else:
                diagnostics.append({"record": "<registry>", "message": str(err)})

        coordinators_map = registry.get("coordinators")
        if isinstance(coordinators_map, dict):
            for coord_id in sorted(coordinators_map):
                record = coordinators_map[coord_id]
                if not isinstance(record, dict):
                    diagnostics.append({"record": str(coord_id), "message": "coordinator record is not an object"})
                    continue
                row, row_diagnostics, valid_jobs = _build_row(
                    root, record, now_dt, previous_map.get(str(coord_id))
                )
                coordinators.append(row)
                all_jobs.extend(valid_jobs)
                diagnostics.extend(row_diagnostics)

    # Questions (open, correlated) across coordinators.
    questions: list[dict[str, Any]] = []
    try:
        questions = list(central_messages.questions(root))
    except Exception as exc:
        diagnostics.append({"record": "<questions>", "message": str(exc)})

    # Totals: aggregate every collected live job (dedup happens in aggregate).
    try:
        aggregate_result = central_metrics.aggregate(all_jobs, [], None, now_dt)
    except Exception as exc:  # pragma: no cover - defensive
        aggregate_result = central_metrics.aggregate([], [], None, now_dt)
        diagnostics.append({"record": "<totals>", "message": str(exc)})

    totals: dict[str, Any] = {
        "coordinators": len(coordinators),
        "jobs": len(all_jobs),
        "questions": len(questions),
    }
    # Merge aggregate totals (jobs count becomes unique-job count).
    for key, value in aggregate_result.items():
        totals[key] = value

    # --- continuity alerts (FR-030, US7) ------------------------------------
    alerts: list[dict[str, Any]] = []
    for row in coordinators:
        if not isinstance(row, dict):
            continue
        continuity = row.get("continuity")
        if not isinstance(continuity, dict):
            continue
        reason = continuity.get("reason")
        if reason not in ("quota", "stall"):
            continue
        stopped_at = continuity.get("stopped_at")
        quota_reset_at = continuity.get("quota_reset_at")
        policy = continuity.get("policy", "ask")
        actions = ["wait", "handoff", "stop"]
        alerts.append({
            "coordinator_id": row.get("id") or row.get("coordinator_id"),
            "reason": reason,
            "stopped_at": stopped_at,
            "quota_reset_at": quota_reset_at,
            "policy": policy,
            "actions": actions,
        })

    return {
        "schema_version": _SCHEMA_VERSION,
        "generated_at": generated_at,
        "alerts": alerts,
        "coordinators": coordinators,
        "questions": questions,
        "totals": totals,
        "diagnostics": diagnostics,
    }
