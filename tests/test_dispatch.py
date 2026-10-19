"""Tests for dispatch: one attempt end to end.

Each test drives one attempt with a stub CLI installed by ``fake_bin``: the
stub is a ``generic`` tool whose command is ``[stub, "{prompt}"]``. The stub
runs with the worktree as its working directory, so ``out.py`` lands inside
the worktree and ``../escaped.txt`` lands in the ``.crew`` sibling area.

The home directory is redirected to an empty folder for every test: the
containment snapshot compares the top level of ``Path.home()``, and on a
developer machine other programs (an agent CLI running the test suite, for
one) rewrite files there at any moment, which made ``ok`` attempts come back
as ``outside_write`` now and then.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from crew_chief import records
from crew_chief.config import Prefs
from crew_chief.dispatch import dispatch
from crew_chief.tools import Executor
from crew_chief.worktree import create_worktree


@pytest.fixture(autouse=True)
def home_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``Path.home()`` to an empty temporary directory."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


#: Stub prelude that changes the worktree, so a clean exit is ``ok`` and not
#: ``no_change``.
_WRITES = (
    "from pathlib import Path\n"
    "Path('out.py').write_text('x = 1\\n', encoding='utf-8')\n"
)


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


def test_executor_stdin_is_closed(git_repo, fake_bin, monkeypatch):
    # An executor left waiting on the coordinator's stdin hangs until the
    # timeout (`opencode run` does this in the background).
    fake_bin("stub", _WRITES + "print('done')\n")
    job = _seed_job(git_repo)
    seen = {}
    real_popen = subprocess.Popen

    def spy(cmd, *args, **kwargs):
        # git runs through Popen too; keep only the executor's own call.
        if "stub" in str(cmd[0]):
            seen["stdin"] = kwargs.get("stdin")
        return real_popen(cmd, *args, **kwargs)

    monkeypatch.setattr("crew_chief.dispatch.subprocess.Popen", spy)

    attempt = dispatch(git_repo, job["id"], 1, _executor(), "Go\n", _prefs())

    assert attempt["status"] == "ok"
    assert seen["stdin"] is subprocess.DEVNULL


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
        _WRITES + "import sys\n"
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
        _WRITES + "import sys\n"
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


# --- containment preferences -----------------------------------------------


def test_home_entries_from_preferences_are_not_outside_write(
    git_repo, fake_bin, home_dir
):
    fake_bin(
        "stub",
        "import os\n"
        "from pathlib import Path\n"
        "home = Path(os.environ['USERPROFILE' if os.name == 'nt' else 'HOME'])\n"
        "(home / '.claude.json').write_text('{}', encoding='utf-8')\n"
        "(home / '.my-agent').mkdir()\n"
        "Path('out.py').write_text('x = 1\\n', encoding='utf-8')\n"
        "print('ok')\n",
    )
    job = _seed_job(git_repo)

    attempt = dispatch(
        git_repo,
        job["id"],
        1,
        _executor(),
        "go\n",
        _prefs(ignore_home_entries=[".my-agent"]),
    )

    assert attempt["status"] == "ok", attempt["outside_writes"]


def test_parallel_attempts_do_not_flag_each_other(git_repo, fake_bin):
    fake_bin("stub", "print('ok')\n")
    job = _seed_job(git_repo)
    other, _ = create_worktree(git_repo, job["id"], 1, 99)
    fake_bin(
        "stub",
        "from pathlib import Path\n"
        f"Path(r'{other}', 'parallel.py').write_text('x', encoding='utf-8')\n"
        f"Path(r'{other.parent}', '{job['id']}-2-1').mkdir()\n"
        "Path('out.py').write_text('x = 1\\n', encoding='utf-8')\n"
        "print('ok')\n",
    )

    attempt = dispatch(git_repo, job["id"], 1, _executor(), "go\n", _prefs())

    assert attempt["status"] == "ok", attempt["outside_writes"]


# --- metrics ---------------------------------------------------------------


def test_generic_attempt_estimates_tokens_and_has_no_steps(git_repo, fake_bin):
    fake_bin("stub", "print('x' * 40)\n")
    job = _seed_job(git_repo)
    brief = "b" * 80

    attempt = dispatch(git_repo, job["id"], 1, _executor(), brief, _prefs())

    assert attempt["steps"] is None
    assert attempt["tokens_estimated"] is True
    assert attempt["tokens_in"] == 20  # 80 brief chars / 4
    assert attempt["tokens_out"] == 11  # 41 output chars / 4, rounded up
    assert attempt["tokens_total"] == 31
    stored = _stored_attempt(git_repo, job["id"])
    assert stored["tokens_total"] == 31 and stored["steps"] is None


def test_opencode_attempt_records_steps_tokens_and_readable_note(
    git_repo, fake_bin
):
    events = [
        {"type": "step_start", "part": {"type": "step-start"}},
        {"type": "step_finish", "part": {"type": "step-finish", "tokens": {
            "input": 100, "output": 20, "reasoning": 5,
            "cache": {"read": 1000, "write": 0}}}},
        {"type": "step_start", "part": {"type": "step-start"}},
        {"type": "text", "part": {"type": "text", "text": "Feito."}},
    ]
    lines = "".join(json.dumps(event) + "\n" for event in events)
    fake_bin(
        "ocstub",
        _WRITES + "import sys\n"
        "assert sys.argv[1:5] == ['run', '--standalone', '--format', 'json'], sys.argv\n"
        f"sys.stdout.write({lines!r})\n",
    )
    job = _seed_job(git_repo)
    tool = {"name": "ocstub", "kind": "opencode", "bin": "ocstub"}
    executor = Executor(
        id="ocstub:m", tool=tool, model="m", cost="free", trains=False
    )

    attempt = dispatch(git_repo, job["id"], 1, executor, "go\n", _prefs())

    assert attempt["status"] == "ok", attempt["note"]
    assert attempt["steps"] == 2
    assert attempt["tokens_in"] == 1100
    assert attempt["tokens_out"] == 25
    assert attempt["tokens_total"] == 1125
    assert attempt["tokens_estimated"] is False
    assert attempt["note"] == "Feito."


def test_refused_brief_has_no_metrics(git_repo, fake_bin):
    fake_bin("stub", "print('must not run')\n")
    job = _seed_job(git_repo)

    attempt = dispatch(
        git_repo, job["id"], 1, _executor(), "sk-" + "a" * 24, _prefs()
    )

    assert attempt["steps"] is None
    assert attempt["tokens_total"] is None
    assert attempt["tokens_estimated"] is False


# --- leftovers and worktree failures ---------------------------------------


def test_leftover_worktree_from_a_lost_attempt_is_skipped(git_repo, fake_bin):
    fake_bin("stub", _WRITES + "print('ok')\n")
    job = _seed_job(git_repo)
    create_worktree(git_repo, job["id"], 1, 1)  # attempt 1 lost from the JSON

    attempt = dispatch(git_repo, job["id"], 1, _executor(), "go\n", _prefs())

    assert attempt["status"] == "ok", attempt["note"]
    assert attempt["branch"] == f"crew/{job['id']}-1-2"
    assert attempt["n"] == 2


def test_worktree_creation_failure_is_recorded_as_failed(
    git_repo, fake_bin, monkeypatch
):
    import crew_chief.dispatch as dispatch_module

    fake_bin("stub", "print('must not run')\n")
    job = _seed_job(git_repo)

    def boom(*args, **kwargs):
        raise RuntimeError("git worktree add failed in somewhere: fatal: nope")

    monkeypatch.setattr(dispatch_module, "create_worktree", boom)

    attempt = dispatch(git_repo, job["id"], 1, _executor(), "go\n", _prefs())

    assert attempt["status"] == "failed"
    assert "cannot create worktree" in attempt["note"]
    assert "fatal: nope" in attempt["note"]
    assert _stored_attempt(git_repo, job["id"])["status"] == "failed"


def test_broken_worktree_after_the_run_is_recorded_as_failed(
    git_repo, fake_bin, monkeypatch
):
    import crew_chief.dispatch as dispatch_module

    fake_bin("stub", "print('ran')\n")
    job = _seed_job(git_repo)

    def boom(*args, **kwargs):
        raise RuntimeError(
            "git merge-base HEAD HEAD failed in x: fatal: not a git repository"
        )

    monkeypatch.setattr(dispatch_module, "diffstat", boom)

    attempt = dispatch(git_repo, job["id"], 1, _executor(), "go\n", _prefs())

    assert attempt["status"] == "failed"
    assert "not a git repository" in attempt["note"]
    assert "ran" in attempt["note"]
    assert _stored_attempt(git_repo, job["id"])["status"] == "failed"


# --- prompt ----------------------------------------------------------------


def test_prompt_names_the_absolute_brief_and_worktree(git_repo, fake_bin):
    # Some agent tools hide dotfiles from their file search, so a relative
    # ".crew-chief-brief.md in the current directory" was never found.
    fake_bin(
        "stub",
        "import sys\n"
        "from pathlib import Path\n"
        "Path('out.py').write_text('x = 1\\n', encoding='utf-8')\n"
        "print(sys.argv[1])\n",
    )
    job = _seed_job(git_repo)

    attempt = dispatch(git_repo, job["id"], 1, _executor(), "go\n", _prefs())

    worktree = Path(attempt["worktree"]).resolve()
    brief = worktree / ".crew-chief-brief.md"
    assert attempt["note"].strip().startswith(
        f"Read the file {brief} and carry out exactly what it asks. "
        f"Work only inside {worktree}."
    )


def test_review_prompt_names_the_target_and_forbids_changes(git_repo, fake_bin):
    fake_bin("stub", "import sys\nprint(sys.argv[1])\n")
    job = _seed_job(git_repo)
    target, _branch = create_worktree(git_repo, job["id"], 1, 1)

    attempt = dispatch(
        git_repo, job["id"], 1, _executor(), "review it\n", _prefs(),
        role="review", review_target=target,
    )

    note = attempt["note"].strip()
    assert str(target.resolve() / ".crew-chief-brief.md") in note
    assert f"Work only inside {target.resolve()}." in note
    assert note.endswith("Do not modify any file; reply with your review.")


# --- no change -------------------------------------------------------------


def test_exec_that_changes_nothing_is_no_change(git_repo, fake_bin):
    fake_bin("stub", "print('I could not find the brief')\n")
    job = _seed_job(git_repo)

    attempt = dispatch(git_repo, job["id"], 1, _executor(), "go\n", _prefs())

    assert attempt["status"] == "no_change"
    assert attempt["diffstat"] == []
    assert "could not find the brief" in attempt["note"]
    assert _stored_attempt(git_repo, job["id"])["status"] == "no_change"


def test_coordinator_files_alone_are_no_change(git_repo, fake_bin):
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

    assert attempt["status"] == "no_change"


def test_exec_that_commits_its_own_work_is_ok(git_repo, fake_bin):
    fake_bin(
        "stub",
        "import subprocess\n"
        "from pathlib import Path\n"
        "Path('out.py').write_text('x = 1\\n', encoding='utf-8')\n"
        "subprocess.run(['git', 'add', 'out.py'], check=True)\n"
        "subprocess.run(['git', 'commit', '-q', '-m', 'self'], check=True)\n"
        "print('committed')\n",
    )
    job = _seed_job(git_repo)

    attempt = dispatch(git_repo, job["id"], 1, _executor(), "go\n", _prefs())

    assert attempt["status"] == "ok"


def test_review_that_changes_nothing_stays_ok(git_repo, fake_bin):
    fake_bin("stub", "print('looks fine')\n")
    job = _seed_job(git_repo)
    target, _branch = create_worktree(git_repo, job["id"], 1, 1)

    attempt = dispatch(
        git_repo, job["id"], 1, _executor(), "review it\n", _prefs(),
        role="review", review_target=target,
    )

    assert attempt["status"] == "ok"


def test_prompt_tells_the_executor_not_to_coordinate(git_repo, fake_bin):
    # AGENTS.md in the worktree tells every agent to use crew-chief; an
    # executor that obeys it starts coordinating instead of doing its piece.
    fake_bin(
        "stub",
        "import sys\n"
        "from pathlib import Path\n"
        "Path('out.py').write_text('x = 1\\n', encoding='utf-8')\n"
        "print(sys.argv[1])\n",
    )
    job = _seed_job(git_repo)

    attempt = dispatch(git_repo, job["id"], 1, _executor(), "go\n", _prefs())

    note = attempt["note"]
    assert attempt["status"] == "ok"
    assert "do not use the crew-chief skill" in note
    assert "do not dispatch other agents" in note


def test_silent_failure_keeps_stderr_in_the_note(git_repo, fake_bin):
    fake_bin(
        "stub",
        "import sys\nsys.stderr.write('boom: no model\\n')\nsys.exit(3)\n",
    )
    job = _seed_job(git_repo)

    attempt = dispatch(git_repo, job["id"], 1, _executor(), "go\n", _prefs())

    assert attempt["status"] == "failed"
    assert "boom: no model" in attempt["note"]


# --- corrections start from the earlier work -------------------------------

#: First attempt: writes a 40-line ``page.py``.
_FIRST_WORK = (
    "from pathlib import Path\n"
    "Path('page.py').write_text(''.join(f'line {i}\\n' for i in range(40)),"
    " encoding='utf-8')\n"
    "print('page written')\n"
)

#: Correction: appends one line to ``page.py`` (creating it when missing).
_ADJUSTMENT = (
    "from pathlib import Path\n"
    "page = Path('page.py')\n"
    "text = page.read_text(encoding='utf-8') if page.exists() else ''\n"
    "page.write_text(text + 'justify-center\\n', encoding='utf-8')\n"
    "print('adjusted')\n"
)


def _correct(repo, job_id: str, reason: str = "add justify-center") -> None:
    records.update_job(
        repo, job_id, lambda job: records.add_correction(job, 1, reason, "stub:m")
    )


def test_correction_starts_from_the_last_ok_attempt(git_repo, fake_bin):
    job = _seed_job(git_repo)
    fake_bin("stub", _FIRST_WORK)
    first = dispatch(git_repo, job["id"], 1, _executor(), "page\n", _prefs())
    assert first["status"] == "ok"
    _correct(git_repo, job["id"])

    fake_bin("stub", _ADJUSTMENT)
    second = dispatch(
        git_repo, job["id"], 1, _executor(), "add justify-center\n", _prefs()
    )

    assert second["status"] == "ok"
    assert second["base"] == {"attempt": first["n"], "branch": first["branch"]}
    page = (Path(second["worktree"]) / "page.py").read_text(encoding="utf-8")
    assert page.startswith("line 0\n")
    assert page.endswith("line 39\njustify-center\n")
    assert second["diffstat"] == [{"file": "page.py", "added": 1, "deleted": 0}]
    assert second["destructive_suspect"] == []
    assert _stored_attempt(git_repo, job["id"], 1, 1)["base"] == second["base"]


def test_fresh_correction_starts_from_head(git_repo, fake_bin):
    job = _seed_job(git_repo)
    fake_bin("stub", _FIRST_WORK)
    dispatch(git_repo, job["id"], 1, _executor(), "page\n", _prefs())
    _correct(git_repo, job["id"])

    fake_bin("stub", _ADJUSTMENT)
    second = dispatch(
        git_repo, job["id"], 1, _executor(), "redo\n", _prefs(), fresh=True
    )

    assert second["status"] == "ok"
    assert second["base"] is None
    page = (Path(second["worktree"]) / "page.py").read_text(encoding="utf-8")
    assert page == "justify-center\n"


def test_new_attempt_without_correction_starts_from_head(git_repo, fake_bin):
    job = _seed_job(git_repo)
    fake_bin("stub", _FIRST_WORK)
    dispatch(git_repo, job["id"], 1, _executor(), "page\n", _prefs())

    fake_bin("stub", _ADJUSTMENT)
    second = dispatch(git_repo, job["id"], 1, _executor(), "again\n", _prefs())

    assert second["base"] is None
    page = (Path(second["worktree"]) / "page.py").read_text(encoding="utf-8")
    assert page == "justify-center\n"


def test_correction_recorded_before_the_ok_attempt_does_not_count(
    git_repo, fake_bin
):
    job = _seed_job(git_repo)
    _correct(git_repo, job["id"], "an older correction")
    fake_bin("stub", _FIRST_WORK)
    dispatch(git_repo, job["id"], 1, _executor(), "page\n", _prefs())

    fake_bin("stub", _ADJUSTMENT)
    second = dispatch(git_repo, job["id"], 1, _executor(), "again\n", _prefs())

    assert second["base"] is None


def test_correction_after_the_base_branch_is_gone_starts_from_head(
    git_repo, fake_bin
):
    job = _seed_job(git_repo)
    fake_bin("stub", _FIRST_WORK)
    first = dispatch(git_repo, job["id"], 1, _executor(), "page\n", _prefs())
    subprocess.run(
        ["git", "worktree", "remove", "--force", first["worktree"]],
        cwd=git_repo, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "branch", "-D", first["branch"]],
        cwd=git_repo, check=True, capture_output=True,
    )
    _correct(git_repo, job["id"])

    fake_bin("stub", _ADJUSTMENT)
    second = dispatch(git_repo, job["id"], 1, _executor(), "again\n", _prefs())

    assert second["status"] == "ok"
    assert second["base"] is None


def test_explicit_base_by_attempt_number_and_by_branch(git_repo, fake_bin):
    job = _seed_job(git_repo)
    fake_bin("stub", _FIRST_WORK)
    first = dispatch(git_repo, job["id"], 1, _executor(), "page\n", _prefs())

    fake_bin("stub", _ADJUSTMENT)
    by_number = dispatch(
        git_repo, job["id"], 1, _executor(), "adjust\n", _prefs(),
        base=str(first["n"]),
    )
    by_branch = dispatch(
        git_repo, job["id"], 1, _executor(), "adjust\n", _prefs(),
        base=first["branch"],
    )

    expected = {"attempt": first["n"], "branch": first["branch"]}
    assert by_number["base"] == expected
    assert by_branch["base"] == expected
    for attempt in (by_number, by_branch):
        assert attempt["diffstat"] == [
            {"file": "page.py", "added": 1, "deleted": 0}
        ]


def test_unknown_base_is_refused_before_anything_runs(git_repo, fake_bin):
    fake_bin("stub", "print('must not run')\n")
    job = _seed_job(git_repo)

    with pytest.raises(ValueError, match="no exec attempt 7"):
        dispatch(git_repo, job["id"], 1, _executor(), "go\n", _prefs(), base="7")
    with pytest.raises(ValueError, match="crew/nope"):
        dispatch(
            git_repo, job["id"], 1, _executor(), "go\n", _prefs(),
            base="crew/nope",
        )
    with pytest.raises(ValueError, match="fresh"):
        dispatch(
            git_repo, job["id"], 1, _executor(), "go\n", _prefs(),
            base="crew/nope", fresh=True,
        )

    stored = json.loads(
        (records.jobs_dir(git_repo) / f"{job['id']}.json").read_text("utf-8")
    )
    assert stored["pieces"][0]["attempts"] == []
    assert not (git_repo.parent / f"{git_repo.name}.crew").exists()


# --- proof and scope -------------------------------------------------------


def _py(code: str) -> str:
    """A shell command running ``code`` with this interpreter (no double quotes)."""
    return f'"{sys.executable}" -c "{code}"'


def test_proof_pass_keeps_ok_and_records_exit(git_repo, fake_bin):
    fake_bin("stub", _WRITES)
    job = _seed_job(git_repo)

    attempt = dispatch(
        git_repo, job["id"], 1, _executor(), "go\n", _prefs(),
        proof=_py("import out"),
    )

    assert attempt["status"] == "ok"
    assert attempt["proof"] == {"cmd": _py("import out"), "exit": 0, "passed": True}
    assert _stored_attempt(git_repo, job["id"])["proof"]["passed"] is True


def test_proof_runs_after_the_commit_and_leaves_its_files_alone(git_repo, fake_bin):
    fake_bin("stub", _WRITES)
    job = _seed_job(git_repo)

    attempt = dispatch(
        git_repo, job["id"], 1, _executor(), "go\n", _prefs(),
        proof=_py("open('made-by-proof.txt', 'w').write('x')"),
    )

    worktree = Path(attempt["worktree"])
    assert attempt["status"] == "ok"
    assert (worktree / "made-by-proof.txt").is_file()
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=worktree, capture_output=True, text=True,
        encoding="utf-8",
    ).stdout.split()
    assert "out.py" in tracked and "made-by-proof.txt" not in tracked


def test_proof_failure_is_proof_failed_with_output_in_note(git_repo, fake_bin):
    fake_bin("stub", _WRITES + "print('executor says hi')\n")
    job = _seed_job(git_repo)
    proof = _py("import sys; print('nope'); sys.exit(3)")

    attempt = dispatch(
        git_repo, job["id"], 1, _executor(), "go\n", _prefs(), proof=proof
    )

    assert attempt["status"] == "proof_failed"
    assert attempt["proof"] == {"cmd": proof, "exit": 3, "passed": False}
    assert attempt["note"].startswith(f"proof: {proof}\n")
    assert "nope" in attempt["note"]
    assert "executor says hi" in attempt["note"]
    assert _stored_attempt(git_repo, job["id"])["status"] == "proof_failed"
    assert _branch_exists(git_repo, attempt["branch"])


def test_proof_timeout_is_proof_failed(git_repo, fake_bin):
    fake_bin("stub", _WRITES)
    job = _seed_job(git_repo)

    start = time.monotonic()
    attempt = dispatch(
        git_repo, job["id"], 1, _executor(), "go\n", _prefs(), timeout_s=3,
        proof=_py("import time; time.sleep(30)"),
    )
    elapsed = time.monotonic() - start

    assert attempt["status"] == "proof_failed"
    assert attempt["proof"]["exit"] is None
    assert attempt["proof"]["passed"] is False
    assert "timed out" in attempt["note"]
    assert elapsed < 20


def test_missing_proof_command_is_proof_failed(git_repo, fake_bin):
    fake_bin("stub", _WRITES)
    job = _seed_job(git_repo)

    attempt = dispatch(
        git_repo, job["id"], 1, _executor(), "go\n", _prefs(),
        proof="definitely-not-a-command-xyz",
    )

    assert attempt["status"] == "proof_failed"
    assert attempt["proof"]["passed"] is False
    assert attempt["proof"]["exit"] != 0
    assert "definitely-not-a-command-xyz" in attempt["note"]


def test_expect_text_missing_is_proof_failed(git_repo, fake_bin):
    fake_bin("stub", _WRITES)
    job = _seed_job(git_repo)

    missing = dispatch(
        git_repo, job["id"], 1, _executor(), "go\n", _prefs(),
        proof=_py("print('3 passed')"), proof_expect="4 passed",
    )
    found = dispatch(
        git_repo, job["id"], 1, _executor(), "go\n", _prefs(),
        proof=_py("print('3 passed')"), proof_expect="3 passed",
    )

    assert missing["status"] == "proof_failed"
    assert missing["proof"]["exit"] == 0 and missing["proof"]["passed"] is False
    assert "4 passed" in missing["note"]
    assert found["status"] == "ok" and found["proof"]["passed"] is True


def test_proof_does_not_run_when_the_attempt_is_not_ok(git_repo, fake_bin):
    fake_bin("stub", "print('nothing to do')\n")
    job = _seed_job(git_repo)

    attempt = dispatch(
        git_repo, job["id"], 1, _executor(), "go\n", _prefs(),
        proof=_py("import sys; sys.exit(3)"),
    )

    assert attempt["status"] == "no_change"
    assert "proof" not in attempt


def test_skipped_proof_is_recorded(git_repo, fake_bin):
    fake_bin("stub", _WRITES)
    job = _seed_job(git_repo)

    attempt = dispatch(
        git_repo, job["id"], 1, _executor(), "go\n", _prefs(), skip_proof=True
    )

    assert attempt["status"] == "ok"
    assert attempt["proof"] == {"skipped": True}


def test_attempt_without_proof_has_no_proof_or_scope_keys(git_repo, fake_bin):
    fake_bin("stub", _WRITES)
    job = _seed_job(git_repo)

    attempt = dispatch(git_repo, job["id"], 1, _executor(), "go\n", _prefs())

    assert "proof" not in attempt and "scope" not in attempt


def test_scope_lists_files_outside_allowed(git_repo, fake_bin):
    fake_bin(
        "stub",
        "from pathlib import Path\n"
        "Path('src').mkdir()\n"
        "Path('src/a.py').write_text('a = 1\\n', encoding='utf-8')\n"
        "Path('out.py').write_text('x = 1\\n', encoding='utf-8')\n",
    )
    job = _seed_job(git_repo)

    attempt = dispatch(
        git_repo, job["id"], 1, _executor(), "go\n", _prefs(),
        allowed_files=["src/*"],
    )

    assert attempt["status"] == "ok"
    assert attempt["scope"] == ["out.py"]


def test_scope_is_absent_when_everything_is_allowed(git_repo, fake_bin):
    fake_bin("stub", _WRITES)
    job = _seed_job(git_repo)

    attempt = dispatch(
        git_repo, job["id"], 1, _executor(), "go\n", _prefs(),
        allowed_files=["out.py"],
    )

    assert attempt["status"] == "ok"
    assert "scope" not in attempt
