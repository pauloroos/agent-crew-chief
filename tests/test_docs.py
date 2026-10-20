"""Checks that the docs keep up with the CLI: commands, version, changelog."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from crew_chief import __version__
from crew_chief.cli import build_parser

ROOT = Path(__file__).resolve().parents[1]
SKILL_MD = ROOT / "skill" / "crew-chief" / "SKILL.md"
README = ROOT / "README.md"
README_PT = ROOT / "README.pt-BR.md"
CHANGELOG = ROOT / "CHANGELOG.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_skill_md_documents_spec_flow():
    text = _read(SKILL_MD)
    assert "Spec-driven work" in text
    for cmd in (
        "spec new",
        "spec import",
        "spec status",
        "brief render",
        "--converge",
        "ruling add",
        "decision add",
        "usage add",
        "economy",
        "--no-proof",
        "--ignore-deps",
    ):
        assert cmd in text, cmd


def test_skill_md_names_every_subcommand():
    text = _read(SKILL_MD)
    subparsers = next(
        action
        for action in build_parser()._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    for name in subparsers.choices:
        assert f"CC {name}" in text, name


def test_skill_md_sizes_the_process():
    text = _read(SKILL_MD)
    for word in ("trivial", "small", "project"):
        assert re.search(rf"\*\*{word}\*\*", text, re.IGNORECASE), word
    assert "read the economy alerts" in text.lower()


def test_both_readmes_document_sdd_and_credit_inspirations():
    for path in (README, README_PT):
        text = _read(path)
        assert "spec-kit 1.2.x" in text, path.name
        assert "spec import" in text, path.name
        assert "crew.toml" in text, path.name
        assert "superpowers" in text, path.name
        assert "MIT" in text, path.name


def test_readmes_have_the_same_command_blocks():
    """The two READMEs have the same number of ```bash blocks (counts only)."""

    def fences(path: Path) -> list[str]:
        return re.findall(r"```bash\n(.*?)```", _read(path), re.DOTALL)

    english, portuguese = fences(README), fences(README_PT)
    assert len(english) == len(portuguese)


def test_economy_docs_match_the_summary_line():
    """The summary line has no duration; the docs must not say it does."""
    from crew_chief.economy import Economy, summary_line

    line = summary_line(Economy(1, 1, 1, False, 1, 1, 1, 1, 1, 90.0))
    assert "min" not in line and "duration" not in line
    claims = ("mean score and duration", "mean score and the job's duration",
              "nota média e a duração do job")
    for path in (README, README_PT, CHANGELOG):
        text = " ".join(_read(path).split())
        for claim in claims:
            assert claim not in text, (path.name, claim)
    assert "alert_job_minutes" in " ".join(_read(README).split())


def test_version_is_0_3_0_and_changelog_has_its_section():
    assert __version__ == "0.3.0"
    text = _read(CHANGELOG)
    heading = next(
        line for line in text.splitlines()
        if line.startswith(("## [Unreleased]", "## [0.3.0]"))
    )
    unreleased = text.split(heading + "\n", 1)[1].split("\n## [", 1)[0]
    for needle in (
        "spec import",
        "brief render",
        "proof_failed",
        "decision add",
        "usage add",
        "economy",
        "crew.toml",
    ):
        assert needle in unreleased, needle


def test_no_invisible_bom_characters_in_sources():
    """A literal U+FEFF in code is invisible in review; spell it as an escape."""
    offenders = []
    for folder in (ROOT / "skill", ROOT / "tests"):
        for path in folder.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if chr(0xFEFF) in text:
                offenders.append(path.relative_to(ROOT).as_posix())
    assert offenders == []
