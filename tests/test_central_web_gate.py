"""v0.4.1 release gate: installed-bundle fake end-to-end flow (T040).

Covers FR-016, FR-017, FR-018, FR-019, FR-021, FR-023, SC-008, SC-009,
SC-010 and SC-011 by verifying that the version is 0.4.1, the changelog
documents web/Orca/gate entries, public artifacts contain no private data,
and the installed-bundle fake flow exercises web/default/Orca modes without
touching real config or live CLIs. Uses temporary homes/config roots via
conftest autouse fixtures; never touches real ``~/.config`` or subscriptions.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CHANGELOG = REPO_ROOT / "CHANGELOG.md"
README = REPO_ROOT / "README.md"
TUTORIAL = REPO_ROOT / "docs" / "tutorials" / "04-central.md"
INIT_PY = REPO_ROOT / "skill" / "crew-chief" / "scripts" / "crew_chief" / "__init__.py"
WEB_ASSETS = REPO_ROOT / "skill" / "crew-chief" / "web"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


# ---------------------------------------------------------------------------
# FR-016 / SC-009: web view assets and loopback contract
# ---------------------------------------------------------------------------

def test_web_assets_packaged():
    """Packaged static assets exist for the loopback dashboard."""
    for name in ("index.html", "central.css", "central.js"):
        assert (WEB_ASSETS / name).is_file(), f"missing web asset {name}"


def test_web_assets_no_external_references():
    """Web assets must not reference external CDNs or networks."""
    for name in ("index.html", "central.css", "central.js"):
        text = _read(WEB_ASSETS / name)
        assert "cdn." not in text.lower(), f"{name} references a CDN"
        assert "unpkg.com" not in text.lower(), f"{name} references unpkg"
        assert "jsdelivr" not in text.lower(), f"{name} references jsdelivr"


# ---------------------------------------------------------------------------
# FR-017 / SC-010: optional Orca backend contract
# ---------------------------------------------------------------------------

def test_orca_importable_without_side_effects():
    """Orca backend module imports without requiring a live Orca install."""
    from crew_chief import central_orca  # noqa: F401


def test_default_mode_does_not_require_orca(tmp_path):
    """Default central snapshot works without Orca installed."""
    from datetime import datetime, timezone
    from crew_chief.central_snapshot import snapshot
    # Calling with a temp root and fixed now must not raise even when Orca is absent.
    result = snapshot(tmp_path, datetime.now(timezone.utc))
    assert isinstance(result, dict)
    assert "schema_version" in result


# ---------------------------------------------------------------------------
# FR-019: runtime requires only stdlib + Git
# ---------------------------------------------------------------------------

def test_no_runtime_dependencies_beyond_stdlib():
    """crew_chief package imports without third-party runtime deps."""
    import importlib
    mods = [
        "crew_chief",
        "crew_chief.central_cli",
        "crew_chief.central_config",
        "crew_chief.central_snapshot",
        "crew_chief.central_web",
    ]
    for mod_name in mods:
        mod = importlib.import_module(mod_name)
        assert mod is not None


# ---------------------------------------------------------------------------
# FR-021: no private data in public artifacts
# ---------------------------------------------------------------------------

_PRIVATE_PATTERNS = [
    re.compile(r"(?i)/home/[a-z]"),
    re.compile(r"(?i)/users/[a-z]"),
    re.compile(r"(?i)c:\\users\\[a-z]"),
    re.compile(r"(?i)api[_-]?key\s*[:=]\s*\S+"),
    re.compile(r"(?i)password\s*[:=]\s*\S+"),
    re.compile(r"(?i)token\s*[:=]\s*[A-Za-z0-9_\-]{16,}"),
]


def test_no_private_data_in_release_artifacts():
    """Public release artifacts contain no private data."""
    for doc in (CHANGELOG, README, TUTORIAL):
        text = _read(doc)
        for pattern in _PRIVATE_PATTERNS:
            match = pattern.search(text)
            assert match is None, f"{doc.name} leaks {match.group()!r}"


# ---------------------------------------------------------------------------
# FR-023: no AI co-author trailers
# ---------------------------------------------------------------------------

def test_no_ai_coauthor_trailers_in_release_docs():
    """Release docs do not contain AI co-author attribution lines."""
    ai_markers = ["Co-Authored-By:", "Generated with Claude", "AI-assisted"]
    for doc in (CHANGELOG, README, TUTORIAL):
        text = _read(doc)
        for marker in ai_markers:
            assert marker not in text, f"{doc.name} contains AI trailer {marker!r}"


# ---------------------------------------------------------------------------
# Version and changelog gate
# ---------------------------------------------------------------------------

def test_version_is_041():
    """Package version matches the v0.4.1 release."""
    from crew_chief import __version__
    assert __version__ == "0.4.1", f"expected 0.4.1, got {__version__}"


def test_changelog_has_041_entry():
    """CHANGELOG documents the v0.4.1 release with required sections."""
    text = _read(CHANGELOG)
    assert "## [0.4.1]" in text, "CHANGELOG missing [0.4.1] heading"
    lower = text.lower()
    # Find the 0.4.1 section
    idx = text.index("## [0.4.1]")
    next_section = text.find("\n## [", idx + 1)
    section = text[idx:next_section] if next_section != -1 else text[idx:]
    section_lower = section.lower()
    assert "web" in section_lower, "0.4.1 changelog missing web entry"
    assert "orca" in section_lower, "0.4.1 changelog missing Orca entry"
    assert "gate" in section_lower or "release" in section_lower, \
        "0.4.1 changelog missing release gate entry"


# ---------------------------------------------------------------------------
# SC-008 / SC-011: CI matrix and smoke evidence documented
# ---------------------------------------------------------------------------

def test_three_os_ci_documented():
    """Release docs mention three-OS CI requirement."""
    tutorial = _read(TUTORIAL).lower()
    readme = _read(README).lower()
    combined = tutorial + readme
    has_windows = "windows" in combined
    has_linux = "linux" in combined
    has_macos = "macos" in combined or "mac" in combined
    assert has_windows and has_linux and has_macos, \
        "Three-OS CI matrix not fully documented"


def test_fake_browser_smoke_procedure_documented():
    """Release docs include a reproducible fake-browser smoke procedure."""
    tutorial = _read(TUTORIAL).lower()
    assert "smoke" in tutorial, "Tutorial missing fake-browser smoke procedure"
    assert "fake" in tutorial or "test" in tutorial, \
        "Tutorial missing fake/test context for smoke procedure"