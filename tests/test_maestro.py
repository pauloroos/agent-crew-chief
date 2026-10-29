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
    "idle": "print('worked a bit')\n",
}


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

    record = maestro.run(folder)

    assert record["state"] == "completed"
    assert record["pr_url"] == "https://example.test/pr/1"
    assert [r["outcome"] for r in record["rounds"]] == ["quota", "ok"]
    assert [r["coordinator"] for r in record["rounds"]] == ["quota:m1", "deliver:m2"]
    # the quota round ran before the job existed; its tokens still reach the job
    job = records.load_job(Path(record["worktree"]), "job-1")
    assert [u["tokens_total"] for u in job["usage"]] == [15, 15]
    [summary] = maestro.status(repo)
    assert summary["switches"] == 1
    assert summary["coordinator_tokens"] == 30
    assert summary["economy"]["coordinator_tokens"] == 30
    assert summary["branch"].startswith("acc/01-auth-")


def test_every_coordinator_on_quota_holds_until_resume(repo, tmp_path):
    folder = _launch(repo, tmp_path, "quota:m1", "quota:m2")

    record = maestro.run(folder)
    assert record["state"] == "quota"
    assert "rate limit" in record["last_error"]

    resumed = maestro.resume(repo, record["id"], spawn=False)
    assert resumed["state"] == "running"
    assert resumed["index"] == 0
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

    prompts = []
    real_prompt = maestro._prompt
    monkeypatch.setattr(maestro, "_prompt", lambda r, f, first: prompts.append(first) or real_prompt(r, f, first))
    maestro.run(folder)

    assert prompts == [False]
    assert (folder / "round-1.log").is_file() and (folder / "round-2.log").is_file()
    assert [u["tokens_in"] for u in maestro.load(folder)["pending_usage"]] == [40, 40]
