"""Tests for rulings in the job and ADRs in ``docs/decisions``."""

from __future__ import annotations

import json
from datetime import date

import pytest

from crew_chief import records
from crew_chief.cli import main
from crew_chief.decisions import next_number, write_adr


def _new_job(repo, title="Export CSV") -> str:
    request = repo / "request.md"
    request.write_text("do it", encoding="utf-8")
    assert main(["job", "new", "--project", str(repo), "--title", title,
                 "--request-file", str(request)]) == 0
    return records.jobs_dir(repo).glob("*.json").__next__().stem


def test_next_number_and_write_adr(tmp_path):
    assert next_number(tmp_path) == 1
    p = write_adr(tmp_path, 1, "CSV uses semicolon", "ctx", "dec", "alt", "cons",
                  "job J1", date(2026, 10, 19))
    assert p.name == "0001-csv-uses-semicolon.md"
    text = p.read_text(encoding="utf-8")
    assert text.startswith("# 0001. CSV uses semicolon\n")
    assert "- Status: accepted" in text and "- Date: 2026-10-19" in text
    assert "- Origin: job J1" in text
    for section in ("## Context", "## Decision", "## Alternatives considered",
                    "## Consequences"):
        assert section in text
    assert next_number(tmp_path) == 2


def test_next_number_uses_highest_and_ignores_other_files(tmp_path):
    folder = tmp_path / "docs" / "decisions"
    folder.mkdir(parents=True)
    (folder / "0003-x.md").write_text("x", encoding="utf-8")
    (folder / "notes.md").write_text("x", encoding="utf-8")
    (folder / "README.md").write_text("x", encoding="utf-8")
    assert next_number(tmp_path) == 4


def test_write_adr_refuses_existing_number(tmp_path):
    write_adr(tmp_path, 1, "A", "c", "d", "a", "q", "job J", date(2026, 10, 19))
    with pytest.raises(FileExistsError):
        write_adr(tmp_path, 1, "B", "c", "d", "a", "q", "job J", date(2026, 10, 19))


def test_add_ruling_numbers_from_one():
    job: dict = {}
    assert records.add_ruling(job, "a — b — c") == 1
    assert records.add_ruling(job, "d — e — f") == 2
    assert job["rulings"][0]["promoted"] is None
    assert job["rulings"][1]["text"] == "d — e — f"
    assert job["rulings"][1]["at"]


def test_ruling_then_decision_from_ruling_cli(git_repo, capsys):
    job_id = _new_job(git_repo)
    capsys.readouterr()
    text = "Use semicolon — Excel in pt-BR needs it — re-export"
    assert main(["ruling", "add", "--project", str(git_repo), "--job", job_id,
                 "--text", text]) == 0
    assert capsys.readouterr().out.strip() == "ruling 1 recorded"
    assert main(["decision", "add", "--project", str(git_repo), "--job", job_id,
                 "--from-ruling", "1", "--context", "ctx",
                 "--alternatives", "comma", "--consequences", "cons"]) == 0
    out = capsys.readouterr().out.strip()
    assert out.replace("\\", "/").endswith("docs/decisions/0001-use-semicolon.md")
    job = records.load_job(git_repo, job_id)
    assert job["rulings"][0]["promoted"] == "0001"
    assert job["decisions"] == ["0001"]
    adr = next((git_repo / "docs" / "decisions").glob("0001-*.md"))
    body = adr.read_text(encoding="utf-8")
    assert text in body
    assert f"- Origin: job {job_id}" in body
    assert "ctx" in body and "comma" in body and "cons" in body


def test_decision_add_with_title_and_spec_dir_origin(git_repo, capsys):
    job_id = _new_job(git_repo)
    records.update_job(git_repo, job_id, lambda j: j.__setitem__("spec_dir", "specs/001-x"))
    capsys.readouterr()
    assert main(["decision", "add", "--project", str(git_repo), "--job", job_id,
                 "--title", "Pick SQLite", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["number"] == "0001"
    body = (git_repo / payload["path"]).read_text(encoding="utf-8")
    assert body.startswith("# 0001. Pick SQLite\n")
    assert f"- Origin: job {job_id} · specs/001-x" in body
    assert records.load_job(git_repo, job_id)["decisions"] == ["0001"]


def test_decision_add_errors_write_nothing(git_repo, capsys):
    job_id = _new_job(git_repo)
    capsys.readouterr()
    assert main(["decision", "add", "--project", str(git_repo), "--job", job_id,
                 "--from-ruling", "7"]) == 2
    assert not (git_repo / "docs" / "decisions").exists()
    assert main(["decision", "add", "--project", str(git_repo), "--job", job_id]) == 2
    assert main(["decision", "add", "--project", str(git_repo), "--job", job_id,
                 "--title", "T", "--from-ruling", "1"]) == 2
    assert not (git_repo / "docs" / "decisions").exists()


def test_decision_add_refuses_ruling_already_promoted(git_repo, capsys):
    job_id = _new_job(git_repo)
    main(["ruling", "add", "--project", str(git_repo), "--job", job_id, "--text", "x — y — z"])
    assert main(["decision", "add", "--project", str(git_repo), "--job", job_id,
                 "--from-ruling", "1"]) == 0
    capsys.readouterr()
    assert main(["decision", "add", "--project", str(git_repo), "--job", job_id,
                 "--from-ruling", "1"]) == 2
    assert "already" in capsys.readouterr().err
    assert len(list((git_repo / "docs" / "decisions").glob("*.md"))) == 1


def test_job_markdown_shows_rulings_and_decisions(git_repo):
    job_id = _new_job(git_repo)
    main(["ruling", "add", "--project", str(git_repo), "--job", job_id, "--text", "first — why — cost"])
    main(["ruling", "add", "--project", str(git_repo), "--job", job_id, "--text", "second — why — cost"])
    main(["decision", "add", "--project", str(git_repo), "--job", job_id, "--from-ruling", "1"])
    md = records.render_markdown(records.load_job(git_repo, job_id))
    assert "## Rulings" in md and "## Decisions" in md
    assert "1. first — why — cost → ADR 0001" in md
    assert "2. second — why — cost\n" in md
    assert "- ADR 0001" in md


def test_old_job_markdown_has_no_rulings_or_decisions_sections():
    md = records.render_markdown({"id": "j", "title": "T", "request": "r", "pieces": []})
    assert "## Rulings" not in md and "## Decisions" not in md
