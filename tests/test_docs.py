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
TUTORIALS = (ROOT / "docs" / "tutorial.md", ROOT / "docs" / "tutorial.pt-BR.md")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_skill_md_names_every_subcommand_and_stays_short():
    text = _read(SKILL_MD)
    commands = _parser()._subparsers._group_actions[0].choices
    for name in commands:
        assert f"ACC {name}" in text, name
    assert len(text.splitlines()) <= 180


def test_skill_md_covers_spec_kit_skills_and_plain_mode():
    text = _read(SKILL_MD)
    for phrase in (
        "specs/",
        "tasks.md",
        "test-driven-development",
        "systematic-debugging",
        "verification-before-completion",
        "ponytail",
        "git worktree add",
        "taskkill",
        "kill --",
    ):
        assert phrase in text, phrase
    for name in ("spec", "plan", "tasks"):
        assert (SKILL_MD.parent / "templates" / "spec" / f"{name}.md").is_file(), name


def test_version_is_current_and_changelog_has_its_section():
    assert __version__ == "0.12.0"
    text = _read(CHANGELOG)
    current_section = text.split(f"## [{__version__}]", 1)[1].split("\n## [", 1)[0]
    assert current_section.strip()


def test_readmes_and_tutorials_pin_the_current_release():
    for path in (README, README_PT, *TUTORIALS):
        assert f"v{__version__}" in _read(path), path.name


def test_readmes_name_every_subcommand():
    commands = _parser()._subparsers._group_actions[0].choices
    for path in (README, README_PT):
        text = _read(path)
        for name in commands:
            assert f"ACC {name}" in text, (path.name, name)


def test_tutorials_use_the_example_project():
    for path in TUTORIALS:
        assert "examples/hello-crew" in _read(path), path.name
    assert (ROOT / "examples" / "hello-crew" / "src" / "hello_crew" / "greet.py").is_file()


def test_pit_wall_method_is_documented():
    skill_text = _read(SKILL_MD)
    readme_text = _read(README)
    readme_pt_text = _read(README_PT)
    pit_wall = SKILL_MD.parent / "pit-wall.md"
    scrutineer = SKILL_MD.parent / "templates" / "scrutineer.md"
    assert pit_wall.is_file(), "pit-wall.md missing"
    assert scrutineer.is_file(), "templates/scrutineer.md missing"
    pw_text = _read(pit_wall)
    sc_text = _read(scrutineer)
    for term in ("Stint", "Relay", "Scrutineering", "pace", "pit-wall.md"):
        assert term in skill_text, f"SKILL.md missing {term}"
    for term in ("Stint", "Relay", "Scrutineering", "pace", "Pit Wall"):
        assert term in readme_text, f"README.md missing {term}"
        assert term in readme_pt_text, f"README.pt-BR.md missing {term}"
    assert "reset --hard" in pw_text, "pit-wall.md missing reset --hard"
    assert "clean -fd" in pw_text, "pit-wall.md missing clean -fd"
    assert "VERDICT" in sc_text, "scrutineer.md missing VERDICT"


def test_race_engineer_template_and_team_docs():
    skill_text = _read(SKILL_MD)
    readme_text = _read(README)
    readme_pt_text = _read(README_PT)
    race_engineer = SKILL_MD.parent / "templates" / "race-engineer.md"
    assert race_engineer.is_file(), "templates/race-engineer.md missing"
    assert "race engineer" in skill_text, "SKILL.md missing race engineer"
    assert "race engineer" in readme_text, "README.md missing race engineer"
    assert "engenheiro de corrida" in readme_pt_text, (
        "README.pt-BR.md missing engenheiro de corrida"
    )


def test_no_invisible_bom_characters_in_sources():
    """A literal U+FEFF in code is invisible in review; spell it as an escape."""
    offenders = [
        path.relative_to(ROOT).as_posix()
        for folder in (ROOT / "skill", ROOT / "tests")
        for path in folder.rglob("*.py")
        if chr(0xFEFF) in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
