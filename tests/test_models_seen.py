"""Tests for the per-project registry of models seen by discovery."""

from __future__ import annotations

import json

import pytest

from crew_chief.discover import Discovery
from crew_chief.models_seen import (
    changes_payload,
    check_models,
    diff_models,
    load_seen,
    models_seen_path,
    render_changes,
    save_seen,
)
from crew_chief.records import RecordError
from crew_chief.tools import Executor

T0 = "2026-10-18T12:00:00+00:00"
T1 = "2026-10-19T12:00:00+00:00"


def _ex(executor_id: str, cost: str = "free", trains: bool = False) -> Executor:
    tool_name, _, model = executor_id.partition(":")
    return Executor(
        id=executor_id,
        tool={"name": tool_name, "kind": "generic", "bin": tool_name},
        model=model,
        cost=cost,
        trains=trains,
    )


def _listed(*executors: Executor) -> dict[str, list[str]]:
    listed: dict[str, list[str]] = {}
    for executor in executors:
        listed.setdefault(executor.tool["name"], []).append(executor.model)
    return listed


def _entry(cost="free", trains=False, present=True, first=T0, last=T0) -> dict:
    return {
        "cost": cost,
        "trains": trains,
        "first_seen": first,
        "last_seen": last,
        "present": present,
    }


# --- diff_models -----------------------------------------------------------


def test_first_run_is_a_baseline_not_a_flood_of_new_models():
    a, b = _ex("oc:a-free"), _ex("oc:b", cost="paid")

    snapshot, changes = diff_models(None, [a, b], _listed(a, b), T0)

    assert changes.added == [] and changes.removed == [] and changes.changed == []
    assert changes.baseline == 2
    assert changes.since is None
    assert snapshot["last_run"] == T0
    assert snapshot["models"]["oc:a-free"] == _entry()
    assert snapshot["models"]["oc:b"] == _entry(cost="paid")
    assert render_changes(changes) == ["models: baseline: 2 executors recorded"]


def test_new_model_of_a_known_tool_is_added():
    a, b = _ex("oc:a-free"), _ex("oc:new-free", trains=True)
    previous = {"last_run": T0, "models": {"oc:a-free": _entry()}}

    snapshot, changes = diff_models(previous, [a, b], _listed(a, b), T1)

    assert changes.added == [{"id": "oc:new-free", "cost": "free", "trains": True}]
    assert changes.removed == [] and changes.changed == []
    assert changes.baseline is None
    assert changes.since == T0
    assert snapshot["models"]["oc:new-free"] == _entry(trains=True, first=T1, last=T1)
    assert snapshot["models"]["oc:a-free"]["last_seen"] == T1
    assert snapshot["models"]["oc:a-free"]["first_seen"] == T0


def test_model_missing_from_a_listed_tool_is_removed():
    a = _ex("oc:a-free")
    previous = {"last_run": T0, "models": {"oc:a-free": _entry(), "oc:gone-free": _entry()}}

    snapshot, changes = diff_models(previous, [a], _listed(a), T1)

    assert changes.removed == [{"id": "oc:gone-free", "cost": "free", "trains": False}]
    assert changes.added == [] and changes.changed == []
    gone = snapshot["models"]["oc:gone-free"]
    assert gone["present"] is False
    assert gone["last_seen"] == T0


def test_removed_model_is_not_reported_twice():
    a = _ex("oc:a-free")
    previous = {
        "last_run": T0,
        "models": {"oc:a-free": _entry(), "oc:gone": _entry(present=False)},
    }

    _snapshot_after, changes = diff_models(previous, [a], _listed(a), T1)

    assert changes.removed == []


def test_model_that_comes_back_is_added_again():
    a, back = _ex("oc:a-free"), _ex("oc:back-free")
    previous = {
        "last_run": T0,
        "models": {"oc:a-free": _entry(), "oc:back-free": _entry(present=False)},
    }

    snapshot, changes = diff_models(previous, [a, back], _listed(a, back), T1)

    assert changes.added == [{"id": "oc:back-free", "cost": "free", "trains": False}]
    entry = snapshot["models"]["oc:back-free"]
    assert entry["present"] is True
    assert entry["first_seen"] == T0
    assert entry["last_seen"] == T1


def test_cost_change_is_reported_as_changed():
    a = _ex("oc:a", cost="paid")
    previous = {"last_run": T0, "models": {"oc:a": _entry(cost="free")}}

    snapshot, changes = diff_models(previous, [a], _listed(a), T1)

    assert changes.changed == [
        {"id": "oc:a", "cost": "paid", "trains": False,
         "was_cost": "free", "was_trains": False}
    ]
    assert snapshot["models"]["oc:a"]["cost"] == "paid"
    assert render_changes(changes) == [
        f"models: changes since {T0}",
        "now paid: oc:a (was free, trains=no)",
    ]


def test_trains_change_is_reported_as_changed():
    a = _ex("oc:a", trains=True)
    previous = {"last_run": T0, "models": {"oc:a": _entry(trains=False)}}

    _snapshot_after, changes = diff_models(previous, [a], _listed(a), T1)

    assert [c["id"] for c in changes.changed] == ["oc:a"]
    assert "trains changed: oc:a (now trains=yes, cost=free)" in render_changes(changes)


def test_skipped_tool_does_not_mark_its_models_removed():
    a = _ex("oc:a-free")
    previous = {
        "last_run": T0,
        "models": {"oc:a-free": _entry(), "agy:m": _entry(cost="paid")},
    }

    # agy was skipped (bin missing, env missing or no models): not in ``listed``.
    snapshot, changes = diff_models(previous, [a], _listed(a), T1)

    assert changes.removed == []
    assert snapshot["models"]["agy:m"] == _entry(cost="paid")


def test_forbidden_model_of_a_listed_tool_is_not_removed():
    a = _ex("oc:a-free")
    previous = {"last_run": T0, "models": {"oc:a-free": _entry(), "oc:banned": _entry()}}
    listed = {"oc": ["a-free", "banned"]}  # listed, but filtered out as forbidden

    snapshot, changes = diff_models(previous, [a], listed, T1)

    assert changes.removed == []
    assert snapshot["models"]["oc:banned"]["present"] is True


def test_first_models_of_a_new_tool_are_a_baseline():
    a, n = _ex("oc:a-free"), _ex("newtool:m", cost="paid")
    previous = {"last_run": T0, "models": {"oc:a-free": _entry()}}

    snapshot, changes = diff_models(previous, [a, n], _listed(a, n), T1)

    assert changes.added == []
    assert changes.baseline == 1
    assert changes.baseline_tools == ["newtool"]
    assert "newtool:m" in snapshot["models"]
    assert render_changes(changes) == [
        "models: baseline: 1 executors recorded (new tool: newtool)"
    ]


def test_no_changes_line_names_the_last_run():
    a = _ex("oc:a-free")
    previous = {"last_run": T0, "models": {"oc:a-free": _entry()}}

    _snapshot_after, changes = diff_models(previous, [a], _listed(a), T1)

    assert render_changes(changes) == [f"models: no changes since {T0}"]


def test_render_puts_free_models_first():
    keep = _ex("oc:keep")
    new_paid, new_free = _ex("oc:np", cost="paid"), _ex("oc:nf")
    now_free = _ex("oc:cheap")
    previous = {
        "last_run": T0,
        "models": {
            "oc:keep": _entry(),
            "oc:cheap": _entry(cost="paid"),
            "oc:old": _entry(),
        },
    }
    executors = [keep, new_paid, new_free, now_free]

    _snapshot_after, changes = diff_models(previous, executors, _listed(*executors), T1)

    assert render_changes(changes) == [
        f"models: changes since {T0}",
        "new free: oc:nf (trains=no)",
        "new paid: oc:np (trains=no)",
        "now free: oc:cheap (was paid, trains=no)",
        "gone: oc:old (was free)",
    ]


def test_changes_payload_shape():
    a, b = _ex("oc:a-free"), _ex("oc:b-free")
    previous = {"last_run": T0, "models": {"oc:a-free": _entry()}}

    _snapshot_after, changes = diff_models(previous, [a, b], _listed(a, b), T1)
    payload = changes_payload(changes)

    assert payload == {
        "since": T0,
        "baseline": None,
        "baseline_tools": [],
        "added": [{"id": "oc:b-free", "cost": "free", "trains": False}],
        "removed": [],
        "changed": [],
        "error": None,
    }


# --- file -------------------------------------------------------------------


def test_save_is_deterministic_sorted_lf(tmp_path):
    snapshot = {"models": {"z:m": _entry(), "a:m": _entry(cost="paid")}, "last_run": T0}

    save_seen(tmp_path, snapshot)
    path = models_seen_path(tmp_path)
    raw = path.read_bytes()

    assert path == tmp_path / "docs" / "crew-chief" / "models-seen.json"
    assert b"\r\n" not in raw and raw.endswith(b"\n")
    assert raw.decode("utf-8") == json.dumps(snapshot, indent=2, sort_keys=True) + "\n"
    assert raw.decode("utf-8").index('"a:m"') < raw.decode("utf-8").index('"z:m"')
    assert load_seen(tmp_path) == snapshot
    assert not list(path.parent.glob("*.tmp"))


def test_load_missing_file_is_none(tmp_path):
    assert load_seen(tmp_path) is None


def test_load_corrupt_file_names_it(tmp_path):
    path = models_seen_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{oops", encoding="utf-8")

    with pytest.raises(RecordError, match="models-seen.json"):
        load_seen(tmp_path)


def test_check_models_writes_and_reports(tmp_path):
    a = _ex("oc:a-free")
    first = check_models(tmp_path, Discovery([a], [], _listed(a)), now=T0)
    assert first.baseline == 1

    b = _ex("oc:b-free")
    second = check_models(tmp_path, Discovery([a, b], [], _listed(a, b)), now=T1)
    assert [item["id"] for item in second.added] == ["oc:b-free"]
    assert load_seen(tmp_path)["last_run"] == T1


def test_check_models_keeps_an_unreadable_file(tmp_path):
    path = models_seen_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{oops", encoding="utf-8")
    a = _ex("oc:a-free")

    changes = check_models(tmp_path, Discovery([a], [], _listed(a)), now=T1)

    assert changes.error and "models-seen.json" in changes.error
    assert path.read_text(encoding="utf-8") == "{oops"
    assert render_changes(changes)[0].startswith("models: not updated: ")
