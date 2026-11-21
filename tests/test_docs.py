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
    assert __version__ == "0.17.5"
    text = _read(CHANGELOG)
    current_section = text.split(f"## [{__version__}]", 1)[1].split("\n## [", 1)[0]
    assert current_section.strip()


def test_branch_and_base_from_the_request_replace_the_acc_branch():
    skill = " ".join(_read(SKILL_MD).split())  # line breaks do not matter
    for phrase in ("`Branch: <name>`", "`Base: <branch>`", "never pushed", "`origin/<Base>` (`git fetch` first", "`<feature>-<lane>`"):
        assert phrase in skill, phrase
    # acc/<slug> is only the default, named once, in the Branches paragraph
    assert skill.count("acc/<slug>") == 1
    assert "Branch:" in _read(SKILL_MD.parent / "pit-wall.md")


def test_readmes_and_tutorials_pin_the_current_release():
    for path in (README, README_PT, *TUTORIALS):
        assert f"v{__version__}" in _read(path), path.name


def test_the_engineer_of_a_launched_round_starts_its_drivers_with_acc_launch():
    """A one-shot engineer must not leave its drivers as Orca workers: they never get the brief."""
    template = _read(SKILL_MD.parent / "templates" / "race-engineer.md")
    for phrase in ("ACC launch --direct", "--title", "ACC runs <id>", "lane worktree",
                   "end your turn", "lane report", "Ask anything"):
        assert phrase in template, phrase
    wall = _read(SKILL_MD.parent / "pit-wall.md")
    for phrase in ("ACC launch --direct", "ACC runs <id>", "ends the lane"):
        assert phrase in wall, phrase


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


def test_skill_regra_spec_aprovada():
    skill = " ".join(_read(SKILL_MD).split())
    for phrase in ("`spec-aprovada.md`", "`Spec: <file>`", "`ACC spec-import <file> [--anexo <file>]... --slug <slug>`",
                   "never write or edit its `spec.md`", "derive only `tasks.md`", "skip the approval stop",
                   "nobody to approve", "assumptions in `spec.md`", "`spec_divergente`",
                   "always imported, even for a small request", "`CREW_CHIEF_JOB_ID` set",
                   "`git add specs/<NNN>-<slug>`", "use another slug and report it",
                   "never with an imported spec or nobody to approve"):
        assert phrase in skill, phrase
