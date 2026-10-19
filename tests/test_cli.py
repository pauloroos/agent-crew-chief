"""Tests for the ``crew-chief`` CLI.

The full cycle runs the real commands against a throwaway git repo with a
``generic`` stub tool declared in its ``docs/crew-chief/preferences.toml``.
Discovery is kept hermetic: ``shutil.which`` is patched so that only the stub
bins are seen on PATH, which means no real executor CLI has to be installed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from crew_chief import records
from crew_chief.cli import main
from crew_chief.worktree import commit_worktree, create_worktree

#: A stub executor: writes ``out.py`` in its cwd (the worktree) and answers.
_STUB = (
    "from pathlib import Path\n"
    "Path('out.py').write_text('x = 1\\n', encoding='utf-8')\n"
    "print('done')\n"
)

_STUB_PREFS = """
[[tools]]
name = "stub"
kind = "generic"
bin = "stub"
models = ["m"]
run_cmd = ["stub", "{prompt}"]

[[models]]
match = "stub:*"
cost = "free"
"""

_ESCALATION_PREFS = """
strong_order = ["strong"]

[[tools]]
name = "freebie"
kind = "generic"
bin = "freebie"
models = ["m"]
run_cmd = ["freebie", "{prompt}"]

[[tools]]
name = "strong"
kind = "generic"
bin = "strong"
models = ["s"]
run_cmd = ["strong", "{prompt}"]

[[models]]
match = "freebie:*"
cost = "free"

[[models]]
match = "strong:*"
cost = "paid"
"""


_GLOBAL_PREFS = """
[[tools]]
name = "gstub"
kind = "generic"
bin = "gstub"
models = ["m"]
run_cmd = ["gstub", "{prompt}"]

[[models]]
match = "gstub:*"
cost = "free"
"""


def _write_prefs(repo: Path, text: str) -> None:
    path = repo / "docs" / "crew-chief" / "preferences.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _only_bins(monkeypatch, names: set[str]) -> None:
    """Restrict ``shutil.which`` to ``names`` so only those CLIs are discovered."""
    real_which = shutil.which

    def which(cmd, *args, **kwargs):
        return real_which(cmd, *args, **kwargs) if cmd in names else None

    monkeypatch.setattr(shutil, "which", which)


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def _stored_job(repo: Path, job_id: str) -> dict:
    path = records.jobs_dir(repo) / f"{job_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _git(repo: Path, *args: str) -> str:
    """Run ``git <args>`` in ``repo`` and return its stdout."""
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout


# --- full cycle ------------------------------------------------------------


def test_full_cycle_with_generic_stub(git_repo, fake_bin, monkeypatch, capsys):
    repo = git_repo
    _write_prefs(repo, _STUB_PREFS)
    fake_bin("stub", _STUB)
    _only_bins(monkeypatch, {"stub"})
    request = _write(repo / "request.md", "Please create out.py.\n")
    brief = _write(repo / "brief.md", "Create out.py.\n")

    assert main(["job", "new", "--project", str(repo), "--title", "Add out.py",
                 "--request-file", str(request)]) == 0
    job_id = capsys.readouterr().out.strip()
    assert job_id

    assert main(["piece", "add", "--project", str(repo), "--job", job_id,
                 "--title", "Write out.py", "--type", "code", "--risk", "low"]) == 0
    assert capsys.readouterr().out.strip() == "1"

    assert main(["pick", "--project", str(repo), "--job", job_id,
                 "--piece", "1"]) == 0
    assert capsys.readouterr().out.splitlines()[0].strip() == "stub:m"

    assert main(["dispatch", "--project", str(repo), "--job", job_id,
                 "--piece", "1", "--executor", "stub:m",
                 "--brief-file", str(brief)]) == 0
    assert "status: ok" in capsys.readouterr().out

    assert main(["grade", "--project", str(repo), "--job", job_id,
                 "--piece", "1", "--executor", "stub:m", "--corrections", "0",
                 "--done", "3", "--missing", "0", "--extra", "0"]) == 0
    capsys.readouterr()

    assert main(["accept", "--project", str(repo), "--job", job_id,
                 "--piece", "1"]) == 0
    capsys.readouterr()

    assert main(["close", "--project", str(repo), "--job", job_id]) == 0
    closed = capsys.readouterr().out
    assert "Record: docs/crew-chief/jobs/" in closed
    assert "---" in closed

    scoreboard = (repo / "docs" / "crew-chief" / "scoreboard.md").read_text(
        encoding="utf-8"
    )
    assert "| stub:m | 100 |" in scoreboard
    assert (repo / "out.py").is_file()


# --- global preferences ----------------------------------------------------


def test_discover_uses_the_global_preferences_file(
    git_repo, tmp_path, fake_bin, monkeypatch, capsys
):
    repo = git_repo
    global_file = tmp_path / "global-preferences.toml"
    global_file.write_text(_GLOBAL_PREFS, encoding="utf-8")
    monkeypatch.setenv("AGENT_CREW_CHIEF_GLOBAL_PREFS", str(global_file))
    fake_bin("gstub", "print('x')\n")
    _only_bins(monkeypatch, {"gstub"})

    assert main(["discover", "--project", str(repo)]) == 0

    out = capsys.readouterr().out
    assert "gstub:m  cost=free  trains=no" in out


# --- escalation ------------------------------------------------------------


def test_pick_escalates_after_two_free_failures(
    git_repo, fake_bin, monkeypatch, capsys
):
    repo = git_repo
    _write_prefs(repo, _ESCALATION_PREFS)
    fake_bin("freebie", "print('x')\n")
    fake_bin("strong", "print('x')\n")
    _only_bins(monkeypatch, {"freebie", "strong"})

    job = records.new_job(repo, "Escalate", "do it", True, "two parts")
    records.add_piece(job, "Hard bit", "code", "low")
    records.add_attempt(job, 1, {"executor": "freebie:m", "role": "exec",
                                 "status": "failed", "cost": "free"})
    records.add_attempt(job, 1, {"executor": "freebie:m", "role": "exec",
                                 "status": "failed", "cost": "free"})
    records.save_job(repo, job)

    assert main(["pick", "--project", str(repo), "--job", job["id"],
                 "--piece", "1"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0].strip() == "strong:s"

    piece = _stored_job(repo, job["id"])["pieces"][0]
    assert piece["escalations"][0]["reason"] == "2 free failures"
    assert piece["escalations"][0]["to"] == "strong:s"
    assert piece["picks"][-1]["pick"] == "strong:s"


# --- errors ----------------------------------------------------------------


def test_errors_exit_2_with_one_line(git_repo, capsys):
    repo = git_repo
    _write_prefs(repo, '[[models]]\nmatch = "x:*"\ncost = "cheap"\n')

    assert main(["discover", "--project", str(repo)]) == 2

    err = capsys.readouterr().err
    assert "preferences.toml" in err
    assert len(err.strip().splitlines()) == 1


def test_job_new_requires_reason_when_not_distributed(git_repo, capsys):
    repo = git_repo
    request = _write(repo / "request.md", "Please do it.\n")

    code = main(["job", "new", "--project", str(repo), "--title", "T",
                 "--request-file", str(request), "--not-distributed"])

    assert code == 2
    err = capsys.readouterr().err
    assert "--reason" in err
    assert len(err.strip().splitlines()) == 1
    assert not (repo / "docs" / "crew-chief" / "jobs").exists()


def test_dispatch_rejects_unknown_executor_and_missing_target(
    git_repo, fake_bin, monkeypatch, capsys
):
    repo = git_repo
    _write_prefs(repo, _STUB_PREFS)
    fake_bin("stub", "print('x')\n")
    _only_bins(monkeypatch, {"stub"})
    brief = _write(repo / "brief.md", "Create out.py.\n")

    job = records.new_job(repo, "Dispatch", "do it", True, "one part")
    records.add_piece(job, "One", "code", "low")
    records.save_job(repo, job)

    code = main(["dispatch", "--project", str(repo), "--job", job["id"],
                 "--piece", "1", "--executor", "ghost:m",
                 "--brief-file", str(brief)])
    assert code == 2
    err = capsys.readouterr().err
    assert "ghost:m" in err
    assert len(err.strip().splitlines()) == 1

    code = main(["dispatch", "--project", str(repo), "--job", job["id"],
                 "--piece", "1", "--executor", "stub:m",
                 "--brief-file", str(brief), "--review"])
    assert code == 2
    assert "--target" in capsys.readouterr().err


def test_accept_without_ok_attempt_exits_1(git_repo, capsys):
    repo = git_repo
    job = records.new_job(repo, "Accept", "do it", True, "one part")
    records.add_piece(job, "One", "code", "low")
    records.save_job(repo, job)

    assert main(["accept", "--project", str(repo), "--job", job["id"],
                 "--piece", "1"]) == 1
    assert capsys.readouterr().out.strip()


def test_close_json_shape(git_repo, capsys):
    repo = git_repo
    _write_prefs(repo, _STUB_PREFS)
    job = records.new_job(repo, "Close", "do it", True, "one part")
    records.add_piece(job, "One", "code", "low")
    records.set_grade(job, 1, {"score": 100, "explain": "adherence 3/3 → 100",
                               "executor": "stub:m",
                               "graded_at": "2026-10-17T12:00:00+00:00"})
    records.save_job(repo, job)

    assert main(["close", "--project", str(repo), "--job", job["id"],
                 "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["commit"].startswith("feat: ")
    assert "Record: docs/crew-chief/jobs/" in payload["issue"]
    assert (repo / "docs" / "crew-chief" / "scoreboard.md").is_file()


# --- git failures ----------------------------------------------------------


def test_accept_merge_conflict_is_one_line_error_and_keeps_worktree(
    git_repo, capsys
):
    repo = git_repo
    job = records.new_job(repo, "Conflict", "do it", True, "one part")
    records.add_piece(job, "One", "code", "low")

    worktree, branch = create_worktree(repo, job["id"], 1, 1)
    (worktree / "README.md").write_text("worktree change\n", encoding="utf-8")
    commit_worktree(worktree, "crew change")

    (repo / "README.md").write_text("main change\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "main change")

    records.add_attempt(
        job,
        1,
        {
            "executor": "stub:m",
            "role": "exec",
            "status": "ok",
            "branch": branch,
            "worktree": str(worktree),
        },
    )
    records.save_job(repo, job)

    assert main(["accept", "--project", str(repo), "--job", job["id"],
                 "--piece", "1"]) == 1

    err = capsys.readouterr().err
    assert err.startswith("error: ")
    assert len(err.strip().splitlines()) == 1
    assert worktree.is_dir()
    assert branch in _git(repo, "branch", "--list", "--format=%(refname:short)")


# --- models seen -----------------------------------------------------------


def _models_prefs(*models: str, paid: tuple[str, ...] = ()) -> str:
    """Prefs with a ``stub`` tool listing ``models``; ``*-free`` ones are free."""
    listed = ", ".join(f'"{model}"' for model in models)
    text = (
        "[[tools]]\n"
        'name = "stub"\n'
        'kind = "generic"\n'
        'bin = "stub"\n'
        f"models = [{listed}]\n"
        'run_cmd = ["stub", "{prompt}"]\n'
    )
    for model in paid:
        text += f'\n[[models]]\nmatch = "stub:{model}"\ncost = "paid"\n'
    text += '\n[[models]]\nmatch = "stub:*-free"\ncost = "free"\n'
    return text


def _seen_path(repo: Path) -> Path:
    return repo / "docs" / "crew-chief" / "models-seen.json"


def test_discover_records_a_baseline_then_reports_changes(
    git_repo, fake_bin, monkeypatch, capsys
):
    repo = git_repo
    fake_bin("stub", "print('x')\n")
    _only_bins(monkeypatch, {"stub"})

    _write_prefs(repo, _models_prefs("a-free", "old-free"))
    assert main(["discover", "--project", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "stub:a-free  cost=free  trains=no" in out
    assert "models: baseline: 2 executors recorded" in out
    seen = json.loads(_seen_path(repo).read_text(encoding="utf-8"))
    assert set(seen["models"]) == {"stub:a-free", "stub:old-free"}
    assert seen["models"]["stub:a-free"]["present"] is True

    assert main(["discover", "--project", str(repo)]) == 0
    assert f"models: no changes since {seen['last_check']}" in capsys.readouterr().out

    _write_prefs(repo, _models_prefs("a-free", "new-free", "b", paid=("a-free",)))
    assert main(["discover", "--project", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "new free: stub:new-free (trains=no)" in out
    assert "new paid: stub:b (trains=no)" in out
    assert "gone: stub:old-free (was free)" in out
    assert "now paid: stub:a-free (was free, trains=no)" in out
    seen = json.loads(_seen_path(repo).read_text(encoding="utf-8"))
    assert seen["models"]["stub:old-free"]["present"] is False


def test_discover_skipped_tool_keeps_its_models(git_repo, fake_bin, monkeypatch, capsys):
    repo = git_repo
    fake_bin("stub", "print('x')\n")
    _only_bins(monkeypatch, {"stub"})
    _write_prefs(repo, _models_prefs("a-free"))
    assert main(["discover", "--project", str(repo)]) == 0
    capsys.readouterr()

    _only_bins(monkeypatch, set())  # the stub bin disappears from PATH
    assert main(["discover", "--project", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "gone:" not in out
    seen = json.loads(_seen_path(repo).read_text(encoding="utf-8"))
    assert seen["models"]["stub:a-free"]["present"] is True


def test_discover_json_has_changes(git_repo, fake_bin, monkeypatch, capsys):
    repo = git_repo
    fake_bin("stub", "print('x')\n")
    _only_bins(monkeypatch, {"stub"})
    _write_prefs(repo, _models_prefs("a-free"))
    assert main(["discover", "--project", str(repo), "--json"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["changes"]["baseline"] == 1
    assert first["changes"]["since"] is None

    _write_prefs(repo, _models_prefs("a-free", "b-free"))
    assert main(["discover", "--project", str(repo), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [e["id"] for e in payload["executors"]] == ["stub:a-free", "stub:b-free"]
    changes = payload["changes"]
    assert changes["added"] == [{"id": "stub:b-free", "cost": "free", "trains": False}]
    assert changes["removed"] == [] and changes["changed"] == []
    assert changes["baseline"] is None and changes["error"] is None
    assert changes["since"]


def test_job_new_checks_models_on_stderr(git_repo, fake_bin, monkeypatch, capsys):
    repo = git_repo
    fake_bin("stub", "print('x')\n")
    _only_bins(monkeypatch, {"stub"})
    _write_prefs(repo, _models_prefs("a-free"))
    assert main(["discover", "--project", str(repo)]) == 0
    capsys.readouterr()

    _write_prefs(repo, _models_prefs("a-free", "fresh-free"))
    request = _write(repo / "request.md", "Do it.\n")
    assert main(["job", "new", "--project", str(repo), "--title", "T",
                 "--request-file", str(request)]) == 0
    captured = capsys.readouterr()
    job_id = captured.out.strip()
    assert len(captured.out.strip().splitlines()) == 1
    assert (records.jobs_dir(repo) / f"{job_id}.json").is_file()
    assert "new free: stub:fresh-free (trains=no)" in captured.err


def test_job_new_survives_a_broken_discovery(git_repo, monkeypatch, capsys):
    from crew_chief import cli

    repo = git_repo

    def boom(prefs):
        raise RuntimeError("discovery exploded")

    monkeypatch.setattr(cli, "discover_full", boom)
    request = _write(repo / "request.md", "Do it.\n")
    assert main(["job", "new", "--project", str(repo), "--title", "T",
                 "--request-file", str(request)]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip()
    assert "models: check skipped: discovery exploded" in captured.err
    assert not _seen_path(repo).exists()


def test_job_new_survives_invalid_preferences(git_repo, capsys):
    repo = git_repo
    _write_prefs(repo, '[[models]]\nmatch = "x:*"\ncost = "cheap"\n')
    request = _write(repo / "request.md", "Do it.\n")
    assert main(["job", "new", "--project", str(repo), "--title", "T",
                 "--request-file", str(request)]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip()
    assert "models: check skipped:" in captured.err


def _seed_seen(repo: Path, hours_ago: float, models: dict | None = None) -> str:
    """Write a registry whose ``last_check`` is ``hours_ago`` hours old."""
    stamp = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat(
        timespec="seconds"
    )
    entry = {"cost": "free", "trains": False, "first_seen": stamp,
             "last_seen": stamp, "present": True}
    data = {"last_check": stamp, "models": models or {"stub:m": entry}}
    path = _seen_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2, sort_keys=True) + "\n"
    path.write_text(text, encoding="utf-8")
    return text


def _pick_job(repo: Path) -> str:
    job = records.new_job(repo, "Pick", "do it", True, "one part")
    records.add_piece(job, "One", "code", "low")
    records.save_job(repo, job)
    return job["id"]


def _stub_with_extra_model(prefs_extra: str = "") -> str:
    """``_STUB_PREFS`` with a second model, so a due check has news to report."""
    return prefs_extra + _STUB_PREFS.replace('models = ["m"]', 'models = ["m", "n"]')


def test_pick_with_a_fresh_check_does_not_write(git_repo, fake_bin, monkeypatch, capsys):
    repo = git_repo
    _write_prefs(repo, _stub_with_extra_model())
    fake_bin("stub", "print('x')\n")
    _only_bins(monkeypatch, {"stub"})
    job_id = _pick_job(repo)
    before = _seed_seen(repo, hours_ago=1)

    assert main(["pick", "--project", str(repo), "--job", job_id, "--piece", "1"]) == 0
    captured = capsys.readouterr()
    assert captured.out.splitlines()[0].startswith("stub:")
    assert "models:" not in captured.err
    assert _seen_path(repo).read_text(encoding="utf-8") == before


def test_pick_with_a_stale_check_writes_and_reports(
    git_repo, fake_bin, monkeypatch, capsys
):
    repo = git_repo
    _write_prefs(repo, _stub_with_extra_model())
    fake_bin("stub", "print('x')\n")
    _only_bins(monkeypatch, {"stub"})
    job_id = _pick_job(repo)
    before = _seed_seen(repo, hours_ago=25)

    assert main(["pick", "--project", str(repo), "--job", job_id, "--piece", "1"]) == 0
    captured = capsys.readouterr()
    assert captured.out.splitlines()[0].startswith("stub:")
    assert "new free: stub:n (trains=no)" in captured.err
    after = json.loads(_seen_path(repo).read_text(encoding="utf-8"))
    assert after["last_check"] > json.loads(before)["last_check"]
    assert "stub:n" in after["models"]


def test_pick_without_a_registry_records_a_baseline(
    git_repo, fake_bin, monkeypatch, capsys
):
    repo = git_repo
    _write_prefs(repo, _STUB_PREFS)
    fake_bin("stub", "print('x')\n")
    _only_bins(monkeypatch, {"stub"})
    job_id = _pick_job(repo)

    assert main(["pick", "--project", str(repo), "--job", job_id, "--piece", "1"]) == 0
    captured = capsys.readouterr()
    assert captured.out.splitlines()[0] == "stub:m"
    assert "models: baseline: 1 executors recorded" in captured.err
    assert _seen_path(repo).is_file()


def test_model_check_hours_changes_the_window(git_repo, fake_bin, monkeypatch, capsys):
    repo = git_repo
    fake_bin("stub", "print('x')\n")
    _only_bins(monkeypatch, {"stub"})
    job_id = _pick_job(repo)

    _write_prefs(repo, _stub_with_extra_model("model_check_hours = 48\n"))
    before = _seed_seen(repo, hours_ago=30)
    assert main(["pick", "--project", str(repo), "--job", job_id, "--piece", "1"]) == 0
    assert "models:" not in capsys.readouterr().err
    assert _seen_path(repo).read_text(encoding="utf-8") == before

    _write_prefs(repo, _stub_with_extra_model("model_check_hours = 1\n"))
    _seed_seen(repo, hours_ago=2)
    assert main(["pick", "--project", str(repo), "--job", job_id, "--piece", "1"]) == 0
    assert "new free: stub:n (trains=no)" in capsys.readouterr().err


def test_pick_survives_a_broken_check(git_repo, fake_bin, monkeypatch, capsys):
    from crew_chief import cli

    repo = git_repo
    _write_prefs(repo, _STUB_PREFS)
    fake_bin("stub", "print('x')\n")
    _only_bins(monkeypatch, {"stub"})
    job_id = _pick_job(repo)

    def boom(project, discovery, now=None):
        raise OSError("disk full")

    monkeypatch.setattr(cli, "check_models", boom)
    assert main(["pick", "--project", str(repo), "--job", job_id, "--piece", "1"]) == 0
    captured = capsys.readouterr()
    assert captured.out.splitlines()[0] == "stub:m"
    assert "models: check skipped: disk full" in captured.err


def test_dispatch_with_a_stale_check_writes(git_repo, fake_bin, monkeypatch, capsys):
    repo = git_repo
    _write_prefs(repo, _stub_with_extra_model())
    fake_bin("stub", _STUB)
    _only_bins(monkeypatch, {"stub"})
    job_id = _pick_job(repo)
    brief = _write(repo / "brief.md", "Create out.py.\n")
    _seed_seen(repo, hours_ago=25)

    assert main(["dispatch", "--project", str(repo), "--job", job_id,
                 "--piece", "1", "--executor", "stub:m",
                 "--brief-file", str(brief)]) == 0
    captured = capsys.readouterr()
    assert "status: ok" in captured.out
    assert "new free: stub:n (trains=no)" in captured.err


# --- parallel processes ----------------------------------------------------

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "skill" / "crew-chief" / "scripts"

#: Runs the CLI in a child process with discovery restricted to the stub bins.
_DRIVER = """
import shutil, sys
sys.path.insert(0, sys.argv[1])
allowed = set(sys.argv[2].split(","))
real_which = shutil.which
shutil.which = lambda cmd, *a, **k: real_which(cmd, *a, **k) if cmd in allowed else None
from crew_chief.cli import main
sys.exit(main(sys.argv[3:]))
"""

_SLOW_STUB = (
    "import time\n"
    "time.sleep(1)\n"
    "print('done')\n"
)


def _run_parallel(bins: str, argvs: list[list[str]]) -> list[int]:
    import sys

    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _DRIVER, str(_SCRIPTS_DIR), bins, *argv],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for argv in argvs
    ]
    return [proc.wait(timeout=300) for proc in procs]


def test_parallel_dispatches_of_one_piece_keep_every_attempt(
    git_repo, fake_bin, monkeypatch
):
    repo = git_repo
    _write_prefs(repo, _STUB_PREFS)
    fake_bin("stub", _SLOW_STUB)
    brief = _write(repo.parent / "brief.md", "Do it.\n")
    job = records.new_job(repo, "Parallel", "do it", True, "parts")
    records.add_piece(job, "Only piece", "code", "low")
    records.save_job(repo, job)

    argv = ["dispatch", "--project", str(repo), "--job", job["id"],
            "--piece", "1", "--executor", "stub:m", "--brief-file", str(brief)]
    _run_parallel("stub", [argv] * 4)

    attempts = _stored_job(repo, job["id"])["pieces"][0]["attempts"]
    assert len(attempts) == 4
    branches = sorted(attempt["branch"] for attempt in attempts)
    assert branches == sorted(
        f"crew/{job['id']}-1-{number}" for number in range(1, 5)
    )


def test_parallel_picks_keep_every_pick(git_repo, fake_bin, monkeypatch):
    repo = git_repo
    _write_prefs(repo, _STUB_PREFS)
    fake_bin("stub", "print('x')\n")
    job = records.new_job(repo, "Parallel picks", "do it", True, "parts")
    for number in range(1, 7):
        records.add_piece(job, f"Piece {number}", "code", "low")
    records.save_job(repo, job)

    codes = _run_parallel(
        "stub",
        [
            ["pick", "--project", str(repo), "--job", job["id"],
             "--piece", str(number)]
            for number in range(1, 7)
        ],
    )

    assert codes == [0] * 6
    pieces = _stored_job(repo, job["id"])["pieces"]
    assert all(len(piece.get("picks", [])) == 1 for piece in pieces)
