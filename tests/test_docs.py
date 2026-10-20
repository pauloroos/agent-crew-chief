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


def test_version_is_0_3_2_and_changelog_has_its_section():
    assert __version__ == "0.3.2"
    text = _read(CHANGELOG)
    section = text.split("## [0.3.2]", 1)[1].split("\n## [", 1)[0]
    for needle in ("Recent", "AppData", "tutorials", "hello-crew"):
        assert needle in section, needle


def test_changelog_keeps_the_0_3_1_section():
    """Bumping the version must not rewrite the released section."""
    text = _read(CHANGELOG)
    section = text.split("## [0.3.1]", 1)[1].split("\n## [", 1)[0]
    for needle in ("included", "cheap", "setup", "spend.jsonl", "sensitive", "0.3.1"):
        assert needle in section, needle


def test_changelog_keeps_the_0_3_0_section():
    """Bumping the version must not rewrite the released section."""
    text = _read(CHANGELOG)
    section = text.split("## [0.3.0]", 1)[1].split("\n## [", 1)[0]
    for needle in (
        "spec import",
        "brief render",
        "proof_failed",
        "decision add",
        "usage add",
        "economy",
        "crew.toml",
    ):
        assert needle in section, needle


def test_skill_documents_tiers_and_setup():
    text = _read(SKILL_MD)
    for needle in (
        "free",
        "included",
        "cheap",
        "paid",
        "setup --yes",
        "--allow-own-cli",
        "cheap_budget_usd_day",
    ):
        assert needle in text, needle


def test_readmes_mention_spend_log():
    for path in (README, README_PT):
        assert "spend.jsonl" in _read(path), path.name


def test_skill_documents_the_cost_ladder_and_its_limits():
    text = " ".join(_read(SKILL_MD).split())
    for needle in (
        "free",
        "included",
        "cheap",
        "paid",
        "setup --yes",
        "For beginners",
        "--allow-own-cli",
        "CREW_CHIEF_COORDINATOR",
        "cheap_budget_usd_day",
        "cheap_budget_usd_job",
        "cap_usd_day",
        "spend.jsonl",
        "sensitive_paths",
        "its own cost",
        "lowest effort",
    ):
        assert needle in text, needle


def test_readmes_document_the_cost_ladder_and_setup():
    for path in (README, README_PT):
        text = _read(path)
        for needle in (
            "free",
            "included",
            "cheap",
            "paid",
            "setup --yes",
            "cheap_budget_usd_day",
            "cheap_budget_usd_job",
            "cap_usd_day",
            "sensitive_paths",
        ):
            assert needle in text, (path.name, needle)


def test_preferences_example_documents_the_cost_keys():
    text = _read(
        ROOT / "skill" / "crew-chief" / "templates" / "preferences.example.toml"
    )
    for needle in (
        "cheap_budget_usd_day",
        "cheap_budget_usd_job",
        "cap_usd_day",
        "sensitive_paths",
        "forbid_training",
        "quota_cooldown_min",
        "included",
    ):
        assert needle in text, needle


def test_no_invisible_bom_characters_in_sources():
    """A literal U+FEFF in code is invisible in review; spell it as an escape."""
    offenders = []
    for folder in (ROOT / "skill", ROOT / "tests"):
        for path in folder.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if chr(0xFEFF) in text:
                offenders.append(path.relative_to(ROOT).as_posix())
    assert offenders == []
