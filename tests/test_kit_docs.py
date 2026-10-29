"""Contract tests for base kit documentation: ponytail, rtk, superpowers."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
README_PT = ROOT / "README.pt-BR.md"
CHANGELOG = ROOT / "CHANGELOG.md"
TUTORIAL_01 = ROOT / "docs" / "tutorials" / "01-first-job.md"
TUTORIAL_01_PT = ROOT / "docs" / "tutorials" / "01-first-job.pt-BR.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# --- Presence in both languages -------------------------------------------

def test_both_readmes_document_base_kit():
    """Both READMEs mention the three base kit components."""
    for path in (README, README_PT):
        text = _read(path)
        assert "Ponytail" in text, f"{path.name} must mention Ponytail"
        assert "rtk" in text or "RTK" in text, f"{path.name} must mention rtk"
        assert "superpowers" in text, f"{path.name} must mention superpowers"


def test_tutorials_document_base_kit():
    """Both tutorial 01 files mention the three base kit components."""
    for path in (TUTORIAL_01, TUTORIAL_01_PT):
        if not path.is_file():
            continue
        text = _read(path)
        assert "Ponytail" in text, f"{path.name} must mention Ponytail"
        assert "rtk" in text or "RTK" in text, f"{path.name} must mention rtk"
        assert "superpowers" in text, f"{path.name} must mention superpowers"


# --- Installer flags -------------------------------------------------------

def test_readmes_document_installer_flags():
    """Both READMEs document --no-ponytail, --no-rtk, --no-superpowers."""
    for path in (README, README_PT):
        text = _read(path)
        assert "--no-ponytail" in text, f"{path.name} must document --no-ponytail"
        assert "--no-rtk" in text, f"{path.name} must document --no-rtk"
        assert "--no-superpowers" in text, f"{path.name} must document --no-superpowers"


# --- Ponytail levels -------------------------------------------------------

def test_readmes_document_ponytail_levels():
    """Both READMEs document ponytail_level values: lite, full, ultra, off."""
    for path in (README, README_PT):
        text = _read(path)
        for level in ("lite", "full", "ultra", "off"):
            assert level in text, f"{path.name} must document ponytail_level '{level}'"


# --- RTK behavior ----------------------------------------------------------

def test_readmes_document_rtk_defaults_and_checks():
    """Both READMEs explain rtk auto default, SHA-256 verification, Apache-2.0."""
    for path in (README, README_PT):
        text = _read(path)
        assert "auto" in text, f"{path.name} must mention rtk auto default"
        assert "SHA" in text or "sha" in text or "checksum" in text.lower(), \
            f"{path.name} must mention checksum verification"
        assert "Apache-2.0" in text, f"{path.name} must mention Apache-2.0 license"


# --- Superpowers behavior --------------------------------------------------

def test_readmes_document_superpowers_defaults_and_skills():
    """Both READMEs explain superpowers off default and list supported skills."""
    for path in (README, README_PT):
        text = _read(path)
        assert "off" in text, f"{path.name} must mention superpowers off default"
        for skill in (
            "test-driven-development",
            "systematic-debugging",
            "verification-before-completion",
        ):
            assert skill in text, f"{path.name} must list skill {skill}"


# --- Licenses --------------------------------------------------------------

def test_readmes_document_licenses():
    """Both READMEs mention MIT for superpowers/ponytail and Apache-2.0 for rtk."""
    for path in (README, README_PT):
        text = _read(path)
        assert "MIT" in text, f"{path.name} must mention MIT license"
        assert "Apache-2.0" in text, f"{path.name} must mention Apache-2.0 license"


# --- Changelog has Unreleased section --------------------------------------

def test_changelog_has_unreleased_section():
    """CHANGELOG.md has an Unreleased section for kit docs."""
    text = _read(CHANGELOG)
    assert "[Unreleased]" in text, "CHANGELOG must have [Unreleased] section"


def test_changelog_documents_base_kit():
    """CHANGELOG documents base kit components in Unreleased section."""
    text = _read(CHANGELOG)
    # Find the Unreleased section
    unreleased_start = text.find("## [Unreleased]")
    assert unreleased_start != -1, "CHANGELOG must have [Unreleased] section"

    # Find the next version section or end of file
    next_version = text.find("## [0.", unreleased_start + 1)
    if next_version == -1:
        unreleased_section = text[unreleased_start:]
    else:
        unreleased_section = text[unreleased_start:next_version]

    # Check for base kit mentions
    assert "ponytail" in unreleased_section.lower(), "CHANGELOG Unreleased must mention ponytail"
    assert "rtk" in unreleased_section.lower() or "rtk-oss" in unreleased_section.lower(), \
        "CHANGELOG Unreleased must mention rtk"
    assert "superpowers" in unreleased_section.lower(), "CHANGELOG Unreleased must mention superpowers"

    # Check for installer flags
    assert "--no-ponytail" in unreleased_section, "CHANGELOG must document --no-ponytail"
    assert "--no-rtk" in unreleased_section, "CHANGELOG must document --no-rtk"
    assert "--no-superpowers" in unreleased_section, "CHANGELOG must document --no-superpowers"

    # Check for RTK details
    assert "Apache-2.0" in unreleased_section, "CHANGELOG must mention Apache-2.0 license for rtk"
    assert "v1.1.0" in unreleased_section or "1.1.0" in unreleased_section, \
        "CHANGELOG must mention rtk-oss v1.1.0"

    # Check for superpowers skills
    assert "test-driven-development" in unreleased_section, "CHANGELOG must list test-driven-development"
    assert "systematic-debugging" in unreleased_section, "CHANGELOG must list systematic-debugging"
    assert "verification-before-completion" in unreleased_section, \
        "CHANGELOG must list verification-before-completion"


# --- Consistency between EN and pt-BR --------------------------------------

def test_readmes_consistent_flags_and_preferences():
    """Both READMEs document the same flags, preferences and skill names."""
    en_text = _read(README)
    pt_text = _read(README_PT)

    # Check installer flags are in both
    for flag in ("--no-ponytail", "--no-rtk", "--no-superpowers"):
        assert flag in en_text, f"README.md must document {flag}"
        assert flag in pt_text, f"README.pt-BR.md must document {flag}"

    # Check ponytail levels are in both
    for level in ("lite", "full", "ultra", "off"):
        assert level in en_text, f"README.md must document ponytail_level '{level}'"
        assert level in pt_text, f"README.pt-BR.md must document ponytail_level '{level}'"

    # Check RTK preferences are in both
    for pref in ("auto", "on", "off"):
        assert pref in en_text, f"README.md must document rtk preference '{pref}'"
        assert pref in pt_text, f"README.pt-BR.md must document rtk preference '{pref}'"

    # Check superpowers skills are in both
    for skill in (
        "test-driven-development",
        "systematic-debugging",
        "verification-before-completion",
    ):
        assert skill in en_text, f"README.md must list skill {skill}"
        assert skill in pt_text, f"README.pt-BR.md must list skill {skill}"

    # Check licenses are in both
    assert "MIT" in en_text and "MIT" in pt_text, "Both READMEs must mention MIT license"
    assert "Apache-2.0" in en_text and "Apache-2.0" in pt_text, \
        "Both READMEs must mention Apache-2.0 license"