"""Tests for the ``crew-chief`` CLI.

The full cycle runs the real commands against a throwaway git repo with a
``generic`` stub tool declared in its ``docs/crew-chief/preferences.toml``.
Discovery is kept hermetic: ``shutil.which`` is patched so that only the stub
bins are seen on PATH, which means no real executor CLI has to be installed.
"""

from __future__ import annotations

import json
import random
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

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
    accepted = _stored_job(repo, job_id)["pieces"][0]["accepted"]
    assert accepted["attempt"] == 1 and accepted["branch"].startswith("crew/")
    assert accepted["at"]

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
    records.add_piece(job, "Hard bit", "design", "low")
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



def test_pick_escalates_after_two_free_no_change_attempts(
    git_repo, fake_bin, monkeypatch, capsys
):
    repo = git_repo
    _write_prefs(repo, _ESCALATION_PREFS)
    fake_bin("freebie", "print('x')\n")
    fake_bin("strong", "print('x')\n")
    _only_bins(monkeypatch, {"freebie", "strong"})

    job = records.new_job(repo, "Escalate", "do it", True, "two parts")
    records.add_piece(job, "Hard bit", "design", "low")
    for _ in range(2):
        records.add_attempt(job, 1, {"executor": "freebie:m", "role": "exec",
                                     "status": "no_change", "cost": "free"})
    records.save_job(repo, job)

    assert main(["pick", "--project", str(repo), "--job", job["id"],
                 "--piece", "1"]) == 0
    assert capsys.readouterr().out.splitlines()[0].strip() == "strong:s"
    piece = _stored_job(repo, job["id"])["pieces"][0]
    assert piece["escalations"][0]["reason"] == "2 free failures"


def test_pick_exec_work_never_escalates_after_two_free_failures(
    git_repo, fake_bin, monkeypatch, capsys
):
    repo = git_repo
    _write_prefs(repo, _ESCALATION_PREFS)
    fake_bin("freebie", "print('x')\n")
    fake_bin("strong", "print('x')\n")
    _only_bins(monkeypatch, {"freebie", "strong"})

    job = records.new_job(repo, "Stay free", "do it", True, "two parts")
    records.add_piece(job, "Hands-on bit", "code", "low")
    for _ in range(2):
        records.add_attempt(job, 1, {"executor": "freebie:m", "role": "exec",
                                     "status": "failed", "cost": "free"})
    records.save_job(repo, job)

    assert main(["pick", "--project", str(repo), "--job", job["id"],
                 "--piece", "1"]) == 1
    out = capsys.readouterr().out
    assert "strong:s" not in out
    assert "split the piece" in out and "do it yourself" in out
    piece = _stored_job(repo, job["id"])["pieces"][0]
    assert not piece.get("escalations")


def test_pick_exec_work_goes_paid_with_strong(
    git_repo, fake_bin, monkeypatch, capsys
):
    repo = git_repo
    _write_prefs(repo, _ESCALATION_PREFS)
    fake_bin("freebie", "print('x')\n")
    fake_bin("strong", "print('x')\n")
    _only_bins(monkeypatch, {"freebie", "strong"})

    job = records.new_job(repo, "Strong", "do it", True, "one part")
    records.add_piece(job, "Knot", "refactor", "high")
    records.save_job(repo, job)

    assert main(["pick", "--project", str(repo), "--job", job["id"],
                 "--piece", "1", "--strong"]) == 0
    assert capsys.readouterr().out.splitlines()[0].strip() == "strong:s"
    piece = _stored_job(repo, job["id"])["pieces"][0]
    assert piece["escalations"][0]["reason"].startswith("--strong")


_REVIEW_PREFS = """
strong_order = ["strong"]

[[tools]]
name = "freebie"
kind = "generic"
bin = "freebie"
models = ["m-high", "n"]
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


def _reviewed_job(repo: Path, executor: str) -> dict:
    """A job whose code piece 1 has an ``ok`` exec attempt by ``executor``."""
    job = records.new_job(repo, "Review me", "do it", True, "one part")
    records.add_piece(job, "Change", "code", "low")
    records.add_attempt(job, 1, {"executor": executor, "role": "exec",
                                 "status": "ok", "cost": "free"})
    records.save_job(repo, job)
    return job


def test_pick_review_leaves_out_the_executor_quota_family(
    git_repo, fake_bin, monkeypatch, capsys
):
    repo = git_repo
    _write_prefs(repo, _REVIEW_PREFS)
    fake_bin("freebie", "print('x')\n")
    fake_bin("strong", "print('x')\n")
    _only_bins(monkeypatch, {"freebie", "strong"})
    job = _reviewed_job(repo, "freebie:n")

    assert main(["pick", "--project", str(repo), "--job", job["id"],
                 "--piece", "1", "--review"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].strip() == "freebie:m-high"
    assert "reviewer outside the family of: freebie:n" in lines[1]
    piece = _stored_job(repo, job["id"])["pieces"][0]
    assert piece["picks"][-1]["role"] == "review"


def test_pick_review_escalates_when_only_the_same_family_is_free(
    git_repo, fake_bin, monkeypatch, capsys
):
    repo = git_repo
    _write_prefs(repo, _REVIEW_PREFS.replace('["m-high", "n"]', '["m-high"]'))
    fake_bin("freebie", "print('x')\n")
    fake_bin("strong", "print('x')\n")
    _only_bins(monkeypatch, {"freebie", "strong"})
    job = _reviewed_job(repo, "freebie:m-low")

    assert main(["pick", "--project", str(repo), "--job", job["id"],
                 "--piece", "1", "--review"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].strip() == "strong:s"
    assert lines[1].startswith("escalated: no free executor available")


#: One model behind the two OpenCode routes, plus another model to review.
_ROUTE_PREFS = """
[[tools]]
name = "opencode"
kind = "opencode"
bin = "opencode"
models = [
  "opencode/grok-code-free",
  "opencode-go/grok-code-free",
  "opencode/qwen3-coder-free",
]

[[models]]
match = "opencode:opencode/*-free"
cost = "free"

[[models]]
match = "opencode:opencode-go/*-free"
cost = "free"
"""


@pytest.mark.parametrize(
    "author",
    [
        "opencode:opencode/grok-code-free",
        "opencode:opencode-go/grok-code-free",
    ],
)
def test_pick_review_leaves_out_the_other_route_of_the_same_model(
    git_repo, fake_bin, monkeypatch, capsys, author
):
    repo = git_repo
    _write_prefs(repo, _ROUTE_PREFS)
    fake_bin("opencode", "print('x')\n")
    _only_bins(monkeypatch, {"opencode"})
    job = _reviewed_job(repo, author)

    assert main(["pick", "--project", str(repo), "--job", job["id"],
                 "--piece", "1", "--review"]) == 0
    lines = capsys.readouterr().out.splitlines()
    # The alias of the author's model stays out; another model is picked.
    assert lines[0].strip() == "opencode:opencode/qwen3-coder-free"
    assert f"reviewer outside the family of: {author}" in lines[1]


#: The two routes of one model, each classified with its own cost. The third
#: model matches no rule, so it falls back to the default ``paid``.
_ROUTE_COST_PREFS = """
[[tools]]
name = "opencode"
kind = "opencode"
bin = "opencode"
models = [
  "opencode/grok-code-free",
  "opencode-go/grok-code-free",
  "opencode/qwen3-coder",
]

[[models]]
match = "{free}"
cost = "free"

[[models]]
match = "{included}"
cost = "included"
"""


@pytest.mark.parametrize(
    "author, free, included",
    [
        ("opencode:opencode/grok-code-free",
         "opencode:opencode/grok-code-free",
         "opencode:opencode-go/grok-code-free"),
        ("opencode:opencode-go/grok-code-free",
         "opencode:opencode-go/grok-code-free",
         "opencode:opencode/grok-code-free"),
        ("opencode:opencode/grok-code-free",
         "opencode:opencode-go/grok-code-free",
         "opencode:opencode/grok-code-free"),
        ("opencode:opencode-go/grok-code-free",
         "opencode:opencode/grok-code-free",
         "opencode:opencode-go/grok-code-free"),
    ],
)
def test_pick_review_never_takes_the_other_route_of_the_author_model(
    git_repo, fake_bin, monkeypatch, capsys, author, free, included
):
    """Whichever route costs what, review never lands on the author's model."""
    repo = git_repo
    _write_prefs(repo, _ROUTE_COST_PREFS.format(free=free, included=included))
    fake_bin("opencode", "print('x')\n")
    _only_bins(monkeypatch, {"opencode"})
    job = _reviewed_job(repo, author)

    assert main(["pick", "--project", str(repo), "--job", job["id"],
                 "--piece", "1", "--review"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].strip() == "opencode:opencode/qwen3-coder"
    assert f"reviewer outside the family of: {author}" in lines[1]


#: Only the alias route of the author's model is still discovered.
_GONE_AUTHOR_PREFS = """
[[tools]]
name = "opencode"
kind = "opencode"
bin = "opencode"
models = ["opencode-go/grok-code-free", "opencode/qwen3-coder"]

[[models]]
match = "opencode:opencode-go/grok-code-free"
cost = "{cost}"
"""


@pytest.mark.parametrize("alias_cost", ["free", "included"])
def test_pick_review_leaves_out_a_route_the_author_is_missing_from(
    git_repo, fake_bin, monkeypatch, capsys, alias_cost
):
    """The author is no longer discovered: the other route still counts."""
    repo = git_repo
    _write_prefs(repo, _GONE_AUTHOR_PREFS.format(cost=alias_cost))
    fake_bin("opencode", "print('x')\n")
    _only_bins(monkeypatch, {"opencode"})
    author = "opencode:opencode/grok-code-free"
    job = _reviewed_job(repo, author)

    assert main(["pick", "--project", str(repo), "--job", job["id"],
                 "--piece", "1", "--review"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].strip() == "opencode:opencode/qwen3-coder"
    assert f"reviewer outside the family of: {author}" in lines[1]


def test_pick_excludes_an_executor_with_no_change_on_the_piece(
    git_repo, fake_bin, monkeypatch, capsys
):
    repo = git_repo
    _write_prefs(repo, _STUB_PREFS.replace('models = ["m"]', 'models = ["m", "n"]'))
    fake_bin("stub", "print('x')\n")
    _only_bins(monkeypatch, {"stub"})

    job = records.new_job(repo, "Exclude", "do it", True, "one part")
    records.add_piece(job, "One", "code", "low")
    records.add_attempt(job, 1, {"executor": "stub:m", "role": "exec",
                                 "status": "no_change", "cost": "free"})
    records.save_job(repo, job)

    assert main(["pick", "--project", str(repo), "--job", job["id"],
                 "--piece", "1"]) == 0
    assert capsys.readouterr().out.splitlines()[0].strip() == "stub:n"


def _three_model_stub_repo(repo, fake_bin, monkeypatch):
    """A repo whose only tool offers three free models: stub:m, stub:n, stub:p."""
    _write_prefs(
        repo, _STUB_PREFS.replace('models = ["m"]', 'models = ["m", "n", "p"]')
    )
    fake_bin("stub", "print('x')\n")
    _only_bins(monkeypatch, {"stub"})


def _excluded_pick_job(repo):
    """A job with one code piece, ready to be picked."""
    job = records.new_job(repo, "Exclude", "do it", True, "one part")
    records.add_piece(job, "One", "code", "low")
    records.save_job(repo, job)
    return job


def test_pick_exclude_repeated_flags_exclude_every_executor(
    git_repo, fake_bin, monkeypatch, capsys
):
    # --exclude twice must leave out both ids: the second flag used to
    # replace the first one, so only the last id was excluded.
    repo = git_repo
    _three_model_stub_repo(repo, fake_bin, monkeypatch)
    job = _excluded_pick_job(repo)

    assert main(["pick", "--project", str(repo), "--job", job["id"],
                 "--piece", "1", "--exclude", "stub:m",
                 "--exclude", "stub:n"]) == 0
    assert capsys.readouterr().out.splitlines()[0].strip() == "stub:p"


def test_pick_exclude_takes_several_ids_in_one_flag(
    git_repo, fake_bin, monkeypatch, capsys
):
    # One flag with several ids keeps working.
    repo = git_repo
    _three_model_stub_repo(repo, fake_bin, monkeypatch)
    job = _excluded_pick_job(repo)

    assert main(["pick", "--project", str(repo), "--job", job["id"],
                 "--piece", "1", "--exclude", "stub:m", "stub:n"]) == 0
    assert capsys.readouterr().out.splitlines()[0].strip() == "stub:p"


def test_pick_exclude_repeated_flags_leave_no_executor(
    git_repo, fake_bin, monkeypatch, capsys
):
    # The ids of every flag pile up, so excluding all three leaves nobody.
    repo = git_repo
    _three_model_stub_repo(repo, fake_bin, monkeypatch)
    job = _excluded_pick_job(repo)

    assert main(["pick", "--project", str(repo), "--job", job["id"],
                 "--piece", "1", "--exclude", "stub:m",
                 "--exclude", "stub:n", "stub:p"]) == 1
    assert "no executor available" in capsys.readouterr().out


def test_pick_hands_a_correction_to_another_executor(
    git_repo, fake_bin, monkeypatch, capsys
):
    # The executor whose work drew a correction does not get the fix.
    repo = git_repo
    _write_prefs(repo, _STUB_PREFS.replace('models = ["m"]', 'models = ["m", "n"]'))
    fake_bin("stub", "print('x')\n")
    _only_bins(monkeypatch, {"stub"})

    job = records.new_job(repo, "Correct", "do it", True, "one part")
    records.add_piece(job, "One", "code", "low")
    records.add_attempt(job, 1, {"executor": "stub:m", "role": "exec",
                                 "status": "ok", "cost": "free"})
    records.add_correction(job, 1, "wrong name", "stub:m")
    # Elsewhere in the job, stub:n is used more: spread alone would pick m.
    records.add_piece(job, "Two", "code", "low")
    records.add_piece(job, "Three", "code", "low")
    for n in (2, 3):
        records.add_attempt(job, n, {"executor": "stub:n", "role": "exec",
                                     "status": "ok", "cost": "free"})
    records.save_job(repo, job)

    assert main(["pick", "--project", str(repo), "--job", job["id"],
                 "--piece", "1"]) == 0
    assert capsys.readouterr().out.splitlines()[0].strip() == "stub:n"


def test_dispatch_with_no_change_exits_1_and_accept_refuses_it(
    git_repo, fake_bin, monkeypatch, capsys
):
    repo = git_repo
    _write_prefs(repo, _STUB_PREFS)
    fake_bin("stub", "print('brief not found')\n")
    _only_bins(monkeypatch, {"stub"})
    brief = _write(repo / "brief.md", "Create out.py.\n")
    job_id = _pick_job(repo)

    assert main(["dispatch", "--project", str(repo), "--job", job_id,
                 "--piece", "1", "--executor", "stub:m",
                 "--brief-file", str(brief)]) == 1
    assert "status: no_change" in capsys.readouterr().out

    assert main(["accept", "--project", str(repo), "--job", job_id,
                 "--piece", "1"]) == 1
    assert "nothing to accept" in capsys.readouterr().out


# --- errors ----------------------------------------------------------------


def test_errors_exit_2_with_one_line(git_repo, capsys):
    repo = git_repo
    _write_prefs(repo, '[[models]]\nmatch = "x:*"\ncost = "bogus"\n')

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


_WORK_TYPES_PREFS = """
[[tools]]
name = "opencode"
kind = "generic"
bin = "stub"
models = ["a-free"]
run_cmd = ["stub", "{prompt}"]

[[tools]]
name = "doer"
kind = "generic"
bin = "stub"
models = ["d"]
run_cmd = ["stub", "{prompt}"]
work_types = ["code", "tests", "refactor", "docs", "data"]

[[tools]]
name = "writer"
kind = "generic"
bin = "stub"
models = ["w"]
run_cmd = ["stub", "{prompt}"]
work_types = ["research", "docs"]

[[models]]
match = "*"
cost = "free"
"""


def test_discover_ends_with_the_work_types_of_each_tool(
    git_repo, fake_bin, monkeypatch, capsys
):
    repo = git_repo
    fake_bin("stub", "print('x')\n")
    _only_bins(monkeypatch, {"stub"})
    _write_prefs(repo, _WORK_TYPES_PREFS)

    assert main(["discover", "--project", str(repo)]) == 0
    lines = capsys.readouterr().out.rstrip().splitlines()
    assert lines[-3:] == [
        "work types: opencode: all types, first on execution work",
        "work types: doer: exec only (code, tests, refactor, docs, data)",
        "work types: writer: docs, research",
    ]

    assert main(["discover", "--project", str(repo), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["work_types"]["writer"] == ["docs", "research"]
    assert payload["exec_first"] == ["opencode"]


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
    _write_prefs(repo, '[[models]]\nmatch = "x:*"\ncost = "bogus"\n')
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
    "from pathlib import Path\n"
    "time.sleep(1)\n"
    "Path('out.py').write_text('x = 1\\n', encoding='utf-8')\n"
    "print('done')\n"
)


def _run_parallel_outputs(
    bins: str, argvs: list[list[str]]
) -> list[tuple[int, str]]:
    """Run each CLI call in its own process; ``(exit code, stdout+stderr)``."""
    import sys

    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _DRIVER, str(_SCRIPTS_DIR), bins, *argv],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        for argv in argvs
    ]
    results = []
    for proc in procs:
        output, _ = proc.communicate(timeout=300)
        results.append((proc.returncode, output))
    return results


def _run_parallel(bins: str, argvs: list[list[str]]) -> list[int]:
    return [code for code, _ in _run_parallel_outputs(bins, argvs)]


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
    results = _run_parallel_outputs("stub", [argv] * 4)

    # The children's output explains a failure that only shows under load.
    report = "\n---\n".join(output for _, output in results)
    assert [code for code, _ in results] == [0] * 4, report
    attempts = _stored_job(repo, job["id"])["pieces"][0]["attempts"]
    assert len(attempts) == 4, report
    assert {attempt["status"] for attempt in attempts} == {"ok"}, report
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


def test_pick_skips_an_executor_that_hit_quota_in_another_job(
    git_repo, fake_bin, monkeypatch, capsys
):
    repo = git_repo
    _write_prefs(repo, _ESCALATION_PREFS)
    fake_bin("freebie", "print('x')\n")
    fake_bin("strong", "print('x')\n")
    _only_bins(monkeypatch, {"freebie", "strong"})

    earlier = records.new_job(repo, "Earlier", "do it", True, "parts")
    records.add_piece(earlier, "Bit", "code", "low")
    records.add_attempt(earlier, 1, {
        "executor": "freebie:m", "role": "exec", "status": "quota",
        "cost": "free", "duration_s": 5.0,
        "started": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
    })
    records.save_job(repo, earlier)
    job = records.new_job(repo, "Now", "do it", True, "parts")
    records.add_piece(job, "Other bit", "diagnosis", "low")
    records.save_job(repo, job)

    assert main(["pick", "--project", str(repo), "--job", job["id"],
                 "--piece", "1"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].strip() == "strong:s"
    assert "skipped for quota: freebie:m" in lines[1]


def test_dispatch_reports_duration_steps_and_tokens(
    git_repo, fake_bin, monkeypatch, capsys
):
    repo = git_repo
    _write_prefs(repo, _STUB_PREFS)
    fake_bin("stub", _STUB)
    _only_bins(monkeypatch, {"stub"})
    brief = _write(repo.parent / "brief.md", "b" * 40)
    job = records.new_job(repo, "Metrics", "do it", True, "parts")
    records.add_piece(job, "Bit", "code", "low")
    records.save_job(repo, job)
    argv = ["dispatch", "--project", str(repo), "--job", job["id"],
            "--piece", "1", "--executor", "stub:m", "--brief-file", str(brief)]

    assert main(argv) == 0
    out = capsys.readouterr().out
    assert "steps: -" in out
    assert "tokens: ~12 (estimated)" in out
    assert "duration: " in out

    assert main([*argv, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["steps"] is None
    assert payload["tokens_total"] == 12
    assert payload["tokens_estimated"] is True
    assert isinstance(payload["duration_s"], float)


#: Correction stub: appends one line to ``out.py`` (creating it when missing).
_ADJUST_STUB = (
    "from pathlib import Path\n"
    "out = Path('out.py')\n"
    "text = out.read_text(encoding='utf-8') if out.exists() else ''\n"
    "out.write_text(text + 'y = 2\\n', encoding='utf-8')\n"
    "print('adjusted')\n"
)


def test_correction_cycle_builds_on_the_earlier_attempt(
    git_repo, fake_bin, monkeypatch, capsys
):
    repo = git_repo
    _write_prefs(repo, _STUB_PREFS)
    fake_bin("stub", _STUB)
    _only_bins(monkeypatch, {"stub"})
    brief = _write(repo.parent / "brief.md", "Create out.py.\n")
    fix = _write(repo.parent / "fix.md", "Add y = 2 to out.py.\n")
    job = records.new_job(repo, "Correct", "do it", True, "parts")
    records.add_piece(job, "Bit", "code", "low")
    records.save_job(repo, job)
    dispatch_argv = ["dispatch", "--project", str(repo), "--job", job["id"],
                     "--piece", "1", "--executor", "stub:m"]

    assert main([*dispatch_argv, "--brief-file", str(brief)]) == 0
    first = capsys.readouterr().out
    assert "base: HEAD" in first
    first_branch = _stored_job(repo, job["id"])["pieces"][0]["attempts"][0][
        "branch"
    ]

    assert main(["correction", "--project", str(repo), "--job", job["id"],
                 "--piece", "1", "--reason", "missing y",
                 "--executor", "stub:m"]) == 0
    capsys.readouterr()

    fake_bin("stub", _ADJUST_STUB)
    assert main([*dispatch_argv, "--brief-file", str(fix), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["base"] == {"attempt": 1, "branch": first_branch}

    assert main(["accept", "--project", str(repo), "--job", job["id"],
                 "--piece", "1"]) == 0
    capsys.readouterr()
    assert (repo / "out.py").read_text(encoding="utf-8") == "x = 1\ny = 2\n"


def test_dispatch_fresh_and_base_options(git_repo, fake_bin, monkeypatch, capsys):
    repo = git_repo
    _write_prefs(repo, _STUB_PREFS)
    fake_bin("stub", _STUB)
    _only_bins(monkeypatch, {"stub"})
    brief = _write(repo.parent / "brief.md", "Create out.py.\n")
    job = records.new_job(repo, "Options", "do it", True, "parts")
    records.add_piece(job, "Bit", "code", "low")
    records.save_job(repo, job)
    argv = ["dispatch", "--project", str(repo), "--job", job["id"],
            "--piece", "1", "--executor", "stub:m", "--brief-file", str(brief)]

    assert main(argv) == 0
    capsys.readouterr()
    records.update_job(
        repo, job["id"], lambda j: records.add_correction(j, 1, "fix", "stub:m")
    )
    fake_bin("stub", _ADJUST_STUB)

    assert main([*argv, "--fresh"]) == 0
    assert "base: HEAD" in capsys.readouterr().out

    assert main([*argv, "--base", "1"]) == 0
    out = capsys.readouterr().out
    assert f"base: crew/{job['id']}-1-1 (attempt 1)" in out

    assert main([*argv, "--base", "9"]) == 2
    assert "no exec attempt 9" in capsys.readouterr().err

    target = _write(repo.parent / "target.txt", "")
    assert main([*argv, "--review", "--target", str(target.parent),
                 "--fresh"]) == 2
    assert "exec" in capsys.readouterr().err


def test_each_executor_keeps_its_own_grade(git_repo, capsys):
    repo = git_repo
    job = records.new_job(repo, "Two hands", "do it", True, "parts")
    records.add_piece(job, "Bit", "code", "low")
    records.add_attempt(job, 1, {"executor": "beam:m", "role": "exec",
                                 "status": "ok"})
    records.add_attempt(job, 1, {"executor": "minimax:m", "role": "exec",
                                 "status": "ok"})
    records.save_job(repo, job)

    def grade(executor: str, corrections: str, missing: str) -> None:
        assert main(["grade", "--project", str(repo), "--job", job["id"],
                     "--piece", "1", "--executor", executor,
                     "--corrections", corrections, "--done", "3",
                     "--missing", missing, "--extra", "0"]) == 0
        capsys.readouterr()

    grade("beam:m", "1", "1")  # 100*3/4 - 15 = 60
    grade("minimax:m", "0", "0")  # 100
    grade("beam:m", "1", "0")  # regrade: 100 - 15 = 85

    grades = _stored_job(repo, job["id"])["pieces"][0]["grades"]
    assert {name: g["score"] for name, g in grades.items()} == {
        "beam:m": 85,
        "minimax:m": 100,
    }
    scoreboard = (repo / "docs" / "crew-chief" / "scoreboard.md").read_text(
        encoding="utf-8"
    )
    assert "| minimax:m | 100 | 1 |" in scoreboard
    assert "| beam:m | 85 | 1 |" in scoreboard

    assert main(["close", "--project", str(repo), "--job", job["id"]]) == 0
    closed = capsys.readouterr().out
    assert "Bit — minimax:m, score 100; also beam:m, score 85" in closed


def test_entry_script_writes_no_bytecode(tmp_path):
    # Running the CLI must not leave __pycache__ folders in the project.
    import shutil
    import subprocess
    import sys as _sys
    from pathlib import Path as _Path

    source = _Path(__file__).resolve().parents[1] / "skill" / "crew-chief" / "scripts"
    copy = tmp_path / "scripts"
    shutil.copytree(source, copy, ignore=shutil.ignore_patterns("__pycache__"))

    subprocess.run(
        [_sys.executable, "-I", str(copy / "crew-chief.py"), "--help"],
        capture_output=True,
        check=False,
    )

    assert not list(copy.rglob("__pycache__"))


def test_accept_on_a_stale_piece_merges_and_warns(
    git_repo, fake_bin, monkeypatch, capsys
):
    repo = git_repo
    _write_prefs(repo, _STUB_PREFS)
    fake_bin("stub", _STUB)
    _only_bins(monkeypatch, {"stub"})
    brief = _write(repo.parent / "brief.md", "Create out.py.\n")
    job = records.new_job(repo, "Stale", "do it", True, "parts")
    records.add_piece(job, "T012 old text", "code", "low", ref="T012")
    records.save_job(repo, job)
    assert main(["dispatch", "--project", str(repo), "--job", job["id"],
                 "--piece", "1", "--executor", "stub:m",
                 "--brief-file", str(brief)]) == 0
    capsys.readouterr()
    records.update_job(
        repo, job["id"], lambda stored: stored["pieces"][0].update(stale=True)
    )

    assert main(["accept", "--project", str(repo), "--job", job["id"],
                 "--piece", "1"]) == 0
    captured = capsys.readouterr()
    assert "accepted crew/" in captured.out
    assert "piece 1 is stale (task T012 changed); tasks.md not marked" in captured.err
    assert (repo / "out.py").is_file()
    assert _stored_job(repo, job["id"])["pieces"][0]["accepted"]["attempt"] == 1


# --- spec proof ------------------------------------------------------------


def _spec_job(repo: Path, **meta) -> str:
    """A job with one spec piece (T012, code) whose sidecar carries ``meta``."""
    from crew_chief.sidecar import Sidecar, TaskMeta, save_sidecar

    folder = repo / "specs" / "001-x"
    folder.mkdir(parents=True)
    (folder / "tasks.md").write_text("- [ ] T012 Make out.py\n", encoding="utf-8")
    save_sidecar(folder, Sidecar(
        spec_dir="specs/001-x",
        tasks={"T012": TaskMeta(meta.pop("type", "code"), "low", **meta)},
    ))
    job = records.new_job(repo, "Spec", "do it", True, "parts")
    records.add_piece(job, "T012 Make out.py", "code", "low", ref="T012")
    job["spec_dir"] = "specs/001-x"
    records.save_job(repo, job)
    return job["id"]


def _dispatch_argv(repo: Path, job_id: str, brief: Path, *extra: str) -> list[str]:
    return ["dispatch", "--project", str(repo), "--job", job_id, "--piece", "1",
            "--executor", "stub:m", "--brief-file", str(brief), *extra]


def _stub_repo(repo, fake_bin, monkeypatch) -> Path:
    _write_prefs(repo, _STUB_PREFS)
    fake_bin("stub", _STUB)
    _only_bins(monkeypatch, {"stub"})
    return _write(repo.parent / "brief.md", "Create out.py.\n")


def _py(code: str) -> str:
    return f'"{sys.executable}" -c "{code}"'


def test_cli_dispatch_refuses_spec_piece_without_proof(
    git_repo, fake_bin, monkeypatch, capsys
):
    repo = git_repo
    brief = _stub_repo(repo, fake_bin, monkeypatch)
    job_id = _spec_job(repo)

    assert main(_dispatch_argv(repo, job_id, brief)) == 2
    captured = capsys.readouterr()
    assert captured.err.strip() == (
        "piece 1 (T012) has no proof in crew.toml; "
        "add one or pass --no-proof"
    )
    assert _stored_job(repo, job_id)["pieces"][0]["attempts"] == []


def test_cli_dispatch_no_proof_flag_records_skip(
    git_repo, fake_bin, monkeypatch, capsys
):
    repo = git_repo
    brief = _stub_repo(repo, fake_bin, monkeypatch)
    job_id = _spec_job(repo)

    assert main(_dispatch_argv(repo, job_id, brief, "--no-proof")) == 0
    attempt = _stored_job(repo, job_id)["pieces"][0]["attempts"][0]
    assert attempt["status"] == "ok"
    assert attempt["proof"] == {"skipped": True}


def test_cli_dispatch_runs_the_sidecar_proof(git_repo, fake_bin, monkeypatch, capsys):
    repo = git_repo
    brief = _stub_repo(repo, fake_bin, monkeypatch)
    job_id = _spec_job(
        repo, proof=_py("import out; print('imported')"), expect="imported"
    )

    assert main(_dispatch_argv(repo, job_id, brief, "--json")) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert payload["proof"]["passed"] is True
    attempt = _stored_job(repo, job_id)["pieces"][0]["attempts"][0]
    assert attempt["proof"]["exit"] == 0


def test_cli_dispatch_failed_proof_exits_1_and_flags_scope(
    git_repo, fake_bin, monkeypatch, capsys
):
    repo = git_repo
    brief = _stub_repo(repo, fake_bin, monkeypatch)
    job_id = _spec_job(
        repo, proof=_py("import sys; sys.exit(4)"), files=["src/*"]
    )

    assert main(_dispatch_argv(repo, job_id, brief)) == 1
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "status: proof_failed"
    assert "scope: out.py" in out
    attempt = _stored_job(repo, job_id)["pieces"][0]["attempts"][0]
    assert attempt["proof"]["exit"] == 4
    assert attempt["scope"] == ["out.py"]


def test_cli_dispatch_think_piece_without_proof_is_not_refused(
    git_repo, fake_bin, monkeypatch, capsys
):
    repo = git_repo
    brief = _stub_repo(repo, fake_bin, monkeypatch)
    job_id = _spec_job(repo, type="design")

    assert main(_dispatch_argv(repo, job_id, brief)) == 0


def test_cli_dispatch_piece_without_ref_keeps_v020_behaviour(
    git_repo, fake_bin, monkeypatch, capsys
):
    repo = git_repo
    brief = _stub_repo(repo, fake_bin, monkeypatch)
    job = records.new_job(repo, "Plain", "do it", True, "parts")
    records.add_piece(job, "One", "code", "low")
    records.save_job(repo, job)

    assert main(_dispatch_argv(repo, job["id"], brief)) == 0
    assert "proof" not in _stored_job(repo, job["id"])["pieces"][0]["attempts"][0]


def test_cli_dispatch_refuses_a_stale_piece_exec_and_review(
    git_repo, fake_bin, monkeypatch, capsys
):
    repo = git_repo
    brief = _stub_repo(repo, fake_bin, monkeypatch)
    job_id = _spec_job(repo, proof=_py("pass"))
    records.update_job(
        repo, job_id, lambda stored: stored["pieces"][0].update(stale=True)
    )
    message = "piece 1 is stale (task T012 changed); re-import the spec"

    assert main(_dispatch_argv(repo, job_id, brief)) == 2
    assert capsys.readouterr().err.strip() == message
    assert main(_dispatch_argv(
        repo, job_id, brief, "--review", "--target", str(repo)
    )) == 2
    assert capsys.readouterr().err.strip() == message
    assert _stored_job(repo, job_id)["pieces"][0]["attempts"] == []


def test_pick_excludes_executor_with_proof_failed(
    git_repo, fake_bin, monkeypatch, capsys
):
    repo = git_repo
    _write_prefs(repo, _STUB_PREFS.replace('models = ["m"]', 'models = ["m", "n"]'))
    fake_bin("stub", "print('x')\n")
    _only_bins(monkeypatch, {"stub"})
    job = records.new_job(repo, "Exclude", "do it", True, "one part")
    records.add_piece(job, "One", "code", "low")
    records.add_attempt(job, 1, {"executor": "stub:m", "role": "exec",
                                 "status": "proof_failed", "cost": "free"})
    records.save_job(repo, job)

    assert main(["pick", "--project", str(repo), "--job", job["id"],
                 "--piece", "1"]) == 0
    assert capsys.readouterr().out.splitlines()[0].strip() == "stub:n"


def test_accept_refuses_a_piece_whose_only_attempt_is_proof_failed(
    git_repo, fake_bin, monkeypatch, capsys
):
    repo = git_repo
    brief = _stub_repo(repo, fake_bin, monkeypatch)
    job_id = _spec_job(repo, proof=_py("import sys; sys.exit(1)"))
    assert main(_dispatch_argv(repo, job_id, brief)) == 1
    capsys.readouterr()

    assert main(["accept", "--project", str(repo), "--job", job_id,
                 "--piece", "1"]) == 1
    assert "nothing to accept" in capsys.readouterr().out
    assert not (repo / "out.py").exists()


def test_pick_takes_the_lowest_effort_then_rises_after_a_failure(
    git_repo, fake_bin, monkeypatch, capsys
):
    repo = git_repo
    prefs = _REVIEW_PREFS.replace('["m-high", "n"]', '["m-high", "m-low", "m-medium"]')
    _write_prefs(repo, prefs)
    fake_bin("freebie", "print('x')\n")
    fake_bin("strong", "print('x')\n")
    _only_bins(monkeypatch, {"freebie", "strong"})
    job = records.new_job(repo, "Effort", "do it", True, "one part")
    records.add_piece(job, "Change", "code", "low")
    records.save_job(repo, job)
    argv = ["pick", "--project", str(repo), "--job", job["id"], "--piece", "1"]

    assert main(argv) == 0
    assert capsys.readouterr().out.splitlines()[0].strip() == "freebie:m-low"

    stored = records.load_job(repo, job["id"])
    records.add_attempt(stored, 1, {"executor": "freebie:m-low", "role": "exec",
                                    "status": "failed", "cost": "free"})
    records.save_job(repo, stored)

    assert main(argv) == 0
    assert capsys.readouterr().out.splitlines()[0].strip() == "freebie:m-medium"


# --- the cost ladder in pick and dispatch -----------------------------------

_LADDER_PREFS = """
# No second chance: these tests pin the ladder, not the exploration.
explore_rate = 0
{extra}
[[tools]]
name = "freebie"
kind = "generic"
bin = "freebie"
models = ["m"]
run_cmd = ["freebie", "{{prompt}}"]

[[tools]]
name = "mine"
kind = "generic"
bin = "mine"
models = ["m"]
run_cmd = ["mine", "{{prompt}}"]

[[tools]]
name = "other"
kind = "generic"
bin = "other"
models = ["m"]
run_cmd = ["other", "{{prompt}}"]

[[tools]]
name = "cheapo"
kind = "generic"
bin = "cheapo"
models = ["m"]
run_cmd = ["cheapo", "{{prompt}}"]

[[tools]]
name = "strong"
kind = "generic"
bin = "strong"
models = ["s"]
run_cmd = ["strong", "{{prompt}}"]

[[models]]
match = "freebie:*"
cost = "free"

[[models]]
match = "mine:*"
cost = "included"

[[models]]
match = "other:*"
cost = "included"

[[models]]
match = "cheapo:*"
cost = "cheap"
{cap}

[[models]]
match = "strong:*"
cost = "paid"
"""

_LADDER_BINS = {"freebie", "mine", "other", "cheapo", "strong"}


def _ladder_repo(repo, fake_bin, monkeypatch, extra="", cap="", bins=None):
    _write_prefs(repo, _LADDER_PREFS.format(extra=extra, cap=cap))
    for name in sorted(bins or _LADDER_BINS):
        fake_bin(name, "print('x')\n")
    _only_bins(monkeypatch, set(bins or _LADDER_BINS))


def _ladder_job(repo, piece_type="code", attempts=()):
    job = records.new_job(repo, "Ladder", "do it", True, "parts")
    records.add_piece(job, "Bit", piece_type, "low")
    for attempt in attempts:
        records.add_attempt(job, 1, attempt)
    records.save_job(repo, job)
    return job


def _failed(executor, cost="free"):
    return {"executor": executor, "role": "exec", "status": "failed",
            "cost": cost}


def _pick_argv(repo, job, *extra):
    return ["pick", "--project", str(repo), "--job", job["id"], "--piece", "1",
            *extra]


def _spend_line(executor="cheapo:m", cost_usd=0.5, known=True, job="other-job",
                days_ago=0):
    from crew_chief import spend

    at = datetime.now(timezone.utc) - timedelta(days=days_ago)
    spend.record({"at": at.isoformat(), "project": "p", "job": job, "piece": 1,
                  "executor": executor, "tier": "cheap", "cost_usd": cost_usd,
                  "known": known})


def test_cli_pick_stays_free_then_climbs_to_included(
    git_repo, fake_bin, monkeypatch, capsys
):
    _ladder_repo(git_repo, fake_bin, monkeypatch)
    job = _ladder_job(git_repo)

    assert main(_pick_argv(git_repo, job)) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].strip() == "freebie:m"
    assert lines[1].endswith("; tier free")

    job = _ladder_job(git_repo, attempts=[_failed("freebie:m")])
    assert main(_pick_argv(git_repo, job)) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].strip() in ("mine:m", "other:m")
    assert "tier included; climbed: free exhausted" in lines[1]


def test_cli_pick_exec_work_does_not_reach_cheap_with_a_zero_budget(
    git_repo, fake_bin, monkeypatch, capsys
):
    # Review Focus 5, end to end: free and included are gone, budget is 0.
    _ladder_repo(git_repo, fake_bin, monkeypatch)
    job = _ladder_job(git_repo, attempts=[
        _failed("freebie:m"), _failed("mine:m", "included"),
        _failed("other:m", "included"),
    ])

    assert main(_pick_argv(git_repo, job)) == 1
    out = capsys.readouterr().out
    assert out.startswith("no executor available")
    assert "cheap skipped: budget US$ 0 reached" in out
    assert "split the piece" in out
    assert "run setup to allow cheap models (current budget US$ 0)" in out


_EXHAUSTED = lambda: [  # noqa: E731 - free and included gone on the piece
    _failed("freebie:m"), _failed("mine:m", "included"),
    _failed("other:m", "included"),
]


def test_cli_pick_cheap_unknown_cost_gets_one_probe_per_day(
    git_repo, fake_bin, monkeypatch, capsys
):
    # Ruling 4: unknown cost and no cap -> one probe attempt per UTC day.
    _ladder_repo(git_repo, fake_bin, monkeypatch,
                 extra="cheap_budget_usd_day = 5.0")
    job = _ladder_job(git_repo, attempts=_EXHAUSTED())

    assert main(_pick_argv(git_repo, job)) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].strip() == "cheapo:m"
    assert "cheap probe: unknown cost, one attempt today" in lines[1]

    # The probe ran today and the model is still unpriced: closed.
    _spend_line(cost_usd=0.0, known=False)
    assert main(_pick_argv(git_repo, job)) == 1
    out = capsys.readouterr().out
    assert "unknown cost and no cap_usd_day" in out
    assert "run setup" not in out

    # A probe of yesterday does not count today.
    from crew_chief import spend

    spend.spend_file().unlink()
    _spend_line(cost_usd=0.0, known=False, days_ago=1)
    assert main(_pick_argv(git_repo, job)) == 0
    assert "cheap probe" in capsys.readouterr().out.splitlines()[1]


def test_cli_pick_no_probe_with_a_zero_budget(
    git_repo, fake_bin, monkeypatch, capsys
):
    _ladder_repo(git_repo, fake_bin, monkeypatch)
    job = _ladder_job(git_repo, attempts=_EXHAUSTED())

    assert main(_pick_argv(git_repo, job)) == 1
    out = capsys.readouterr().out
    assert "cheap probe" not in out
    assert out.rstrip().endswith(
        "; run setup to allow cheap models (current budget US$ 0)"
    )


def test_cli_pick_hint_without_any_cheap_executor(
    git_repo, fake_bin, monkeypatch, capsys
):
    _ladder_repo(git_repo, fake_bin, monkeypatch, bins={"freebie", "mine"})
    job = _ladder_job(git_repo, attempts=[
        _failed("freebie:m"), _failed("mine:m", "included"),
    ])

    assert main(_pick_argv(git_repo, job)) == 1
    out = capsys.readouterr().out
    assert "split the piece" in out
    assert out.rstrip().endswith(
        "; run setup to allow cheap models (current budget US$ 0)"
    )


def test_cli_dispatch_rechecks_the_budget_for_a_cheap_executor(
    git_repo, fake_bin, monkeypatch, capsys
):
    _ladder_repo(git_repo, fake_bin, monkeypatch)
    brief = _write(git_repo.parent / "brief.md", "Create out.py.\n")
    job = _ladder_job(git_repo)

    argv = ["dispatch", "--project", str(git_repo), "--job", job["id"],
            "--piece", "1", "--executor", "cheapo:m", "--brief-file", str(brief)]
    assert main(argv) == 2
    err = capsys.readouterr().err
    assert "budget US$ 0 reached (cheap_budget_usd_day)" in err
    assert _stored_job(git_repo, job["id"])["pieces"][0]["attempts"] == []
    from crew_chief import spend

    assert not spend.spend_file().exists()

    # A free executor is never budget-checked.
    argv[argv.index("cheapo:m")] = "freebie:m"
    assert main(argv) != 2


def test_cli_dispatch_cheap_probe_is_allowed_once_then_refused(
    git_repo, fake_bin, monkeypatch, capsys
):
    _ladder_repo(git_repo, fake_bin, monkeypatch,
                 extra="cheap_budget_usd_day = 5.0")
    brief = _write(git_repo.parent / "brief.md", "Create out.py.\n")
    job = _ladder_job(git_repo)
    argv = ["dispatch", "--project", str(git_repo), "--job", job["id"],
            "--piece", "1", "--executor", "cheapo:m", "--brief-file", str(brief)]

    assert main(argv) != 2
    capsys.readouterr()
    from crew_chief import spend

    lines = spend.spend_file().read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and '"cheapo:m"' in lines[0]

    assert main(argv) == 2
    assert "unknown cost and no cap_usd_day" in capsys.readouterr().err


def test_cli_pick_cheap_unknown_cost_with_a_cap_is_allowed(
    git_repo, fake_bin, monkeypatch, capsys
):
    _ladder_repo(git_repo, fake_bin, monkeypatch,
                 extra="cheap_budget_usd_day = 5.0", cap="cap_usd_day = 1.0")
    job = _ladder_job(git_repo, attempts=[
        _failed("freebie:m"), _failed("mine:m", "included"),
        _failed("other:m", "included"),
    ])

    assert main(_pick_argv(git_repo, job)) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].strip() == "cheapo:m"
    assert "tier cheap; climbed: included exhausted" in lines[1]


def test_cli_pick_cheap_zero_cost_lines_still_count_as_unknown(
    git_repo, fake_bin, monkeypatch, capsys
):
    # OpenCode prints cost 0 for unpriced models: not proof of a known price.
    _ladder_repo(git_repo, fake_bin, monkeypatch,
                 extra="cheap_budget_usd_day = 5.0")
    _spend_line(cost_usd=0.0, known=True)
    job = _ladder_job(git_repo, attempts=[
        _failed("freebie:m"), _failed("mine:m", "included"),
        _failed("other:m", "included"),
    ])

    assert main(_pick_argv(git_repo, job)) == 1
    assert "unknown cost and no cap_usd_day" in capsys.readouterr().out


def test_cli_pick_cheap_with_a_known_price_needs_no_cap(
    git_repo, fake_bin, monkeypatch, capsys
):
    _ladder_repo(git_repo, fake_bin, monkeypatch,
                 extra="cheap_budget_usd_day = 5.0")
    _spend_line(cost_usd=0.5, known=True, days_ago=2)
    job = _ladder_job(git_repo, attempts=[
        _failed("freebie:m"), _failed("mine:m", "included"),
        _failed("other:m", "included"),
    ])

    assert main(_pick_argv(git_repo, job)) == 0
    assert capsys.readouterr().out.splitlines()[0].strip() == "cheapo:m"


def test_cli_pick_cheap_stops_when_the_day_budget_is_reached(
    git_repo, fake_bin, monkeypatch, capsys
):
    _ladder_repo(git_repo, fake_bin, monkeypatch,
                 extra="cheap_budget_usd_day = 1.0")
    _spend_line(cost_usd=0.6, known=True, job="a")
    _spend_line(executor="x:y", cost_usd=0.4, known=True, job="b")
    job = _ladder_job(git_repo, attempts=[
        _failed("freebie:m"), _failed("mine:m", "included"),
        _failed("other:m", "included"),
    ])

    assert main(_pick_argv(git_repo, job)) == 1
    out = capsys.readouterr().out
    assert "budget US$ 1 reached (cheap_budget_usd_day)" in out
    assert "(current budget US$ 1)" in out


def test_cli_pick_cheap_stops_when_the_job_budget_is_reached(
    git_repo, fake_bin, monkeypatch, capsys
):
    _ladder_repo(git_repo, fake_bin, monkeypatch,
                 extra="cheap_budget_usd_day = 10.0\ncheap_budget_usd_job = 0.5")
    job = _ladder_job(git_repo, attempts=[
        _failed("freebie:m"), _failed("mine:m", "included"),
        _failed("other:m", "included"),
    ])
    _spend_line(cost_usd=0.5, known=True, job=job["id"], days_ago=1)

    assert main(_pick_argv(git_repo, job)) == 1
    assert "budget US$ 0.5 reached (cheap_budget_usd_job)" in (
        capsys.readouterr().out
    )


def test_cli_pick_cheap_stops_at_the_cap_of_the_model(
    git_repo, fake_bin, monkeypatch, capsys
):
    _ladder_repo(git_repo, fake_bin, monkeypatch,
                 extra="cheap_budget_usd_day = 10.0", cap="cap_usd_day = 0.3")
    _spend_line(cost_usd=0.3, known=True)
    job = _ladder_job(git_repo, attempts=[
        _failed("freebie:m"), _failed("mine:m", "included"),
        _failed("other:m", "included"),
    ])

    assert main(_pick_argv(git_repo, job)) == 1
    assert "cap US$ 0.3 reached (cap_usd_day)" in capsys.readouterr().out


def test_cli_pick_ignores_a_malformed_spend_line_instead_of_blocking(
    git_repo, fake_bin, monkeypatch, capsys
):
    from crew_chief import spend

    _ladder_repo(git_repo, fake_bin, monkeypatch,
                 extra="cheap_budget_usd_day = 5.0", cap="cap_usd_day = 1.0")
    spend.spend_file().write_text(
        '{"at": "%s", "executor": "cheapo:m", "job": "z", "cost_usd": NaN}\n'
        % datetime.now(timezone.utc).isoformat(),
        encoding="utf-8",
    )
    job = _ladder_job(git_repo, attempts=[
        _failed("freebie:m"), _failed("mine:m", "included"),
        _failed("other:m", "included"),
    ])

    assert main(_pick_argv(git_repo, job)) == 0
    assert capsys.readouterr().out.splitlines()[0].strip() == "cheapo:m"


def test_cli_pick_thinking_after_two_free_failures_goes_to_the_next_tier(
    git_repo, fake_bin, monkeypatch, capsys
):
    _ladder_repo(git_repo, fake_bin, monkeypatch)
    job = _ladder_job(git_repo, "design",
                      attempts=[_failed("freebie:m"), _failed("freebie:m")])

    assert main(_pick_argv(git_repo, job)) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].strip() in ("mine:m", "other:m")
    assert "tier included" in lines[1]
    piece = _stored_job(git_repo, job["id"])["pieces"][0]
    assert piece["escalations"][0]["reason"] == "2 free failures"


def test_cli_pick_coordinator_cli_goes_last_unless_allowed(
    git_repo, fake_bin, monkeypatch, capsys
):
    _ladder_repo(git_repo, fake_bin, monkeypatch)
    attempts = [_failed("freebie:m")]
    monkeypatch.setenv("CREW_CHIEF_COORDINATOR", "mine")

    job = _ladder_job(git_repo, attempts=attempts)
    assert main(_pick_argv(git_repo, job)) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].strip() == "other:m"
    assert "coordinator mine kept last" in lines[1]

    job = _ladder_job(git_repo, attempts=attempts)
    assert main(_pick_argv(git_repo, job, "--allow-own-cli")) == 0
    assert capsys.readouterr().out.splitlines()[0].strip() == "mine:m"

    # The flag beats the environment.
    job = _ladder_job(git_repo, attempts=attempts)
    assert main(_pick_argv(git_repo, job, "--coordinator", "other")) == 0
    assert capsys.readouterr().out.splitlines()[0].strip() == "mine:m"

    monkeypatch.delenv("CREW_CHIEF_COORDINATOR")
    job = _ladder_job(git_repo, attempts=attempts)
    assert main(_pick_argv(git_repo, job)) == 0
    assert capsys.readouterr().out.splitlines()[0].strip() == "mine:m"


_EFFORT_PREFS = """
[[tools]]
name = "eff"
kind = "generic"
bin = "eff"
effort = true
models = ["m"]
run_cmd = ["eff", "{prompt}"]

[[models]]
match = "eff:*"
cost = "free"
"""


def _effort_repo(repo, fake_bin, monkeypatch):
    _write_prefs(repo, _EFFORT_PREFS)
    fake_bin("eff", _STUB)
    _only_bins(monkeypatch, {"eff"})


def test_cli_pick_prints_effort_line(git_repo, fake_bin, monkeypatch, capsys):
    _effort_repo(git_repo, fake_bin, monkeypatch)
    job = _ladder_job(git_repo)

    assert main(_pick_argv(git_repo, job)) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].strip() == "eff:m"
    assert lines[2] == "effort: low"
    assert len(lines) == 3

    assert main(_pick_argv(git_repo, job, "--json")) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["executor"] == "eff:m"
    assert payload["effort"] == "low"


def test_cli_pick_effort_rises_after_a_failure_of_the_family(
    git_repo, fake_bin, monkeypatch, capsys
):
    # The tool takes an effort flag and has two models; the first one failed
    # once on the piece, so the second starts at medium: the family is the tool
    # model, so use two efforts of one model instead.
    _write_prefs(git_repo, _EFFORT_PREFS.replace('models = ["m"]',
                                                 'models = ["m-low", "m-medium"]'))
    fake_bin("eff", _STUB)
    _only_bins(monkeypatch, {"eff"})
    job = _ladder_job(git_repo, attempts=[_failed("eff:m-low")])

    assert main(_pick_argv(git_repo, job)) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].strip() == "eff:m-medium"
    assert "effort medium (low failed 1×)" in lines[1]
    assert lines[2] == "effort: medium"


def test_cli_pick_has_no_effort_line_for_a_tool_without_effort(
    git_repo, fake_bin, monkeypatch, capsys
):
    _ladder_repo(git_repo, fake_bin, monkeypatch)
    job = _ladder_job(git_repo)

    assert main(_pick_argv(git_repo, job)) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 2
    assert not any(line.startswith("effort:") for line in lines)

    assert main(_pick_argv(git_repo, job, "--json")) == 0
    assert json.loads(capsys.readouterr().out)["effort"] is None


def _spy_dispatch(monkeypatch):
    from crew_chief import cli

    seen = []
    real = cli.run_attempt

    def spy(*args, **kwargs):
        seen.append(kwargs.get("effort"))
        return real(*args, **kwargs)

    monkeypatch.setattr(cli, "run_attempt", spy)
    return seen


def _effort_dispatch_argv(repo, job, brief, executor, *extra):
    return ["dispatch", "--project", str(repo), "--job", job["id"],
            "--piece", "1", "--executor", executor, "--brief-file", str(brief),
            *extra]


def test_dispatch_defaults_to_picked_effort(
    git_repo, fake_bin, monkeypatch, capsys
):
    _write_prefs(git_repo, _EFFORT_PREFS.replace('models = ["m"]',
                                                 'models = ["m-low", "m-medium"]'))
    fake_bin("eff", _STUB)
    _only_bins(monkeypatch, {"eff"})
    brief = _write(git_repo.parent / "brief.md", "Create out.py.\n")
    seen = _spy_dispatch(monkeypatch)

    job = _ladder_job(git_repo)
    main(_effort_dispatch_argv(git_repo, job, brief, "eff:m-low"))
    job = _ladder_job(git_repo, attempts=[_failed("eff:m-low")])
    main(_effort_dispatch_argv(git_repo, job, brief, "eff:m-medium"))
    job = _ladder_job(git_repo, attempts=[_failed("eff:m-low")])
    main(_effort_dispatch_argv(git_repo, job, brief, "eff:m-medium",
                               "--effort", "high"))

    assert seen == ["low", "medium", "high"]


def test_antigravity_model_effort_suffix_is_the_effort(
    git_repo, fake_bin, monkeypatch, capsys
):
    # gemini-x-medium carries its effort: no ladder "low" that would conflict.
    _write_prefs(git_repo, """
[[tools]]
name = "antigravity"
kind = "antigravity"
bin = "agy"
models = ["gemini-x-medium"]

[[models]]
match = "antigravity:*"
cost = "free"
""")
    fake_bin("agy", _STUB)
    _only_bins(monkeypatch, {"agy"})
    brief = _write(git_repo.parent / "brief.md", "Create out.py.\n")
    seen = _spy_dispatch(monkeypatch)
    job = _ladder_job(git_repo)

    assert main(_pick_argv(git_repo, job, "--json")) == 0
    assert json.loads(capsys.readouterr().out)["effort"] == "medium"
    main(_effort_dispatch_argv(git_repo, job, brief, "antigravity:gemini-x-medium",
                               "--effort", "low"))
    assert seen == ["medium"]


def test_dispatch_default_effort_is_none_for_a_tool_without_effort(
    git_repo, fake_bin, monkeypatch, capsys
):
    brief = _stub_repo(git_repo, fake_bin, monkeypatch)
    seen = _spy_dispatch(monkeypatch)
    job = _ladder_job(git_repo)

    main(_effort_dispatch_argv(git_repo, job, brief, "stub:m"))

    assert seen == [None]


# --- T040: CLI pick with team allowlist (US10, FR-037) -----------------------

def test_cli_pick_enforces_team_executors_allowlist(
    git_repo, fake_bin, monkeypatch, capsys
):
    prefs_text = """
[[tools]]
name = "multimodel"
kind = "generic"
bin = "multimodel"
models = ["model-a", "model-b"]
run_cmd = ["multimodel", "{prompt}"]

[[models]]
match = "multimodel:*"
cost = "free"

[team]
executors = ["multimodel:model-b"]
"""
    _write_prefs(git_repo, prefs_text)
    fake_bin("multimodel", _STUB)
    _only_bins(monkeypatch, {"multimodel"})

    job = _ladder_job(git_repo)
    assert main(_pick_argv(git_repo, job)) == 0
    chosen = capsys.readouterr().out.splitlines()[0].strip()
    assert chosen == "multimodel:model-b"


def test_cli_pick_team_executors_fails_when_none_eligible(
    git_repo, fake_bin, monkeypatch, capsys
):
    prefs_text = """
[[tools]]
name = "multimodel"
kind = "generic"
bin = "multimodel"
models = ["model-a", "model-b"]
run_cmd = ["multimodel", "{prompt}"]

[[models]]
match = "multimodel:*"
cost = "free"

[team]
executors = ["multimodel:nonexistent"]
"""
    _write_prefs(git_repo, prefs_text)
    fake_bin("multimodel", _STUB)
    _only_bins(monkeypatch, {"multimodel"})

    job = _ladder_job(git_repo)
    assert main(_pick_argv(git_repo, job)) == 1
    err = capsys.readouterr().out
    assert "allowlist" in err.lower()
    assert "multimodel:nonexistent" in err
    assert "multimodel:model-a" in err


def test_cli_pick_team_omitted_backwards_compatible(
    git_repo, fake_bin, monkeypatch, capsys
):
    prefs_text = """
[[tools]]
name = "multimodel"
kind = "generic"
bin = "multimodel"
models = ["model-a", "model-b"]
run_cmd = ["multimodel", "{prompt}"]

[[models]]
match = "multimodel:*"
cost = "free"
"""
    _write_prefs(git_repo, prefs_text)
    fake_bin("multimodel", _STUB)
    _only_bins(monkeypatch, {"multimodel"})

    job = _ladder_job(git_repo)
    assert main(_pick_argv(git_repo, job)) == 0
    chosen = capsys.readouterr().out.splitlines()[0].strip()
    assert chosen in ("multimodel:model-a", "multimodel:model-b")


def test_cli_pick_team_reviewers_independence(
    git_repo, fake_bin, monkeypatch, capsys
):
    prefs_text = """
explore_rate = 0

[[tools]]
name = "multimodel"
kind = "generic"
bin = "multimodel"
models = ["model-a", "model-b", "model-c"]
run_cmd = ["multimodel", "{prompt}"]

[[models]]
match = "multimodel:*"
cost = "free"

[team]
executors = ["multimodel:model-c"]
reviewers = ["multimodel:model-b"]
"""
    _write_prefs(git_repo, prefs_text)
    fake_bin("multimodel", _STUB)
    _only_bins(monkeypatch, {"multimodel"})

    # Exec pick respects executors allowlist (picks model-c, not model-a)
    job = _ladder_job(git_repo)
    assert main(_pick_argv(git_repo, job)) == 0
    assert capsys.readouterr().out.splitlines()[0].strip() == "multimodel:model-c"

    # Review pick respects reviewers allowlist (picks model-b, not model-a)
    assert main(_pick_argv(git_repo, job, "--review")) == 0
    assert capsys.readouterr().out.splitlines()[0].strip() == "multimodel:model-b"

    # Review pick fails when none eligible in reviewers allowlist
    prefs_bad_reviewer = """
explore_rate = 0

[[tools]]
name = "multimodel"
kind = "generic"
bin = "multimodel"
models = ["model-a", "model-b", "model-c"]
run_cmd = ["multimodel", "{prompt}"]

[[models]]
match = "multimodel:*"
cost = "free"

[team]
executors = ["multimodel:model-c"]
reviewers = ["multimodel:nonexistent"]
"""
    _write_prefs(git_repo, prefs_bad_reviewer)
    job_bad = _ladder_job(git_repo)
    assert main(_pick_argv(git_repo, job_bad, "--review")) == 1
    err = capsys.readouterr().out
    assert "allowlist" in err.lower()
    assert "multimodel:nonexistent" in err

    # When reviewers is omitted, review pick is independent of executors allowlist
    prefs_no_reviewers = """
explore_rate = 0

[[tools]]
name = "multimodel"
kind = "generic"
bin = "multimodel"
models = ["model-a", "model-b", "model-c"]
run_cmd = ["multimodel", "{prompt}"]

[[models]]
match = "multimodel:*"
cost = "free"

[team]
executors = ["multimodel:model-c"]
"""
    _write_prefs(git_repo, prefs_no_reviewers)
    job2 = _ladder_job(git_repo)
    assert main(_pick_argv(git_repo, job2, "--review")) == 0
    rev_choice = capsys.readouterr().out.splitlines()[0].strip()
    assert rev_choice in ("multimodel:model-a", "multimodel:model-b")


def test_cli_pick_team_reviewers_independence_explore_regression(
    git_repo, fake_bin, monkeypatch, capsys
):
    """(Known issue #12 regression) Reviewer pick determinism when explore_rate is pinned to 0."""
    prefs_no_reviewers = """
explore_rate = 0

[[tools]]
name = "multimodel"
kind = "generic"
bin = "multimodel"
models = ["model-a", "model-b", "model-c"]
run_cmd = ["multimodel", "{prompt}"]

[[models]]
match = "multimodel:*"
cost = "free"

[team]
executors = ["multimodel:model-c"]
"""
    _write_prefs(git_repo, prefs_no_reviewers)
    fake_bin("multimodel", _STUB)
    _only_bins(monkeypatch, {"multimodel"})

    # Even if RNG generates sequence that would explore under default explore_rate (< 0.1),
    # explore_rate = 0 ensures exploration is disabled and selection is deterministic.
    class _MockRng:
        def __init__(self):
            self._seq = iter([0.0, 0.99])

        def random(self):
            return next(self._seq, 0.5)

    import crew_chief.cli_dispatch
    monkeypatch.setattr(crew_chief.cli_dispatch.random, "Random", _MockRng)

    job = _ladder_job(git_repo)
    assert main(_pick_argv(git_repo, job, "--review")) == 0
    rev_choice = capsys.readouterr().out.splitlines()[0].strip()
    assert rev_choice in ("multimodel:model-a", "multimodel:model-b")


# --- T036: acc interview --json (US9, FR-036) --------------------------------


def test_piece_start_native_refuses_when_interview_pending(git_repo, capsys):
    """(P1-6) piece start --native must enforce interview gate before reservation."""
    from datetime import datetime, timezone
    from crew_chief import records

    job = records.new_job(git_repo, "Test Interview Gate", "req", False, "test")
    job["state"] = "interview"
    job["interview"] = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "pending": ["coordinator"],
        "answers": {},
    }
    records.add_piece(job, "p1", "code", "low")
    records.save_job(git_repo, job)

    exit_code = main([
        "piece", "start",
        "--project", str(git_repo),
        "--job", job["id"],
        "--piece", "1",
        "--native",
        "--coordinator", "claude",
        "--model", "sonnet-3.7",
    ])
    assert exit_code != 0
    err = capsys.readouterr().err
    assert "interview" in err.lower()
    # Confirm NO attempt was created
    refreshed = records.load_job(git_repo, job["id"])
    assert len(refreshed["pieces"][0].get("attempts") or []) == 0


def test_piece_start_native_refuses_when_spec_unreviewed(git_repo, capsys):
    """(P1-6) piece start --native must enforce SDD gate before reservation."""
    from crew_chief import records
    from crew_chief.sdd_gate import enter_spec_phase

    job = records.new_job(git_repo, "Test SDD Gate", "req", False, "test")
    job["coordinator"] = "claude"
    job["project"] = str(git_repo)
    enter_spec_phase(job, title="Spec Gate Test")
    records.add_piece(job, "p1", "code", "low", ref="T001")
    records.save_job(git_repo, job)

    exit_code = main([
        "piece", "start",
        "--project", str(git_repo),
        "--job", job["id"],
        "--piece", "1",
        "--native",
        "--coordinator", "claude",
        "--model", "sonnet-3.7",
    ])
    assert exit_code != 0
    err = capsys.readouterr().err
    assert "spec tasks" in err.lower()
    # Confirm NO attempt was created
    refreshed = records.load_job(git_repo, job["id"])
    assert len(refreshed["pieces"][0].get("attempts") or []) == 0


def test_cli_close_honours_persisted_end_mode(git_repo, capsys):
    """(P2) close honours persisted job end_mode='pr_review' when --end-mode is omitted."""
    from unittest.mock import patch
    from crew_chief import records

    work = git_repo
    # Write preferences with team reviewers
    prefs_dir = work / "docs" / "crew-chief"
    prefs_dir.mkdir(parents=True, exist_ok=True)
    (prefs_dir / "preferences.toml").write_text("""
[team]
coordinators = ["claude:sonnet"]
executors = ["claude:opus"]
reviewers = ["claude:haiku"]
""")

    job = records.new_job(work, "Review Test", "implement feature", True, "requested")
    job["job_branch"] = "crew/job-test"
    job["base_branch"] = "main"
    job["end_mode"] = "pr_review"
    records.add_piece(job, "Feature Piece", "code", "low")
    attempt = {
        "n": 1,
        "branch": "crew/job-test-p1",
        "role": "exec",
        "status": "ok",
        "executor": "claude:opus",
    }
    records.add_attempt(job, 1, attempt)
    records.mark_accepted(job, 1, attempt)
    records.save_job(work, job)

    with patch("crew_chief.cli.create_pr", return_value="https://github.com/test/repo/pull/123"), \
         patch("crew_chief.cli.create_pr_review_comment") as mock_review, \
         patch("crew_chief.cli._run_review_model", return_value="LGTM"):
        exit_code = main(["close", "--project", str(work), "--job", job["id"], "--json"])
        assert exit_code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["state"] == "pr_review_posted"
        mock_review.assert_called_once()


