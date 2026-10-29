"""Job and piece lifecycle commands for the crew-chief CLI."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from . import cli

__all__ = [
    "_cmd_job_new",
    "_cmd_job_set",
    "_cmd_job_show",
    "_cmd_piece_add",
    "_cmd_piece_start",
    "_cmd_piece_finish",
]


def _cmd_job_new(args: argparse.Namespace) -> int:
    from . import cli

    project = cli._project(args)
    distributed = not args.not_distributed
    reason = args.reason or ""
    if not distributed and not reason:
        raise ValueError("--not-distributed requires --reason")

    request = cli._read_text(Path(args.request_file), "--request-file")
    executors = args.executors
    if executors is None:
        try:
            prefs = cli.load_prefs(project)
            executors = prefs.executors_default
        except Exception:  # noqa: BLE001 - invalid prefs must not block job creation
            executors = "free_first"
        # "ask" means an interactive coordinator prompts at job start; at
        # creation time we persist the non-interactive fallback instead.
        fallback_from_ask = executors == "ask"
        if fallback_from_ask:
            executors = "free_first"
        else:
            fallback_from_ask = False
    else:
        fallback_from_ask = False
    job = cli.records.new_job(
        project,
        args.title,
        request,
        distributed,
        reason,
        args.issue or "",
        executors=executors,
        superpowers=getattr(args, "superpowers", None),
    )
    try:
        from .git_delivery import prepare_job_branch

        try:
            prefs = cli.load_prefs(project)
            base_branch = getattr(prefs, "base_branch", "main") or "main"
        except Exception:
            base_branch = "main"

        branch_name = prepare_job_branch(project, job["id"], base_branch=base_branch)
        job = cli.records.update_job(
            project,
            job["id"],
            lambda stored: stored.update({"job_branch": branch_name, "base_branch": base_branch}) or stored,
        )
    except Exception:
        pass
    if fallback_from_ask:
        # FR-040: record the non-interactive fallback so audits can tell
        # an explicit choice from a coordinator-prompt default.
        cli.records.set_executors_fallback(
            project, job["id"], "ask", "free_first", source="job new"
        )
    cli._emit(args, job["id"], {"id": job["id"]})
    sys.stdout.flush()
    cli._check_models_to_stderr(project)
    return 0


def _cmd_job_set(args: argparse.Namespace) -> int:
    """Update mutable job fields atomically under lock."""
    from . import cli

    project = cli._project(args)
    if args.executors is None:
        raise ValueError("job set requires at least one field to update (--executors)")

    cli.records.set_executors(project, args.job, args.executors, source="job set")
    payload = {"job": args.job, "executors": args.executors}
    cli._emit(args, f"executors: {args.executors}", payload)
    return 0


def _cmd_job_show(args: argparse.Namespace) -> int:
    """Display job details."""
    from . import cli

    project = cli._project(args)
    job = cli.records.load_job(project, args.job)
    mode = cli.records.job_executors(job)

    coordinator = (
        getattr(args, "coordinator", None)
        or os.environ.get("CREW_CHIEF_COORDINATOR")
        or ""
    )
    if mode in ("own", "subagents") and coordinator:
        exec_line = f"executors: {mode} ({coordinator})"
    else:
        exec_line = f"executors: {mode}"

    lines = [
        f"job: {job.get('id', '')}",
        f"title: {job.get('title', '')}",
        exec_line,
    ]
    text = "\n".join(lines)
    payload = dict(job)
    payload["executors"] = mode
    cli._emit(args, text, payload)
    return 0


def _cmd_piece_add(args: argparse.Namespace) -> int:
    from . import cli

    project = cli._project(args)
    job = cli.records.load_job(project, args.job)
    if job.get("state") == "blocked" or job.get("status") == "blocked":
        raise ValueError(f"job {args.job} is blocked; local commits ceased")
    number = cli.records.update_job(
        project,
        args.job,
        lambda job: cli.records.add_piece(
            job,
            args.title,
            args.type,
            args.risk,
            superpowers=getattr(args, "superpowers", None),
        ),
    )
    cli._emit(args, str(number), {"piece": number})
    return 0


def _cmd_piece_start(args: argparse.Namespace) -> int:
    """Create an isolated worktree for a native subagent piece (FR-048)."""
    from . import cli

    if not args.native:
        raise ValueError("piece start requires --native")
    project = cli._project(args)
    coordinator = args.coordinator or os.environ.get(cli._COORDINATOR_ENV) or ""
    model = args.model or ""
    if not coordinator or not model:
        raise ValueError(
            "piece start --native requires --coordinator and --model "
            "(or CREW_CHIEF_COORDINATOR and --model)"
        )
    executor_id = f"native:{coordinator}/{model}"
    prefs = cli.load_prefs(project)

    # FR-043: native flow must enforce the same dependency gate as dispatch.
    pre_job = cli.records.load_job(project, args.job)
    if pre_job.get("state") == "blocked" or pre_job.get("status") == "blocked":
        raise ValueError(f"job {args.job} is blocked; local commits ceased")

    from .interview import check_interview_gate
    check_interview_gate(pre_job, prefs=prefs)

    from .sdd_gate import check_spec_gate
    coord_id = pre_job.get("coordinator") or coordinator
    job_for_gate = pre_job if pre_job.get("coordinator") else dict(pre_job, coordinator=coord_id)
    spec_gate_error = check_spec_gate(project, job_for_gate, args.piece, role="exec")
    if spec_gate_error:
        raise ValueError(spec_gate_error)

    pre_piece = cli._piece(pre_job, args.piece)
    pre_ref = pre_piece.get("ref")
    pre_spec_dir = pre_job.get("spec_dir")

    lease_dir = (
        getattr(prefs, "executor_lease_dir", None)
        or os.environ.get("AGENT_CREW_CHIEF_EXECUTOR_LEASE_DIR")
        or os.environ.get("ACC_EXECUTOR_LEASE_DIR")
    )
    max_running = int(
        os.environ.get("AGENT_CREW_CHIEF_MAX_RUNNING_EXECUTORS")
        or os.environ.get("ACC_MAX_RUNNING_EXECUTORS")
        or getattr(prefs, "max_running_executors", 3)
    )
    slots = cli.ExecutorSlots(lease_dir=lease_dir, max_running=max_running)
    timeout_s = (
        float(args.timeout)
        if getattr(args, "timeout", None) is not None
        else 5.0
    )

    next_num = max(
        1 + len(pre_piece.get("attempts") or []),
        int(pre_piece.get("next_attempt") or 1),
    )
    next_num = cli.worktree.next_free_attempt(project, pre_job["id"], args.piece, next_num)
    owner = f"{args.job}-p{args.piece}-a{next_num}"

    def _on_waiting() -> None:
        wait_event = {
            "type": "waiting_for_executor_slot",
            "reason": f"queued for executor slot (max_running={slots.max_running})",
            "piece": args.piece,
            "attempt": next_num,
            "timestamp": cli._now_iso(),
        }
        try:
            cli.records.update_job(
                project,
                args.job,
                lambda j: j.setdefault("events", []).append(wait_event),
            )
        except Exception:
            pass

    try:
        slots.acquire(
            owner,
            timeout_s=timeout_s,
            on_waiting=_on_waiting,
            durable=True,
            project=project,
            job_id=args.job,
            piece=args.piece,
            attempt=next_num,
            timeout_s_meta=float(prefs.dispatch_timeout_s),
        )
    except cli.LeaseError:
        msg = (
            f"refused: executor pool is full (max_running={slots.max_running}); "
            f"could not acquire slot within {timeout_s}s"
        )
        print(f"error: {msg}", file=sys.stderr)
        cli._emit(args, msg, {"error": "executor_pool_full", "max_running": slots.max_running})
        return 1

    if pre_ref and pre_spec_dir and not args.ignore_deps:
        waits = cli._spec_waits(project, pre_job, pre_ref, pre_spec_dir)
        if waits:
            slots.release(owner=owner)
            note = f"piece {args.piece} ({pre_ref}) waits for {waits}"

            def fail_deps(job: dict) -> tuple[int, Path, str]:
                piece = cli._piece(job, args.piece)
                number = max(
                    1 + len(piece.get("attempts") or []),
                    int(piece.get("next_attempt") or 1),
                )
                number = cli.worktree.next_free_attempt(
                    project, job["id"], args.piece, number
                )
                piece["next_attempt"] = number + 1
                attempt = {
                    "n": number,
                    "executor": executor_id,
                    "role": "exec",
                    "status": "failed",
                    "started": cli._now_iso(),
                    "note": note,
                }
                cli.records.add_attempt(job, args.piece, attempt)
                return number, Path(""), ""

            number, wt, branch = cli.records.update_job(
                project, args.job, fail_deps
            )
            cli._emit(args, note, {"piece": args.piece, "attempt": number, "status": "failed"})
            return 1

    def reserve_and_record(job: dict) -> tuple[int, Path, str]:
        check_interview_gate(job, prefs=prefs)
        coord_id = job.get("coordinator") or coordinator
        job_for_gate = job if job.get("coordinator") else dict(job, coordinator=coord_id)
        spec_gate_error = check_spec_gate(project, job_for_gate, args.piece, role="exec")
        if spec_gate_error:
            raise ValueError(spec_gate_error)
        piece = cli._piece(job, args.piece)
        number = max(
            1 + len(piece.get("attempts") or []),
            int(piece.get("next_attempt") or 1),
        )
        number = cli.worktree.next_free_attempt(project, job["id"], args.piece, number)
        # A correction starts from the attempt it corrects, as in dispatch.
        start_from = cli.resolve_base(job, args.piece, project, args.base, args.fresh)
        piece["next_attempt"] = number + 1
        wt, branch = cli.worktree.create_worktree(
            project,
            job["id"],
            args.piece,
            number,
            start=start_from["branch"] if start_from else "HEAD",
        )
        if prefs.sensitive_paths:
            cli.worktree.hide_sensitive(wt, prefs.sensitive_paths)
        attempt = {
            "n": number,
            "executor": executor_id,
            "role": "exec",
            "cost": "included",
            "status": "running",
            "started": cli._now_iso(),
            "duration_s": 0.0,
            "duration": 0.0,
            "branch": branch,
            "worktree": str(wt),
            # Where the worktree started (the diff and review base, as in
            # dispatch) and where the main checkout was (the isolation check).
            "start_sha": cli.worktree.head_commit(wt),
            "project_sha": cli.worktree.head_commit(project),
            "base": start_from,
            "diffstat": [],
            "destructive_suspect": [],
            "outside_writes": [],
        }
        cli.records.add_attempt(job, args.piece, attempt)
        return number, wt, branch

    try:
        number, wt, branch = cli.records.update_job(project, args.job, reserve_and_record)
        if number != next_num:
            slots.release(owner=owner)
            slots.acquire(
                f"{args.job}-p{args.piece}-a{number}",
                timeout_s=0.1,
                durable=True,
                project=project,
                job_id=args.job,
                piece=args.piece,
                attempt=number,
                timeout_s_meta=float(prefs.dispatch_timeout_s),
            )
    except Exception:
        slots.release(owner=owner)
        raise
    text = f"worktree: {wt}\nbranch: {branch}"
    payload = {
        "piece": args.piece,
        "attempt": number,
        "executor": executor_id,
        "worktree": str(wt),
        "branch": branch,
    }
    cli._emit(args, text, payload)
    return 0


def _cmd_piece_finish(args: argparse.Namespace) -> int:
    """Finish a native piece: record diff, run proof, refuse outside work (FR-049)."""
    from . import cli
    from .dispatch import _run_proof

    project = cli._project(args)
    job = cli.records.load_job(project, args.job)
    if job.get("state") == "blocked" or job.get("status") == "blocked":
        raise ValueError(f"job {args.job} is blocked; local commits ceased")
    prefs = cli.load_prefs(project)

    def finalize(job: dict) -> tuple[dict, str | None]:
        piece = cli._piece(job, args.piece)
        attempts = [
            a for a in (piece.get("attempts") or [])
            if isinstance(a, dict) and a.get("role") == "exec"
        ]
        running = [a for a in attempts if a.get("status") == "running"]
        if not running:
            raise ValueError(
                f"piece {args.piece} has no running native attempt to finish"
            )
        attempt = running[-1]
        if attempt.get("started"):
            try:
                started_dt = datetime.fromisoformat(attempt["started"])
                if started_dt.tzinfo is None:
                    started_dt = started_dt.replace(tzinfo=timezone.utc)
                now_dt = datetime.now(timezone.utc)
                dur = round(max(0.0, (now_dt - started_dt).total_seconds()), 1)
                attempt["duration_s"] = dur
                attempt["duration"] = dur
            except Exception:
                attempt["duration_s"] = 0.0
                attempt["duration"] = 0.0
        else:
            attempt["duration_s"] = 0.0
            attempt["duration"] = 0.0
        wt = Path(attempt["worktree"])
        branch = attempt["branch"]
        # Validate worktree HEAD is on the recorded branch (US9/AC8).
        try:
            current_branch = cli.worktree._git(
                ["rev-parse", "--abbrev-ref", "HEAD"], wt
            ).strip()
        except RuntimeError as exc:
            attempt["status"] = "failed"
            attempt["note"] = f"cannot read worktree HEAD: {exc}"
            return attempt, attempt["note"]
        if current_branch != branch:
            attempt["status"] = "failed"
            attempt["note"] = (
                f"refused: worktree HEAD is on '{current_branch}', "
                f"expected '{branch}'"
            )
            return attempt, attempt["note"]
        start_sha = attempt.get("start_sha")
        # Attempts recorded before project_sha existed started from HEAD.
        project_sha = attempt.get("project_sha") or start_sha
        # Refuse uncommitted changes in main checkout (ignoring coordinator
        # records that crew-chief itself writes during normal operation).
        raw_status = cli.worktree._git(
            ["status", "--porcelain=v1", "-uall"], project, check=False
        )
        outside_lines = [
            line for line in raw_status.splitlines()
            if line.strip()
            and not cli.worktree._is_coordinator_record(line)
            and not cli.worktree._is_untracked_cache(line)
        ]
        if outside_lines:
            attempt["status"] = "failed"
            attempt["note"] = (
                "refused: uncommitted changes in main checkout since piece start"
            )
            return attempt, attempt["note"]
        # Refuse commits on main/job branch since piece start.
        if project_sha:
            try:
                head_main = cli.worktree._git(
                    ["rev-parse", "HEAD"], project
                ).strip()
                if head_main != project_sha:
                    log = cli.worktree._git(
                        ["log", "--oneline", f"{project_sha}..HEAD"],
                        project,
                        check=False,
                    ).strip()
                    attempt["status"] = "failed"
                    attempt["note"] = (
                        "refused: commits on main/job branch since piece start"
                        + (f": {log}" if log else "")
                    )
                    return attempt, attempt["note"]
            except RuntimeError:
                pass
        # Against the starting point, so a correction counts only its own work.
        base = start_sha or "HEAD"
        try:
            rows, destructive = cli.worktree.diffstat(wt, base)
            rows = [r for r in rows if r["file"] not in cli.worktree.EXCLUDED_FILES]
            attempt["diffstat"] = rows
            attempt["destructive_suspect"] = destructive
            changed = bool(rows) or cli.worktree.head_commit(wt) != base
            if changed:
                # FR-049: refuse to commit sensitive paths in the native flow,
                # matching the protection dispatch.py applies to managed runs.
                if prefs.sensitive_paths:
                    try:
                        found = cli.worktree.sensitive_present(wt, prefs.sensitive_paths)
                    except (RuntimeError, OSError) as exc:
                        attempt["status"] = "failed"
                        attempt["note"] = f"sensitive path check failed: {exc}"
                        return attempt, attempt["note"]
                    if found:
                        try:
                            cli.worktree.remove_paths(wt, found)
                        except OSError as exc:
                            attempt["status"] = "failed"
                            attempt["note"] = f"cannot remove sensitive paths: {exc}"
                            return attempt, attempt["note"]
                        attempt["outside_writes"] = [
                            *(attempt.get("outside_writes") or []),
                            *(f"sensitive: {name}" for name in found),
                        ]
                        attempt["status"] = "outside_write"
                        attempt["note"] = (
                            f"sensitive path in worktree: {', '.join(found)}"
                        )
                        return attempt, attempt["note"]
                cli.worktree.commit_worktree(
                    wt,
                    f"crew: {args.job} piece {args.piece} attempt {attempt.get('n')}",
                )
                attempt["end_sha"] = cli.worktree.head_commit(wt)
            else:
                attempt["end_sha"] = cli.worktree.head_commit(wt)
        except RuntimeError as exc:
            attempt["status"] = "failed"
            attempt["note"] = f"cannot compute diff: {exc}"
            return attempt, attempt["note"]
        if not changed:
            attempt["status"] = "no_change"
            return attempt, None
        # FR-043/049: enforce the same spec-proof contract as dispatch.
        # A stale piece or a hands-on task without a proof must be refused
        # so the native flow cannot certify work the managed flow would not.
        ref = piece.get("ref")
        spec_dir = job.get("spec_dir")
        proof_cmd = getattr(args, "proof", None) or ""
        proof_expect = getattr(args, "proof_expect", None) or ""
        if not proof_cmd and ref and spec_dir:
            meta = cli.load_sidecar(project / spec_dir).tasks.get(ref)
            if meta:
                proof_cmd = meta.proof
                proof_expect = meta.expect
            work_type = meta.type if meta else piece.get("type")
            if piece.get("stale"):
                attempt["status"] = "failed"
                attempt["note"] = (
                    f"piece {args.piece} is stale (task {ref} changed); "
                    "re-import the spec"
                )
                return attempt, attempt["note"]
            # FR-043: native finish must enforce the same dependency gate as dispatch.
            if not args.ignore_deps:
                waits = cli._spec_waits(project, job, ref, spec_dir)
                if waits:
                    attempt["status"] = "failed"
                    attempt["note"] = (
                        f"piece {args.piece} ({ref}) waits for {waits}"
                    )
                    return attempt, attempt["note"]
            if not proof_cmd and work_type in cli.records.EXEC_TYPES and not getattr(args, "no_proof", False):
                attempt["status"] = "failed"
                attempt["note"] = (
                    f"piece {args.piece} ({ref}) has no proof in crew.toml; "
                    "add one or pass --no-proof"
                )
                return attempt, attempt["note"]
        if getattr(args, "no_proof", False):
            attempt["proof"] = {"skipped": True}
        elif proof_cmd:
            result, note = _run_proof(
                proof_cmd, proof_expect, wt, prefs.dispatch_timeout_s
            )
            attempt["proof"] = result
            if not result["passed"]:
                attempt["status"] = "proof_failed"
                attempt["note"] = note
                return attempt, note
        attempt["status"] = "ok"
        return attempt, None

    attempt, diag = cli.records.update_job(project, args.job, finalize)
    lease_dir = (
        getattr(prefs, "executor_lease_dir", None)
        or os.environ.get("AGENT_CREW_CHIEF_EXECUTOR_LEASE_DIR")
        or os.environ.get("ACC_EXECUTOR_LEASE_DIR")
    )
    max_running = int(
        os.environ.get("AGENT_CREW_CHIEF_MAX_RUNNING_EXECUTORS")
        or os.environ.get("ACC_MAX_RUNNING_EXECUTORS")
        or getattr(prefs, "max_running_executors", 3)
    )
    slots = cli.ExecutorSlots(lease_dir=lease_dir, max_running=max_running)
    if attempt.get("n"):
        slots.release(owner=f"{args.job}-p{args.piece}-a{attempt['n']}")
    status = attempt["status"]
    text = f"status: {status}"
    if diag:
        text += f"\n{diag}"
    payload = {"piece": args.piece, "status": status, "attempt": attempt.get("n")}
    cli._emit(args, text, payload)
    return 0 if status in ("ok", "no_change") else 1
