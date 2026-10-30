"""Pick, dispatch, and proof commands for the crew-chief CLI."""

from __future__ import annotations

import argparse
import os
import random
from pathlib import Path
from typing import TYPE_CHECKING

from .scoring_effort import model_effort

if TYPE_CHECKING:
    from . import cli

__all__ = [
    "_cmd_dispatch",
    "_cmd_pick",
    "_proof_text",
    "_spec_proof",
    "_spec_waits",
    "_wait_target",
]


def _cmd_pick(args: argparse.Namespace) -> int:
    from . import cli

    project = cli._project(args)
    prefs = cli.load_prefs(project)
    found = cli.discover_full(prefs)
    executors = found.executors
    cli._piece(cli.records.load_job(project, args.job), args.piece)
    cli._check_models_to_stderr(project, prefs, found, due_only=True)
    stats = cli.build_stats(project)
    cooling = cli.quota_cooldown(
        project, prefs.quota_cooldown_min, executors=[e.id for e in executors]
    )
    coordinator = args.coordinator or os.environ.get(cli._COORDINATOR_ENV) or None

    def choose(job: dict) -> tuple:
        """Pick under the job lock, so parallel picks see each other's picks.

        With ``--review`` the pick is for a reviewer: the work type is
        ``review``, failed review attempts are left out instead of failed exec
        attempts, and so is every executor in the quota family of an executor
        with an ``ok`` exec attempt on the piece. Two free failures send
        thinking work to the next tier on their own; execution work only
        climbs when its tier is exhausted and reaches ``paid`` only with
        ``--strong``.
        """
        piece = cli._piece(job, args.piece)
        role = "review" if args.review else "exec"
        work_type = "review" if args.review else piece.get("type", "")
        exclude = set(args.exclude or [])
        reviewed: set[str] = set()
        for attempt in piece.get("attempts") or []:
            executor_id = attempt.get("executor")
            if not isinstance(executor_id, str) or not executor_id:
                continue
            if args.review and attempt.get("role") == "exec" and attempt.get(
                "status"
            ) == "ok":
                reviewed.add(executor_id)
            if attempt.get("role") != role or attempt.get("status") == "ok":
                continue
            exclude.add(executor_id)
        # A native exec id (subagents mode) names the same model as its CLI
        # id: keep both so the quota-family exclusion below sees the model.
        for executor_id in list(reviewed):
            if executor_id.startswith("native:"):
                rest = executor_id[len("native:"):]
                if "/" in rest:
                    cli_name, model = rest.split("/", 1)
                    if cli_name and model:
                        reviewed.add(f"{cli_name}:{model}")
        for executor_id in list(exclude):
            if executor_id.startswith("native:"):
                rest = executor_id[len("native:"):]
                if "/" in rest:
                    cli_name, model = rest.split("/", 1)
                    if cli_name and model:
                        exclude.add(f"{cli_name}:{model}")
        if not args.review:
            # Work that drew a correction goes to someone else for the fix.
            exclude |= {
                correction.get("executor")
                for correction in piece.get("corrections") or []
                if isinstance(correction, dict) and correction.get("executor")
            }
        costs = {executor.id: executor.cost for executor in executors}
        families = {
            cli.quota_family(executor_id, costs.get(executor_id, ""))
            for executor_id in reviewed
        }
        # FR-042: the quota-family exclusion prevents the same underlying
        # model from reviewing its own work.  For ``included``-cost executors
        # the family is ``sub:<tool>``, which covers every model of that CLI
        # and over-blocks: a review by claude:sonnet would also exclude
        # claude:opus even though they are distinct models.  In ``own`` and
        # ``subagents`` modes we fall back to excluding only the exact
        # reviewed executor IDs plus any other executor whose *model-level*
        # family matches (so different effort tiers of the same model are
        # still excluded).  In ``free_first`` the whole family stays out.
        included_families = {f for f in families if f.startswith("sub:")}
        if included_families and cli.records.job_executors(job) in (
            "own",
            "subagents",
        ):
            exclude |= reviewed
            model_families = {
                cli.quota_family(eid, "")
                for eid in reviewed
            }
            exclude |= {
                executor.id
                for executor in executors
                if cli.quota_family(executor.id, "") in model_families
            }
        else:
            exclude |= {
                executor.id
                for executor in executors
                if cli.quota_family(executor.id, executor.cost) in families
            }
        # One model behind the two OpenCode routes is one quota whatever its
        # cost, which the family above folds into ``sub:opencode`` for
        # ``included``: review never lands on the author's own model again.
        same_model = {
            cli.quota_family(executor_id)
            for executor_id in reviewed
            if executor_id.startswith(cli._OPENCODE_ROUTE_IDS)
        }
        exclude |= {
            executor.id
            for executor in executors
            if cli.quota_family(executor.id) in same_model
        }

        escalation = ""
        if (
            not args.review
            and work_type not in cli.records.EXEC_TYPES
            and cli.free_failures(job, args.piece) >= 2
        ):
            escalation = cli._ESCALATION_FREE_FAILURES
        elif args.strong:
            escalation = cli._ESCALATION_STRONG
        if escalation:
            exclude |= {
                executor.id for executor in executors if executor.cost == "free"
            }

        # FR-042: in own/subagents review, pass the executor being reviewed
        # so pick() can enforce the different-model rule inside those modes.
        review_for = next(iter(reviewed), None) if args.review and reviewed else None
        raw_allowlist = prefs.team.reviewers if args.review else prefs.team.executors
        allowlist = set(raw_allowlist) if raw_allowlist else None
        choice, reason = cli.pick_executor(
            executors,
            stats,
            work_type,
            piece.get("risk", ""),
            prefs,
            exclude,
            random.Random(),
            job=job,
            cooling=cooling,
            strong=args.strong,
            piece=args.piece,
            budget=cli.spend.BudgetCheck(prefs, job["id"]),
            coordinator_tool=coordinator,
            allow_own_cli=args.allow_own_cli,
            executor_mode=cli.records.job_executors(job),
            review_for_executor=review_for,
            allowlist=allowlist,
        )
        if reviewed:
            reason += (
                f"; reviewer outside the family of: {', '.join(sorted(reviewed))}"
            )

        if escalation:
            cli.records.add_escalation(
                job, args.piece, escalation, choice.id if choice else ""
            )
        if choice is not None:
            entry = {"pick": choice.id, "reason": reason, "at": cli._now_iso()}
            if args.review:
                entry["role"] = "review"
            piece.setdefault("picks", []).append(entry)

            pick_role = "reviewer" if args.review else "executor"
            if raw_allowlist:
                team_section = "reviewers" if args.review else "executors"
                pick_origin = (
                    prefs.team.origin(team_section)
                    or cli.config.team_origin(project, team_section)
                    or "project_team"
                )
            else:
                pick_origin = "discovery"

            cli.records.record_team_used(
                job,
                role=pick_role,
                id=choice.id,
                origin=pick_origin,
                piece=args.piece,
            )
        effort = (
            model_effort(choice.tool, choice.model)
            or cli.picked_effort(choice, job, args.piece)
            if choice is not None
            else None
        )
        return choice, reason, effort

    choice, reason, effort = cli.records.update_job(project, args.job, choose)

    if choice is None:
        cli._emit(args, reason, {"executor": None, "reason": reason})
        return 1

    text = f"{choice.id}\n{reason}"
    if effort is not None:
        text += f"\neffort: {effort}"
    cli._emit(
        args,
        text,
        {"executor": choice.id, "reason": reason, "effort": effort},
    )
    return 0


def _cmd_dispatch(args: argparse.Namespace) -> int:
    from . import cli

    project = cli._project(args)
    job = cli.records.load_job(project, args.job)
    if job.get("state") == "blocked" or job.get("status") == "blocked":
        raise ValueError(f"job {args.job} is blocked; local commits ceased")
    if args.review and not args.target:
        raise ValueError(
            "--review requires --target (the attempt worktree to review) "
            "and --brief-file"
        )
    if args.review and (args.base or args.fresh):
        raise ValueError("--base and --fresh are for exec attempts, not --review")
    if args.review and args.no_proof:
        raise ValueError("--no-proof is for exec attempts, not --review")
    if args.review and args.ignore_deps:
        raise ValueError("--ignore-deps is for exec attempts, not --review")
    proof, expect, files, ignored_deps = cli._spec_proof(project, args)

    prefs = cli.load_prefs(project)
    found = cli.discover_full(prefs)
    executor = next(
        (item for item in found.executors if item.id == args.executor), None
    )
    if executor is None:
        raise ValueError(f"unknown executor {args.executor!r}")

    if executor.cost == "cheap":
        # The same budget check as ``pick``: a pick made earlier may be stale.
        blocked = cli.spend.BudgetCheck(prefs, args.job)(executor)
        if blocked is not None:
            raise ValueError(f"{executor.id}: {blocked}")

    brief = cli._read_text(Path(args.brief_file), "--brief-file")
    cli._check_models_to_stderr(project, prefs, found, due_only=True)
    # A model id with an effort suffix fixes the effort; build_command adds no flag.
    effort = model_effort(executor.tool, executor.model) or args.effort
    if effort is None:
        # The effort ``pick`` printed: lowest first, one step up per failure.
        effort = cli.picked_effort(
            executor, cli.records.load_job(project, args.job), args.piece
        )
    attempt = cli.run_attempt(
        project,
        args.job,
        args.piece,
        executor,
        brief,
        prefs,
        role="review" if args.review else "exec",
        effort=effort,
        timeout_s=args.timeout,
        review_target=(
            Path(args.target).expanduser().resolve() if args.target else None
        ),
        base=args.base,
        fresh=args.fresh,
        proof=proof,
        proof_expect=expect,
        allowed_files=files,
        skip_proof=args.no_proof,
        ignored_deps=ignored_deps,
        superpowers=getattr(args, "superpowers", None),
    )

    status = attempt["status"]
    lines = [
        f"status: {status}",
        f"branch: {attempt['branch'] or '-'}",
        f"worktree: {attempt['worktree'] or '-'}",
        f"base: {cli._base_text(attempt)}",
        "destructive_suspect: "
        + (", ".join(attempt["destructive_suspect"]) or "-"),
        "outside_writes: " + (", ".join(attempt["outside_writes"]) or "-"),
        f"duration: {attempt['duration_s']}s",
        f"steps: {'-' if attempt['steps'] is None else attempt['steps']}",
        f"tokens: {cli._tokens_text(attempt)}",
    ]
    if "proof" in attempt:
        lines.append(f"proof: {cli._proof_text(attempt['proof'])}")
    if attempt.get("scope"):
        lines.append("scope: " + ", ".join(attempt["scope"]))
    payload = {
        "status": status,
        "branch": attempt["branch"],
        "worktree": attempt["worktree"],
        "base": attempt.get("base"),
        "destructive_suspect": attempt["destructive_suspect"],
        "outside_writes": attempt["outside_writes"],
        "duration_s": attempt["duration_s"],
        "steps": attempt["steps"],
        "tokens_in": attempt["tokens_in"],
        "tokens_out": attempt["tokens_out"],
        "tokens_total": attempt["tokens_total"],
        "tokens_estimated": attempt["tokens_estimated"],
    }
    if "proof" in attempt:
        payload["proof"] = attempt["proof"]
    if attempt.get("scope"):
        payload["scope"] = attempt["scope"]
    if args.review:
        answer = (attempt.get("note") or "")[-cli._REVIEW_ANSWER_CHARS:]
        lines += ["answer:", answer.rstrip("\n") or "-"]
        payload["answer"] = answer
    cli._emit(args, "\n".join(lines), payload)
    return 0 if status == "ok" else 1


def _proof_text(proof: dict) -> str:
    """``skipped``, ``passed``, ``failed (exit 3)`` or ``failed (no exit)``."""
    if proof.get("skipped"):
        return "skipped"
    if proof.get("passed"):
        return "passed"
    code = proof.get("exit")
    return "failed (no exit)" if code is None else f"failed (exit {code})"


def _wait_target(reason: str) -> str:
    """What a :func:`readiness` reason waits for: ``T012`` or ``phase 'X'``."""
    if reason.startswith("depends on "):
        return reason[len("depends on "):]
    return reason.removesuffix(" incomplete")


def _spec_waits(project: Path, job: dict, ref: str, spec_dir: str) -> str:
    """What the spec task ``ref`` still waits for, or ``""`` when it may start.

    Reuses :func:`readiness` (dependencies from the sidecar, then earlier
    phases); a ``spec_dir`` without a ``tasks.md`` blocks nothing.
    """
    from . import cli

    tasks_path = project / spec_dir / "tasks.md"
    if not tasks_path.is_file():
        return ""
    tasks = cli.parse_tasks(cli._read_text(tasks_path, "tasks.md"))
    reason = cli.readiness(job, tasks, cli.load_sidecar(project / spec_dir)).get(ref, "")
    return cli._wait_target(reason) if reason else ""


def _spec_proof(
    project: Path, args: argparse.Namespace
) -> tuple[str, str, list[str] | None, list[str] | None]:
    """The proof, expected text, allowed files and ignored waits of a ``dispatch``.

    Only a piece bound to a spec task (``ref``) of a job with a ``spec_dir``
    has any: they come from the task's ``crew.toml`` entry. A stale piece is
    refused for exec and review alike. An exec waiting for an unaccepted
    dependency or an incomplete earlier phase is refused unless
    ``--ignore-deps`` was given, which returns what it ignored. An exec of a
    hands-on task (see :data:`records.EXEC_TYPES`) without a proof is refused
    unless ``--no-proof`` was given. Anything else keeps the v0.2.0 behaviour.
    """
    from . import cli

    job = cli.records.load_job(project, args.job)
    piece = cli._piece(job, args.piece)
    ref = piece.get("ref")
    if piece.get("stale"):
        raise ValueError(
            f"piece {args.piece} is stale (task {ref} changed); re-import the spec"
        )
    spec_dir = job.get("spec_dir")
    if not ref or not spec_dir:
        return "", "", None, None
    meta = cli.load_sidecar(project / spec_dir).tasks.get(ref)
    tasks_path = project / spec_dir / "tasks.md"
    if meta is not None and tasks_path.is_file():
        task = next(
            (t for t in cli.parse_tasks(cli._read_text(tasks_path, "tasks.md")) if t.id == ref),
            None,
        )
        if task is not None:
            cli._check_entry(ref, meta, task.text, f"piece {args.piece} ({ref}): ")
    if args.review:
        return "", "", None, None
    waits = cli._spec_waits(project, job, ref, spec_dir)
    ignored_deps = None
    if waits:
        if not args.ignore_deps:
            raise ValueError(f"piece {args.piece} ({ref}) waits for {waits}")
        ignored_deps = [waits]
    work_type = meta.type if meta else piece.get("type")
    proof = meta.proof if meta else ""
    if not proof and work_type in cli.records.EXEC_TYPES and not args.no_proof:
        raise ValueError(
            f"piece {args.piece} ({ref}) has no proof in crew.toml; "
            "add one or pass --no-proof"
        )
    expect = meta.expect if meta else ""
    files = list(meta.files) if meta and meta.files else None
    return proof, expect, files, ignored_deps
