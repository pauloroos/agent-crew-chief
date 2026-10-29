"""Tests for kit preferences, manifest, and vendored assets."""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from crew_chief.config import (
    PrefsError,
    load_prefs,
)

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "skill" / "crew-chief" / "scripts" / "crew_chief" / "kit_manifest.json"
PREFS_EXAMPLE = ROOT / "skill" / "crew-chief" / "templates" / "preferences.example.toml"
VENDOR_SUPERPOWERS = ROOT / "vendor" / "superpowers"


@pytest.fixture
def fake_env(tmp_path, monkeypatch):
    """Isolated environment with temporary home and fake PATH."""
    home = tmp_path / "home"
    home.mkdir()
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("PATH", str(empty_bin))
    return home, empty_bin


def test_kit_preferences_defaults(tmp_path, fake_env):
    """Preferences default to rtk=auto and superpowers=off."""
    project = tmp_path / "project"
    project.mkdir()
    prefs = load_prefs(project)
    assert prefs.rtk == "auto"
    assert prefs.superpowers == "off"


def test_kit_preferences_valid_rtk_values(tmp_path, fake_env):
    """rtk accepts 'auto', 'on', and 'off'."""
    project = tmp_path / "project"
    pref_dir = project / "docs" / "crew-chief"
    pref_dir.mkdir(parents=True)
    for val in ("auto", "on", "off"):
        (pref_dir / "preferences.toml").write_text(f'rtk = "{val}"\n', encoding="utf-8")
        prefs = load_prefs(project)
        assert prefs.rtk == val


def test_kit_preferences_invalid_rtk_raises(tmp_path, fake_env):
    """Invalid rtk preference raises PrefsError."""
    project = tmp_path / "project"
    pref_dir = project / "docs" / "crew-chief"
    pref_dir.mkdir(parents=True)
    (pref_dir / "preferences.toml").write_text('rtk = "invalid"\n', encoding="utf-8")
    with pytest.raises(PrefsError, match="rtk must be one of"):
        load_prefs(project)


def test_kit_preferences_valid_superpowers_values(tmp_path, fake_env):
    """superpowers accepts 'off', 'auto', and allowed skill names."""
    project = tmp_path / "project"
    pref_dir = project / "docs" / "crew-chief"
    pref_dir.mkdir(parents=True)
    valid_values = (
        "off",
        "auto",
        "test-driven-development",
        "systematic-debugging",
        "verification-before-completion",
    )
    for val in valid_values:
        (pref_dir / "preferences.toml").write_text(
            f'superpowers = "{val}"\n', encoding="utf-8"
        )
        prefs = load_prefs(project)
        assert prefs.superpowers == val


def test_kit_preferences_invalid_superpowers_raises(tmp_path, fake_env):
    """Invalid superpowers preference raises PrefsError."""
    project = tmp_path / "project"
    pref_dir = project / "docs" / "crew-chief"
    pref_dir.mkdir(parents=True)
    (pref_dir / "preferences.toml").write_text(
        'superpowers = "unknown-skill"\n', encoding="utf-8"
    )
    with pytest.raises(PrefsError, match="superpowers must be one of"):
        load_prefs(project)


def test_kit_preferences_precedence_project_over_global(tmp_path, fake_env):
    """Project preferences override global preferences."""
    project = tmp_path / "project"
    pref_dir = project / "docs" / "crew-chief"
    pref_dir.mkdir(parents=True)
    (pref_dir / "preferences.toml").write_text(
        'rtk = "on"\nsuperpowers = "test-driven-development"\n', encoding="utf-8"
    )

    global_prefs = tmp_path / "global-prefs.toml"
    global_prefs.write_text(
        'rtk = "off"\nsuperpowers = "systematic-debugging"\n', encoding="utf-8"
    )

    prefs = load_prefs(project, global_path=global_prefs)
    assert prefs.rtk == "on"
    assert prefs.superpowers == "test-driven-development"


def test_kit_manifest_content_and_pins():
    """Verify kit_manifest.json contains the exact pinned release and hashes."""
    assert MANIFEST_PATH.is_file(), "kit_manifest.json must exist"
    data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    # Superpowers manifest checks
    sp = data["superpowers"]
    assert sp["repository"] == "https://github.com/obra/superpowers"
    assert sp["commit"] == "2ce8aa15d05bd57213e6f746f953aafb7ded7a8b"
    assert sp["license"] == "MIT"
    assert sp["skills"] == [
        "test-driven-development",
        "systematic-debugging",
        "verification-before-completion",
    ]


def test_vendored_superpowers_files():
    """Verify vendor/superpowers contains license, attribution, and only the 3 skills."""
    assert (VENDOR_SUPERPOWERS / "LICENSE").is_file()
    license_text = (VENDOR_SUPERPOWERS / "LICENSE").read_text(encoding="utf-8")
    assert "MIT" in license_text
    assert "Jesse Vincent" in license_text

    skills_dir = VENDOR_SUPERPOWERS / "skills"
    assert skills_dir.is_dir()

    expected_skills = {
        "test-driven-development",
        "systematic-debugging",
        "verification-before-completion",
    }
    actual_skills = {p.name for p in skills_dir.iterdir() if p.is_dir()}
    assert actual_skills == expected_skills

    for skill in expected_skills:
        skill_file = skills_dir / skill / "SKILL.md"
        assert skill_file.is_file(), f"Missing SKILL.md for {skill}"
        content = skill_file.read_text(encoding="utf-8")
        assert len(content) > 50


def test_preferences_example_documents_kit():
    """Verify templates/preferences.example.toml documents rtk and superpowers."""
    text = PREFS_EXAMPLE.read_text(encoding="utf-8")
    assert "rtk" in text
    assert "superpowers" in text
    assert "test-driven-development" in text
    assert "systematic-debugging" in text
