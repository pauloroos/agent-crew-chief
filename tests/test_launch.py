"""ACC launch / runs / compare: a run starts in an Orca terminal when Orca runs, else in the background, and
leaves the same files either way. No real orca, agent or bash runs here: ``call`` and ``spawn`` are fakes."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from crew_chief import launch

NOW = datetime(2026, 11, 17, 20, 0, 0, tzinfo=timezone.utc)


class FakeOrca:
    """Answers ``orca`` calls like Orca 1.4.256; records them."""

    def __init__(self, ready=True, opens=True, add_fails=False):
        self.ready, self.opens, self.add_fails, self.calls = ready, opens, add_fails, []

    def __call__(self, args):
        self.calls.append(args)
        if args[:1] == ["status"]:
            return {"runtime": {"state": "ready" if self.ready else "starting", "reachable": self.ready}}
        if args[:2] == ["repo", "add"]:
            return None if self.add_fails else {"repo": {"id": "r1"}}
        if args[:2] == ["terminal", "create"]:
            return {"terminal": {"handle": "t1"}} if self.opens else None
        raise AssertionError(args)


def _brief(tmp_path: Path, text: str = "Fix the greeting: say 'olá' and $HOME stays literal.") -> Path:
    brief = tmp_path / "pedido.md"
    brief.write_text(text, encoding="utf-8")
    return brief


def _go(tmp_path, orca, spawned, **kw):
    opts = dict(mode="crew", agent="opencode", model="omni/pensar", timeout="4h", job_dir=tmp_path / "jobs",
                job_id=None, title=None, where="auto", cwd=tmp_path, call=orca, spawn=spawned.append, now=NOW)
    opts.update(kw)
    return launch.launch(_brief(tmp_path), **opts)


def test_orca_running_opens_a_titled_terminal_running_the_script(tmp_path):
    orca, spawned = FakeOrca(), []
    run = _go(tmp_path, orca, spawned, job_id="20261117-200000-abc123")
    assert run["where"] == "orca" and spawned == []
    create = orca.calls[-1]
    assert create[:2] == ["terminal", "create"]
    assert create[create.index("--worktree") + 1] == f"path:{tmp_path.as_posix()}"
    assert create[create.index("--title") + 1] == f"acc {tmp_path.name} 20261117-200000-abc123"
    script = tmp_path / "jobs" / "20261117-200000-abc123.run.sh"
    assert create[create.index("--command") + 1] == launch.terminal_command(script)
    assert script.as_posix() in launch.terminal_command(script)


def test_repo_already_in_orca_does_not_stop_the_launch(tmp_path):
    run = _go(tmp_path, FakeOrca(add_fails=True), [])
    assert run["where"] == "orca"


def test_no_orca_runs_in_the_background(tmp_path):
    spawned = []
    run = _go(tmp_path, FakeOrca(ready=False), spawned)
    assert run["where"] == "background"
    assert spawned == [tmp_path / "jobs" / f"{run['id']}.run.sh"]


def test_orca_refusing_the_terminal_falls_back_to_the_background(tmp_path):
    spawned = []
    run = _go(tmp_path, FakeOrca(opens=False), spawned)
    assert run["where"] == "background" and len(spawned) == 1


def test_where_orca_without_orca_is_an_error(tmp_path):
    with pytest.raises(launch.LaunchError, match="Orca"):
        _go(tmp_path, FakeOrca(ready=False), [], where="orca")


def test_crew_mode_asks_for_acc_and_direct_sends_the_request_as_is(tmp_path):
    crew = _go(tmp_path, FakeOrca(), [], job_id="a-1")
    direct = _go(tmp_path, FakeOrca(), [], job_id="a-2", mode="direct")
    jobs = tmp_path / "jobs"
    crew_prompt = (jobs / "a-1.prompt.md").read_text(encoding="utf-8")
    # a one-shot crew chief must not leave lanes behind: background mode and no end of turn before the PR
    assert crew_prompt.startswith("ACC, sem Orca") and "until the pull request is open" in crew_prompt
    assert (jobs / "a-2.prompt.md").read_text(encoding="utf-8") == _brief(tmp_path).read_text(encoding="utf-8")
    assert json.loads((jobs / "a-2.meta.json").read_text(encoding="utf-8"))["mode"] == "direct"
    assert crew["id"] == "a-1" and direct["id"] == "a-2"


def test_script_never_inlines_the_request_and_keeps_pid_log_rc(tmp_path):
    _go(tmp_path, FakeOrca(), [], job_id="j1")
    script = (tmp_path / "jobs" / "j1.run.sh").read_text(encoding="utf-8")
    assert "olá" not in script and "$HOME stays" not in script
    for piece in ('echo $$ > ', "j1.pid", "timeout 4h opencode run --standalone --auto -m omni/pensar",
                  '"$(cat ', "j1.prompt.md", "< /dev/null", "| tee ", "j1.log", "${PIPESTATUS[0]}", "j1.rc",
                  "export CREW_CHIEF_JOB_ID=j1", "export CREW_CHIEF_COORDINATOR=opencode:omni/pensar"):
        assert piece in script, piece


def test_launcher_env_reaches_the_terminal(tmp_path, monkeypatch):
    monkeypatch.setenv("CREW_CHIEF_CARD", "COMP-12")
    _go(tmp_path, FakeOrca(), [], job_id="j2")
    assert "export CREW_CHIEF_CARD=COMP-12" in (tmp_path / "jobs" / "j2.run.sh").read_text(encoding="utf-8")


@pytest.mark.parametrize("agent,piece", [("claude", "claude -p --dangerously-skip-permissions --model m1"),
                                         ("codex", "codex exec --dangerously-bypass-approvals-and-sandbox -m m1")])
def test_other_agents(tmp_path, agent, piece):
    _go(tmp_path, FakeOrca(), [], job_id="j3", agent=agent, model="m1")
    assert piece in (tmp_path / "jobs" / "j3.run.sh").read_text(encoding="utf-8")


@pytest.mark.parametrize("bad", ["../x", "a b", "", "x;rm"])
def test_bad_id_is_refused(tmp_path, bad):
    with pytest.raises(launch.LaunchError):
        _go(tmp_path, FakeOrca(), [], job_id=bad)


def test_same_id_twice_is_refused(tmp_path):
    _go(tmp_path, FakeOrca(), [], job_id="j4")
    with pytest.raises(launch.LaunchError, match="already"):
        _go(tmp_path, FakeOrca(), [], job_id="j4")


def test_default_id_is_a_launcher_id(tmp_path):
    run = _go(tmp_path, FakeOrca(), [])
    assert run["id"].startswith("20261117-200000-") and len(run["id"]) == 22


def test_state_follows_the_files(tmp_path):
    jobs = tmp_path / "jobs"
    _go(tmp_path, FakeOrca(), [], job_id="s1")
    assert launch.state(jobs, "s1")["state"] == "starting"
    (jobs / "s1.pid").write_text(f"{os.getpid()}\n")
    (jobs / "s1.log").write_text("line 1\nline 2\n")
    got = launch.state(jobs, "s1")
    assert got["state"] == "running" and got["tail"][-1] == "line 2"
    (jobs / "s1.rc").write_text("0\n")
    assert launch.state(jobs, "s1")["state"] == "finished"
    (jobs / "s1.rc").write_text("124\n")
    assert launch.state(jobs, "s1") ["state"] == "failed"


def _done(jobs: Path, run_id: str, mode: str, rc: int, minutes: float):
    meta = {"id": run_id, "mode": mode, "agent": "opencode", "model": "m", "repo": "r", "where": "orca",
            "start": "2026-11-17T20:00:00+00:00"}
    jobs.mkdir(parents=True, exist_ok=True)
    (jobs / f"{run_id}.meta.json").write_text(json.dumps(meta))
    rc_file = jobs / f"{run_id}.rc"
    rc_file.write_text(f"{rc}\n")
    end = datetime(2026, 11, 17, 20, 0, tzinfo=timezone.utc).timestamp() + minutes * 60
    os.utime(rc_file, (end, end))


def test_compare_counts_each_mode(tmp_path, ledger_home):
    jobs = tmp_path / "jobs"
    _done(jobs, "20261117-200000-aaaaaa", "crew", 0, 30)
    _done(jobs, "20261117-200000-bbbbbb", "crew", 1, 50)
    _done(jobs, "20261117-200000-cccccc.opencode", "direct", 0, 10)
    # a summary line (ACC summary or a launcher) carries the PR; the launcher may add a suffix to the run id
    summary = ledger_home / "jobs" / "orca" / "2026-11.jsonl"
    summary.parent.mkdir(parents=True)
    summary.write_text(json.dumps({"id": "20261117-200000-cccccc", "pr_url": "https://x/pull/1"}) + "\n"
                       + json.dumps({"id": "20261117-200000-aaaaaa", "pr_url": "https://x/pull/2"}) + "\n")
    got = launch.compare(jobs)
    assert got["crew"] == {"runs": 2, "finished": 2, "ok": 1, "with_pr": 1, "minutes_median": 40.0}
    assert got["direct"] == {"runs": 1, "finished": 1, "ok": 1, "with_pr": 1, "minutes_median": 10.0}


def test_compare_since_skips_older_runs(tmp_path):
    jobs = tmp_path / "jobs"
    _done(jobs, "x1", "crew", 0, 5)
    assert launch.compare(jobs, since="2026-11-18") == {}


def test_cli_runs_and_compare(tmp_path, capsys):
    from crew_chief.cli import main

    jobs = tmp_path / "jobs"
    _done(jobs, "r1", "crew", 0, 12)
    assert main(["runs", "--job-dir", str(jobs)]) == 0
    assert "| r1 | finished | crew | orca |" in capsys.readouterr().out
    assert main(["runs", "r1", "--job-dir", str(jobs)]) == 0
    assert capsys.readouterr().out.startswith("r1: finished (rc 0)")
    assert main(["compare", "--job-dir", str(jobs)]) == 0
    assert "| crew | 1 | 1 | 1 | 0 | 12.0 |" in capsys.readouterr().out
    assert main(["runs", "nope", "--job-dir", str(jobs)]) == 2


def test_cli_launch_needs_orca_when_asked(tmp_path, capsys, monkeypatch):
    from crew_chief.cli import main

    monkeypatch.setattr(launch, "orca_call", lambda args: None)
    brief = _brief(tmp_path)
    assert main(["launch", "--brief", str(brief), "--where", "orca", "--job-dir", str(tmp_path / "j")]) == 2
    assert "Orca is not running" in capsys.readouterr().err
