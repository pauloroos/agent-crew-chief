"""The maestro launcher: fallback on quota, questions, rounds and metrics.

Each fake coordinator is a ``generic`` tool that runs a small Python script in
the front's worktree: one hits a quota, one records a job with a PR, one asks
the owner, one ends without delivering. Runs are driven in-process with
``maestro.run`` (no detached runner).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from crew_chief import cli, maestro, records

SCRIPTS = Path(__file__).resolve().parents[1] / "skill" / "crew-chief" / "scripts"

#: Every fake but ``quota`` also reviews: it passes the delivery it is shown.
REVIEW_PASS = (
    "import re, sys\nfrom pathlib import Path\n"
    "m = re.search(r'Write the review to (.+?) in markdown', sys.argv[1])\n"
    "if m:\n"
    "    Path(m.group(1)).write_text('- login: delivered\\nVERDICT: PASS\\nSCORE: 8\\n', encoding='utf-8')\n"
    "    sys.exit(0)\n"
)

FAKES = {
    "quota": "import sys\nprint('Error: rate limit exceeded', file=sys.stderr)\nsys.exit(1)\n",
    "deliver": (
        "import sys\nfrom pathlib import Path\n"
        f"sys.path.insert(0, {str(SCRIPTS)!r})\n"
        "from crew_chief import records\n"
        "records.save_job(Path.cwd(), {'id': 'job-1', 'pieces': [], 'usage': [],\n"
        "    'pr_url': 'https://example.test/pr/1'})\n"
    ),
    "ask": (
        "import re, sys\nfrom pathlib import Path\n"
        "path = re.search(r'write the question to (.+?) and stop', sys.argv[1]).group(1)\n"
        "Path(path).write_text('Which database?', encoding='utf-8')\n"
    ),
    "idle": "import os\nprint('worked a bit as', os.environ.get('CREW_CHIEF_COORDINATOR'))\n",
    "block": (
        "import sys\nfrom pathlib import Path\n"
        f"sys.path.insert(0, {str(SCRIPTS)!r})\n"
        "from crew_chief import records\n"
        "records.save_job(Path.cwd(), {'id': 'job-1', 'pieces': [], 'usage': [],\n"
        "    'state': 'blocked', 'status': 'blocked',\n"
        "    'result': {'blocked_error': 'remote rejected', 'failed_command': 'git push origin x'}})\n"
    ),
    "local": (
        "import sys\nfrom pathlib import Path\n"
        f"sys.path.insert(0, {str(SCRIPTS)!r})\n"
        "from crew_chief import records\n"
        "records.save_job(Path.cwd(), {'id': 'job-1', 'pieces': [], 'usage': [],\n"
        "    'state': 'closed', 'status': 'closed'})\n"
    ),
    "stopme": (
        "import json, re, sys\nfrom pathlib import Path\n"
        "path = Path(re.search(r'write the question to (.+?) and stop', sys.argv[1]).group(1)).parent / 'record.json'\n"
        "record = json.loads(path.read_text(encoding='utf-8'))\n"
        "record['state'] = 'stopped'\n"
        "path.write_text(json.dumps(record), encoding='utf-8')\n"
        "sys.exit(1)\n"
    ),
    "commit": (
        "import subprocess, sys\nfrom pathlib import Path\n"
        f"sys.path.insert(0, {str(SCRIPTS)!r})\n"
        "from crew_chief import records\n"
        "records.save_job(Path.cwd(), {'id': 'job-1', 'pieces': [], 'usage': []})\n"
        "subprocess.run(['git', 'add', 'docs'], check=True)\n"
        "subprocess.run(['git', 'commit', '-qm', 'job'], check=True)\n"
        "subprocess.run(['git', 'push', '-qu', 'origin', 'HEAD'], check=True)\n"
    ),
    # fails the first review, then passes; its fix round only reports
    "picky": (
        "import re, sys\nfrom pathlib import Path\n"
        f"sys.path.insert(0, {str(SCRIPTS)!r})\n"
        "from crew_chief import records\n"
        "m = re.search(r'Write the review to (.+?) in markdown', sys.argv[1])\n"
        "if m:\n"
        "    verdict = 'FAIL' if m.group(1).endswith('review-1.md') else 'PASS'\n"
        "    Path(m.group(1)).write_text(f'- tests: missing\\n**VERDICT: {verdict}**\\nSCORE: 6,5\\n', encoding='utf-8')\n"
        "elif 'failed it against the request' in sys.argv[1]:\n"
        "    print('fixed')\n"
        "else:\n"
        "    records.save_job(Path.cwd(), {'id': 'job-1', 'pieces': [], 'usage': [],\n"
        "        'pr_url': 'https://example.test/pr/1'})\n"
    ),
    # always fails the review
    "harsh": (
        "import re, sys\nfrom pathlib import Path\n"
        f"sys.path.insert(0, {str(SCRIPTS)!r})\n"
        "from crew_chief import records\n"
        "m = re.search(r'Write the review to (.+?) in markdown', sys.argv[1])\n"
        "if m:\n"
        "    Path(m.group(1)).write_text('VERDICT: FAIL\\nSCORE: 2\\n', encoding='utf-8')\n"
        "elif 'failed it against the request' not in sys.argv[1]:\n"
        "    records.save_job(Path.cwd(), {'id': 'job-1', 'pieces': [], 'usage': [],\n"
        "        'pr_url': 'https://example.test/pr/1'})\n"
    ),
    # delivers, but as a reviewer writes no verdict
    "mute": (
        "import sys\nfrom pathlib import Path\n"
        f"sys.path.insert(0, {str(SCRIPTS)!r})\n"
        "from crew_chief import records\n"
        "if 'Write the review to' not in sys.argv[1]:\n"
        "    records.save_job(Path.cwd(), {'id': 'job-1', 'pieces': [], 'usage': [],\n"
        "        'pr_url': 'https://example.test/pr/1'})\n"
    ),
}
FAKES.update({name: REVIEW_PASS + FAKES[name] for name in ("deliver", "local", "commit")})


@pytest.fixture(autouse=True)
def comments(monkeypatch):
    """The reviews the runner would post on the pull request (no gh in tests)."""
    posted = []
    monkeypatch.setattr(maestro, "_comment_review", lambda record, entry: posted.append(entry["file"]))
    return posted


@pytest.fixture
def repo(git_repo: Path, tmp_path: Path) -> Path:
    """A repo whose committed preferences define the fake coordinator tools."""
    lines = []
    for name, body in FAKES.items():
        script = tmp_path / f"fake_{name}.py"
        script.write_text(body, encoding="utf-8")
        lines += [
            "[[tools]]",
            f'name = "{name}"',
            'kind = "generic"',
            f"bin = {json.dumps(sys.executable)}",
            f"run_cmd = [{json.dumps(sys.executable)}, {json.dumps(str(script))}, \"{{prompt}}\"]",
            "",
        ]
    prefs = git_repo / "docs" / "crew-chief" / "preferences.toml"
    prefs.parent.mkdir(parents=True, exist_ok=True)
    prefs.write_text("\n".join(lines), encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-qm", "prefs"], cwd=git_repo, check=True)
    return git_repo


def _request(tmp_path: Path) -> Path:
    path = tmp_path / "01-auth.md"
    path.write_text("Add login.\n", encoding="utf-8")
    return path


def _launch(repo: Path, tmp_path: Path, *coordinators: str) -> Path:
    record = maestro.launch(repo, _request(tmp_path), coordinators=list(coordinators), spawn=False)
    return maestro.coordinators_dir(repo) / record["id"]


def test_quota_falls_back_to_the_next_coordinator_and_books_tokens(repo, tmp_path, monkeypatch):
    monkeypatch.setattr(
        maestro.tools, "parse_metrics",
        lambda *a, **k: {"tokens_in": 10, "tokens_out": 5, "tokens_total": 15, "cost_usd": None, "steps": 3},
    )
    folder = _launch(repo, tmp_path, "quota:m1", "deliver:m2")
    prompts = []
    real_prompt = maestro._prompt
    monkeypatch.setattr(maestro, "_prompt", lambda r, f, first: prompts.append(first) or real_prompt(r, f, first))

    record = maestro.run(folder)

    # the quota round made no job, so the fallback starts one instead of continuing
    assert prompts == [True, True]
    assert record["state"] == "completed"
    assert record["pr_url"] == "https://example.test/pr/1"
    assert [r["outcome"] for r in record["rounds"]] == ["quota", "ok"]
    assert [r["coordinator"] for r in record["rounds"]] == ["quota:m1", "deliver:m2"]
    # the quota round ran before the job existed; its tokens still reach the job, whose
    # copy outlives the worktree the completed front removed
    assert not Path(record["worktree"]).exists()
    job = json.loads((folder / "job.json").read_text(encoding="utf-8"))
    # the review's tokens count for the coordinator too
    assert [u["tokens_total"] for u in job["usage"]] == [15, 15, 15]
    [summary] = maestro.status(repo)
    assert summary["switches"] == 1
    assert summary["coordinator_tokens"] == 45
    assert summary["economy"]["coordinator_tokens"] == 45
    assert summary["branch"].startswith("maestro/01-auth-")


def test_every_coordinator_on_quota_holds_until_resume(repo, tmp_path):
    folder = _launch(repo, tmp_path, "quota:m1", "quota:m2")

    record = maestro.run(folder)
    assert record["state"] == "quota"
    assert "rate limit" in record["last_error"]
    assert record["finished_at"]
    # two failed rounds, one change of coordinator
    assert maestro.status(repo)[0]["switches"] == 1

    resumed = maestro.resume(repo, record["id"], spawn=False)
    assert resumed["state"] == "running"
    assert resumed["index"] == 0
    assert resumed["finished_at"] is None
    assert len(resumed["history"]) == 2


def test_a_question_waits_for_the_answer(repo, tmp_path):
    folder = _launch(repo, tmp_path, "ask:m1")

    record = maestro.run(folder)
    assert record["state"] == "question"
    assert maestro.status(repo)[0]["question"] == "Which database?"

    maestro.resume(repo, record["id"], answer="Postgres", spawn=False)
    answers = (folder / "answers.md").read_text(encoding="utf-8")
    assert "Which database?" in answers and "Postgres" in answers
    assert not (folder / "question.md").exists()
    assert str(folder / "answers.md") in maestro._prompt(maestro.load(folder), folder, first=False)


def test_rounds_without_a_pr_continue_then_fail(repo, tmp_path, monkeypatch):
    monkeypatch.setattr(maestro, "MAX_ROUNDS", 2)
    folder = _launch(repo, tmp_path, "idle:m1")

    record = maestro.run(folder)

    assert record["state"] == "failed"
    assert len(record["rounds"]) == 2
    assert "no pull request" in record["last_error"]
    assert "This continues earlier work" in maestro._prompt(record, folder, first=False)
    assert "worked a bit as idle" in (folder / "round-1.log").read_text(encoding="utf-8")


def test_launch_rejects_an_unknown_tool(repo, tmp_path):
    with pytest.raises(maestro.MaestroError, match="known tool"):
        maestro.launch(repo, _request(tmp_path), coordinators=["nope:x"], spawn=False)


def test_cli_status_lists_the_coordinators(repo, tmp_path, capsys):
    _launch(repo, tmp_path, "deliver:m1")

    assert cli.main(["status", "--project", str(repo), "--json"]) == 0

    [summary] = json.loads(capsys.readouterr().out)["coordinators"]
    assert summary["front"] == "01-auth"
    assert summary["coordinator"] == "deliver:m1"


def test_the_detached_runner_delivers(repo, tmp_path):
    import time

    record = maestro.launch(repo, _request(tmp_path), coordinators=["deliver:m1"])
    assert record["pid"]
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        [summary] = maestro.status(repo)
        if summary["state"] != "running":
            break
        time.sleep(0.5)
    assert summary["state"] == "completed", summary
    assert summary["pr_url"] == "https://example.test/pr/1"


def test_a_resumed_coordinator_continues_and_keeps_its_logs(repo, tmp_path, monkeypatch):
    monkeypatch.setattr(maestro, "MAX_ROUNDS", 1)
    monkeypatch.setattr(
        maestro.tools, "parse_metrics",
        lambda *a, **k: {"tokens_in": None, "tokens_out": None, "tokens_total": 40, "cost_usd": None, "steps": None},
    )
    folder = _launch(repo, tmp_path, "idle:m1")
    record = maestro.run(folder)
    maestro.resume(repo, record["id"], spawn=False)
    records.save_job(Path(record["worktree"]), {"id": "job-1", "pieces": [], "usage": []})

    prompts = []
    real_prompt = maestro._prompt
    monkeypatch.setattr(maestro, "_prompt", lambda r, f, first: prompts.append(first) or real_prompt(r, f, first))
    maestro.run(folder)

    assert prompts == [False]
    assert (folder / "round-1.log").is_file() and (folder / "round-2.log").is_file()
    job = records.load_job(Path(record["worktree"]), "job-1")
    assert [u["tokens_in"] for u in job["usage"]] == [40, 40]


def test_the_job_branch_becomes_the_coordinator_branch(repo, tmp_path):
    folder = _launch(repo, tmp_path, "idle:m1")
    record = maestro.load(folder)
    records.save_job(Path(record["worktree"]), {"id": "job-1", "pieces": [], "usage": [], "job_branch": "feature/x"})

    assert maestro.status(repo)[0]["branch"] == "feature/x"


def test_the_prompt_is_one_line_and_names_the_end_mode(repo, tmp_path):
    record = maestro.launch(repo, _request(tmp_path), coordinators=["idle:m1"], spawn=False, end_mode="pr_review")
    folder = maestro.coordinators_dir(repo) / record["id"]

    prompt = maestro._prompt(maestro.load(folder), folder, first=True)

    assert "\n" not in prompt
    assert "close --end-mode pr_review" in prompt
    assert "CHANGELOG" in prompt


def test_a_coordinator_that_cannot_start_falls_back(repo, tmp_path, monkeypatch):
    real = maestro.tools.build_command
    missing = str(tmp_path / "no-such-cli.exe")
    monkeypatch.setattr(
        maestro.tools, "build_command",
        lambda tool, *a, **k: [missing] if tool["name"] == "idle" else real(tool, *a, **k),
    )
    folder = _launch(repo, tmp_path, "idle:m1", "deliver:m2")

    record = maestro.run(folder)

    assert [r["outcome"] for r in record["rounds"]] == ["failed", "ok"]
    assert "cannot start" in record["rounds"][0]["error"]
    assert record["state"] == "completed"


def test_a_claude_coordinator_ignores_user_mcp_servers(repo, tmp_path, monkeypatch):
    commands = []
    real = maestro.tools.build_command
    monkeypatch.setattr(maestro.tools, "build_command", lambda *a, **k: commands.append(real(*a, **k)) or commands[-1])
    monkeypatch.setattr(maestro.tools, "tool_env", lambda tool: None)
    folder = _launch(repo, tmp_path, "claude:opus")

    maestro.run(folder)

    assert "--strict-mcp-config" in commands[0]


def test_a_blocked_job_fails_the_coordinator(repo, tmp_path):
    folder = _launch(repo, tmp_path, "block:m1", "deliver:m2")

    record = maestro.run(folder)

    assert record["state"] == "failed"
    assert len(record["rounds"]) == 1
    assert "remote rejected" in record["last_error"] and "git push origin x" in record["last_error"]


def test_a_job_closed_without_a_pr_completes(repo, tmp_path):
    folder = _launch(repo, tmp_path, "local:m1")

    record = maestro.run(folder)

    assert record["state"] == "completed"
    assert record["pr_url"] is None
    assert len(record["rounds"]) == 1


def test_booked_coordinator_tokens_are_committed_and_pushed(repo, tmp_path, monkeypatch):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=repo, check=True)
    monkeypatch.setattr(maestro, "MAX_ROUNDS", 1)
    monkeypatch.setattr(
        maestro.tools, "parse_metrics",
        lambda *a, **k: {"tokens_in": 10, "tokens_out": 5, "tokens_total": 15, "cost_usd": None, "steps": None},
    )
    folder = _launch(repo, tmp_path, "commit:m1")

    record = maestro.run(folder)

    def git(*args):
        return subprocess.run(["git", *args], cwd=record["worktree"], capture_output=True, text=True).stdout.strip()

    assert git("log", "-1", "--format=%s") == "Book coordinator usage"
    assert git("status", "--porcelain", "docs") == ""
    assert git("rev-parse", "HEAD") == git("rev-parse", "@{u}")


def test_a_stop_during_a_round_is_kept(repo, tmp_path):
    folder = _launch(repo, tmp_path, "stopme:m1", "deliver:m2")

    record = maestro.run(folder)

    assert record["state"] == "stopped"
    assert len(record["rounds"]) == 1
    assert maestro.load(folder)["state"] == "stopped"


def test_stop_leaves_a_completed_coordinator_alone(repo, tmp_path):
    folder = _launch(repo, tmp_path, "deliver:m1")
    record = maestro.run(folder)

    assert maestro.stop(repo, record["id"]) == maestro.load(folder)
    assert maestro.load(folder)["state"] == "completed"


def test_status_passes_the_worktree_prefs_to_the_economy(repo, tmp_path, monkeypatch):
    seen = []
    real = maestro.economy.job_economy
    monkeypatch.setattr(maestro.economy, "job_economy", lambda job, prefs: seen.append(prefs) or real(job, prefs))
    maestro.run(_launch(repo, tmp_path, "deliver:m1"))

    maestro.status(repo)

    assert seen and seen[0] is not None


def test_status_reads_the_repo_record_once_the_worktree_is_gone(repo, tmp_path):
    folder = _launch(repo, tmp_path, "deliver:m1")
    record = maestro.run(folder)
    # the PR merged: its record is in the repo, and the copy clean() kept is gone
    snapshot = folder / "job.json"
    records.save_job(repo, json.loads(snapshot.read_text(encoding="utf-8")))
    snapshot.unlink()

    [summary] = maestro.status(repo)

    assert not Path(record["worktree"]).exists()
    assert summary["economy"] is not None


def test_status_shows_the_whole_error_on_one_line():
    line = maestro.status_lines([{
        "id": "a", "front": "f", "state": "failed", "coordinator": "x:m", "rounds": 1, "switches": 0,
        "seconds": 1, "coordinator_tokens": 0, "economy": None, "question": None, "pr_url": None,
        "last_error": "job j is blocked: pull request create failed" + chr(10) + "## Accepted" + chr(10) + ")",
    }])[-1]

    assert line == "    last error: job j is blocked: pull request create failed ## Accepted )"


def test_a_failed_review_sends_the_coordinator_back_until_it_passes(repo, tmp_path, monkeypatch, comments):
    folder = _launch(repo, tmp_path, "picky:m1")
    prompts = []
    real_run_round = maestro._run_round

    def spy(record, folder, prefs, **kwargs):
        if kwargs.get("label", "round") == "round":
            prompts.append(maestro._prompt(record, folder, first=not record.get("job_id")))
        return real_run_round(record, folder, prefs, **kwargs)

    monkeypatch.setattr(maestro, "_run_round", spy)

    record = maestro.run(folder)

    assert record["state"] == "completed"
    assert [r["outcome"] for r in record["rounds"]] == ["ok", "ok"]
    assert [(r["verdict"], r["score"]) for r in record["reviews"]] == [("fail", 6.5), ("pass", 6.5)]
    # the fix round names the failed review and stays on the open pull request
    assert "review-1.md" in prompts[1] and "do not open another one" in prompts[1]
    assert record["fix"] is None
    assert comments == [r["file"] for r in record["reviews"]]
    [summary] = maestro.status(repo)
    assert summary["reviews"] == ["fail", "pass"]
    assert any(line.startswith("    review: fail -> pass, score 6.5/10") for line in maestro.status_lines([summary]))


def test_a_review_without_a_verdict_falls_back_to_the_next_coordinator(repo, tmp_path):
    folder = _launch(repo, tmp_path, "mute:m1", "deliver:m2")

    record = maestro.run(folder)

    assert record["state"] == "completed"
    assert [(r["coordinator"], r["outcome"]) for r in record["reviews"]] == [("mute:m1", "failed"), ("deliver:m2", "ok")]
    assert "no verdict" in record["reviews"][0]["error"]


def test_a_review_that_keeps_failing_holds_the_front(repo, tmp_path, monkeypatch):
    monkeypatch.setattr(maestro, "MAX_REVIEWS", 2)
    folder = _launch(repo, tmp_path, "harsh:m1")

    record = maestro.run(folder)

    assert record["state"] == "failed"
    assert "after 2 reviews" in record["last_error"]
    # held fronts keep their worktree for the owner
    assert Path(record["worktree"]).is_dir()


def test_the_review_runs_in_its_own_checkout_of_the_delivered_head(repo, tmp_path, monkeypatch):
    seen = []
    real_run_round = maestro._run_round

    def spy(record, folder, prefs, **kwargs):
        if kwargs.get("label") == "review":
            cwd = kwargs["cwd"]
            seen.append((cwd != Path(record["worktree"]), cwd.is_dir(), "clean session" in kwargs["prompt"]))
        return real_run_round(record, folder, prefs, **kwargs)

    monkeypatch.setattr(maestro, "_run_round", spy)
    folder = _launch(repo, tmp_path, "deliver:m1")

    record = maestro.run(folder)

    assert seen == [(True, True, True)]
    assert record["reviews"][0]["head"]
    assert not any(p.name.startswith("review-") for p in maestro._crew_dir(repo).iterdir())


def test_a_completed_front_removes_its_worktrees_and_attempt_branches(repo, tmp_path):
    folder = _launch(repo, tmp_path, "deliver:m1")
    worktree = Path(maestro.load(folder)["worktree"])
    attempt = worktree.parent / f"{worktree.name}.crew" / "job-1-1-1"
    subprocess.run(["git", "worktree", "add", "-q", "-b", "crew/job-1-1-1", str(attempt)], cwd=worktree, check=True)

    record = maestro.run(folder)

    assert record["cleaned_at"] and record["clean_errors"] == []
    assert not worktree.exists() and not attempt.parent.exists()
    branches = subprocess.run(["git", "branch", "--list"], cwd=repo, capture_output=True, text=True).stdout
    assert "crew/job-1-1-1" not in branches
    assert record["branch"] in branches  # the PR's branch stays


def test_cli_clean_removes_a_named_front(repo, tmp_path, capsys):
    folder = _launch(repo, tmp_path, "idle:m1")
    worktree = Path(maestro.load(folder)["worktree"])

    assert cli.main(["clean", "--project", str(repo)]) == 0
    assert worktree.is_dir()  # only completed fronts by default
    assert cli.main(["clean", maestro.load(folder)["id"], "--project", str(repo)]) == 0

    assert not worktree.exists()
    assert "removed 1" in capsys.readouterr().out
