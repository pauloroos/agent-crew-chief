"""One attempt end to end: brief filter, worktree, process, containment.

:func:`dispatch` is the coordinator's single attempt runner. It wires the
other modules together in a fixed order: a brief that leaks a secret is
refused before anything is created; the executor CLI runs inside a throwaway
worktree with a hard timeout and its process tree is killed when it overruns;
the status then follows from the exit code, the quota patterns, the two
containment snapshots taken around the run and, for an exec, whether the
worktree changed at all (a clean exit that changed nothing is ``no_change``).

The brief travels as a file (``.crew-chief-brief.md``) rather than on the
command line, so long or accented text survives; the prompt names it, and the
worktree, by absolute path (:func:`build_prompt`). Some tools write their answer
to ``.crew-chief-out.txt`` instead of stdout; that file is read only when
stdout came back empty. Both files are listed in the worktree's
``info/exclude`` by :mod:`crew_chief.worktree`.

Every attempt also records how much work it took: ``duration_s``, the agent
``steps`` and the tokens (``tokens_in``, ``tokens_out``, ``tokens_total``)
the CLI reported, or a characters / 4 estimate flagged ``tokens_estimated``.

The note keeps the cause of a failure, not only the answer: the provider
error of an ``opencode`` run (its type, its message and its HTTP status,
never the raw response body) or a quota line on stderr or in the output
comes first, so a long output cannot tail-truncate it.

A review is the same run without a worktree of its own: it uses the
``review_target`` it was given, commits nothing and removes its brief file
afterwards.

An exec attempt starts from ``HEAD``, except a correction: when the piece's
last ``ok`` exec attempt drew a correction after it started, the new worktree
starts from that attempt's branch, so the executor finds the earlier work and
the brief only has to describe the adjustment. ``base`` names the starting
point explicitly (an attempt number of the piece or a branch) and ``fresh``
forces ``HEAD``; the attempt records its ``base`` (``None`` for ``HEAD``) and
its diffstat is measured against that starting point.
"""

from __future__ import annotations

import fnmatch
import os
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from .briefcheck import check_brief
from . import spend
from .executor_slots import ExecutorSlots, LeaseError
from .interview import check_interview_gate
from .kit import render_kit_brief, resolve_superpowers
from .records import add_attempt, load_job, update_job
from .sdd_gate import check_spec_gate
from .tools import (
    QUOTA_PATTERNS,
    Executor,
    build_command,
    output_text,
    parse_metrics,
    parse_reported_model,
    provider_error,
    tool_env,
    wants_effort,
)
from .worktree import (
    EXCLUDED_FILES,
    commit_worktree,
    create_worktree,
    diffstat,
    branch_exists,
    head_commit,
    hide_sensitive,
    next_free_attempt,
    outside_writes,
    remove_paths,
    sensitive_present,
    snapshot,
)

#: The brief file the executor is told to read, inside the worktree.
BRIEF_FILE = ".crew-chief-brief.md"

#: Optional output file some tools write instead of stdout.
OUTPUT_FILE = ".crew-chief-out.txt"

#: Prompt handed to every executor, formatted by :func:`build_prompt`. Both
#: paths are absolute: some agent tools hide dotfiles from their file search,
#: so a brief named relative to the cwd was not always found.
PROMPT_TEMPLATE = (
    "Read the file {brief} and carry out exactly what it asks. "
    "Work only inside {worktree}. You are an executor dispatched by "
    "crew-chief so do not use the crew-chief skill and do not dispatch other "
    "agents even if AGENTS.md or CLAUDE.md say so."
)

#: Appended to the prompt for a review: the target must not change.
REVIEW_SUFFIX = " Do not modify any file; reply with your review."


def build_prompt(brief_file: Path, worktree: Path, review: bool = False) -> str:
    """The prompt for one attempt, naming the brief and worktree absolutely."""
    prompt = PROMPT_TEMPLATE.format(
        brief=Path(brief_file).resolve(), worktree=Path(worktree).resolve()
    )
    return prompt + (REVIEW_SUFFIX if review else "")


def git_identity_error(repo: Path) -> str | None:
    """Why Git has no usable author/committer identity in ``repo``, or None.

    Asks ``git var`` with the current environment, so ``GIT_AUTHOR_*`` /
    ``GIT_COMMITTER_*`` and repo, global or system config all count. Guessed
    identities do not: ``user.useConfigOnly`` stops Git from making one up
    from the user and host name (``runner@Mac.local`` on macOS) or ``EMAIL``.
    A repo that is not a Git repository, or a missing ``git``, is an error
    too: the check never fails open. Never writes Git config or invents an
    identity.
    """
    for args in (
        ["rev-parse", "--git-dir"],
        ["-c", "user.useConfigOnly=true", "var", "GIT_AUTHOR_IDENT"],
        ["-c", "user.useConfigOnly=true", "var", "GIT_COMMITTER_IDENT"],
    ):
        try:
            proc = subprocess.run(
                ["git", *args], cwd=repo, capture_output=True, text=True,
                encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return f"cannot run git in {repo}: {exc}"
        if proc.returncode != 0:
            detail = " ".join((proc.stderr or proc.stdout or "").split())[:200]
            label = " ".join(args[2:] if args[0] == "-c" else args)
            return f"{label}: {detail}" if detail else label
    return None


def git_identity_note(repo: Path) -> str | None:
    """The infrastructure diagnostic for a missing identity in ``repo``, or None."""
    reason = git_identity_error(repo)
    if reason is None:
        return None
    return (
        f"infrastructure: git identity missing ({reason}). "
        f"Git needs an author identity in {Path(repo).name}: set user.name and "
        "user.email (git config) or GIT_AUTHOR_NAME/GIT_AUTHOR_EMAIL"
    )


#: How many trailing characters of executor output are kept in the note.
MAX_NOTE = 4000

#: How many trailing characters of a proof's output go into the note.
MAX_PROOF_NOTE = 2000


def dispatch(
    project: Path,
    job_id: str,
    piece: int,
    executor: Executor,
    brief: str,
    prefs,
    role: str = "exec",
    effort: str | None = None,
    timeout_s: int | None = None,
    review_target: Path | None = None,
    base: str | None = None,
    fresh: bool = False,
    proof: str = "",
    proof_expect: str = "",
    allowed_files: list[str] | None = None,
    skip_proof: bool = False,
    ignored_deps: list[str] | None = None,
    slots: ExecutorSlots | None = None,
    superpowers: str | None = None,
) -> dict:
    """Run one attempt, record it in the job and return the attempt dict.

    ``role`` is ``"exec"`` (create a worktree and commit the result) or
    ``"review"`` (run inside ``review_target``, commit nothing). A review
    without a ``review_target`` raises :class:`ValueError`.

    The returned dict is also appended to ``piece`` of the job and the job is
    saved, whatever the status: ``ok``, ``no_change`` (an exec that exited
    cleanly without changing the worktree), ``failed``, ``timeout``,
    ``quota``, ``outside_write``, ``proof_failed`` or ``brief_refused``.

    The project's ``sensitive_paths`` stay out of the executor's reach: an exec
    worktree is created without the tracked ones (:func:`hide_sensitive`), and
    after the run, exec or review, any sensitive path found in the worktree is
    deleted and makes the attempt ``outside_write`` with the note
    ``sensitive path in worktree: <paths>``; an exec of that kind is not
    committed.

    An exec attempt starts from what :func:`resolve_base` returns for
    ``base`` and ``fresh``; a review takes neither.

    An exec that would be ``ok`` runs ``proof`` afterwards (a shell command in
    the worktree, after the commit, with the same timeout as the executor and
    no stdin). A non-zero exit, a timeout, a command that cannot run or output
    that lacks ``proof_expect`` makes the status ``proof_failed``; the note then
    starts with ``proof: <command>`` and the end of the output, and the attempt
    records ``proof: {"cmd", "exit", "passed"}`` (``exit`` is ``None`` for a
    timeout). ``skip_proof`` records ``proof: {"skipped": true}`` instead. When
    ``allowed_files`` is given, the attempt lists the changed files that match
    none of its patterns under ``scope``; that never changes the status.

    ``ignored_deps`` (what the caller dispatched in spite of: a dependency or
    an earlier phase) is stored on the attempt as given.

    An exec attempt whose worktree survived the run records ``start_sha`` (the
    commit the worktree started from) and ``end_sha`` (its ``HEAD`` after the
    commit), so a review can be pinned to exactly that work.

    Precondition: ``piece`` must exist in the job and ``base`` must name an
    existing branch; otherwise ValueError is raised and nothing is recorded.
    """
    project = Path(project)
    if role == "review" and review_target is None:
        raise ValueError("role 'review' requires review_target")
    if role == "review" and (base or fresh):
        raise ValueError("a review runs in its target: base and fresh are for exec")

    job = load_job(project, job_id)
    check_interview_gate(job, prefs=prefs)

    identity = git_identity_note(project)

    piece_ref: str | None = None

    def reserve(job: dict) -> tuple[int, dict | None]:
        check_interview_gate(job, prefs=prefs)
        nonlocal piece_ref
        try:
            for entry in job.get("pieces") or []:
                if entry.get("n") == piece:
                    ref = entry.get("ref")
                    piece_ref = ref if isinstance(ref, str) and ref else None
                    break
        except AttributeError:
            pass
        start_from = (
            resolve_base(job, piece, project, base, fresh)
            if role == "exec" and not identity
            else None
        )
        return _reserve_attempt(job, piece, project, role == "exec" and not identity), start_from

    number, start_from = update_job(project, job_id, reserve)
    started = datetime.now(timezone.utc)
    start = time.monotonic()

    piece_entry = next(
        (p for p in job.get("pieces") or [] if p.get("n") == piece), {}
    )
    sp_pref = "off"
    if prefs is not None:
        if hasattr(prefs, "superpowers"):
            sp_pref = prefs.superpowers
        elif isinstance(prefs, dict) and "superpowers" in prefs:
            sp_pref = prefs["superpowers"]
    piece_flag = piece_entry.get("superpowers") if isinstance(piece_entry, dict) else None
    job_flag = job.get("superpowers") if isinstance(job, dict) else None
    task_type = None
    task_text = None
    if isinstance(piece_entry, dict):
        task_type = piece_entry.get("work_type") or piece_entry.get("type")
        task_text = piece_entry.get("text") or piece_entry.get("title") or piece_entry.get("task_text")

    sp_mode, sp_skill = resolve_superpowers(
        pref=sp_pref,
        job_flag=job_flag,
        piece_flag=piece_flag,
        dispatch_flag=superpowers,
        task_type=task_type,
        task_text=task_text,
    )

    attempt: dict = {
        "n": number,
        "executor": executor.id,
        "role": role,
        "cost": executor.cost,
        "started": started.isoformat(),
        "duration_s": 0.0,
        "status": "failed",
        "reported_model": None,
        "model_mismatch": False,
        "note": "",
        "diffstat": [],
        "destructive_suspect": [],
        "outside_writes": [],
        "branch": None,
        "worktree": None,
        "base": None,
        "steps": None,
        "tokens_in": None,
        "tokens_out": None,
        "tokens_total": None,
        "tokens_estimated": False,
        "cost_usd": None,
        "cost_known": False,
        "superpowers": {
            "mode": sp_mode,
            "skill": sp_skill,
        },
    }
    if ignored_deps:
        attempt["ignored_deps"] = list(ignored_deps)

    def finish(status: str, note: str | None = None) -> dict:
        """Set the final status and append the attempt to the job on disk.

        The job is re-read under its lock at this moment, so attempts that
        parallel dispatches recorded meanwhile are kept.
        """
        attempt["status"] = status
        if note is not None:
            attempt["note"] = note
        attempt["duration_s"] = round(time.monotonic() - start, 1)
        update_job(project, job_id, lambda job: add_attempt(job, piece, attempt))
        return attempt

    findings = check_brief(brief, prefs.sensitive_paths)
    if findings:
        return finish("brief_refused", "\n".join(findings))

    # T029 — Block code dispatch until spec tasks reviewed (US7, FR-033)
    current_job = load_job(project, job_id)
    spec_gate_error = check_spec_gate(project, current_job, piece, role=role)
    if spec_gate_error:
        return finish("brief_refused", spec_gate_error)

    if identity:
        return finish("failed", identity)

    if role == "review":
        worktree = Path(review_target)
    else:
        try:
            worktree, branch = create_worktree(
                project,
                job_id,
                piece,
                number,
                start=start_from["branch"] if start_from else "HEAD",
            )
        except RuntimeError as exc:
            return finish("failed", f"cannot create worktree: {exc}")
        attempt["branch"] = branch
        attempt["worktree"] = str(worktree)
        attempt["base"] = start_from
        try:
            hide_sensitive(worktree, prefs.sensitive_paths)
        except (RuntimeError, OSError) as exc:
            return finish("failed", f"cannot hide sensitive paths: {exc}")

    # Inject base kit (ponytail + rtk + superpowers) into the brief
    job = load_job(project, job_id)
    piece_entry = next(
        (p for p in job.get("pieces") or [] if p.get("n") == piece), {}
    )
    kit_lines = render_kit_brief(
        role=role,
        backend=executor.tool.get("name", ""),
        config=prefs,
        piece=piece_entry,
        job=job,
        is_reseller=bool(executor.tool.get("reseller")),
        dispatch_flag=superpowers,
        brief=brief,
    )
    kit_text = "\n".join(kit_lines) if kit_lines else ""
    combined_brief = f"{kit_text}\n{brief}" if kit_text else brief

    brief_file = worktree / BRIEF_FILE
    output_file = worktree / OUTPUT_FILE
    brief_file.write_text(combined_brief, encoding="utf-8")

    tool = executor.tool
    prompt = build_prompt(brief_file, worktree, role == "review")
    cmd = build_command(
        tool,
        executor.model,
        worktree,
        prompt,
        brief_file,
        output_file,
        effort if wants_effort(tool) else None,
        role == "review",
        superpowers=bool(sp_skill),
    )

    env = tool_env(tool)
    if env is None:
        _remove_brief(role, brief_file)
        names = ", ".join(str(name) for name in tool.get("env", []))
        return finish("failed", f"missing env: {names}")

    ignore_home = prefs.ignore_home_entries
    start_head = head_commit(worktree) if role == "exec" else None
    before = snapshot(project, worktree, ignore_home)
    slots_obj = slots
    if slots_obj is None:
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
        slots_obj = ExecutorSlots(lease_dir=lease_dir, max_running=max_running)

    def _on_waiting() -> None:
        wait_event = {
            "type": "waiting_for_executor_slot",
            "reason": f"queued for executor slot (max_running={slots_obj.max_running})",
            "piece": piece,
            "attempt": number,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        try:
            update_job(
                project,
                job_id,
                lambda j: j.setdefault("events", []).append(wait_event),
            )
        except Exception:
            pass

    try:
        slots_obj.acquire(
            f"{job_id}-p{piece}-a{number}",
            timeout_s=float(timeout_s or prefs.dispatch_timeout_s),
            on_waiting=_on_waiting,
        )
    except LeaseError as exc:
        _remove_brief(role, brief_file)
        return finish("failed", f"could not acquire executor slot: {exc}")

    try:
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=worktree,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                start_new_session=(os.name != "nt"),
            )
        except OSError as exc:
            _remove_brief(role, brief_file)
            return finish("failed", f"cannot start {cmd[0]}: {exc}")

        timed_out = False
        try:
            stdout, stderr = proc.communicate(timeout=timeout_s or prefs.dispatch_timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_process_tree(proc)
            stdout, stderr = proc.communicate()
    finally:
        slots_obj.release()

    after = snapshot(project, worktree, ignore_home)
    writes = outside_writes(before, after)
    attempt["outside_writes"] = writes

    text = stdout or ""
    if text == "" and output_file.exists():
        text = output_file.read_text(encoding="utf-8", errors="replace")
    answer = output_text(tool, text)
    cause = _cause(tool, text, answer, stderr)
    if not answer.strip() and (stderr or "").strip():
        # A run that printed nothing would leave no clue; keep its stderr.
        body = f"stderr:\n{stderr}"
    else:
        body = answer
    attempt["note"] = _note(body, cause)
    sensitive, check_error = _clean_sensitive(worktree, prefs.sensitive_paths)
    if check_error:
        failed = f"sensitive path check failed: {check_error}"
        attempt["note"] = f"{failed}\n\n{attempt['note']}".rstrip()
    if sensitive:
        writes = [*writes, *(f"sensitive: {name}" for name in sensitive)]
        attempt["outside_writes"] = writes
        found = f"sensitive path in worktree: {', '.join(sensitive)}"
        attempt["note"] = f"{found}\n\n{attempt['note']}".rstrip()
    attempt.update(_metrics(tool, brief, text, answer, stderr))
    attempt["cost_known"] = attempt["cost_usd"] is not None
    _record_spend(attempt, project, job_id, piece)

    reported = parse_reported_model(tool, text)
    attempt["reported_model"] = reported
    if reported:
        attempt["model_mismatch"] = not (
            executor.model in reported or reported in executor.model
        )

    changed = True
    if role == "exec":
        try:
            # Against the starting point, so a correction counts only the
            # adjustment and work the executor committed itself still counts.
            rows, destructive = diffstat(worktree, start_head)
            rows = [row for row in rows if row["file"] not in EXCLUDED_FILES]
            attempt["diffstat"] = rows
            attempt["destructive_suspect"] = destructive
            if allowed_files is not None:
                outside = [
                    row["file"]
                    for row in rows
                    if not _in_scope(row["file"], allowed_files)
                ]
                if outside:
                    attempt["scope"] = outside
            changed = bool(rows) or head_commit(worktree) != start_head
            if not sensitive:
                commit_worktree(
                    worktree, f"crew: {job_id} piece {piece} attempt {number}"
                )
            # What a reviewer must find in the worktree (see brief render).
            attempt["start_sha"] = start_head
            attempt["end_sha"] = head_commit(worktree)
        except RuntimeError as exc:
            note = f"broken worktree after the run: {exc}"
            return finish("failed", f"{note}\n\n{attempt['note']}".rstrip())
    else:
        _remove_brief(role, brief_file)

    status = _status(timed_out, writes, proc.returncode, stderr, text, changed)
    if skip_proof:
        attempt["proof"] = {"skipped": True}
    elif role == "exec" and status == "ok" and proof:
        result, proof_note = _run_proof(
            proof, proof_expect, worktree, timeout_s or prefs.dispatch_timeout_s
        )
        attempt["proof"] = result
        if not result["passed"]:
            status = "proof_failed"
            attempt["note"] = f"{proof_note}\n\n{attempt['note']}".rstrip()
    return finish(status)


def _clean_sensitive(
    worktree: Path, patterns: list[str]
) -> tuple[list[str], str]:
    """Delete the sensitive paths found in ``worktree``.

    Returns ``(found, error)``. A failure to look or to delete never fails
    the attempt by itself: what was found is still reported and ``error``
    says what went wrong (empty when all went well), so the caller can
    note it instead of failing open silently.
    """
    try:
        found = sensitive_present(worktree, patterns)
    except (RuntimeError, OSError) as exc:
        return [], str(exc)
    try:
        remove_paths(worktree, found)
    except OSError as exc:
        return found, str(exc)
    return found, ""


def _in_scope(path: str, allowed: list[str]) -> bool:
    """Whether ``path`` matches one of the ``allowed`` patterns.

    A pattern is an :mod:`fnmatch` pattern on the ``/``-separated path; one
    that ends with ``/`` stands for the whole folder.
    """
    for pattern in allowed:
        if pattern.endswith("/"):
            if path.startswith(pattern):
                return True
        elif fnmatch.fnmatchcase(path, pattern):
            return True
    return False


def _run_proof(
    proof: str, expect: str, worktree: Path, timeout_s: int
) -> tuple[dict, str]:
    """Run the proof command in ``worktree``; return its record and note.

    The record is ``{"cmd", "exit", "passed"}``: ``exit`` is ``None`` when the
    command timed out or could not start. The note is ``proof: <command>``
    followed by the end of the output and, when relevant, why the proof
    failed. Never raises and never waits past ``timeout_s`` (plus a short
    grace to collect the output of the killed tree).
    """
    result: dict = {"cmd": proof, "exit": None, "passed": False}
    header = f"proof: {proof}"
    try:
        proc = subprocess.Popen(
            proof,
            shell=True,
            cwd=worktree,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=(os.name != "nt"),
        )
    except OSError as exc:
        return result, f"{header}\ncannot run the proof: {exc}"

    timed_out = False
    try:
        output, _ = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_process_tree(proc)
        try:
            output, _ = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:  # a stray holder of the pipe
            output = ""
    output = output or ""
    tail = output[-MAX_PROOF_NOTE:]
    body = f"{header}\n{tail}".rstrip("\n")
    if timed_out:
        return result, f"{body}\ntimed out after {timeout_s}s"

    result["exit"] = proc.returncode
    if proc.returncode != 0:
        return result, f"{body}\nexit {proc.returncode}"
    if expect and expect not in output:
        return result, f"{body}\nexpected text not found: {expect}"
    result["passed"] = True
    return result, body


def resolve_base(
    job: dict,
    piece: int,
    project: Path,
    base: str | None = None,
    fresh: bool = False,
) -> dict | None:
    """Where a new exec attempt of ``piece`` starts: ``{"attempt", "branch"}``.

    ``None`` means ``HEAD``. ``fresh`` always gives ``None``. ``base`` is an
    attempt number of the piece (digits) or a branch name; ``attempt`` is
    ``None`` for a branch no exec attempt of the piece owns. Without either,
    the piece's last ``ok`` exec attempt is the start when a correction was
    recorded after that attempt started and its branch still exists (an
    accepted or discarded branch is gone, and ``HEAD`` then holds whatever
    landed). Raises :class:`ValueError` for ``base`` together with ``fresh``,
    an attempt number the piece does not have, or a branch that does not
    exist.
    """
    if base and fresh:
        raise ValueError("base and fresh exclude each other (fresh starts from HEAD)")
    if fresh:
        return None
    entry = _piece_entry(job, piece)
    attempts = [
        attempt
        for attempt in entry.get("attempts") or []
        if isinstance(attempt, dict) and attempt.get("role") == "exec"
    ]

    if base:
        if base.isdigit():
            owner = next(
                (
                    attempt
                    for attempt in attempts
                    if attempt.get("n") == int(base) and attempt.get("branch")
                ),
                None,
            )
            if owner is None:
                raise ValueError(
                    f"piece {piece} has no exec attempt {base} with a branch"
                )
            branch = owner["branch"]
        else:
            branch = base
            owner = next(
                (attempt for attempt in attempts if attempt.get("branch") == branch),
                None,
            )
        if not branch_exists(project, branch):
            raise ValueError(
                f"base branch {branch!r} does not exist "
                "(accepted or discarded already?); use --fresh to start from HEAD"
            )
        return {"attempt": owner.get("n") if owner else None, "branch": branch}

    last_ok = next(
        (attempt for attempt in reversed(attempts) if attempt.get("status") == "ok"),
        None,
    )
    if last_ok is None or not last_ok.get("branch"):
        return None
    started = _parse_iso(last_ok.get("started"))
    if started is None:
        return None
    corrected = any(
        (when := _parse_iso(correction.get("at"))) is not None and when > started
        for correction in entry.get("corrections") or []
        if isinstance(correction, dict)
    )
    if not corrected or not branch_exists(project, last_ok["branch"]):
        return None
    return {"attempt": last_ok.get("n"), "branch": last_ok["branch"]}


def _piece_entry(job: dict, piece: int) -> dict:
    """The piece numbered ``piece``; :class:`ValueError` when it is absent."""
    for entry in job.get("pieces") or []:
        if entry.get("n") == piece:
            return entry
    raise ValueError(f"job {job.get('id')!r} has no piece {piece}")


def _parse_iso(text: object) -> datetime | None:
    """An ISO 8601 timestamp as an aware datetime (UTC when naive), or ``None``."""
    if not isinstance(text, str):
        return None
    try:
        when = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _reserve_attempt(
    job: dict, piece: int, project: Path, needs_worktree: bool
) -> int:
    """Reserve and return the next attempt number of ``piece``.

    Called under the job lock. The number is ``1 +`` the attempts already
    recorded, whatever their role, but never below the piece's
    ``next_attempt`` counter, which is then advanced: two parallel dispatches
    of one piece get distinct numbers (and worktrees) even though neither has
    recorded its attempt yet. For an exec attempt the number also skips the
    worktrees and branches that already exist (an attempt whose record was
    lost still holds them).
    """
    for entry in job.get("pieces") or []:
        if entry.get("n") == piece:
            number = max(
                1 + len(entry.get("attempts") or []),
                int(entry.get("next_attempt") or 1),
            )
            if needs_worktree:
                number = next_free_attempt(project, job["id"], piece, number)
            entry["next_attempt"] = number + 1
            return number
    raise ValueError(f"job {job.get('id')!r} has no piece {piece}")


def _record_spend(attempt: dict, project: Path, job_id: str, piece: int) -> None:
    """Append the attempt to the global spend log when it cost money.

    Only the ``cheap`` and ``paid`` tiers are recorded; a cost the CLI did
    not report is written as 0.0 with ``known`` false. A log that cannot be
    written is noted on the attempt instead of failing it.
    """
    if attempt["cost"] not in ("cheap", "paid"):
        return
    try:
        spend.record({
            "at": datetime.now(timezone.utc).isoformat(),
            "project": str(project),
            "job": job_id,
            "piece": piece,
            "executor": attempt["executor"],
            "tier": attempt["cost"],
            "cost_usd": attempt["cost_usd"] or 0.0,
            "known": attempt["cost_known"],
        })
    except (OSError, spend.SpendLockTimeout) as exc:
        note = f"{attempt['note']}\n[spend not recorded: {exc}]"
        attempt["note"] = note.strip()


def _metrics(
    tool: dict, brief: str, text: str, answer: str, stderr: str | None
) -> dict:
    """Steps and tokens of the run, estimated when the CLI reported none.

    Without a reported token total the tokens are estimated as one per four
    characters (rounded up) of the brief (in) and of the readable answer
    (out), and ``tokens_estimated`` is set; steps stay ``None`` unless the
    CLI reported them.
    """
    metrics = parse_metrics(tool, text, stderr or "")
    metrics["tokens_estimated"] = False
    if metrics["tokens_total"] is None:
        tokens_in = -(-len(brief) // 4)
        tokens_out = -(-len(answer) // 4)
        metrics.update(
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            tokens_total=tokens_in + tokens_out,
            tokens_estimated=True,
        )
    return metrics


def _status(
    timed_out: bool,
    writes: list[str],
    returncode: int | None,
    stderr: str | None,
    text: str,
    changed: bool = True,
) -> str:
    """Status from the run's outcome: timeout and containment first.

    A clean exit that left the worktree untouched (``changed`` false, only
    ever for an exec) is ``no_change``: the executor did not do the work.
    """
    if timed_out:
        return "timeout"
    if writes:
        return "outside_write"
    if returncode != 0:
        return "quota" if _quota_hit(stderr, text) else "failed"
    if not changed:
        return "no_change"
    return "ok"


def _quota_hit(stderr: str | None, text: str) -> bool:
    """Whether stderr or the output text matches any :data:`QUOTA_PATTERNS`."""
    haystack = f"{stderr or ''}\n{text or ''}"
    return any(pattern.search(haystack) for pattern in QUOTA_PATTERNS)


def _quota_lines(text: str | None) -> list[str]:
    """The quota/rate-limit lines of ``text``, in order.

    A line that is a JSON object is left out: a structured response is
    never shown as a cause.
    """
    lines = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("{") or line.startswith("["):
            continue
        if any(pattern.search(line) for pattern in QUOTA_PATTERNS):
            lines.append(line)
    return lines


def _cause(tool: dict, text: str, answer: str, stderr: str) -> str:
    """Why the attempt failed, as readable text.

    The provider error of the run's own events comes first (its type, its
    message and its HTTP status, never the raw response body); without
    one, the quota lines on stderr and in the answer.
    """
    error = provider_error(tool, text)
    if error:
        return error
    lines = []
    for line in [*_quota_lines(stderr), *_quota_lines(answer)]:
        if line not in lines:
            lines.append(line)
    return "\n".join(lines)


def _note(body: str, cause: str) -> str:
    """The note of an attempt: the cause first, then the readable output.

    The cause (the provider error, a quota line) comes first and is never
    truncated: a long output cannot tail-truncate the diagnosis. The
    output keeps its own tail, trimmed to what is left of
    :data:`MAX_NOTE`; a cause longer than the note budget is kept whole
    and the note grows past it.
    """
    cause = cause.strip()
    if not cause:
        return body[-MAX_NOTE:]
    said = [line.strip() for line in cause.splitlines() if line.strip()]
    body = "\n".join(
        line for line in body.splitlines() if line.strip() not in said
    )
    if not body.strip():
        return cause
    room = MAX_NOTE - len(cause) - 2
    tail = body[-room:].strip("\n") if room > 0 else ""
    return f"{cause}\n\n{tail}" if tail else cause


def _remove_brief(role: str, brief_file: Path) -> None:
    """Remove the brief file a review wrote into its target; exec keeps it."""
    if role == "review":
        brief_file.unlink(missing_ok=True)


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """Kill ``proc`` and everything it started.

    On Windows ``taskkill /T /F`` walks the child tree; on POSIX the child
    leads its own session (``start_new_session=True``), so killing the process
    group takes its descendants with it.
    """
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:  # pragma: no cover - the child already exited
        proc.kill()
