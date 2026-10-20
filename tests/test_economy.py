"""Tests for the economy metrics, their alerts and the CLI around them."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from crew_chief import records
from crew_chief.cli import main
from crew_chief.config import Prefs, PrefsError, load_prefs
from crew_chief.economy import job_economy, summary_line

NOW = datetime(2026, 10, 19, 12, 0, 0, tzinfo=timezone.utc)


def _prefs(**overrides) -> Prefs:
    values = dict(
        tools=[], models=[], strong_order=[], explore_rate=0.1,
        forbid_training=False, sensitive_paths=[], issue_tracker="none",
        dispatch_timeout_s=900, review_default="",
    )
    values.update(overrides)
    return Prefs(**values)


def _job_with(attempts, accepted_executor=None, usage=(), grades=None,
              created_min_ago=30):
    """One piece with ``attempts`` = (executor, cost, status, tokens) exec runs."""
    created = NOW - timedelta(minutes=created_min_ago)
    job = {"id": "j", "title": "T", "request": "r", "distributed": True,
           "reason": "", "created": created.isoformat(), "pieces": []}
    records.add_piece(job, "One", "code", "low")
    for index, (executor, cost, status, tokens) in enumerate(attempts, start=1):
        started = created + timedelta(minutes=index)
        records.add_attempt(job, 1, {
            "n": index, "role": "exec", "status": status, "executor": executor,
            "cost": cost, "started": started.isoformat(), "duration_s": 60.0,
            "tokens_in": tokens - 100, "tokens_out": 100,
            "tokens_total": tokens, "tokens_estimated": False,
            "branch": f"b{index}",
        })
        if executor == accepted_executor and status == "ok":
            records.mark_accepted(job, 1, {"n": index, "branch": f"b{index}"})
    for executor, score in (grades or {}).items():
        records.set_grade(job, 1, {"score": score, "executor": executor})
    for role, tokens_in, tokens_out, estimated in usage:
        records.add_usage(job, role, tokens_in, tokens_out, estimated)
    return job


def _job_with_costs(attempts, usage=()):
    """One piece with ``attempts`` = dicts of cost / cost_usd / cost_known."""
    job = {"id": "j", "title": "T", "request": "r", "distributed": True,
           "reason": "", "created": (NOW - timedelta(minutes=30)).isoformat(),
           "pieces": []}
    records.add_piece(job, "One", "code", "low")
    for index, extra in enumerate(attempts, start=1):
        attempt = {
            "n": index, "role": "exec", "status": "ok", "executor": "x:y",
            "tokens_total": 100, "duration_s": 60.0,
            "started": (NOW - timedelta(minutes=10)).isoformat(),
        }
        attempt.update(extra)
        records.add_attempt(job, 1, attempt)
    for role, tokens_in, tokens_out, estimated in usage:
        records.add_usage(job, role, tokens_in, tokens_out, estimated)
    return job


def test_job_economy_shares_and_alerts():
    job = _job_with(attempts=[("opencode:a-free", "free", "ok", 9000),
                              ("opencode:b-free", "free", "failed", 1000)],
                    accepted_executor="opencode:a-free",
                    usage=[("coordinator", 4000, 1000, False)],
                    grades={"opencode:a-free": 90}, created_min_ago=30)
    e = job_economy(job, _prefs(alert_coordinator_share=0.2), now=NOW)
    assert e.free_tokens == 10000 and e.coordinator_tokens == 5000
    assert e.paid_exec_tokens == 0 and not e.coordinator_estimated
    assert e.free_piece_share == 1.0
    assert round(e.coordinator_share, 3) == 0.333
    assert round(e.free_token_share, 3) == 0.667
    assert e.attempts_per_piece == 2.0
    assert e.mean_score == 90
    # created 30 min ago; the last attempt started at +2 min and ran 60 s
    assert e.minutes == pytest.approx(3.0)
    assert e.alerts == ["coordinator spent 33% of the tokens (limit 20%)"]


def test_paid_accepted_piece_and_attempts_and_time_alerts():
    job = _job_with(
        attempts=[("opencode:a", "free", "failed", 100),
                  ("opencode:a", "free", "failed", 100),
                  ("claude:opus", "paid", "ok", 800)],
        accepted_executor="claude:opus", created_min_ago=300)
    job["pieces"][0]["attempts"][-1]["started"] = (
        NOW - timedelta(minutes=10)).isoformat()
    e = job_economy(job, _prefs(alert_attempts_per_piece=2.5), now=NOW)
    assert e.free_piece_share == 0.0
    assert e.paid_exec_tokens == 800 and e.free_tokens == 200
    assert e.attempts_per_piece == 3.0
    # started 290 min after creation, ran 60 s
    assert e.minutes == pytest.approx(291.0)
    assert e.alerts == ["3.0 attempts per piece (limit 2.5)",
                        "job took 291 min (limit 180)"]


def test_naive_created_is_local_time_and_aware_started_is_compared():
    local_now = datetime.now().astimezone()
    created = (local_now - timedelta(minutes=20)).replace(tzinfo=None)
    job = {"id": "j", "created": created.isoformat(), "pieces": []}
    records.add_piece(job, "One", "code", "low")
    records.add_attempt(job, 1, {
        "n": 1, "role": "exec", "status": "ok", "executor": "x:y",
        "cost": "free", "started": (local_now - timedelta(minutes=10))
        .astimezone(timezone.utc).isoformat(), "duration_s": 60.0,
        "tokens_total": 10})
    e = job_economy(job, _prefs(), now=local_now)
    assert e.minutes == pytest.approx(11.0, abs=0.1)


def test_empty_job_has_none_values_and_no_alerts():
    job = {"id": "j", "created": NOW.isoformat(), "pieces": []}
    records.add_piece(job, "One", "code", "low")
    e = job_economy(job, _prefs(), now=NOW)
    assert e.free_tokens == 0 and e.coordinator_tokens == 0
    assert e.free_piece_share is None and e.free_token_share is None
    assert e.coordinator_share is None and e.attempts_per_piece is None
    assert e.mean_score is None and e.minutes is None
    assert e.alerts == []


def test_summary_line_with_all_values():
    job = _job_with(attempts=[("opencode:a", "free", "ok", 9000)],
                    accepted_executor="opencode:a",
                    usage=[("central", 500, 500, True)],
                    grades={"opencode:a": 90})
    line = summary_line(job_economy(job, _prefs(), now=NOW))
    assert line == ("free work: 100% of pieces, 90% of tokens · "
                    "coordinator: 10% (estimated) · 1.0 attempts/piece · "
                    "mean score 90")


def test_summary_line_with_missing_values_uses_dash():
    job = {"id": "j", "created": NOW.isoformat(), "pieces": []}
    line = summary_line(job_economy(job, _prefs(), now=NOW))
    assert line == ("free work: — of pieces, — of tokens · coordinator: — · "
                    "— attempts/piece · mean score —")


def test_zero_disables_alert():
    job = _job_with(
        attempts=[("opencode:a", "free", "failed", 100)] * 5
        + [("opencode:a", "free", "ok", 100)],
        accepted_executor="opencode:a",
        usage=[("coordinator", 9000, 0, False)], created_min_ago=600)
    # the last attempt started at +6 min; push it to 5 h after creation
    job["pieces"][0]["attempts"][-1]["started"] = (
        NOW - timedelta(minutes=10)).isoformat()
    prefs = _prefs(alert_coordinator_share=0, alert_attempts_per_piece=0,
                   alert_job_minutes=0)
    assert job_economy(job, prefs, now=NOW).alerts == []
    assert len(job_economy(job, _prefs(), now=NOW).alerts) == 3


def test_add_usage_validates_role_and_tokens():
    job = {"id": "j", "pieces": []}
    records.add_usage(job, "coordinator", 10, 5, True, note="plan")
    entry = job["usage"][0]
    assert entry["role"] == "coordinator" and entry["tokens_in"] == 10
    assert entry["tokens_out"] == 5 and entry["tokens_total"] == 15
    assert entry["estimated"] is True and entry["note"] == "plan"
    for bad in [("worker", 1, 1), ("central", -1, 1), ("central", 1.5, 1),
                ("central", True, 1), ("central", 1, "2")]:
        with pytest.raises(ValueError):
            records.add_usage(job, bad[0], bad[1], bad[2], False)
    assert len(job["usage"]) == 1


def test_markdown_economy_section_only_with_data():
    empty = {"id": "j", "title": "T", "request": "r", "pieces": []}
    records.add_piece(empty, "One", "code", "low")
    assert "## Economy" not in records.render_markdown(empty)
    job = _job_with(attempts=[("opencode:a", "free", "ok", 9000)],
                    accepted_executor="opencode:a",
                    usage=[("coordinator", 500, 500, False)])
    text = records.render_markdown(job)
    assert "## Economy" in text
    assert text.index("## Economy") < text.index("## Commit message")
    assert "free work: 100% of pieces" in text
    assert "alert:" not in text


def test_alert_prefs_defaults_and_merge(tmp_path):
    prefs = load_prefs(tmp_path, tmp_path / "none.toml")
    assert prefs.alert_coordinator_share == 0.5
    assert prefs.alert_attempts_per_piece == 2.5
    assert prefs.alert_job_minutes == 180
    (tmp_path / "g.toml").write_text(
        "alert_job_minutes = 60\nalert_coordinator_share = 0.3\n",
        encoding="utf-8")
    (tmp_path / "docs/crew-chief").mkdir(parents=True)
    (tmp_path / "docs/crew-chief/preferences.toml").write_text(
        "alert_coordinator_share = 0\n", encoding="utf-8")
    prefs = load_prefs(tmp_path, tmp_path / "g.toml")
    assert prefs.alert_job_minutes == 60
    assert prefs.alert_coordinator_share == 0


@pytest.mark.parametrize("key", ["alert_coordinator_share",
                                 "alert_attempts_per_piece",
                                 "alert_job_minutes"])
@pytest.mark.parametrize("value", ["-1", '"high"', "true"])
def test_alert_prefs_reject_negative(tmp_path, key, value):
    (tmp_path / "docs/crew-chief").mkdir(parents=True)
    (tmp_path / "docs/crew-chief/preferences.toml").write_text(
        f"{key} = {value}\n", encoding="utf-8")
    with pytest.raises(PrefsError, match=key):
        load_prefs(tmp_path, tmp_path / "none.toml")


def _saved_job(repo, **kwargs):
    job = _job_with(**kwargs)
    job["id"] = "20261019-1200-eco"
    # CLI commands compare against the real clock: stamp times relative to it
    created = datetime.now().astimezone() - timedelta(minutes=30)
    job["created"] = created.replace(tzinfo=None).isoformat()
    for piece in job["pieces"]:
        for attempt in piece["attempts"]:
            attempt["started"] = (created + timedelta(minutes=1)).isoformat()
    records.save_job(repo, job)
    return job


def test_usage_add_and_economy_cli(git_repo, capsys):
    repo = git_repo
    job = _saved_job(repo, attempts=[("opencode:a", "free", "ok", 1000)],
                     accepted_executor="opencode:a")
    base = ["--project", str(repo), "--job", job["id"]]
    assert main(["usage", "add", *base, "--role", "coordinator",
                 "--tokens-in", "3000", "--tokens-out", "1000",
                 "--estimated", "--note", "planning"]) == 0
    capsys.readouterr()
    saved = records.load_job(repo, job["id"])
    assert saved["usage"][0]["tokens_total"] == 4000
    assert saved["usage"][0]["estimated"] is True

    assert main(["economy", *base]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("free work: 100% of pieces")
    assert "(estimated)" in out[0]
    assert out[1] == "alert: coordinator spent 80% of the tokens (limit 50%)"
    assert len(out) == 2

    assert main(["economy", *base, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["coordinator_tokens"] == 4000
    assert payload["coordinator_estimated"] is True
    assert payload["alerts"] == [
        "coordinator spent 80% of the tokens (limit 50%)"]


def test_usage_add_rejects_bad_input(git_repo, capsys):
    repo = git_repo
    job = _saved_job(repo, attempts=[("opencode:a", "free", "ok", 1000)])
    base = ["--project", str(repo), "--job", job["id"]]
    assert main(["usage", "add", *base, "--role", "coordinator",
                 "--tokens-in", "-5", "--tokens-out", "1"]) == 2
    assert capsys.readouterr().err.strip()
    assert "usage" not in records.load_job(repo, job["id"])


def test_close_prints_economy_block(git_repo, capsys):
    repo = git_repo
    job = _saved_job(repo, attempts=[("opencode:a", "free", "ok", 1000)],
                     accepted_executor="opencode:a",
                     usage=[("coordinator", 4000, 0, False)])
    assert main(["close", "--project", str(repo), "--job", job["id"],
                 "--no-changelog"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("free work: 100% of pieces")
    assert lines[1].startswith("alert: coordinator spent 80%")
    assert lines[2].startswith("feat: ")


def test_close_economy_block_follows_changelog_line(git_repo, capsys):
    repo = git_repo
    job = _saved_job(repo, attempts=[("opencode:a", "free", "ok", 1000)],
                     accepted_executor="opencode:a")
    assert main(["close", "--project", str(repo), "--job", job["id"]]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("changelog: ")
    assert lines[1].startswith("free work: ")


def test_close_json_has_economy(git_repo, capsys):
    repo = git_repo
    job = _saved_job(repo, attempts=[("opencode:a", "free", "ok", 1000)],
                     accepted_executor="opencode:a")
    assert main(["close", "--project", str(repo), "--job", job["id"],
                 "--no-changelog", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["economy"]["free_tokens"] == 1000


def test_close_without_attempts_prints_no_economy(git_repo, capsys):
    repo = git_repo
    job = records.new_job(repo, "Empty", "r", True, "x")
    records.add_piece(job, "One", "code", "low")
    records.save_job(repo, job)
    assert main(["close", "--project", str(repo), "--job", job["id"],
                 "--no-changelog"]) == 0
    assert "free work:" not in capsys.readouterr().out


def test_attempts_per_piece_counts_only_pieces_with_exec_attempts():
    job = {"id": "j", "created": NOW.isoformat(), "pieces": [
        {"n": 1, "accepted": {"attempt": 2}, "attempts": [
            {"role": "exec", "status": "ok", "cost": "free", "tokens_total": 10},
            {"role": "exec", "status": "ok", "cost": "free", "tokens_total": 10},
            {"role": "exec", "status": "quota", "cost": "free", "tokens_total": 1},
            {"role": "exec", "status": "brief_refused", "cost": "free"},
            {"role": "review", "status": "ok", "cost": "free", "tokens_total": 10},
        ]},
        {"n": 2, "attempts": [{"role": "review", "status": "ok", "cost": "free"}]},
        {"n": 3, "attempts": [{"role": "exec", "status": "quota", "cost": "free"}]},
        {"n": 4, "attempts": [{"role": "exec", "status": "brief_refused"}]},
    ]}
    assert job_economy(job, None, now=NOW).attempts_per_piece == 2.0


def test_tier_tokens_split():
    job = _job_with(attempts=[("opencode:a", "free", "ok", 1000),
                              ("opencode:b", "included", "ok", 2000),
                              ("claude:haiku", "cheap", "ok", 3000),
                              ("claude:opus", "paid", "ok", 4000)])
    e = job_economy(job, None, now=NOW)
    assert e.tier_tokens == {"free": 1000, "included": 2000,
                             "cheap": 3000, "paid": 4000}
    assert e.free_tokens == 1000 and e.paid_exec_tokens == 4000
    # included and cheap tokens are in the denominator now
    assert e.free_token_share == pytest.approx(1000 / 10000)
    assert e.coordinator_share == 0.0
    assert e.as_dict()["tier_tokens"] == e.tier_tokens

    empty = {"id": "j", "created": NOW.isoformat(), "pieces": []}
    assert job_economy(empty, None, now=NOW).tier_tokens == {
        "free": 0, "included": 0, "cheap": 0, "paid": 0}


def test_cheap_usd_sum_and_unknown_flag():
    job = _job_with_costs([
        {"executor": "claude:haiku", "cost": "cheap", "cost_usd": 1.50,
         "cost_known": True},
        {"executor": "claude:haiku", "cost": "cheap", "cost_usd": 0.25,
         "cost_known": True},
        {"executor": "claude:haiku", "cost": "cheap", "cost_usd": float("nan"),
         "cost_known": False},
        {"executor": "claude:haiku", "cost": "cheap", "cost_usd": -1.0,
         "cost_known": True},
        {"executor": "claude:opus", "cost": "paid", "cost_usd": 2.0,
         "cost_known": True},
        {"executor": "claude:opus", "cost": "paid", "cost_usd": None,
         "cost_known": True},
        {"executor": "opencode:a", "cost": "free", "cost_usd": 9.0,
         "cost_known": False},
    ])
    e = job_economy(job, None, now=NOW)
    assert e.cheap_usd == pytest.approx(1.75)
    assert e.paid_usd == pytest.approx(2.0)
    # a free attempt with an unknown cost does not set the flag
    assert e.cost_unknown is True

    known = _job_with_costs([{"cost": "cheap", "cost_usd": 0.5,
                              "cost_known": True}])
    assert job_economy(known, None, now=NOW).cost_unknown is False


def test_budget_alert_at_80_percent():
    job = _job_with(attempts=[("opencode:a", "free", "ok", 1000)])
    prefs = _prefs(cheap_budget_usd_day=10.0)
    assert job_economy(job, prefs, now=NOW, spent_today=7.9).alerts == []
    assert job_economy(job, prefs, now=NOW, spent_today=8.0).alerts == [
        "cheap spend at 80% of the daily budget (US$ 8.00 of 10.00)"]
    # 0 turns the budget off, and no spent figure means no alert
    assert job_economy(job, _prefs(cheap_budget_usd_day=0.0), now=NOW,
                       spent_today=100.0).alerts == []
    assert job_economy(job, prefs, now=NOW).alerts == []


def test_summary_line_includes_tiers_only_when_used():
    free_only = summary_line(job_economy(
        _job_with(attempts=[("opencode:a", "free", "ok", 9000)],
                  accepted_executor="opencode:a",
                  usage=[("central", 500, 500, True)],
                  grades={"opencode:a": 90}), _prefs(), now=NOW))
    assert free_only == (
        "free work: 100% of pieces, 90% of tokens · "
        "coordinator: 10% (estimated) · 1.0 attempts/piece · "
        "mean score 90")

    included = _job_with(attempts=[("opencode:a", "free", "ok", 1000),
                                   ("opencode:b", "included", "ok", 500)])
    assert summary_line(job_economy(
        included, None, now=NOW)).endswith(" · included 33%")

    cheap = _job_with(attempts=[("claude:haiku", "cheap", "ok", 500)])
    cheap["pieces"][0]["attempts"][0]["cost_usd"] = 1.75
    cheap["pieces"][0]["attempts"][0]["cost_known"] = True
    line = summary_line(job_economy(cheap, None, now=NOW))
    assert line.endswith(" · cheap US$ 1.75")
    assert "(cost unknown)" not in line

    cheap["pieces"][0]["attempts"][0]["cost_known"] = False
    assert summary_line(job_economy(cheap, None, now=NOW)).endswith(
        " · cheap US$ 1.75 (cost unknown)")
