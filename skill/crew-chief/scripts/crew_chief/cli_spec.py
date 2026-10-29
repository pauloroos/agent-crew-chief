"""Spec import, status, new and brief rendering commands for the crew-chief CLI."""

from __future__ import annotations

import argparse
import copy
import re
import sys
from datetime import datetime
from pathlib import Path
from .sidecar import Sidecar, TaskMeta
from .speckit import SpecTask

__all__ = [
    "_SPEC_NUMBER_RE",
    "_SPEC_TEMPLATES",
    "_check_entry",
    "_cmd_brief_render",
    "_cmd_spec_import",
    "_cmd_spec_new",
    "_cmd_spec_status",
    "_entry_mismatch",
    "_load_spec",
    "_norm_path",
    "_parallel_conflicts",
    "_piece_task_text",
    "_review_target",
    "_spec_relative",
    "_squash",
    "_stale_text",
]

_SPEC_TEMPLATES = Path(__file__).resolve().parents[2] / "templates" / "spec"
_SPEC_NUMBER_RE = re.compile(r"^(\d{3})-")


def _cmd_spec_new(args: argparse.Namespace) -> int:
    from . import cli

    project = cli._project(args)
    title = args.title.strip()
    if not project.is_dir():
        raise ValueError(f"--project: not a directory: {project}")
    if not any(ch.isalnum() for ch in title):
        raise ValueError("--title needs at least one letter or digit")

    specs = project / "specs"
    numbers = [
        int(match.group(1))
        for entry in (specs.iterdir() if specs.is_dir() else [])
        if entry.is_dir() and (match := cli._SPEC_NUMBER_RE.match(entry.name))
    ]
    number = max(numbers, default=0) + 1
    if number > 999:
        raise ValueError("specs/ already holds 999 numbered folders")
    folder = specs / f"{number:03d}-{cli.records.slug(title)[:40].strip('-')}"
    if folder.exists():
        raise ValueError(f"{folder.relative_to(project).as_posix()} already exists")

    today = datetime.now().strftime("%Y-%m-%d")
    contents = {}
    for name in ("spec.md", "plan.md", "tasks.md"):
        text = cli._read_text(cli._SPEC_TEMPLATES / name, "template")
        contents[name] = text.replace("[FEATURE NAME]", title).replace("[DATE]", today)
    folder.mkdir(parents=True)
    for name, text in contents.items():
        (folder / name).write_text(text, encoding="utf-8", newline="\n")

    relative = folder.relative_to(project).as_posix()
    cli._emit(args, relative, {"spec_dir": relative})
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
    from . import cli

    folder = cli.find_spec_dir(project, spec_dir or job.get("spec_dir") or None)
    relative = cli._spec_relative(project, folder)
    tasks_path = folder / "tasks.md"
    if not tasks_path.is_file():
        raise cli.SpecError(f"tasks.md not found in {relative}")
    tasks = cli.parse_tasks(cli._read_text(tasks_path, "tasks.md"))
    return folder, relative, tasks, cli.load_sidecar(folder)


def _norm_path(path: str) -> str:
    return path.replace("\\", "/")


def _parallel_conflicts(
    tasks: list[SpecTask], sidecar: Sidecar
) -> list[tuple[str, str, list[str]]]:
    """``[P]`` tasks of one phase whose sidecar ``files`` overlap."""
    from . import cli

    open_tasks = [t for t in tasks if t.parallel and not t.done]
    conflicts = []
    for index, first in enumerate(open_tasks):
        for second in open_tasks[index + 1:]:
            if first.phase != second.phase:
                continue
            files = [
                {cli._norm_path(f) for f in sidecar.tasks[t.id].files}
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
    from . import cli

    text = piece.get("task_text")
    if isinstance(text, str):
        return cli._squash(text)
    prefix = f"{piece.get('ref')} "
    title = piece.get("title")
    if isinstance(title, str) and title.startswith(prefix):
        return cli._squash(title[len(prefix):])
    return None


def _entry_mismatch(meta: TaskMeta | None, task_text: str) -> bool:
    """Whether a ``crew.toml`` entry was written for a task with other text.

    Entries without a ``task`` (written before the field existed) never
    mismatch.
    """
    from . import cli

    return (
        meta is not None and bool(meta.task) and cli._squash(meta.task) != cli._squash(task_text)
    )


def _check_entry(ref: str, meta: TaskMeta | None, task_text: str, prefix: str = "") -> None:
    """Refuse (:class:`ValueError`) a ``crew.toml`` entry made for another task."""
    from . import cli

    if cli._entry_mismatch(meta, task_text):
        raise ValueError(
            f"{prefix}crew.toml entry {ref} was written for another task "
            f"({meta.task!r}); set its task to the current text once its fields "
            "fit, then retry"
        )


def _stale_text(stale: list[dict]) -> str:
    return ", ".join(f"{entry['piece']} ({entry['ref']})" for entry in stale)


def _cmd_spec_import(args: argparse.Namespace) -> int:
    from . import cli

    project = cli._project(args)
    job = cli.records.load_job(project, args.job)
    folder, relative, tasks, sidecar = cli._load_spec(project, job, args.spec_dir)
    bound = job.get("spec_dir")
    if bound and bound != relative:
        raise ValueError(f"job is bound to {bound}; use a new job")
    if not job.get("job_branch"):
        try:
            from .git_delivery import prepare_job_branch

            try:
                prefs = cli.load_prefs(project)
                base_branch = getattr(prefs, "base_branch", "main") or "main"
            except Exception:
                base_branch = "main"

            branch_name = prepare_job_branch(project, job["id"], base_branch=base_branch)
            job["job_branch"] = branch_name
            job["base_branch"] = base_branch
            cli.records.update_job(
                project,
                args.job,
                lambda stored: stored.update({"job_branch": branch_name, "base_branch": base_branch}) or stored,
            )
        except Exception:
            pass
    original = copy.deepcopy(sidecar)
    # Entries written for another task (spec-kit renumbered tasks.md): never
    # reused, never rewritten; dispatch and brief render refuse them.
    mismatched = [
        task.id for task in tasks if cli._entry_mismatch(sidecar.tasks.get(task.id), task.text)
    ]

    def apply(stored: dict) -> tuple[list[dict], list[dict], list[dict]]:
        stale = []
        for task in tasks:
            piece = cli.records.piece_for_ref(stored, task.id)
            if piece is not None and cli._piece_task_text(piece) not in (
                None,
                cli._squash(task.text),
            ):
                piece["stale"] = True
                stale.append({"piece": piece["n"], "ref": task.id})
        created = []
        for task in tasks:
            if task.done or cli.records.piece_for_ref(stored, task.id) is not None:
                continue
            meta = None if task.id in mismatched else sidecar.tasks.get(task.id)
            work_type = meta.type if meta else cli.guess_type(task)
            risk = meta.risk if meta else cli.guess_risk(task)
            number = cli.records.add_piece(
                stored,
                f"{task.id} {task.text}",
                work_type,
                risk,
                ref=task.id,
                superpowers=getattr(args, "superpowers", None),
            )
            stored["pieces"][number - 1]["task_text"] = task.text
            created.append(
                {"piece": number, "ref": task.id, "type": work_type, "risk": risk}
            )
        stored["spec_dir"] = relative
        stored["phase"] = "spec"
        if "authorization" in stored and isinstance(stored["authorization"], dict):
            stored["authorization"]["phase"] = "spec"
            stored["authorization"]["spec_path"] = str(relative)
        return created, stale, [dict(p) for p in stored["pieces"]]

    created, stale, pieces = cli.records.update_job(project, args.job, apply)

    by_ref = {
        p["ref"]: p for p in reversed(pieces) if p.get("ref") and not p.get("stale")
    }
    for task in tasks:
        if task.id in mismatched:
            continue
        meta = sidecar.tasks.get(task.id)
        if meta is None:
            if task.done:
                continue
            piece = by_ref.get(task.id)
            sidecar.tasks[task.id] = cli.TaskMeta(
                piece["type"] if piece else cli.guess_type(task),
                piece["risk"] if piece else cli.guess_risk(task),
                depends=list(task.depends),
                task=task.text,
            )
            continue
        if not meta.task:
            meta.task = task.text
        if not meta.depends and task.depends:
            meta.depends = list(task.depends)
    sidecar.spec_dir = relative
    sidecar.job = args.job
    if sidecar != original and cli.update_sidecar(folder, sidecar) == "rewritten":
        print("crew.toml rewritten; comments are not kept", file=sys.stderr)

    ids = {task.id for task in tasks}
    missing_proof = [
        task.id
        for task in tasks
        if not task.done
        and task.id in by_ref
        and not by_ref[task.id].get("accepted")
        and by_ref[task.id].get("type") in cli.records.EXEC_TYPES
        and not (
            task.id in sidecar.tasks
            and task.id not in mismatched
            and sidecar.tasks[task.id].proof
        )
    ]
    orphan_sidecar = sorted(set(sidecar.tasks) - ids)
    orphan_pieces = [
        p["ref"]
        for p in pieces
        if p.get("ref") and not p.get("stale") and p["ref"] not in ids
    ]
    conflicts = cli._parallel_conflicts(tasks, sidecar)

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
        lines.append("stale pieces: " + cli._stale_text(stale))
    lines.extend(
        f"crew.toml entry {ref} was written for another task; review it"
        for ref in mismatched
    )
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
        "sidecar_mismatch": mismatched,
        "parallel_conflicts": [
            {"tasks": [a, b], "files": shared} for a, b, shared in conflicts
        ],
    }
    cli._emit(args, "\n".join(lines), payload)
    return 0


def _cmd_spec_status(args: argparse.Namespace) -> int:
    from . import cli

    project = cli._project(args)
    job_id = args.job
    if not job_id:
        folder = cli.find_spec_dir(project, args.spec_dir)
        sidecar = cli.load_sidecar(folder)
        if not sidecar.job:
            raise ValueError(f"no --job provided and no job recorded in {folder}/crew.toml")
        job_id = sidecar.job
    job = cli.records.load_job(project, job_id)
    _, relative, tasks, sidecar = cli._load_spec(project, job, args.spec_dir)

    waiting = cli.readiness(job, tasks, sidecar)
    accepted_refs = {
        task.id
        for task in tasks
        if (piece := cli.records.piece_for_ref(job, task.id)) and piece.get("accepted")
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
        lines.append("stale: " + cli._stale_text(stale))
    payload = {
        "spec_dir": relative,
        "stale": stale,
        "phases": phases,
        "ready": ready,
        "blocked": blocked,
    }
    cli._emit(args, "\n".join(lines), payload)
    return 0


def _review_target(
    project: Path, piece: dict, number: int, attempt_n: int | None
) -> tuple[str, str, str]:
    """``(worktree, short start sha, short end sha)`` of the attempt to review.

    ``attempt_n`` names an exec attempt of the piece; ``None`` takes the last
    ``ok`` one. Attempts recorded before ``start_sha``/``end_sha`` existed get
    them from git: the tip of their branch and its merge base with ``HEAD``.
    """
    from . import cli

    if attempt_n is None:
        attempt = cli._last_ok_exec_attempt(piece)
        if attempt is None:
            raise ValueError(f"piece {number} has no ok exec attempt to review")
    else:
        attempt = next(
            (
                a
                for a in piece.get("attempts") or []
                if a.get("role") == "exec" and a.get("n") == attempt_n
            ),
            None,
        )
        if attempt is None:
            raise ValueError(f"piece {number} has no exec attempt {attempt_n}")
    if not attempt.get("worktree"):
        raise ValueError(f"attempt {attempt.get('n')} of piece {number} has no worktree")
    try:
        end = attempt.get("end_sha") or attempt.get("branch") or ""
        start = attempt.get("start_sha") or cli.merge_base(project, "HEAD", end)
        return attempt["worktree"], cli.short_sha(project, start), cli.short_sha(project, end)
    except RuntimeError as exc:
        raise ValueError(
            f"cannot find the commits of attempt {attempt.get('n')} of piece "
            f"{number} (accepted or discarded already?)"
        ) from exc


def _cmd_brief_render(args: argparse.Namespace) -> int:
    from . import cli

    project = cli._project(args)
    job = cli.records.load_job(project, args.job)
    if args.review and args.converge:
        raise ValueError("--review and --converge cannot be combined")
    if args.attempt is not None and not args.review:
        raise ValueError("--attempt is for --review")
    number = args.piece
    if args.converge:
        folder = cli.find_spec_dir(project, job.get("spec_dir") or None)
        kind = "converge"
        brief = cli.render_converge_brief(cli._spec_relative(project, folder))
    else:
        if number is None:
            raise ValueError("brief render needs --piece (or --converge)")
        piece = cli._piece(job, number)
        ref = piece.get("ref")
        if not ref:
            print(
                f"piece {number} has no spec task; write the brief to a file "
                "and pass it with --brief-file, or link the piece to a spec task",
                file=sys.stderr,
            )
            return 1
        if piece.get("stale"):
            print(
                f"piece {number} is stale (task {ref} changed); re-import the spec",
                file=sys.stderr,
            )
            return 1
        folder, relative, tasks, sidecar = cli._load_spec(project, job, None)
        task = next((t for t in tasks if t.id == ref), None)
        if task is None:
            raise ValueError(
                f"task {ref} not found in {relative}/tasks.md; re-import the spec"
            )
        cli._check_entry(ref, sidecar.tasks.get(ref), task.text, f"piece {number} ({ref}): ")
        meta = sidecar.tasks.get(ref) or cli.TaskMeta(
            piece.get("type") or "code", piece.get("risk") or "medium"
        )
        spec_path = folder / "spec.md"
        index = (
            cli.parse_spec_refs(cli._read_text(spec_path, "spec.md"))
            if spec_path.is_file()
            else {}
        )
        found, missing = cli.resolve_refs(list(dict.fromkeys(meta.accept)), index)
        refs = found + [f"UNRESOLVED: {item}" for item in missing]
        ponytail_level = cli.load_prefs(project).ponytail_level
        if args.review:
            worktree, start, end = cli._review_target(project, piece, number, args.attempt)
            kind = "review"
            brief = cli.render_review_brief(
                task, meta, refs, sidecar.global_constraints, worktree, start, end,
                ponytail_level,
            )
        else:
            kind = "exec"
            brief = cli.render_exec_brief(
                task, meta, refs, sidecar.global_constraints, folder.name,
                ponytail_level,
            )
    payload = {"job": args.job, "piece": number, "kind": kind, "brief": brief}
    if args.out:
        target = Path(args.out).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(brief, encoding="utf-8", newline="\n")
        payload["out"] = str(target)
        cli._emit(args, f"wrote {target}", payload)
    else:
        cli._emit(args, brief.rstrip("\n"), payload)
    return 0
