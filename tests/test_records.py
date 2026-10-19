"""Tests for the job record model, Markdown rendering and summaries."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import pytest

from crew_chief.records import (
    RISKS,
    JobLockTimeout,
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
    update_job,
    write_atomic,
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


def test_correction_and_escalation_timestamps_are_utc(tmp_path):
    job = _new_job(tmp_path)
    piece = add_piece(job, "A", "code", "low")

    add_correction(job, piece, "missed the docstring", executor="opencode:m")
    add_escalation(job, piece, "two free failures", "codex:g")

    assert job["pieces"][0]["corrections"][0]["at"].endswith("+00:00")
    assert job["pieces"][0]["escalations"][0]["at"].endswith("+00:00")


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


# --- update_job: atomic read-modify-write under a cross-process lock -------

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "skill" / "crew-chief" / "scripts"

_APPEND_SCRIPT = """
import sys, time
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from crew_chief.records import update_job

def add(job):
    piece = job["pieces"][0]
    seen = len(piece.setdefault("picks", []))
    time.sleep(0.05)  # widen the read-modify-write window
    piece["picks"].append({"pick": sys.argv[4], "seen": seen})

update_job(Path(sys.argv[2]), sys.argv[3], add)
"""


def test_update_job_applies_fn_and_saves(tmp_path):
    job = _new_job(tmp_path)
    add_piece(job, "Piece", "code", "low")
    save_job(tmp_path / "project", job)

    def rename(current):
        current["title"] = "Novo título"
        return "done"

    result = update_job(tmp_path / "project", job["id"], rename)

    assert result == "done"
    assert load_job(tmp_path / "project", job["id"])["title"] == "Novo título"
    assert "Novo título" in (
        jobs_dir(tmp_path / "project") / f"{job['id']}.md"
    ).read_text(encoding="utf-8")
    assert not (jobs_dir(tmp_path / "project") / f"{job['id']}.lock").exists()


def test_update_job_does_not_save_when_fn_raises(tmp_path):
    job = _new_job(tmp_path)
    project = tmp_path / "project"

    def boom(current):
        current["title"] = "changed"
        raise ValueError("nope")

    with pytest.raises(ValueError):
        update_job(project, job["id"], boom)

    assert load_job(project, job["id"])["title"] == job["title"]
    assert not (jobs_dir(project) / f"{job['id']}.lock").exists()


def test_update_job_parallel_processes_lose_nothing(tmp_path):
    import subprocess
    import sys

    job = _new_job(tmp_path)
    add_piece(job, "Piece", "code", "low")
    project = tmp_path / "project"
    save_job(project, job)

    procs = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                _APPEND_SCRIPT,
                str(_SCRIPTS_DIR),
                str(project),
                job["id"],
                f"worker-{index}",
            ]
        )
        for index in range(8)
    ]
    assert [proc.wait(timeout=120) for proc in procs] == [0] * 8

    picks = load_job(project, job["id"])["pieces"][0]["picks"]
    assert sorted(p["pick"] for p in picks) == sorted(
        f"worker-{index}" for index in range(8)
    )
    assert sorted(p["seen"] for p in picks) == list(range(8))


def test_update_job_times_out_with_a_clear_error(tmp_path):
    job = _new_job(tmp_path)
    project = tmp_path / "project"
    lock = jobs_dir(project) / f"{job['id']}.lock"
    lock.write_text("12345\n", encoding="utf-8")

    with pytest.raises(JobLockTimeout) as info:
        update_job(project, job["id"], lambda current: None, timeout_s=0.3)

    assert str(lock) in str(info.value)
    assert isinstance(info.value, RecordError)
    assert lock.exists()


def test_update_job_breaks_a_stale_lock(tmp_path):
    import os
    import time

    job = _new_job(tmp_path)
    project = tmp_path / "project"
    lock = jobs_dir(project) / f"{job['id']}.lock"
    lock.write_text("12345\n", encoding="utf-8")
    old = time.time() - 11 * 60
    os.utime(lock, (old, old))

    update_job(
        project, job["id"], lambda current: current.update(title="x"), timeout_s=2
    )

    assert load_job(project, job["id"])["title"] == "x"
    assert not lock.exists()


def test_write_atomic_retries_a_replace_refused_by_a_reader(tmp_path, monkeypatch):
    import crew_chief.records as records_module

    real_replace = os.replace
    calls = {"n": 0}

    def flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError(13, "Access is denied")
        real_replace(src, dst)

    monkeypatch.setattr(records_module.os, "replace", flaky_replace)
    target = tmp_path / "file.json"

    write_atomic(target, "{}\n")

    assert target.read_text(encoding="utf-8") == "{}\n"
    assert calls["n"] == 3
    assert list(tmp_path.iterdir()) == [target]


def test_load_job_retries_a_read_refused_during_a_replace(tmp_path, monkeypatch):
    job = _new_job(tmp_path)
    real_read_text = Path.read_text
    calls = {"n": 0}

    def flaky_read_text(self, *args, **kwargs):
        if self.suffix == ".json":
            calls["n"] += 1
            if calls["n"] < 3:
                raise PermissionError(13, "Permission denied")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", flaky_read_text)

    assert load_job(tmp_path / "project", job["id"])["id"] == job["id"]
    assert calls["n"] == 3
