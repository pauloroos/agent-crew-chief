"""Tests for executor discovery."""

from __future__ import annotations

from crew_chief.config import Prefs
from crew_chief.discover import Executor, discover


def _prefs(tools, models=()):
    return Prefs(
        tools=[dict(t) for t in tools],
        models=[dict(m) for m in models],
        strong_order=["codex", "claude"],
        explore_rate=0.1,
        forbid_training=False,
        sensitive_paths=[],
        issue_tracker="none",
        dispatch_timeout_s=900,
        review_default="",
    )


def test_discover_finds_available_executor(fake_bin):
    fake_bin("opencode", "print('oc/free\\noc/paid')\n")
    prefs = _prefs([{"name": "opencode", "kind": "opencode", "bin": "opencode"}])
    executors, notes = discover(prefs)
    assert [e.id for e in executors] == ["opencode:oc/free", "opencode:oc/paid"]
    assert all(e.cost == "paid" and e.trains is False for e in executors)
    assert notes == []


def test_discover_skips_missing_bin_and_missing_env(fake_bin, monkeypatch):
    monkeypatch.delenv("CREWCHIEF_TEST_MISSING", raising=False)
    fake_bin("envtool", "print('m')\n")
    prefs = _prefs([
        {"name": "ghost", "kind": "generic", "bin": "definitely-not-here"},
        {"name": "envtool", "kind": "generic", "bin": "envtool", "models": ["m1"],
         "env": ["CREWCHIEF_TEST_MISSING"]},
    ])
    executors, notes = discover(prefs)
    assert executors == []
    notes_text = "\n".join(notes)
    assert "ghost" in notes_text
    assert "envtool" in notes_text
    assert "CREWCHIEF_TEST_MISSING" in notes_text


def test_discover_excludes_forbidden(fake_bin):
    fake_bin("paidcli", "print('m')\n")
    prefs = _prefs(
        [{"name": "paidcli", "kind": "generic", "bin": "paidcli", "models": ["m1"]}],
        models=[{"match": "paidcli:*", "cost": "forbidden"}],
    )
    executors, _notes = discover(prefs)
    assert [e.id for e in executors] == []


def test_discover_applies_cost_default(fake_bin):
    fake_bin("agy", "print('m1')\n")
    prefs = _prefs([{"name": "agy", "kind": "antigravity", "bin": "agy",
                     "cost_default": "free"}])
    executors, _notes = discover(prefs)
    assert [(e.id, e.cost) for e in executors] == [("agy:m1", "free")]


def test_discover_notes_tool_without_models(fake_bin):
    fake_bin("quiet", "print('m')\n")
    prefs = _prefs([{"name": "quiet", "kind": "generic", "bin": "quiet", "models": []}])
    executors, notes = discover(prefs)
    assert executors == []
    assert any("quiet" in n and "no models listed" in n for n in notes)


def test_discover_executor_carries_tool_dict(fake_bin):
    fake_bin("opencode", "print('oc/m')\n")
    tool = {"name": "opencode", "kind": "opencode", "bin": "opencode"}
    executors, _notes = discover(_prefs([tool]))
    ex = executors[0]
    assert isinstance(ex, Executor) and ex.tool is not None
    assert ex.tool["kind"] == "opencode"
