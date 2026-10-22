"""Browser and optional Orca documentation verification (T039).

Covers FR-016, FR-017, FR-021 and SC-011 by checking that README.md and
docs/tutorials/04-central.md document the v0.4.1 ``--web`` view, local
action authorization, controlled terminal fallback, optional Orca probes
and v0.4.1 limits, and that every example command parses against the real
CLI. Uses temporary homes/config roots via conftest autouse fixtures and
fake CLIs; never touches real ``~/.config`` or live model CLIs. No
private data, client names or personal paths appear in committed artifacts.
"""
from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
README = REPO_ROOT / "README.md"
TUTORIAL = REPO_ROOT / "docs" / "tutorials" / "04-central.md"
SCRIPTS_DIR = REPO_ROOT / "skill" / "crew-chief" / "scripts"
WEB_ASSETS = REPO_ROOT / "skill" / "crew-chief" / "web"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


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


def test_no_private_data_in_web_docs():
    for doc in (README, TUTORIAL):
        text = _read(doc)
        for pattern in _PRIVATE_PATTERNS:
            match = pattern.search(text)
            assert match is None, f"{doc.name} leaks {match.group()!r}"


# ---------------------------------------------------------------------------
# FR-016: loopback web view with packaged assets and full views
# ---------------------------------------------------------------------------

_WEB_VIEWS = ["cards", "charts", "history", "scoreboard"]


def test_web_view_documented():
    tutorial, readme = _read(TUTORIAL).lower(), _read(README).lower()
    assert "acc central --web" in tutorial
    assert "--web" in readme
    for view in _WEB_VIEWS:
        assert view in tutorial, f"04-central.md must document the {view} view"
    assert "message" in tutorial and "question" in tutorial


def test_loopback_and_packaged_assets_documented():
    text = _read(TUTORIAL).lower()
    assert "127.0.0.1" in text and "loopback" in text
    assert "packaged" in text or "skill/crew-chief/web" in text
    assert "cdn" in text  # documents that no CDN/external resource is used
    for name in ("index.html", "central.css", "central.js"):
        assert (WEB_ASSETS / name).is_file(), f"missing packaged asset: {name}"


# ---------------------------------------------------------------------------
# FR-016 + US5/AC4: local action authorization and controlled terminal entry
# ---------------------------------------------------------------------------

def test_local_action_authorization_documented():
    text = _read(TUTORIAL).lower()
    assert "csrf" in text
    assert "origin" in text or "host" in text


def test_controlled_terminal_entry_documented():
    text = _read(TUTORIAL)
    lowered = text.lower()
    assert "acc attach" in text  # copyable fallback instruction
    assert "copy" in lowered  # US5/AC4: page shows a copyable instruction
    assert "bridge" in lowered  # terminal bridge handoff
    assert "does not claim" in lowered or "does not open" in lowered


# ---------------------------------------------------------------------------
# FR-017 + US6/AC3: explicitly optional Orca with probed immutable backend
# ---------------------------------------------------------------------------

def test_optional_orca_documented():
    tutorial, readme = _read(TUTORIAL).lower(), _read(README).lower()
    assert "--backend" in tutorial and "orca" in tutorial
    assert "probe" in tutorial
    assert "default" in tutorial  # default mode never needs Orca
    assert "immutable" in tutorial or "without switching" in tutorial
    assert "orca" in readme and "--backend" in readme


# ---------------------------------------------------------------------------
# SC-011: v0.4.1 limits, fake smoke procedure and owner-controlled integration
# ---------------------------------------------------------------------------

def test_v041_limits_and_owner_integration_documented():
    text = _read(TUTORIAL).lower()
    assert "ephemeral" in text
    assert "loopback" in text
    assert "merge" in text  # owner merges the coordinator branch themselves
    assert "never" in text  # ACC never merges/pushes or switches backend silently


def test_fake_browser_smoke_documented():
    text = _read(TUTORIAL)
    assert "test_central_web_assets.py" in text and "--smoke" in text
    assert "synthetic" in text.lower()  # fake snapshots only, no live coordinator


# ---------------------------------------------------------------------------
# Verification: doc commands parse, links and assets exist
# ---------------------------------------------------------------------------

def _acc_lines(text: str) -> list[str]:
    """Every line of a fenced code block that starts with ``acc ``."""
    lines, in_block = [], False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_block = not in_block
        elif in_block and stripped.startswith("acc "):
            lines.append(stripped)
    return lines


def test_every_acc_command_parses_with_the_real_parser():
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    from crew_chief import cli

    for doc in (README, TUTORIAL):
        for line in _acc_lines(_read(doc)):
            argv = shlex.split(line)[1:]
            # Placeholders stand for real ids/paths; the parser only checks shape.
            argv = [a if a not in ("COORDINATOR_ID", "QUESTION_ID", "ID") else
                    "00000000-0000-4000-8000-000000000001" for a in argv]
            try:
                cli.build_parser().parse_args(argv)
            except SystemExit as exc:
                assert exc.code == 0, f"{doc.name}: {line!r} does not parse"


def test_doc_links_and_web_assets_exist():
    for doc in (README, TUTORIAL):
        for match in re.finditer(r"\]\(([^)#\s]+\.md[^)\s]*)\)", _read(doc)):
            target = (doc.parent / match.group(1)).resolve()
            assert target.is_file(), f"{doc.name} links to missing {match.group(1)}"
        for match in re.finditer(r"`(skill/crew-chief/web/[^\s`]+)`", _read(doc)):
            assert (REPO_ROOT / match.group(1)).is_file(), f"missing {match.group(1)}"
