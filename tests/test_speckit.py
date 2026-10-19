"""Tests for the spec-kit tasks.md parser and checkbox marker."""

from __future__ import annotations

import pytest

from crew_chief.speckit import mark_done, parse_tasks

SAMPLE = (
    "# Tasks\n\n## Phase 1: Setup\n"
    "- [ ] T001 Create project structure\n"
    "- [X] T002 [P] Configure linting in .ruff.toml\n\n"
    "## Phase 3: User Story 1\n"
    "- [ ] T012 [P] [US1] Create User model in src/models/user.py\n"
    "- [ ] T013 [US1] Implement UserService in src/services/user.py (depends on T012)\n"
    "Notes: not a task\n"
)


def test_parse_tasks_reads_ids_flags_story_phase_and_depends():
    tasks = parse_tasks(SAMPLE)
    assert [t.id for t in tasks] == ["T001", "T002", "T012", "T013"]
    assert tasks[1].done is True and tasks[1].parallel is True
    assert tasks[2].story == "US1" and tasks[2].phase == "Phase 3: User Story 1"
    assert tasks[3].depends == ("T012",)
    assert tasks[0].phase == "Phase 1: Setup" and tasks[0].story is None


def test_mark_done_changes_only_that_line_and_keeps_crlf_and_bom():
    text = "\ufeff" + SAMPLE.replace("\n", "\r\n")
    out = mark_done(text, "T012")
    assert out.startswith("\ufeff")
    assert "- [X] T012 [P] [US1] Create User model in src/models/user.py\r\n" in out
    assert "- [ ] T013 [US1]" in out          # other lines intact
    assert out.replace("- [X] T012", "- [ ] T012") == text   # only that line changed


def test_mark_done_unknown_task_raises_keyerror():
    with pytest.raises(KeyError):
        mark_done(SAMPLE, "T999")


def test_parse_tasks_accepts_lowercase_x_and_flags_in_any_order():
    tasks = parse_tasks("- [x] T005 [US2] [P] Do it\n")
    assert tasks[0].done and tasks[0].parallel and tasks[0].story == "US2"


def test_parse_tasks_handles_crlf_bom_and_reports_zero_based_line():
    tasks = parse_tasks("\ufeff" + SAMPLE.replace("\n", "\r\n"))
    assert [t.line for t in tasks] == [3, 4, 7, 8]
    assert tasks[0].text == "Create project structure"
    assert tasks[2].text == "Create User model in src/models/user.py"


def test_parse_tasks_depends_variants_and_no_phase():
    tasks = parse_tasks(
        "- [ ] T004 Base\n"
        "- [ ] T007 Other\n"
        "- [ ] T008 A depends on T004, T007\n"
        "- [ ] T009 B (after T003)\n"
    )
    assert tasks[0].phase == ""
    assert tasks[2].depends == ("T004", "T007")
    assert tasks[3].depends == ("T003",)


def test_mark_done_already_done_returns_same_text():
    assert mark_done(SAMPLE, "T002") == SAMPLE


def test_mark_done_keeps_lf_files_lf_and_accented_text():
    text = "## Fase 1\n- [ ] T001 Criar configuração\n- [ ] T002 Outra\n"
    out = mark_done(text, "T001")
    assert out == "## Fase 1\n- [X] T001 Criar configuração\n- [ ] T002 Outra\n"
