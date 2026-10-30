"""Command-line interface for agent-crew-chief.

Every subcommand wires the modules of the package together in the order the
coordinator uses them: ``discover`` lists the executors present right now,
``job new``/``piece add`` build the record, ``pick`` chooses an executor,
``dispatch`` runs one attempt, ``correction``/``grade`` close the loop and
``accept``/``discard``/``close`` land or drop the work.

``spec new``/``spec import``/``spec status`` connect spec-kit folders: ``import``
turns the open tasks of ``tasks.md`` into pieces (``ref`` = task id) and keeps the
``crew.toml`` sidecar; ``status`` lists what is ready or blocked. ``brief render``
turns one spec piece into the minimal brief an executor or reviewer receives.

``ruling add`` records a one-line ruling in the job; ``decision add`` promotes a
ruling (or a title) to an ADR file in ``docs/decisions``.

``usage add`` records the coordinator's own token spend in the job;
``economy`` prints the job's economy summary and alerts, and ``close`` prints the
same block before the commit message.

``launch`` starts one coordinator per front for the maestro (see
:mod:`maestro`); ``status``, ``resume`` and ``stop`` follow and steer them.

``setup`` detects the executors and the logins this machine has, asks only what
it could not deduce and writes one managed block (``cheap_budget_usd_day``,
``forbid_training`` and the ``[[models]]`` rules that promote a paid-for
subscription to ``included``) into the global preferences file. Without
``--yes`` it needs a terminal.

All commands take ``--project PATH`` (default: the current directory) and
``--json`` (structured output instead of text). A ``PrefsError``,
``RecordError``, ``NotAGitRepo`` or ``ValueError`` prints one line to stderr
and exits 2; exit 1 means "nothing to do" (no executor, an attempt that is not
``ok``, nothing to accept) and exit 0 means success. A ``RuntimeError`` from a
git or worktree operation (a merge conflict, a taken worktree path) is an
operational failure: one ``error: ...`` line on stderr and exit 1.

Every command that runs discovery also keeps ``docs/crew-chief/models-seen.json``
up to date and reports the models that appeared, disappeared or changed cost:
``discover`` always (on stdout, after the list, before the closing
``work types:`` lines of each tool), ``job new`` always (on
stderr, so the job id stays alone on stdout), ``pick`` and ``dispatch`` only
when the last check is ``model_check_hours`` old (on stderr). A failing check
never fails the command.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import random
import re
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__, economy, maestro, records, setup, spend
from .brief import render_converge_brief, render_exec_brief, render_review_brief
from .changelog import CATEGORY_BY_COMMIT, entry_line, insert_entry
from .config import (
    Prefs,
    PrefsError,
    default_global_path,
    load_prefs,
    tool_work_types,
)
from .decisions import DECISIONS_DIR, next_number, write_adr
from .discover import Discovery, discover_full
from .dispatch import dispatch as run_attempt
from .dispatch import resolve_base
from .models_seen import changes_payload, check_due, check_models, render_changes
from .records import RecordError
from .scoring import (
    build_stats,
    free_failures,
    grade as score_piece,
    pick as pick_executor,
    picked_effort,
    quota_cooldown,
    quota_family,
    render_scoreboard,
)
from .sidecar import Sidecar, SidecarError, TaskMeta, load_sidecar, update_sidecar
from .speckit import (
    SpecError,
    SpecTask,
    find_spec_dir,
    guess_risk,
    guess_type,
    mark_done,
    parse_spec_refs,
    parse_tasks,
    readiness,
    resolve_refs,
)
from . import worktree
from .worktree import NotAGitRepo, commit_paths, merge_base, path_is_clean, short_sha
from .worktree import accept as accept_branch
from .worktree import discard as discard_branch
from .executor_slots import ExecutorSlots, LeaseError
from .git_delivery import create_pr, create_pr_review_comment, handle_git_failure, push_to_origin
from .tools import build_command, output_text, tool_env

#: Path of the committed scoreboard, relative to the project root.
_SCOREBOARD_RELATIVE_PATH = Path("docs") / "crew-chief" / "scoreboard.md"

#: Executor-id prefixes of the two OpenCode routes that reach one model
#: under one quota (see :func:`crew_chief.scoring.quota_family`).
_OPENCODE_ROUTE_IDS = ("opencode:opencode/", "opencode:opencode-go/")

#: How much of the reviewer's answer (the end of the attempt note) ``dispatch
#: --review`` prints.
_REVIEW_ANSWER_CHARS = 3000

#: Environment variable naming the CLI that is coordinating (``claude``,
#: ``codex``...): its ``included`` executors go last in ``pick``.
_COORDINATOR_ENV = "CREW_CHIEF_COORDINATOR"

#: Escalation reasons recorded by ``pick``.
_ESCALATION_FREE_FAILURES = "2 free failures"
_ESCALATION_STRONG = "--strong: complex, cannot be split"


# --- helpers ---------------------------------------------------------------


def _reconfigure_streams() -> None:
    """Force UTF-8 on stdout and stderr so accented text survives on Windows."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


def _project(args: argparse.Namespace) -> Path:
    """The project to act on: ``--project`` or the current directory."""
    if args.project:
        return Path(args.project).expanduser().resolve()
    return Path.cwd()


def _has_tty() -> bool:
    """Whether stdin is an interactive terminal (``setup`` needs one)."""
    try:
        return bool(sys.stdin.isatty())
    except (AttributeError, ValueError):
        return False


def _emit(args: argparse.Namespace, text: str, payload: dict) -> None:
    """Print ``text``, or ``payload`` as JSON when ``--json`` was given."""
    if args.json:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    else:
        sys.stdout.write(text + "\n")


def _read_text(path: Path, label: str) -> str:
    """Read ``path`` as UTF-8; a read failure becomes a :class:`ValueError`."""
    try:
        return path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ValueError(f"{label}: cannot read {path}: {exc}") from exc


def _now_iso() -> str:
    """The current UTC instant as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _piece(job: dict, number: int) -> dict:
    """The piece numbered ``number``; :class:`ValueError` when it is absent."""
    for piece in job.get("pieces") or []:
        if piece.get("n") == number:
            return piece
    raise ValueError(f"job {job.get('id')!r} has no piece {number}")


def _write_scoreboard(project: Path, text: str) -> Path:
    """Write the rendered scoreboard into the project and return its path."""
    path = project / _SCOREBOARD_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _tokens_text(attempt: dict) -> str:
    """``12345 (in 12000 / out 345)``, ``~120 (estimated)`` or ``-``."""
    total = attempt.get("tokens_total")
    if total is None:
        return "-"
    if attempt.get("tokens_estimated"):
        return f"~{total} (estimated)"
    if attempt.get("tokens_in") is None or attempt.get("tokens_out") is None:
        return str(total)
    return f"{total} (in {attempt['tokens_in']} / out {attempt['tokens_out']})"


def _base_text(attempt: dict) -> str:
    """``crew/... (attempt 1)``, a bare branch, ``HEAD`` or ``-`` for a review."""
    if attempt.get("role") != "exec":
        return "-"
    base = attempt.get("base")
    if not base:
        return "HEAD"
    if base.get("attempt") is None:
        return str(base.get("branch"))
    return f"{base.get('branch')} (attempt {base['attempt']})"


def _last_ok_exec_attempt(piece: dict) -> dict | None:
    """The piece's last ``exec`` attempt with status ``ok``, or ``None``."""
    for attempt in reversed(piece.get("attempts") or []):
        if attempt.get("role") == "exec" and attempt.get("status") == "ok":
            return attempt
    return None


def _work_types_summary(prefs: Prefs, executors: list) -> dict[str, list[str]]:
    """Effective work types of every tool that has an executor, in tool order."""
    present = {executor.tool.get("name") for executor in executors}
    return {
        tool["name"]: list(tool_work_types(tool))
        for tool in prefs.tools
        if tool["name"] in present
    }


def _work_types_line(name: str, types: list[str], prefs: Prefs) -> str:
    """One ``work types: <tool>: ...`` line of ``discover``."""
    if tuple(types) == records.WORK_TYPES:
        text = "all types"
    elif tuple(types) == records.EXEC_TYPES:
        text = f"exec only ({', '.join(types)})"
    elif tuple(types) == records.THINK_TYPES:
        text = f"think only ({', '.join(types)})"
    else:
        text = ", ".join(types)
    if name in prefs.exec_first and set(types) & set(records.EXEC_TYPES):
        text += ", first on execution work"
    return f"work types: {name}: {text}"


# --- commands --------------------------------------------------------------


def _cmd_interview(args: argparse.Namespace) -> int:
    """Create or inspect an interview-mode job (T036, FR-036)."""
    from . import interview_service

    projects_dir = getattr(args, "projects_dir", None)
    project = getattr(args, "project", None)

    if getattr(args, "job", None):
        job_id = args.job
        if getattr(args, "answer", None):
            key, sep, value = args.answer.partition("=")
            if not sep or not key.strip():
                raise ValueError("--answer must be KEY=VALUE")
            res = interview_service.answer_interview_job(
                project=project, job_id=job_id, key=key.strip(), value=value.strip(),
                projects_dir=projects_dir,
            )
            pending_str = ", ".join(res["pending"]) if res["pending"] else "none"
            _emit(args, f"job: {job_id}\nstate: {res['state']}\npending: {pending_str}", res)
            return 0
        if getattr(args, "cursor", None) is not None:
            res = interview_service.get_interview_events(
                project=project, job_id=job_id, cursor=args.cursor, projects_dir=projects_dir
            )
            text = f"job: {job_id}\nevents: {len(res['events'])}\ncursor: {res['cursor']}"
            _emit(args, text, res)
            return 0

        res = interview_service.get_interview_status(
            project=project, job_id=job_id, projects_dir=projects_dir
        )
        pending_str = ", ".join(res["pending"]) if res["pending"] else "none"
        text = f"job: {job_id}\nstate: {res['state']}\npending: {pending_str}"
        _emit(args, text, res)
        return 0

    return _handle_start_job_cli(args)


def _handle_start_job_cli(args: argparse.Namespace) -> int:
    from . import interview_service

    projects_dir = getattr(args, "projects_dir", None)
    project = getattr(args, "project", None) or getattr(args, "repo", None)

    request_text = None
    if getattr(args, "request_file", None):
        request_text = _read_text(Path(args.request_file), "--request-file")
    elif getattr(args, "request", None):
        request_text = args.request

    caller_answers: dict[str, Any] = {}
    if project:
        caller_answers["project"] = str(project)
    if getattr(args, "coordinator", None):
        caller_answers["coordinator"] = args.coordinator
    if getattr(args, "executors", None):
        caller_answers["executors"] = args.executors
    if getattr(args, "end_mode", None):
        caller_answers["end_mode"] = args.end_mode
    if request_text:
        caller_answers["request"] = request_text

    payload = interview_service.start_interview_job(
        project=project,
        request=request_text,
        coordinator=getattr(args, "coordinator", None),
        executors=getattr(args, "executors", None),
        end_mode=getattr(args, "end_mode", None),
        title=getattr(args, "title", None),
        caller_answers=caller_answers,
        projects_dir=projects_dir,
    )

    job_id = payload["job_id"]
    state = payload["state"]
    pending_keys = payload["pending"]
    text = (
        f"job: {job_id}\n"
        f"state: {state}\n"
        f"pending: {', '.join(pending_keys) if pending_keys else 'none'}"
    )
    _emit(args, text, payload)
    return 0


def _cmd_launch(args: argparse.Namespace) -> int:
    record = maestro.launch(
        _project(args),
        Path(args.request_file),
        spec_dir=args.spec_dir,
        coordinators=args.coordinator,
        executors=args.executors,
        base=args.base,
    )
    text = (
        f"coordinator: {record['id']}\nfront: {record['front']}\n"
        f"branch: {record['branch']}\nworktree: {record['worktree']}\n"
        f"coordinators: {', '.join(record['coordinators'])}"
    )
    _emit(args, text, record)
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    summaries = maestro.status(_project(args))
    _emit(args, "\n".join(maestro.status_lines(summaries)), {"coordinators": summaries})
    return 0


def _cmd_resume(args: argparse.Namespace) -> int:
    record = maestro.resume(_project(args), args.coordinator_id, answer=args.answer)
    _emit(args, f"coordinator: {record['id']}\nstate: {record['state']}", record)
    return 0


def _cmd_stop(args: argparse.Namespace) -> int:
    record = maestro.stop(_project(args), args.coordinator_id)
    _emit(args, f"coordinator: {record['id']}\nstate: {record['state']}", record)
    return 0


def _cmd_clean(args: argparse.Namespace) -> int:
    results = maestro.clean_fronts(_project(args), args.coordinator_ids or None)
    lines = [
        f"{r['id']}: removed {len(r['removed'])}" + (f", errors: {'; '.join(r['errors'])}" if r["errors"] else "")
        for r in results
    ]
    _emit(args, "\n".join(lines) or "nothing to clean", {"cleaned": results})
    return 0


def _cmd_discover(args: argparse.Namespace) -> int:
    project = _project(args)
    prefs = load_prefs(project)
    found = discover_full(prefs)
    executors, notes = found.executors, found.notes
    changes = check_models(project, found)

    lines = [
        f"{executor.id}  cost={executor.cost}  "
        f"trains={'yes' if executor.trains else 'no'}"
        for executor in executors
    ]
    lines.extend(f"skipped: {note}" for note in notes)
    work_types = _work_types_summary(prefs, executors)
    setup_hint = setup.setup_hint()

    payload = {
        "executors": [
            {
                "id": executor.id,
                "tool": executor.tool.get("name"),
                "model": executor.model,
                "cost": executor.cost,
                "trains": executor.trains,
            }
            for executor in executors
        ],
        "skipped": list(notes),
        "changes": changes_payload(changes),
        "work_types": work_types,
        "exec_first": list(prefs.exec_first),
        "setup_hint": setup_hint,
    }
    text = "\n".join(lines) if lines else "no executors"
    text += "\n\n" + "\n".join(render_changes(changes))
    if setup_hint:
        text += "\n\n" + setup_hint
    if work_types:
        text += "\n\n" + "\n".join(
            _work_types_line(name, types, prefs)
            for name, types in work_types.items()
        )
    _emit(args, text, payload)
    return 0


def _check_models_to_stderr(
    project: Path,
    prefs: Prefs | None = None,
    found: Discovery | None = None,
    due_only: bool = False,
) -> None:
    """Run the models check and print its lines on stderr.

    ``prefs`` and ``found`` are loaded or discovered when not given. With
    ``due_only`` nothing happens (and nothing is written) unless the last
    check is ``prefs.model_check_hours`` old. Never raises: a broken check
    (bad preferences, a crashing CLI, an unwritable registry) prints one
    ``models: check skipped`` line, so the main command never fails over it.
    """
    try:
        if prefs is None:
            prefs = load_prefs(project)
        if due_only and not check_due(project, prefs.model_check_hours):
            return
        if found is None:
            found = discover_full(prefs)
        lines = render_changes(check_models(project, found))
    except Exception as exc:  # noqa: BLE001 - the main command must not fail here
        text_lines = str(exc).splitlines()
        reason = text_lines[0] if text_lines else type(exc).__name__
        lines = [f"models: check skipped: {reason}"]
    sys.stderr.write("\n".join(lines) + "\n")


from .cli_jobs import (
    _cmd_job_new,
    _cmd_job_set,
    _cmd_job_show,
    _cmd_piece_add,
    _cmd_piece_start,
    _cmd_piece_finish,
)


# --- pick / dispatch / proof commands ---------------------------------------

from .cli_dispatch import (
    _cmd_dispatch,
    _cmd_pick,
    _proof_text,
    _spec_proof,
    _spec_waits,
    _wait_target,
)


def _cmd_correction(args: argparse.Namespace) -> int:
    project = _project(args)
    executor = args.executor or ""
    records.update_job(
        project,
        args.job,
        lambda job: records.add_correction(job, args.piece, args.reason, executor),
    )
    _emit(
        args,
        f"correction recorded on piece {args.piece}",
        {"piece": args.piece, "reason": args.reason, "executor": executor},
    )
    return 0


def _cmd_ruling_add(args: argparse.Namespace) -> int:
    project = _project(args)
    text = args.text.strip()
    if not text:
        raise ValueError("--text must not be empty")
    number = records.update_job(
        project, args.job, lambda job: records.add_ruling(job, text)
    )
    _emit(args, f"ruling {number} recorded", {"ruling": number})
    return 0


def _cmd_decision_add(args: argparse.Namespace) -> int:
    project = _project(args)
    if (args.from_ruling is None) == (args.title is None):
        raise ValueError("decision add needs exactly one of --from-ruling or --title")
    title_arg = (args.title or "").strip()
    if args.from_ruling is None and not title_arg:
        raise ValueError("--title must not be empty")

    def promote(job: dict) -> tuple[str, Path]:
        decision_text = ""
        ruling = None
        title = title_arg
        if args.from_ruling is not None:
            for entry in job.get("rulings") or []:
                if entry.get("n") == args.from_ruling:
                    ruling = entry
            if ruling is None:
                raise ValueError(f"job has no ruling {args.from_ruling}")
            if ruling.get("promoted"):
                raise ValueError(
                    f"ruling {args.from_ruling} already promoted to ADR "
                    f"{ruling['promoted']}"
                )
            decision_text = str(ruling.get("text", ""))
            title = decision_text.split(" — ")[0].strip() or decision_text.strip()
        origin = f"job {job['id']}"
        if job.get("spec_dir"):
            origin += f" · {job['spec_dir']}"
        number = next_number(project)
        try:
            path = write_adr(
                project,
                number,
                title,
                args.context,
                decision_text or title,
                args.alternatives,
                args.consequences,
                origin,
                date.today(),
            )
        except FileExistsError as exc:
            raise ValueError(str(exc)) from exc
        label = f"{number:04d}"
        if ruling is not None:
            ruling["promoted"] = label
        job.setdefault("decisions", []).append(label)
        return label, path

    label, path = records.update_job(project, args.job, promote)
    relative = (DECISIONS_DIR / path.name).as_posix()
    _emit(args, relative, {"number": label, "path": relative})
    return 0


def _cmd_grade(args: argparse.Namespace) -> int:
    project = _project(args)

    stored = score_piece(
        args.corrections, args.done, args.missing, args.extra, args.penalty or []
    )
    stored["executor"] = args.executor
    stored["graded_at"] = _now_iso()
    records.update_job(
        project, args.job, lambda job: records.set_grade(job, args.piece, stored)
    )
    _write_scoreboard(project, render_scoreboard(project))

    _emit(
        args,
        f"score: {stored['score']}\n{stored['explain']}",
        {
            "score": stored["score"],
            "explain": stored["explain"],
            "executor": stored["executor"],
            "graded_at": stored["graded_at"],
        },
    )
    return 0


# --- delivery / close / review commands -------------------------------------

from .cli_delivery import (
    _cmd_accept,
    _cmd_close,
    _cmd_discard,
    _collect_accepted_pieces,
    _mark_task_done,
    _run_review_model,
    _write_changelog,
)


def _cmd_usage_add(args: argparse.Namespace) -> int:
    project = _project(args)
    records.update_job(
        project,
        args.job,
        lambda job: records.add_usage(
            job, args.role, args.tokens_in, args.tokens_out, args.estimated, args.note
        ),
    )
    _emit(
        args,
        f"usage recorded: {args.role} {args.tokens_in + args.tokens_out} tokens",
        {
            "role": args.role,
            "tokens_in": args.tokens_in,
            "tokens_out": args.tokens_out,
            "estimated": args.estimated,
        },
    )
    return 0


def _cmd_economy(args: argparse.Namespace) -> int:
    project = _project(args)
    job = records.load_job(project, args.job)
    figures = economy.job_economy(
        job, load_prefs(project), spent_today=spend.spent(date.today())
    )
    _emit(args, "\n".join(economy.report_lines(figures)), figures.as_dict())
    return 0


def _cmd_scoreboard(args: argparse.Namespace) -> int:
    project = _project(args)
    text = render_scoreboard(project)
    _write_scoreboard(project, text)
    _emit(args, text.rstrip("\n"), {"scoreboard": text})
    return 0


def _cmd_setup(args: argparse.Namespace) -> int:
    """Detect the machine, ask the rest, write the managed preferences block.

    Without ``--yes`` the command needs a terminal: a script or an agent has
    nobody to answer, so it exits 2 and writes nothing. The questions and the
    summary are in ``--lang`` (default: the system language); the summary
    counts the executors per tier with the block's rules already applied.
    """
    if not args.yes and not _has_tty():
        raise ValueError("no terminal: run acc setup --yes or answer in a terminal")

    project = _project(args)
    prefs = load_prefs(project)
    lang = args.lang or setup.detect_lang(os.environ)
    detected = setup.detect(prefs, os.environ, setup.home_dir())
    questions = setup.questions(detected, lang)
    if args.yes:
        answers = {question.key: question.default for question in questions}
    else:
        answers = setup.ask(questions, sys.stdin, sys.stderr)

    block = setup.render_block(detected, answers)
    path = default_global_path()
    setup.write_block(path, block)

    values = setup.block_values(block)
    counts = setup.tier_counts(prefs, detected, block)
    budget = setup.budget_value(values.get("cheap_budget_usd_day"))
    _emit(
        args,
        setup.summary_text(counts, f"{budget:g}", lang),
        {
            "lang": lang,
            "tiers": counts,
            "cheap_budget_usd_day": budget,
            "forbid_training": bool(values.get("forbid_training", True)),
            "models": list(values.get("models", [])),
            "accounts": dict(detected.accounts),
            "executors": [executor.id for executor in detected.executors],
            "preferences": str(path),
        },
    )
    return 0


# --- spec commands -----------------------------------------------------------

from .cli_spec import (
    _SPEC_NUMBER_RE,
    _SPEC_TEMPLATES,
    _check_entry,
    _cmd_brief_render,
    _cmd_spec_import,
    _cmd_spec_new,
    _cmd_spec_status,
    _entry_mismatch,
    _load_spec,
    _norm_path,
    _parallel_conflicts,
    _piece_task_text,
    _review_target,
    _spec_relative,
    _squash,
    _stale_text,
)


# --- parser ----------------------------------------------------------------

from .cli_parser import build_parser


def main(argv: list[str] | None = None) -> int:
    """Run the CLI for ``argv`` (default: ``sys.argv[1:]``) and return a code."""
    _reconfigure_streams()
    tokens = list(sys.argv[1:] if argv is None else argv)
    if tokens[:1] == ["_coordinator-run"] and len(tokens) == 2:
        # Internal entry of the detached runner; kept out of the public help.
        maestro.run(Path(tokens[1]))
        return 0
    try:
        args = build_parser().parse_args(tokens)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    try:
        return args.handler(args)
    except (PrefsError, RecordError, SidecarError, NotAGitRepo, ValueError, maestro.MaestroError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except RuntimeError as exc:
        first_line = str(exc).splitlines()[0] if str(exc).splitlines() else ""
        print(f"error: {first_line}", file=sys.stderr)
        return 1
