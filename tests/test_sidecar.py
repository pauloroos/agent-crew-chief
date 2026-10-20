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


# -- unknown keys, the task field and in-place updates ------------------------

from crew_chief.sidecar import update_sidecar  # noqa: E402

COMMENTED = (
    "# notes about proofs\n"
    "[tasks.T001]\n"
    'type = "code"  # the model\n'
    'risk = "low"\n'
    "# keep this\n"
    'proof = "pytest -q"\n'
)


def test_unknown_task_key_raises_naming_key_and_task(tmp_path):
    (tmp_path / "crew.toml").write_text(
        '[tasks.T002]\ntype = "code"\nrisk = "low"\nexpectt = "passed"\n',
        encoding="utf-8",
    )
    with pytest.raises(SidecarError) as excinfo:
        load_sidecar(tmp_path)
    message = str(excinfo.value)
    assert "task T002" in message and "unknown key 'expectt'" in message
    assert len(message.splitlines()) == 1


def test_unknown_top_level_key_raises(tmp_path):
    (tmp_path / "crew.toml").write_text('spec_dirr = "x"\n', encoding="utf-8")
    with pytest.raises(SidecarError, match="unknown key 'spec_dirr'"):
        load_sidecar(tmp_path)


def test_task_field_round_trips_and_is_optional(tmp_path):
    meta = TaskMeta("code", "low", task="Create the model in src/m.py")
    save_sidecar(tmp_path, Sidecar("s", "j", [], {"T001": meta}))
    assert 'task = "Create the model in src/m.py"' in (tmp_path / "crew.toml").read_text(
        encoding="utf-8"
    )
    assert load_sidecar(tmp_path).tasks["T001"] == meta
    (tmp_path / "crew.toml").write_text('[tasks.T001]\ntype = "code"\nrisk = "low"\n',
                                        encoding="utf-8")
    assert load_sidecar(tmp_path).tasks["T001"].task == ""


def test_update_creates_a_missing_file(tmp_path):
    sc = Sidecar("specs/x", "j", [], {"T001": TaskMeta("code", "low", task="A")})
    assert update_sidecar(tmp_path, sc) == "created"
    assert load_sidecar(tmp_path) == sc


def test_update_appends_new_tables_and_keeps_comments(tmp_path):
    (tmp_path / "crew.toml").write_text(COMMENTED, encoding="utf-8")
    sc = load_sidecar(tmp_path)
    sc.spec_dir, sc.job = "specs/x", "J1"
    sc.tasks["T002"] = TaskMeta("docs", "low", depends=["T001"], task="Write docs")
    assert update_sidecar(tmp_path, sc) == "updated"
    text = (tmp_path / "crew.toml").read_text(encoding="utf-8")
    assert text.startswith("# notes about proofs\n")
    assert COMMENTED.split("\n", 1)[1] in text          # old tables untouched
    assert text.index('spec_dir = "specs/x"') < text.index("[tasks.T001]")
    assert text.index("[tasks.T001]") < text.index("[tasks.T002]")
    assert load_sidecar(tmp_path) == sc


def test_update_adds_missing_keys_under_the_header(tmp_path):
    (tmp_path / "crew.toml").write_text(COMMENTED, encoding="utf-8")
    sc = load_sidecar(tmp_path)
    sc.tasks["T001"].task = "Create the model"
    sc.tasks["T001"].depends = ["T000"]
    assert update_sidecar(tmp_path, sc) == "updated"
    text = (tmp_path / "crew.toml").read_text(encoding="utf-8")
    assert "# keep this\n" in text and "# the model" in text
    assert load_sidecar(tmp_path).tasks["T001"] == sc.tasks["T001"]


def test_update_replaces_a_one_line_value(tmp_path):
    (tmp_path / "crew.toml").write_text(COMMENTED + "depends = []\n", encoding="utf-8")
    sc = load_sidecar(tmp_path)
    sc.tasks["T001"].depends = ["T000"]
    assert update_sidecar(tmp_path, sc) == "updated"
    text = (tmp_path / "crew.toml").read_text(encoding="utf-8")
    assert 'depends = ["T000"]' in text and "depends = []" not in text
    assert "# keep this\n" in text


def test_update_keeps_existing_top_level_values(tmp_path):
    (tmp_path / "crew.toml").write_text('job = "first"\n' + COMMENTED, encoding="utf-8")
    sc = load_sidecar(tmp_path)
    sc.job = "second"
    sc.spec_dir = "specs/x"
    assert update_sidecar(tmp_path, sc) == "updated"
    loaded = load_sidecar(tmp_path)
    assert loaded.job == "first" and loaded.spec_dir == "specs/x"


def test_update_with_nothing_new_does_not_write(tmp_path):
    (tmp_path / "crew.toml").write_text('spec_dir = "s"\njob = "j"\n' + COMMENTED,
                                        encoding="utf-8")
    before = (tmp_path / "crew.toml").stat().st_mtime_ns
    assert update_sidecar(tmp_path, load_sidecar(tmp_path)) == "unchanged"
    assert (tmp_path / "crew.toml").stat().st_mtime_ns == before


def test_update_keeps_crlf_and_bom(tmp_path):
    raw = (chr(0xFEFF) + COMMENTED).replace("\n", "\r\n").encode("utf-8")
    (tmp_path / "crew.toml").write_bytes(raw)
    sc = load_sidecar(tmp_path)
    sc.tasks["T002"] = TaskMeta("docs", "low")
    assert update_sidecar(tmp_path, sc) == "updated"
    out = (tmp_path / "crew.toml").read_bytes()
    assert out.startswith(raw)
    assert b"\n" not in out.replace(b"\r\n", b"")


def test_update_rewrites_an_unusual_layout(tmp_path):
    (tmp_path / "crew.toml").write_text(
        '# dotted keys\n[tasks]\nT001.type = "code"\nT001.risk = "low"\n', encoding="utf-8"
    )
    sc = load_sidecar(tmp_path)
    sc.tasks["T001"].task = "Create the model"
    assert update_sidecar(tmp_path, sc) == "rewritten"
    assert load_sidecar(tmp_path).tasks["T001"].task == "Create the model"
