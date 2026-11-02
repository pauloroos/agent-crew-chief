"""Tests for declared models (models.toml): written first, alongside T001-T003."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from crew_chief import ledger, models
from crew_chief.cli import main


def _write_global(ledger_home: Path, text: str) -> Path:
    path = ledger_home / "models.toml"
    path.write_text(text, encoding="utf-8")
    return path


def _repo(tmp_path: Path, monkeypatch, name: str = "shop") -> Path:
    repo = tmp_path / name
    (repo / ".crew-chief").mkdir(parents=True)
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q"], cwd=repo, check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "start"], cwd=repo, check=True)
    monkeypatch.chdir(repo)
    return repo


def _write_project(repo: Path, text: str) -> Path:
    path = repo / ".crew-chief" / "models.toml"
    path.write_text(text, encoding="utf-8")
    return path


def _grade(model: str, value: int, kind: str = "code", cost: str = "free", n: int = 1,
           agent: str = "opencode", **extra) -> None:
    for _ in range(n):
        ledger.append({"kind": kind, "agent": agent, "model": model, "cost": cost,
                       "outcome": "pass" if value >= 60 else "fail",
                       "score": value, "project": "shop", **extra})


FREE_A = """\
[[model]]
agent = "opencode"
model = "provider/model-a"
cost = "free"
"""

PAID_B = """\
[[model]]
agent = "opencode"
model = "provider/model-b"
cost = "paid"
"""


def test_no_file_pick_matches_08(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert models.load() == ([], [])
    _grade("provider/model-a", 90, n=2)
    assert main(["pick", "--kind", "code"]) == 0
    assert capsys.readouterr().out.startswith(
        "pick code: opencode:provider/model-a (score 76.7, n 2, free)\n")
    assert main(["pick", "--kind", "docs"]) == 0
    assert "no graded docs jobs yet" in capsys.readouterr().out
    assert main(["score"]) == 0
    assert "not tried yet" not in capsys.readouterr().out


def test_no_file_models_message(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["models"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "No models.toml found: ACC pick uses the scoreboard only."
    assert str(models.global_path()) in out


def test_only_global(ledger_home: Path, tmp_path: Path, monkeypatch, capsys):
    global_path = _write_global(ledger_home, FREE_A + PAID_B.replace(
        'cost = "paid"', 'cost = "paid"\nkinds = ["tests"]'))
    monkeypatch.chdir(tmp_path)
    assert main(["models"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == f"models from: {global_path}"
    assert "| model | from | cost | kinds | scores | note |" in out
    assert "| opencode:provider/model-a | global | free | all | not tried yet |  |" in out
    assert main(["models", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["files"] == [str(global_path)]
    assert [(m["model"], m["source"], m["kinds"]) for m in payload["models"]] == [
        ("provider/model-a", "global", None), ("provider/model-b", "global", ["tests"])]
    # only model-a takes code work
    assert main(["pick", "--kind", "code"]) == 0
    assert capsys.readouterr().out.startswith(
        "pick code: opencode:provider/model-a (not tried yet, free) - record its score")


def test_only_project(tmp_path: Path, monkeypatch, capsys):
    repo = _repo(tmp_path, monkeypatch)
    project_path = _write_project(repo, FREE_A)
    assert main(["models"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == f"models from: {project_path}"
    assert "| opencode:provider/model-a | project | free | all | not tried yet |  |" in out


def test_project_overrides_one_field(ledger_home: Path, tmp_path: Path, monkeypatch, capsys):
    _write_global(ledger_home, FREE_A.replace('cost = "free"', 'cost = "cheap"\nnote = "global note"'))
    repo = _repo(tmp_path, monkeypatch)
    _write_project(repo, """\
[[model]]
agent = "opencode"
model = "provider/model-a"
note = "project note"
""")
    assert main(["models", "--json"]) == 0
    (entry,) = json.loads(capsys.readouterr().out)["models"]
    assert (entry["cost"], entry["note"], entry["source"]) == ("cheap", "project note", "project")


def test_project_enabled_false_switches_off_global(ledger_home: Path, tmp_path: Path,
                                                   monkeypatch, capsys):
    _write_global(ledger_home, FREE_A + PAID_B)
    repo = _repo(tmp_path, monkeypatch)
    _write_project(repo, """\
[[model]]
agent = "opencode"
model = "provider/model-a"
enabled = false
""")
    assert main(["models", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [m["model"] for m in payload["models"]] == ["provider/model-b"]


def test_inherit_false(ledger_home: Path, tmp_path: Path, monkeypatch, capsys):
    _write_global(ledger_home, FREE_A)
    repo = _repo(tmp_path, monkeypatch)
    _write_project(repo, "inherit = false\n" + PAID_B)
    assert main(["models", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [(m["model"], m["source"]) for m in payload["models"]] == [("provider/model-b", "project")]


def test_kinds_filter_excludes_everything(ledger_home: Path, tmp_path: Path, monkeypatch, capsys):
    _write_global(ledger_home, FREE_A.replace('cost = "free"', 'cost = "free"\nkinds = ["tests"]'))
    monkeypatch.chdir(tmp_path)
    assert main(["pick", "--kind", "code"]) == 0
    assert capsys.readouterr().out == (
        "no model in your models.toml takes code work: add one or widen --max-cost\n")


def test_max_cost(ledger_home: Path, tmp_path: Path, monkeypatch, capsys):
    _write_global(ledger_home, FREE_A.replace('cost = "free"', 'cost = "cheap"') + PAID_B)
    _grade("provider/model-b", 100, cost="paid", n=2)
    monkeypatch.chdir(tmp_path)
    assert main(["pick", "--kind", "code", "--max-cost", "cheap"]) == 0
    assert capsys.readouterr().out.startswith(
        "pick code: opencode:provider/model-a (not tried yet, cheap) - record its score")
    assert main(["pick", "--kind", "code", "--max-cost", "free"]) == 0
    assert capsys.readouterr().out == (
        "no model in your models.toml takes code work within free: add one or widen --max-cost\n")


def test_untried_cheapest_tier_before_not_proven(ledger_home: Path, tmp_path: Path,
                                                monkeypatch, capsys):
    _write_global(ledger_home, FREE_A + PAID_B)
    _grade("provider/model-b", 100, cost="paid")  # one great grade: not proven yet
    monkeypatch.chdir(tmp_path)
    assert main(["pick", "--kind", "code"]) == 0
    assert capsys.readouterr().out.startswith(
        "pick code: opencode:provider/model-a (not tried yet, free) - record its score")


def test_proven_wins_over_untried(ledger_home: Path, tmp_path: Path, monkeypatch, capsys):
    _write_global(ledger_home, FREE_A + PAID_B)
    _grade("provider/model-b", 100, cost="paid", n=2)
    monkeypatch.chdir(tmp_path)
    assert main(["pick", "--kind", "code"]) == 0
    assert capsys.readouterr().out.startswith(
        "pick code: opencode:provider/model-b (score 83.3, n 2, paid)\n")


def test_graded_model_outside_the_list_is_never_picked(ledger_home: Path, tmp_path: Path,
                                                      monkeypatch, capsys):
    _write_global(ledger_home, FREE_A)
    _grade("provider/outsider", 100, n=3)
    monkeypatch.chdir(tmp_path)
    assert main(["pick", "--kind", "code"]) == 0
    assert capsys.readouterr().out.startswith(
        "pick code: opencode:provider/model-a (not tried yet, free) - record its score")
    assert main(["pick", "--kind", "code", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["pick"]["model"] == "provider/model-a"
    assert all(r["model"] != "provider/outsider" for r in payload["rows"])


def test_score_kind_adds_n0_rows(ledger_home: Path, tmp_path: Path, monkeypatch, capsys):
    _write_global(ledger_home, FREE_A + PAID_B)
    _grade("provider/model-a", 90, n=2)
    monkeypatch.chdir(tmp_path)
    assert main(["score", "--kind", "code"]) == 0
    out = capsys.readouterr().out
    assert "| opencode:provider/model-b | - | 0 | - |" in out
    assert main(["score", "--kind", "code", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [(r["model"], r["n"]) for r in rows] == [("provider/model-a", 2), ("provider/model-b", 0)]


def test_score_without_kind_lists_not_tried_yet(ledger_home: Path, tmp_path: Path,
                                                monkeypatch, capsys):
    _write_global(ledger_home, FREE_A + PAID_B)
    _grade("provider/model-a", 90, n=2)
    monkeypatch.chdir(tmp_path)
    assert main(["score"]) == 0
    out = capsys.readouterr().out
    section = out.split("## not tried yet", 1)[1]
    assert "opencode:provider/model-b" in section
    assert "provider/model-a" not in section


def test_effort_matches_only_same_effort_grades(ledger_home: Path, tmp_path: Path,
                                                monkeypatch, capsys):
    _write_global(ledger_home, FREE_A.replace('cost = "free"', 'cost = "free"\neffort = "high"'))
    _grade("provider/model-a", 90, n=2)
    _grade("provider/model-a", 40, n=2, effort="high")
    monkeypatch.chdir(tmp_path)
    assert main(["models", "--json"]) == 0
    (entry,) = json.loads(capsys.readouterr().out)["models"]
    assert [(s["kind"], s["n"]) for s in entry["scores"]] == [("code", 2)]
    assert main(["models"]) == 0
    assert "| opencode:provider/model-a (high) | global | free | all | code " in capsys.readouterr().out


INVALID_GLOBAL = {
    "broken TOML": "[[model\nagent = ",
    "bad cost": FREE_A.replace('cost = "free"', 'cost = "luxury"'),
    "missing agent": '[[model]]\nmodel = "provider/model-a"\ncost = "free"\n',
    "unknown model key": FREE_A + 'foo = 1\n',
    "unknown top key": 'oops = true\n' + FREE_A,
    "inherit in global": "inherit = false\n" + FREE_A,
    "missing cost": '[[model]]\nagent = "opencode"\nmodel = "provider/model-a"\n',
    "duplicate": FREE_A + FREE_A,
    "bad kinds": FREE_A.replace('cost = "free"', 'cost = "free"\nkinds = ["poetry"]'),
}


@pytest.mark.parametrize("content", INVALID_GLOBAL.values(), ids=list(INVALID_GLOBAL))
def test_invalid_global_file_exits_2(ledger_home: Path, tmp_path: Path, monkeypatch,
                                     capsys, content: str):
    global_path = _write_global(ledger_home, content)
    monkeypatch.chdir(tmp_path)
    for command in (["models"], ["pick", "--kind", "code"], ["score"]):
        assert main(command) == 2
        err = capsys.readouterr().err
        assert str(global_path) in err


def test_project_inherit_must_be_bool(tmp_path: Path, monkeypatch, capsys):
    repo = _repo(tmp_path, monkeypatch)
    project_path = _write_project(repo, 'inherit = "yes"\n' + FREE_A)
    assert main(["models"]) == 2
    assert str(project_path) in capsys.readouterr().err


def test_record_ignores_an_invalid_file(ledger_home: Path, tmp_path: Path, monkeypatch, capsys):
    _write_global(ledger_home, INVALID_GLOBAL["broken TOML"])
    monkeypatch.chdir(tmp_path)
    assert main(["record", "--kind", "code", "--agent", "opencode", "--model", "provider/model-a",
                 "--cost", "free", "--outcome", "pass", "--score", "80", "--project", "shop"]) == 0
    assert len(ledger.read()) == 1


def test_worktree_reads_the_main_repo_file(tmp_path: Path, monkeypatch, capsys):
    repo = _repo(tmp_path, monkeypatch, name="shop")
    project_path = _write_project(repo, FREE_A)
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "worktree", "add", "-q", "-b", "lane", str(tmp_path / "shop-lane")],
                   cwd=repo, check=True)
    monkeypatch.chdir(tmp_path / "shop-lane")
    assert models.project_path() == project_path
    assert main(["models"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == f"models from: {project_path}"
    assert "| opencode:provider/model-a | project |" in out
