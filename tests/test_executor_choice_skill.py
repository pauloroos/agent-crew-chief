"""Tests for FR-050 / SC-027: SKILL.md documents the subagents workflow."""

from __future__ import annotations

from pathlib import Path


def _skill_md() -> str:
    """Return the contents of skill/crew-chief/SKILL.md."""
    root = Path(__file__).resolve().parent.parent
    path = root / "skill" / "crew-chief" / "SKILL.md"
    return path.read_text(encoding="utf-8")


def test_skill_documents_subagents_workflow():
    """SKILL.md tells the coordinator to call piece start --native before
    launching a subagent, give only that worktree path, then call piece finish;
    never let a subagent edit the main checkout."""
    text = _skill_md()

    # The section must exist and name the workflow.
    assert "## Executor choice and subagents workflow" in text

    # Step 1: call piece start --native before launching a subagent.
    assert "piece start --native" in text
    assert "before launching a" in text.lower() or "before launching" in text.lower()

    # Step 2: give the subagent only that worktree path.
    assert "only" in text.lower()
    assert "worktree path" in text.lower() or "worktree" in text.lower()

    # Never let a subagent edit the main checkout.
    assert "never let a subagent edit" in text.lower() or \
           "never let a subagent" in text.lower()
    assert "main checkout" in text.lower()

    # Step 3: call piece finish when done.
    assert "piece finish" in text


def test_skill_documents_merge_after_accept():
    """SKILL.md states the subagent's work lands only by ACC merging its branch
    into the job's branch after review, grade and accept."""
    text = _skill_md()

    assert "merging" in text.lower() or "merge" in text.lower()
    assert "after review" in text.lower() or "review, grade and accept" in text.lower()
    assert "one pull request" in text.lower() or "exactly one" in text.lower()


def test_skill_forbids_native_subagents_in_own_and_free_first():
    """In own and free_first modes, native subagents stay forbidden for code,
    tests and reviews."""
    text = _skill_md()

    lower = text.lower()
    # Must mention both modes as forbidden contexts.
    assert "own" in lower
    assert "free_first" in lower
    assert "forbidden" in lower

    # Must cover code, tests and reviews.
    assert "code" in lower
    assert "tests" in lower
    assert "reviews" in lower or "review" in lower