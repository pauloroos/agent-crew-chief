"""Tests for FR-041: pick respects executor mode (own, subagents, free_first)."""

from __future__ import annotations

import pytest

from crew_chief.scoring import pick
from crew_chief.tools import Executor


# --- helpers ---------------------------------------------------------------

_KINDS = {"antigravity": "antigravity", "claude": "claude-code",
          "ghost": "claude-code", "codex": "codex"}


def _ex(
    id_: str,
    cost: str = "free",
    name: str | None = None,
) -> Executor:
    tool_name, model = id_.split(":", 1)
    tool = {
        "name": name or tool_name,
        "kind": _KINDS.get(tool_name, "opencode"),
        "bin": tool_name,
    }
    return Executor(id=id_, tool=tool, model=model, cost=cost, trains=False)


class _Rng:
    def __init__(self, value: float) -> None:
        self.value = value

    def random(self) -> float:
        return self.value


def _prefs():
    from crew_chief.config import Prefs
    return Prefs(
        exec_first=["opencode"],
        tools=[],
        models=[],
        strong_order=["ghost", "codex"],
        explore_rate=0.1,
        forbid_training=False,
        sensitive_paths=[],
        issue_tracker="none",
        dispatch_timeout_s=900,
        review_default="",
    )


# --- own mode --------------------------------------------------------------


def test_own_mode_excludes_free_executors():
    """In own mode, free executors are excluded from the candidate pool."""
    own = _ex("claude:sonnet", cost="included")
    free = _ex("opencode:free-m", cost="free")
    # Free has better score but should be excluded in own mode.
    stats = {
        ("opencode:free-m", "code"): type("S", (), {"score": 99.0, "mean_duration_s": 1.0, "n": 1, "last": "2026-10-25"})(),
        ("claude:sonnet", "code"): type("S", (), {"score": 10.0, "mean_duration_s": 5.0, "n": 1, "last": "2026-10-25"})(),
    }

    chosen, reason = pick(
        [own, free], stats, "code", "medium", _prefs(), set(),
        _Rng(0.9), coordinator_tool="claude", executor_mode="own",
    )
    assert chosen is own
    assert "tier included" in reason


def test_own_mode_fails_when_no_own_executor_available():
    """When no own-family executor is available, pick fails explicitly."""
    free = _ex("opencode:free-m", cost="free")

    chosen, reason = pick(
        [free], {}, "code", "medium", _prefs(), set(),
        _Rng(0.9), coordinator_tool="claude", executor_mode="own",
    )
    assert chosen is None
    assert "no executor available in own CLI family" in reason


def test_own_mode_fails_without_coordinator_tool():
    """Own mode requires a coordinator_tool to identify the family."""
    own = _ex("claude:sonnet", cost="included")

    chosen, reason = pick(
        [own], {}, "code", "medium", _prefs(), set(),
        _Rng(0.9), coordinator_tool=None, executor_mode="own",
    )
    assert chosen is None
    assert "no executor available in own CLI family" in reason


def test_own_mode_with_allow_own_cli_is_accepted():
    """--allow-own-cli is redundant in own mode but accepted without error."""
    own = _ex("claude:sonnet", cost="included")

    chosen, reason = pick(
        [own], {}, "code", "medium", _prefs(), set(),
        _Rng(0.9), coordinator_tool="claude", allow_own_cli=True,
        executor_mode="own",
    )
    assert chosen is own


def test_own_mode_sorts_own_family_first():
    """Own CLI and included family sort first; free excluded."""
    own1 = _ex("claude:sonnet", cost="included")
    own2 = _ex("claude:opus", cost="included")
    free = _ex("antigravity:g-low", cost="free")
    stats = {
        ("antigravity:g-low", "code"): type("S", (), {"score": 99.0, "mean_duration_s": 1.0, "n": 1, "last": "2026-10-25"})(),
        ("claude:sonnet", "code"): type("S", (), {"score": 50.0, "mean_duration_s": 5.0, "n": 1, "last": "2026-10-25"})(),
        ("claude:opus", "code"): type("S", (), {"score": 40.0, "mean_duration_s": 5.0, "n": 1, "last": "2026-10-25"})(),
    }

    chosen, _ = pick(
        [own1, own2, free], stats, "code", "medium", _prefs(), set(),
        _Rng(0.9), coordinator_tool="claude", executor_mode="own",
    )
    # Best-scored among own family.
    assert chosen is own1


# --- subagents mode --------------------------------------------------------


def test_subagents_mode_returns_native_id():
    """Subagents mode returns native:<cli>/<model> instead of a CLI to spawn."""
    own = _ex("claude:sonnet", cost="included")
    free = _ex("opencode:free-m", cost="free")

    chosen, reason = pick(
        [own, free], {}, "code", "medium", _prefs(), set(),
        _Rng(0.9), coordinator_tool="claude", executor_mode="subagents",
    )
    assert chosen is not None
    assert chosen.id == "native:claude/sonnet"
    assert "subagents mode" in reason


def test_subagents_mode_fails_without_coordinator_tool():
    """Subagents mode requires coordinator_tool to build the native id."""
    own = _ex("claude:sonnet", cost="included")

    chosen, reason = pick(
        [own], {}, "code", "medium", _prefs(), set(),
        _Rng(0.9), coordinator_tool=None, executor_mode="subagents",
    )
    assert chosen is None
    assert "no executor available in own CLI family" in reason


def test_subagents_mode_fails_when_no_own_executor_in_pool():
    """Subagents mode needs at least one own-family executor to get the model."""
    free = _ex("opencode:free-m", cost="free")

    chosen, reason = pick(
        [free], {}, "code", "medium", _prefs(), set(),
        _Rng(0.9), coordinator_tool="claude", executor_mode="subagents",
    )
    assert chosen is None
    assert "no executor available in own CLI family" in reason


def test_subagents_mode_sorts_own_family_by_score():
    """In subagents mode, candidates from coordinator's CLI are sorted by score."""
    sonnet = _ex("claude:sonnet", cost="included")
    opus = _ex("claude:opus", cost="included")
    free = _ex("opencode:free-m", cost="free")
    stats = {
        ("claude:sonnet", "code"): type("S", (), {"score": 30.0, "mean_duration_s": 5.0, "n": 1, "last": "2026-10-25"})(),
        ("claude:opus", "code"): type("S", (), {"score": 90.0, "mean_duration_s": 5.0, "n": 1, "last": "2026-10-25"})(),
        ("opencode:free-m", "code"): type("S", (), {"score": 99.0, "mean_duration_s": 1.0, "n": 1, "last": "2026-10-25"})(),
    }

    chosen, reason = pick(
        [sonnet, opus, free], stats, "code", "medium", _prefs(), set(),
        _Rng(0.9), coordinator_tool="claude", executor_mode="subagents",
    )
    assert chosen is not None
    assert chosen.id == "native:claude/opus"
    assert "subagents mode" in reason


def test_subagents_mode_respects_exclude_and_quota():
    """In subagents mode, excluded or quota-cooling candidates are skipped."""
    sonnet = _ex("claude:sonnet", cost="included")
    opus = _ex("claude:opus", cost="included")

    # Exclude sonnet; opus is chosen.
    chosen, _ = pick(
        [sonnet, opus], {}, "code", "medium", _prefs(), {"claude:sonnet"},
        _Rng(0.9), coordinator_tool="claude", executor_mode="subagents",
    )
    assert chosen is not None
    assert chosen.id == "native:claude/opus"

    # Exclude sonnet via native id format; opus is chosen.
    chosen, _ = pick(
        [sonnet, opus], {}, "code", "medium", _prefs(), {"native:claude/sonnet"},
        _Rng(0.9), coordinator_tool="claude", executor_mode="subagents",
    )
    assert chosen is not None
    assert chosen.id == "native:claude/opus"

    # Exclude all own models; fails explicitly.
    chosen, reason = pick(
        [sonnet, opus], {}, "code", "medium", _prefs(), {"claude:sonnet", "claude:opus"},
        _Rng(0.9), coordinator_tool="claude", executor_mode="subagents",
    )
    assert chosen is None
    assert "no executor available in own CLI family" in reason


def test_subagents_mode_with_allow_own_cli_accepted():
    """--allow-own-cli is accepted in subagents mode without error."""
    sonnet = _ex("claude:sonnet", cost="included")

    chosen, reason = pick(
        [sonnet], {}, "code", "medium", _prefs(), set(),
        _Rng(0.9), coordinator_tool="claude", allow_own_cli=True,
        executor_mode="subagents",
    )
    assert chosen is not None
    assert chosen.id == "native:claude/sonnet"


def test_subagents_mode_review_follows_different_model_rule():
    """In subagents mode, review selection enforces different-model independence."""
    sonnet = _ex("claude:sonnet", cost="included")
    opus = _ex("claude:opus", cost="included")
    inc = _ex("codex:c1", cost="included")
    free = _ex("opencode:free-m", cost="free")
    stats = {
        ("claude:opus", "review"): type("S", (), {"score": 20.0, "mean_duration_s": 5.0, "n": 1, "last": "2026-10-25"})(),
        ("codex:c1", "review"): type("S", (), {"score": 50.0, "mean_duration_s": 5.0, "n": 1, "last": "2026-10-25"})(),
        ("opencode:free-m", "review"): type("S", (), {"score": 90.0, "mean_duration_s": 5.0, "n": 1, "last": "2026-10-25"})(),
    }

    # (1) Coordinator CLI with a different model wins, as an executor that
    # ``dispatch --review`` can run (a review is never native).
    chosen, reason = pick(
        [sonnet, opus, inc, free], stats, "review", "medium", _prefs(), set(),
        _Rng(0.9), coordinator_tool="claude", executor_mode="subagents",
        review_for_executor="native:claude/sonnet",
    )
    assert chosen is opus

    # (2) When own CLI has only the executor's model, included tier is chosen (CLI to spawn).
    chosen, _ = pick(
        [sonnet, inc, free], stats, "review", "medium", _prefs(), set(),
        _Rng(0.9), coordinator_tool="claude", executor_mode="subagents",
        review_for_executor="claude:sonnet",
    )
    assert chosen is inc
    assert chosen.id == "codex:c1"

    # (3) When own and included are exhausted, free tier is chosen (CLI to spawn).
    chosen, _ = pick(
        [sonnet, free], stats, "review", "medium", _prefs(), set(),
        _Rng(0.9), coordinator_tool="claude", executor_mode="subagents",
        review_for_executor="claude:sonnet",
    )
    assert chosen is free
    assert chosen.id == "opencode:free-m"

    # (4) When all candidates share executor's model, review is not dispatched.
    chosen, reason = pick(
        [sonnet], {}, "review", "medium", _prefs(), set(),
        _Rng(0.9), coordinator_tool="claude", executor_mode="subagents",
        review_for_executor="claude:sonnet",
    )
    assert chosen is None
    assert "no reviewer available" in reason
    assert "sonnet" in reason


# --- free_first mode unchanged ---------------------------------------------


def test_free_first_mode_unchanged():
    """free_first mode keeps the existing ladder behaviour."""
    free = _ex("opencode:free-m", cost="free")
    included = _ex("claude:sonnet", cost="included")
    stats = {
        ("opencode:free-m", "code"): type("S", (), {"score": 50.0, "mean_duration_s": 5.0, "n": 1, "last": "2026-10-25"})(),
    }

    chosen, reason = pick(
        [free, included], stats, "code", "medium", _prefs(), set(),
        _Rng(0.9), coordinator_tool="claude", executor_mode="free_first",
    )
    assert chosen is free
    assert "tier free" in reason


def test_free_first_with_allow_own_cli_permits_own_after_free():
    """In free_first, --allow-own-cli permits coordinator's CLI after free."""
    own = _ex("claude:sonnet", cost="included")
    other = _ex("codex:gpt-5.5-low", cost="included")
    stats = {
        ("claude:sonnet", "code"): type("S", (), {"score": 99.0, "mean_duration_s": 5.0, "n": 1, "last": "2026-10-25"})(),
        ("codex:gpt-5.5-low", "code"): type("S", (), {"score": 10.0, "mean_duration_s": 5.0, "n": 1, "last": "2026-10-25"})(),
    }

    # Without allow_own_cli, coordinator goes last in included tier.
    chosen, _ = pick(
        [own, other], stats, "code", "medium", _prefs(), set(),
        _Rng(0.9), coordinator_tool="claude", executor_mode="free_first",
    )
    assert chosen is other

    # With allow_own_cli, coordinator's CLI is eligible by score.
    chosen, _ = pick(
        [own, other], stats, "code", "medium", _prefs(), set(),
        _Rng(0.9), coordinator_tool="claude", allow_own_cli=True,
        executor_mode="free_first",
    )
    assert chosen is own


def test_allow_own_cli_never_changes_executor_mode():
    """--allow-own-cli does not change the resolved executor mode."""
    own = _ex("claude:sonnet", cost="included")
    free = _ex("opencode:free-m", cost="free")

    # In own mode with allow_own_cli, free is still excluded.
    chosen, _ = pick(
        [own, free], {}, "code", "medium", _prefs(), set(),
        _Rng(0.9), coordinator_tool="claude", allow_own_cli=True,
        executor_mode="own",
    )
    assert chosen is own