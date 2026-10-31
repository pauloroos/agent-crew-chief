"""Checks that the docs keep up with the CLI: commands, size, version, changelog."""

from __future__ import annotations

from pathlib import Path

from crew_chief import __version__
from crew_chief.cli import _parser

ROOT = Path(__file__).resolve().parents[1]
SKILL_MD = ROOT / "skill" / "crew-chief" / "SKILL.md"
README = ROOT / "README.md"
README_PT = ROOT / "README.pt-BR.md"
CHANGELOG = ROOT / "CHANGELOG.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_skill_md_names_every_subcommand_and_stays_short():
    text = _read(SKILL_MD)
    commands = _parser()._subparsers._group_actions[0].choices
    for name in commands:
        assert f"ACC {name}" in text, name
    assert len(text.splitlines()) <= 130


def test_version_is_current_and_changelog_has_its_section():
    assert __version__ == "0.7.1"
    text = _read(CHANGELOG)
    current_section = text.split(f"## [{__version__}]", 1)[1].split("\n## [", 1)[0]
    assert current_section.strip()


def test_readmes_pin_the_current_release():
    for path in (README, README_PT):
        assert f"v{__version__}" in _read(path), path.name


def test_no_invisible_bom_characters_in_sources():
    """A literal U+FEFF in code is invisible in review; spell it as an escape."""
    offenders = [
        path.relative_to(ROOT).as_posix()
        for folder in (ROOT / "skill", ROOT / "tests")
        for path in folder.rglob("*.py")
        if chr(0xFEFF) in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
