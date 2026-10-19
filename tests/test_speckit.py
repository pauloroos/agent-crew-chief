"""Tests for the spec-kit tasks.md parser and checkbox marker."""

from __future__ import annotations

import pytest

from crew_chief.speckit import mark_done, parse_spec_refs, parse_tasks, resolve_refs

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


SPEC = (
    "### User Story 1 - Export CSV (Priority: P1)\n\nAs a user...\n\n"
    "**Acceptance Scenarios**:\n\n"
    "1. **Given** rows exist, **When** I export, **Then** a CSV is saved\n"
    "2. **Given** no rows, **When** I export, **Then** I see an empty-state message\n\n"
    "### Functional Requirements\n\n"
    "- **FR-001**: System MUST export UTF-8 CSV\n"
    "- FR-002: System MUST use ';' as separator\n\n"
    "- **SC-001**: Export of 10k rows takes under 2 s\n"
)


def test_parse_spec_refs_indexes_stories_scenarios_requirements_and_criteria():
    idx = parse_spec_refs(SPEC)
    assert idx["US1"].startswith("Export CSV")
    assert "a CSV is saved" in idx["US1/AC1"]
    assert "empty-state" in idx["US1/AC2"]
    assert idx["FR-002"] == "System MUST use ';' as separator"
    assert "10k rows" in idx["SC-001"]


def test_resolve_refs_returns_texts_and_missing():
    found, missing = resolve_refs(["FR-001", "US1/AC9"], parse_spec_refs(SPEC))
    assert found == ["FR-001: System MUST export UTF-8 CSV"]
    assert missing == ["US1/AC9"]


def test_parse_spec_refs_numbers_scenarios_per_story_and_ignores_other_lists():
    text = (
        "### User Story 1 - First (Priority: P1)\n"
        "**Acceptance Scenarios**:\n"
        "1. **Given** a, **When** b, **Then** c\n\n"
        "### User Story 2 - Second (Priority: P2)\n"
        "Steps:\n1. Not a scenario\n"
        "**Acceptance Scenarios**:\n"
        "1. Given x, When y, Then z\n"
        "### Edge Cases\n"
        "1. **Given** stray, **When** it, **Then** is ignored\n"
    )
    idx = parse_spec_refs(text)
    assert "Given** a" in idx["US1/AC1"]
    assert idx["US2"] == "Second (Priority: P2)"
    assert idx["US2/AC1"] == "Given x, When y, Then z"
    assert "US2/AC2" not in idx and "US1/AC2" not in idx


def test_parse_spec_refs_handles_crlf_bom_accents_and_bold_variants():
    text = "﻿" + (
        "### User Story 1 - Exportação (Priority: P1)\n"
        "**Acceptance Scenarios**:\n"
        "1. **Given** linhas, **When** exporto, **Then** salvo\n"
        "- **FR-001:** Sistema DEVE exportar\n"
        "* SC-002: Tempo menor que 2 s\n"
    ).replace("\n", "\r\n")
    idx = parse_spec_refs(text)
    assert idx["US1"] == "Exportação (Priority: P1)"
    assert idx["US1/AC1"].endswith("salvo")
    assert idx["FR-001"] == "Sistema DEVE exportar"
    assert idx["SC-002"] == "Tempo menor que 2 s"


def test_parse_spec_refs_empty_text_gives_empty_index():
    assert parse_spec_refs("") == {}


def _task(text, phase="Phase 3: Story", parallel=False):
    from crew_chief.speckit import SpecTask

    return SpecTask("T020", False, parallel, None, text, phase, (), 0)


def test_guess_type_heuristics():
    from crew_chief.speckit import guess_type

    assert guess_type(_task("Add unit coverage in tests/test_a.py")) == "tests"
    assert guess_type(_task("Contract test for /users")) == "tests"
    assert guess_type(_task("Write the guide in docs/usage.md")) == "docs"
    assert guess_type(_task("Update README.md")) == "docs"
    assert guess_type(_task("Research CSV libraries")) == "research"
    assert guess_type(_task("Evaluate options for caching")) == "research"
    assert guess_type(_task("Create User model in src/user.py")) == "code"
    assert guess_type(_task("Pick the latest build")) == "code"


def test_guess_risk_heuristics():
    from crew_chief.speckit import guess_risk

    assert guess_risk(_task("Create model", phase="Phase 2: Foundational")) == "medium"
    assert guess_risk(_task("Write data-model.md notes")) == "high"
    assert guess_risk(_task("Add auth middleware")) == "high"
    assert guess_risk(_task("Implement contracts/users.yaml")) == "high"
    assert guess_risk(_task("Run migration", phase="Phase 2: Foundational")) == "high"
    assert guess_risk(_task("Create model", parallel=True)) == "low"
    assert guess_risk(_task("Create model", phase="Phase 2: Foundational", parallel=True)) == "medium"
    assert guess_risk(_task("Create model")) == "medium"


def test_readiness_reports_dependency_and_phase_blockers():
    from crew_chief.sidecar import Sidecar, TaskMeta
    from crew_chief.speckit import parse_tasks, readiness

    tasks = parse_tasks(
        "## Phase 1: Setup\n- [ ] T001 a\n- [ ] T002 b\n"
        "## Phase 2: Core\n- [ ] T003 c (depends on T001)\n- [ ] T004 d\n"
    )
    job = {"pieces": [
        {"n": 1, "ref": "T001", "accepted": {"attempt": 1}},
        {"n": 2, "ref": "T002"},
        {"n": 3, "ref": "T003"},
        {"n": 4, "ref": "T004"},
    ]}
    result = readiness(job, tasks, Sidecar(spec_dir="s"))
    assert result == {
        "T002": "",
        "T003": "phase 'Phase 1: Setup' incomplete",
        "T004": "phase 'Phase 1: Setup' incomplete",
    }
    job["pieces"][1]["accepted"] = {"attempt": 1}
    assert readiness(job, tasks, Sidecar(spec_dir="s")) == {"T003": "", "T004": ""}
    # sidecar depends win over prose depends
    sidecar = Sidecar(spec_dir="s", tasks={"T003": TaskMeta("code", "low", depends=["T004"])})
    result = readiness(job, tasks, sidecar)
    assert result == {"T003": "depends on T004", "T004": ""}


def test_find_spec_dir_prefers_argument_then_feature_json(tmp_path):
    import json

    import pytest

    from crew_chief.speckit import SpecError, find_spec_dir

    (tmp_path / "specs" / "001-a").mkdir(parents=True)
    (tmp_path / "specs" / "002-b").mkdir()
    assert find_spec_dir(tmp_path, "specs/001-a") == tmp_path / "specs" / "001-a"
    with pytest.raises(SpecError):
        find_spec_dir(tmp_path, "specs/nope")
    with pytest.raises(SpecError):
        find_spec_dir(tmp_path, None)
    feature = tmp_path / ".specify" / "feature.json"
    feature.parent.mkdir()
    feature.write_text(json.dumps({"feature_directory": "specs/002-b"}), encoding="utf-8")
    assert find_spec_dir(tmp_path, None) == tmp_path / "specs" / "002-b"
    feature.write_text(json.dumps({"path": "specs/001-a"}), encoding="utf-8")
    assert find_spec_dir(tmp_path, None) == tmp_path / "specs" / "001-a"
    feature.write_text("{not json", encoding="utf-8")
    with pytest.raises(SpecError):
        find_spec_dir(tmp_path, None)


def test_readiness_ignores_stale_pieces():
    from crew_chief.sidecar import Sidecar
    from crew_chief.speckit import parse_tasks, readiness

    tasks = parse_tasks("## Phase 1: A\n- [ ] T001 a\n- [ ] T002 b (depends on T001)\n")
    job = {"pieces": [
        {"n": 1, "ref": "T001", "stale": True, "accepted": {"attempt": 1}},
        {"n": 2, "ref": "T001"},
        {"n": 3, "ref": "T002"},
    ]}
    assert readiness(job, tasks, Sidecar(spec_dir="s")) == {
        "T001": "", "T002": "depends on T001",
    }
