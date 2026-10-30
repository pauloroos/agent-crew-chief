"""Shared canonical persisted interview service (T036, US9, US2, FR-011, FR-012, FR-036).

Provides canonical job initialization, validation, preference resolution,
persistence, and cursor-based event queries for the CLI.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
import tomllib
from typing import Any

from . import config, records
from .events import EventLog, InvalidCursorError
from .interview import (
    InterviewEngine,
    InterviewExpiredError,
    InterviewPendingError,
    check_interview_expiration,
)
from .project_resolver import ProjectResolutionError, resolve_project_path
from .records import transition_terminal_state

VALID_EXECUTOR_MODES = ("own", "subagents", "free_first")
VALID_END_MODES = ("pr", "pr_review")


def get_trusted_projects_dir(explicit: str | Path | None = None) -> Path | None:
    """Resolve trusted projects_dir from the explicit ``--projects-dir`` only.

    No preference file (global or project) can set or self-authorize it.
    """
    return Path(explicit).resolve() if explicit is not None else None


def validate_answer(
    key: str,
    value: Any,
    team_coordinators: list[str] | None = None,
    projects_dir: str | Path | None = None,
) -> Any:
    """Validate a single interview answer value.

    Raises ValueError or ProjectResolutionError with descriptive message on validation failure.
    """
    if key == "project":
        if not value or not str(value).strip():
            raise ValueError("project name or identifier is required")
        val_str = str(value).strip()
        trusted_pdir = get_trusted_projects_dir(projects_dir)
        if trusted_pdir is not None:
            cand = Path(val_str).expanduser()
            if not cand.is_absolute() and not cand.drive and not cand.root:
                if (trusted_pdir / val_str).exists():
                    cand = trusted_pdir / val_str
                elif (Path.cwd() / val_str).exists():
                    cand = Path.cwd() / val_str
                else:
                    cand = trusted_pdir / val_str
            resolved = resolve_project_path(cand, trusted_pdir)
            return resolved.name
        return val_str

    if key == "request":
        if not value or not str(value).strip():
            raise ValueError("request description is required")
        return str(value).strip()

    if key == "coordinator":
        if value is None or str(value).strip() == "":
            return None
        val_str = str(value).strip()
        if team_coordinators:
            allowed = [str(c).strip() for c in team_coordinators if str(c).strip()]
            if allowed and val_str not in allowed:
                raise ValueError(
                    f"invalid coordinator {value!r}; expected one of: {', '.join(allowed)}"
                )
        return val_str

    if key == "executors":
        if value is None:
            return None
        val_str = str(value).strip()
        if val_str not in VALID_EXECUTOR_MODES:
            choices = ", ".join(VALID_EXECUTOR_MODES)
            raise ValueError(
                f"invalid executor mode {value!r}; expected one of: {choices}"
            )
        return val_str

    if key == "end_mode":
        if value is None:
            return None
        val_str = str(value).strip()
        if val_str not in VALID_END_MODES:
            choices = ", ".join(VALID_END_MODES)
            raise ValueError(
                f"invalid end_mode {value!r}; expected one of: {choices}"
            )
        return val_str

    return value


def resolve_project_info(
    project: str | Path | None = None,
    projects_dir: str | Path | None = None,
) -> tuple[Path, str]:
    """Resolve project directory Path and project identifier name.

    When projects_dir is given explicitly,
    the resolved project directory must be a strict child of projects_dir
    enforced by resolve_project_path. Root-itself, outside absolute paths,
    path traversal, and symlink escapes are strictly rejected.

    Returns (project_dir, project_id).
    """
    trusted_pdir = get_trusted_projects_dir(projects_dir)

    if trusted_pdir is not None:
        if project:
            cand = Path(project).expanduser()
            if not cand.is_absolute() and not cand.drive and not cand.root:
                if (trusted_pdir / project).exists():
                    cand = trusted_pdir / project
                elif (Path.cwd() / project).exists():
                    cand = Path.cwd() / project
                else:
                    cand = trusted_pdir / project
            resolved = resolve_project_path(cand, trusted_pdir)
            return resolved, resolved.name

        cwd = Path.cwd().resolve()
        resolved = resolve_project_path(cwd, trusted_pdir)
        return resolved, resolved.name

    if project:
        p = Path(project).expanduser()
        if p.is_dir():
            project_dir = p.resolve()
            project_id = project_dir.name
            return project_dir, project_id
        # Might be a relative name or identifier
        cwd = Path.cwd().resolve()
        if (cwd / project).is_dir():
            project_dir = (cwd / project).resolve()
            return project_dir, project_dir.name
        return cwd, str(project)

    cwd = Path.cwd().resolve()
    return cwd, cwd.name


def load_interview_prefs(
    project_dir: Path,
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """Load project and global preferences dicts for interview skipping.

    Returns (project_prefs, global_prefs, team_coordinators).
    """
    project_prefs_file = project_dir / "docs" / "crew-chief" / "preferences.toml"
    project_data: dict[str, Any] = {}
    if project_prefs_file.is_file():
        try:
            project_data = tomllib.loads(project_prefs_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    global_data: dict[str, Any] = {}
    try:
        env_global = os.environ.get("AGENT_CREW_CHIEF_GLOBAL_PREFS") or os.environ.get(
            "CREW_CHIEF_PREFERENCES"
        )
        global_path = Path(env_global) if env_global else config.default_global_path()
        if global_path and global_path.is_file():
            global_data = tomllib.loads(global_path.read_text(encoding="utf-8"))
    except Exception:
        pass

    project_prefs: dict[str, Any] = {}
    global_prefs: dict[str, Any] = {}

    # Extract coordinator
    if "coordinator" in project_data and project_data["coordinator"]:
        project_prefs["coordinator"] = str(project_data["coordinator"]).strip()
    elif (
        isinstance(project_data.get("team"), dict)
        and project_data["team"].get("coordinators")
    ):
        coords = [c for c in project_data["team"]["coordinators"] if c]
        if coords:
            project_prefs["coordinator"] = str(coords[0]).strip()

    if "coordinator" in global_data and global_data["coordinator"]:
        global_prefs["coordinator"] = str(global_data["coordinator"]).strip()
    elif (
        isinstance(global_data.get("team"), dict)
        and global_data["team"].get("coordinators")
    ):
        coords = [c for c in global_data["team"]["coordinators"] if c]
        if coords:
            global_prefs["coordinator"] = str(coords[0]).strip()

    # Extract executors
    if "executors" in project_data and project_data["executors"]:
        project_prefs["executors"] = str(project_data["executors"]).strip()
    elif "executors_default" in project_data and project_data["executors_default"]:
        project_prefs["executors"] = str(project_data["executors_default"]).strip()

    if "executors" in global_data and global_data["executors"]:
        global_prefs["executors"] = str(global_data["executors"]).strip()
    elif "executors_default" in global_data and global_data["executors_default"]:
        global_prefs["executors"] = str(global_data["executors_default"]).strip()

    # Extract end_mode
    if "end_mode" in project_data and project_data["end_mode"]:
        project_prefs["end_mode"] = str(project_data["end_mode"]).strip()
    if "end_mode" in global_data and global_data["end_mode"]:
        global_prefs["end_mode"] = str(global_data["end_mode"]).strip()

    # Extract project / request if present
    if "project" in project_data and project_data["project"]:
        project_prefs["project"] = str(project_data["project"]).strip()
    if "project" in global_data and global_data["project"]:
        global_prefs["project"] = str(global_data["project"]).strip()

    if "request" in project_data and project_data["request"]:
        project_prefs["request"] = str(project_data["request"]).strip()
    if "request" in global_data and global_data["request"]:
        global_prefs["request"] = str(global_data["request"]).strip()

    team_coords: list[str] = []
    if (
        isinstance(project_data.get("team"), dict)
        and project_data["team"].get("coordinators")
    ):
        team_coords = [str(c).strip() for c in project_data["team"]["coordinators"] if c]
    elif (
        isinstance(global_data.get("team"), dict)
        and global_data["team"].get("coordinators")
    ):
        team_coords = [str(c).strip() for c in global_data["team"]["coordinators"] if c]

    return project_prefs, global_prefs, team_coords


def _launch_coordinator(
    project_dir: Path,
    req_file: Path,
    cli_arg: str | None,
    model_arg: str | None,
    executors: str | None,
    end_mode: str | None,
) -> tuple[int, str | None, int | None, str | None]:
    """Start the job's coordinator with the maestro launcher: ``(exit, id, pid, error)``."""
    from . import maestro

    entries = [f"{cli_arg}:{model_arg}"] if cli_arg and model_arg else None
    try:
        record = maestro.launch(
            project_dir, req_file, coordinators=entries, executors=executors, end_mode=end_mode
        )
    except Exception as exc:
        return 1, None, None, str(exc)
    return 0, record["id"], record.get("pid"), None


def _launch_completed_interview_job(
    project_dir: Path,
    job: dict[str, Any],
    log: EventLog,
    now_iso: str,
    projects_dir: str | Path | None = None,
) -> None:
    """Move job to queued, write request file in job records area, and launch detached."""

    # 1. State moves to queued (never ready) with lifecycle event
    job["state"] = "queued"
    job["status"] = "queued"
    ev_queued = {"type": "queued", "state": "queued", "at": now_iso}
    seq_q = log.append(ev_queued)
    job.setdefault("events", []).append({
        "sequence": seq_q,
        "payload": ev_queued,
        **ev_queued,
    })
    records.save_job(project_dir, job)

    # 2. Write request file inside project's records area (docs/crew-chief/jobs), not /tmp
    jobs_area = records.jobs_dir(project_dir)
    jobs_area.mkdir(parents=True, exist_ok=True)
    job_id = job.get("id") or "job"
    req_file = jobs_area / f"{job_id}.request.md"
    req_text = (
        job.get("request")
        or (job.get("interview", {}).get("answers", {}).get("request") or {}).get("value")
        or ""
    )
    req_file.write_text(req_text, encoding="utf-8")

    # 3. Map coordinator answer (cli:model -> cli and model arguments)
    answers = (job.get("interview") or {}).get("answers", {})
    coord_val = job.get("coordinator") or (answers.get("coordinator") or {}).get("value")
    cli_arg = None
    model_arg = None
    if coord_val:
        c_str = str(coord_val).strip()
        cli_part, sep, model_part = c_str.partition(":")
        if sep:
            cli_arg = cli_part.strip()
            model_arg = model_part.strip() if model_part.strip() else None
        elif "-" in c_str and c_str.split("-")[0].lower() in ("claude", "codex"):
            parts = c_str.split("-", 1)
            cli_arg = parts[0]
            model_arg = parts[1]
        else:
            cli_arg = c_str

    executors = job.get("executors") or (answers.get("executors") or {}).get("value") or "free_first"
    end_mode = job.get("end_mode") or (answers.get("end_mode") or {}).get("value") or "pr"

    launch_info = {
        "cli": cli_arg,
        "model": model_arg,
        "executors": executors,
        "end_mode": end_mode,
    }

    exit_code, coord_id, runner_pid, launch_error = _launch_coordinator(
        project_dir, req_file, cli_arg, model_arg, executors, end_mode
    )

    if exit_code == 0:
        now_run = datetime.now(timezone.utc).isoformat()
        job["state"] = "running"
        job["status"] = "running"
        job["coordinator_id"] = coord_id
        job["runner_pid"] = runner_pid
        job["launch_args"] = launch_info
        job["request_file"] = str(req_file)

        ev_running = {
            "type": "running",
            "state": "running",
            "coordinator_id": coord_id,
            "runner_pid": runner_pid,
            "launch_args": launch_info,
            "at": now_run,
        }
        seq_run = log.append(ev_running)
        job.setdefault("events", []).append({
            "sequence": seq_run,
            "payload": ev_running,
            **ev_running,
        })
        records.save_job(project_dir, job)
    else:
        err_msg = launch_error or f"launch exited with code {exit_code}"
        transition_terminal_state(
            job=job,
            project=project_dir,
            job_id=job["id"],
            new_state="failed",
            extra_fields={
                "error": str(err_msg),
                "reason": "launch_failed",
            },
        )


def _reconstruct_event_log(job: dict[str, Any]) -> EventLog:
    """Reconstruct an EventLog instance from a persisted job's events list."""
    log = EventLog()
    for ev in job.get("events", []):
        if isinstance(ev, dict):
            payload = ev.get("payload") if "payload" in ev else {k: v for k, v in ev.items() if k != "sequence"}
            log.append(payload)
    return log


def start_interview_job(
    *,
    project: str | Path | None = None,
    request: str | None = None,
    coordinator: str | None = None,
    executors: str | None = None,
    end_mode: str | None = None,
    title: str | None = None,
    caller_answers: dict[str, Any] | None = None,
    projects_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Initialize a canonical persisted interview job.

    Enforces validation, skips questions covered by preferences, records
    source attribution ('caller', 'project_prefs', 'global_prefs'), and
    emits lifecycle events.
    """
    project_dir, project_id = resolve_project_info(project, projects_dir=projects_dir)
    project_prefs, global_prefs, team_coords = load_interview_prefs(project_dir)

    raw_caller: dict[str, Any] = dict(caller_answers or {})
    if project is not None or project_id:
        raw_caller.setdefault("project", project_id or str(project))
    if request is not None and str(request).strip():
        raw_caller.setdefault("request", str(request).strip())
    if coordinator is not None and str(coordinator).strip():
        raw_caller.setdefault("coordinator", str(coordinator).strip())
    if executors is not None and str(executors).strip():
        raw_caller.setdefault("executors", str(executors).strip())
    if end_mode is not None and str(end_mode).strip():
        raw_caller.setdefault("end_mode", str(end_mode).strip())

    # Validate caller-supplied answers
    for k, v in raw_caller.items():
        validate_answer(k, v, team_coords, projects_dir=projects_dir)

    # Build answers dict with provenance
    answers: dict[str, Any] = {}
    for key in ("project", "request", "coordinator", "executors", "end_mode"):
        if key in raw_caller and raw_caller[key] is not None and raw_caller[key] != "":
            answers[key] = {"value": raw_caller[key], "source": "caller"}
        elif key in project_prefs and project_prefs[key] is not None and project_prefs[key] != "":
            answers[key] = {"value": project_prefs[key], "source": "project_prefs"}
        elif key in global_prefs and global_prefs[key] is not None and global_prefs[key] != "":
            answers[key] = {"value": global_prefs[key], "source": "global_prefs"}

    engine = InterviewEngine(project_prefs=project_prefs, global_prefs=global_prefs)
    pending_questions = engine.pending_questions(answers)
    pending_keys = [q.key for q in pending_questions]
    state = "queued" if len(pending_keys) == 0 else "interview"

    # Create canonical job record via records.new_job
    exec_mode = answers.get("executors", {}).get("value") or "free_first"
    req_text = answers.get("request", {}).get("value") or ""
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()

    job = records.new_job(
        project_dir,
        title=title or "Interview job",
        request=req_text,
        distributed=True,
        reason="interview",
        now=now,
        executors=exec_mode,
    )
    job["state"] = state
    if "coordinator" in answers:
        coord_val = answers["coordinator"]["value"]
        coord_source = answers["coordinator"].get("source", "caller")
        job["coordinator"] = coord_val
        records.record_team_used(job, role="coordinator", id=coord_val, origin=coord_source)
    if "executors" in answers:
        job["executors"] = answers["executors"]["value"]
    if "end_mode" in answers:
        job["end_mode"] = answers["end_mode"]["value"]

    job["interview"] = {
        "started_at": now_iso,
        "answers": answers,
        "pending": pending_keys,
    }

    log = EventLog()
    ev1 = {"type": "interview_started", "at": now_iso, "state": state, "pending": pending_keys}
    seq1 = log.append(ev1)
    events_list = [{"sequence": seq1, "payload": ev1}]

    if state == "queued":
        ev2 = {"type": "interview_completed", "at": now_iso, "state": "queued"}
        seq2 = log.append(ev2)
        events_list.append({"sequence": seq2, "payload": ev2})
        job["events"] = events_list
        records.save_job(project_dir, job)

        _launch_completed_interview_job(project_dir, job, log, now_iso, projects_dir=projects_dir)
        state = job["state"]
    else:
        job["events"] = events_list
        records.save_job(project_dir, job)

    return {
        "ok": True,
        "job_id": job["id"],
        "state": state,
        "questions": [
            {
                "key": q.key,
                "prompt": q.prompt,
                "answered": engine.is_question_answered(q, answers),
            }
            for q in engine.questions()
        ],
        "pending": pending_keys,
    }


def answer_interview_job(
    *,
    project: str | Path | None = None,
    job_id: str,
    key: str,
    value: Any,
    projects_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Record an answer for an existing interview job and transition state if complete."""
    project_dir, _ = resolve_project_info(project, projects_dir=projects_dir)

    project_prefs, global_prefs, team_coords = load_interview_prefs(project_dir)
    validated_value = validate_answer(key, value, team_coords, projects_dir=projects_dir)

    # Answer -> decide -> mark launching atomically under the job lock so two
    # concurrent final answers cannot both launch. Only the caller that flips
    # the state to queued performs the launch outside the lock; the loser only
    # records its answer and returns without launching.
    claimed_launch = False
    launch_req_text = ""
    launch_coord_val: Any = None
    launch_executors: Any = None
    launch_end_mode: Any = None
    return_state: Any = None
    return_pending: list[str] = []

    with records.job_lock(project_dir, job_id):
        job = records.load_job(project_dir, job_id)

        if check_interview_expiration(job, project=project_dir, job_id=job_id):
            try:
                records.save_job(project_dir, job)
            except Exception:
                pass
            raise InterviewExpiredError(
                "cannot answer: interview has expired after configured timeout"
            )
        if job.get("state") == "expired" or job.get("status") == "expired":
            raise InterviewExpiredError(
                "cannot answer: interview has expired after configured timeout"
            )

        interview_data = job.setdefault("interview", {})
        answers = interview_data.setdefault("answers", {})
        answers[key] = {"value": validated_value, "source": "caller"}

        if key == "coordinator":
            job["coordinator"] = validated_value
            records.record_team_used(job, role="coordinator", id=validated_value, origin="caller")
        elif key == "executors":
            job["executors"] = validated_value
        elif key == "end_mode":
            job["end_mode"] = validated_value
        elif key == "request":
            job["request"] = validated_value

        engine = InterviewEngine(project_prefs=project_prefs, global_prefs=global_prefs)
        pending_questions = engine.pending_questions(answers)
        pending_keys = [q.key for q in pending_questions]
        interview_data["pending"] = pending_keys

        now_iso = datetime.now(timezone.utc).isoformat()
        log = _reconstruct_event_log(job)
        ev_ans = {"type": "answer", "key": key, "value": validated_value, "at": now_iso}
        seq_ans = log.append(ev_ans)
        job.setdefault("events", []).append({"sequence": seq_ans, "payload": ev_ans})

        if len(pending_keys) == 0:
            # Launch exactly once per job: idempotent if already queued, launched, or running
            current_state = job.get("state")
            current_status = job.get("status")
            already_launched = (
                current_state in ("queued", "running", "launching")
                or current_status in ("queued", "running", "launching")
                or bool(job.get("coordinator_id"))
                # "supervisor" is the pre-0.6.0 name of runner_pid
                or bool(job.get("runner_pid") or job.get("supervisor"))
            )
            if not already_launched:
                ev_done = {"type": "interview_completed", "state": "queued", "at": now_iso}
                seq_done = log.append(ev_done)
                job["events"].append({"sequence": seq_done, "payload": ev_done})
                job["state"] = "queued"
                job["status"] = "queued"
                records.save_job(project_dir, job)
                claimed_launch = True
                launch_req_text = (
                    job.get("request")
                    or (answers.get("request") or {}).get("value")
                    or ""
                )
                launch_coord_val = job.get("coordinator") or (answers.get("coordinator") or {}).get("value")
                launch_executors = job.get("executors") or (answers.get("executors") or {}).get("value") or "free_first"
                launch_end_mode = job.get("end_mode") or (answers.get("end_mode") or {}).get("value") or "pr"
                return_state = "queued"
                return_pending = []
            else:
                records.save_job(project_dir, job)
                return_state = job.get("state")
                return_pending = []
        else:
            records.save_job(project_dir, job)
            return_state = job.get("state")
            return_pending = pending_keys

    if not claimed_launch:
        return {
            "ok": True,
            "job_id": job_id,
            "state": return_state,
            "pending": return_pending,
        }

    # Only the claim winner launches, outside the lock. The final running/failed
    # transition is applied with update_job so a concurrent answer saved after
    # the claim is preserved and the launched coordinator is recorded once.

    jobs_area = records.jobs_dir(project_dir)
    jobs_area.mkdir(parents=True, exist_ok=True)
    req_file = jobs_area / f"{job_id}.request.md"
    req_file.write_text(launch_req_text, encoding="utf-8")

    cli_arg = None
    model_arg = None
    if launch_coord_val:
        c_str = str(launch_coord_val).strip()
        cli_part, sep, model_part = c_str.partition(":")
        if sep:
            cli_arg = cli_part.strip()
            model_arg = model_part.strip() if model_part.strip() else None
        elif "-" in c_str and c_str.split("-")[0].lower() in ("claude", "codex"):
            parts = c_str.split("-", 1)
            cli_arg = parts[0]
            model_arg = parts[1]
        else:
            cli_arg = c_str

    launch_info = {
        "cli": cli_arg,
        "model": model_arg,
        "executors": launch_executors,
        "end_mode": launch_end_mode,
    }

    exit_code, coord_id, runner_pid, launch_error = _launch_coordinator(
        project_dir, req_file, cli_arg, model_arg, launch_executors, launch_end_mode
    )

    if exit_code == 0:
        now_run = datetime.now(timezone.utc).isoformat()
        ev_running = {
            "type": "running",
            "state": "running",
            "coordinator_id": coord_id,
            "runner_pid": runner_pid,
            "launch_args": launch_info,
            "at": now_run,
        }

        def _apply_running(stored: dict) -> None:
            stored["state"] = "running"
            stored["status"] = "running"
            stored["coordinator_id"] = coord_id
            stored["runner_pid"] = runner_pid
            stored["launch_args"] = launch_info
            stored["request_file"] = str(req_file)
            try:
                running_log = _reconstruct_event_log(stored)
                seq_run = running_log.append(ev_running)
            except Exception:
                seq_run = len(stored.get("events", [])) + 1
            stored.setdefault("events", []).append({
                "sequence": seq_run,
                "payload": ev_running,
                **ev_running,
            })

        records.update_job(project_dir, job_id, _apply_running)
        return {
            "ok": True,
            "job_id": job_id,
            "state": "running",
            "pending": [],
        }
    else:
        err_msg = launch_error or f"launch exited with code {exit_code}"
        transition_terminal_state(
            job=None,
            project=project_dir,
            job_id=job_id,
            new_state="failed",
            extra_fields={
                "error": str(err_msg),
                "reason": "launch_failed",
            },
        )
        failed_job = records.load_job(project_dir, job_id)
        return {
            "ok": True,
            "job_id": job_id,
            "state": failed_job.get("state"),
            "pending": [],
        }


def _coordinator_summary(project_dir: Path, coordinator_id: str | None) -> dict[str, Any] | None:
    """The maestro summary of the job's coordinator, or ``None`` when it has no record."""
    if not coordinator_id:
        return None
    from . import maestro

    folder = maestro.coordinators_dir(project_dir) / str(coordinator_id)
    try:
        return maestro.summary(maestro.load(folder))
    except (OSError, ValueError, KeyError):
        return None


def get_interview_status(
    project: str | Path | None = None,
    job_id: str | None = None,
    projects_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Retrieve status and pending questions for a job.

    The expiration check runs under the job lock on a freshly loaded copy
    and the record is rewritten only when the job actually transitioned to
    expired. A read-only status query must never blind-save a stale copy:
    otherwise a status that read before a final answer launched would
    overwrite (and erase) the launch record, letting a later answer launch
    a second coordinator.
    """
    project_dir, _ = resolve_project_info(project, projects_dir=projects_dir)
    if not job_id:
        raise ValueError("job_id is required")
    with records.job_lock(project_dir, job_id):
        job = records.load_job(project_dir, job_id)
        if check_interview_expiration(job, project=project_dir, job_id=job_id):
            try:
                records.save_job(project_dir, job)
            except Exception:
                pass

    interview_data = job.get("interview", {})
    raw_state = job.get("state")
    state = "queued" if raw_state == "ready" else raw_state
    raw_status = job.get("status", raw_state)
    status_val = "queued" if raw_status == "ready" else raw_status
    pr_url = job.get("pr_url")
    # Once launched, the job's progress lives in its maestro coordinator record.
    coordinator = _coordinator_summary(project_dir, job.get("coordinator_id"))
    if coordinator is not None:
        state = status_val = coordinator["state"]
        pr_url = coordinator.get("pr_url") or pr_url
    return {
        "ok": True,
        "job_id": job_id,
        "state": state,
        "status": status_val,
        "pr_url": pr_url,
        "pending": interview_data.get("pending", []),
        "answers": interview_data.get("answers", {}),
        "team_used": list(job.get("team_used", [])),
    }


def get_interview_events(
    project: str | Path | None = None,
    job_id: str | None = None,
    cursor: int = 0,
    projects_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Query append-only events for a job using cursor replay."""
    project_dir, _ = resolve_project_info(project, projects_dir=projects_dir)
    if not job_id:
        raise ValueError("job_id is required")
    job = records.load_job(project_dir, job_id)
    log = _reconstruct_event_log(job)
    replayed = log.replay(cursor)
    latest_cursor = len(log._events)
    return {
        "ok": True,
        "job_id": job_id,
        "events": [{"sequence": e.sequence, "payload": e.payload} for e in replayed],
        "cursor": latest_cursor,
    }

