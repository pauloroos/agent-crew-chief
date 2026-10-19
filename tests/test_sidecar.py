"""Tests for the crew.toml sidecar of spec-kit tasks."""

from __future__ import annotations

import pytest

from crew_chief.sidecar import (
    SIDECAR_NAME,
    Sidecar,
    SidecarError,
    TaskMeta,
    load_sidecar,
    save_sidecar,
)


def test_sidecar_round_trip(tmp_path):
    sc = Sidecar(
        spec_dir="specs/003-x",
        job="J1",
        global_constraints=["No new deps"],
        tasks={
            "T012": TaskMeta(
                "code",
                "low",
                ["FR-001"],
                ["src/a.py"],
                "python -m pytest -q",
                "passed",
                ["T004"],
                {"produces": "f(x: int) -> int"},
            )
        },
    )
    save_sidecar(tmp_path, sc)
    assert (tmp_path / SIDECAR_NAME).is_file()
    assert load_sidecar(tmp_path) == sc


def test_missing_sidecar_is_empty(tmp_path):
    sc = load_sidecar(tmp_path)
    assert sc.tasks == {} and sc.job == ""
    assert sc.spec_dir == str(tmp_path)
    assert sc.global_constraints == []


def test_invalid_type_raises_with_task_id(tmp_path):
    (tmp_path / "crew.toml").write_text(
        '[tasks.T001]\ntype = "coding"\nrisk = "low"\n', encoding="utf-8"
    )
    with pytest.raises(SidecarError, match="T001"):
        load_sidecar(tmp_path)


def test_invalid_risk_raises_with_path_and_task_id(tmp_path):
    (tmp_path / "crew.toml").write_text(
        '[tasks.T002]\ntype = "code"\nrisk = "extreme"\n', encoding="utf-8"
    )
    with pytest.raises(SidecarError) as excinfo:
        load_sidecar(tmp_path)
    assert "T002" in str(excinfo.value)
    assert str(tmp_path / "crew.toml") in str(excinfo.value)


def test_invalid_toml_names_file(tmp_path):
    (tmp_path / "crew.toml").write_text("tasks = [", encoding="utf-8")
    with pytest.raises(SidecarError, match="crew.toml"):
        load_sidecar(tmp_path)


def test_defaults_when_task_fields_omitted(tmp_path):
    (tmp_path / "crew.toml").write_text(
        '[tasks.T001]\ntype = "docs"\nrisk = "medium"\n', encoding="utf-8"
    )
    meta = load_sidecar(tmp_path).tasks["T001"]
    assert meta == TaskMeta("docs", "medium", [], [], "", "", [], {})


def test_strings_with_quotes_and_backslashes_survive(tmp_path):
    meta = TaskMeta("tests", "low", [], ["C:\\x\\y.py"], 'pytest -k "a and b"', "", [], {})
    save_sidecar(tmp_path, Sidecar("s", "", [], {"T001": meta}))
    assert load_sidecar(tmp_path).tasks["T001"] == meta


def test_control_characters_and_unicode_survive(tmp_path):
    meta = TaskMeta(
        "code",
        "high",
        ["linha1\nlinha2\ttab\r\x01\x7f"],
        ["dir/acentuação.py"],
        "echo 'x' \"y\" \\n",
        "ção",
        [],
        {"k\"ey": "v\\al\nue"},
    )
    sc = Sidecar("sp\\ec", "j", ["a\"b", "c\\d"], {"T001": meta})
    save_sidecar(tmp_path, sc)
    assert load_sidecar(tmp_path) == sc


def test_tasks_written_sorted_by_id(tmp_path):
    base = TaskMeta("code", "low", [], [], "", "", [], {})
    sc = Sidecar("s", "", [], {"T010": base, "T002": base, "T001": base})
    save_sidecar(tmp_path, sc)
    text = (tmp_path / "crew.toml").read_text(encoding="utf-8")
    assert text.index("[tasks.T001]") < text.index("[tasks.T002]") < text.index("[tasks.T010]")
    assert list(load_sidecar(tmp_path).tasks) == ["T001", "T002", "T010"]


def test_save_overwrites_and_leaves_no_temp_files(tmp_path):
    meta = TaskMeta("code", "low", [], [], "", "", [], {})
    save_sidecar(tmp_path, Sidecar("s", "", [], {"T001": meta}))
    save_sidecar(tmp_path, Sidecar("s", "j2", [], {}))
    assert load_sidecar(tmp_path).job == "j2"
    assert [p.name for p in tmp_path.iterdir()] == ["crew.toml"]


def test_save_rejects_invalid_type(tmp_path):
    meta = TaskMeta("coding", "low", [], [], "", "", [], {})
    with pytest.raises(SidecarError, match="T001"):
        save_sidecar(tmp_path, Sidecar("s", "", [], {"T001": meta}))
    assert not (tmp_path / "crew.toml").exists()
