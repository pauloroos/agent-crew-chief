"""Tests for the ``crew-chief`` CLI.

The full cycle runs the real commands against a throwaway git repo with a
``generic`` stub tool declared in its ``docs/crew-chief/preferences.toml``.
Discovery is kept hermetic: ``shutil.which`` is patched so that only the stub
bins are seen on PATH, which means no real executor CLI has to be installed.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from crew_chief import records
from crew_chief.cli import main

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
