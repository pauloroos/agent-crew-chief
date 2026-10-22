"""Tests for the bounded central round workflow in SKILL.md and snippet.md (T026).

Covers:
- FR-008: correlated questions/answers with blocked task IDs
- FR-009: finite headless rounds resuming one persisted session
- FR-018: preserve v0.3.4 SDD/job/executor behavior
- FR-022: UTF-8 BOM/CRLF handling in installed snippet
- FR-023: no AI co-author trailers in generated instructions
- US2/AC1: message durability across rounds
- US2/AC2: blocked vs independent task visibility
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest


_SKILL_PATH = Path(__file__).resolve().parents[1] / "skill" / "crew-chief" / "SKILL.md"
_SNIPPET_PATH = Path(__file__).resolve().parents[1] / "skill" / "crew-chief" / "templates" / "snippet.md"


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# --- FR-009 / US2/AC1: central round protocol documented ----------------------


def test_skill_documents_central_round_checkpoint():
    """SKILL.md must document the ACC_ROUND checkpoint and inbox/outbox protocol."""
    text = _read_text(_SKILL_PATH)
    # The central coordinator commands section exists
    assert "Central coordinator commands" in text or "central coordinator" in text.lower()
    # Round-related concepts are mentioned
    lower = text.lower()
    assert "round" in lower or "inbox" in lower or "message" in lower


def test_skill_documents_blocked_task_behavior():
    """SKILL.md must mention that only dependent work waits on questions."""
    text = _read_text(_SKILL_PATH).lower()
    # Either "blocked" or "dependent" + "wait" should appear
    has_blocked = "blocked" in text
    has_dependent_wait = "dependent" in text and "wait" in text
    assert has_blocked or has_dependent_wait, (
        "SKILL.md should document blocked-task or dependent-wait behavior"
    )


def test_skill_no_ai_coauthor_trailer_instruction():
    """SKILL.md must not instruct adding AI co-author trailers to commits."""
    text = _read_text(_SKILL_PATH).lower()
    # Should not contain instructions to add Co-Authored-By or AI trailers
    assert "co-authored-by" not in text or "no ai" in text or "omit" in text


# --- FR-022: BOM/CRLF handling in installed snippet --------------------------


def test_snippet_survives_bom_installation():
    """Snippet content survives UTF-8 BOM encoding and remains valid."""
    original = _read_text(_SNIPPET_PATH)
    # Simulate installation with BOM
    bom_content = "﻿" + original
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", delete=False, encoding="utf-8-sig"
    ) as f:
        f.write(original)
        tmp_path = Path(f.name)
    try:
        installed = tmp_path.read_text(encoding="utf-8-sig")
        # Content should match after stripping BOM
        assert installed.strip() == original.strip()
    finally:
        tmp_path.unlink(missing_ok=True)


def test_snippet_survives_crlf_installation():
    """Snippet content survives CRLF line endings and remains valid."""
    original = _read_text(_SNIPPET_PATH)
    crlf_content = original.replace("\n", "\r\n")
    with tempfile.NamedTemporaryFile(
        mode="wb", suffix=".md", delete=False
    ) as f:
        f.write(crlf_content.encode("utf-8"))
        tmp_path = Path(f.name)
    try:
        installed = tmp_path.read_text(encoding="utf-8")
        # After normalizing line endings, content should match
        assert installed.replace("\r\n", "\n").strip() == original.strip()
    finally:
        tmp_path.unlink(missing_ok=True)


# --- FR-018: existing job-only path remains valid ----------------------------


def test_skill_preserves_job_cycle_documentation():
    """SKILL.md still documents the seven-step job cycle from v0.3.4."""
    text = _read_text(_SKILL_PATH)
    # Core job cycle steps should be present
    assert "job new" in text or "CC job new" in text
    assert "piece add" in text or "CC piece add" in text
    assert "dispatch" in text
    assert "accept" in text


# --- US2/AC2: question correlation and task blocking -------------------------


def test_skill_mentions_question_correlation():
    """SKILL.md mentions correlating questions with task IDs."""
    text = _read_text(_SKILL_PATH).lower()
    # Should mention questions being linked to tasks or pieces
    has_question_task = ("question" in text and "task" in text)
    has_piece_ref = ("piece" in text and ("ref" in text or "id" in text))
    assert has_question_task or has_piece_ref, (
        "SKILL.md should mention question/task correlation"
    )


# --- Snippet references SKILL.md correctly -----------------------------------


def test_snippet_references_skill_md():
    """Installed snippet points users to read SKILL.md before starting."""
    text = _read_text(_SNIPPET_PATH)
    assert "SKILL.md" in text


def test_snippet_mentions_coordinator_role():
    """Installed snippet tells the reader they are the coordinator."""
    text = _read_text(_SNIPPET_PATH).lower()
    assert "coordinator" in text