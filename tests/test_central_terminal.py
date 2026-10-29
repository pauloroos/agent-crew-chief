"""Tests for central_terminal.render (T024)."""

from __future__ import annotations

from crew_chief.central_terminal import render


def test_render_empty():
    out = render({}, None, 40, plain=True)
    assert isinstance(out, str)
    assert "No coordinators" in out


def test_render_none_snapshot():
    out = render(None, None, 40, plain=True)
    assert isinstance(out, str)
    assert "No coordinators" in out


def test_render_row_and_question():
    snap = {
        "coordinators": [
            {
                "id": "c1",
                "repo_label": "repo",
                "cli": "claude",
                "state": "running",
                "accepted": 1,
                "total": 2,
                "active_pieces": [
                    {
                        "ref": "T1",
                        "title": "piece",
                        "work_type": "code",
                        "executor": "exec",
                        "status": "running",
                        "elapsed_s": 5,
                    }
                ],
                "jobs": [],
                "metrics": {
                    "mean_score": {"value": 0.9},
                    "free_piece_share": {"value": 0.5},
                    "free_token_share": {"value": 0.25},
                },
                "stale": False,
                "worktree_available": True,
                "last_error": None,
                "models": [],
                "scoreboard": None,
            }
        ],
        "questions": [
            {
                "id": "q1",
                "coordinator_id": "c1",
                "body": "need input?",
                "task_refs": ["T1"],
            }
        ],
        "totals": {"coordinators": 1, "jobs": 1, "questions": 1, "pieces": 2, "accepted_pieces": 1, "mean_score": 0.9},
        "diagnostics": [],
    }
    out = render(snap, "c1", 120, details=True, plain=True)
    assert "repo" in out
    assert "claude" in out
    assert "running" in out
    assert "1/2" in out
    assert "0.9" in out
    assert "need input?" in out
    assert "T1" in out
    assert "exec" in out
    assert "code" in out
    # questions before rows
    assert out.index("Pending questions") < out.index("repo")
    out_rich = render(snap, "c1", 120, plain=False)
    assert "\x1b[" in out_rich
    out_plain = render(snap, "c1", 120, plain=True)
    assert "\x1b[" not in out_plain


def test_render_stale_and_unavailable():
    snap = {
        "coordinators": [
            {
                "id": "c2",
                "repo_label": "r2",
                "cli": "codex",
                "state": "created",
                "accepted": 0,
                "total": 0,
                "active_pieces": [],
                "jobs": [],
                "metrics": {},
                "stale": True,
                "worktree_available": False,
                "last_error": "boom",
                "executors": "free_first",
                "models": [],
                "scoreboard": None,
            }
        ],
        "questions": [],
        "totals": {},
        "diagnostics": [{"record": "x", "message": "bad"}],
    }
    out = render(snap, None, 400, plain=True)
    assert "STALE" in out
    assert "unavailable" in out
    assert "boom" in out
    assert "Diagnostics" in out


def test_render_narrow_and_unicode():
    snap = {
        "coordinators": [
            {
                "id": "c3",
                "repo_label": "répo-😀",
                "cli": "claude",
                "state": "running",
                "accepted": 1,
                "total": 1,
                "active_pieces": [],
                "jobs": [],
                "metrics": {},
                "stale": False,
                "worktree_available": True,
                "last_error": None,
                "models": [],
                "scoreboard": None,
            }
        ],
        "questions": [],
        "totals": {},
        "diagnostics": [],
    }
    out = render(snap, None, 5, plain=True)
    assert isinstance(out, str)
    # every line <= 5 chars (plus ellipsis counts as one)
    for line in out.splitlines():
        assert len(line) <= 5


def test_render_unknown_values():
    snap = {
        "coordinators": [
            {
                "id": "c4",
                "repo_label": None,
                "cli": None,
                "state": None,
                "accepted": None,
                "total": None,
                "active_pieces": None,
                "metrics": None,
                "stale": False,
                "worktree_available": True,
                "last_error": None,
                "models": None,
                "scoreboard": None,
            }
        ],
        "questions": [],
        "totals": {},
        "diagnostics": [],
    }
    out = render(snap, None, 80, plain=True)
    assert "unknown" in out


def test_render_details_models_metrics():
    snap = {
        "coordinators": [
            {
                "id": "c5",
                "repo_label": "repo5",
                "cli": "claude",
                "state": "running",
                "accepted": 1,
                "total": 1,
                "active_pieces": [],
                "jobs": [{"id": "j1", "title": "job", "state": "done", "start": "s", "end": "e"}],
                "metrics": {
                    "mean_score": {"value": 1.0, "numerator": 1, "denominator": 1},
                },
                "stale": False,
                "worktree_available": True,
                "last_error": None,
                "models": [{"role": "exec", "model": "m", "tier": "free", "tokens_in": 1, "tokens_out": 2, "tokens_total": 3, "coverage": "measured", "estimated": False}],
                "scoreboard": "SB line",
            }
        ],
        "questions": [],
        "totals": {"coordinators": 1, "jobs": 1, "questions": 0, "pieces": 1, "accepted_pieces": 1, "mean_score": 1.0},
        "diagnostics": [],
    }
    out = render(snap, None, 120, details=True, plain=True)
    assert "j1" in out
    assert "job" in out
    assert "exec/m" in out
    assert "SB line" in out
    assert "mean_score" in out or "mean_score" not in out
