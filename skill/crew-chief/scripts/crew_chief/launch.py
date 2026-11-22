"""``ACC launch``: start a crew chief (``crew``: the request goes as "ACC, ...") or a plain agent (``direct``) on a
request, in an Orca terminal you can watch from the desktop or the phone when Orca runs here, else in the
background. Either way the run leaves the same files in its job folder: ``<id>.prompt.md``, ``<id>.run.sh``,
``<id>.meta.json``, ``<id>.pid``, ``<id>.log`` and ``<id>.rc``. ``ACC runs`` reads them and ``ACC compare`` sets
the two modes side by side."""

from __future__ import annotations

import json
import os
import re
import secrets
import shlex
import shutil
import statistics
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import ledger, models, terminals

MODES = ("crew", "direct")
WHERE = ("auto", "orca", "background")
AGENTS = {
    "opencode": lambda m: ["opencode", "run", "--standalone", "--auto", *(["-m", m] if m else [])],
    "claude": lambda m: ["claude", "-p", "--dangerously-skip-permissions", *(["--model", m] if m else [])],
    "codex": lambda m: ["codex", "exec", "--dangerously-bypass-approvals-and-sandbox", *(["-m", m] if m else [])],
}
# an Orca terminal does not inherit the launcher's environment: these go into the script
PASS_ENV = ("CREW_CHIEF_CARD", "CREW_CHIEF_SPEC_SHA256", "CREW_CHIEF_MACHINE", "CREW_CHIEF_HOME", "CREW_CHIEF_MAX_COST",
            "CREW_CHIEF_JOB_DIR")
# A crew chief that ends its turn before the delivery is resumed in its own session (audit 20/11: 8 of 21 runs
# ended rc 0 saying "I will wait"). Each agent: (first run, resume) flags for the run id; codex has no session id
# known in advance, so it is not resumed and only fails.
SESSION = {
    "opencode": lambda rid: (["--session", f"ses_acc-{rid}"],) * 2,  # OpenCode 2.x creates it when missing
    "claude": lambda rid: (["--session-id", str(uuid.uuid5(uuid.NAMESPACE_URL, f"acc:{rid}"))],
                           ["--resume", str(uuid.uuid5(uuid.NAMESPACE_URL, f"acc:{rid}"))]),
}
RESUMES = 3
NO_DELIVERY_RC = 5
RESUME_PROMPT = (
    "You ended your turn before the delivery, and that ends the run. Go on: wait for the lane engineers with "
    "`ACC runs <id>`, review their work, open the pull request and run `ACC summary`. If it cannot be delivered, "
    "run `ACC summary --rc 1` and say why."
)
RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")
TIMEOUT = re.compile(r"\d+[smhd]?")


def seconds(text: str) -> int:
    """``4h``/``90m``/``600``/``1d`` (the ``timeout`` format) in seconds."""
    return int(text[:-1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[text[-1]] if text[-1].isalpha() else int(text)
# A launched crew chief is a one-shot command (`opencode run`, `claude -p`): ending its turn ends the run, and lanes
# left in Orca never get merged (lab, 17/11). The crew chief starts each lane engineer with `ACC launch --engineer`
# and waits on it with `ACC runs <id>`; no end of turn before the pull request and ACC summary.
CREW_PROMPT = (
    "ACC. You run as a one-shot command: ending your turn ends the run. "
    "For each lane, start its engineer with "
    "`ACC launch --engineer --slug <slug> --lane <lane> --brief <race-engineer brief> "
    "[--job-dir <dir>] [--id <id>]` from inside the lane worktree (without --model it takes "
    "`ACC pick --kind coordinate`), then wait on it "
    "with `ACC runs <id>` in short checks until the state is finished or failed. "
    "Do not end your turn before the pull request is open and ACC summary is written. "
    "Nobody watches to approve: follow the spec rules for a launcher run. "
    "Do the request below.\n\n"
)


class LaunchError(Exception):
    pass


def runs_dir() -> Path:
    """``$CREW_CHIEF_JOB_DIR``, else ``runs`` next to the ledger."""
    folder = os.environ.get("CREW_CHIEF_JOB_DIR")
    return Path(folder) if folder else ledger.ledger_path().parent / "runs"


def _repo_name(cwd: Path) -> str:
    return models.repo_name(cwd)


def _orca_command() -> list[str] | None:
    """Orca's CLI; never the GNOME screen reader that answers to ``orca`` on a Linux desktop."""
    for name in ("CREW_CHIEF_ORCA", "ORCA_CLI_COMMAND"):
        if os.environ.get(name):
            return shlex.split(os.environ[name])
    if shutil.which("orca-ide"):
        return ["orca-ide"]
    found = shutil.which("orca")
    return [found] if found and Path(found).resolve() != Path("/usr/bin/orca") else None


def orca_call(args: list[str]) -> dict | None:
    """``orca <args> --json``; its ``result`` when it answered ok, else None."""
    cmd = _orca_command()
    if cmd is None:
        return None
    try:
        out = subprocess.run([*cmd, *args, "--json"], capture_output=True, text=True, timeout=60).stdout
        reply = json.loads(out)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    return reply.get("result") if isinstance(reply, dict) and reply.get("ok") else None


def _git_usr_bin() -> str | None:
    """Git's ``usr/bin``: where ``cat``, ``tee`` and ``timeout`` live on Windows.

    An Orca terminal inherits a PATH without them, and the run script dies on
    its first ``cat``.
    """
    if sys.platform != "win32":
        return None
    tools = ("cat", "tee", "timeout")
    for tool in ("bash", *tools):
        found = shutil.which(tool)
        folder = Path(found).parent if found else None
        if folder and all((folder / f"{name}.exe").is_file() for name in tools):
            # the MSYS form: a PATH entry with a space needs the quotes of the export below
            return "/" + folder.drive[0].lower() + folder.as_posix()[2:]
    return None


def _bash() -> str | None:
    """bash for the run script; on Windows Git's own, since ``bash`` there is often WSL's."""
    if sys.platform == "win32" and shutil.which("git"):
        git_bash = Path(shutil.which("git")).resolve().parents[1] / "bin" / "bash.exe"
        if git_bash.is_file():
            return str(git_bash)
    return shutil.which("bash")


def terminal_command(script: Path) -> str:
    """What the Orca terminal types: PowerShell on Windows needs ``&`` before a quoted program."""
    bash = _bash() or "bash"
    if sys.platform == "win32":
        return f"& '{bash}' '{script.as_posix()}'"
    return f"{shlex.quote(bash)} {shlex.quote(script.as_posix())}"


def spawn_background(script: Path) -> None:
    bash = _bash()
    if bash is None:
        raise LaunchError("background mode needs bash on PATH (Git Bash on Windows)")
    extra = ({"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
              | subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {"start_new_session": True})
    subprocess.Popen([bash, script.as_posix()], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, **extra)


#: Blocks while runs this run launched are still working (``crew-chief.py wait-children``).
WAIT_CHILDREN = " ".join(shlex.quote(x) for x in (Path(sys.executable).as_posix(), "-I",
                                                     (Path(__file__).resolve().parents[1] / "crew-chief.py").as_posix(),
                                                     "wait-children"))


def _resume_lines(run_id: str, agent: str, model: str | None, timeout: str, log: str) -> list[str]:
    """After the first run: resume a crew chief that ended rc 0 with no summary of its id, at most RESUMES times;
    still none, rc NO_DELIVERY_RC."""
    jobs = shlex.quote((ledger.ledger_path().parent / "jobs").as_posix())
    # the line ACC summary writes (jobs.write): json.dumps puts the id first
    delivered = f"grep -qsF -- {shlex.quote(json.dumps({'id': run_id})[:-1] + ',')} {jobs}/*/*.jsonl"
    flags = SESSION.get(agent)
    lines = ["rc=${PIPESTATUS[0]}"]
    if flags:
        resume = " ".join(map(shlex.quote, [*AGENTS[agent](model), *flags(run_id)[1]]))
        lines += [
            f"for n in $(seq 1 {RESUMES}); do",
            f'  [ "$rc" = 0 ] && ! {delivered} || break',
            # 22/11: a crew chief "waiting for the backend" ended 3x while its engineer still worked: rc 5, work lost
            f"  {WAIT_CHILDREN} {shlex.quote(run_id)} 2>&1 | tee -a {log}",
            f'  echo "ACC: coordenador encerrou sem entrega; retomada $n de {RESUMES}" | tee -a {log}',
            # shortcut: each resume gets the whole --timeout again; a deadline if runs ever go past it
            f"  timeout {timeout} {resume} {shlex.quote(RESUME_PROMPT)} < /dev/null 2>&1 | tee -a {log}",
            "  rc=${PIPESTATUS[0]}",
            "done",
        ]
    times = f"{RESUMES}x" if flags else f"({agent} sem retomada)"
    lines += [f'if [ "$rc" = 0 ] && ! {delivered}; then',
              f'  echo "ACC: coordenador encerrou sem entrega {times}: rc {NO_DELIVERY_RC}" | tee -a {log}',
              f"  rc={NO_DELIVERY_RC}", "fi"]
    return lines


def _script(job: Path, cwd: Path, run_id: str, agent: str, model: str | None, timeout: str,
            mode: str = "direct") -> str:
    q = lambda p: shlex.quote(p.as_posix() if isinstance(p, Path) else p)  # noqa: E731
    env = {"CREW_CHIEF_JOB_ID": run_id, "CREW_CHIEF_COORDINATOR": f"{agent}:{model or 'default'}"}
    env.update({k: os.environ[k] for k in PASS_ENV if os.environ.get(k)})
    crew = mode == "crew"
    first = SESSION[agent](run_id)[0] if crew and agent in SESSION else []
    cmd = " ".join(map(shlex.quote, [*AGENTS[agent](model), *first]))
    j = lambda ext: q(job.parent / f"{run_id}.{ext}")  # noqa: E731
    usr_bin = _git_usr_bin()
    return "\n".join([
        "#!/bin/bash",
        f"# ACC launch {run_id}: written by agent-crew-chief",
        # an Orca terminal inherits a PATH without Git's tools: cat, tee and timeout would be missing
        # (double quotes: the entry has a space and $PATH must still expand)
        *( [f'export PATH="{usr_bin}:$PATH"'] if usr_bin else [] ),
        f"cd {q(cwd)} || exit 1",
        *(f"export {k}={shlex.quote(v)}" for k, v in env.items()),
        f"echo $$ > {j('pid')}",
        # the request is read from its file, never pasted into the command line
        f'timeout {timeout} {cmd} "$(cat {j("prompt.md")})" < /dev/null 2>&1 | tee {j("log")}',
        *(_resume_lines(run_id, agent, model, timeout, j("log")) if crew else ["rc=${PIPESTATUS[0]}"]),
        f"echo $rc > {j('rc')}",
        "",
    ])


def _is_linked_worktree(cwd: Path) -> bool:
    """True when the checkout is a linked worktree: its git dir lives in the main repo's ``.git``.

    Such a worktree is already a child of the repo Orca knows; adding it as a
    project of its own would duplicate it in Orca.
    """
    def git_dir(*args: str) -> str:
        out = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--path-format=absolute", *args],
                             capture_output=True, text=True, check=True).stdout.strip()
        return os.path.normcase(os.path.normpath(out))
    try:
        return git_dir("--git-dir") != git_dir("--git-common-dir")
    except (OSError, subprocess.CalledProcessError):
        return False


def launch(brief: Path, *, mode: str = "crew", agent: str = "opencode", model: str | None = None,
           timeout: str = "4h", job_dir: Path | None = None, job_id: str | None = None, title: str | None = None,
           where: str = "auto", cwd: Path | None = None, slug: str | None = None, lane: str | None = None,
           call=None, spawn=None, now: datetime | None = None) -> dict:
    if mode == "engineer":
        if not slug or not isinstance(slug, str) or not slug.strip():
            raise LaunchError("--engineer requires --slug")
        if not lane or not isinstance(lane, str) or not lane.strip():
            raise LaunchError("--engineer requires --lane")
        if not brief.exists() or not brief.read_text(encoding="utf-8").strip():
            raise LaunchError(f"--engineer requires a non-empty --brief ({brief})")
        # shortcut: o engenheiro roda em modo direto; o título segue o padrão da lane
        title = title or terminals.terminal_name(slug, lane, None, "engineer")
        mode = "direct"
    model = ledger.clean_model(agent, model)  # ACC pick names "opencode:omni/x"; the agent wants "omni/x"
    if mode not in MODES or agent not in AGENTS or where not in WHERE:
        raise LaunchError(f"mode {MODES}, agent {tuple(AGENTS)}, where {WHERE}")
    call, spawn = call or orca_call, spawn or spawn_background  # looked up now: tests replace them
    if not TIMEOUT.fullmatch(timeout):
        raise LaunchError(f"timeout like 4h, 90m or 600: {timeout!r}")
    started = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    run_id = job_id if job_id is not None else f"{started:%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"
    if not RUN_ID.fullmatch(run_id) or ".." in run_id:
        raise LaunchError(f"run id: letters, digits, dot, _ and -, at most 80: {run_id!r}")
    cwd = (cwd or Path.cwd()).absolute()
    jobs = (job_dir or runs_dir()).absolute()
    jobs.mkdir(parents=True, exist_ok=True)
    if (jobs / f"{run_id}.meta.json").exists():
        raise LaunchError(f"run {run_id} already exists in {jobs}")
    text = brief.read_text(encoding="utf-8")
    if not text.strip():
        raise LaunchError(f"{brief} is empty")

    ready = False
    if where != "background":
        status = call(["status"]) or {}
        runtime = status.get("runtime") or {}
        ready = runtime.get("state") == "ready" and bool(runtime.get("reachable"))
        if where == "orca" and not ready:
            raise LaunchError("Orca is not running here (orca status): use --where auto or background")

    repo = _repo_name(cwd)
    script = jobs / f"{run_id}.run.sh"
    (jobs / f"{run_id}.prompt.md").write_text((CREW_PROMPT if mode == "crew" else "") + text, encoding="utf-8")
    script.write_text(_script(script, cwd, run_id, agent, model, timeout, mode), encoding="utf-8", newline="\n")
    title = title or f"{'acc' if mode == 'crew' else 'direct'} {repo} {run_id}"
    meta = {"id": run_id, "mode": mode, "agent": agent, "model": model, "repo": repo, "cwd": cwd.as_posix(),
            "title": title, "start": started.isoformat(), "where": "background"}
    parent = os.environ.get("CREW_CHIEF_JOB_ID")
    if parent and parent != run_id:  # launched from inside another run (a crew chief's engineer, an engineer's driver)
        meta["parent"] = parent

    if ready:
        # a linked worktree is a child of the repo Orca knows: adding it would duplicate the project
        if not _is_linked_worktree(cwd):
            call(["repo", "add", "--path", cwd.as_posix()])  # already added answers an error: fine
        opened = call(["terminal", "create", "--worktree", f"path:{cwd.as_posix()}", "--title", title,
                       "--command", terminal_command(script)])
        if opened is not None:
            meta["where"] = "orca"
    if meta["where"] == "background":
        if where == "orca":
            raise LaunchError("Orca did not open the terminal: nothing started")
        spawn(script)
    (jobs / f"{run_id}.meta.json").write_text(json.dumps(meta, ensure_ascii=False) + "\n", encoding="utf-8")
    return {**meta, "job": (jobs / run_id).as_posix()}


def _alive(pid: int) -> bool:
    if sys.platform == "win32":
        return True  # shortcut: the pid is bash's own (MSYS) on Windows; trust the missing .rc there
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def state(job_dir: Path, run_id: str, lines: int = 20) -> dict:
    j = lambda ext: job_dir / f"{run_id}.{ext}"  # noqa: E731
    if not j("meta.json").exists():
        raise LaunchError(f"no run {run_id} in {job_dir}")
    meta = json.loads(j("meta.json").read_text(encoding="utf-8"))
    rc = None
    if j("rc").exists():
        rc = int((j("rc").read_text().strip() or "1"))
        now = "finished" if rc == 0 else "failed"
    elif not j("pid").exists():
        now = "starting"
    else:
        try:
            now = "running" if _alive(int(j("pid").read_text().strip())) else "stopped"
        except ValueError:
            now = "starting"
    tail = j("log").read_text(encoding="utf-8", errors="replace").splitlines()[-lines:] if j("log").exists() else []
    return {**meta, "state": now, "rc": rc, "tail": tail}


def runs(job_dir: Path) -> list[dict]:
    found = sorted(job_dir.glob("*.meta.json")) if job_dir.is_dir() else []
    return [state(job_dir, p.name.removesuffix(".meta.json"), lines=0) for p in found]


def _summaries() -> dict[str, dict]:
    """Every job summary line (``ACC summary`` or a launcher), any machine, by id."""
    out = {}
    for path in (ledger.ledger_path().parent / "jobs").glob("*/*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
                out[row["id"]] = row
            except (ValueError, KeyError, TypeError):
                continue
    return out


def _job_summary(path: Path) -> dict:
    """A launcher's own summary next to the run (it keeps test repos out of the jobs/ files)."""
    try:
        got = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def compare(job_dir: Path, since: str | None = None) -> dict[str, dict]:
    summaries = _summaries()
    groups: dict[str, list[dict]] = {}
    for meta_file in (sorted(job_dir.glob("*.meta.json")) if job_dir.is_dir() else []):
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        if since and meta["start"][:10] < since:
            continue
        rc_file = job_dir / f"{meta['id']}.rc"
        rc = int(rc_file.read_text().strip() or "1") if rc_file.exists() else None
        minutes = None
        if rc_file.exists():
            start = datetime.fromisoformat(meta["start"]).timestamp()
            minutes = round((rc_file.stat().st_mtime - start) / 60, 1)
        # a launcher may run its agent under "<its id>.<suffix>": its summary keeps the bare id
        bare = meta["id"].split(".", 1)[0]
        summary = summaries.get(meta["id"]) or summaries.get(bare) or _job_summary(job_dir / f"{bare}.resumo.json")
        groups.setdefault(meta["mode"], []).append(
            {"rc": rc, "minutes": minutes, "pr": bool(summary.get("pr_url"))})
    return {mode: {
        "runs": len(rows),
        "finished": sum(r["rc"] is not None for r in rows),
        "ok": sum(r["rc"] == 0 for r in rows),
        "with_pr": sum(r["pr"] for r in rows),
        "minutes_median": statistics.median(m) if (m := [r["minutes"] for r in rows if r["minutes"] is not None])
        else None,
    } for mode, rows in groups.items()}
