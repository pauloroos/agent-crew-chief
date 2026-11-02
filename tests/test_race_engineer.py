"""Checks that the race engineer template and team docs are in place."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL_MD = ROOT / "skill" / "crew-chief" / "SKILL.md"
README = ROOT / "README.md"
README_PT = ROOT / "README.pt-BR.md"
RACE_ENGINEER = ROOT / "skill" / "crew-chief" / "templates" / "race-engineer.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_race_engineer_template_exists():
    assert RACE_ENGINEER.is_file(), "templates/race-engineer.md missing"


def test_skill_md_and_readmes_name_race_engineer():
    skill_text = _read(SKILL_MD)
    readme_text = _read(README)
    readme_pt_text = _read(README_PT)
    assert "race engineer" in skill_text, "SKILL.md missing race engineer"
    assert "race engineer" in readme_text, "README.md missing race engineer"
    assert "engenheiro de corrida" in readme_pt_text, (
        "README.pt-BR.md missing engenheiro de corrida"
    )