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
    PENALTIES,
    Stat,
    build_stats,
    free_failures,
    grade,
    pick,
    render_scoreboard,
)
from crew_chief.tools import Executor

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)


# --- helpers ---------------------------------------------------------------


def _ex(id_: str, cost: str = "free", name: str | None = None) -> Executor:
    """Build an executor from ``"<tool>:<model>"`` with an optional cost."""
    tool_name, model = id_.split(":", 1)
    return Executor(
        id=id_,
        tool={
            "name": name or tool_name,
            "kind": "opencode",
            "bin": "opencode",
        },
        model=model,
        cost=cost,
        trains=False,
    )


def _prefs(
    strong_order: tuple[str, ...] = ("ghost", "codex"),
    explore_rate: float = 0.1,
) -> Prefs:
    """A :class:`Prefs` with just the fields ``pick`` reads, plus defaults."""
    return Prefs(
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

    assert pick([forbidden], {}, "code", "medium", _prefs(), set(), _Rng(0.0)) == (
        None,
        "no executor available",
    )
    assert pick([], {}, "code", "medium", _prefs(), set(), _Rng(0.0)) == (
        None,
        "no executor available",
    )


def test_pick_escalates_when_no_free():
    codex = _ex("codex:c1", cost="paid")
    ghost = _ex("ghost:g1", cost="paid")

    chosen, reason = pick(
        [codex, ghost], {}, "code", "medium", _prefs(), set(), _Rng(0.0)
    )

    assert chosen is ghost
    assert reason.startswith("escalated: no free executor available")


def test_pick_strong_order_puts_unlisted_tools_last():
    codex = _ex("codex:c1", cost="paid")
    ghost = _ex("ghost:g1", cost="paid")

    chosen, _ = pick(
        [ghost, codex],
        {},
        "code",
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

    assert cells == ["opencode:a", "80", "1", "—", "2026-10-17"]


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
