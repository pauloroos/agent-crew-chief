"""Tests for the ledger, the scores and the CLI."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from crew_chief import ledger, score
from crew_chief.cli import main

NOW = datetime(2026, 10, 31, tzinfo=timezone.utc)


def _entry(model: str, value: int, kind: str = "code", cost: str = "free", days: int = 0, **extra) -> dict:
    at = (NOW - timedelta(days=days)).isoformat()
    return {"at": at, "kind": kind, "agent": "opencode", "model": model, "cost": cost,
            "outcome": "pass" if value >= 60 else "fail", "score": value, **extra}


def test_append_and_read_skip_a_broken_line(ledger_home: Path):
    ledger.append({"kind": "code", "agent": "codex", "model": "m", "score": 80, "note": None})
    with ledger.ledger_path().open("a", encoding="utf-8") as handle:
        handle.write('{"cut short')
    ledger.append({"kind": "code", "agent": "codex", "model": "m", "score": 70})
    entries = ledger.read()
    assert [e["score"] for e in entries] == [80, 70]
    assert entries[0]["at"] and "note" not in entries[0]
    assert ledger.ledger_path().parent == ledger_home


def test_one_perfect_grade_is_not_a_perfect_score():
    rows = score.table([_entry("a", 100, days=0)], now=NOW)
    assert rows[0]["score"] == 75.0  # pulled toward the prior of 50


def test_old_grades_weigh_less():
    rows = score.table([_entry("a", 100, days=60), _entry("a", 0, days=0)], now=NOW)
    # weights 0.25 and 1, prior 50 x 1: (25 + 0 + 50) / 2.25
    assert rows[0]["score"] == 33.3
    assert rows[0]["n"] == 2 and rows[0]["pass_rate"] == 0.5


def test_rows_split_by_kind_and_effort_and_filter_by_project():
    entries = [
        _entry("a", 90, project="x"),
        _entry("a", 90, kind="tests", project="y"),
        _entry("a", 90, effort="high", project="x"),
    ]
    rows = score.table(entries, now=NOW)
    assert [(r["kind"], r["executor"]) for r in rows] == [
        ("code", "opencode:a"), ("code", "opencode:a (high)"), ("tests", "opencode:a")
    ]
    assert len(score.table(entries, project="x", now=NOW)) == 2
    assert len(score.table(entries, kind="tests", now=NOW)) == 1


def test_suggest_takes_the_cheapest_proven_model():
    entries = [
        *[_entry("paid-best", 100, cost="paid") for _ in range(3)],
        *[_entry("free-good", 85, cost="free") for _ in range(2)],
        _entry("free-lucky", 100, cost="free"),
    ]
    rows = score.table(entries, now=NOW)
    assert score.suggest(rows)["model"] == "free-good"
    assert score.suggest(rows, max_cost="free")["model"] == "free-good"
    # nothing proven: the best score so far, even with one job
    assert score.suggest(score.table([_entry("x", 40), _entry("y", 65)], now=NOW))["model"] == "y"
    # a tie goes to the cheaper model
    tie = score.table([_entry("paid", 90, cost="paid"), _entry("free", 90, cost="free")], now=NOW)
    assert score.suggest(tie)["model"] == "free"
    assert score.suggest([]) is None


def test_render_lists_each_kind():
    text = score.render(score.table([_entry("a", 90, minutes=12), _entry("b", 50, kind="docs")], now=NOW))
    assert "## code" in text and "## docs" in text
    assert "| opencode:a | 70.0 | 1 | 100% | 12.0 | free |" in text
    assert score.render([]) == "No graded jobs yet."


def _v06_project(tmp_path: Path) -> Path:
    project = tmp_path / "shop"
    jobs = project / "docs" / "crew-chief" / "jobs"
    jobs.mkdir(parents=True)
    job = {"id": "20261030-x", "created": "2026-10-30T01:00:00+00:00", "pieces": [{
        "n": 1, "title": "login", "type": "tests",
        "attempts": [{"executor": "opencode:m", "cost": "free"}],
        "grades": {"opencode:m": {"score": 55, "graded_at": "2026-10-30T02:00:00+00:00"},
                   "claude:opus": {"score": None}},
    }, {
        "n": 2, "title": "old", "type": "code", "attempts": [],
        "grade": {"executor": "codex:m", "score": 90, "graded_at": "2026-10-29T00:00:00+00:00"},
    }]}
    (jobs / "20261030-x.json").write_text(json.dumps(job), encoding="utf-8")
    return project


def test_import_v06_copies_grades_once(tmp_path: Path):
    project = _v06_project(tmp_path)
    assert ledger.import_v06(project) == 2
    assert ledger.import_v06(project) == 0
    entry, legacy = ledger.read()
    assert (legacy["agent"], legacy["score"], legacy["job"]) == ("codex", 90, "old")
    assert entry["at"] == "2026-10-30T02:00:00+00:00"
    assert (entry["project"], entry["kind"], entry["agent"], entry["model"]) == ("shop", "tests", "opencode", "m")
    assert (entry["cost"], entry["outcome"], entry["score"], entry["job"]) == ("free", "fail", 55, "login")


def test_cli_record_pick_and_score(capsys):
    def record(value: int) -> None:
        assert main(["record", "--kind", "code", "--agent", "codex", "--model", "m", "--effort", "high",
                     "--cost", "included", "--outcome", "pass", "--score", str(value),
                     "--minutes", "9", "--project", "shop"]) == 0

    record(90)
    assert "recorded codex:m (high) code pass 90" in capsys.readouterr().out
    assert main(["pick", "--kind", "code"]) == 0
    assert capsys.readouterr().out.startswith("pick code: codex:m (high) (score 70.0, n 1, included) - not proven")

    record(80)
    capsys.readouterr()
    assert main(["pick", "--kind", "code"]) == 0
    assert capsys.readouterr().out.startswith("pick code: codex:m (high) (score 73.3, n 2, included)\n")

    assert main(["pick", "--kind", "code", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["pick"]["model"] == "m"

    assert main(["pick", "--kind", "docs"]) == 0
    assert "no graded docs jobs yet" in capsys.readouterr().out

    assert main(["score", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["n"] == 2


def test_cli_record_refuses_a_score_out_of_range(capsys):
    with pytest.raises(SystemExit):
        main(["record", "--kind", "code", "--agent", "a", "--model", "m", "--outcome", "pass", "--score", "101"])
    assert "0 to 100" in capsys.readouterr().err
    assert ledger.read() == []


def test_cli_import(tmp_path: Path, capsys):
    assert main(["import", str(_v06_project(tmp_path))]) == 0
    assert "imported 2 grades from shop" in capsys.readouterr().out
    assert main(["import", str(tmp_path / "nothing")]) == 2


def test_project_name_is_the_repository_seen_from_a_worktree(tmp_path: Path, monkeypatch):
    import subprocess

    from crew_chief.cli import _project_name

    repo = tmp_path / "shop"
    repo.mkdir()
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q"], cwd=repo, check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "start"], cwd=repo, check=True)
    subprocess.run([*git, "worktree", "add", "-q", "-b", "lane", str(tmp_path / "shop-lane")], cwd=repo, check=True)
    monkeypatch.chdir(tmp_path / "shop-lane")
    assert _project_name() == "shop"
    monkeypatch.chdir(tmp_path)
    assert _project_name() == tmp_path.name
