"""Executor choice one-question rule and subagents workflow documentation tests.

Covers FR-040 / SC-021 / SC-027: the coordinator round prompt must instruct
the coordinator to ask exactly one question when executors_default resolves to
"ask", record the answer via job set --executors, never ask again for that job,
and fall back to free_first in non-interactive runs. SKILL.md must document the
subagents workflow (piece start --native, isolated worktree, piece finish) and
forbid native subagents in own and free_first modes.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from crew_chief.coordinator_protocol import render_round


def _record(**overrides):
    base = dict(mode="direct-spec", authorization={"approved": True})
    base.update(overrides)
    return base


@pytest.mark.parametrize("mode", ["approved-spec", "direct-spec", "raw-request"])
def test_render_round_includes_executor_choice_one_question_rule(mode):
    """The round prompt must contain the exact one-question text."""
    record = _record(mode=mode)
    if mode == "approved-spec":
        record["authorization"]["approved"] = True
    if mode == "raw-request":
        record["authorization"]["authorize_raw"] = True
    prompt = render_round(record, "request", [], {})
    assert "Who should do the work: my own CLI (uses your subscription), my subagents in this session (uses your subscription), or free models?" in prompt
    assert "job set --executors" in prompt
    assert "never ask again" in prompt.lower() or "never prompts again" in prompt.lower()


def test_render_round_documents_non_interactive_fallback():
    """Non-interactive runs must fall back to free_first silently."""
    prompt = render_round(_record(), "request", [], {})
    assert "non-interactive" in prompt.lower()
    assert "free_first" in prompt


def test_render_round_documents_subagents_workflow():
    """The prompt must describe piece start --native and piece finish."""
    prompt = render_round(_record(), "request", [], {})
    assert "piece start --native" in prompt
    assert "piece finish" in prompt
    assert "worktree" in prompt.lower()


def test_render_round_forbids_native_subagents_in_own_and_free_first():
    """Native subagents are forbidden in own and free_first modes."""
    prompt = render_round(_record(), "request", [], {})
    lower = prompt.lower()
    assert "own" in lower and "free_first" in lower
    assert "forbidden" in lower or "never let" in lower


def test_skill_md_documents_executor_choice_and_subagents_workflow():
    """SKILL.md must contain the executor choice section with required content."""
    skill_path = Path(__file__).resolve().parents[1] / "skill" / "crew-chief" / "SKILL.md"
    content = skill_path.read_text(encoding="utf-8")
    assert "## Executor choice and subagents workflow" in content
    assert "Who should do the work: my own CLI (uses your subscription), my subagents in this session (uses your subscription), or free models?" in content
    assert "job set --executors" in content
    assert "piece start --native" in content
    assert "piece finish" in content
    assert "free_first" in content
    # Subagents forbidden in own and free_first
    assert "own" in content and "forbidden" in content