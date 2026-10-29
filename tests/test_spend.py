"""The global spend log: one line per paid attempt, shared by every project."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from crew_chief import config, spend

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "skill" / "crew-chief" / "scripts"


def _entry(**overrides) -> dict:
    entry = {
        "at": datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc).isoformat(),
        "project": "proj-a",
        "job": "job-1",
        "piece": 1,
        "executor": "opencode:openrouter/x",
        "tier": "cheap",
        "cost_usd": 0.25,
        "known": True,
    }
    entry.update(overrides)
    return entry


def test_spend_file_sits_next_to_the_global_preferences(isolated_global_prefs):
    assert spend.spend_file() == isolated_global_prefs.parent / "spend.jsonl"
    assert spend.spend_file() == config.default_global_path().parent / "spend.jsonl"
    assert spend.SPEND_FILE == spend.spend_file()


def test_record_and_spent_by_day_and_job():
    spend.record(_entry(cost_usd=0.25))
    spend.record(_entry(job="job-2", cost_usd=0.5))
    spend.record(_entry(
        at=datetime(2026, 10, 21, 0, 0, 1, tzinfo=timezone.utc).isoformat(),
        cost_usd=4.0,
    ))
    spend.record(_entry(job="job-1", cost_usd=0.125, known=False))

    assert spend.spent(date(2026, 10, 20)) == pytest.approx(0.875)
    assert spend.spent(date(2026, 10, 20), "job-1") == pytest.approx(0.375)
    assert spend.spent(date(2026, 10, 20), "job-2") == pytest.approx(0.5)
    assert spend.spent(date(2026, 10, 21)) == pytest.approx(4.0)
    assert spend.spent(date(2026, 10, 19)) == 0.0


def test_spent_without_a_file_is_zero():
    assert not spend.spend_file().exists()
    assert spend.spent(date(2026, 10, 20)) == 0.0


def test_record_fills_at_and_keeps_only_known_fields():
    spend.record({"project": "p", "job": "j", "cost_usd": 1, "secret": "oops"})

    (line,) = spend.spend_file().read_text(encoding="utf-8").splitlines()
    data = json.loads(line)
    assert set(data) == {
        "at", "project", "job", "piece", "executor", "tier", "cost_usd", "known",
    }
    assert data["cost_usd"] == 1.0
    assert datetime.fromisoformat(data["at"]).utcoffset() is not None


def test_spent_skips_damaged_lines():
    spend.record(_entry(cost_usd=1.0))
    with spend.spend_file().open("a", encoding="utf-8") as handle:
        handle.write("not json\n{\"at\": \"garbage\", \"cost_usd\": 9}\n[1]\n")
    spend.record(_entry(cost_usd=2.0))

    assert spend.spent(date(2026, 10, 20)) == pytest.approx(3.0)


_WRITER = """
import sys
from crew_chief import spend
n, count = sys.argv[1], int(sys.argv[2])
for i in range(count):
    spend.record({
        "at": "2026-10-20T12:00:00+00:00", "project": "proj-" + n,
        "job": "job-" + n, "piece": i, "executor": "x:y", "tier": "paid",
        "cost_usd": 0.5, "known": True,
    })
"""


def test_parallel_records_keep_every_line(tmp_path):
    # Review Focus 2: paid dispatches of different projects run in parallel
    # processes; none of their spend lines may be lost or torn.
    workers, per_worker = 8, 15
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SCRIPTS_DIR)
    env["AGENT_CREW_CHIEF_GLOBAL_PREFS"] = str(spend.spend_file().parent / "p.toml")
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _WRITER, str(n), str(per_worker)],
            env=env,
            stderr=subprocess.PIPE,
            text=True,
        )
        for n in range(workers)
    ]
    for proc in procs:
        _, err = proc.communicate(timeout=120)
        assert proc.returncode == 0, err

    lines = spend.spend_file().read_text(encoding="utf-8").splitlines()
    assert len(lines) == workers * per_worker
    rows = [json.loads(line) for line in lines]
    for n in range(workers):
        assert sum(1 for row in rows if row["project"] == f"proj-{n}") == per_worker
    assert spend.spent(date(2026, 10, 20)) == pytest.approx(
        workers * per_worker * 0.5
    )
    assert not spend.spend_file().with_name("spend.lock").exists()


def test_stale_spend_lock_is_broken(monkeypatch):
    lock = spend.spend_file().with_name("spend.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("999999\n", encoding="utf-8")
    old = lock.stat().st_mtime - 10_000
    os.utime(lock, (old, old))

    spend.record(_entry())

    assert not lock.exists()
    assert spend.spent(date(2026, 10, 20)) == pytest.approx(0.25)


def test_taken_spend_lock_times_out_with_a_clear_error(monkeypatch):
    lock = spend.spend_file().with_name("spend.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("1\n", encoding="utf-8")
    monkeypatch.setattr(spend, "LOCK_TIMEOUT_S", 0.2)

    with pytest.raises(spend.SpendLockTimeout, match="spend.lock"):
        spend.record(_entry())


def test_spent_ignores_non_finite_and_negative_costs():
    spend.record(_entry(cost_usd=1.0))
    with spend.spend_file().open("a", encoding="utf-8") as handle:
        for bad in ("NaN", "Infinity", "-Infinity", "-5"):
            handle.write(
                '{"at": "2026-10-20T12:00:00+00:00", "job": "job-1", '
                f'"cost_usd": {bad}}}\n'
            )
    spend.record(_entry(cost_usd=2.0))

    assert spend.spent(date(2026, 10, 20)) == pytest.approx(3.0)
    assert spend.spent(date(2026, 10, 20), "job-1") == pytest.approx(3.0)


def test_record_writes_zero_for_a_non_finite_or_negative_cost():
    for bad in (float("nan"), float("inf"), -3.0):
        spend.record(_entry(cost_usd=bad, known=True))

    rows = [
        json.loads(line)
        for line in spend.spend_file().read_text(encoding="utf-8").splitlines()
    ]
    assert [row["cost_usd"] for row in rows] == [0.0, 0.0, 0.0]
    assert [row["known"] for row in rows] == [False, False, False]


def test_ledger_totals_by_day_job_and_executor():
    day = date(2026, 10, 20)
    spend.record(_entry(cost_usd=0.25, executor="a:x"))
    spend.record(_entry(cost_usd=0.5, executor="b:y", job="job-2"))
    spend.record(_entry(
        at=datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc).isoformat(),
        cost_usd=3.0, executor="a:x",
    ))

    ledger = spend.ledger(day)

    assert ledger.day_total == pytest.approx(0.75)
    assert ledger.by_job == {"job-1": pytest.approx(3.25), "job-2": 0.5}
    assert ledger.executor_day == {"a:x": pytest.approx(0.25), "b:y": 0.5}


def test_ledger_priced_needs_a_known_positive_cost():
    day = date(2026, 10, 20)
    # OpenCode prints cost 0 for unpriced models, even when it "reports" it.
    spend.record(_entry(executor="zero:m", cost_usd=0.0, known=True))
    spend.record(_entry(executor="guess:m", cost_usd=0.4, known=False))
    spend.record(_entry(executor="real:m", cost_usd=0.01, known=True))
    spend.record(_entry(
        at=datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc).isoformat(),
        executor="old:m", cost_usd=0.2, known=True,
    ))

    ledger = spend.ledger(day)

    assert ledger.priced == {"real:m", "old:m"}


def test_ledger_without_a_file_is_empty():
    ledger = spend.ledger(date(2026, 10, 20))

    assert ledger.day_total == 0.0
    assert ledger.by_job == {} and ledger.executor_day == {}
    assert ledger.priced == set()
