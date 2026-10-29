"""Tests for executors_default preference loading, validation and precedence (FR-039)."""

from __future__ import annotations

import json

import pytest

from crew_chief import records
from crew_chief.cli import main
from crew_chief.config import (
    DEFAULT_EXECUTORS_DEFAULT,
    EXECUTORS_DEFAULT_VALUES,
    PrefsError,
    load_prefs,
)


def _write_project_prefs(tmp_path, text: str) -> None:
    proj = tmp_path / "proj"
    prefs_dir = proj / "docs" / "crew-chief"
    prefs_dir.mkdir(parents=True, exist_ok=True)
    (prefs_dir / "preferences.toml").write_text(text, encoding="utf-8")


def test_executors_default_defaults_to_ask_when_omitted(tmp_path):
    p = load_prefs(tmp_path, global_path=tmp_path / "none.toml")
    assert p.executors_default == "ask"
    assert p.executors_default == DEFAULT_EXECUTORS_DEFAULT


def test_executors_default_accepts_all_valid_values(tmp_path):
    for value in EXECUTORS_DEFAULT_VALUES:
        _write_project_prefs(tmp_path, f'executors_default = "{value}"\n')
        p = load_prefs(tmp_path / "proj", global_path=tmp_path / "none.toml")
        assert p.executors_default == value


def test_executors_default_rejects_invalid_value(tmp_path):
    _write_project_prefs(tmp_path, 'executors_default = "bogus"\n')
    with pytest.raises(PrefsError, match="executors_default"):
        load_prefs(tmp_path / "proj", global_path=tmp_path / "none.toml")


def test_executors_default_rejects_non_string(tmp_path):
    _write_project_prefs(tmp_path, "executors_default = 42\n")
    with pytest.raises(PrefsError, match="executors_default"):
        load_prefs(tmp_path / "proj", global_path=tmp_path / "none.toml")


def test_executors_default_global_overridden_by_project(tmp_path):
    global_file = tmp_path / "global.toml"
    global_file.write_text('executors_default = "own"\n', encoding="utf-8")
    _write_project_prefs(tmp_path, 'executors_default = "subagents"\n')
    p = load_prefs(tmp_path / "proj", global_path=global_file)
    assert p.executors_default == "subagents"


def test_executors_default_from_global_when_project_omits_it(tmp_path):
    global_file = tmp_path / "global.toml"
    global_file.write_text('executors_default = "free_first"\n', encoding="utf-8")
    # Project has no preferences.toml at all.
    proj = tmp_path / "proj"
    proj.mkdir()
    p = load_prefs(proj, global_path=global_file)
    assert p.executors_default == "free_first"


def test_executors_default_with_bom_and_crlf(tmp_path):
    """BOM/CRLF handling follows existing preferences parsing."""
    content = 'executors_default = "own"\r\n'
    _write_project_prefs(tmp_path, content)
    p = load_prefs(tmp_path / "proj", global_path=tmp_path / "none.toml")
    assert p.executors_default == "own"


def test_executors_default_empty_string_is_rejected(tmp_path):
    _write_project_prefs(tmp_path, 'executors_default = ""\n')
    with pytest.raises(PrefsError, match="executors_default"):
        load_prefs(tmp_path / "proj", global_path=tmp_path / "none.toml")


def test_job_new_uses_executors_default_when_flag_omitted(tmp_path):
    """``job new`` without ``--executors`` honours ``executors_default``."""
    proj = tmp_path / "proj"
    proj.mkdir()
    # Minimal git repo so CLI accepts it as a project.
    import subprocess

    subprocess.run(["git", "init", "-q", str(proj)], check=True)
    prefs_dir = proj / "docs" / "crew-chief"
    prefs_dir.mkdir(parents=True)
    (prefs_dir / "preferences.toml").write_text(
        'executors_default = "own"\n', encoding="utf-8"
    )
    request = tmp_path / "request.md"
    request.write_text("do it\n", encoding="utf-8")

    code = main([
        "job", "new",
        "--project", str(proj),
        "--title", "Prefs test",
        "--request-file", str(request),
    ])
    assert code == 0

    jobs_dir = proj / "docs" / "crew-chief" / "jobs"
    job_files = list(jobs_dir.glob("*.json"))
    assert len(job_files) == 1
    stored = json.loads(job_files[0].read_text(encoding="utf-8"))
    assert stored["executors"] == "own"