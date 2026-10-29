"""Tests for FR-042: review selection enforces different-model rule."""

from __future__ import annotations

import pytest

from crew_chief.config import Prefs
from crew_chief.scoring import pick
from crew_chief.tools import Executor


_KINDS = {"antigravity": "antigravity", "claude": "claude-code",
          "ghost": "claude-code", "codex": "codex"}


def _ex(
    id_: str,
    cost: str = "free",
    name: str | None = None,
    work_types: list[str] | None = None,
) -> Executor:
    tool_name, model = id_.split(":", 1)
    tool = {
        "name": name or tool_name,
        "kind": _KINDS.get(tool_name, "opencode"),
        "bin": tool_name,
    }
    if work_types is not None:
        tool["work_types"] = work_types
    return Executor(id=id_, tool=tool, model=model, cost=cost, trains=False)


def _prefs(
    strong_order: tuple[str, ...] = ("ghost", "codex"),
    explore_rate: float = 0.1,
    exec_first: tuple[str, ...] = ("opencode",),
) -> Prefs:
    return Prefs(
        exec_first=list(exec_first),
        tools=[],
        models=[],
        strong_order=list(strong_order),
        explore_rate=explore_rate,
        forbid_training=False,
        sensitive_paths=[],
        issue_tracker="none",
        dispatch_timeout_s=900,
        review_default="",
    )


class _Rng:
    def __init__(self, value: float) -> None:
        self.value = value

    def random(self) -> float:
        return self.value


# --- subagents mode ---------------------------------------------------------


def test_subagents_review_rejects_same_model():
    """In subagents mode, review must not pick the executor's model."""
    sonnet = _ex("claude:sonnet")
    opus = _ex("claude:opus")

    chosen, reason = pick(
        [sonnet, opus], {}, "review", "medium", _prefs(), set(), _Rng(0.9),
        coordinator_tool="claude",
        executor_mode="subagents",
        review_for_executor="claude:sonnet",
    )

    assert chosen is not None
    assert chosen.model == "opus"
    assert "sonnet" not in chosen.id or "opus" in chosen.id


def test_subagents_review_reports_when_all_share_executor_model():
    """When every own-CLI candidate shares the executor's model, report it."""
    sonnet_a = _ex("claude:sonnet")
    sonnet_b = _ex("claude:sonnet")

    chosen, reason = pick(
        [sonnet_a, sonnet_b], {}, "review", "medium", _prefs(), set(), _Rng(0.9),
        coordinator_tool="claude",
        executor_mode="subagents",
        review_for_executor="claude:sonnet",
    )

    assert chosen is None
    assert "executor's model" in reason
    assert "sonnet" in reason


def test_subagents_review_without_review_for_unchanged():
    """Without review_for_executor, subagents mode works as before."""
    sonnet = _ex("claude:sonnet")

    chosen, reason = pick(
        [sonnet], {}, "code", "medium", _prefs(), set(), _Rng(0.9),
        coordinator_tool="claude",
        executor_mode="subagents",
    )

    assert chosen is not None
    assert chosen.model == "sonnet"
    assert "subagents mode" in reason


# --- own mode ---------------------------------------------------------------


def test_own_review_filters_same_model_and_falls_through():
    """In own mode, same-model candidates are filtered; falls through if needed."""
    sonnet = _ex("claude:sonnet")
    opus = _ex("claude:opus")
    codex = _ex("codex:c1", cost="paid")

    chosen, reason = pick(
        [sonnet, opus, codex], {}, "review", "medium", _prefs(), set(), _Rng(0.9),
        coordinator_tool="claude",
        executor_mode="own",
        review_for_executor="claude:sonnet",
    )

    # Should pick opus (different model in own pool), not sonnet
    assert chosen is not None
    assert chosen.model == "opus"


def test_own_review_exhausts_own_pool_and_escalates():
    """When own pool has only same-model candidates, escalate to included/free."""
    sonnet = _ex("claude:sonnet")
    codex = _ex("codex:c1", cost="paid")

    chosen, reason = pick(
        [sonnet, codex], {}, "review", "medium",
        _prefs(strong_order=("codex",)), set(), _Rng(0.9),
        coordinator_tool="claude",
        executor_mode="own",
        review_for_executor="claude:sonnet",
        strong=True,
    )

    # Own pool exhausted (only sonnet), should escalate to codex
    assert chosen is codex


def test_review_never_reaches_paid_without_strong():
    """Review must not reach paid executors without strong."""
    for mode in ("own", "subagents"):
        sonnet = _ex("claude:sonnet")
        codex = _ex("codex:c1", cost="paid")

        chosen, reason = pick(
            [sonnet, codex], {}, "review", "medium",
            _prefs(strong_order=("codex",)), set(), _Rng(0.9),
            coordinator_tool="claude",
            executor_mode=mode,
            review_for_executor="claude:sonnet",
        )

        assert chosen is None
        assert "no reviewer available" in reason


def test_own_review_no_candidates_at_all():
    """When no candidate at all avoids the executor's model, report it."""
    sonnet = _ex("claude:sonnet")

    chosen, reason = pick(
        [sonnet], {}, "review", "medium", _prefs(), set(), _Rng(0.9),
        coordinator_tool="claude",
        executor_mode="own",
        review_for_executor="claude:sonnet",
    )

    assert chosen is None
    assert "executor's model" in reason
    assert "sonnet" in reason


def _stat(score: float, duration: float = 5.0) -> object:
    return type("S", (), {"score": score, "mean_duration_s": duration, "n": 1, "last": "2026-10-25"})()


def test_own_review_tier_order_own_then_included_then_free():
    """In own mode review, candidate order is (1) own CLI, (2) included, (3) free."""
    own = _ex("claude:opus", cost="included")
    inc = _ex("codex:c1", cost="included")
    free = _ex("opencode:free-m", cost="free")
    stats = {
        ("claude:opus", "review"): _stat(10.0),
        ("codex:c1", "review"): _stat(50.0),
        ("opencode:free-m", "review"): _stat(99.0),
    }

    # (1) Own CLI with different model wins over included and free despite lower score.
    chosen, _ = pick(
        [own, inc, free], stats, "review", "medium", _prefs(), set(), _Rng(0.9),
        coordinator_tool="claude",
        executor_mode="own",
        review_for_executor="claude:sonnet",
    )
    assert chosen is own

    # (2) When own CLI has only the executor's model, included wins over free despite lower score.
    same_own = _ex("claude:sonnet", cost="included")
    chosen, _ = pick(
        [same_own, inc, free], stats, "review", "medium", _prefs(), set(), _Rng(0.9),
        coordinator_tool="claude",
        executor_mode="own",
        review_for_executor="claude:sonnet",
    )
    assert chosen is inc

    # (3) When own CLI and included are exhausted, free is chosen.
    chosen, _ = pick(
        [same_own, free], stats, "review", "medium", _prefs(), set(), _Rng(0.9),
        coordinator_tool="claude",
        executor_mode="own",
        review_for_executor="claude:sonnet",
    )
    assert chosen is free


def test_subagents_review_tier_order_own_then_included_then_free():
    """In subagents mode review, candidate order is (1) own CLI, (2) included, (3) free."""
    own = _ex("claude:opus", cost="included")
    inc = _ex("codex:c1", cost="included")
    free = _ex("opencode:free-m", cost="free")
    stats = {
        ("claude:opus", "review"): _stat(10.0),
        ("codex:c1", "review"): _stat(50.0),
        ("opencode:free-m", "review"): _stat(99.0),
    }

    # (1) Own CLI with different model wins as a dispatchable executor:
    # ``dispatch --review`` must be able to run whatever ``pick --review`` says.
    chosen, reason = pick(
        [own, inc, free], stats, "review", "medium", _prefs(), set(), _Rng(0.9),
        coordinator_tool="claude",
        executor_mode="subagents",
        review_for_executor="claude:sonnet",
    )
    assert chosen is own
    assert not chosen.id.startswith("native:")

    # (2) When own CLI has only same-model, included is picked as regular executor (not native).
    same_own = _ex("claude:sonnet", cost="included")
    chosen, _ = pick(
        [same_own, inc, free], stats, "review", "medium", _prefs(), set(), _Rng(0.9),
        coordinator_tool="claude",
        executor_mode="subagents",
        review_for_executor="claude:sonnet",
    )
    assert chosen is inc
    assert chosen.id == "codex:c1"

    # (3) When own CLI and included are exhausted, free is chosen as regular executor.
    chosen, _ = pick(
        [same_own, free], stats, "review", "medium", _prefs(), set(), _Rng(0.9),
        coordinator_tool="claude",
        executor_mode="subagents",
        review_for_executor="claude:sonnet",
    )
    assert chosen is free
    assert chosen.id == "opencode:free-m"


def test_review_existing_scoring_applies_within_each_tier():
    """Within each tier, the existing scoring (e.g. decayed score) selects the candidate."""
    opus = _ex("claude:opus", cost="included")
    haiku = _ex("claude:haiku", cost="included")
    stats = {
        ("claude:opus", "review"): _stat(40.0),
        ("claude:haiku", "review"): _stat(95.0),
    }

    chosen, _ = pick(
        [opus, haiku], stats, "review", "medium", _prefs(), set(), _Rng(0.9),
        coordinator_tool="claude",
        executor_mode="own",
        review_for_executor="claude:sonnet",
    )
    assert chosen is haiku


def test_review_rejects_same_model_across_different_clis():
    """A reviewer with the executor's model is rejected even on a different CLI."""
    sonnet_claude = _ex("claude:sonnet", cost="included")
    sonnet_opencode = _ex("opencode:sonnet", cost="free")

    chosen, reason = pick(
        [sonnet_claude, sonnet_opencode], {}, "review", "medium", _prefs(), set(), _Rng(0.9),
        coordinator_tool="claude",
        executor_mode="own",
        review_for_executor="claude:sonnet",
    )
    assert chosen is None
    assert "executor's model" in reason
    assert "sonnet" in reason


def test_review_rejects_model_from_native_executor_id():
    """review_for_executor formatted as native:<cli>/<model> extracts and rejects that model."""
    sonnet = _ex("claude:sonnet", cost="included")
    opus = _ex("claude:opus", cost="included")

    chosen, _ = pick(
        [sonnet, opus], {}, "review", "medium", _prefs(), set(), _Rng(0.9),
        coordinator_tool="claude",
        executor_mode="subagents",
        review_for_executor="native:claude/sonnet",
    )
    assert chosen is not None
    assert chosen.model == "opus"


def test_review_rejects_same_model_with_effort_suffix():
    """Reviewer with or without effort suffix matches and is rejected."""
    sonnet_low = _ex("claude:sonnet-low", cost="included")
    opus = _ex("claude:opus", cost="included")

    chosen, _ = pick(
        [sonnet_low, opus], {}, "review", "medium", _prefs(), set(), _Rng(0.9),
        coordinator_tool="claude",
        executor_mode="own",
        review_for_executor="claude:sonnet-high",
    )
    assert chosen is opus


# --- free_first mode unchanged ----------------------------------------------


def test_free_first_review_ignores_review_for_executor():
    """free_first mode does not enforce the different-model rule."""
    sonnet = _ex("claude:sonnet")

    chosen, reason = pick(
        [sonnet], {}, "review", "medium", _prefs(), set(), _Rng(0.9),
        coordinator_tool="claude",
        executor_mode="free_first",
        review_for_executor="claude:sonnet",
    )

    # free_first doesn't filter by review_for_executor
    assert chosen is sonnet


def test_free_first_review_without_coordinator_tool():
    """free_first review works without coordinator_tool (no filtering)."""
    sonnet = _ex("claude:sonnet")

    chosen, reason = pick(
        [sonnet], {}, "review", "medium", _prefs(), set(), _Rng(0.9),
        executor_mode="free_first",
        review_for_executor="claude:sonnet",
    )

    assert chosen is sonnet


def test_review_after_mode_switch_never_picks_the_native_model(
    git_repo, monkeypatch, capsys
):
    """A native exec author must not be reviewed by its own CLI model.

    The piece was executed as ``native:claude/sonnet`` (subagents mode),
    the job was then switched to ``free_first`` via ``job set
    --executors``, and discovery offers ``claude:sonnet``,
    ``claude:opus`` and one free executor (mocked like
    ``test_job_new_survives_a_broken_discovery`` in ``test_cli.py``).
    ``pick --review`` must not answer ``claude:sonnet``.
    """
    from crew_chief import cli as cli_mod
    from crew_chief import records
    from crew_chief.cli import main
    from crew_chief.discover import Discovery

    job = records.new_job(
        git_repo, "Mode switch", "do it", True, "reason",
        executors="subagents",
    )
    records.add_piece(job, "Change", "code", "low")
    records.add_attempt(job, 1, {
        "executor": "native:claude/sonnet", "role": "exec",
        "status": "ok", "cost": "free",
    })
    # Failed review attempts leave out the alternatives, so the author's
    # own model is the only viable candidate unless it is excluded.
    records.add_attempt(job, 1, {
        "executor": "claude:opus", "role": "review",
        "status": "failed", "cost": "free",
    })
    records.add_attempt(job, 1, {
        "executor": "opencode:free-m", "role": "review",
        "status": "failed", "cost": "free",
    })
    records.save_job(git_repo, job)

    assert main([
        "job", "set", "--project", str(git_repo),
        "--job", job["id"], "--executors", "free_first",
    ]) == 0
    capsys.readouterr()

    sonnet = _ex("claude:sonnet", cost="free")
    opus = _ex("claude:opus", cost="free")
    free = _ex("opencode:free-m", cost="free")

    def _fake_discover(prefs):
        return Discovery(
            executors=[sonnet, opus, free], notes=[], listed={},
        )

    monkeypatch.setattr(cli_mod, "discover_full", _fake_discover)

    main([
        "pick", "--project", str(git_repo), "--job", job["id"],
        "--piece", "1", "--review",
    ])
    out = capsys.readouterr().out
    first = out.splitlines()[0].strip() if out.splitlines() else ""
    assert first != "claude:sonnet", (
        f"pick --review chose the author's own model: {out!r}"
    )


def test_free_first_review_excludes_the_included_family(
    git_repo, monkeypatch, capsys
):
    """In free_first, a review by an included executor excludes the whole family.

    The piece was executed by ``claude:sonnet`` (included cost), discovery
    offers ``claude:sonnet`` (included), ``claude:opus`` (included) and one
    free executor. The free executor already failed review, so ``claude:opus``
    is the only viable candidate unless the whole ``sub:claude`` family is
    excluded. ``pick --review`` must not answer ``claude:opus``.
    """
    from crew_chief import cli as cli_mod
    from crew_chief import records
    from crew_chief.cli import main
    from crew_chief.discover import Discovery

    job = records.new_job(
        git_repo, "Free review", "do it", True, "reason",
        executors="free_first",
    )
    records.add_piece(job, "Change", "code", "low")
    records.add_attempt(job, 1, {
        "executor": "claude:sonnet", "role": "exec",
        "status": "ok", "cost": "included",
    })
    # Leave out the free alternative, so same-family opus is the only
    # viable candidate unless it is excluded.
    records.add_attempt(job, 1, {
        "executor": "opencode:free-m", "role": "review",
        "status": "failed", "cost": "free",
    })
    records.save_job(git_repo, job)

    sonnet = _ex("claude:sonnet", cost="included")
    opus = _ex("claude:opus", cost="included")
    free = _ex("opencode:free-m", cost="free")

    def _fake_discover(prefs):
        return Discovery(
            executors=[sonnet, opus, free], notes=[], listed={},
        )

    monkeypatch.setattr(cli_mod, "discover_full", _fake_discover)

    main([
        "pick", "--project", str(git_repo), "--job", job["id"],
        "--piece", "1", "--review",
    ])
    out = capsys.readouterr().out
    first = out.splitlines()[0].strip() if out.splitlines() else ""
    assert first != "claude:opus", (
        f"pick --review chose the same subscription family: {out!r}"
    )


def test_own_review_allows_the_included_family(
    git_repo, monkeypatch, capsys
):
    """In own mode, a review by an included executor allows another model.

    Same setup as above but with ``executors="own"``: the FR-042 relaxation
    applies, so ``pick --review`` must answer ``claude:opus``.
    """
    from crew_chief import cli as cli_mod
    from crew_chief import records
    from crew_chief.cli import main
    from crew_chief.discover import Discovery

    job = records.new_job(
        git_repo, "Own review", "do it", True, "reason",
        executors="own",
    )
    records.add_piece(job, "Change", "code", "low")
    records.add_attempt(job, 1, {
        "executor": "claude:sonnet", "role": "exec",
        "status": "ok", "cost": "included",
    })
    records.add_attempt(job, 1, {
        "executor": "opencode:free-m", "role": "review",
        "status": "failed", "cost": "free",
    })
    records.save_job(git_repo, job)

    sonnet = _ex("claude:sonnet", cost="included")
    opus = _ex("claude:opus", cost="included")
    free = _ex("opencode:free-m", cost="free")

    def _fake_discover(prefs):
        return Discovery(
            executors=[sonnet, opus, free], notes=[], listed={},
        )

    monkeypatch.setattr(cli_mod, "discover_full", _fake_discover)

    assert main([
        "pick", "--project", str(git_repo), "--job", job["id"],
        "--piece", "1", "--review",
    ]) == 0
    out = capsys.readouterr().out
    first = out.splitlines()[0].strip() if out.splitlines() else ""
    assert first == "claude:opus", (
        f"pick --review did not pick the other included model: {out!r}"
    )