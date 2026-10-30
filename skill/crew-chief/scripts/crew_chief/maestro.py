"""The maestro's coordinators: launch, run with fallback, status, resume, stop.

One coordinator works one front in its own worktree and branch
(``<project>.crew/coord-<front>-<id>``, branch ``maestro/<front>-<id>``). It is a
headless run of a configured ``tool:model`` (``[team].coordinators``, the
same tools the executors use) on a prompt that tells it to follow the
crew-chief job cycle and end in a PR. A detached runner drives it round by
round:

* a round that hits a quota or fails moves to the next ``tool:model`` of the
  list; when the list is exhausted the coordinator holds in ``quota`` or
  ``failed`` until ``resume``;
* a round that ends without a PR and without a question continues with the
  same coordinator, up to :data:`MAX_ROUNDS` rounds;
* a coordinator that needs the owner writes ``question.md`` in its folder and
  stops; ``resume --answer`` relays the answer and runs it again;
* once the job is closed (``pr_url`` or a local end mode) the same
  ``tool:model`` reviews the delivery in a clean session and a checkout of its
  own: a checklist of what was asked against what was delivered, plus the
  test results. A failed review sends the coordinator back to fix it, up to
  :data:`MAX_REVIEWS` reviews; a passed one completes the front and removes
  its worktrees (:func:`clean`).

Every round records its coordinator, duration, exit code and the tokens the
CLI reported; the tokens also land in the job's ``usage``, so the job's
economy and the scoreboard count the coordinator next to its executors.
The records live in ``<project>.crew/coordinators/<id>/``, outside the repo.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from . import config, economy, records, tools, worktree as worktrees
from ._os_compat import pid_alive
from .dispatch import _quota_hit

#: Rounds a coordinator gets before it is failed for not delivering a PR.
MAX_ROUNDS = 6
#: Reviews a front gets; one that still fails after these holds for the owner.
MAX_REVIEWS = 3
#: Used when neither ``--coordinator`` nor ``[team].coordinators`` names one.
DEFAULT_COORDINATOR = "claude:opus"
ENTRY_SCRIPT = Path(__file__).resolve().parents[1] / "crew-chief.py"
SKILL_FILE = Path(__file__).resolve().parents[2] / "SKILL.md"


class MaestroError(Exception):
    """A launch, resume or stop that cannot be done; the message says why."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _crew_dir(repo: Path) -> Path:
    return repo.parent / f"{repo.name}.crew"


def coordinators_dir(repo: Path) -> Path:
    return _crew_dir(repo) / "coordinators"


def _record_path(folder: Path) -> Path:
    return folder / "record.json"


def load(folder: Path) -> dict:
    return json.loads(_record_path(folder).read_text(encoding="utf-8"))


def _save(folder: Path, record: dict) -> None:
    record["updated_at"] = _now()
    tmp = folder / "record.json.tmp"
    tmp.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, _record_path(folder))


def _folder(repo: Path, coordinator_id: str) -> Path:
    folder = coordinators_dir(repo) / coordinator_id
    if not _record_path(folder).is_file():
        raise MaestroError(f"no coordinator {coordinator_id!r} in {coordinators_dir(repo)}")
    return folder


def _resolve(entry: str, prefs: config.Prefs) -> tuple[dict, str]:
    """The tool dict and model of one ``tool:model`` entry."""
    name, sep, model = entry.partition(":")
    tool = next((t for t in prefs.tools if t.get("name") == name.strip()), None)
    if not sep or not model.strip() or tool is None:
        known = ", ".join(sorted(t["name"] for t in prefs.tools))
        raise MaestroError(f"coordinator {entry!r} must be 'tool:model' with a known tool ({known})")
    return tool, model.strip()


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if proc.returncode != 0:
        raise MaestroError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "front"


def _job_ids(worktree: Path) -> list[str]:
    return [job["id"] for job, _, _ in records.iter_jobs(worktree) if job and job.get("id")]


def launch(
    repo: Path | str,
    request_file: Path | str,
    *,
    spec_dir: str | None = None,
    coordinators: list[str] | None = None,
    executors: str | None = None,
    base: str = "HEAD",
    spawn: bool = True,
    end_mode: str | None = None,
) -> dict:
    """Create the front's worktree and record, start its runner, return the record."""
    repo = Path(_git(Path(repo), "rev-parse", "--show-toplevel"))
    request_file = Path(request_file)
    try:
        request = request_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise MaestroError(f"cannot read request file {request_file}: {exc}") from exc
    if not request.strip():
        raise MaestroError(f"request file {request_file} is empty")
    prefs = config.load_prefs(repo)
    entries = list(coordinators or prefs.team_coordinators or [DEFAULT_COORDINATOR])
    for entry in entries:
        _resolve(entry, prefs)

    coordinator_id = secrets.token_hex(4)
    front = _slug(request_file.stem)
    # not acc/: job new keeps a feature branch, but replaces an acc/ one with a new job branch
    branch = f"maestro/{front}-{coordinator_id}"
    worktree = _crew_dir(repo) / f"coord-{front}-{coordinator_id}"
    worktree.parent.mkdir(parents=True, exist_ok=True)
    base_sha = _git(repo, "rev-parse", base)
    _git(repo, "worktree", "add", "-b", branch, str(worktree), base_sha)

    folder = coordinators_dir(repo) / coordinator_id
    folder.mkdir(parents=True)
    (folder / "request.md").write_text(request, encoding="utf-8")
    record = {
        "id": coordinator_id,
        "front": front,
        "repo": str(repo),
        "worktree": str(worktree),
        "branch": branch,
        "base": base_sha,
        "spec_dir": spec_dir,
        "executors": executors,
        "end_mode": end_mode,
        "coordinators": entries,
        "index": 0,
        "state": "running",
        "pid": None,
        "created_at": _now(),
        "finished_at": None,
        "known_jobs": _job_ids(worktree),
        "job_id": None,
        "pr_url": None,
        "pending_usage": [],
        "rounds": [],
        "reviews": [],
        "fix": None,
        "last_error": None,
    }
    _save(folder, record)
    if spawn:
        _spawn(folder, record)
    return record


def _spawn(folder: Path, record: dict) -> None:
    """Start the detached runner for ``folder`` and record its pid."""
    kwargs: dict = {}
    if os.name == "nt":
        # a hidden console, not none: without one every git or gh the runner calls opens a window
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    with open(folder / "runner.log", "a", encoding="utf-8") as log:
        proc = subprocess.Popen(
            [sys.executable, "-I", str(ENTRY_SCRIPT), "_coordinator-run", str(folder)],
            stdin=subprocess.DEVNULL, stdout=log, stderr=log, **kwargs,
        )
    record["pid"] = proc.pid
    _save(folder, record)


def _prompt(record: dict, folder: Path, first: bool) -> str:
    cc = f'python -I "{ENTRY_SCRIPT}"'
    lines = [
        "You are a crew-chief coordinator for one front of a larger goal.",
        f"Read the skill at {SKILL_FILE} and follow its job cycle; CC means {cc}.",
        f"The project is this worktree ({record['worktree']}) on branch {record['branch']}.",
        f"The request is in {folder / 'request.md'}.",
    ]
    if record.get("spec_dir"):
        lines.append(f"Its approved spec is in {record['spec_dir']}; import it with spec import.")
    if record.get("executors"):
        lines.append(f"Executor mode for this job: {record['executors']} (job new --executors).")
    lines += [
        "Dispatch the work to the configured executors and reviewers, grade every piece and",
        "close the job; close pushes the branch and opens the pull request. Never merge.",
        "Put the output of the checks you ran (build, lint, tests) in the pull request description:",
        "a reviewer checks the delivery against the request afterwards and fails it without them.",
    ]
    if record.get("end_mode"):
        lines.append(f"Close the job with close --end-mode {record['end_mode']}.")
    lines += [
        "close writes the CHANGELOG and the scoreboard after its last push: commit those files "
        "and push the job branch again.",
        f"Never ask interactively: if you need the owner's decision, write the question to "
        f"{folder / 'question.md'} and stop.",
    ]
    if record.get("fix"):
        lines.append(
            f"Your job {record.get('job_id') or ''} is closed and delivered, but a reviewer in a clean "
            f"session failed it against the request: read {record['fix']}. Fix every item it marks "
            "partial or missing and every problem it lists; run the checks it finds without results "
            "and put their output in the pull request description. Commit and push to "
            f"{record['branch']}: the open pull request follows the branch, so do not open another "
            "one and do not start a new job."
        )
    elif not first:
        job = f" (job {record['job_id']})" if record.get("job_id") else ""
        lines.append(
            f"This continues earlier work{job}: read docs/crew-chief/jobs/ in this worktree "
            "and carry on from where it stopped; do not start a new job."
        )
    answers = folder / "answers.md"
    if answers.is_file():
        lines.append(f"The owner's answers to your questions are in {answers}.")
    # one line: a Windows .cmd shim (opencode.CMD) passes only the first line of an argument
    return " ".join(lines)


def _find_job(record: dict) -> dict | None:
    worktree = Path(record["worktree"])
    if record.get("job_id"):
        job = None
        # the merged record in the repo still holds the numbers once the worktree is gone
        for where in (worktree, Path(record["repo"])):
            try:
                job = records.load_job(where, record["job_id"])
                break
            except records.RecordError:
                continue
        snapshot = coordinators_dir(Path(record["repo"])) / record["id"] / "job.json"
        if job is None and snapshot.is_file():
            # clean() keeps a copy for a PR not merged yet
            job = json.loads(snapshot.read_text(encoding="utf-8"))
        if job is None:
            return None
    elif not worktree.is_dir():
        return None
    else:
        known = set(record.get("known_jobs") or [])
        job = next(
            (j for j, _, _ in records.iter_jobs(worktree) if j and j.get("id") and j["id"] not in known), None
        )
        if job:
            record["job_id"] = job["id"]
    if job and job.get("job_branch"):
        # the branch the job pushes, so status names the PR's branch
        record["branch"] = job["job_branch"]
    return job


def _commit_usage(record: dict) -> None:
    """Commit the job record ``_book_usage`` changed and push it, when git tracks it."""
    worktree = Path(record["worktree"])
    paths = [str(records.job_path(worktree, record["job_id"], suffix)) for suffix in (".json", ".md")]
    try:
        tracked = _git(worktree, "ls-files", "--", *paths).splitlines()
        if not tracked:
            return
        _git(worktree, "commit", "-m", "Book coordinator usage", "--", *tracked)
        _git(worktree, "rev-parse", "--abbrev-ref", "@{u}")
        _git(worktree, "push")
    except MaestroError:
        pass  # no upstream or a failed push: the tokens stay committed or in the worktree's record


def _book_usage(record: dict) -> None:
    """Move the rounds' pending token counts into the job's ``usage``."""
    if not record.get("job_id") or not record.get("pending_usage"):
        return
    pending = record["pending_usage"]

    def apply(job: dict) -> dict:
        for item in pending:
            records.add_usage(job, "coordinator", item["tokens_in"], item["tokens_out"], False, item["note"])
        return job

    records.update_job(Path(record["worktree"]), record["job_id"], apply)
    record["pending_usage"] = []
    _commit_usage(record)


def _kill_tree(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True)
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        pass


def _run_round(
    record: dict, folder: Path, prefs: config.Prefs, *, prompt: str | None = None,
    cwd: Path | None = None, label: str = "round",
) -> dict:
    """Run one round of the current coordinator and return its round entry.

    ``prompt`` and ``cwd`` default to the coordinator's own; a review passes
    its prompt and checkout, and ``label`` names its logs.
    """
    # numbered across resumes, so earlier round logs are never overwritten
    done = record.get("history", []) + record["rounds"] if label == "round" else record.get("reviews", [])
    number = len(done) + 1
    entry = record["coordinators"][record["index"]]
    tool, model = _resolve(entry, prefs)
    worktree = cwd or Path(record["worktree"])
    out_file = folder / f"{label}-{number}.out"
    if prompt is None:
        _find_job(record)
        # a fallback after a round that made no job must still start one
        prompt = _prompt(record, folder, first=not record.get("job_id"))
    cmd = tools.build_command(
        tool, model, worktree, prompt, folder / "request.md", out_file, None, False
    )
    if tool["kind"] == "codex":
        # the coordinator pushes and opens the PR: it needs the network the sandbox blocks
        at = cmd.index("-s")
        cmd[at:at + 2] = ["--dangerously-bypass-approvals-and-sandbox"]
    elif tool["kind"] == "claude-code":
        # the owner's own MCP servers stay out of the headless coordinator
        cmd.append("--strict-mcp-config")
    started = datetime.now(timezone.utc)
    stdout_path, stderr_path = folder / f"{label}-{number}.log", folder / f"{label}-{number}.err"
    exit_code, timed_out = None, False
    env = tools.tool_env(tool)
    if env is None:
        stderr_text = f"missing environment for {tool['name']}: {', '.join(tools.missing_env(tool))}"
        stdout = ""
    else:
        # the detached runner has no console: without this each coordinator opens its own window
        kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
        # pick puts this CLI's own executors last; it expects the tool name
        env["CREW_CHIEF_COORDINATOR"] = tool["name"]
        with open(stdout_path, "w", encoding="utf-8") as out, open(stderr_path, "w", encoding="utf-8") as err:
            try:
                proc = subprocess.Popen(
                    cmd, cwd=worktree, stdin=subprocess.DEVNULL, stdout=out, stderr=err, env=env, **kwargs
                )
            except OSError as exc:
                # a missing CLI fails the round, so the next coordinator gets its turn
                err.write(f"cannot start {cmd[0]}: {exc}\n")
            else:
                record["child_pid"] = proc.pid
                _save(folder, record)
                try:
                    exit_code = proc.wait(timeout=prefs.coordinator_timeout_s)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    _kill_tree(proc.pid)
                    proc.wait()
        stdout = stdout_path.read_text(encoding="utf-8", errors="replace")
        stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace")
    ended = datetime.now(timezone.utc)
    metrics = tools.parse_metrics(tool, stdout, stderr_text)
    text = tools.output_text(tool, stdout) + "\n" + tools.provider_error(tool, stdout)
    if out_file.is_file():
        text += "\n" + out_file.read_text(encoding="utf-8", errors="replace")
    if exit_code == 0 and not timed_out:
        outcome = "ok"
    elif not timed_out and _quota_hit(stderr_text, text):
        outcome = "quota"
    else:
        outcome = "timeout" if timed_out else "failed"
    round_entry = {
        label: number,
        "coordinator": entry,
        "started_at": started.isoformat(timespec="seconds"),
        "seconds": round((ended - started).total_seconds()),
        "exit_code": exit_code,
        "outcome": outcome,
        "tokens_in": metrics.get("tokens_in"),
        "tokens_out": metrics.get("tokens_out"),
        "tokens_total": metrics.get("tokens_total"),
        "cost_usd": metrics.get("cost_usd"),
        "steps": metrics.get("steps"),
        "error": "" if outcome == "ok" else (stderr_text.strip() or text.strip())[-500:],
    }
    tokens_in, tokens_out = metrics.get("tokens_in"), metrics.get("tokens_out")
    if tokens_in is None and tokens_out is None and metrics.get("tokens_total") is not None:
        # codex reports only a total: book it as input so the sum stays right
        tokens_in = metrics["tokens_total"]
    if tokens_in is not None or tokens_out is not None:
        record["pending_usage"].append({
            "tokens_in": tokens_in or 0,
            "tokens_out": tokens_out or 0,
            "note": f"coordinator {entry} {label} {number}",
        })
    return round_entry


def _delivered(job: dict | None) -> bool:
    """The job is closed: a pull request, or a local end mode without one."""
    states = {job.get("state"), job.get("status")} if job else set()
    return bool(job and (job.get("pr_url") or states & {"closed", "delivered"}))


def _review_prompt(record: dict, folder: Path, review_file: Path, base: str) -> str:
    lines = [
        "You are an independent reviewer in a clean session. Another agent delivered one front of",
        f"a larger goal on branch {record['branch']}; this checkout is its head. Trust nothing it",
        "wrote (pull request text, job records, CHANGELOG): check the code itself. Do not commit,",
        "push, merge or comment on GitHub.",
        f"The request is in {folder / 'request.md'}; the change is git diff {base}...HEAD.",
    ]
    if record.get("pr_url"):
        lines.append(f"Its pull request is {record['pr_url']}: read its description with gh pr view.")
    lines += [
        "Run the checks the request or the project names (install, build, type check, lint, tests)",
        "and note each command with its result; a check that cannot run here gets the reason.",
        f"Write the review to {review_file} in markdown: a checklist with one line per item the",
        "request asks for, each delivered, partial or missing with file:line proof; a Tests section",
        "with each command and its result, and whether the pull request shows the test results of",
        "each change; a Problems section with bugs, risks and broken rules. Fail the review when an",
        "item is partial or missing, when test results are missing from the pull request or",
        "failing, or when there is a serious bug. End the file with the line VERDICT: PASS or",
        "VERDICT: FAIL, then the line SCORE: <0-10>.",
    ]
    return " ".join(lines)


def _review(record: dict, folder: Path, prefs: config.Prefs, job: dict) -> dict:
    """Review the delivered front with the current ``tool:model`` and return the entry."""
    repo = Path(record["repo"])
    number = len(record.get("review_history", [])) + len(record["reviews"]) + 1
    review_file = folder / f"review-{number}.md"
    head = _git(Path(record["worktree"]), "rev-parse", "HEAD")
    # a checkout of its own: the reviewer installs and tests without touching the coordinator's
    checkout = _crew_dir(repo) / f"review-{record['front']}-{record['id']}"
    if checkout.exists():
        worktrees.remove_worktree(repo, checkout)
    _git(repo, "worktree", "add", "--detach", str(checkout), head)
    base = record.get("base") or job.get("base_branch") or "main"
    try:
        entry = _run_round(
            record, folder, prefs, prompt=_review_prompt(record, folder, review_file, base),
            cwd=checkout, label="review",
        )
    finally:
        try:
            worktrees.remove_worktree(repo, checkout)
        except worktrees.WorktreeRemovalError:
            pass  # clean() tries again
    text = review_file.read_text(encoding="utf-8", errors="replace") if review_file.is_file() else ""
    verdicts = re.findall(r"^[\W_]*VERDICT:[\W_]*(PASS|FAIL)", text, re.M | re.I)
    scores = re.findall(r"^[\W_]*SCORE:[\W_]*(\d+(?:[.,]\d+)?)", text, re.M | re.I)
    entry.update(
        head=head,
        file=str(review_file),
        verdict=verdicts[-1].lower() if verdicts else None,
        score=float(scores[-1].replace(",", ".")) if scores else None,
    )
    if entry["outcome"] == "ok" and not entry["verdict"]:
        entry.update(outcome="failed", error=f"the reviewer wrote no verdict to {review_file}")
    return entry


def _comment_review(record: dict, entry: dict) -> None:
    """Post the review on the pull request, so the owner reads it there."""
    if not record.get("pr_url") or not shutil.which("gh"):
        return
    try:
        subprocess.run(
            ["gh", "pr", "comment", record["pr_url"], "--body-file", entry["file"]],
            cwd=record["repo"], stdin=subprocess.DEVNULL, capture_output=True, timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass  # the review stays in the coordinator's folder


def run(folder: Path | str) -> dict:
    """Drive the coordinator in ``folder`` until it completes, asks or holds."""
    folder = Path(folder)
    record = load(folder)
    record.setdefault("reviews", [])
    prefs = config.load_prefs(Path(record["worktree"]))
    while True:
        job = _find_job(record)
        # a delivered job is reviewed, unless the last review sent it back to be fixed
        reviewing = _delivered(job) and not record.get("fix")
        if reviewing:
            record["pr_url"] = job.get("pr_url")
            round_entry = _review(record, folder, prefs, job)
            record["reviews"].append(round_entry)
        else:
            round_entry = _run_round(record, folder, prefs)
            record["rounds"].append(round_entry)
            # a fix that asks the owner keeps its review, so the answer resumes the fix
            if round_entry["outcome"] == "ok" and not (folder / "question.md").is_file():
                record["fix"] = None
        record.pop("child_pid", None)
        job = _find_job(record)
        _book_usage(record)
        stored = load(folder)
        if stored["state"] == "stopped":
            # stop() ran during the round: keep its state, no next coordinator
            record.update(state="stopped", finished_at=stored.get("finished_at"))
            break
        job_states = {job.get("state"), job.get("status")} if job else set()
        if "blocked" in job_states:
            result = job.get("result") or {}
            record.update(
                state="failed",
                last_error=f"job {job['id']} is blocked: {(result.get('blocked_error') or '').strip()} "
                f"(failed command: {result.get('failed_command') or ''})",
            )
            break
        if reviewing and round_entry["outcome"] == "ok":
            _comment_review(record, round_entry)
            if round_entry["verdict"] == "pass":
                record.update(state="completed", last_error=None, finished_at=_now())
                _save(folder, record)
                clean(record)
                break
            if sum(1 for r in record["reviews"] if r.get("verdict") == "fail") >= MAX_REVIEWS:
                record.update(
                    state="failed",
                    last_error=f"the review still fails after {MAX_REVIEWS} reviews: {round_entry['file']}",
                )
                break
            record["fix"] = round_entry["file"]
            _save(folder, record)
            continue
        if (folder / "question.md").is_file():
            record["state"] = "question"
            break
        if round_entry["outcome"] != "ok":
            record["last_error"] = round_entry["error"]
            if record["index"] + 1 < len(record["coordinators"]):
                record["index"] += 1
            else:
                record["state"] = "quota" if round_entry["outcome"] == "quota" else "failed"
                break
        # a job delivered on the last round still gets its review; fixes are bounded by MAX_REVIEWS
        if len(record["rounds"]) >= MAX_ROUNDS and not _delivered(job):
            record.update(state="failed", last_error=f"no pull request after {MAX_ROUNDS} rounds")
            break
        _save(folder, record)
    # held states stop the clock too, so elapsed time is the work, not the wait
    record.update(pid=None, finished_at=record.get("finished_at") or _now())
    _save(folder, record)
    return record


def resume(repo: Path | str, coordinator_id: str, answer: str | None = None, spawn: bool = True) -> dict:
    """Run a held, asking or failed coordinator again, from the top of its list."""
    folder = _folder(Path(repo), coordinator_id)
    record = load(folder)
    if record["state"] == "running" and record.get("pid") and pid_alive(record["pid"]):
        raise MaestroError(f"coordinator {coordinator_id} is still running")
    if record["state"] == "completed":
        raise MaestroError(f"coordinator {coordinator_id} already delivered {record['pr_url'] or 'its job'}")
    question = folder / "question.md"
    if answer:
        with open(folder / "answers.md", "a", encoding="utf-8") as fh:
            asked = question.read_text(encoding="utf-8").strip() if question.is_file() else ""
            fh.write(f"## {_now()}\n\nQuestion: {asked or '(none)'}\n\nAnswer: {answer}\n\n")
    question.unlink(missing_ok=True)
    record.update(state="running", index=0, last_error=None, finished_at=None)
    # a resumed front gets a fresh round budget
    record["history"] = record.get("history", []) + record["rounds"]
    record["rounds"] = []
    record["review_history"] = record.get("review_history", []) + record.get("reviews", [])
    record["reviews"] = []
    _save(folder, record)
    if spawn:
        _spawn(folder, record)
    return record


def stop(repo: Path | str, coordinator_id: str) -> dict:
    """Kill the runner and its coordinator; the worktree and branch stay."""
    folder = _folder(Path(repo), coordinator_id)
    record = load(folder)
    if record["state"] == "completed":
        return record
    # the runner first, so it cannot start the next coordinator once the child dies
    for key in ("pid", "child_pid"):
        pid = record.get(key)
        if pid and pid_alive(pid):
            _kill_tree(pid)
    record.update(state="stopped", pid=None, finished_at=_now())
    record.pop("child_pid", None)
    _save(folder, record)
    return record


def _registered_worktrees(repo: Path) -> list[Path]:
    listing = _git(repo, "worktree", "list", "--porcelain")
    return [Path(line[len("worktree "):]).resolve() for line in listing.splitlines() if line.startswith("worktree ")]


def clean(record: dict) -> dict:
    """Remove a front's worktrees, its attempts' checkouts and their branches.

    The coordinator's folder stays, with a copy of the job record, so ``status``
    keeps the front's numbers; so does the front's own branch, which holds the
    pull request's commits.
    """
    repo = Path(record["repo"])
    folder = coordinators_dir(repo) / record["id"]
    worktree = Path(record["worktree"]).resolve()
    job = _find_job(dict(record))
    if job:
        (folder / "job.json").write_text(json.dumps(job, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    # attempts live in <worktree>.crew, a review checkout beside the worktree
    attempts = worktree.parent / f"{worktree.name}.crew"
    review = worktree.parent / f"review-{record['front']}-{record['id']}"
    targets = [path for path in _registered_worktrees(repo) if path in (worktree, review) or attempts in path.parents]
    removed, errors = [], []
    for path in targets + [p for p in (attempts, review) if p.exists() and p not in targets]:
        try:
            if path in targets:
                worktrees.remove_worktree(repo, path)
            else:
                worktrees._remove_tree(path)
            removed.append(str(path))
        except (worktrees.WorktreeRemovalError, OSError) as exc:
            errors.append(f"{path}: {exc}")
    _git(repo, "worktree", "prune")
    # the branches the job's own attempts made: a prefix match would catch job "x-2" when cleaning "x"
    branches = {
        attempt.get("branch")
        for piece in (job or {}).get("pieces") or []
        for attempt in piece.get("attempts") or []
        if str(attempt.get("branch") or "").startswith("crew/")
    }
    for branch in sorted(branches):
        if not _git(repo, "branch", "--list", branch):
            continue
        try:
            _git(repo, "branch", "-D", branch)
            removed.append(branch)
        except MaestroError as exc:
            errors.append(str(exc))
    # a front with leftovers stays on the list of the next clean
    record.update(cleaned_at=None if errors else _now(), clean_errors=errors)
    _save(folder, record)
    return {"id": record["id"], "removed": removed, "errors": errors}


def clean_fronts(repo: Path | str, coordinator_ids: list[str] | None = None) -> list[dict]:
    """Clean the named fronts, or every completed front not cleaned yet."""
    repo = Path(_git(Path(repo), "rev-parse", "--show-toplevel"))
    if coordinator_ids:
        found = [load(_folder(repo, coordinator_id)) for coordinator_id in coordinator_ids]
    else:
        root = coordinators_dir(repo)
        found = [load(p.parent) for p in root.glob("*/record.json")] if root.is_dir() else []
        found = [r for r in found if r["state"] == "completed" and not r.get("cleaned_at")]
    for record in found:
        if record["state"] == "running" and record.get("pid") and pid_alive(record["pid"]):
            raise MaestroError(f"coordinator {record['id']} is still running: stop it first")
    return [clean(record) for record in found]


def _seconds(start: str, end: str | None) -> int:
    begin = datetime.fromisoformat(start)
    finish = datetime.fromisoformat(end) if end else datetime.now(timezone.utc)
    return max(0, round((finish - begin).total_seconds()))


def summary(record: dict) -> dict:
    """One coordinator with its rounds, time, tokens and its job's economy."""
    rounds = record.get("history", []) + record["rounds"]
    reviews = record.get("review_history", []) + record.get("reviews", [])
    state = record["state"]
    if state == "running" and record.get("pid") and not pid_alive(record["pid"]):
        state = "failed"
        record["last_error"] = record.get("last_error") or "the runner exited unexpectedly"
    found = dict(record)
    job = _find_job(found)
    job_economy = None
    if job:
        try:
            prefs = config.load_prefs(Path(record["worktree"]))
        except (config.PrefsError, OSError):
            prefs = None  # the figures still show; only the budget alerts need prefs
        job_economy = economy.job_economy(job, prefs)
    return {
        "id": record["id"],
        "front": record["front"],
        "state": state,
        "coordinator": record["coordinators"][record["index"]],
        "coordinators": record["coordinators"],
        "switches": sum(1 for a, b in zip(rounds, rounds[1:]) if a["coordinator"] != b["coordinator"]),
        "rounds": len(rounds),
        "seconds": _seconds(record["created_at"], record.get("finished_at")),
        "coordinator_tokens": sum(r.get("tokens_total") or 0 for r in rounds + reviews),
        "coordinator_cost_usd": round(sum(r.get("cost_usd") or 0 for r in rounds + reviews), 4),
        "reviews": [r.get("verdict") or r["outcome"] for r in reviews],
        "review_score": next((r["score"] for r in reversed(reviews) if r.get("score") is not None), None),
        "review_file": next((r["file"] for r in reversed(reviews) if r.get("file")), None),
        "branch": found["branch"],
        "worktree": record["worktree"],
        "job_id": record.get("job_id") or (job or {}).get("id"),
        "pr_url": record.get("pr_url"),
        "question": _question(record),
        "last_error": record.get("last_error"),
        "economy": asdict(job_economy) if job_economy else None,
        "per_round": rounds,
    }


def _question(record: dict) -> str | None:
    path = coordinators_dir(Path(record["repo"])) / record["id"] / "question.md"
    return path.read_text(encoding="utf-8").strip() if path.is_file() else None


def status(repo: Path | str) -> list[dict]:
    """Summaries of every coordinator launched from ``repo``, oldest first."""
    root = coordinators_dir(Path(_git(Path(repo), "rev-parse", "--show-toplevel")))
    if not root.is_dir():
        return []
    found = [load(p.parent) for p in root.glob("*/record.json")]
    return [summary(r) for r in sorted(found, key=lambda r: r["created_at"])]


def _minutes(seconds: int) -> str:
    return f"{seconds // 60}m{seconds % 60:02d}s"


def status_lines(summaries: list[dict]) -> list[str]:
    """The plain status the maestro reads (``--json`` prints the dicts)."""
    if not summaries:
        return ["no coordinators launched from this project"]
    lines = []
    for s in summaries:
        lines.append(
            f"{s['id']}  {s['front']}  {s['state']}  coordinator {s['coordinator']}  "
            f"rounds {s['rounds']} (switches {s['switches']})  {_minutes(s['seconds'])}  "
            f"coordinator tokens {s['coordinator_tokens']}"
        )
        if s["economy"]:
            lines.append(f"    job {s['job_id']}: {economy.summary_line(economy.Economy(**s['economy']))}")
        if s.get("reviews"):
            score = "" if s["review_score"] is None else f", score {s['review_score']:g}/10"
            lines.append(f"    review: {' -> '.join(s['reviews'])}{score} ({s['review_file']})")
        if s["question"]:
            lines.append(f"    question: {s['question']}")
        if s["last_error"] and s["state"] != "completed":
            lines.append(f"    last error: {' '.join(s['last_error'].split())[:300]}")
        if s["pr_url"]:
            lines.append(f"    PR: {s['pr_url']}")
    return lines
