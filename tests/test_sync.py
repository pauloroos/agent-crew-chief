"""Tests for ledger dedupe (FR-003), ACC merge (FR-004) and ACC sync (FR-005)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from crew_chief import ledger
from crew_chief.cli import main


def _grade(agent="opencode", model="m", score=80, kind="code", at="2026-11-01T00:00:00+00:00", **extra):
    return ledger.append({"at": at, "kind": kind, "agent": agent, "model": model,
                          "cost": "free", "outcome": "pass", "score": score, **extra})


def _read_lines(home: Path) -> list[str]:
    return (home / "ledger.jsonl").read_text(encoding="utf-8").splitlines()


# FR-003: read() dedupe

def test_read_dedupes_identical_entries_keeping_first_position():
    _grade(score=80, at="2026-11-01T00:00:00+00:00")
    _grade(score=70, at="2026-11-01T01:00:00+00:00")
    path = ledger.ledger_path()
    # same entry again, with keys in a different order on the line
    entry = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    reordered = {k: entry[k] for k in reversed(list(entry))}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(reordered, ensure_ascii=False) + "\n")
    entries = ledger.read()
    assert [e["score"] for e in entries] == [80, 70]


# FR-004: merge

def test_merge_appends_new_skips_duplicates_and_reruns_empty(tmp_path: Path, capsys):
    _grade(score=80, at="2026-11-01T00:00:00+00:00")
    local_first = ledger.read()[0]
    other = tmp_path / "other.jsonl"
    new_line = json.dumps({"at": "2026-11-02T00:00:00+00:00", "kind": "code", "agent": "opencode",
                           "model": "m", "cost": "free", "outcome": "pass", "score": 70},
                          ensure_ascii=False)
    other.write_text(json.dumps(local_first, ensure_ascii=False) + "\n" + new_line + "\n"
                     + new_line + "\n" + '{"cut short\n', encoding="utf-8")
    assert main(["merge", str(other)]) == 0
    out = capsys.readouterr().out
    assert out == f"merged 1 new grades from {other} (2 already here)\n"
    entries = ledger.read()
    assert [e["score"] for e in entries] == [80, 70]
    assert entries[1]["at"] == "2026-11-02T00:00:00+00:00"
    assert main(["merge", str(other)]) == 0
    assert capsys.readouterr().out == f"merged 0 new grades from {other} (3 already here)\n"
    assert [e["score"] for e in ledger.read()] == [80, 70]


def test_merge_missing_and_non_ledger_leave_local_untouched(tmp_path: Path, capsys):
    _grade(score=80)
    before = ledger.ledger_path().read_bytes()
    missing = tmp_path / "nope.jsonl"
    assert main(["merge", str(missing)]) == 2
    err = capsys.readouterr().err
    assert err.startswith(f"{missing}: ")
    not_ledger = tmp_path / "notes.txt"
    not_ledger.write_text("just some notes\nsecond line\n", encoding="utf-8")
    assert main(["merge", str(not_ledger)]) == 2
    assert capsys.readouterr().err.startswith(f"{not_ledger}: ")
    assert ledger.ledger_path().read_bytes() == before


def test_merge_empty_file_merges_zero(tmp_path: Path, capsys):
    _grade(score=80)
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    assert main(["merge", str(empty)]) == 0
    assert capsys.readouterr().out == f"merged 0 new grades from {empty} (0 already here)\n"
    assert [e["score"] for e in ledger.read()] == [80]


# FR-005: sync

def _remote(tmp_path: Path) -> str:
    repo = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(repo)], check=True)
    return str(repo)


def _home_with_grades(home: Path, *scores: int) -> None:
    for value in scores:
        ledger.append({"at": f"2026-11-01T00:00:{value:02d}+00:00", "kind": "code",
                       "agent": "opencode", "model": f"m{value}", "cost": "free",
                       "outcome": "pass", "score": value})


def _switch_home(tmp_path: Path, monkeypatch, name: str) -> Path:
    home = tmp_path / name
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("CREW_CHIEF_HOME", str(home))
    return home


def test_sync_not_set_up_prints_message_and_creates_no_git(capsys):
    assert main(["sync"]) == 0
    assert capsys.readouterr().out == "sync is not set up: ACC sync --init <url of a private repo>\n"
    assert not ledger.ledger_path().parent.joinpath(".git").exists()


def test_sync_init_pushes_and_second_machine_combines(tmp_path: Path, monkeypatch, capsys):
    url = _remote(tmp_path)
    home_a = _switch_home(tmp_path, monkeypatch, "a")
    _home_with_grades(home_a, 80, 70)
    (home_a / "models.toml").write_text("[x]\n", encoding="utf-8")
    assert main(["sync", "--init", url]) == 0
    capsys.readouterr()
    show = subprocess.run(["git", "--git-dir", str(tmp_path / "remote.git"),
                           "show", "main:ledger.jsonl"], capture_output=True, text=True)
    assert show.returncode == 0
    assert len(show.stdout.splitlines()) == 2
    for name in ("ledger.jsonl", ".gitattributes", ".gitignore"):
        ls = subprocess.run(["git", "--git-dir", str(tmp_path / "remote.git"),
                             "show", f"main:{name}"], capture_output=True, text=True)
        assert ls.returncode == 0, name
    assert b"models.toml" not in subprocess.run(
        ["git", "--git-dir", str(tmp_path / "remote.git"), "ls-tree", "-r", "--name-only", "main"],
        capture_output=True).stdout

    home_b = _switch_home(tmp_path, monkeypatch, "b")
    _home_with_grades(home_b, 60)
    assert main(["sync", "--init", url]) == 0
    capsys.readouterr()
    scores_b = sorted(e["score"] for e in ledger.read())
    assert scores_b == [60, 70, 80]

    monkeypatch.setenv("CREW_CHIEF_HOME", str(home_a))
    assert main(["sync"]) == 0
    capsys.readouterr()
    assert sorted(e["score"] for e in ledger.read()) == [60, 70, 80]


def test_sync_concurrent_appends_converge_without_conflict(tmp_path: Path, monkeypatch, capsys):
    url = _remote(tmp_path)
    home_a = _switch_home(tmp_path, monkeypatch, "a")
    _home_with_grades(home_a, 80)
    assert main(["sync", "--init", url]) == 0
    capsys.readouterr()
    home_b = _switch_home(tmp_path, monkeypatch, "b")
    _home_with_grades(home_b, 81)
    assert main(["sync", "--init", url]) == 0
    capsys.readouterr()

    monkeypatch.setenv("CREW_CHIEF_HOME", str(home_a))
    ledger.append({"at": "2026-11-03T00:00:00+00:00", "kind": "code", "agent": "opencode",
                   "model": "new-a", "cost": "free", "outcome": "pass", "score": 91})
    assert main(["sync"]) == 0
    assert capsys.readouterr().out.startswith("synced ")
    monkeypatch.setenv("CREW_CHIEF_HOME", str(home_b))
    ledger.append({"at": "2026-11-03T01:00:00+00:00", "kind": "code", "agent": "opencode",
                   "model": "new-b", "cost": "free", "outcome": "pass", "score": 92})
    assert main(["sync"]) == 0
    assert capsys.readouterr().out.startswith("synced ")
    monkeypatch.setenv("CREW_CHIEF_HOME", str(home_a))
    assert main(["sync"]) == 0
    capsys.readouterr()

    for home in (home_a, home_b):
        lines = (home / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
        assert "<<<" not in (home / "ledger.jsonl").read_text(encoding="utf-8")
        assert len(lines) == len(set(lines))
        monkeypatch.setenv("CREW_CHIEF_HOME", str(home))
        assert sorted(e["score"] for e in ledger.read()) == [80, 81, 91, 92]


def test_sync_rerun_is_idempotent(tmp_path: Path, monkeypatch, capsys):
    url = _remote(tmp_path)
    home_a = _switch_home(tmp_path, monkeypatch, "a")
    _home_with_grades(home_a, 80)
    assert main(["sync", "--init", url]) == 0
    before = (home_a / "ledger.jsonl").read_bytes()
    capsys.readouterr()
    assert main(["sync"]) == 0
    assert capsys.readouterr().out.startswith("synced ")
    assert (home_a / "ledger.jsonl").read_bytes() == before


def test_sync_broken_remote_exits_1_and_keeps_grades(tmp_path: Path, monkeypatch, capsys):
    url = _remote(tmp_path)
    home_a = _switch_home(tmp_path, monkeypatch, "a")
    _home_with_grades(home_a, 80, 70)
    assert main(["sync", "--init", url]) == 0
    capsys.readouterr()
    bad = str(tmp_path / "missing.git")
    subprocess.run(["git", "-C", str(home_a), "remote", "set-url", "origin", bad], check=True)
    before = (home_a / "ledger.jsonl").read_bytes()
    assert main(["sync"]) == 1
    assert capsys.readouterr().err
    assert (home_a / "ledger.jsonl").read_bytes() == before
    assert sorted(e["score"] for e in ledger.read()) == [70, 80]


def test_sync_init_with_different_url_exits_2(tmp_path: Path, monkeypatch, capsys):
    url = _remote(tmp_path)
    home_a = _switch_home(tmp_path, monkeypatch, "a")
    _home_with_grades(home_a, 80)
    assert main(["sync", "--init", url]) == 0
    capsys.readouterr()
    assert main(["sync", "--init", str(tmp_path / "other.git")]) == 2
    assert url in capsys.readouterr().err


def test_sync_conflicting_gitattributes_aborts_merge_and_keeps_grades(
        tmp_path: Path, monkeypatch, capsys):
    url = _remote(tmp_path)
    home_a = _switch_home(tmp_path, monkeypatch, "a")
    _home_with_grades(home_a, 80)
    assert main(["sync", "--init", url]) == 0
    capsys.readouterr()

    home_b = _switch_home(tmp_path, monkeypatch, "b")
    _home_with_grades(home_b, 81)
    assert main(["sync", "--init", url]) == 0
    capsys.readouterr()

    monkeypatch.setenv("CREW_CHIEF_HOME", str(home_a))
    with (home_a / ".gitattributes").open("a", encoding="utf-8") as handle:
        handle.write("# a\n")
    _home_with_grades(home_a, 82)
    assert main(["sync"]) == 0
    capsys.readouterr()

    monkeypatch.setenv("CREW_CHIEF_HOME", str(home_b))
    with (home_b / ".gitattributes").open("a", encoding="utf-8") as handle:
        handle.write("# b\n")
    _home_with_grades(home_b, 83)
    assert main(["sync"]) == 1
    err = capsys.readouterr().err
    assert "CONFLICT" in err
    assert not (home_b / ".git" / "MERGE_HEAD").exists()
    assert "<<<<<<<" not in (home_b / "ledger.jsonl").read_text(encoding="utf-8")
    assert sorted(e["score"] for e in ledger.read()) == [80, 81, 83]


def test_git_disables_terminal_prompt_and_detaches_stdin(monkeypatch, tmp_path: Path):
    from crew_chief import sync as syncmod

    captured: dict = {}

    class _Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kwargs):
        captured.update(kwargs)
        return _Result()

    monkeypatch.setattr(syncmod.subprocess, "run", fake_run)
    syncmod._git(tmp_path, "status")
    assert captured["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert captured["stdin"] == subprocess.DEVNULL
