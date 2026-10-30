"""Delivery, close, and review commands for the crew-chief CLI."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import date
from pathlib import Path
from .config import Prefs
from .worktree import WorktreeRemovalError, remove_worktree

__all__ = [
    "_cmd_accept",
    "_cmd_close",
    "_cmd_discard",
    "_collect_accepted_pieces",
    "_mark_task_done",
    "_run_review_model",
    "_write_changelog",
]


def _cmd_accept(args: argparse.Namespace) -> int:
    from . import cli

    project = cli._project(args)
    job = cli.records.load_job(project, args.job)
    if job.get("state") == "blocked" or job.get("status") == "blocked":
        print(f"error: job {args.job} is blocked; local commits ceased", file=sys.stderr)
        return 1
    piece = cli._piece(job, args.piece)

    # If the piece is already accepted, only retry cleanup (idempotent).
    if piece.get("accepted"):
        attempt = cli._last_ok_exec_attempt(piece)
        if attempt is None or not attempt.get("worktree"):
            cli._emit(
                args,
                f"nothing to accept on piece {args.piece}",
                {"branch": None, "worktree": None},
            )
            return 1
        try:
            remove_worktree(project, Path(attempt["worktree"]), attempt.get("branch"))
        except WorktreeRemovalError:
            print(
                f"warning: worktree removal failed for {attempt['worktree']}; "
                "cleanup pending",
                file=sys.stderr,
            )
        return 0

    attempt = cli._last_ok_exec_attempt(piece)

    if attempt is None or not attempt.get("branch") or not attempt.get("worktree"):
        cli._emit(
            args,
            f"nothing to accept on piece {args.piece}",
            {"branch": None, "worktree": None},
        )
        return 1

    try:
        cli.accept_branch(project, Path(attempt["worktree"]), attempt["branch"])
    except WorktreeRemovalError:
        print(
            f"warning: worktree removal failed for {attempt['worktree']}; "
            "cleanup pending",
            file=sys.stderr,
        )

    cli.records.update_job(
        project,
        args.job,
        lambda stored: cli.records.mark_accepted(stored, args.piece, attempt),
    )
    cli._emit(
        args,
        f"accepted {attempt['branch']}",
        {"branch": attempt["branch"], "worktree": attempt["worktree"]},
    )
    if piece.get("stale"):
        print(
            f"piece {args.piece} is stale (task {piece.get('ref')} changed); "
            "tasks.md not marked",
            file=sys.stderr,
        )
    elif piece.get("ref") and job.get("spec_dir"):
        cli._mark_task_done(project, job["spec_dir"], piece["ref"])

    # Push the job branch to origin immediately following each accepted piece (FR-027)
    job_branch = job.get("job_branch")
    if job_branch:
        base_branch = job.get("base_branch", "main")
        try:
            cli.push_to_origin(project, job_branch, base_branch=base_branch)
        except subprocess.CalledProcessError as exc:
            # T021 / FR-030: On git push failure, transition to blocked, log exact command and stderr
            cmd_str, stderr_text = cli.handle_git_failure(
                project,
                args.job,
                exc,
                summary=job.get("result", {}).get("summary", ""),
            )
            print(
                f"error: git push failed — {stderr_text.strip()}",
                file=sys.stderr,
            )
            return 1

    return 0


def _mark_task_done(project: Path, spec_dir: str, ref: str) -> None:
    """Tick ``ref`` in the project's ``tasks.md`` and commit just that line.

    The file is read and written without newline translation, so CRLF and BOM
    stay as the user had them. The tick is committed only when ``tasks.md`` was
    tracked and unchanged before it; otherwise it stays in the working tree
    with a warning, so none of the user's other edits land in the commit.
    Problems only warn: the branch is already merged and the piece accepted.
    """
    from . import cli

    path = project / spec_dir / "tasks.md"
    try:
        with open(path, encoding="utf-8", newline="") as handle:
            text = handle.read()
    except OSError:
        print(f"tasks.md not found in {spec_dir}; not marked", file=sys.stderr)
        return
    try:
        marked = cli.mark_done(text, ref)
    except KeyError:
        print(f"task {ref} not found in tasks.md; not marked", file=sys.stderr)
        return
    if marked == text:
        return
    relative = (Path(spec_dir) / "tasks.md").as_posix()
    clean = cli.path_is_clean(project, relative)
    try:
        cli.records.write_atomic(path, marked)
    except OSError as exc:
        print(f"cannot write {path}: {exc}; {ref} not marked", file=sys.stderr)
        return
    if not clean:
        print(
            f"tasks.md has other changes; marked {ref} but did not commit",
            file=sys.stderr,
        )
        return
    try:
        cli.commit_paths(project, [relative], f"docs(tasks): mark {ref} done")
    except RuntimeError as exc:
        print(f"{ref} ticked in tasks.md but not committed: {exc}", file=sys.stderr)


def _cmd_discard(args: argparse.Namespace) -> int:
    from . import cli

    project = cli._project(args)
    job = cli.records.load_job(project, args.job)
    piece = cli._piece(job, args.piece)
    attempt = cli._last_ok_exec_attempt(piece)

    if attempt is None or not attempt.get("branch") or not attempt.get("worktree"):
        cli._emit(
            args,
            f"nothing to discard on piece {args.piece}",
            {"branch": None, "worktree": None},
        )
        return 1

    cli.discard_branch(project, Path(attempt["worktree"]), attempt["branch"])
    prefs = cli.load_prefs(project)
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
    for att in piece.get("attempts") or []:
        if isinstance(att, dict) and att.get("n"):
            if att.get("status") == "running" or att.get("n") == attempt.get("n"):
                slots.release(owner=f"{args.job}-p{args.piece}-a{att['n']}")
    cli._emit(
        args,
        f"discarded {attempt['branch']}",
        {"branch": attempt["branch"], "worktree": attempt["worktree"]},
    )
    return 0


def _collect_accepted_pieces(job: dict) -> list[dict]:
    """Extract accepted piece summaries, grades, and verification proofs for PR audit."""
    from . import cli

    accepted_pieces: list[dict] = []
    for piece in job.get("pieces") or []:
        if piece.get("accepted"):
            grade_str = piece.get("executor_grade")
            if not grade_str:
                grades = cli.records.piece_grades(piece)
                if grades:
                    grade_str = cli.records._piece_credit(piece)
                else:
                    grade_str = "N/A"

            verification_str = piece.get("verification")
            if not verification_str:
                acc_info = piece.get("accepted")
                if isinstance(acc_info, dict):
                    att_n = acc_info.get("attempt")
                    att = next(
                        (a for a in piece.get("attempts", []) if a.get("n") == att_n),
                        None,
                    )
                    if att:
                        proof = att.get("proof")
                        if isinstance(proof, dict):
                            from .cli_dispatch import _proof_text
                            proof = _proof_text(proof)
                        verification_str = att.get("verification") or proof or att.get("note")
            if not verification_str:
                verification_str = "not recorded"

            item = {
                "n": piece.get("n"),
                "title": piece.get("title", f"Piece {piece.get('n')}"),
                "status": "accepted",
                "executor_grade": grade_str,
                "verification": verification_str,
            }
            if piece.get("type"):
                item["type"] = piece["type"]
            elif piece.get("ref") and job.get("spec_dir"):
                try:
                    sidecar = cli.load_sidecar(Path(job.get("project", ".")) / job["spec_dir"])
                    meta = sidecar.tasks.get(piece["ref"])
                    if meta and meta.type:
                        item["type"] = meta.type
                except Exception:
                    pass
            accepted_pieces.append(item)
    return accepted_pieces


def _run_review_model(
    project: Path,
    prefs: Prefs,
    reviewer_model: str,
    job: dict,
    job_branch: str,
    base_branch: str,
) -> str:
    """Run the selected review model via configured CLI/tool machinery."""
    from . import cli

    if ":" in reviewer_model:
        tool_name, model_name = reviewer_model.split(":", 1)
    else:
        tool_name, model_name = reviewer_model, ""

    tool = next(
        (t for t in prefs.tools if t.get("name") == tool_name or t.get("kind") == tool_name),
        None,
    )
    if tool is None:
        tool = {"name": tool_name, "kind": tool_name, "bin": tool_name}

    brief_file = Path(project) / ".crew-chief-review-brief.md"
    output_file = Path(project) / ".crew-chief-review-out.txt"
    brief_content = (
        f"# Pull Request Review Request\n\n"
        f"Job: {job.get('id')}\n"
        f"Branch: {job_branch}\n"
        f"Base: {base_branch}\n\n"
        f"Please review the changes on branch '{job_branch}' against '{base_branch}'."
    )
    brief_file.write_text(brief_content, encoding="utf-8")

    prompt = (
        f"Read the file {brief_file} and review the changes for branch {job_branch}. "
        f"Do not modify any file; reply with your review."
    )

    cmd = cli.build_command(
        tool,
        model_name,
        Path(project),
        prompt,
        brief_file,
        output_file,
        effort=None,
        review=True,
    )

    env = cli.tool_env(tool)
    if env is None:
        env = os.environ.copy()

    runner = getattr(cli, "subprocess", subprocess)
    try:
        proc = runner.run(
            cmd,
            cwd=project,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=prefs.dispatch_timeout_s or 60,
            check=False,
        )
        stdout = proc.stdout or ""
        if not stdout.strip() and output_file.exists():
            stdout = output_file.read_text(encoding="utf-8", errors="replace")
        review_text = cli.output_text(tool, stdout)
        if not review_text.strip() and proc.stderr:
            review_text = proc.stderr.strip()
        return review_text
    finally:
        if brief_file.exists():
            try:
                brief_file.unlink()
            except OSError:
                pass
        if output_file.exists():
            try:
                output_file.unlink()
            except OSError:
                pass


def _cmd_close(args: argparse.Namespace) -> int:
    from . import cli

    project = cli._project(args)
    job = cli.records.load_job(project, args.job)
    if job.get("state") == "blocked" or job.get("status") == "blocked":
        print(f"error: job {args.job} is blocked; local commits ceased", file=sys.stderr)
        return 1
    if args.type:

        def store_type(stored: dict) -> dict:
            stored["commit_type"] = args.type
            return stored

        job = cli.records.update_job(project, args.job, store_type)
    written = None if args.no_changelog else cli._write_changelog(project, job)
    commit, issue = cli.records.summaries(job, written[0] if written else None)
    cli._write_scoreboard(project, cli.render_scoreboard(project))
    text = f"{commit}\n\n---\n\n{issue}"
    payload = {"commit": commit, "issue": issue}
    if cli.economy.has_data(job):
        figures = cli.economy.job_economy(
            job, cli.load_prefs(project), spent_today=cli.spend.spent(date.today())
        )
        text = "\n".join([*cli.economy.report_lines(figures), text])
        payload["economy"] = figures.as_dict()
    if written:
        text = f"changelog: {written[0]} ({written[1]})\n{text}"
        payload["changelog"] = {"path": written[0], "category": written[1]}

    # Pipeline jobs: open PR via gh pr create at job completion (FR-028, US6, US8)
    job_branch = job.get("job_branch")
    if job_branch:
        base_branch = job.get("base_branch", "main")
        end_mode = (
            getattr(args, "end_mode", None)
            or job.get("end_mode")
            or (job.get("interview", {}).get("answers", {}).get("end_mode") or {}).get("value")
            or "pr"
        )
        pr_url = job.get("pr_url")
        if end_mode == "pr_review":
            # T020 / FR-029: post a review comment from a distinct reviewer model
            prefs = cli.load_prefs(project)
            executor_models = []
            for piece in job.get("pieces") or []:
                for attempt in piece.get("attempts") or []:
                    if attempt.get("role") == "exec":
                        ex = attempt.get("executor")
                        if not ex or not str(ex).strip():
                            raise ValueError("Executor attempt is missing model identity; cannot verify distinctness")
                        executor_models.append(str(ex).strip())

            coord_env = os.environ.get(cli._COORDINATOR_ENV, "").strip()
            coord_arg = (getattr(args, "coordinator", None) or "").strip()
            coord_prefs = [str(c).strip() for c in (prefs.team.coordinators or []) if str(c).strip()]

            all_coordinators = set()
            if coord_arg:
                all_coordinators.add(coord_arg)
            if coord_env:
                all_coordinators.add(coord_env)
            all_coordinators.update(coord_prefs)

            if not all_coordinators:
                raise ValueError("Coordinator model identity is missing; cannot verify distinctness")

            def _norm(s: str) -> str:
                if s.startswith("native:"):
                    rest = s[len("native:"):]
                    if "/" in rest:
                        cli_tool, model = rest.split("/", 1)
                        return f"{cli_tool}:{model}"
                return s

            def _matches_identity(a: str, b: str) -> bool:
                a = _norm(a)
                b = _norm(b)
                if a == b:
                    return True
                if ":" in a and ":" not in b:
                    return a.split(":", 1)[1] == b
                if ":" not in a and ":" in b:
                    return a == b.split(":", 1)[1]
                return False

            excluded_exact = set(all_coordinators) | set(executor_models)

            candidate_reviewers = list(prefs.team.reviewers)
            if not candidate_reviewers:
                raise ValueError("No team reviewers configured; cannot run pr_review")

            reviewers = []
            for r in candidate_reviewers:
                r_str = str(r).strip()
                if not r_str:
                    continue  # empty identity must never bypass distinctness
                if any(_matches_identity(r_str, ex) for ex in excluded_exact):
                    continue
                reviewers.append(r_str)

            if not reviewers:
                raise ValueError(
                    f"no distinct reviewer available: reviewers={candidate_reviewers}, "
                    f"excluded={excluded_exact}"
                )
            reviewer_model = reviewers[0]
            coordinator_model = coord_arg or (coord_prefs[0] if coord_prefs else coord_env)

            if not pr_url:
                accepted_pieces = cli._collect_accepted_pieces(job)
                try:
                    pr_url = cli.create_pr(
                        project,
                        job["id"],
                        job_branch,
                        base_branch=base_branch,
                        accepted_pieces=accepted_pieces,
                        spec_dir=job.get("spec_dir") or job.get("spec_path") or (job.get("authorization") or {}).get("spec_path"),
                    )
                except ValueError as exc:
                    print(f"error: {exc}", file=sys.stderr)
                    return 1
                except subprocess.CalledProcessError as exc:
                    # T021 / FR-030: On gh pr create failure, transition to blocked
                    cmd_str, stderr_text = cli.handle_git_failure(
                        project,
                        args.job,
                        exc,
                        summary=issue,
                    )
                    print(
                        f"error: gh pr create failed — {stderr_text.strip()}",
                        file=sys.stderr,
                    )
                    return 1

            review_text = cli._run_review_model(
                project,
                prefs,
                reviewer_model,
                job,
                job_branch,
                base_branch,
            )

            cli.create_pr_review_comment(
                project,
                job["id"],
                job_branch,
                base_branch=base_branch,
                reviewer_model=reviewer_model,
                coordinator_model=coordinator_model,
                executor_models=executor_models,
                review_text=review_text,
            )

            def store_pr_review(stored: dict) -> dict:
                stored["pr_url"] = pr_url
                stored["state"] = "pr_review_posted"
                stored["status"] = "pr_review_posted"
                stored["result"] = {
                    "pr_url": pr_url,
                    "reviewer_model": reviewer_model,
                    "review_text": review_text,
                    "summary": issue,
                    "team_used": list(stored.get("team_used", [])),
                }
                return stored

            job = cli.records.update_job(project, job["id"], store_pr_review)
            text = f"PR review posted: {pr_url}\n{text}"
            payload["pr_url"] = pr_url
            payload["state"] = job.get("state", "pr_review_posted")
            payload["status"] = job.get("status", "pr_review_posted")
        else:
            if not pr_url:
                accepted_pieces = cli._collect_accepted_pieces(job)
                try:
                    pr_url = cli.create_pr(
                        project,
                        job["id"],
                        job_branch,
                        base_branch=base_branch,
                        accepted_pieces=accepted_pieces,
                        spec_dir=job.get("spec_dir") or job.get("spec_path") or (job.get("authorization") or {}).get("spec_path"),
                    )
                except ValueError as exc:
                    print(f"error: {exc}", file=sys.stderr)
                    return 1
                except subprocess.CalledProcessError as exc:
                    # T021 / FR-030: On gh pr create failure, transition to blocked
                    cmd_str, stderr_text = cli.handle_git_failure(
                        project,
                        args.job,
                        exc,
                        summary=issue,
                    )
                    print(
                        f"error: gh pr create failed — {stderr_text.strip()}",
                        file=sys.stderr,
                    )
                    return 1

                job = cli.records.transition_terminal_state(
                    project=project,
                    job_id=job["id"],
                    new_state="pr_open",
                    extra_fields={
                        "pr_url": pr_url,
                        "result": {
                            "pr_url": pr_url,
                            "summary": issue,
                            "tests": job.get("tests", {"passed": len(accepted_pieces), "failed": 0}),
                            "team_used": list(job.get("team_used", [])),
                        },
                    },
                )
            text = f"PR: {pr_url}\n{text}"
            payload["pr_url"] = pr_url
            payload["state"] = job.get("state", "pr_open")
            payload["status"] = job.get("status", "pr_open")
            payload["result"] = job.get(
                "result",
                {"pr_url": pr_url, "summary": issue, "tests": job.get("tests")},
            )

    cli._emit(args, text, payload)
    return 0


def _write_changelog(project: Path, job: dict) -> tuple[str, str] | None:
    """Add the job to the project CHANGELOG; ``(path, category)`` when written.

    Nothing is written when the preference is ``none``, no piece of the job was
    accepted, the job's commit type has no category, or the job's entry is
    already in the file (``close`` run twice). The file is read and written
    without newline translation, so its CRLF and BOM survive.
    """
    from . import cli

    prefs = cli.load_prefs(project)
    if prefs.changelog == "none":
        return None
    if not any(piece.get("accepted") for piece in job.get("pieces") or []):
        return None
    category = cli.CATEGORY_BY_COMMIT.get(cli.records.commit_type(job))
    if category is None:
        return None

    relative = prefs.changelog_path
    path = (project / relative).resolve()
    if not path.is_relative_to(project.resolve()) or path.is_dir():
        raise ValueError(
            f"changelog_path {relative!r} must be a file inside the project"
        )
    try:
        old = None
        if path.is_file():
            with path.open(encoding="utf-8", newline="") as handle:
                old = handle.read()
        new = cli.insert_entry(old, category, cli.entry_line(job))
        if new == old:
            return None
        cli.records.write_atomic(path, new)
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"changelog: cannot update {path}: {exc}") from exc
    return path.relative_to(project.resolve()).as_posix(), category
