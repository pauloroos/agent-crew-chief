"""Tests for the job record model, Markdown rendering and summaries."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from crew_chief.records import (
    RISKS,
    WORK_TYPES,
    RecordError,
    add_attempt,
    add_correction,
    add_escalation,
    add_piece,
    iter_jobs,
    jobs_dir,
    load_job,
    new_job,
    render_markdown,
    save_job,
    set_grade,
    slug,
    summaries,
)

NOW = datetime(2026, 10, 17, 14, 30)


def _new_job(tmp_path: Path, **overrides) -> dict:
    """Create a job in ``tmp_path/project`` with the fixed ``NOW`` clock."""
    kwargs = dict(
        title="Média ponderada",
        request="Implemente a média ponderada; não altere testes.",
        distributed=True,
        reason="chosen: best score",
        now=NOW,
    )
    kwargs.update(overrides)
    return new_job(tmp_path / "project", **kwargs)


# --- cases from the brief --------------------------------------------------


def test_new_job_writes_json_and_md(tmp_path):
    job = _new_job(tmp_path)
    directory = jobs_dir(tmp_path / "project")
    json_path = directory / f"{job['id']}.json"
    md_path = directory / f"{job['id']}.md"

    assert json_path.is_file()
    assert md_path.is_file()

    md = md_path.read_text(encoding="utf-8")
    assert "Implemente a média ponderada; não altere testes." in md

    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["request"] == job["request"]
    assert data["distributed"] is True


def test_id_collision_gets_suffix(tmp_path):
    first = _new_job(tmp_path)
    second = _new_job(tmp_path)

    assert second["id"] == f"{first['id']}-2"


def test_add_piece_rejects_unknown_type(tmp_path):
    job = _new_job(tmp_path)
    with pytest.raises(ValueError):
        add_piece(job, "Something", "banana", "low")


def test_summaries_list_each_piece(tmp_path):
    job = _new_job(tmp_path)
    first = add_piece(job, "Records module", "code", "low")
    add_attempt(
        job,
        first,
        {"executor": "opencode:m", "role": "exec", "status": "ok"},
    )
    set_grade(job, first, {"score": 90, "explain": "clean"})
    second = add_piece(job, "Records tests", "tests", "low")
    add_attempt(
        job,
        second,
        {"executor": "codex:g", "role": "exec", "status": "ok"},
    )
    set_grade(job, second, {"score": 70, "explain": "one extra"})

    commit, comment = summaries(job)

    assert commit.startswith("feat: ")  # first piece type code -> feat
    assert "Records module" in commit
    assert "Records tests" in commit
    assert "opencode:m" in commit
    assert "90" in commit

    assert "Records module" in comment
    assert "Records tests" in comment
    assert "opencode:m" in comment
    assert "Record: docs/crew-chief/jobs/" in comment


def test_iter_jobs_reports_corrupt_file(tmp_path):
    directory = jobs_dir(tmp_path / "project")
    directory.mkdir(parents=True)
    bad = directory / "x.json"
    bad.write_text("{not json", encoding="utf-8")

    items = list(iter_jobs(tmp_path / "project"))

    assert len(items) == 1
    job, path, error = items[0]
    assert job is None
    assert path == bad
    assert error


def test_load_job_missing_raises_recorderror_naming_file(tmp_path):
    with pytest.raises(RecordError) as excinfo:
        load_job(tmp_path / "project", "nope")

    assert "nope.json" in str(excinfo.value)


# --- vocabularies and validation ------------------------------------------


def test_vocabularies_match_the_global_constraints():
    assert WORK_TYPES == (
        "code",
        "tests",
        "refactor",
        "design",
        "docs",
        "research",
        "diagnosis",
        "data",
        "review",
    )
    assert RISKS == ("low", "medium", "high")


def test_add_piece_rejects_unknown_risk(tmp_path):
    job = _new_job(tmp_path)
    with pytest.raises(ValueError):
        add_piece(job, "Something", "code", "extreme")


def test_add_piece_numbers_from_one_and_returns_the_number(tmp_path):
    job = _new_job(tmp_path)

    assert add_piece(job, "A", "code", "low") == 1
    assert add_piece(job, "B", "tests", "high") == 2

    assert job["pieces"][0]["n"] == 1
    assert job["pieces"][1]["n"] == 2
    assert job["pieces"][0]["attempts"] == []
    assert job["pieces"][0]["corrections"] == []
    assert job["pieces"][0]["escalations"] == []
    assert job["pieces"][0]["grade"] is None


def test_add_attempt_validates_role_and_status(tmp_path):
    job = _new_job(tmp_path)
    piece = add_piece(job, "A", "code", "low")

    with pytest.raises(ValueError):
        add_attempt(job, piece, {"role": "boss", "status": "ok"})
    with pytest.raises(ValueError):
        add_attempt(job, piece, {"role": "exec", "status": "exploded"})


def test_add_attempt_stores_arbitrary_keys_verbatim(tmp_path):
    job = _new_job(tmp_path)
    piece = add_piece(job, "A", "code", "low")
    attempt = {
        "executor": "opencode:m",
        "role": "exec",
        "status": "ok",
        "cost": "free",
        "diffstat": [{"file": "a.py", "added": 3, "deleted": 1}],
    }

    add_attempt(job, piece, attempt)

    assert job["pieces"][0]["attempts"] == [attempt]


def test_add_correction_and_escalation_shapes(tmp_path):
    job = _new_job(tmp_path)
    piece = add_piece(job, "A", "code", "low")

    add_correction(job, piece, "missed the docstring", executor="opencode:m")
    add_escalation(job, piece, "two free failures", "codex:g")

    correction = job["pieces"][0]["corrections"][0]
    assert correction["reason"] == "missed the docstring"
    assert correction["executor"] == "opencode:m"
    assert correction["at"]

    escalation = job["pieces"][0]["escalations"][0]
    assert escalation["reason"] == "two free failures"
    assert escalation["to"] == "codex:g"
    assert escalation["at"]


# --- rendering and summaries ----------------------------------------------


def test_render_markdown_has_every_section(tmp_path):
    job = _new_job(tmp_path)
    piece = add_piece(job, "Records module", "code", "low")
    add_attempt(job, piece, {"executor": "opencode:m", "role": "exec", "status": "ok"})
    set_grade(job, piece, {"score": 90, "explain": "adherence 1/1 → 100"})

    md = render_markdown(job)

    for section in (
        "## Request",
        "## Decision",
        "## Pieces",
        "## Commit message",
        "## Issue comment",
    ):
        assert section in md
    assert "Records module" in md
    assert "90" in md
    assert "adherence 1/1 → 100" in md


def test_render_markdown_shows_corrections_and_escalations(tmp_path):
    job = _new_job(tmp_path)
    piece = add_piece(job, "A", "code", "low")
    add_correction(job, piece, "renamed the parameter")
    add_escalation(job, piece, "quota twice", "codex:g")

    md = render_markdown(job)

    assert "renamed the parameter" in md
    assert "quota twice" in md
    assert "codex:g" in md


@pytest.mark.parametrize(
    "work_type, commit_type",
    [
        ("code", "feat"),
        ("tests", "test"),
        ("refactor", "refactor"),
        ("design", "feat"),
        ("docs", "docs"),
        ("research", "docs"),
        ("diagnosis", "fix"),
        ("data", "feat"),
        ("review", "chore"),
    ],
)
def test_commit_type_comes_from_the_first_piece(tmp_path, work_type, commit_type):
    job = _new_job(tmp_path)
    add_piece(job, "P", work_type, "low")

    commit, _ = summaries(job)

    assert commit.startswith(f"{commit_type}: ")


def test_job_without_pieces_commits_as_chore(tmp_path):
    job = _new_job(tmp_path)

    commit, _ = summaries(job)

    assert commit.startswith("chore: ")


def test_issue_comment_lists_escalations_and_record_path(tmp_path):
    job = _new_job(tmp_path)
    piece = add_piece(job, "A", "code", "low")
    add_escalation(job, piece, "no free executor", "codex:g")

    _, comment = summaries(job)

    assert "no free executor" in comment
    assert f"Record: docs/crew-chief/jobs/{job['id']}.md" in comment


# --- slug, ids and atomic saves -------------------------------------------


def test_slug_folds_accents_and_punctuation():
    assert slug("Fix: Média ponderada (v2)!") == "fix-media-ponderada-v2"


def test_slug_is_never_empty():
    assert slug("!!!") == "job"
    assert slug("") == "job"


def test_id_uses_the_timestamp_and_a_slug_truncated_to_40(tmp_path):
    job = _new_job(tmp_path, title="a" * 80)

    assert job["id"] == f"20261017-1430-{'a' * 40}"


def test_save_job_leaves_no_temp_file(tmp_path):
    job = _new_job(tmp_path)
    save_job(tmp_path / "project", job)

    leftovers = [p.name for p in jobs_dir(tmp_path / "project").glob("*.tmp")]

    assert leftovers == []


def test_save_job_round_trips_accented_request(tmp_path):
    job = _new_job(tmp_path, request="revisão concluída ✓")

    reloaded = load_job(tmp_path / "project", job["id"])

    assert reloaded == job
