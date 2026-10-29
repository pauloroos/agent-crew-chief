"""Read-only shared snapshot over registry metadata and live worktree jobs.

This module owns the single snapshot object consumed by the terminal screen
and ``central --once --json``. It never writes derived data back to disk:
registry and job records are read, aggregated in memory and returned as one
deterministic dict.

Strip task states: accepted | waiting | running | not_started | unavailable.
"""

from __future__ import annotations

import json
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from . import central_messages, central_metrics, central_registry, central_usage, records
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
    executors_value = job.get("executors") if isinstance(job, dict) else None
    if not isinstance(executors_value, str) or not executors_value.strip():
        executors_value = "free_first"
    return {
        "id": job.get("id"),
        "title": job.get("title"),
        "state": job.get("state"),
        "start": _first(job, ("started_at", "started", "created_at", "created")),
        "end": _first(job, ("finished_at", "ended_at", "completed_at", "finished", "end")),
        "executors": executors_value,
    }


def _is_finished_review(piece: dict[str, Any], coordinator_ended: bool) -> bool:
    if not isinstance(piece, dict):
        return False
    # Only actual auxiliary review pieces may disappear: a spec task keeps its
    # ref and a code/spec work type, and stays visible until accepted.
    work_type = piece.get("type")
    if work_type is None:
        work_type = piece.get("work_type")
    if work_type != "review":
        return False
    ref = piece.get("ref")
    if isinstance(ref, str) and ref.strip():
        return False
    attempts = piece.get("attempts")
    if not isinstance(attempts, list) or not attempts:
        return False
    last = attempts[-1]
    if not isinstance(last, dict):
        return False
    if last.get("role") != "review":
        return False
    if last.get("status") != "ok":
        return False
    if coordinator_ended:
        return True
    try:
        grades = records.piece_grades(piece)
    except Exception:
        grades = {}
    if grades:
        return True
    for key in ("grade", "score", "verdict"):
        if last.get(key) is not None:
            return True
    return False


def _active_piece(
    piece: dict[str, Any], now_dt: datetime, coordinator_ended: bool = False
) -> dict[str, Any] | None:
    if not isinstance(piece, dict):
        return None
    # records.mark_accepted stores an object; legacy records store True.
    if piece.get("accepted"):
        return None
    if _is_finished_review(piece, coordinator_ended):
        return None
    work_type = piece.get("type")
    if work_type is None:
        work_type = piece.get("work_type")
    ref = piece.get("ref")
    if not isinstance(ref, str) or not ref.strip():
        n_value = _normalize_int_or_none(piece.get("n"))
        ref = f"#{n_value}" if n_value is not None else None
    status = piece.get("status")
    if status is None:
        # Derive running activity from the existing strip lifecycle so a
        # recorded running attempt or a reserved next attempt reports running
        # even without piece.status; no parallel state machine.
        strip_state = _piece_strip_state(piece, [], None)
        if strip_state == "running":
            status = "running"
        else:
            attempts = piece.get("attempts")
            if not isinstance(attempts, list):
                attempts = []
            finished = [
                a for a in attempts
                if isinstance(a, dict) and a.get("status") is not None and a.get("status") != "running"
            ]
            if finished:
                status = "not_started"
            else:
                status = "pending"
    attempt_n = None
    attempts = piece.get("attempts")
    if isinstance(attempts, list) and attempts:
        last_attempt = attempts[-1]
        if isinstance(last_attempt, dict):
            attempt_n = _normalize_int_or_none(last_attempt.get("n"))
    return {
        "ref": ref,
        "title": piece.get("title"),
        "work_type": work_type,
        "executor": _lead_executor(piece),
        "status": status,
        "elapsed_s": _piece_elapsed(piece, now_dt),
        "attempt": attempt_n,
    }


def _job_sort_key(job: Any) -> tuple:
    job = job if isinstance(job, dict) else {}
    created = job.get("created")
    if created is None:
        created = job.get("created_at")
    parsed = _parse_iso(created)
    if parsed is None:
        parsed = datetime.min.replace(tzinfo=timezone.utc)
    job_id = job.get("id")
    return (parsed, str(job_id) if job_id is not None else "")


def _task_sort_key(piece: Any) -> tuple:
    if not isinstance(piece, dict):
        return (2, 0, "")
    n = piece.get("n")
    if isinstance(n, int) and not isinstance(n, bool):
        return (0, n, "")
    ref = piece.get("ref")
    ref_key = ref if isinstance(ref, str) else ""
    return (1, 0, ref_key)


def _normalize_int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


_FAILED_ATTEMPT_STATUSES = {"failed", "proof_failed", "timeout", "rejected", "error"}


def _piece_failed_attempt(piece: dict[str, Any]) -> bool:
    if not isinstance(piece, dict):
        return False
    attempts = piece.get("attempts")
    if not isinstance(attempts, list):
        return False
    for attempt in attempts:
        if not isinstance(attempt, dict):
            continue
        role = attempt.get("role")
        if role not in (None, "exec"):
            continue
        status = attempt.get("status")
        if status in _FAILED_ATTEMPT_STATUSES:
            return True
    return False


def _piece_strip_state(piece: dict[str, Any], coord_questions: list[dict[str, Any]], ref: str | None) -> str:
    if not isinstance(piece, dict):
        return "not_started"
    accepted = piece.get("accepted")
    if accepted:
        return "accepted"
    # waiting: an open question of this coordinator lists the piece's ref
    if ref is not None:
        for question in coord_questions:
            if not isinstance(question, dict):
                continue
            task_refs = question.get("task_refs")
            if isinstance(task_refs, list) and ref in task_refs:
                return "waiting"
    attempts = piece.get("attempts")
    if not isinstance(attempts, list):
        attempts = []
    # recorded running attempt
    for attempt in attempts:
        if isinstance(attempt, dict) and attempt.get("status") == "running":
            return "running"
    # reserved but unrecorded attempt number
    highest = 0
    for attempt in attempts:
        if not isinstance(attempt, dict):
            continue
        value = attempt.get("n")
        if isinstance(value, int) and not isinstance(value, bool):
            if value > highest:
                highest = value
        elif isinstance(value, str) and value.isdigit():
            ivalue = int(value)
            if ivalue > highest:
                highest = ivalue
    next_attempt = piece.get("next_attempt")
    next_int = None
    if isinstance(next_attempt, bool):
        next_int = None
    elif isinstance(next_attempt, int):
        next_int = next_attempt
    elif isinstance(next_attempt, str) and next_attempt.isdigit():
        next_int = int(next_attempt)
    if next_int is not None and next_int - 1 > highest:
        return "running"
    # latest exec attempt ok but not accepted yet (awaiting review/accept)
    exec_attempts = [
        attempt for attempt in attempts
        if isinstance(attempt, dict) and attempt.get("role") in (None, "exec")
    ]
    if exec_attempts and exec_attempts[-1].get("status") == "ok":
        return "running"
    return "not_started"


def _build_job_piece_strip(job: Any, coord_id: Any, questions: list[dict[str, Any]] | None) -> dict[str, Any]:
    job = job if isinstance(job, dict) else {}
    job_id = job.get("id")
    if not isinstance(job_id, str):
        job_id = str(job_id) if job_id is not None else None
    coord_questions = []
    if isinstance(questions, list):
        for q in questions:
            if isinstance(q, dict) and str(q.get("coordinator_id")) == str(coord_id):
                coord_questions.append(q)
    pieces = job.get("pieces")
    if not isinstance(pieces, list):
        pieces = []
    valid_pieces = [p for p in pieces if isinstance(p, dict) and not p.get("stale")]
    ordered = sorted(valid_pieces, key=_task_sort_key)
    truncated = False
    if len(ordered) > 256:
        ordered = ordered[:256]
        truncated = True
    tasks: list[dict[str, Any]] = []
    for piece in ordered:
        n = _normalize_int_or_none(piece.get("n"))
        ref = piece.get("ref")
        if not isinstance(ref, str):
            ref = None
        state = _piece_strip_state(piece, coord_questions, ref)
        failed = _piece_failed_attempt(piece)
        tasks.append({"n": n, "ref": ref, "state": state, "failed_attempt": failed})
    return {"job_id": job_id, "tasks": tasks, "truncated": truncated}


def _build_events(valid_jobs: Any) -> tuple[list[dict[str, Any]], bool]:
    collected: list[tuple] = []
    jobs = valid_jobs if isinstance(valid_jobs, list) else []
    for job_index, job in enumerate(sorted(jobs, key=_job_sort_key)):
        if not isinstance(job, dict):
            continue
        job_id_raw = job.get("id")
        job_id = str(job_id_raw) if job_id_raw is not None else None
        pieces = job.get("pieces")
        if not isinstance(pieces, list):
            pieces = []
        valid_pieces = [p for p in pieces if isinstance(p, dict) and not p.get("stale")]
        ordered = sorted(valid_pieces, key=_task_sort_key)
        for piece in ordered:
            piece_n = _normalize_int_or_none(piece.get("n"))
            ref_raw = piece.get("ref")
            ref = ref_raw if isinstance(ref_raw, str) else None
            # accepted piece event
            accepted = piece.get("accepted")
            if isinstance(accepted, dict):
                accepted_at_raw = accepted.get("at")
                if not isinstance(accepted_at_raw, str):
                    accepted_at_raw = None
                at_dt = _parse_iso(accepted_at_raw) if accepted_at_raw is not None else None
                if at_dt is not None:
                    event = {
                        "at": at_dt.astimezone(timezone.utc).isoformat(),
                        "kind": "accepted",
                        "job_id": job_id,
                        "n": piece_n,
                        "ref": ref,
                    }
                    collected.append(
                        (at_dt, job_index, piece_n is None, piece_n or 0, -1, event)
                    )
            # failed exec attempt events
            attempts = piece.get("attempts")
            if isinstance(attempts, list):
                for attempt in attempts:
                    if not isinstance(attempt, dict):
                        continue
                    role = attempt.get("role")
                    if role not in (None, "exec"):
                        continue
                    status = attempt.get("status")
                    if status not in _FAILED_ATTEMPT_STATUSES:
                        continue
                    started_raw = _first(attempt, ("started", "started_at"))
                    if not isinstance(started_raw, str):
                        started_dt = None
                    else:
                        started_dt = _parse_iso(started_raw)
                    if started_dt is None:
                        continue
                    dur = attempt.get("duration_s")
                    duration_seconds = None
                    if isinstance(dur, bool):
                        duration_seconds = None
                    elif isinstance(dur, (int, float)):
                        duration_seconds = float(dur)
                    if duration_seconds is not None:
                        try:
                            at_dt = started_dt + timedelta(seconds=duration_seconds)
                        except (OverflowError, ValueError):
                            at_dt = started_dt
                    else:
                        at_dt = started_dt
                    attempt_n = _normalize_int_or_none(attempt.get("n"))
                    event = {
                        "at": at_dt.astimezone(timezone.utc).isoformat(),
                        "kind": "attempt_failed",
                        "job_id": job_id,
                        "n": piece_n,
                        "ref": ref,
                        "attempt": attempt_n,
                    }
                    sort_attempt = attempt_n if attempt_n is not None else 0
                    collected.append(
                        (at_dt, job_index, piece_n is None, piece_n or 0, sort_attempt, event)
                    )
    collected.sort(key=lambda item: (item[0], item[1], item[2], item[3], item[4]))
    events = [item[5] for item in collected]
    truncated = len(events) > 200
    if truncated:
        events = events[-200:]
    return events, truncated


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


def _unavailable_row(record: dict[str, Any], now_dt: datetime, error: str, worktree_available: bool = False, pending_events: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    coord_id = record.get("id")
    repo = record.get("repo")
    if not isinstance(repo, str) or not repo:
        repo = record.get("worktree")
    repo_label = Path(repo).name if isinstance(repo, str) and repo else ""
    metrics_result = central_metrics.aggregate([], pending_events or [], None, now_dt)
    continuity = record.get("continuity")
    if not isinstance(continuity, dict):
        continuity = None
    launched_at = record.get("created_at")
    if not isinstance(launched_at, str):
        launched_at = None
    active_job_id = record.get("active_job_id")
    if not isinstance(active_job_id, str):
        active_job_id = None
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
        "spec_tasks_accepted": 0,
        "spec_tasks_total": 0,
        "active_pieces": [],
        "jobs": [],
        "metrics": metrics_result.get("metrics", {}),
        "models": metrics_result.get("models", []),
        "scoreboard": None,
        "scoreboard_rows": [],
        "last_error": error if error else record.get("last_error"),
        "continuity": continuity,
        "launched_at": launched_at,
        "active_job_id": active_job_id,
        "job_piece_strips": [],
        "events": [],
        "events_truncated": False,
        "executors": record.get("executors") if isinstance(record.get("executors"), str) and record.get("executors").strip() else "free_first",
    }


def _pending_for_snapshot(root: Path, record: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Lock-free pending round usage plus read diagnostics, never stale."""
    coord_id = record.get("id") if isinstance(record, dict) else None
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            events = central_usage.read_pending_usage(root, record)
    except Exception as exc:
        return [], [{"record": str(coord_id), "message": str(exc)}]
    diagnostics = [
        {"record": str(coord_id), "message": str(item.message)} for item in caught
    ]
    return events, diagnostics


def _build_row(
    root: Path,
    record: dict[str, Any],
    now_dt: datetime,
    previous_row: dict[str, Any] | None = None,
    questions: list[dict[str, Any]] | None = None,
    pending_sink: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    diagnostics: list[dict[str, Any]] = []
    if questions is None:
        try:
            questions = list(central_messages.questions(root))
        except Exception:
            questions = []
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

    # Pending round usage: lock-free read-only; unreadable rounds become
    # diagnostics without changing the row staleness policy.
    pending_events, pending_diagnostics = _pending_for_snapshot(root, record)
    # Pending usage must reach global aggregation even on early returns.
    if pending_sink is not None:
        pending_sink.extend(pending_events)

    # Missing worktree: row-level failure.
    if not worktree_available:
        error = f"missing worktree: {worktree_value!r}"
        diagnostics.append({"record": str(coord_id), "message": error})
        diagnostics.extend(pending_diagnostics)
        if isinstance(previous_row, dict):
            stale = dict(previous_row)
            stale["stale"] = True
            stale["worktree_available"] = False
            if stale.get("last_error") is None:
                stale["last_error"] = error
            return stale, diagnostics, []
        return _unavailable_row(record, now_dt, error, pending_events=pending_events), diagnostics, []

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
        diagnostics.extend(pending_diagnostics)
        if isinstance(previous_row, dict):
            stale = dict(previous_row)
            stale["stale"] = True
            stale["worktree_available"] = worktree_available
            if stale.get("last_error") is None:
                stale["last_error"] = job_errors[0]["message"]
            return stale, diagnostics, []
        return _unavailable_row(record, now_dt, job_errors[0]["message"], worktree_available=True, pending_events=pending_events), diagnostics, []

    active_job_id = record.get("active_job_id")
    if not isinstance(active_job_id, str):
        active_job_id = None
    launched_at = record.get("created_at")
    if not isinstance(launched_at, str):
        launched_at = None
    job_rows = [_job_row(job, active_job_id) for job in sorted(valid_jobs, key=lambda j: str(j.get("id") or ""))]
    # Coordinator-level executor mode: prefer the active job's executors field,
    # else fall back to the latest job, else "free_first" for legacy records.
    coord_executors = "free_first"
    if active_job_id is not None:
        for job in valid_jobs:
            if isinstance(job, dict) and str(job.get("id")) == active_job_id:
                ce = job.get("executors")
                if isinstance(ce, str) and ce.strip():
                    coord_executors = ce
                break
    if coord_executors == "free_first" and valid_jobs:
        latest = sorted(valid_jobs, key=_job_sort_key)[-1]
        if isinstance(latest, dict):
            ce = latest.get("executors")
            if isinstance(ce, str) and ce.strip():
                coord_executors = ce
    coordinator_ended = str(record.get("state") or "").lower() in ("completed", "failed", "stopped", "done")
    active_pieces = []
    for job in valid_jobs:
        for piece in job.get("pieces") or []:
            if not isinstance(piece, dict):
                continue
            row_piece = _active_piece(piece, now_dt, coordinator_ended)
            if row_piece is not None:
                active_pieces.append(row_piece)
    spec_tasks_total = 0
    spec_tasks_accepted = 0
    for job in valid_jobs:
        pieces = [
            piece for piece in job.get("pieces") or []
            if isinstance(piece, dict) and not piece.get("stale")
        ]
        has_spec_ref = any(
            isinstance(piece.get("ref"), str) and piece.get("ref").strip()
            for piece in pieces
        )
        for piece in pieces:
            ref_value = piece.get("ref")
            if isinstance(ref_value, str) and ref_value.strip():
                spec_tasks_total += 1
                if piece.get("accepted"):
                    spec_tasks_accepted += 1
            elif not has_spec_ref:
                # Small job with no spec refs: count actionable non-review
                # work (e.g. a ref-less type=code piece), never aux reviews.
                work_type = piece.get("type")
                if work_type is None:
                    work_type = piece.get("work_type")
                if work_type != "review":
                    spec_tasks_total += 1
                    if piece.get("accepted"):
                        spec_tasks_accepted += 1

    try:
        metrics_result = central_metrics.aggregate(valid_jobs, pending_events, None, now_dt)
    except Exception as exc:  # pragma: no cover - defensive
        metrics_result = central_metrics.aggregate([], pending_events, None, now_dt)
        diagnostics.append({"record": str(coord_id), "message": str(exc)})
    diagnostics.extend(pending_diagnostics)
    scoreboard, scoreboard_rows = None, []
    try:
        from . import scoring

        scoreboard = scoring.render_scoreboard(worktree_path, now=now_dt)
        scoreboard_rows = scoring.scoreboard_rows(worktree_path, now=now_dt)
    except Exception:
        scoreboard, scoreboard_rows = None, []

    continuity = record.get("continuity")
    if not isinstance(continuity, dict):
        continuity = None

    job_piece_strips = [
        _build_job_piece_strip(job, coord_id, questions)
        for job in sorted(valid_jobs, key=_job_sort_key)
    ]
    if len(job_piece_strips) > 32:
        job_piece_strips = job_piece_strips[:32]

    events, events_truncated = _build_events(valid_jobs)

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
        "spec_tasks_accepted": spec_tasks_accepted,
        "spec_tasks_total": spec_tasks_total,
        "active_pieces": active_pieces,
        "jobs": job_rows,
        "metrics": metrics_result.get("metrics", {}),
        "models": metrics_result.get("models", []),
        "scoreboard": scoreboard,
        "scoreboard_rows": scoreboard_rows,
        "last_error": record.get("last_error"),
        "continuity": continuity,
        "launched_at": launched_at,
        "active_job_id": active_job_id,
        "job_piece_strips": job_piece_strips,
        "events": events,
        "events_truncated": events_truncated,
        "executors": coord_executors,
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

    # Questions (open, correlated) across coordinators.
    questions: list[dict[str, Any]] = []
    try:
        questions = list(central_messages.questions(root))
    except Exception as exc:
        diagnostics.append({"record": "<questions>", "message": str(exc)})

    coordinators_map = registry.get("coordinators")
    all_pending: list[dict[str, Any]] = []
    if isinstance(coordinators_map, dict):
        for coord_id in sorted(coordinators_map):
            record = coordinators_map[coord_id]
            if not isinstance(record, dict):
                diagnostics.append({"record": str(coord_id), "message": "coordinator record is not an object"})
                continue
            row, row_diagnostics, valid_jobs = _build_row(
                root, record, now_dt, previous_map.get(str(coord_id)), questions,
                pending_sink=all_pending,
            )
            coordinators.append(row)
            all_jobs.extend(valid_jobs)
            diagnostics.extend(row_diagnostics)

    # Totals: aggregate every collected live job (dedup happens in aggregate).
    try:
        aggregate_result = central_metrics.aggregate(all_jobs, all_pending, None, now_dt)
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
