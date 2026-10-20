"""Tests for grading, decayed scoreboard stats and free-first picking."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from crew_chief.config import Prefs
from crew_chief.records import (
    add_attempt,
    add_correction,
    add_piece,
    jobs_dir,
    new_job,
    save_job,
    set_grade,
)
from crew_chief.scoring import (
    _rank_key,
    PENALTIES,
    Stat,
    build_stats,
    effort_ceiling,
    effort_rank,
    free_failures,
    grade,
    next_effort,
    pick,
    quota_cooldown,
    quota_family,
    render_scoreboard,
)
from crew_chief.tools import Executor

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)


# --- helpers ---------------------------------------------------------------


#: Tool kind of each tool name the tests use; any other name is ``opencode``.
_KINDS = {"antigravity": "antigravity", "claude": "claude-code",
          "ghost": "claude-code", "codex": "codex"}


def _ex(
    id_: str,
    cost: str = "free",
    name: str | None = None,
    work_types: list[str] | None = None,
) -> Executor:
    """Build an executor from ``"<tool>:<model>"`` with an optional cost.

    The kind follows the tool name (``_KINDS``); ``work_types`` sets the
    tool's explicit list.
    """
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
    """A :class:`Prefs` with just the fields ``pick`` reads, plus defaults."""
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
    """Stub rng whose every ``random()`` call returns the same fixed value.

    ``pick`` calls ``random()`` twice on the second-chance path (once for the
    explore check, once for the uniform choice), so ``0.0`` drives both:
    ``0.0 < explore_rate`` is true and the uniform index is ``0``.
    """

    def __init__(self, value: float) -> None:
        self.value = value

    def random(self) -> float:
        return self.value


def _stats(*entries: tuple[str, str, float | None, float | None]) -> dict:
    """Stats dict from ``(executor, type, score, mean_duration_s)`` tuples."""
    stats: dict = {}
    for executor, type_, score, duration in entries:
        stats[(executor, type_)] = Stat(
            executor=executor,
            type=type_,
            n=1,
            score=score,
            mean_duration_s=duration,
            last=NOW.isoformat(),
        )
    return stats


def _g(
    executor: str,
    score: float,
    when: datetime = NOW,
    explain: str = "test",
) -> dict:
    """A stored grade: the grade dict plus executor and UTC ``graded_at``."""
    return {
        "score": score,
        "explain": explain,
        "executor": executor,
        "graded_at": when.isoformat(),
    }


def _attempt(
    executor: str,
    status: str = "ok",
    cost: str = "free",
    role: str = "exec",
    duration_s: float | None = None,
) -> dict:
    """One stored attempt dict; ``duration_s`` is added only when given."""
    attempt = {
        "executor": executor,
        "role": role,
        "status": status,
        "cost": cost,
    }
    if duration_s is not None:
        attempt["duration_s"] = duration_s
    return attempt


def _project(
    tmp_path: Path,
    pieces: list[dict],
    name: str = "project",
) -> Path:
    """Create a project whose single job holds one piece per spec."""
    project = tmp_path / name
    job = new_job(
        project,
        title="Job",
        request="do it",
        distributed=True,
        reason="test",
        now=datetime(2026, 10, 1, 9, 0),
    )
    for spec in pieces:
        number = add_piece(
            job,
            spec.get("title", "P"),
            spec["type"],
            spec.get("risk", "low"),
        )
        for attempt in spec.get("attempts", []):
            add_attempt(job, number, attempt)
        if spec.get("grade") is not None:
            set_grade(job, number, spec["grade"])
        for stored in spec.get("grades", []):
            set_grade(job, number, stored)
        if spec.get("legacy_grade") is not None:
            # A record written before grades were kept per executor.
            piece = job["pieces"][-1]
            piece.pop("grades", None)
            piece["grade"] = spec["legacy_grade"]
    save_job(project, job)
    return project


def _job_with_piece(
    tmp_path: Path,
    attempts=(),
    corrections=(),
) -> tuple[dict, int]:
    """A job with one ``code`` piece carrying the given attempts/corrections."""
    project = tmp_path / "project"
    job = new_job(
        project,
        title="Job",
        request="do it",
        distributed=True,
        reason="test",
        now=NOW,
    )
    number = add_piece(job, "P", "code", "low")
    for attempt in attempts:
        add_attempt(job, number, attempt)
    for reason, executor in corrections:
        add_correction(job, number, reason, executor=executor)
    return job, number


# --- grade -----------------------------------------------------------------


def test_grade_first_try_full_adherence_is_100():
    assert grade(0, 5, 0, 0, [])["score"] == 100


def test_grade_formula_values():
    g = grade(1, 4, 1, 1, [])

    assert g["score"] == 60 and "1 correction" in g["explain"]  # 80 - 15 - 5


def test_grade_clamps_and_rejects_unknown_penalty():
    assert grade(5, 0, 3, 4, ["outside_write"])["score"] == 0
    with pytest.raises(ValueError):
        grade(0, 1, 0, 0, ["lazy"])


def test_grade_returns_its_inputs_and_lists_every_term_once():
    g = grade(1, 4, 1, 0, [])

    assert g["explain"] == (
        "adherence 4/5 → 80; 1 correction −15; 0 extra; penalties none → 65"
    )
    assert (g["corrections"], g["done"], g["missing"], g["extra"]) == (
        1,
        4,
        1,
        0,
    )
    assert g["penalties"] == []


def test_grade_lists_penalties_with_their_weights():
    g = grade(0, 3, 1, 0, ["model_mismatch"])

    assert g["score"] == 45
    assert "model_mismatch −30" in g["explain"]
    assert g["penalties"] == ["model_mismatch"]


def test_grade_with_no_pieces_is_full_adherence():
    assert grade(0, 0, 0, 0, [])["score"] == 100


def test_penalty_vocabulary_is_fixed():
    assert PENALTIES == {
        "destructive": 40,
        "outside_write": 60,
        "false_done": 40,
        "model_mismatch": 30,
    }


def test_grade_pluralises_corrections_and_shows_extra_deduction():
    g = grade(2, 2, 0, 3, [])

    assert g["score"] == 55  # 100 - 30 - 15
    assert "2 corrections −30" in g["explain"]
    assert "3 extra −15" in g["explain"]


# --- build_stats -----------------------------------------------------------


def test_every_executor_graded_on_a_piece_counts(tmp_path):
    project = _project(
        tmp_path,
        [
            {
                "type": "code",
                "attempts": [
                    {"executor": "beam:a", "role": "exec", "status": "ok"},
                    {"executor": "minimax:b", "role": "exec", "status": "ok"},
                ],
                "grades": [_g("beam:a", 52, NOW), _g("minimax:b", 100, NOW)],
            },
        ],
    )

    stats = build_stats(project, now=NOW)

    assert stats[("beam:a", "code")].score == 52
    assert stats[("minimax:b", "code")].score == 100
    assert stats[("beam:a", "code")].attempts_per_pass == 1
    board = render_scoreboard(project, now=NOW)
    assert "| minimax:b | 100 | 1 |" in board
    assert "| beam:a | 52 | 1 |" in board


def test_legacy_single_grade_is_still_read(tmp_path):
    project = _project(
        tmp_path,
        [{"type": "code", "legacy_grade": _g("opencode:a", 70, NOW)}],
    )

    stats = build_stats(project, now=NOW)

    assert stats[("opencode:a", "code")].score == 70
    assert stats[("opencode:a", "code")].n == 1


def test_decay_prefers_recent(tmp_path):
    old = NOW - timedelta(days=120)
    project = _project(
        tmp_path,
        [
            {"type": "code", "grade": _g("opencode:a", 100, old)},
            {"type": "code", "grade": _g("opencode:a", 20, NOW)},
        ],
    )

    stats = build_stats(project, half_life_days=30, now=NOW)

    stat = stats[("opencode:a", "code")]
    assert stat.n == 2
    assert stat.score < 60


def test_stats_key_on_executor_and_type(tmp_path):
    project = _project(
        tmp_path,
        [
            {"type": "code", "grade": _g("opencode:a", 80, NOW)},
            {"type": "code", "grade": _g("codex:g", 60, NOW)},
            {"type": "tests", "grade": _g("opencode:a", 40, NOW)},
        ],
    )

    stats = build_stats(project, now=NOW)

    assert set(stats) == {
        ("opencode:a", "code"),
        ("codex:g", "code"),
        ("opencode:a", "tests"),
    }
    assert stats[("opencode:a", "code")].score == pytest.approx(80.0)
    assert stats[("opencode:a", "tests")].score == pytest.approx(40.0)


def test_stats_ignore_ungraded_pieces_and_count_review_as_a_type(tmp_path):
    project = _project(
        tmp_path,
        [
            {"type": "code", "grade": None},
            {"type": "review", "grade": _g("claude:r", 90, NOW)},
        ],
    )

    stats = build_stats(project, now=NOW)

    assert set(stats) == {("claude:r", "review")}
    assert stats[("claude:r", "review")].score == pytest.approx(90.0)


def test_stats_ignore_grade_without_executor_or_time(tmp_path):
    project = _project(
        tmp_path,
        [
            {"type": "code", "grade": {"score": 90, "explain": "no metadata"}},
            {"type": "code", "grade": _g("opencode:a", 90, NOW)},
        ],
    )

    stats = build_stats(project, now=NOW)

    assert set(stats) == {("opencode:a", "code")}


def test_mean_duration_uses_ok_exec_attempts_of_that_type(tmp_path):
    project = _project(
        tmp_path,
        [
            {
                "type": "code",
                "grade": _g("opencode:a", 90, NOW),
                "attempts": [
                    _attempt("opencode:a", duration_s=40),
                    _attempt("opencode:a", status="failed", duration_s=1000),
                    _attempt("opencode:a", role="review", duration_s=1000),
                    _attempt("codex:g", cost="paid", duration_s=1000),
                ],
            },
            {
                "type": "code",
                "grade": _g("opencode:a", 70, NOW),
                "attempts": [_attempt("opencode:a", duration_s=60)],
            },
        ],
    )

    stat = build_stats(project, now=NOW)[("opencode:a", "code")]

    assert stat.mean_duration_s == pytest.approx(50.0)


def test_mean_duration_is_none_without_ok_attempts(tmp_path):
    project = _project(
        tmp_path,
        [
            {
                "type": "code",
                "grade": _g("opencode:a", 90, NOW),
                "attempts": [_attempt("opencode:a", status="failed", duration_s=40)],
            },
        ],
    )

    stat = build_stats(project, now=NOW)[("opencode:a", "code")]

    assert stat.mean_duration_s is None


def test_mean_duration_does_not_mix_types(tmp_path):
    project = _project(
        tmp_path,
        [
            {
                "type": "code",
                "grade": _g("opencode:a", 90, NOW),
                "attempts": [_attempt("opencode:a", duration_s=40)],
            },
            {
                "type": "docs",
                "grade": _g("opencode:a", 90, NOW),
                "attempts": [_attempt("opencode:a", duration_s=600)],
            },
        ],
    )

    stats = build_stats(project, now=NOW)

    assert stats[("opencode:a", "code")].mean_duration_s == pytest.approx(40.0)
    assert stats[("opencode:a", "docs")].mean_duration_s == pytest.approx(600.0)


def test_last_is_the_most_recent_grade(tmp_path):
    project = _project(
        tmp_path,
        [
            {"type": "code", "grade": _g("opencode:a", 90, NOW - timedelta(days=5))},
            {"type": "code", "grade": _g("opencode:a", 90, NOW)},
        ],
    )

    stat = build_stats(project, now=NOW)[("opencode:a", "code")]

    assert stat.last is not None and stat.last.startswith("2026-10-17")


def test_build_stats_ignores_corrupt_jobs(tmp_path):
    project = _project(
        tmp_path, [{"type": "code", "grade": _g("opencode:a", 90, NOW)}]
    )
    (jobs_dir(project) / "broken.json").write_text("{oops", encoding="utf-8")

    stats = build_stats(project, now=NOW)

    assert set(stats) == {("opencode:a", "code")}


# --- pick ------------------------------------------------------------------


def test_pick_free_first_by_score():
    a = _ex("opencode:a")
    b = _ex("opencode:b")
    c = _ex("claude:c", cost="paid")
    stats = _stats(
        ("opencode:a", "code", 80.0, 10.0),
        ("opencode:b", "code", 50.0, 5.0),
    )

    chosen, reason = pick(
        [b, a, c], stats, "code", "medium", _prefs(), set(), _Rng(0.0)
    )

    assert chosen is a
    assert reason.startswith("best score")


def test_pick_second_chance_only_low_risk():
    best = _ex("opencode:best")
    unrated = _ex("opencode:new")
    stats = _stats(("opencode:best", "code", 80.0, 10.0))

    chosen, reason = pick(
        [best, unrated], stats, "code", "low", _prefs(), set(), _Rng(0.0)
    )

    assert chosen is unrated
    assert reason.startswith("second chance:")

    chosen_high, reason_high = pick(
        [best, unrated], stats, "code", "high", _prefs(), set(), _Rng(0.0)
    )

    assert chosen_high is best
    assert reason_high.startswith("best score")


def test_pick_second_chance_skips_the_median_and_above():
    top = _ex("opencode:top")  # 80
    middle = _ex("opencode:mid")  # 30 == median, not below it
    low = _ex("opencode:low")  # 20 < median
    stats = _stats(
        ("opencode:top", "code", 80.0, 10.0),
        ("opencode:mid", "code", 30.0, 10.0),
        ("opencode:low", "code", 20.0, 10.0),
    )

    chosen, reason = pick(
        [top, middle, low], stats, "code", "low", _prefs(), set(), _Rng(0.0)
    )

    assert chosen is low
    assert reason.startswith("second chance:")
    assert "below median" in reason


def test_pick_second_chance_holds_everyone_when_nothing_is_rated():
    a = _ex("opencode:a")
    b = _ex("opencode:b")

    chosen, reason = pick(
        [a, b], {}, "code", "low", _prefs(), set(), _Rng(0.0)
    )

    assert chosen is a  # first of the ranked pool
    assert reason.startswith("second chance:")


def test_pick_does_not_explore_above_the_explore_rate():
    best = _ex("opencode:best")
    unrated = _ex("opencode:new")
    stats = _stats(("opencode:best", "code", 80.0, 10.0))

    chosen, reason = pick(
        [best, unrated], stats, "code", "low", _prefs(), set(), _Rng(0.9)
    )

    assert chosen is best
    assert reason.startswith("best score")


def test_pick_does_not_explore_when_rate_is_zero():
    best = _ex("opencode:best")
    unrated = _ex("opencode:new")
    stats = _stats(("opencode:best", "code", 80.0, 10.0))

    chosen, _ = pick(
        [best, unrated],
        stats,
        "code",
        "low",
        _prefs(explore_rate=0.0),
        set(),
        _Rng(0.0),
    )

    assert chosen is best


def test_pick_ties_break_by_duration_then_id():
    slow = _ex("opencode:a")
    fast = _ex("opencode:b")
    stats = _stats(
        ("opencode:a", "code", 80.0, 50.0),
        ("opencode:b", "code", 80.0, 30.0),
    )

    chosen, _ = pick(
        [slow, fast], stats, "code", "medium", _prefs(), set(), _Rng(0.0)
    )

    assert chosen is fast

    first = _ex("opencode:a")
    second = _ex("opencode:b")
    same = _stats(
        ("opencode:a", "code", 80.0, 30.0),
        ("opencode:b", "code", 80.0, 30.0),
    )

    chosen, _ = pick(
        [second, first], same, "code", "medium", _prefs(), set(), _Rng(0.0)
    )

    assert chosen is first  # executor id ascending


def test_pick_missing_duration_goes_last():
    unknown = _ex("opencode:a")
    known = _ex("opencode:b")
    stats = _stats(
        ("opencode:a", "code", 80.0, None),
        ("opencode:b", "code", 80.0, 30.0),
    )

    chosen, _ = pick(
        [unknown, known], stats, "code", "medium", _prefs(), set(), _Rng(0.0)
    )

    assert chosen is known


def test_pick_unrated_goes_last_in_the_free_pool():
    unrated = _ex("opencode:new")
    weak = _ex("opencode:weak")
    stats = _stats(("opencode:weak", "code", 10.0, 5.0))

    chosen, reason = pick(
        [unrated, weak], stats, "code", "medium", _prefs(), set(), _Rng(0.0)
    )

    assert chosen is weak
    assert reason.startswith("best score")


def test_pick_unrated_reason_when_there_is_no_history():
    only = _ex("opencode:new")

    chosen, reason = pick(
        [only], {}, "code", "medium", _prefs(), set(), _Rng(0.0)
    )

    assert chosen is only
    assert reason.startswith("unrated, no history")


def test_pick_drops_excluded_and_never_picks_forbidden():
    a = _ex("opencode:a")
    b = _ex("opencode:b")
    forbidden = _ex("opencode:f", cost="forbidden")
    stats = _stats(
        ("opencode:a", "code", 90.0, 5.0),
        ("opencode:b", "code", 50.0, 5.0),
    )

    chosen, _ = pick(
        [a, b, forbidden],
        stats,
        "code",
        "medium",
        _prefs(),
        {"opencode:a"},
        _Rng(0.0),
    )

    assert chosen is b


def test_pick_returns_none_without_a_candidate():
    forbidden = _ex("opencode:f", cost="forbidden")

    assert pick([forbidden], {}, "review", "medium", _prefs(), set(),
                _Rng(0.0)) == (None, "no executor available")
    assert pick([], {}, "review", "medium", _prefs(), set(), _Rng(0.0)) == (
        None,
        "no executor available",
    )
    chosen, reason = pick([], {}, "code", "medium", _prefs(), set(), _Rng(0.0))
    assert chosen is None
    assert reason.startswith("no executor available: no free executor left")


def test_pick_escalates_when_no_free():
    codex = _ex("codex:c1", cost="paid")
    ghost = _ex("ghost:g1", cost="paid")

    chosen, reason = pick(
        [codex, ghost], {}, "review", "medium", _prefs(), set(), _Rng(0.0)
    )

    assert chosen is ghost
    assert reason.startswith("escalated: no free executor available")


def test_pick_strong_order_puts_unlisted_tools_last():
    codex = _ex("codex:c1", cost="paid")
    ghost = _ex("ghost:g1", cost="paid")

    chosen, _ = pick(
        [ghost, codex],
        {},
        "review",
        "medium",
        _prefs(strong_order=("codex",)),
        set(),
        _Rng(0.0),
    )

    assert chosen is codex


def test_pick_ignores_stats_of_other_types():
    a = _ex("opencode:a")
    b = _ex("opencode:b")
    stats = _stats(
        ("opencode:a", "docs", 99.0, 5.0),
        ("opencode:b", "code", 60.0, 5.0),
    )

    chosen, _ = pick(
        [a, b], stats, "code", "medium", _prefs(), set(), _Rng(0.0)
    )

    assert chosen is b


# --- free_failures ---------------------------------------------------------


def test_free_failures_counts_free_exec_attempts_that_did_not_succeed(tmp_path):
    job, number = _job_with_piece(
        tmp_path,
        attempts=[
            _attempt("opencode:a"),
            _attempt("opencode:a", status="failed"),
            _attempt("opencode:a", status="timeout"),
            _attempt("claude:c", status="failed", cost="paid"),
            _attempt("opencode:a", status="failed", role="review"),
        ],
    )

    assert free_failures(job, number) == 2



def test_free_failures_counts_no_change_attempts(tmp_path):
    job, number = _job_with_piece(
        tmp_path,
        attempts=[
            _attempt("opencode:a", status="no_change"),
            _attempt("opencode:b", status="no_change"),
        ],
    )

    assert free_failures(job, number) == 2


def test_free_failures_counts_proof_failed_attempts(tmp_path):
    job, number = _job_with_piece(
        tmp_path,
        attempts=[
            _attempt("opencode:a", status="proof_failed"),
            _attempt("opencode:b"),
        ],
    )

    assert free_failures(job, number) == 1


def test_free_failures_counts_corrections_from_the_free_executor(tmp_path):
    job, number = _job_with_piece(
        tmp_path,
        attempts=[_attempt("opencode:a", status="failed")],
        corrections=[
            ("renamed a parameter", "opencode:a"),
            ("unrelated nit", "claude:c"),
        ],
    )

    assert free_failures(job, number) == 2


def test_free_failures_is_zero_without_free_attempts(tmp_path):
    job, number = _job_with_piece(
        tmp_path,
        attempts=[_attempt("claude:c", status="failed", cost="paid")],
        corrections=[("nit", "claude:c")],
    )

    assert free_failures(job, number) == 0


def test_free_failures_unknown_piece_raises(tmp_path):
    job, _ = _job_with_piece(tmp_path)

    with pytest.raises(ValueError):
        free_failures(job, 7)


# --- render_scoreboard -----------------------------------------------------


def test_scoreboard_skips_corrupt(tmp_path):
    project = _project(
        tmp_path,
        [
            {
                "type": "code",
                "grade": _g("opencode:a", 80, NOW),
                "attempts": [_attempt("opencode:a", duration_s=42)],
            },
        ],
    )
    (jobs_dir(project) / "broken.json").write_text("{oops", encoding="utf-8")

    text = render_scoreboard(project, now=NOW)

    assert "Skipped records" in text
    assert "broken.json" in text
    assert "opencode:a" in text
    assert "42s" in text
    assert str(tmp_path) not in text


def test_scoreboard_without_corrupt_files_has_no_skipped_section(tmp_path):
    project = _project(
        tmp_path, [{"type": "code", "grade": _g("opencode:a", 80, NOW)}]
    )

    assert "Skipped records" not in render_scoreboard(project, now=NOW)


def test_scoreboard_row_shows_score_count_duration_and_date(tmp_path):
    project = _project(
        tmp_path, [{"type": "code", "grade": _g("opencode:a", 80, NOW)}]
    )

    text = render_scoreboard(project, now=NOW)
    row = next(line for line in text.splitlines() if line.startswith("| opencode:a"))
    cells = [cell.strip() for cell in row.strip("|").split("|")]

    assert cells == ["opencode:a", "80", "1", "—", "—", "—", "—", "2026-10-17"]


def test_scoreboard_orders_types_and_executors(tmp_path):
    project = _project(
        tmp_path,
        [
            {"type": "docs", "grade": _g("claude:d", 90, NOW)},
            {"type": "code", "grade": _g("opencode:low", 40, NOW)},
            {"type": "code", "grade": _g("opencode:high", 95, NOW)},
        ],
    )

    text = render_scoreboard(project, now=NOW)

    assert text.index("## code") < text.index("## docs")
    assert text.index("opencode:high") < text.index("opencode:low")


def test_scoreboard_of_an_empty_project_is_still_markdown(tmp_path):
    text = render_scoreboard(tmp_path / "nothing", now=NOW)

    assert text.startswith("# Scoreboard")
    assert "—" not in text


# --- pick: spread over a job and quota cooldown ----------------------------


def _job_with_picks(*picks_per_piece: list[str]) -> dict:
    """A job dict whose pieces carry ``picks`` for the given executor ids."""
    return {
        "id": "job",
        "pieces": [
            {
                "n": index,
                "type": "code",
                "attempts": [],
                "picks": [{"pick": executor} for executor in picks],
            }
            for index, picks in enumerate(picks_per_piece, start=1)
        ],
    }


def test_pick_spreads_unrated_executors_over_the_job():
    a = _ex("antigravity:opus")
    b = _ex("antigravity:sonnet")
    c = _ex("opencode:flash")
    job = _job_with_picks(["antigravity:opus"])

    chosen, reason = pick(
        [a, b, c], {}, "code", "medium", _prefs(exec_first=()), set(),
        _Rng(0.9), job=job,
    )

    # b and c are both unused; c's tool has not been used in the job yet.
    assert chosen is c
    assert reason.startswith("unrated, no history")


def test_pick_spreads_nine_pieces_in_a_row():
    pool = [
        _ex("antigravity:opus"),
        _ex("antigravity:sonnet"),
        _ex("opencode:flash"),
    ]
    job = _job_with_picks(*([] for _ in range(9)))

    chosen_ids = []
    for piece in job["pieces"]:
        chosen, _ = pick(
            pool, {}, "code", "medium", _prefs(exec_first=()), set(),
            _Rng(0.9), job=job,
        )
        piece["picks"].append({"pick": chosen.id})
        chosen_ids.append(chosen.id)

    assert {executor.id: chosen_ids.count(executor.id) for executor in pool} == {
        "antigravity:opus": 3,
        "antigravity:sonnet": 3,
        "opencode:flash": 3,
    }
    assert chosen_ids[:2] == ["antigravity:opus", "opencode:flash"]


def test_pick_counts_attempts_without_a_pick_as_use():
    a = _ex("opencode:a")
    b = _ex("opencode:b")
    job = {
        "id": "job",
        "pieces": [
            {"n": 1, "type": "code", "picks": [],
             "attempts": [_attempt("opencode:a")]},
        ],
    }

    chosen, _ = pick([a, b], {}, "code", "medium", _prefs(), set(), _Rng(0.9),
                     job=job)

    assert chosen is b


def test_pick_spread_prefers_an_unused_quota_family():
    # Two Antigravity Claude models share one quota: after opus-high is in
    # use, opus-low is no spread at all; gemini is.
    opus_low = _ex("antigravity:claude-opus-5-5-low")
    gemini = _ex("antigravity:gemini-4-argon-high")
    job = _job_with_picks(["antigravity:claude-opus-5-5-high"])

    chosen, _ = pick([opus_low, gemini], {}, "code", "medium", _prefs(), set(),
                     _Rng(0.9), job=job)

    assert chosen is gemini


def test_pick_spread_never_beats_a_better_score():
    best = _ex("opencode:best")
    other = _ex("opencode:other")
    stats = _stats(
        ("opencode:best", "code", 90.0, 10.0),
        ("opencode:other", "code", 50.0, 10.0),
    )
    job = _job_with_picks(["opencode:best"] * 5)

    chosen, _ = pick([best, other], stats, "code", "medium", _prefs(), set(),
                     _Rng(0.9), job=job)

    assert chosen is best


def test_pick_skips_executors_cooling_down_after_quota():
    a = _ex("antigravity:opus")
    b = _ex("opencode:flash")

    chosen, reason = pick(
        [a, b], {}, "code", "medium", _prefs(), set(), _Rng(0.9),
        cooling={"antigravity:opus"},
    )

    assert chosen is b
    assert "skipped for quota: antigravity:opus" in reason


def test_pick_escalates_when_every_free_executor_is_cooling():
    free = _ex("antigravity:gemini-4-argon-high")
    paid = _ex("codex:c1", cost="paid")

    chosen, reason = pick(
        [free, paid], {}, "review", "medium", _prefs(), set(), _Rng(0.0),
        cooling={"antigravity:gemini-4-argon-high"},
    )

    assert chosen is paid
    assert reason.startswith("escalated: no free executor available")
    assert "skipped for quota: antigravity:gemini-4-argon-high" in reason


def test_pick_with_everything_cooling_has_no_executor():
    free = _ex("opencode:flash")

    chosen, reason = pick(
        [free], {}, "code", "medium", _prefs(), set(), _Rng(0.0),
        cooling={"opencode:flash"},
    )

    assert chosen is None
    assert reason.startswith("no executor available")
    assert "skipped for quota: opencode:flash" in reason



@pytest.mark.parametrize(
    "executor_id, family",
    [
        ("antigravity:claude-opus-5-5-high", "antigravity:claude"),
        ("antigravity:claude-sonnet-5", "antigravity:claude"),
        ("antigravity:gemini-3-pro-low", "antigravity:gemini"),
        ("antigravity:gpt-oss-120b-medium", "antigravity:gpt"),
        ("antigravity:opus", "antigravity:opus"),
        ("opencode:x-free", "opencode:x-free"),
        ("codex:gpt-5.5-xhigh", "codex:gpt-5.5"),
        ("claude:opus-max", "claude:opus"),
        ("ghost:astra-minimal", "ghost:astra"),
        ("opencode:deepseek-flash", "opencode:deepseek-flash"),
    ],
)
def test_quota_family(executor_id, family):
    assert quota_family(executor_id) == family


def test_pick_skips_the_whole_antigravity_family_in_quota():
    high = _ex("antigravity:claude-opus-5-5-high")
    medium = _ex("antigravity:claude-opus-5-5-medium")
    sonnet = _ex("antigravity:claude-sonnet-5")
    gemini = _ex("antigravity:gemini-3-pro-high")

    chosen, reason = pick(
        [high, medium, sonnet, gemini], {}, "code", "medium", _prefs(), set(),
        _Rng(0.9), cooling={"antigravity:claude-opus-5-5-high"},
    )

    assert chosen is gemini
    assert reason.endswith(
        "skipped for quota: antigravity:claude-opus-5-5-high, "
        "antigravity:claude-opus-5-5-medium, antigravity:claude-sonnet-5"
    )


def test_pick_quota_on_one_opencode_model_spares_the_others():
    x = _ex("opencode:x-free")
    y = _ex("opencode:y-free")

    chosen, reason = pick(
        [x, y], {}, "code", "medium", _prefs(), set(), _Rng(0.9),
        cooling={"opencode:x-free"},
    )

    assert chosen is y
    assert reason.endswith("skipped for quota: opencode:x-free")


def _quota_attempt(executor: str, ended_min_ago: float, status: str = "quota"):
    started = NOW - timedelta(minutes=ended_min_ago, seconds=30)
    return {
        "executor": executor,
        "role": "exec",
        "status": status,
        "cost": "free",
        "started": started.isoformat(),
        "duration_s": 30.0,
    }


def test_quota_cooldown_reads_every_job_of_the_project(tmp_path):
    project = _project(
        tmp_path,
        [{"type": "code", "attempts": [
            _quota_attempt("antigravity:opus", 10),
            _quota_attempt("antigravity:old", 61),
            _quota_attempt("opencode:fail", 5, status="failed"),
        ]}],
    )
    other = new_job(project, "Other", "x", True, "y",
                    now=datetime(2026, 10, 2, 9, 0))
    add_piece(other, "P", "docs", "low")
    add_attempt(other, 1, _quota_attempt("opencode:docs", 59))
    save_job(project, other)

    cooling = quota_cooldown(project, 60, now=NOW)

    assert cooling == {"antigravity:opus", "opencode:docs"}
    assert quota_cooldown(project, 0, now=NOW) == set()


# --- scoreboard metrics: attempts per pass, steps, tokens ------------------


def _metered(executor, status="ok", steps=None, tokens=None, estimated=False,
             duration_s=10.0):
    attempt = _attempt(executor, status=status, duration_s=duration_s)
    attempt.update(steps=steps, tokens_total=tokens, tokens_estimated=estimated)
    return attempt


def test_stats_carry_attempts_per_pass_steps_and_tokens(tmp_path):
    project = _project(
        tmp_path,
        [
            {"type": "code", "grade": _g("opencode:a", 80), "attempts": [
                _metered("opencode:a", status="failed", steps=3, tokens=900),
                _metered("opencode:a", steps=6, tokens=2000, duration_s=20.0),
            ]},
            {"type": "code", "grade": _g("opencode:a", 90), "attempts": [
                _metered("opencode:a", steps=4, tokens=1000, duration_s=40.0),
                _metered("opencode:a", status="brief_refused"),
            ]},
        ],
    )

    stat = build_stats(project, now=NOW)[("opencode:a", "code")]

    assert stat.attempts_per_pass == 1.5  # 3 runs, 2 pieces passed
    assert stat.mean_steps == 5.0
    assert stat.mean_tokens == 1500.0
    assert stat.tokens_estimated is False
    assert stat.mean_duration_s == 30.0


def test_stats_flag_estimated_tokens_and_unknown_steps(tmp_path):
    project = _project(
        tmp_path,
        [{"type": "code", "grade": _g("cursor:a", 70), "attempts": [
            _metered("cursor:a", tokens=400, estimated=True),
        ]}],
    )

    stat = build_stats(project, now=NOW)[("cursor:a", "code")]

    assert stat.mean_steps is None
    assert stat.mean_tokens == 400.0
    assert stat.tokens_estimated is True


def test_stats_attempts_per_pass_is_none_without_a_pass(tmp_path):
    project = _project(
        tmp_path,
        [{"type": "code", "grade": _g("opencode:a", 0), "attempts": [
            _metered("opencode:a", status="failed"),
        ]}],
    )

    assert build_stats(project, now=NOW)[("opencode:a", "code")].attempts_per_pass is None


def test_scoreboard_row_shows_the_new_metric_columns(tmp_path):
    project = _project(
        tmp_path,
        [
            {"type": "code", "grade": _g("opencode:a", 80, NOW), "attempts": [
                _metered("opencode:a", status="quota"),
                _metered("opencode:a", steps=7, tokens=12345, duration_s=95.0),
            ]},
            {"type": "code", "grade": _g("cursor:b", 60, NOW), "attempts": [
                _metered("cursor:b", tokens=800, estimated=True, duration_s=30.0),
            ]},
        ],
    )

    text = render_scoreboard(project, now=NOW)

    assert (
        "| executor | score | n | attempts/pass | mean steps | mean tokens "
        "| mean duration | last graded |"
    ) in text
    assert "| opencode:a | 80 | 1 | 2 | 7 | 12345 | 95s | 2026-10-17 |" in text
    assert "| cursor:b | 60 | 1 | 1 | — | ~800 | 30s | 2026-10-17 |" in text


# --- pick: execute vs think work types --------------------------------------


def test_pick_review_accepts_a_free_opencode_model():
    opencode = _ex("opencode:x-free")
    sonnet = _ex("claude:sonnet", cost="paid")
    stats = _stats(("opencode:x-free", "review", 70.0, 5.0))

    chosen, reason = pick(
        [opencode, sonnet], stats, "review", "medium", _prefs(), set(),
        _Rng(0.9),
    )

    assert chosen is opencode
    assert reason == "best score 70 (opencode:x-free)"


def test_pick_review_ranks_free_executors_by_score_not_exec_first():
    opencode = _ex("opencode:x-free")
    argon = _ex("antigravity:gemini-4-argon-high")
    stats = _stats(("antigravity:gemini-4-argon-high", "review", 90.0, 5.0),
                   ("opencode:x-free", "review", 60.0, 5.0))

    chosen, _ = pick([opencode, argon], stats, "review", "medium", _prefs(),
                     set(), _Rng(0.9))

    assert chosen is argon


def test_pick_review_escalates_to_paid_when_no_free_fits():
    exec_only = ["code", "tests", "refactor", "docs", "data"]
    x = _ex("opencode:x-free", work_types=exec_only)
    y = _ex("opencode:y-free", work_types=exec_only)
    claude = _ex("claude:sonnet", cost="paid")
    codex = _ex("codex:c1", cost="paid")

    chosen, reason = pick(
        [x, y, claude, codex], {}, "review", "medium",
        _prefs(strong_order=("codex", "claude")), set(), _Rng(0.0),
    )

    assert chosen is codex
    assert reason == (
        "escalated: no free executor available (codex:c1); "
        "not for review: opencode"
    )


def test_pick_code_puts_opencode_before_other_free_executors():
    gemini = _ex("antigravity:gemini-4-argon-high")
    opencode = _ex("opencode:x-free")
    stats = _stats(("antigravity:gemini-4-argon-high", "code", 95.0, 5.0))

    chosen, reason = pick(
        [gemini, opencode], stats, "code", "medium", _prefs(), set(), _Rng(0.9)
    )

    assert chosen is opencode
    assert "not for" not in reason


def test_pick_code_falls_back_to_other_free_when_opencode_is_out():
    gemini = _ex("antigravity:gemini-4-argon-high")
    x = _ex("opencode:x-free")
    y = _ex("opencode:y-free")

    chosen, reason = pick(
        [gemini, x, y], {}, "code", "medium", _prefs(), {"opencode:x-free"},
        _Rng(0.9), cooling={"opencode:y-free"},
    )

    assert chosen is gemini
    assert reason.endswith("skipped for quota: opencode:y-free")


def test_pick_tool_with_explicit_work_types_only_gets_those():
    writer = _ex("writer:w", work_types=["docs"])
    codex = _ex("codex:c1")

    stats = _stats(("writer:w", "docs", 80.0, 5.0),
                   ("writer:w", "code", 99.0, 5.0))

    chosen, _ = pick([writer, codex], stats, "docs", "medium", _prefs(), set(),
                     _Rng(0.9))
    assert chosen is writer

    chosen, reason = pick([writer, codex], stats, "code", "medium", _prefs(),
                          set(), _Rng(0.9))
    assert chosen is codex
    assert reason.endswith("; not for code: writer")


def test_pick_exec_work_never_escalates_to_paid_on_its_own():
    free = _ex("opencode:x-free")
    paid = _ex("codex:c1", cost="paid")

    chosen, reason = pick(
        [free, paid], {}, "code", "medium", _prefs(), {"opencode:x-free"},
        _Rng(0.0),
    )

    assert chosen is None
    assert reason.startswith(
        "no executor available: no free executor left for code"
    )
    assert "split the piece" in reason and "do it yourself" in reason


def test_pick_exec_work_goes_paid_only_when_strong_is_asked():
    free = _ex("opencode:x-free")
    paid = _ex("codex:c1", cost="paid")

    chosen, reason = pick(
        [free, paid], {}, "tests", "medium", _prefs(), {"opencode:x-free"},
        _Rng(0.0), strong=True,
    )

    assert chosen is paid
    assert reason.startswith("escalated")


def test_pick_not_for_note_groups_by_tool():
    pool = [_ex(f"opencode:m{index}-free", work_types=["code"])
            for index in range(12)]
    pool.append(_ex("antigravity:gemini-4-argon-high"))

    _, reason = pick(pool, {}, "design", "medium", _prefs(), set(), _Rng(0.9))

    assert reason.endswith("; not for design: opencode")
    assert "m3-free" not in reason


# --- subscription families and minimum effort ------------------------------


def test_included_executors_share_subscription_family():
    a = _ex("codex:a", cost="included")
    b = _ex("codex:b-high", cost="included")

    assert quota_family(a.id, a.cost) == "sub:codex"
    assert quota_family(b.id, b.cost) == "sub:codex"
    # Without the cost the behavior is the old per-model one.
    assert quota_family(a.id) == "codex:a"
    assert quota_family("codex:b-high") == "codex:b"
    assert quota_family("claude:opus", "paid") == "claude:opus"
    assert quota_family("sub:claude") == "sub:claude"


def test_included_quota_pauses_every_model_of_the_cli():
    opus = _ex("claude:opus", cost="included")
    sonnet = _ex("claude:sonnet", cost="included")
    flash = _ex("opencode:flash")

    chosen, reason = pick(
        [opus, sonnet, flash], {}, "code", "medium", _prefs(), set(),
        _Rng(0.9), cooling={"claude:opus"},
    )

    assert chosen is flash
    assert reason.endswith("skipped for quota: claude:opus, claude:sonnet")


def test_quota_cooldown_adds_the_subscription_family_for_included(tmp_path):
    attempt = _quota_attempt("claude:opus", 10)
    attempt["cost"] = "included"
    project = _project(tmp_path, [{"type": "code", "attempts": [attempt]}])

    assert quota_cooldown(project, 60, now=NOW) == {"claude:opus", "sub:claude"}


def test_rank_key_spread_counts_the_subscription_family():
    # codex:a and codex:b share one plan: use of codex:a counts against codex:b.
    b = _ex("codex:b", cost="included")
    other = _ex("opencode:zeta", cost="included")
    usage = ({"codex:a": 2}, {"codex": 2})
    costs = {"codex:a": "included", "codex:b": "included",
             "opencode:zeta": "included"}

    assert _rank_key(b, {}, "code", usage, costs)[1] == 2
    assert _rank_key(other, {}, "code", usage, costs)[1] == 0
    # Without the cost map the two codex models would not be told apart.
    assert _rank_key(b, {}, "code", usage)[1] == 0


def test_effort_rank_by_suffix():
    assert effort_rank("antigravity:gemini-3-pro-minimal") == 0
    assert effort_rank("antigravity:gemini-3-pro-low") == 1
    assert effort_rank("codex:gpt-5.5-medium") == 2
    assert effort_rank("codex:gpt-5.5-high") == 3
    assert effort_rank("codex:gpt-5.5-xhigh") == 4
    assert effort_rank("claude:opus-max") == 5
    assert effort_rank("opencode:deepseek-flash") == 1


def test_pick_prefers_lowest_effort_variant():
    low = _ex("antigravity:gemini-3-pro-low")
    medium = _ex("antigravity:gemini-3-pro-medium")
    high = _ex("antigravity:gemini-3-pro-high")
    # A better score on the high variant does not lift it above the ceiling.
    stats = _stats(("antigravity:gemini-3-pro-high", "code", 95.0, 10.0))
    job = _job_with_picks([])

    chosen, _ = pick([high, medium, low], stats, "code", "medium", _prefs(),
                     set(), _Rng(0.9), job=job, piece=1)

    assert chosen is low


def test_pick_without_a_piece_still_takes_the_lowest_effort():
    low = _ex("antigravity:gemini-3-pro-low")
    high = _ex("antigravity:gemini-3-pro-high")

    chosen, _ = pick([high, low], {}, "code", "medium", _prefs(), set(),
                     _Rng(0.9))

    assert chosen is low


def test_pick_keeps_a_model_that_only_exists_at_high_effort():
    high = _ex("antigravity:gemini-3-pro-high")

    chosen, _ = pick([high], {}, "code", "medium", _prefs(), set(), _Rng(0.9))

    assert chosen is high


def _failed_attempt(executor: str, cost: str = "free") -> dict:
    return {"executor": executor, "role": "exec", "status": "failed",
            "cost": cost}


def test_effort_ceiling_grows_with_family_failures():
    job = _job_with_picks([])
    piece = job["pieces"][0]

    assert effort_ceiling(job, 1, "antigravity:gemini") == 1
    assert effort_ceiling(None, None, "antigravity:gemini") == 1

    piece["attempts"] = [
        _failed_attempt("antigravity:gemini-3-pro-low"),
        _failed_attempt("antigravity:gemini-3-pro-medium"),
        _failed_attempt("opencode:other"),
        {"executor": "antigravity:gemini-3-pro-low", "role": "exec",
         "status": "ok", "cost": "free"},
    ]
    piece["corrections"] = [{"executor": "antigravity:gemini-3-pro-high"}]

    assert effort_ceiling(job, 1, "antigravity:gemini") == 4
    assert effort_ceiling(job, 1, "opencode:other") == 2
    assert effort_ceiling(job, 9, "antigravity:gemini") == 1


def test_effort_rises_after_failure_before_tier():
    low = _ex("antigravity:gemini-3-pro-low")
    medium = _ex("antigravity:gemini-3-pro-medium")
    high = _ex("antigravity:gemini-3-pro-high")
    included = _ex("codex:gpt-5.5-low", cost="included")
    job = _job_with_picks([])
    job["pieces"][0]["attempts"] = [
        _failed_attempt("antigravity:gemini-3-pro-low")
    ]

    # Same outcome whether or not the caller excludes the variant that failed.
    for exclude in (set(), {"antigravity:gemini-3-pro-low"}):
        chosen, _ = pick(
            [low, medium, high, included], {}, "code", "medium", _prefs(),
            exclude, _Rng(0.9), job=job, piece=1,
        )
        assert chosen is medium

    job["pieces"][0]["attempts"].append(
        _failed_attempt("antigravity:gemini-3-pro-medium")
    )
    chosen, _ = pick([low, medium, high, included], {}, "code", "medium",
                     _prefs(), set(), _Rng(0.9), job=job, piece=1)
    assert chosen is high


def test_effort_ceiling_ignores_other_families():
    gemini_low = _ex("antigravity:gemini-3-pro-low")
    gemini_med = _ex("antigravity:gemini-3-pro-medium")
    job = _job_with_picks([])
    job["pieces"][0]["attempts"] = [_failed_attempt("opencode:x-free")]

    chosen, _ = pick([gemini_med, gemini_low], {}, "code", "medium", _prefs(),
                     set(), _Rng(0.9), job=job, piece=1)

    assert chosen is gemini_low


def test_next_effort_for_flag_tools():
    codex = {"name": "codex", "kind": "codex", "bin": "codex"}
    generic = {"name": "g", "kind": "generic", "bin": "g"}
    forced = {"name": "g", "kind": "generic", "bin": "g", "effort": True}
    off = {"name": "ag", "kind": "antigravity", "bin": "ag", "effort": False}

    assert [next_effort(codex, n) for n in range(5)] == [
        "low", "medium", "high", "high", "high"]
    assert next_effort(forced, 1) == "medium"
    assert next_effort(generic, 1) is None
    assert next_effort(off, 0) is None


def _ceiling_for(attempts=(), corrections=()):
    job = _job_with_picks([])
    job["pieces"][0]["attempts"] = list(attempts)
    job["pieces"][0]["corrections"] = list(corrections)
    return effort_ceiling(job, 1, "antigravity:gemini")


def test_effort_ceiling_ignores_quota_and_refused_brief_attempts():
    low = "antigravity:gemini-3-pro-low"
    for status in ("quota", "brief_refused"):
        attempt = _failed_attempt(low)
        attempt["status"] = status
        assert _ceiling_for([attempt]) == 1


def test_effort_ceiling_rises_after_a_failed_attempt_or_a_correction():
    low = "antigravity:gemini-3-pro-low"

    assert _ceiling_for([_failed_attempt(low)]) == 2
    assert _ceiling_for(corrections=[{"executor": low}]) == 2


def test_pick_keeps_the_low_variant_after_a_quota_attempt():
    low = _ex("antigravity:gemini-3-pro-low")
    medium = _ex("antigravity:gemini-3-pro-medium")
    job = _job_with_picks([])
    attempt = _failed_attempt("antigravity:gemini-3-pro-low")
    attempt["status"] = "quota"
    job["pieces"][0]["attempts"] = [attempt]

    chosen, _ = pick([medium, low], {}, "code", "medium", _prefs(), set(),
                     _Rng(0.9), job=job, piece=1)

    assert chosen is low
