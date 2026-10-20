"""Tests for executor discovery."""

from __future__ import annotations

from crew_chief.config import Prefs
from crew_chief.discover import Discovery, Executor, discover, discover_full


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


def test_discover_missing_env_note_names_only_vars_still_missing(fake_bin, monkeypatch):
    """The note must not name a var that the Windows registry fill satisfied."""
    monkeypatch.setenv("CREWCHIEF_PRESENT_X", "value")
    monkeypatch.delenv("CREWCHIEF_MISSING_Y", raising=False)
    fake_bin("envtool", "print('m')\n")
    prefs = _prefs([
        {"name": "envtool", "kind": "generic", "bin": "envtool", "models": ["m1"],
         "env": ["CREWCHIEF_PRESENT_X", "CREWCHIEF_MISSING_Y"]},
    ])
    executors, notes = discover(prefs)
    assert executors == []
    note = next(n for n in notes if n.startswith("envtool"))
    assert "CREWCHIEF_MISSING_Y" in note
    assert "CREWCHIEF_PRESENT_X" not in note


def test_discover_note_ignores_names_the_registry_filled(fake_bin, monkeypatch):
    """A name the Windows registry can supply must not appear in the note."""
    import os as _os
    import types

    from crew_chief import tools as tools_module

    monkeypatch.delenv("CREWCHIEF_FROM_REGISTRY", raising=False)
    monkeypatch.delenv("CREWCHIEF_MISSING_Y", raising=False)
    monkeypatch.setattr(
        tools_module,
        "_fill_from_registry",
        lambda names, env: [
            name for name in names if name != "CREWCHIEF_FROM_REGISTRY"
        ],
    )
    # Fake a Windows environment so the registry fill path is taken.
    monkeypatch.setattr(
        tools_module,
        "os",
        types.SimpleNamespace(name="nt", environ=dict(_os.environ)),
    )
    fake_bin("envtool", "print('m')\n")
    prefs = _prefs([
        {"name": "envtool", "kind": "generic", "bin": "envtool", "models": ["m1"],
         "env": ["CREWCHIEF_FROM_REGISTRY", "CREWCHIEF_MISSING_Y"]},
    ])
    executors, notes = discover(prefs)
    assert executors == []
    note = next(n for n in notes if n.startswith("envtool"))
    assert "CREWCHIEF_MISSING_Y" in note
    assert "CREWCHIEF_FROM_REGISTRY" not in note


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


def test_discover_suggests_setup_while_the_global_file_has_no_block(
    tmp_path, monkeypatch
):
    """``discover`` adds its ``acc setup`` suggestion only without a block."""
    from crew_chief import setup

    global_file = tmp_path / "preferences.toml"
    monkeypatch.setenv("AGENT_CREW_CHIEF_GLOBAL_PREFS", str(global_file))

    assert setup.setup_hint() == "run setup to configure cost tiers"

    setup.write_block(
        global_file,
        setup.render_block(setup.Detected(executors=[], accounts={}), {}),
    )
    assert setup.setup_hint() is None

    # A lone marker is not a block: the suggestion comes back.
    global_file.write_text(f"{setup.SETUP_BEGIN}\nexplore_rate = 0.2\n", "utf-8")
    assert setup.setup_hint() == "run setup to configure cost tiers"


def test_discover_full_lists_models_only_for_tools_that_listed(fake_bin):
    fake_bin("opencode", "print('oc/free\\noc/banned')\n")
    prefs = _prefs(
        [
            {"name": "opencode", "kind": "opencode", "bin": "opencode"},
            {"name": "ghost", "kind": "generic", "bin": "definitely-not-here"},
        ],
        models=[{"match": "opencode:oc/banned", "cost": "forbidden"}],
    )
    result = discover_full(prefs)
    assert isinstance(result, Discovery)
    assert [e.id for e in result.executors] == ["opencode:oc/free"]
    assert result.listed == {"opencode": ["oc/free", "oc/banned"]}
    assert any("ghost" in n for n in result.notes)
    assert discover(prefs) == (result.executors, result.notes)
