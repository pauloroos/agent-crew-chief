"""Tests for dispatch: one attempt end to end.

Each test drives one attempt with a stub CLI installed by ``fake_bin``: the
stub is a ``generic`` tool whose command is ``[stub, "{prompt}"]``. The stub
runs with the worktree as its working directory, so ``out.py`` lands inside
the worktree and ``../escaped.txt`` lands in the ``.crew`` sibling area.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from crew_chief import records
from crew_chief.config import Prefs
from crew_chief.dispatch import dispatch
from crew_chief.tools import Executor
from crew_chief.worktree import create_worktree


def _prefs(**overrides) -> Prefs:
    values = dict(
        tools=[],
        models=[],
        strong_order=[],
        explore_rate=0.1,
        forbid_training=False,
        sensitive_paths=[],
        issue_tracker="none",
        dispatch_timeout_s=900,
        review_default="",
    )
    values.update(overrides)
    return Prefs(**values)


def _tool(name: str = "stub") -> dict:
    return {
        "name": name,
        "kind": "generic",
        "bin": name,
        "run_cmd": [name, "{prompt}"],
        "models": ["m"],
    }


def _executor(name: str = "stub") -> Executor:
    return Executor(
        id=f"{name}:m", tool=_tool(name), model="m", cost="free", trains=False
    )


def _seed_job(repo) -> dict:
    """A job with one piece, saved so ``dispatch`` can load it."""
    job = records.new_job(repo, "Wire the dispatch", "do it", True, "two parts")
    records.add_piece(job, "Run one attempt", "code", "low")
    records.save_job(repo, job)
    return job


def _head(repo) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


def _branch_exists(repo, branch: str) -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", branch],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.returncode == 0


def _stored_attempt(repo, job_id: str, piece: int = 1, index: int = 0) -> dict:
    path = records.jobs_dir(repo) / f"{job_id}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    piece_data = next(p for p in data["pieces"] if p["n"] == piece)
    return piece_data["attempts"][index]


# --- brief refusal ---------------------------------------------------------


def test_refused_brief_creates_no_worktree(git_repo, fake_bin):
    fake_bin("stub", "print('must not run')\n")
    job = _seed_job(git_repo)
    secret = "sk-" + "a" * 24

    attempt = dispatch(
        git_repo, job["id"], 1, _executor(), f"use the key {secret}", _prefs()
    )

    assert attempt["status"] == "brief_refused"
    assert "openai-key" in attempt["note"]
    assert secret not in attempt["note"]
    assert attempt["diffstat"] == []
    assert not (git_repo.parent / f"{git_repo.name}.crew").exists()
    assert _stored_attempt(git_repo, job["id"])["status"] == "brief_refused"


# --- success ---------------------------------------------------------------


def test_successful_attempt_records_diffstat_and_branch(git_repo, fake_bin):
    fake_bin(
        "stub",
        "from pathlib import Path\n"
        "Path('out.py').write_text('x = 1\\n', encoding='utf-8')\n"
        "print('all done')\n",
    )
    job = _seed_job(git_repo)

    attempt = dispatch(
        git_repo, job["id"], 1, _executor(), "Write out.py\n", _prefs()
    )

    assert attempt["status"] == "ok"
    assert [row["file"] for row in attempt["diffstat"]] == ["out.py"]
    assert attempt["branch"]
    assert _branch_exists(git_repo, attempt["branch"])
    assert attempt["worktree"]
    assert attempt["duration_s"] == round(attempt["duration_s"], 1)
    assert attempt["executor"] == "stub:m"
    assert attempt["role"] == "exec"
    assert attempt["cost"] == "free"
    assert "all done" in attempt["note"]
    assert attempt["reported_model"] is None
    assert attempt["model_mismatch"] is False


def test_second_attempt_is_numbered_two(git_repo, fake_bin):
    fake_bin("stub", "print('ok')\n")
    job = _seed_job(git_repo)
    prefs = _prefs()

    dispatch(git_repo, job["id"], 1, _executor(), "first\n", prefs)
    attempt = dispatch(git_repo, job["id"], 1, _executor(), "second\n", prefs)

    assert attempt["branch"].endswith("-2")
    assert _stored_attempt(git_repo, job["id"], 1, 1)["branch"] == attempt["branch"]


# --- containment -----------------------------------------------------------


def test_outside_write_is_rejected(git_repo, fake_bin):
    fake_bin(
        "stub",
        "from pathlib import Path\n"
        "Path('../escaped.txt').write_text('boom\\n', encoding='utf-8')\n",
    )
    job = _seed_job(git_repo)

    attempt = dispatch(git_repo, job["id"], 1, _executor(), "escape\n", _prefs())

    assert attempt["status"] == "outside_write"
    assert any("escaped.txt" in entry for entry in attempt["outside_writes"])
    assert _stored_attempt(git_repo, job["id"])["status"] == "outside_write"


# --- timeout ---------------------------------------------------------------


def test_timeout_kills_and_records(git_repo, fake_bin):
    fake_bin("stub", "import time\ntime.sleep(30)\n")
    job = _seed_job(git_repo)

    start = time.monotonic()
    attempt = dispatch(
        git_repo, job["id"], 1, _executor(), "hang\n", _prefs(), timeout_s=2
    )
    elapsed = time.monotonic() - start

    assert attempt["status"] == "timeout"
    assert elapsed < 15
    assert attempt["worktree"]


# --- quota and failure -----------------------------------------------------


def test_quota_detected(git_repo, fake_bin):
    fake_bin(
        "stub",
        "import sys\nprint('Error 429 Too Many Requests')\nsys.exit(1)\n",
    )
    job = _seed_job(git_repo)

    attempt = dispatch(git_repo, job["id"], 1, _executor(), "go\n", _prefs())

    assert attempt["status"] == "quota"
    assert "429" in attempt["note"]


def test_plain_failure_is_failed(git_repo, fake_bin):
    fake_bin("stub", "import sys\nprint('nope')\nsys.exit(3)\n")
    job = _seed_job(git_repo)

    attempt = dispatch(git_repo, job["id"], 1, _executor(), "go\n", _prefs())

    assert attempt["status"] == "failed"


# --- encoding --------------------------------------------------------------


def test_accented_output_roundtrip(git_repo, fake_bin):
    fake_bin(
        "stub",
        "import sys\n"
        "sys.stdout.buffer.write('revisão concluída ✓\\n'.encode('utf-8'))\n"
        "sys.stdout.buffer.flush()\n",
    )
    job = _seed_job(git_repo)
    text = "brief com acentuação: revisão\n"

    attempt = dispatch(git_repo, job["id"], 1, _executor(), text, _prefs())

    assert "revisão concluída ✓" in attempt["note"]
    assert _stored_attempt(git_repo, job["id"])["note"] == attempt["note"]


# --- output file and environment -------------------------------------------


def test_output_file_used_when_stdout_is_empty(git_repo, fake_bin):
    fake_bin(
        "stub",
        "import sys\n"
        "from pathlib import Path\n"
        "Path(sys.argv[1]).write_text('answer from file', encoding='utf-8')\n",
    )
    job = _seed_job(git_repo)
    tool = _tool()
    tool["run_cmd"] = ["stub", "{output_file}", "{prompt}"]
    executor = Executor(id="stub:m", tool=tool, model="m", cost="free", trains=False)

    attempt = dispatch(git_repo, job["id"], 1, executor, "go\n", _prefs())

    assert attempt["status"] == "ok"
    assert attempt["note"] == "answer from file"


def test_whitespace_stdout_beats_output_file(git_repo, fake_bin):
    fake_bin(
        "stub",
        "import sys\n"
        "from pathlib import Path\n"
        "Path(sys.argv[1]).write_text('answer from file', encoding='utf-8')\n"
        "sys.stdout.buffer.write(b'\\n')\n"
        "sys.stdout.buffer.flush()\n",
    )
    job = _seed_job(git_repo)
    tool = _tool()
    tool["run_cmd"] = ["stub", "{output_file}", "{prompt}"]
    executor = Executor(id="stub:m", tool=tool, model="m", cost="free", trains=False)

    attempt = dispatch(git_repo, job["id"], 1, executor, "go\n", _prefs())

    assert attempt["status"] == "ok"
    assert attempt["note"] == "\n"


def test_missing_env_fails_before_starting(git_repo, fake_bin, monkeypatch):
    fake_bin(
        "stub",
        "from pathlib import Path\nPath('ran.txt').write_text('x', encoding='utf-8')\n",
    )
    monkeypatch.delenv("CREWCHIEF_TEST_ABSENT", raising=False)
    job = _seed_job(git_repo)
    tool = _tool()
    tool["env"] = ["CREWCHIEF_TEST_ABSENT"]
    executor = Executor(id="stub:m", tool=tool, model="m", cost="free", trains=False)

    attempt = dispatch(git_repo, job["id"], 1, executor, "go\n", _prefs())

    assert attempt["status"] == "failed"
    assert attempt["note"] == "missing env: CREWCHIEF_TEST_ABSENT"
    assert not (Path(attempt["worktree"]) / "ran.txt").exists()


# --- review ----------------------------------------------------------------


def test_review_runs_in_target_without_commit(git_repo, fake_bin):
    fake_bin("stub", "print('review: one issue')\n")
    job = _seed_job(git_repo)
    target, _branch = create_worktree(git_repo, job["id"], 1, 1)
    head_before = _head(target)

    attempt = dispatch(
        git_repo,
        job["id"],
        1,
        _executor(),
        "review it\n",
        _prefs(),
        role="review",
        review_target=target,
    )

    assert attempt["status"] == "ok"
    assert attempt["role"] == "review"
    assert attempt["branch"] is None
    assert attempt["worktree"] is None
    assert attempt["diffstat"] == []
    assert "review: one issue" in attempt["note"]
    assert not (target / ".crew-chief-brief.md").exists()
    assert _head(target) == head_before


def test_review_requires_review_target(git_repo, fake_bin):
    fake_bin("stub", "print('x')\n")
    job = _seed_job(git_repo)

    with pytest.raises(ValueError):
        dispatch(
            git_repo, job["id"], 1, _executor(), "review\n", _prefs(), role="review"
        )

    assert not (git_repo.parent / f"{git_repo.name}.crew").exists()
