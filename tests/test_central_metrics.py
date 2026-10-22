from types import SimpleNamespace

from crew_chief.central_metrics import aggregate


def test_aggregate_smoke():
    jobs = [
        {
            "id": "job-1",
            "worktree": "/tmp/repo",
            "created": "2026-01-01T00:00:00+00:00",
            "pieces": [
                {
                    "n": 1,
                    "accepted": True,
                    "attempts": [
                        {
                            "role": "exec",
                            "status": "ok",
                            "cost": "free",
                            "tokens_total": 10,
                            "tokens_in": 4,
                            "tokens_out": 6,
                            "tokens_estimated": False,
                            "started": "2026-01-01T00:00:00+00:00",
                            "duration_s": 5,
                        }
                    ],
                }
            ],
            "usage": [
                {
                    "role": "coordinator",
                    "tokens_total": 3,
                    "tokens_in": 1,
                    "tokens_out": 2,
                    "estimated": False,
                }
            ],
        }
    ]
    prefs = SimpleNamespace(
        alert_coordinator_share=0.5,
        alert_attempts_per_piece=2.0,
        alert_job_minutes=10.0,
        cheap_budget_usd_day=0.0,
    )
    now = "2026-01-01T00:00:10+00:00"
    result = aggregate(jobs, [], prefs, now)
    assert result["jobs"] == 1
    assert result["free_tokens"] == 10
    assert result["total_tokens"] == 13
    assert result["attempts_per_piece"] == 1.0
    assert result["wall_seconds"] == 5.0
    assert result["coverage_label"] == "measured"