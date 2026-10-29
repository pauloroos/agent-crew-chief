"""MCP tool handlers delegating to existing central CLI/supervisor/messages (T003).

Each public function in this module is a thin adapter: it translates MCP tool
parameters into the argparse-style namespace or direct function call expected
by the existing product code, and returns a JSON-serializable result dict.
No business logic is reimplemented here.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from . import central_cli, central_messages, central_registry, central_supervisor, sdd_gate

__all__ = [
    "answer",
    "events",
    "list_jobs",
    "questions",
    "result",
    "start_job",
    "status",
    "stop",
]


def _ok(payload: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, **payload}


def _error(message: str) -> dict[str, Any]:
    return {"ok": False, "error": message}


# ---------------------------------------------------------------------------
# Argument builders (thin adapters for argparse namespaces)
# ---------------------------------------------------------------------------


def _build_launch_args(
    *,
    repo: str,
    request_file: str,
    mode: str,
    authorize_raw: bool = False,
    approved: bool = False,
    spec_dir: str | None = None,
    cli: str | None = None,
    backend: str | None = None,
    base: str | None = None,
    restricted: bool = False,
    unrestricted: bool = False,
    install_worktree_acc: bool = False,
    release_root: str | None = None,
    executors: str | None = None,
    central_dir: str | None = None,
    projects_dir: str | None = None,
) -> argparse.Namespace:
    return argparse.Namespace(
        repo=repo,
        request_file=request_file,
        mode=mode,
        authorize_raw=authorize_raw,
        approved=approved,
        spec_dir=spec_dir,
        cli=cli,
        backend=backend,
        base=base,
        restricted=restricted,
        unrestricted=unrestricted,
        install_worktree_acc=install_worktree_acc,
        release_root=release_root,
        executors=executors,
        central_dir=central_dir,
        projects_dir=projects_dir,
        json=True,
    )


def _build_message_args(
    *, coordinator_id: str, text: str, reply_to: str | None = None, central_dir: str | None = None
) -> argparse.Namespace:
    return argparse.Namespace(
        id=coordinator_id,
        text=text,
        reply_to=reply_to,
        central_dir=central_dir,
        json=True,
    )


def _build_central_args(*, central_dir: str | None = None) -> argparse.Namespace:
    return argparse.Namespace(
        once=True,
        plain=True,
        poll=1.0,
        central_dir=central_dir,
        json=True,
        project=None,
    )


def _build_control_args(
    *, coordinator_id: str, action: str, central_dir: str | None = None
) -> argparse.Namespace:
    return argparse.Namespace(
        id=coordinator_id,
        action=action,
        central_dir=central_dir,
        json=True,
    )


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------


def _trusted_projects_dir(projects_dir: str | None) -> Path | None:
    """Return the trusted projects_dir when containment is active, else None."""
    from . import interview_service

    try:
        return interview_service.get_trusted_projects_dir(projects_dir)
    except Exception:
        return None


def _legacy_blocked_error() -> dict[str, Any]:
    return {
        "ok": False,
        "isError": True,
        "error": "legacy coordinator operation requires a project within projects_dir",
    }


def start_job(
    *,
    project: str | None = None,
    request: str | None = None,
    coordinator: str | None = None,
    executors: str | None = None,
    end_mode: str | None = None,
    title: str | None = None,
    repo: str | None = None,
    request_file: str | None = None,
    mode: str | None = None,
    authorize_raw: bool = False,
    approved: bool = False,
    spec_dir: str | None = None,
    cli: str | None = None,
    backend: str | None = None,
    base: str | None = None,
    restricted: bool = False,
    unrestricted: bool = False,
    install_worktree_acc: bool = False,
    release_root: str | None = None,
    central_dir: str | None = None,
    projects_dir: str | None = None,
) -> dict[str, Any]:
    """Start a new job: canonical interview mode or legacy launch."""
    if mode is not None and repo is not None and request_file is not None:
        from . import interview_service, mcp_errors

        try:
            project_dir, _ = interview_service.resolve_project_info(
                repo, projects_dir=projects_dir
            )
        except Exception as exc:
            sanitized = mcp_errors.sanitize_error(exc)
            return {"ok": False, "isError": True, "error": sanitized["message"]}
        try:
            from .project_resolver import resolve_contained_file

            resolve_contained_file(request_file, project_dir)
        except Exception as exc:
            sanitized = mcp_errors.sanitize_error(exc)
            return {"ok": False, "isError": True, "error": sanitized["message"]}
        args = _build_launch_args(
            repo=str(project_dir),
            request_file=str(request_file),
            mode=mode,
            authorize_raw=authorize_raw,
            approved=approved,
            spec_dir=spec_dir,
            cli=cli,
            backend=backend,
            base=base,
            restricted=restricted,
            unrestricted=unrestricted,
            install_worktree_acc=install_worktree_acc,
            release_root=release_root,
            executors=executors,
            central_dir=central_dir,
            projects_dir=projects_dir,
        )
        exit_code = central_cli.handle_launch(args)
        if exit_code == 0:
            return _ok({"exit_code": exit_code})
        return _error(f"launch exited with code {exit_code}")

    from . import interview_service, mcp_errors

    try:
        req_text = request
        if req_text is None and request_file is not None:
            project_dir, _ = interview_service.resolve_project_info(
                project or repo, projects_dir=projects_dir
            )
            from .project_resolver import resolve_contained_file

            resolve_contained_file(request_file, project_dir)
            req_text = Path(request_file).read_text(encoding="utf-8-sig")

        caller_answers: dict[str, Any] = {}
        if project or repo:
            caller_answers["project"] = str(project or repo)
        if coordinator:
            caller_answers["coordinator"] = coordinator
        if executors:
            caller_answers["executors"] = executors
        if end_mode:
            caller_answers["end_mode"] = end_mode
        if req_text:
            caller_answers["request"] = req_text

        res = interview_service.start_interview_job(
            project=project or repo,
            request=req_text,
            coordinator=coordinator,
            executors=executors,
            end_mode=end_mode,
            title=title,
            caller_answers=caller_answers,
            projects_dir=projects_dir,
        )
        return _ok(res)
    except Exception as exc:
        sanitized = mcp_errors.sanitize_error(exc)
        return {"ok": False, "isError": True, "error": sanitized["message"]}


def answer(
    *,
    coordinator_id: str | None = None,
    job_id: str | None = None,
    key: str | None = None,
    value: Any = None,
    text: str | None = None,
    project: str | None = None,
    reply_to: str | None = None,
    central_dir: str | None = None,
    projects_dir: str | None = None,
) -> dict[str, Any]:
    """Send an answer: interview question answer or legacy coordinator message."""
    if key is not None or (job_id is not None and coordinator_id is None):
        from . import interview_service, mcp_errors, records

        target_id = job_id or coordinator_id or ""
        val = value if value is not None else text
        try:
            if job_id is not None:
                records.validate_job_id(job_id)
            res = interview_service.answer_interview_job(
                project=project,
                job_id=target_id,
                key=key or "",
                value=val,
                projects_dir=projects_dir,
            )
            return _ok(res)
        except Exception as exc:
            sanitized = mcp_errors.sanitize_error(exc)
            return {"ok": False, "isError": True, "error": sanitized["message"]}

    args = _build_message_args(
        coordinator_id=coordinator_id or "", text=text or "", reply_to=reply_to, central_dir=central_dir
    )
    if _trusted_projects_dir(projects_dir) is not None:
        return _legacy_blocked_error()
    exit_code = central_cli.handle_message(args)
    if exit_code == 0:
        return _ok({"exit_code": exit_code})
    return _error(f"message exited with code {exit_code}")


def status(
    *,
    job_id: str | None = None,
    project: str | None = None,
    central_dir: str | None = None,
    projects_dir: str | None = None,
) -> dict[str, Any]:
    """Return status: interview job status or legacy one-shot central snapshot."""
    from . import interview_service, mcp_errors

    if project is not None:
        try:
            interview_service.resolve_project_info(project, projects_dir=projects_dir)
        except Exception as exc:
            sanitized = mcp_errors.sanitize_error(exc)
            return {"ok": False, "isError": True, "error": sanitized["message"]}

    if job_id is not None:
        try:
            from . import records
            records.validate_job_id(job_id)
            res = interview_service.get_interview_status(
                project=project, job_id=job_id, projects_dir=projects_dir
            )
            return _ok(res)
        except Exception as exc:
            sanitized = mcp_errors.sanitize_error(exc)
            return {"ok": False, "isError": True, "error": sanitized["message"]}

    args = _build_central_args(central_dir=central_dir)
    if _trusted_projects_dir(projects_dir) is not None:
        return _legacy_blocked_error()
    exit_code = central_cli.handle_central(args)
    if exit_code == 0:
        return _ok({"exit_code": exit_code})
    return _error(f"central exited with code {exit_code}")


def events(
    *,
    job_id: str | None = None,
    coordinator_id: str | None = None,
    cursor: int = 0,
    project: str | None = None,
    central_dir: str | None = None,
    projects_dir: str | None = None,
) -> dict[str, Any]:
    """Read events: cursor-based job events or legacy supervisor events."""
    from . import interview_service, mcp_errors

    if project is not None:
        try:
            interview_service.resolve_project_info(project, projects_dir=projects_dir)
        except Exception as exc:
            sanitized = mcp_errors.sanitize_error(exc)
            return {"ok": False, "isError": True, "error": sanitized["message"]}

    if job_id is not None:
        try:
            from . import records
            records.validate_job_id(job_id)
            res = interview_service.get_interview_events(
                project=project, job_id=job_id, cursor=cursor, projects_dir=projects_dir
            )
            return _ok(res)
        except Exception as exc:
            sanitized = mcp_errors.sanitize_error(exc)
            return {"ok": False, "isError": True, "error": sanitized["message"]}

    try:
        if _trusted_projects_dir(projects_dir) is not None:
            return _legacy_blocked_error()
        payload = _read_supervisor_events(coordinator_id or "", central_dir)
        return _ok(payload)
    except Exception as exc:
        return _error(str(exc))


def _read_supervisor_events(coordinator_id: str, central_dir: str | None) -> dict[str, Any]:
    """Delegate supervisor event reading to central_supervisor's own record path."""
    from . import central_config

    root = central_config.resolve_root(central_dir, {})
    coord = central_messages._coordinator_dir(root, coordinator_id)
    supervisor_path = coord / "supervisor.json"
    if not supervisor_path.is_file():
        return {"events": [], "state": None}
    data = supervisor_path.read_text(encoding="utf-8-sig")
    record = json.loads(data)
    return {
        "events": list(record.get("events", [])),
        "state": record.get("state"),
        "round_id": record.get("round_id"),
        "heartbeat_at": record.get("heartbeat_at"),
    }


def questions(
    *,
    job_id: str | None = None,
    project: str | None = None,
    central_dir: str | None = None,
    projects_dir: str | None = None,
) -> dict[str, Any]:
    """List open questions: for a job record or across all coordinators."""
    from . import interview_service, records, mcp_errors

    project_dir = None
    if project is not None:
        try:
            project_dir, _ = interview_service.resolve_project_info(project, projects_dir=projects_dir)
        except Exception as exc:
            sanitized = mcp_errors.sanitize_error(exc)
            return {"ok": False, "isError": True, "error": sanitized["message"]}

    if job_id is not None:
        try:
            records.validate_job_id(job_id)
            if project_dir is None:
                project_dir, _ = interview_service.resolve_project_info(None, projects_dir=projects_dir)
            job = records.load_job(project_dir, job_id)
            interview_data = job.get("interview", {})
            return _ok({
                "job_id": job_id,
                "pending": interview_data.get("pending", []),
                "answers": interview_data.get("answers", {}),
            })
        except Exception as exc:
            sanitized = mcp_errors.sanitize_error(exc)
            return {"ok": False, "isError": True, "error": sanitized["message"]}

    from . import central_config

    if _trusted_projects_dir(projects_dir) is not None:
        return _legacy_blocked_error()
    root = central_config.resolve_root(central_dir, {})
    rows = central_messages.questions(root)
    return _ok({"questions": rows})


def result(
    *,
    coordinator_id: str | None = None,
    job_id: str | None = None,
    project: str | None = None,
    central_dir: str | None = None,
    projects_dir: str | None = None,
) -> dict[str, Any]:
    """Read pending outbox messages or structured job results."""
    from . import interview_service, records, mcp_errors

    project_dir = None
    if project is not None:
        try:
            project_dir, _ = interview_service.resolve_project_info(project, projects_dir=projects_dir)
        except Exception as exc:
            sanitized = mcp_errors.sanitize_error(exc)
            return {"ok": False, "isError": True, "error": sanitized["message"]}

    if job_id is not None:
        try:
            records.validate_job_id(job_id)
            if project_dir is None:
                project_dir, _ = interview_service.resolve_project_info(None, projects_dir=projects_dir)
            job = records.load_job(project_dir, job_id)
            notice_payload = job.get("notice_payload") or {
                "job_id": job_id,
                "status": job.get("status") or job.get("state"),
                "pr_url": job.get("pr_url"),
                "timestamp": job.get("timestamp"),
            }
            return _ok({
                "job_id": job_id,
                "pr_url": job.get("pr_url"),
                "status": job.get("status") or job.get("state"),
                "summary": job.get("summary") or job.get("title"),
                "tests": job.get("tests"),
                "notice_payload": notice_payload,
                "results": [],
                "team_used": list(job.get("team_used", [])),
            })
        except Exception as exc:
            sanitized = mcp_errors.sanitize_error(exc)
            return {"ok": False, "isError": True, "error": sanitized["message"]}

    from . import central_config

    if _trusted_projects_dir(projects_dir) is not None:
        return _legacy_blocked_error()
    root = central_config.resolve_root(central_dir, {})
    rows = central_messages.pending(root, coordinator_id or "", "out")

    pr_url: str | None = None
    summary: str | None = None
    tests: dict[str, Any] | list[Any] | None = None

    for row in rows:
        if not isinstance(row, dict):
            continue
        if pr_url is None and isinstance(row.get("pr_url"), str):
            pr_url = row["pr_url"]
        if summary is None and isinstance(row.get("summary"), str):
            summary = row["summary"]
        if tests is None:
            candidate = row.get("tests")
            if isinstance(candidate, (dict, list)):
                tests = candidate
            elif isinstance(row.get("test_results"), (dict, list)):
                tests = row["test_results"]

    return _ok({
        "results": rows,
        "pr_url": pr_url,
        "summary": summary,
        "tests": tests,
    })


def stop(
    *,
    coordinator_id: str | None = None,
    job_id: str | None = None,
    project: str | None = None,
    central_dir: str | None = None,
    projects_dir: str | None = None,
) -> dict[str, Any]:
    """Request a stop for an interview job or coordinator."""
    from . import interview_service, records, mcp_errors

    project_dir = None
    if project is not None:
        try:
            project_dir, _ = interview_service.resolve_project_info(project, projects_dir=projects_dir)
        except Exception as exc:
            sanitized = mcp_errors.sanitize_error(exc)
            return {"ok": False, "isError": True, "error": sanitized["message"]}

    if job_id is not None:
        try:
            records.validate_job_id(job_id)
            if project_dir is None:
                project_dir, _ = interview_service.resolve_project_info(None, projects_dir=projects_dir)

            job = records.load_job(project_dir, job_id)
            coord_id = job.get("coordinator_id")

            extra_fields: dict[str, Any] = {}
            control_result: dict[str, Any] | None = None

            if coord_id:
                ctrl_args = _build_control_args(
                    coordinator_id=str(coord_id),
                    action="stop",
                    central_dir=central_dir or job.get("central_dir"),
                )
                try:
                    exit_code = central_cli.handle_control(ctrl_args)
                    if exit_code == 0:
                        control_result = {"ok": True, "exit_code": exit_code}
                    else:
                        control_result = {
                            "ok": False,
                            "exit_code": exit_code,
                            "error": f"coordinator stop exited with code {exit_code}",
                        }
                except Exception as exc:
                    sanitized_ctrl = mcp_errors.sanitize_error(exc)
                    control_result = {
                        "ok": False,
                        "error": sanitized_ctrl.get("message", str(exc)),
                    }

                extra_fields["control"] = control_result
                if not control_result.get("ok"):
                    extra_fields["error"] = control_result.get("error")
                    extra_fields["result"] = {"control": control_result, "error": control_result.get("error")}
                else:
                    extra_fields["result"] = {"control": control_result}

            # Load preferences for notify settings
            from . import config
            prefs = config.load_prefs(project_dir)
            notify_url = getattr(prefs, "notify_url", None)
            notify_secret_file = getattr(prefs, "notify_secret_file", None)
            notify_allow_http = getattr(prefs, "notify_allow_http", False)

            # Use shared terminal-transition helper
            from . import webhook
            webhook.transition_terminal_state(
                project=project_dir,
                job_id=job_id,
                new_state="stopped",
                extra_fields=extra_fields if extra_fields else None,
                notify_url=notify_url,
                notify_secret_file=notify_secret_file,
                notify_allow_http=notify_allow_http,
            )
            res_payload: dict[str, Any] = {"job_id": job_id, "state": "stopped"}
            if control_result is not None:
                res_payload["control"] = control_result
            return _ok(res_payload)
        except Exception as exc:
            sanitized = mcp_errors.sanitize_error(exc)
            return {"ok": False, "isError": True, "error": sanitized["message"]}

    args = _build_control_args(
        coordinator_id=coordinator_id or "", action="stop", central_dir=central_dir
    )
    if _trusted_projects_dir(projects_dir) is not None:
        return _legacy_blocked_error()
    exit_code = central_cli.handle_control(args)
    if exit_code == 0:
        return _ok({"exit_code": exit_code})
    return _error(f"stop exited with code {exit_code}")


def list_jobs(
    *,
    project: str | None = None,
    central_dir: str | None = None,
    projects_dir: str | None = None,
) -> dict[str, Any]:
    """List registered coordinators and interview jobs."""
    from . import central_config, interview_service, mcp_errors

    try:
        jobs = interview_service.list_interview_jobs(project, projects_dir=projects_dir)
    except Exception as exc:
        sanitized = mcp_errors.sanitize_error(exc)
        return {"ok": False, "isError": True, "error": sanitized["message"]}

    try:
        root = central_config.resolve_root(central_dir, {})
        registry = central_registry.load_registry(root)
        coordinators = registry.get("coordinators", {})
        trusted = _trusted_projects_dir(projects_dir)
        for cid, rec in coordinators.items():
            if isinstance(rec, dict):
                if trusted is not None:
                    from .project_resolver import is_path_within

                    candidates = [
                        rec.get("repo"),
                        rec.get("worktree"),
                    ]
                    contained = False
                    for cand in candidates:
                        if not cand:
                            continue
                        try:
                            if is_path_within(str(cand), str(trusted)):
                                contained = True
                                break
                        except Exception:
                            continue
                    if not contained:
                        continue
                jobs.append({
                    "id": cid,
                    "state": rec.get("state"),
                    "worktree": rec.get("worktree"),
                })
    except Exception:
        pass

    return _ok({"jobs": jobs})