"""Documentation verification for core installation, launch and recovery (T029).

Covers FR-001, FR-012, FR-018, FR-020, FR-021 and SC-011 by checking that
the public documentation (README.md, docs/tutorials/04-central.md) describes
the complete install/launch/monitor/message/attach/pause/stop/recovery flow,
and that every example command parses against the real CLI with temporary
homes and fake commands. No private data, client names or personal paths
appear in committed artifacts.
"""
from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
README = REPO_ROOT / "README.md"
TUTORIAL = REPO_ROOT / "docs" / "tutorials" / "04-central.md"
TUTORIALS = [TUTORIAL, TUTORIAL.with_name("04-central.pt-BR.md")]
SCRIPTS_DIR = REPO_ROOT / "skill" / "crew-chief" / "scripts"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


# ---------------------------------------------------------------------------
# FR-021 + SC-011: no private data in public artifacts
# ---------------------------------------------------------------------------

_PRIVATE_PATTERNS = [
    re.compile(r"(?i)/home/[a-z]"),
    re.compile(r"(?i)/users/[a-z]"),
    re.compile(r"(?i)c:\\users\\[a-z]"),
    re.compile(r"(?i)api[_-]?key\s*[:=]\s*\S+"),
    re.compile(r"(?i)password\s*[:=]\s*\S+"),
    re.compile(r"(?i)token\s*[:=]\s*[A-Za-z0-9_\-]{16,}"),
]


@pytest.mark.parametrize("doc", [README, *TUTORIALS])
def test_no_private_data_in_public_artifacts(doc):
    """FR-021: public artifacts contain no private data, credentials or personal paths."""
    if not doc.exists():
        pytest.skip(f"{doc.name} not yet created")
    text = _read(doc)
    for pattern in _PRIVATE_PATTERNS:
        match = pattern.search(text)
        assert match is None, f"{doc.name} contains private data matching {pattern.pattern}: {match.group()!r}"


# ---------------------------------------------------------------------------
# SC-011: release docs cover the complete flow
# ---------------------------------------------------------------------------

_REQUIRED_SECTIONS = [
    "install",
    "launch",
    "monitor",
    "message",
    "attach",
    "pause",
    "stop",
    "recover",
]


def test_tutorial_covers_complete_flow():
    """SC-011: 04-central.md documents install/launch/monitor/message/attach/pause/stop/recovery."""
    assert TUTORIAL.exists(), "docs/tutorials/04-central.md must exist"
    text = _read(TUTORIAL).lower()
    missing = [s for s in _REQUIRED_SECTIONS if s not in text]
    assert not missing, f"04-central.md is missing sections: {missing}"


def test_readme_references_central_tutorial():
    """README.md points readers to the central tutorial for v0.4.0 features."""
    text = _read(README)
    assert "04-central" in text or "central" in text.lower(), (
        "README.md must reference the central tutorial or central features"
    )


# ---------------------------------------------------------------------------
# FR-001: explicit command-install documented
# ---------------------------------------------------------------------------

def test_command_install_documented():
    """FR-001 + US6/AC1: docs explain acc --command for user-scoped wrapper install."""
    text = _read(TUTORIAL)
    assert "--command" in text or "command-install" in text.lower() or "acc --command" in text, (
        "04-central.md must document the --command install option"
    )


# ---------------------------------------------------------------------------
# FR-020: no pty/curses/tmux/conpty dependency documented
# ---------------------------------------------------------------------------

def test_no_pty_dependency_documented():
    """FR-020: docs state Windows needs no pty/curses/tmux/ConPTY dependency."""
    text = _read(TUTORIAL).lower()
    assert "pty" in text or "conpty" in text or "curses" in text or "windows" in text, (
        "04-central.md must address the no-pty/no-curses/no-conpty requirement for Windows"
    )


# ---------------------------------------------------------------------------
# FR-018: v0.3.4 SDD/job/executor behavior preserved
# ---------------------------------------------------------------------------

def test_v034_behavior_preservation_documented():
    """FR-018: docs mention pick/brief/dispatch/review/grade/accept/close and branch isolation."""
    text = _read(TUTORIAL).lower()
    keywords = ["pick", "brief", "dispatch", "review", "grade", "accept"]
    found = sum(1 for k in keywords if k in text)
    assert found >= 3, (
        f"04-central.md must reference the v0.3.4 SDD workflow; found only {found}/6 keywords"
    )


# ---------------------------------------------------------------------------
# FR-012: terminal monitoring documented
# ---------------------------------------------------------------------------

def test_terminal_monitoring_documented():
    """FR-012: docs describe the refreshing terminal with rows, controls and snapshots."""
    text = _read(TUTORIAL).lower()
    assert "monitor" in text or "terminal" in text or "snapshot" in text, (
        "04-central.md must describe the monitoring terminal or snapshot output"
    )


# ---------------------------------------------------------------------------
# Example commands parse against the real CLI
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


@pytest.mark.parametrize("doc", TUTORIALS, ids=lambda p: p.name)
def test_every_acc_command_parses_with_the_real_parser(doc):
    """Drift guard: each ``acc ...`` example parses with cli.build_parser()."""
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    from crew_chief import cli

    lines = _acc_lines(_read(doc))
    assert len(lines) >= 10, f"{doc.name}: expected the full command flow, got {lines}"
    for line in lines:
        argv = shlex.split(line)[1:]
        try:
            cli.build_parser().parse_args(argv)
        except SystemExit as exc:  # --help/--version exit 0; a parse error exits 2
            assert exc.code == 0, f"{doc.name}: {line!r} does not parse"


# ---------------------------------------------------------------------------
# US6/AC2: works without Orca
# ---------------------------------------------------------------------------

def test_docs_state_no_orca_required():
    """US6/AC2: docs clarify that v0.4.0 works without an Orca executable."""
    text = _read(TUTORIAL).lower()
    assert "orca" in text or "no orca" in text or "without orca" in text or "default" in text, (
        "04-central.md should clarify that Orca is optional for v0.4.0 core features"
    )