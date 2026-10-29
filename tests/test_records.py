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
    job_lock,
    load_job,
    new_job,
    piece_grades,
    render_markdown,
    save_job,
    set_grade,
    slug,
    summaries,
    update_job,
    validate_job_id,
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


def test_load_job_rejects_outside_or_traversal_job_id(tmp_path):
    """load_job must reject absolute and traversal job_ids."""
    import json
    project = tmp_path / "project"
    (project / "docs" / "crew-chief" / "jobs").mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"id": "outside", "title": "secret"}))

    with pytest.raises(RecordError):
        load_job(project, str(outside.with_suffix("")))

    with pytest.raises(RecordError):
        load_job(project, "../../../../outside")

    with pytest.raises(RecordError):
        load_job(project, "foo/bar")

    with pytest.raises(RecordError):
        load_job(project, "foo\\bar")

    with pytest.raises(RecordError):
        load_job(project, "")


def test_validate_job_id_and_save_job(tmp_path):
    """validate_job_id and save_job must enforce strict pattern and containment."""
    assert validate_job_id("job-1") == "job-1"
    assert validate_job_id("20261027-1200-test_job") == "20261027-1200-test_job"

    for invalid in ["", "/abs", "../rel", "a/b", "a\\b", "job.json", "job space", "job\0null"]:
        with pytest.raises(RecordError):
            validate_job_id(invalid)

    project = tmp_path / "project"
    with pytest.raises(RecordError):
        save_job(project, {"id": "../evil", "title": "evil"})

    with pytest.raises(RecordError):
        with job_lock(project, "../evil"):
            pass




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
    assert job["pieces"][0]["grades"] == {}


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


def _stored_grade(executor: str, score: int) -> dict:
    return {"score": score, "explain": f"explain {score}", "executor": executor,
            "graded_at": "2026-10-17T12:00:00+00:00"}


def test_grades_are_kept_per_executor(tmp_path):
    job = _new_job(tmp_path)
    piece = add_piece(job, "A", "code", "low")

    set_grade(job, piece, _stored_grade("beam:m", 52))
    set_grade(job, piece, _stored_grade("minimax:m", 100))
    set_grade(job, piece, _stored_grade("beam:m", 60))

    grades = piece_grades(job["pieces"][0])
    assert list(grades) == ["beam:m", "minimax:m"]
    assert grades["beam:m"]["score"] == 60
    assert grades["minimax:m"]["score"] == 100


def test_legacy_single_grade_is_read_and_kept_on_regrade(tmp_path):
    job = _new_job(tmp_path)
    piece = add_piece(job, "A", "code", "low")
    entry = job["pieces"][0]
    entry.pop("grades")
    entry["grade"] = _stored_grade("beam:m", 52)

    assert piece_grades(entry) == {"beam:m": _stored_grade("beam:m", 52)}

    set_grade(job, piece, _stored_grade("minimax:m", 100))

    assert "grade" not in entry
    assert piece_grades(entry) == {
        "beam:m": _stored_grade("beam:m", 52),
        "minimax:m": _stored_grade("minimax:m", 100),
    }


def test_summaries_and_markdown_list_every_graded_executor(tmp_path):
    job = _new_job(tmp_path)
    piece = add_piece(job, "Centre the card", "code", "low")
    add_attempt(job, piece, {"executor": "beam:m", "role": "exec", "status": "ok"})
    add_attempt(job, piece, {"executor": "minimax:m", "role": "exec",
                             "status": "ok"})
    set_grade(job, piece, _stored_grade("beam:m", 52))
    set_grade(job, piece, _stored_grade("minimax:m", 100))

    commit, comment = summaries(job)
    md = render_markdown(job)

    line = "- Centre the card — minimax:m, score 100; also beam:m, score 52"
    assert line in commit
    assert "1. Centre the card — minimax:m, score 100; also beam:m, score 52" in (
        comment
    )
    assert "- `minimax:m`: score 100 — explain 100" in md
    assert "- `beam:m`: score 52 — explain 52" in md


def test_render_markdown_names_the_base_of_a_correction(tmp_path):
    job = _new_job(tmp_path)
    piece = add_piece(job, "A", "code", "low")
    add_attempt(job, piece, {"executor": "beam:m", "role": "exec", "status": "ok",
                             "branch": "crew/j-1-1", "base": None})
    add_attempt(job, piece, {"executor": "minimax:m", "role": "exec",
                             "status": "ok", "branch": "crew/j-1-2",
                             "base": {"attempt": 1, "branch": "crew/j-1-1"}})

    md = render_markdown(job)

    first, second = [line for line in md.splitlines() if "- attempt " in line]
    assert "from `" not in first
    assert "from `crew/j-1-1` (attempt 1)" in second


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


def test_update_job_releases_a_lock_whose_removal_is_refused_once(
    tmp_path, monkeypatch
):
    # On Windows an antivirus or indexer briefly opening the fresh lock file
    # makes its removal fail; a lock left behind stalls every other process
    # for the whole lock timeout.
    job = _new_job(tmp_path)
    project = tmp_path / "project"
    lock = jobs_dir(project) / f"{job['id']}.lock"
    real_unlink = Path.unlink
    calls = {"n": 0}

    def flaky_unlink(self, *args, **kwargs):
        if self.suffix == ".lock":
            calls["n"] += 1
            if calls["n"] < 3:
                raise PermissionError(13, "Access is denied")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)

    update_job(project, job["id"], lambda current: current.update(title="x"))

    assert not lock.exists()
    assert calls["n"] == 3
    assert load_job(project, job["id"])["title"] == "x"


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


def test_attempt_line_shows_steps_and_tokens(tmp_path):
    job = _new_job(tmp_path)
    add_piece(job, "Piece", "code", "low")
    add_attempt(job, 1, {
        "executor": "opencode:a", "role": "exec", "status": "ok",
        "duration_s": 42.0, "steps": 7, "tokens_in": 12000, "tokens_out": 345,
        "tokens_total": 12345, "tokens_estimated": False,
    })
    add_attempt(job, 1, {
        "executor": "cursor:b", "role": "exec", "status": "ok",
        "duration_s": 3.0, "steps": None, "tokens_in": 100, "tokens_out": 20,
        "tokens_total": 120, "tokens_estimated": True,
    })

    text = render_markdown(job)

    assert "42.0s, 7 steps, 12345 tokens (in 12000 / out 345)" in text
    assert "3.0s, ~120 tokens (estimated)" in text


def test_add_piece_keeps_ref(tmp_path):
    job = _new_job(tmp_path)
    with_ref = add_piece(job, "t", "code", "low", ref="T012")
    without = add_piece(job, "u", "code", "low")
    assert job["pieces"][with_ref - 1]["ref"] == "T012"
    assert "ref" not in job["pieces"][without - 1]


def test_mark_accepted_records_attempt_branch_and_time(tmp_path):
    from crew_chief.records import mark_accepted

    job = _new_job(tmp_path)
    number = add_piece(job, "t", "code", "low")
    piece = job["pieces"][number - 1]
    assert "accepted" not in piece
    mark_accepted(job, number, {"n": 2, "branch": "crew/x-2"})
    assert piece["accepted"]["attempt"] == 2
    assert piece["accepted"]["branch"] == "crew/x-2"
    assert datetime.fromisoformat(piece["accepted"]["at"]).tzinfo is not None
    with pytest.raises(ValueError):
        mark_accepted(job, 99, {"n": 1, "branch": "b"})


def test_piece_for_ref_ignores_stale_pieces(tmp_path):
    from crew_chief.records import piece_for_ref

    job = _new_job(tmp_path)
    old = add_piece(job, "T012 old", "code", "low", ref="T012")
    assert piece_for_ref(job, "T012")["n"] == old
    assert piece_for_ref(job, "T999") is None
    job["pieces"][old - 1]["stale"] = True
    assert piece_for_ref(job, "T012") is None
    new = add_piece(job, "T012 new", "code", "low", ref="T012")
    assert piece_for_ref(job, "T012")["n"] == new


def test_write_atomic_keeps_text_byte_for_byte(tmp_path):
    target = tmp_path / "f.md"
    text = chr(0xFEFF) + "a\r\nb\nc\r\n"
    write_atomic(target, text)
    assert target.read_bytes() == text.encode("utf-8")


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_write_atomic_keeps_the_mode_of_an_existing_file(tmp_path):
    target = tmp_path / "tasks.md"
    target.write_text("old\n", encoding="utf-8")
    target.chmod(0o644)
    write_atomic(target, "new\n")
    assert (target.stat().st_mode & 0o777) == 0o644


def test_team_used_records_assignment_origin(git_repo, fake_bin, monkeypatch):
    """Test team_used audit trail recording across interview start and pick."""
    import shutil
    from crew_chief import cli, interview_service, records

    repo = git_repo
    fake_bin("stub", "print('stub-output')\n")

    def _only_bins(names: set[str]) -> None:
        real_which = shutil.which

        def which(cmd, *args, **kwargs):
            return real_which(cmd, *args, **kwargs) if cmd in names else None

        monkeypatch.setattr(shutil, "which", which)

    fake_home = repo.parent / "fake_home"
    fake_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(fake_home))
    empty_global = fake_home / "empty_global.toml"
    empty_global.write_text("", encoding="utf-8")
    monkeypatch.setenv("AGENT_CREW_CHIEF_GLOBAL_PREFS", str(empty_global))
    monkeypatch.delenv("CREW_CHIEF_PREFERENCES", raising=False)
    _only_bins({"stub"})

    # Set up preferences with [team] table
    prefs_text = """
[[tools]]
name = "stub"
kind = "generic"
bin = "stub"
models = ["m", "n"]
run_cmd = ["stub", "{prompt}"]

[[models]]
match = "stub:*"
cost = "free"

[team]
coordinators = ["claude:opus", "codex:preview"]
executors = ["stub:m"]
reviewers = ["stub:n"]
"""
    prefs_dir = repo / "docs" / "crew-chief"
    prefs_dir.mkdir(parents=True, exist_ok=True)
    (prefs_dir / "preferences.toml").write_text(prefs_text, encoding="utf-8")

    # 1. Interview start with a [team] coordinator from project prefs
    start_payload = interview_service.start_interview_job(
        project=str(repo),
        request="Test team used recording",
        caller_answers={
            "project": str(repo),
            "request": "Test team used recording",
            "executors": "free_first",
            "end_mode": "pr",
        },
    )
    job_id = start_payload["job_id"]
    job = records.load_job(repo, job_id)

    assert "team_used" in job
    assert len(job["team_used"]) == 1
    assert job["team_used"][0]["role"] == "coordinator"
    assert job["team_used"][0]["id"] == "claude:opus"
    assert job["team_used"][0]["origin"] == "project_prefs"
    assert "at" in job["team_used"][0]

    # 2. Pick with a project [team] allowlist
    records.add_piece(job, "Test piece", "code", "low")
    records.save_job(repo, job)

    ret = cli.main(["pick", "--project", str(repo), "--job", job_id, "--piece", "1"])
    assert ret == 0

    ret_rev = cli.main(["pick", "--project", str(repo), "--job", job_id, "--piece", "1", "--review"])
    assert ret_rev == 0

    job = records.load_job(repo, job_id)
    assert len(job["team_used"]) == 3
    assert job["team_used"][1]["role"] == "executor"
    assert job["team_used"][1]["id"] == "stub:m"
    assert job["team_used"][1]["origin"] == "project_team"
    assert job["team_used"][1]["piece"] == 1
    assert "at" in job["team_used"][1]

    assert job["team_used"][2]["role"] == "reviewer"
    assert job["team_used"][2]["id"] == "stub:n"
    assert job["team_used"][2]["origin"] == "project_team"
    assert job["team_used"][2]["piece"] == 1
    assert "at" in job["team_used"][2]

    # 4. Old job records without team_used must still load
    old_job_dict = {
        "id": "old-job-1",
        "title": "Old Job",
        "request": "Legacy",
        "created": "2026-01-01T00:00:00",
        "distributed": False,
        "reason": "old",
        "pieces": [],
    }
    old_job_path = records.jobs_dir(repo) / "old-job-1.json"
    records.write_atomic(old_job_path, json.dumps(old_job_dict))
    loaded_old = records.load_job(repo, "old-job-1")
    assert loaded_old["team_used"] == []

    # 5. Job status/result must expose team_used
    status_res = interview_service.get_interview_status(project=repo, job_id=job_id)
    assert status_res["ok"] is True
    assert status_res["team_used"] == job["team_used"]

    # 6. Test discovery origin when no [team] allowlist
    repo_no_team = repo.parent / "repo_no_team"
    repo_no_team.mkdir()
    pdir2 = repo_no_team / "docs" / "crew-chief"
    pdir2.mkdir(parents=True)
    prefs_no_team = """
[[tools]]
name = "stub"
kind = "generic"
bin = "stub"
models = ["m"]
run_cmd = ["stub", "{prompt}"]

[[models]]
match = "stub:*"
cost = "free"
"""
    (pdir2 / "preferences.toml").write_text(prefs_no_team, encoding="utf-8")
    job_disc = records.new_job(repo_no_team, "Job discovery", "req", False, "test")
    records.add_piece(job_disc, "Piece 1", "code", "low")
    records.save_job(repo_no_team, job_disc)
    ret_disc = cli.main(["pick", "--project", str(repo_no_team), "--job", job_disc["id"], "--piece", "1"])
    assert ret_disc == 0
    loaded_disc = records.load_job(repo_no_team, job_disc["id"])
    assert len(loaded_disc["team_used"]) == 1
    assert loaded_disc["team_used"][0]["role"] == "executor"
    assert loaded_disc["team_used"][0]["origin"] == "discovery"
    assert loaded_disc["team_used"][0]["piece"] == 1
    assert loaded_disc["team_used"][0]["id"] == "stub:m"

    # 8. Test global_team origin when [team] is only in global preferences
    repo_gt = repo.parent / "repo_gt"
    repo_gt.mkdir()
    pdir_gt = repo_gt / "docs" / "crew-chief"
    pdir_gt.mkdir(parents=True)
    prefs_gt_proj = """
[[tools]]
name = "stub"
kind = "generic"
bin = "stub"
models = ["m", "n"]
run_cmd = ["stub", "{prompt}"]

[[models]]
match = "stub:*"
cost = "free"
"""
    (pdir_gt / "preferences.toml").write_text(prefs_gt_proj, encoding="utf-8")

    global_prefs_file = fake_home / "global_prefs.toml"
    global_prefs_file.write_text("""
[[tools]]
name = "stub"
kind = "generic"
bin = "stub"
models = ["m", "n"]
run_cmd = ["stub", "{prompt}"]

[[models]]
match = "stub:*"
cost = "free"

[team]
coordinators = ["codex:global-coord", "claude:global-coord2"]
executors = ["stub:m"]
reviewers = ["stub:n"]
""", encoding="utf-8")
    monkeypatch.setenv("AGENT_CREW_CHIEF_GLOBAL_PREFS", str(global_prefs_file))

    job_gt = records.new_job(repo_gt, "Job global team", "req", False, "test")
    records.add_piece(job_gt, "Piece 1", "code", "low")
    records.save_job(repo_gt, job_gt)

    # 8a. Pick executor with [team] from global prefs
    ret_gt_exec = cli.main(["pick", "--project", str(repo_gt), "--job", job_gt["id"], "--piece", "1"])
    assert ret_gt_exec == 0
    loaded_gt = records.load_job(repo_gt, job_gt["id"])
    assert len(loaded_gt["team_used"]) == 1
    assert loaded_gt["team_used"][0]["role"] == "executor"
    assert loaded_gt["team_used"][0]["id"] == "stub:m"
    assert loaded_gt["team_used"][0]["origin"] == "global_team"

    # 8b. Pick reviewer with [team] from global prefs
    ret_gt_rev = cli.main(["pick", "--project", str(repo_gt), "--job", job_gt["id"], "--piece", "1", "--review"])
    assert ret_gt_rev == 0
    loaded_gt = records.load_job(repo_gt, job_gt["id"])
    assert len(loaded_gt["team_used"]) == 2
    assert loaded_gt["team_used"][1]["role"] == "reviewer"
    assert loaded_gt["team_used"][1]["id"] == "stub:n"
    assert loaded_gt["team_used"][1]["origin"] == "global_team"
