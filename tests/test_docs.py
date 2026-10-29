"""Checks that the docs keep up with the CLI: commands, version, changelog."""

from __future__ import annotations

import argparse
import hashlib
import re
from pathlib import Path

import pytest

from crew_chief import __version__
from crew_chief.cli import build_parser

ROOT = Path(__file__).resolve().parents[1]
SKILL_MD = ROOT / "skill" / "crew-chief" / "SKILL.md"
README = ROOT / "README.md"
README_PT = ROOT / "README.pt-BR.md"
CHANGELOG = ROOT / "CHANGELOG.md"
TUTORIAL_01 = ROOT / "docs" / "tutorials" / "01-first-job.md"
TUTORIAL_01_PT = ROOT / "docs" / "tutorials" / "01-first-job.pt-BR.md"


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


def test_readmes_credit_ponytail():
    for path in (README, README_PT):
        text = _read(path)
        assert "DietrichGebert/ponytail" in text, path.name
        assert "MIT" in text, path.name


def test_version_is_current_and_changelog_has_its_section():
    """The version is 0.5.0 and the CHANGELOG has its section and the legacy pointer."""
    assert __version__ == "0.5.0"
    text = _read(CHANGELOG)
    assert "## [0.5.0]" in text
    current_section = text.split("## [0.5.0]", 1)[1].split("\n## [", 1)[0]
    assert "maestro" in current_section.lower()
    assert "agent-crew-chief-legacy" in text


def test_install_pins_the_current_release():
    """Install commands pin the current release tag (v0.5.0), not an older one."""
    for path in (README, README_PT, TUTORIAL_01, TUTORIAL_01_PT):
        text = _read(path)
        assert "v0.5.0" in text, f"{path.name} must pin v0.5.0"
        assert "v0.3.5" not in text, path.name


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


def test_no_server_paths_in_briefs_or_job_records():
    """No committed brief or job record contains '/home/orca/' literal strings."""
    jobs_dir = ROOT / "docs" / "crew-chief" / "jobs"
    if not jobs_dir.is_dir():
        pytest.skip("no committed job records")
    offenders = []
    for path in jobs_dir.rglob("*"):
        if path.is_file() and path.suffix in (".md", ".json"):
            text = path.read_text(encoding="utf-8")
            if "/home/orca/" in text or "/home/orca" in text:
                offenders.append(path.relative_to(ROOT).as_posix())
    assert offenders == []


def test_no_windows_drive_paths_in_published_docs():
    """No committed published doc contains a Windows drive-letter path."""
    docs_dir = ROOT / "docs"
    assert docs_dir.is_dir(), f"Missing docs directory: {docs_dir}"
    offenders = []
    drive_path = re.compile(
        r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]+(?:Users|DEV|dev|Projects|src)\b"
    )
    for path in docs_dir.rglob("*"):
        if path.is_file() and path.suffix in (".md", ".json"):
            text = path.read_text(encoding="utf-8")
            if drive_path.search(text):
                offenders.append(path.relative_to(ROOT).as_posix())
    assert offenders == []


def test_no_temporary_paths_in_job_records():
    """No committed job record contains an absolute /tmp path."""
    jobs_dir = ROOT / "docs" / "crew-chief" / "jobs"
    if not jobs_dir.is_dir():
        pytest.skip("no committed job records")
    offenders = []
    pattern = re.compile(r"(?<![\w$])/tmp/[A-Za-z0-9._-]+")
    for path in jobs_dir.rglob("*"):
        if path.is_file() and path.suffix in (".md", ".json"):
            text = path.read_text(encoding="utf-8")
            if pattern.search(text):
                offenders.append(path.relative_to(ROOT).as_posix())
    assert offenders == []
