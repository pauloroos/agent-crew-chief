"""One attempt end to end: brief filter, worktree, process, containment.

:func:`dispatch` is the coordinator's single attempt runner. It wires the
other modules together in a fixed order: a brief that leaks a secret is
refused before anything is created; the executor CLI runs inside a throwaway
worktree with a hard timeout and its process tree is killed when it overruns;
the status then follows from the exit code, the quota patterns and the two
containment snapshots taken around the run.

The brief travels as a file (``.crew-chief-brief.md``) rather than on the
command line, so long or accented text survives. Some tools write their answer
to ``.crew-chief-out.txt`` instead of stdout; that file is read only when
stdout came back empty. Both files are listed in the worktree's
``info/exclude`` by :mod:`crew_chief.worktree`.

Every attempt also records how much work it took: ``duration_s``, the agent
``steps`` and the tokens (``tokens_in``, ``tokens_out``, ``tokens_total``)
the CLI reported, or a characters / 4 estimate flagged ``tokens_estimated``.

A review is the same run without a worktree of its own: it uses the
``review_target`` it was given, commits nothing and removes its brief file
afterwards.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from .briefcheck import check_brief
from .records import add_attempt, update_job
from .tools import (
    QUOTA_PATTERNS,
    Executor,
    build_command,
    output_text,
    parse_metrics,
    parse_reported_model,
    tool_env,
    wants_effort,
)
from .worktree import (
    commit_worktree,
    create_worktree,
    diffstat,
    outside_writes,
    snapshot,
)

#: The brief file the executor is told to read, inside the worktree.
BRIEF_FILE = ".crew-chief-brief.md"

#: Optional output file some tools write instead of stdout.
OUTPUT_FILE = ".crew-chief-out.txt"

#: Prompt handed to every executor; it points at the brief file in the cwd.
PROMPT = (
    "Read the file .crew-chief-brief.md in the current directory and carry "
    "out exactly what it asks. Work only inside the current directory."
)

#: Appended to the prompt for a review: the target must not change.
REVIEW_SUFFIX = " Do not modify any file; reply with your review."

#: How many trailing characters of executor output are kept in the note.
MAX_NOTE = 4000


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
) -> dict:
    """Run one attempt, record it in the job and return the attempt dict.

    ``role`` is ``"exec"`` (create a worktree and commit the result) or
    ``"review"`` (run inside ``review_target``, commit nothing). A review
    without a ``review_target`` raises :class:`ValueError`.

    The returned dict is also appended to ``piece`` of the job and the job is
    saved, whatever the status: ``ok``, ``failed``, ``timeout``, ``quota``,
    ``outside_write`` or ``brief_refused``.

    Precondition: ``piece`` must exist in the job; otherwise ValueError is
    raised and nothing is recorded.
    """
    project = Path(project)
    if role == "review" and review_target is None:
        raise ValueError("role 'review' requires review_target")

    number = update_job(project, job_id, lambda job: _reserve_attempt(job, piece))
    started = datetime.now(timezone.utc)
    start = time.monotonic()

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
        "steps": None,
        "tokens_in": None,
        "tokens_out": None,
        "tokens_total": None,
        "tokens_estimated": False,
    }

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

    if role == "review":
        worktree = Path(review_target)
    else:
        worktree, branch = create_worktree(project, job_id, piece, number)
        attempt["branch"] = branch
        attempt["worktree"] = str(worktree)

    brief_file = worktree / BRIEF_FILE
    output_file = worktree / OUTPUT_FILE
    brief_file.write_text(brief, encoding="utf-8")

    tool = executor.tool
    prompt = PROMPT + (REVIEW_SUFFIX if role == "review" else "")
    cmd = build_command(
        tool,
        executor.model,
        worktree,
        prompt,
        brief_file,
        output_file,
        effort if wants_effort(tool) else None,
        role == "review",
    )

    env = tool_env(tool)
    if env is None:
        _remove_brief(role, brief_file)
        names = ", ".join(str(name) for name in tool.get("env", []))
        return finish("failed", f"missing env: {names}")

    ignore_home = prefs.ignore_home_entries
    before = snapshot(project, worktree, ignore_home)
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=worktree,
            env=env,
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

    after = snapshot(project, worktree, ignore_home)
    writes = outside_writes(before, after)
    attempt["outside_writes"] = writes

    text = stdout or ""
    if text == "" and output_file.exists():
        text = output_file.read_text(encoding="utf-8", errors="replace")
    answer = output_text(tool, text)
    attempt["note"] = answer[-MAX_NOTE:]
    attempt.update(_metrics(tool, brief, text, answer, stderr))

    reported = parse_reported_model(tool, text)
    attempt["reported_model"] = reported
    if reported:
        attempt["model_mismatch"] = not (
            executor.model in reported or reported in executor.model
        )

    if role == "exec":
        rows, destructive = diffstat(worktree)
        attempt["diffstat"] = rows
        attempt["destructive_suspect"] = destructive
        commit_worktree(worktree, f"crew: {job_id} piece {piece} attempt {number}")
    else:
        _remove_brief(role, brief_file)

    return finish(_status(timed_out, writes, proc.returncode, stderr, text))


def _reserve_attempt(job: dict, piece: int) -> int:
    """Reserve and return the next attempt number of ``piece``.

    Called under the job lock. The number is ``1 +`` the attempts already
    recorded, whatever their role, but never below the piece's
    ``next_attempt`` counter, which is then advanced: two parallel dispatches
    of one piece get distinct numbers (and worktrees) even though neither has
    recorded its attempt yet.
    """
    for entry in job.get("pieces") or []:
        if entry.get("n") == piece:
            number = max(
                1 + len(entry.get("attempts") or []),
                int(entry.get("next_attempt") or 1),
            )
            entry["next_attempt"] = number + 1
            return number
    raise ValueError(f"job {job.get('id')!r} has no piece {piece}")


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
) -> str:
    """Status from the run's outcome: timeout and containment first."""
    if timed_out:
        return "timeout"
    if writes:
        return "outside_write"
    if returncode != 0:
        return "quota" if _quota_hit(stderr, text) else "failed"
    return "ok"


def _quota_hit(stderr: str | None, text: str) -> bool:
    """Whether stderr or the output text matches any :data:`QUOTA_PATTERNS`."""
    haystack = f"{stderr or ''}\n{text or ''}"
    return any(pattern.search(haystack) for pattern in QUOTA_PATTERNS)


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
