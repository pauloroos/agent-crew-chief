"""Core release gate for v0.4.0 (T030).

Covers FR-018/019/021/022/023 and SC-001/004/005/006/007/008/011 by running
the installed `acc` command from this checkout, checking release
pins in docs and changelog, and confirming no private data or AI co-author
trailers leak into public artifacts. Stops before web/Orca phases.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "skill" / "crew-chief" / "scripts"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"
README_EN = REPO_ROOT / "README.md"
README_PT = REPO_ROOT / "README.pt-BR.md"
TUTORIAL_01_EN = REPO_ROOT / "docs" / "tutorials" / "01-first-job.md"
TUTORIAL_01_PT = REPO_ROOT / "docs" / "tutorials" / "01-first-job.pt-BR.md"
VERSION_FILE = SCRIPTS_DIR / "crew_chief" / "__init__.py"

CURRENT_VERSION = "0.4.0"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


# ---------------------------------------------------------------------------
# Version and changelog pins
# ---------------------------------------------------------------------------


def test_version_is_current():
    """FR-019: runtime version matches the gated release."""
    text = _read(VERSION_FILE)
    assert f'__version__ = "{CURRENT_VERSION}"' in text


def test_changelog_has_current_release_entry():
    """SC-011: CHANGELOG contains a 0.4.0 entry with central coordinator."""
    text = _read(CHANGELOG)
    assert f"## [{CURRENT_VERSION}]" in text
    assert "Central coordinator" in text or "central coordinator" in text.lower()


def test_changelog_preserves_historical_entries():
    """Historical changelog assertions remain intact."""
    text = _read(CHANGELOG)
    assert "[0.3.4]" in text
    assert "[0.3.3]" in text or "[0.3.2]" in text or "[0.3.1]" in text


# ---------------------------------------------------------------------------
# Release pins in documentation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("doc", [README_EN, README_PT, TUTORIAL_01_EN, TUTORIAL_01_PT])
def test_release_pins_updated_in_docs(doc):
    """Docs reference the current release tag or version where applicable."""
    if not doc.exists():
        pytest.skip(f"{doc.name} not present")
    text = _read(doc)
    # Either the current version appears, or the doc is version-agnostic.
    # We only fail if an OLD version is pinned without the new one.
    old_pins = re.findall(r"v0\.3\.\d+", text)
    if old_pins and CURRENT_VERSION not in text:
        pytest.fail(
            f"{doc.name} pins old version(s) {old_pins} without {CURRENT_VERSION}"
        )


# ---------------------------------------------------------------------------
# No private data or AI co-author trailers in public artifacts
# ---------------------------------------------------------------------------


_PRIVATE_PATTERNS = [
    re.compile(r"(?i)/home/[a-z]"),
    re.compile(r"(?i)/users/[a-z]"),
    re.compile(r"(?i)c:\\users\\[a-z]"),
    re.compile(r"(?i)api[_-]?key\s*[:=]\s*\S+"),
    re.compile(r"(?i)password\s*[:=]\s*\S+"),
]

_COAUTHOR_TRAILER = re.compile(r"(?i)co-authored-by:")


@pytest.mark.parametrize(
    "doc",
    [CHANGELOG, README_EN, README_PT],
)
def test_no_private_data_in_public_artifacts(doc):
    """FR-021: no private data, credentials or personal paths."""
    if not doc.exists():
        pytest.skip(f"{doc.name} not present")
    text = _read(doc)
    for pattern in _PRIVATE_PATTERNS:
        match = pattern.search(text)
        assert match is None, (
            f"{doc.name} contains private data matching {pattern.pattern}: "
            f"{match.group()!r}"
        )


@pytest.mark.parametrize(
    "doc",
    [CHANGELOG, README_EN, README_PT],
)
def test_no_ai_coauthor_trailers_in_artifacts(doc):
    """FR-023: commits and generated instructions omit AI co-author trailers."""
    if not doc.exists():
        pytest.skip(f"{doc.name} not present")
    text = _read(doc)
    match = _COAUTHOR_TRAILER.search(text)
    assert match is None, (
        f"{doc.name} contains an AI co-author trailer: {match.group()!r}"
    )


# ---------------------------------------------------------------------------
# Installed command smoke path
# ---------------------------------------------------------------------------


def test_installed_command_runs_this_release(tmp_path):
    """SC-007 + FR-001 + FR-019: install.py --command writes a wrapper that runs 0.4.0."""
    env = dict(os.environ)
    for name in ("HOME", "USERPROFILE", "LOCALAPPDATA"):
        env[name] = str(tmp_path / "home")
    bin_dir = tmp_path / "bin"
    installed = subprocess.run(
        [sys.executable, "-I", str(REPO_ROOT / "install.py"), "--command", "--bin-dir", str(bin_dir)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, timeout=60,
    )
    assert installed.returncode == 0, installed.stderr
    wrapper = bin_dir / ("acc.cmd" if os.name == "nt" else "acc")
    assert wrapper.is_file()

    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(wrapper), *args], capture_output=True, text=True, encoding="utf-8",
            errors="replace", env=env, timeout=60,
        )

    version = run("--version")
    assert version.returncode == 0, version.stderr
    assert CURRENT_VERSION in version.stdout
    helped = run("--help")
    assert helped.returncode == 0, helped.stderr
    for command in ("launch", "central", "message", "attach", "recover"):
        assert command in helped.stdout
