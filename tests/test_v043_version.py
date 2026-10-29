"""Version verification for historical v0.4.3 release."""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_changelog_has_043_section_with_date():
    """CHANGELOG has a dated [0.4.3] section, not just Unreleased."""
    text = _read("CHANGELOG.md")
    assert "## [0.4.3] - 2026-10-26" in text


def test_changelog_043_covers_phases():
    """The 0.4.3 section mentions Phase 1, 2, 2b and 4 content."""
    text = _read("CHANGELOG.md")
    section = text.split("## [0.4.3]", 1)[1].split("\n## [", 1)[0]
    lower = section.lower()
    # Phase 1: central coordinator
    assert "central" in lower
    # Phase 2: web view
    assert "web" in lower
    # Phase 2b: Orca backend
    assert "orca" in lower
    # Phase 4: executor choice / native pieces
    assert "executor" in lower or "piece" in lower
