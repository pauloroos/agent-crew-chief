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

``usage add`` records the coordinator's (or central's) own token spend in the job;
``economy`` prints the job's economy summary and alerts, and ``close`` prints the
same block before the commit message.

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
import random
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from . import economy, records
from .brief import render_converge_brief, render_exec_brief, render_review_brief
from .changelog import CATEGORY_BY_COMMIT, entry_line, insert_entry
from .config import Prefs, PrefsError, load_prefs, tool_work_types
from .decisions import DECISIONS_DIR, next_number, write_adr
from .discover import Discovery, discover_full
from .dispatch import dispatch as run_attempt
from .models_seen import changes_payload, check_due, check_models, render_changes
from .records import RecordError
from .scoring import (
    build_stats,
    free_failures,
    grade as score_piece,
    pick as pick_executor,
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
from .worktree import NotAGitRepo, commit_paths, path_is_clean
from .worktree import accept as accept_branch
from .worktree import discard as discard_branch

#: Path of the committed scoreboard, relative to the project root.
_SCOREBOARD_RELATIVE_PATH = Path("docs") / "crew-chief" / "scoreboard.md"

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


def _emit(args: argparse.Namespace, text: str, payload: dict) -> None:
    """Print ``text``, or ``payload`` as JSON when ``--json`` was given."""
    if args.json:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    else:
        sys.stdout.write(text + "\n")


def _read_text(path: Path, label: str) -> str:
    """Read ``path`` as UTF-8; a read failure becomes a :class:`ValueError`."""
    try:
        return path.read_text(encoding="utf-8")
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
    }
    text = "\n".join(lines) if lines else "no executors"
    text += "\n\n" + "\n".join(render_changes(changes))
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


def _cmd_job_new(args: argparse.Namespace) -> int:
    project = _project(args)
    distributed = not args.not_distributed
    reason = args.reason or ""
    if not distributed and not reason:
        raise ValueError("--not-distributed requires --reason")

    request = _read_text(Path(args.request_file), "--request-file")
    job = records.new_job(
        project, args.title, request, distributed, reason, args.issue or ""
    )
    _emit(args, job["id"], {"id": job["id"]})
    sys.stdout.flush()
    _check_models_to_stderr(project)
    return 0


def _cmd_piece_add(args: argparse.Namespace) -> int:
    project = _project(args)
    number = records.update_job(
        project,
        args.job,
        lambda job: records.add_piece(job, args.title, args.type, args.risk),
    )
    _emit(args, str(number), {"piece": number})
    return 0


def _cmd_pick(args: argparse.Namespace) -> int:
    project = _project(args)
    prefs = load_prefs(project)
    found = discover_full(prefs)
    executors = found.executors
    _piece(records.load_job(project, args.job), args.piece)
    _check_models_to_stderr(project, prefs, found, due_only=True)
    stats = build_stats(project)
    cooling = quota_cooldown(project, prefs.quota_cooldown_min)

    def choose(job: dict) -> tuple:
        """Pick under the job lock, so parallel picks see each other's picks.

        With ``--review`` the pick is for a reviewer: the work type is
        ``review``, failed review attempts are left out instead of failed exec
        attempts, and so is every executor in the quota family of an executor
        with an ``ok`` exec attempt on the piece. Two free failures escalate
        on their own only on thinking work; execution work stays free unless
        ``--strong`` is given.
        """
        piece = _piece(job, args.piece)
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
        if not args.review:
            # Work that drew a correction goes to someone else for the fix.
            exclude |= {
                correction.get("executor")
                for correction in piece.get("corrections") or []
                if isinstance(correction, dict) and correction.get("executor")
            }
        families = {quota_family(executor_id) for executor_id in reviewed}
        exclude |= {
            executor.id
            for executor in executors
            if quota_family(executor.id) in families
        }

        escalation = ""
        if (
            not args.review
            and work_type not in records.EXEC_TYPES
            and free_failures(job, args.piece) >= 2
        ):
            escalation = _ESCALATION_FREE_FAILURES
        elif args.strong:
            escalation = _ESCALATION_STRONG
        if escalation:
            exclude |= {
                executor.id for executor in executors if executor.cost == "free"
            }

        choice, reason = pick_executor(
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
        )
        if reviewed:
            reason += (
                f"; reviewer outside the family of: {', '.join(sorted(reviewed))}"
            )

        if escalation:
            records.add_escalation(
                job, args.piece, escalation, choice.id if choice else ""
            )
        if choice is not None:
            entry = {"pick": choice.id, "reason": reason, "at": _now_iso()}
            if args.review:
                entry["role"] = "review"
            piece.setdefault("picks", []).append(entry)
        return choice, reason

    choice, reason = records.update_job(project, args.job, choose)

    if choice is None:
        _emit(args, reason, {"executor": None, "reason": reason})
        return 1

    _emit(args, f"{choice.id}\n{reason}", {"executor": choice.id, "reason": reason})
    return 0


def _cmd_dispatch(args: argparse.Namespace) -> int:
    project = _project(args)
    if args.review and not args.target:
        raise ValueError("--review requires --target")
    if args.review and (args.base or args.fresh):
        raise ValueError("--base and --fresh are for exec attempts, not --review")
    if args.review and args.no_proof:
        raise ValueError("--no-proof is for exec attempts, not --review")
    if args.review and args.ignore_deps:
        raise ValueError("--ignore-deps is for exec attempts, not --review")
    proof, expect, files, ignored_deps = _spec_proof(project, args)

    prefs = load_prefs(project)
    found = discover_full(prefs)
    executor = next(
        (item for item in found.executors if item.id == args.executor), None
    )
    if executor is None:
        raise ValueError(f"unknown executor {args.executor!r}")

    brief = _read_text(Path(args.brief_file), "--brief-file")
    _check_models_to_stderr(project, prefs, found, due_only=True)
    attempt = run_attempt(
        project,
        args.job,
        args.piece,
        executor,
        brief,
        prefs,
        role="review" if args.review else "exec",
        effort=args.effort,
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
    )

    status = attempt["status"]
    lines = [
        f"status: {status}",
        f"branch: {attempt['branch'] or '-'}",
        f"worktree: {attempt['worktree'] or '-'}",
        f"base: {_base_text(attempt)}",
        "destructive_suspect: "
        + (", ".join(attempt["destructive_suspect"]) or "-"),
        "outside_writes: " + (", ".join(attempt["outside_writes"]) or "-"),
        f"duration: {attempt['duration_s']}s",
        f"steps: {'-' if attempt['steps'] is None else attempt['steps']}",
        f"tokens: {_tokens_text(attempt)}",
    ]
    if "proof" in attempt:
        lines.append(f"proof: {_proof_text(attempt['proof'])}")
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
    _emit(args, "\n".join(lines), payload)
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
    tasks_path = project / spec_dir / "tasks.md"
    if not tasks_path.is_file():
        return ""
    tasks = parse_tasks(_read_text(tasks_path, "tasks.md"))
    reason = readiness(job, tasks, load_sidecar(project / spec_dir)).get(ref, "")
    return _wait_target(reason) if reason else ""


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
    job = records.load_job(project, args.job)
    piece = _piece(job, args.piece)
    ref = piece.get("ref")
    if piece.get("stale"):
        raise ValueError(
            f"piece {args.piece} is stale (task {ref} changed); re-import the spec"
        )
    spec_dir = job.get("spec_dir")
    if args.review or not ref or not spec_dir:
        return "", "", None, None
    waits = _spec_waits(project, job, ref, spec_dir)
    ignored_deps = None
    if waits:
        if not args.ignore_deps:
            raise ValueError(f"piece {args.piece} ({ref}) waits for {waits}")
        ignored_deps = [waits]
    meta = load_sidecar(project / spec_dir).tasks.get(ref)
    work_type = meta.type if meta else piece.get("type")
    proof = meta.proof if meta else ""
    if not proof and work_type in records.EXEC_TYPES and not args.no_proof:
        raise ValueError(
            f"piece {args.piece} ({ref}) has no proof in crew.toml; "
            "add one or pass --no-proof"
        )
    expect = meta.expect if meta else ""
    files = list(meta.files) if meta and meta.files else None
    return proof, expect, files, ignored_deps


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


def _cmd_accept(args: argparse.Namespace) -> int:
    project = _project(args)
    job = records.load_job(project, args.job)
    piece = _piece(job, args.piece)
    attempt = _last_ok_exec_attempt(piece)

    if attempt is None or not attempt.get("branch") or not attempt.get("worktree"):
        _emit(
            args,
            f"nothing to accept on piece {args.piece}",
            {"branch": None, "worktree": None},
        )
        return 1

    accept_branch(project, Path(attempt["worktree"]), attempt["branch"])
    records.update_job(
        project,
        args.job,
        lambda stored: records.mark_accepted(stored, args.piece, attempt),
    )
    _emit(
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
        _mark_task_done(project, job["spec_dir"], piece["ref"])
    return 0


def _mark_task_done(project: Path, spec_dir: str, ref: str) -> None:
    """Tick ``ref`` in the project's ``tasks.md`` and commit just that line.

    The file is read and written without newline translation, so CRLF and BOM
    stay as the user had them. The tick is committed only when ``tasks.md`` was
    tracked and unchanged before it; otherwise it stays in the working tree
    with a warning, so none of the user's other edits land in the commit.
    Problems only warn: the branch is already merged and the piece accepted.
    """
    path = project / spec_dir / "tasks.md"
    try:
        with open(path, encoding="utf-8", newline="") as handle:
            text = handle.read()
    except OSError:
        print(f"tasks.md not found in {spec_dir}; not marked", file=sys.stderr)
        return
    try:
        marked = mark_done(text, ref)
    except KeyError:
        print(f"task {ref} not found in tasks.md; not marked", file=sys.stderr)
        return
    if marked == text:
        return
    relative = (Path(spec_dir) / "tasks.md").as_posix()
    clean = path_is_clean(project, relative)
    try:
        records.write_atomic(path, marked)
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
        commit_paths(project, [relative], f"docs(tasks): mark {ref} done")
    except RuntimeError as exc:
        print(f"{ref} ticked in tasks.md but not committed: {exc}", file=sys.stderr)


def _cmd_discard(args: argparse.Namespace) -> int:
    project = _project(args)
    job = records.load_job(project, args.job)
    piece = _piece(job, args.piece)
    attempt = _last_ok_exec_attempt(piece)

    if attempt is None or not attempt.get("branch") or not attempt.get("worktree"):
        _emit(
            args,
            f"nothing to discard on piece {args.piece}",
            {"branch": None, "worktree": None},
        )
        return 1

    discard_branch(project, Path(attempt["worktree"]), attempt["branch"])
    _emit(
        args,
        f"discarded {attempt['branch']}",
        {"branch": attempt["branch"], "worktree": attempt["worktree"]},
    )
    return 0


def _cmd_close(args: argparse.Namespace) -> int:
    project = _project(args)
    if args.type:

        def store_type(stored: dict) -> dict:
            stored["commit_type"] = args.type
            return stored

        job = records.update_job(project, args.job, store_type)
    else:
        job = records.load_job(project, args.job)
    written = None if args.no_changelog else _write_changelog(project, job)
    commit, issue = records.summaries(job, written[0] if written else None)
    _write_scoreboard(project, render_scoreboard(project))
    text = f"{commit}\n\n---\n\n{issue}"
    payload = {"commit": commit, "issue": issue}
    if economy.has_data(job):
        figures = economy.job_economy(job, load_prefs(project))
        text = "\n".join([*economy.report_lines(figures), text])
        payload["economy"] = figures.as_dict()
    if written:
        text = f"changelog: {written[0]} ({written[1]})\n{text}"
        payload["changelog"] = {"path": written[0], "category": written[1]}
    _emit(args, text, payload)
    return 0


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
    figures = economy.job_economy(job, load_prefs(project))
    _emit(args, "\n".join(economy.report_lines(figures)), figures.as_dict())
    return 0


def _write_changelog(project: Path, job: dict) -> tuple[str, str] | None:
    """Add the job to the project CHANGELOG; ``(path, category)`` when written.

    Nothing is written when the preference is ``none``, no piece of the job was
    accepted, the job's commit type has no category, or the job's entry is
    already in the file (``close`` run twice). The file is read and written
    without newline translation, so its CRLF and BOM survive.
    """
    prefs = load_prefs(project)
    if prefs.changelog == "none":
        return None
    if not any(piece.get("accepted") for piece in job.get("pieces") or []):
        return None
    category = CATEGORY_BY_COMMIT.get(records.commit_type(job))
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
        new = insert_entry(old, category, entry_line(job))
        if new == old:
            return None
        records.write_atomic(path, new)
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"changelog: cannot update {path}: {exc}") from exc
    return path.relative_to(project.resolve()).as_posix(), category


def _cmd_scoreboard(args: argparse.Namespace) -> int:
    project = _project(args)
    text = render_scoreboard(project)
    _write_scoreboard(project, text)
    _emit(args, text.rstrip("\n"), {"scoreboard": text})
    return 0


# --- spec commands -----------------------------------------------------------

_SPEC_TEMPLATES = Path(__file__).resolve().parents[2] / "templates" / "spec"
_SPEC_NUMBER_RE = re.compile(r"^(\d{3})-")


def _cmd_spec_new(args: argparse.Namespace) -> int:
    project = _project(args)
    title = args.title.strip()
    if not project.is_dir():
        raise ValueError(f"--project: not a directory: {project}")
    if not any(ch.isalnum() for ch in title):
        raise ValueError("--title needs at least one letter or digit")

    specs = project / "specs"
    numbers = [
        int(match.group(1))
        for entry in (specs.iterdir() if specs.is_dir() else [])
        if entry.is_dir() and (match := _SPEC_NUMBER_RE.match(entry.name))
    ]
    number = max(numbers, default=0) + 1
    if number > 999:
        raise ValueError("specs/ already holds 999 numbered folders")
    folder = specs / f"{number:03d}-{records.slug(title)[:40].strip('-')}"
    if folder.exists():
        raise ValueError(f"{folder.relative_to(project).as_posix()} already exists")

    today = datetime.now().strftime("%Y-%m-%d")
    contents = {}
    for name in ("spec.md", "plan.md", "tasks.md"):
        text = _read_text(_SPEC_TEMPLATES / name, "template")
        contents[name] = text.replace("[FEATURE NAME]", title).replace("[DATE]", today)
    folder.mkdir(parents=True)
    for name, text in contents.items():
        (folder / name).write_text(text, encoding="utf-8", newline="\n")

    relative = folder.relative_to(project).as_posix()
    _emit(args, relative, {"spec_dir": relative})
    return 0


def _spec_relative(project: Path, folder: Path) -> str:
    """``folder`` as a ``/`` path relative to the project (absolute if outside)."""
    try:
        return folder.resolve().relative_to(project.resolve()).as_posix()
    except ValueError:
        return folder.resolve().as_posix()


def _load_spec(
    project: Path, job: dict, spec_dir: str | None
) -> tuple[Path, str, list[SpecTask], Sidecar]:
    """Locate and read a job's spec folder; any problem is a one-line error.

    The folder is ``--spec-dir``, else the job's ``spec_dir``, else spec-kit's
    ``.specify/feature.json``. Nothing is written.
    """
    folder = find_spec_dir(project, spec_dir or job.get("spec_dir") or None)
    relative = _spec_relative(project, folder)
    tasks_path = folder / "tasks.md"
    if not tasks_path.is_file():
        raise SpecError(f"tasks.md not found in {relative}")
    tasks = parse_tasks(_read_text(tasks_path, "tasks.md"))
    return folder, relative, tasks, load_sidecar(folder)


def _norm_path(path: str) -> str:
    return path.replace("\\", "/")


def _parallel_conflicts(
    tasks: list[SpecTask], sidecar: Sidecar
) -> list[tuple[str, str, list[str]]]:
    """``[P]`` tasks of one phase whose sidecar ``files`` overlap."""
    open_tasks = [t for t in tasks if t.parallel and not t.done]
    conflicts = []
    for index, first in enumerate(open_tasks):
        for second in open_tasks[index + 1:]:
            if first.phase != second.phase:
                continue
            files = [
                {_norm_path(f) for f in sidecar.tasks[t.id].files}
                if t.id in sidecar.tasks
                else set()
                for t in (first, second)
            ]
            shared = sorted(files[0] & files[1])
            if shared:
                conflicts.append((first.id, second.id, shared))
    return conflicts


def _squash(text: str) -> str:
    """Collapse whitespace so only real wording changes make a piece stale."""
    return " ".join(text.split())


def _piece_task_text(piece: dict) -> str | None:
    """The task text a piece was created from, squashed; ``None`` if unknown.

    Pieces made before ``task_text`` existed carry it in their title
    (``"<ref> <text>"``).
    """
    text = piece.get("task_text")
    if isinstance(text, str):
        return _squash(text)
    prefix = f"{piece.get('ref')} "
    title = piece.get("title")
    if isinstance(title, str) and title.startswith(prefix):
        return _squash(title[len(prefix):])
    return None


def _stale_text(stale: list[dict]) -> str:
    return ", ".join(f"{entry['piece']} ({entry['ref']})" for entry in stale)


def _cmd_spec_import(args: argparse.Namespace) -> int:
    project = _project(args)
    job = records.load_job(project, args.job)
    folder, relative, tasks, sidecar = _load_spec(project, job, args.spec_dir)
    bound = job.get("spec_dir")
    if bound and bound != relative:
        raise ValueError(f"job is bound to {bound}; use a new job")
    original = copy.deepcopy(sidecar)

    def apply(stored: dict) -> tuple[list[dict], list[dict], list[dict]]:
        stale = []
        for task in tasks:
            piece = records.piece_for_ref(stored, task.id)
            if piece is not None and _piece_task_text(piece) not in (
                None,
                _squash(task.text),
            ):
                piece["stale"] = True
                stale.append({"piece": piece["n"], "ref": task.id})
        created = []
        for task in tasks:
            if task.done or records.piece_for_ref(stored, task.id) is not None:
                continue
            meta = sidecar.tasks.get(task.id)
            work_type = meta.type if meta else guess_type(task)
            risk = meta.risk if meta else guess_risk(task)
            number = records.add_piece(
                stored, f"{task.id} {task.text}", work_type, risk, ref=task.id
            )
            stored["pieces"][number - 1]["task_text"] = task.text
            created.append(
                {"piece": number, "ref": task.id, "type": work_type, "risk": risk}
            )
        stored["spec_dir"] = relative
        return created, stale, [dict(p) for p in stored["pieces"]]

    created, stale, pieces = records.update_job(project, args.job, apply)

    by_ref = {
        p["ref"]: p for p in reversed(pieces) if p.get("ref") and not p.get("stale")
    }
    for task in tasks:
        meta = sidecar.tasks.get(task.id)
        if meta is None:
            if task.done:
                continue
            piece = by_ref.get(task.id)
            sidecar.tasks[task.id] = TaskMeta(
                piece["type"] if piece else guess_type(task),
                piece["risk"] if piece else guess_risk(task),
                depends=list(task.depends),
            )
        elif not meta.depends and task.depends:
            meta.depends = list(task.depends)
    sidecar.spec_dir = relative
    sidecar.job = args.job
    if sidecar != original and update_sidecar(folder, sidecar) == "rewritten":
        print("crew.toml rewritten; comments are not kept", file=sys.stderr)

    ids = {task.id for task in tasks}
    missing_proof = [
        task.id
        for task in tasks
        if not task.done
        and task.id in by_ref
        and not by_ref[task.id].get("accepted")
        and by_ref[task.id].get("type") in records.EXEC_TYPES
        and not (task.id in sidecar.tasks and sidecar.tasks[task.id].proof)
    ]
    orphan_sidecar = sorted(set(sidecar.tasks) - ids)
    orphan_pieces = [
        p["ref"]
        for p in pieces
        if p.get("ref") and not p.get("stale") and p["ref"] not in ids
    ]
    conflicts = _parallel_conflicts(tasks, sidecar)

    lines = [
        f"piece {c['piece']}: {c['ref']} ({c['type']}, {c['risk']})" for c in created
    ] or ["nothing new to import"]
    if missing_proof:
        lines.append("missing proof: " + ", ".join(missing_proof))
    if orphan_sidecar:
        lines.append("orphan sidecar entries: " + ", ".join(orphan_sidecar))
    if orphan_pieces:
        lines.append("orphan pieces: " + ", ".join(orphan_pieces))
    if stale:
        lines.append("stale pieces: " + _stale_text(stale))
    lines.extend(
        f"parallel conflict: {a}/{b} share {', '.join(shared)}"
        for a, b, shared in conflicts
    )
    payload = {
        "spec_dir": relative,
        "created": created,
        "missing_proof": missing_proof,
        "orphan_sidecar": orphan_sidecar,
        "orphan_pieces": orphan_pieces,
        "stale": stale,
        "parallel_conflicts": [
            {"tasks": [a, b], "files": shared} for a, b, shared in conflicts
        ],
    }
    _emit(args, "\n".join(lines), payload)
    return 0


def _cmd_spec_status(args: argparse.Namespace) -> int:
    project = _project(args)
    job = records.load_job(project, args.job)
    _, relative, tasks, sidecar = _load_spec(project, job, args.spec_dir)

    waiting = readiness(job, tasks, sidecar)
    accepted_refs = {
        task.id
        for task in tasks
        if (piece := records.piece_for_ref(job, task.id)) and piece.get("accepted")
    }
    stale = [
        {"piece": p["n"], "ref": p["ref"]}
        for p in job.get("pieces") or []
        if p.get("ref") and p.get("stale")
    ]
    phases: list[dict] = []
    for task in tasks:
        entry = next((p for p in phases if p["phase"] == task.phase), None)
        if entry is None:
            entry = {"phase": task.phase, "accepted": 0, "total": 0}
            phases.append(entry)
        entry["total"] += 1
        if task.done or task.id in accepted_refs:
            entry["accepted"] += 1

    ready = [task_id for task_id, reason in waiting.items() if not reason]
    blocked = {task_id: reason for task_id, reason in waiting.items() if reason}
    lines = [
        f"{p['phase'] or '(no phase)'}: {p['accepted']}/{p['total']} accepted"
        for p in phases
    ]
    lines.append("ready: " + (", ".join(ready) if ready else "-"))
    if blocked:
        lines.append("blocked:")
        lines.extend(f"  {task_id}: {reason}" for task_id, reason in blocked.items())
    else:
        lines.append("blocked: -")
    if stale:
        lines.append("stale: " + _stale_text(stale))
    payload = {
        "spec_dir": relative,
        "stale": stale,
        "phases": phases,
        "ready": ready,
        "blocked": blocked,
    }
    _emit(args, "\n".join(lines), payload)
    return 0


def _cmd_brief_render(args: argparse.Namespace) -> int:
    project = _project(args)
    job = records.load_job(project, args.job)
    if args.review and args.converge:
        raise ValueError("--review and --converge cannot be combined")
    number = args.piece
    if args.converge:
        folder = find_spec_dir(project, job.get("spec_dir") or None)
        kind = "converge"
        brief = render_converge_brief(_spec_relative(project, folder))
    else:
        if number is None:
            raise ValueError("brief render needs --piece (or --converge)")
        piece = _piece(job, number)
        ref = piece.get("ref")
        if not ref:
            print(
                f"piece {number} has no spec task; write the brief yourself",
                file=sys.stderr,
            )
            return 1
        if piece.get("stale"):
            print(
                f"piece {number} is stale (task {ref} changed); re-import the spec",
                file=sys.stderr,
            )
            return 1
        folder, relative, tasks, sidecar = _load_spec(project, job, None)
        task = next((t for t in tasks if t.id == ref), None)
        if task is None:
            raise ValueError(
                f"task {ref} not found in {relative}/tasks.md; re-import the spec"
            )
        meta = sidecar.tasks.get(ref) or TaskMeta(
            piece.get("type") or "code", piece.get("risk") or "medium"
        )
        spec_path = folder / "spec.md"
        index = (
            parse_spec_refs(_read_text(spec_path, "spec.md"))
            if spec_path.is_file()
            else {}
        )
        found, missing = resolve_refs(list(dict.fromkeys(meta.accept)), index)
        refs = found + [f"UNRESOLVED: {item}" for item in missing]
        if args.review:
            kind, brief = "review", render_review_brief(task, meta, refs)
        else:
            kind = "exec"
            brief = render_exec_brief(
                task, meta, refs, sidecar.global_constraints, folder.name
            )
    payload = {"job": args.job, "piece": number, "kind": kind, "brief": brief}
    if args.out:
        target = Path(args.out).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(brief, encoding="utf-8", newline="\n")
        payload["out"] = str(target)
        _emit(args, f"wrote {target}", payload)
    else:
        _emit(args, brief.rstrip("\n"), payload)
    return 0


# --- parser ----------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for every subcommand."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--project",
        metavar="PATH",
        default=None,
        help="project root (default: the current directory)",
    )
    common.add_argument(
        "--json", action="store_true", help="print the result as JSON"
    )

    parser = argparse.ArgumentParser(
        prog="crew-chief", description="Coordinate several executor CLIs."
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    discover_parser = subcommands.add_parser(
        "discover", parents=[common], help="list the executors available now"
    )
    discover_parser.set_defaults(handler=_cmd_discover)

    job_parser = subcommands.add_parser("job", help="job records")
    job_commands = job_parser.add_subparsers(dest="job_command", required=True)
    job_new = job_commands.add_parser(
        "new", parents=[common], help="create a job from a request file"
    )
    job_new.add_argument("--title", required=True, help="job title")
    job_new.add_argument(
        "--request-file", metavar="F", required=True, help="UTF-8 request file"
    )
    job_new.add_argument(
        "--not-distributed",
        action="store_true",
        help="the job stays with the coordinator",
    )
    job_new.add_argument("--reason", default="", help="why the job was not distributed")
    job_new.add_argument("--issue", default="", help="issue tracker reference")
    job_new.set_defaults(handler=_cmd_job_new)

    piece_parser = subcommands.add_parser("piece", help="job pieces")
    piece_commands = piece_parser.add_subparsers(dest="piece_command", required=True)
    piece_add = piece_commands.add_parser(
        "add", parents=[common], help="append a piece to a job"
    )
    piece_add.add_argument("--job", required=True, help="job id")
    piece_add.add_argument("--title", required=True, help="piece title")
    piece_add.add_argument("--type", required=True, help="work type")
    piece_add.add_argument("--risk", required=True, help="risk level")
    piece_add.set_defaults(handler=_cmd_piece_add)

    pick_parser = subcommands.add_parser(
        "pick", parents=[common], help="choose the next executor for a piece"
    )
    pick_parser.add_argument("--job", required=True, help="job id")
    pick_parser.add_argument("--piece", type=int, required=True, help="piece number")
    pick_parser.add_argument(
        "--exclude",
        nargs="+",
        default=[],
        metavar="EXECUTOR",
        help="executor ids to leave out",
    )
    pick_parser.add_argument(
        "--strong",
        action="store_true",
        help="the piece is complex and cannot be split (the only way execution "
        "work reaches a paid executor)",
    )
    pick_parser.add_argument(
        "--review",
        action="store_true",
        help="pick a reviewer for the piece: thinking work, outside the quota "
        "family of the executor that did it",
    )
    pick_parser.set_defaults(handler=_cmd_pick)

    dispatch_parser = subcommands.add_parser(
        "dispatch", parents=[common], help="run one attempt"
    )
    dispatch_parser.add_argument("--job", required=True, help="job id")
    dispatch_parser.add_argument(
        "--piece", type=int, required=True, help="piece number"
    )
    dispatch_parser.add_argument("--executor", required=True, help="executor id")
    dispatch_parser.add_argument(
        "--brief-file", metavar="F", required=True, help="UTF-8 brief file"
    )
    dispatch_parser.add_argument(
        "--review", action="store_true", help="run as a review, not as exec"
    )
    dispatch_parser.add_argument(
        "--target", default=None, help="worktree a review must run in"
    )
    start = dispatch_parser.add_mutually_exclusive_group()
    start.add_argument(
        "--base",
        default=None,
        metavar="N|BRANCH",
        help="start the worktree from this attempt number of the piece or "
        "this branch (default: HEAD, or the last ok attempt once a correction "
        "was recorded after it)",
    )
    start.add_argument(
        "--fresh",
        action="store_true",
        help="start the worktree from HEAD even after a correction",
    )
    dispatch_parser.add_argument(
        "--no-proof",
        action="store_true",
        help="dispatch a spec piece without running its proof (recorded as skipped)",
    )
    dispatch_parser.add_argument(
        "--ignore-deps",
        action="store_true",
        help="dispatch a spec piece although a dependency or an earlier phase "
        "is not accepted yet (recorded as ignored_deps)",
    )
    dispatch_parser.add_argument("--effort", default=None, help="effort/reasoning")
    dispatch_parser.add_argument(
        "--timeout", type=int, default=None, metavar="S", help="timeout in seconds"
    )
    dispatch_parser.set_defaults(handler=_cmd_dispatch)

    correction_parser = subcommands.add_parser(
        "correction", parents=[common], help="record a correction on a piece"
    )
    correction_parser.add_argument("--job", required=True, help="job id")
    correction_parser.add_argument(
        "--piece", type=int, required=True, help="piece number"
    )
    correction_parser.add_argument("--reason", required=True, help="what went wrong")
    correction_parser.add_argument("--executor", default=None, help="executor id")
    correction_parser.set_defaults(handler=_cmd_correction)

    grade_parser = subcommands.add_parser(
        "grade", parents=[common], help="grade a piece and refresh the scoreboard"
    )
    grade_parser.add_argument("--job", required=True, help="job id")
    grade_parser.add_argument("--piece", type=int, required=True, help="piece number")
    grade_parser.add_argument("--executor", required=True, help="executor id")
    grade_parser.add_argument(
        "--corrections", type=int, required=True, help="number of corrections"
    )
    grade_parser.add_argument("--done", type=int, required=True, help="items done")
    grade_parser.add_argument("--missing", type=int, required=True, help="items missing")
    grade_parser.add_argument("--extra", type=int, required=True, help="items added")
    grade_parser.add_argument(
        "--penalty",
        action="append",
        default=[],
        metavar="P",
        help="named penalty (repeatable)",
    )
    grade_parser.set_defaults(handler=_cmd_grade)

    accept_parser = subcommands.add_parser(
        "accept", parents=[common], help="merge the piece's last ok attempt"
    )
    accept_parser.add_argument("--job", required=True, help="job id")
    accept_parser.add_argument("--piece", type=int, required=True, help="piece number")
    accept_parser.set_defaults(handler=_cmd_accept)

    discard_parser = subcommands.add_parser(
        "discard", parents=[common], help="drop the piece's last ok attempt"
    )
    discard_parser.add_argument("--job", required=True, help="job id")
    discard_parser.add_argument(
        "--piece", type=int, required=True, help="piece number"
    )
    discard_parser.set_defaults(handler=_cmd_discard)

    close_parser = subcommands.add_parser(
        "close", parents=[common], help="print the job's commit message and comment"
    )
    close_parser.add_argument("--job", required=True, help="job id")
    close_parser.add_argument(
        "--no-changelog",
        action="store_true",
        help="do not add the job to the project CHANGELOG",
    )
    close_parser.add_argument(
        "--type",
        choices=records.CLOSE_TYPES,
        default=None,
        help="commit type and CHANGELOG category (default: a 'fix: ' style "
        "prefix of the job title, else the first piece's work type)",
    )
    close_parser.set_defaults(handler=_cmd_close)

    spec_parser = subcommands.add_parser(
        "spec", help="spec-kit specs: new folder, import tasks as pieces, status"
    )
    spec_commands = spec_parser.add_subparsers(dest="spec_command", required=True)
    spec_new = spec_commands.add_parser(
        "new", parents=[common], help="create specs/NNN-<slug>/ from the templates"
    )
    spec_new.add_argument("--title", required=True, help="feature title")
    spec_new.set_defaults(handler=_cmd_spec_new)
    spec_import = spec_commands.add_parser(
        "import", parents=[common], help="turn open tasks.md tasks into pieces"
    )
    spec_import.add_argument("--job", required=True, help="job id")
    spec_import.add_argument(
        "--spec-dir",
        default=None,
        metavar="PATH",
        help="spec folder (default: the job's, else .specify/feature.json)",
    )
    spec_import.set_defaults(handler=_cmd_spec_import)
    spec_status = spec_commands.add_parser(
        "status", parents=[common], help="progress per phase, ready and blocked tasks"
    )
    spec_status.add_argument("--job", required=True, help="job id")
    spec_status.add_argument(
        "--spec-dir", default=None, metavar="PATH", help="spec folder"
    )
    spec_status.set_defaults(handler=_cmd_spec_status)

    brief_parser = subcommands.add_parser(
        "brief", help="briefs for spec tasks: exec, review, converge"
    )
    brief_commands = brief_parser.add_subparsers(dest="brief_command", required=True)
    brief_render = brief_commands.add_parser(
        "render", parents=[common], help="render the brief of a spec piece"
    )
    brief_render.add_argument("--job", required=True, help="job id")
    brief_render.add_argument(
        "--piece", type=int, default=None, help="piece number (not needed with --converge)"
    )
    brief_render.add_argument(
        "--review", action="store_true", help="the reviewer brief (two verdicts)"
    )
    brief_render.add_argument(
        "--converge", action="store_true", help="the final spec-vs-code comparison brief"
    )
    brief_render.add_argument(
        "--out", default=None, metavar="FILE", help="write the brief here, not to stdout"
    )
    brief_render.set_defaults(handler=_cmd_brief_render)

    ruling_parser = subcommands.add_parser("ruling", help="rulings recorded in the job")
    ruling_commands = ruling_parser.add_subparsers(dest="ruling_command", required=True)
    ruling_add = ruling_commands.add_parser(
        "add", parents=[common], help="record a ruling in the job"
    )
    ruling_add.add_argument("--job", required=True, help="job id")
    ruling_add.add_argument(
        "--text", required=True, help='"<decision> — <why> — <cost if wrong>"'
    )
    ruling_add.set_defaults(handler=_cmd_ruling_add)

    decision_parser = subcommands.add_parser("decision", help="ADRs in docs/decisions")
    decision_commands = decision_parser.add_subparsers(
        dest="decision_command", required=True
    )
    decision_add = decision_commands.add_parser(
        "add", parents=[common], help="write an ADR (from a ruling or a title)"
    )
    decision_add.add_argument("--job", required=True, help="job id")
    decision_add.add_argument(
        "--from-ruling", type=int, default=None, metavar="N", help="promote ruling N"
    )
    decision_add.add_argument("--title", default=None, help="ADR title (no ruling)")
    decision_add.add_argument("--context", default="", help="why this came up")
    decision_add.add_argument("--alternatives", default="", help="what was weighed")
    decision_add.add_argument("--consequences", default="", help="what follows")
    decision_add.set_defaults(handler=_cmd_decision_add)

    usage_parser = subcommands.add_parser(
        "usage", help="the coordinator's own token spend"
    )
    usage_commands = usage_parser.add_subparsers(dest="usage_command", required=True)
    usage_add = usage_commands.add_parser(
        "add", parents=[common], help="record the coordinator's or central's tokens"
    )
    usage_add.add_argument("--job", required=True, help="job id")
    usage_add.add_argument(
        "--role", required=True, choices=records.USAGE_ROLES, help="who spent them"
    )
    usage_add.add_argument("--tokens-in", type=int, required=True, help="input tokens")
    usage_add.add_argument(
        "--tokens-out", type=int, required=True, help="output tokens"
    )
    usage_add.add_argument(
        "--estimated", action="store_true", help="the counts are an estimate"
    )
    usage_add.add_argument("--note", default="", help="what the tokens were for")
    usage_add.set_defaults(handler=_cmd_usage_add)

    economy_parser = subcommands.add_parser(
        "economy", parents=[common], help="free share, coordinator share, rework, time"
    )
    economy_parser.add_argument("--job", required=True, help="job id")
    economy_parser.set_defaults(handler=_cmd_economy)

    scoreboard_parser = subcommands.add_parser(
        "scoreboard", parents=[common], help="regenerate and print the scoreboard"
    )
    scoreboard_parser.set_defaults(handler=_cmd_scoreboard)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the CLI for ``argv`` (default: ``sys.argv[1:]``) and return a code."""
    _reconfigure_streams()
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except (PrefsError, RecordError, SidecarError, NotAGitRepo, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except RuntimeError as exc:
        first_line = str(exc).splitlines()[0] if str(exc).splitlines() else ""
        print(f"error: {first_line}", file=sys.stderr)
        return 1
