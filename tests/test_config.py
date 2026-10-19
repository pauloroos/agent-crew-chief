"""Tests for preferences loading, merging, validation and classification."""

from __future__ import annotations

from pathlib import Path

import pytest

from crew_chief.config import (
    Prefs,
    PrefsError,
    classify,
    default_global_path,
    load_prefs,
    tool_work_types,
)
from crew_chief.records import WORK_TYPES


def test_defaults_when_no_files(tmp_path):
    p = load_prefs(tmp_path, global_path=tmp_path / "none.toml")
    assert p.explore_rate == 0.1 and p.dispatch_timeout_s == 900
    assert {t["name"] for t in p.tools} == {
        "opencode", "antigravity", "codex", "claude", "cursor",
    }


def test_generic_tool_without_run_cmd_is_rejected(tmp_path):
    (tmp_path / "docs/crew-chief").mkdir(parents=True)
    (tmp_path / "docs/crew-chief/preferences.toml").write_text(
        '[[tools]]\nname="mycli"\nkind="generic"\nbin="mycli"\n', encoding="utf-8")
    with pytest.raises(PrefsError) as excinfo:
        load_prefs(tmp_path, tmp_path / "n.toml")
    message = str(excinfo.value)
    assert "preferences.toml" in message and "run_cmd" in message


def test_project_overrides_global_by_name(tmp_path):
    g = tmp_path / "g.toml"; g.write_text('[[tools]]\nname="mycli"\nkind="claude-code"\nbin="a"\n', encoding="utf-8")
    proj = tmp_path / "proj"; (proj / "docs/crew-chief").mkdir(parents=True)
    (proj / "docs/crew-chief/preferences.toml").write_text('[[tools]]\nname="mycli"\nkind="claude-code"\nbin="b"\n', encoding="utf-8")
    p = load_prefs(proj, global_path=g)
    assert next(t for t in p.tools if t["name"] == "mycli")["bin"] == "b"


def test_unmatched_model_is_paid(tmp_path):
    assert classify("opencode:x/y", load_prefs(tmp_path, tmp_path / "n.toml")) == ("paid", False)


def test_forbid_training_turns_trains_into_forbidden(tmp_path):
    proj = tmp_path; (proj / "docs/crew-chief").mkdir(parents=True)
    (proj / "docs/crew-chief/preferences.toml").write_text(
        'forbid_training=true\n[[models]]\nmatch="opencode:*-preview-free"\ncost="free"\ntrains=true\n', encoding="utf-8")
    assert classify("opencode:go/longcat-2.5-preview-free", load_prefs(proj, proj / "n.toml"))[0] == "forbidden"


def test_invalid_toml_names_file(tmp_path):
    (tmp_path / "docs/crew-chief").mkdir(parents=True)
    f = tmp_path / "docs/crew-chief/preferences.toml"; f.write_text("explore_rate = = 1", encoding="utf-8")
    with pytest.raises(PrefsError, match="preferences.toml"):
        load_prefs(tmp_path, tmp_path / "n.toml")


def test_bad_cost_value_rejected(tmp_path):
    (tmp_path / "docs/crew-chief").mkdir(parents=True)
    (tmp_path / "docs/crew-chief/preferences.toml").write_text('[[models]]\nmatch="*"\ncost="cheap"\n', encoding="utf-8")
    with pytest.raises(PrefsError, match="cost"):
        load_prefs(tmp_path, tmp_path / "n.toml")


def test_tool_default_used_when_no_match(tmp_path):
    p = load_prefs(tmp_path, tmp_path / "n.toml")
    assert classify("antigravity:m", p, tool_default="free") == ("free", False)


def test_default_global_path_follows_the_env_var(tmp_path, monkeypatch):
    custom = tmp_path / "custom-global.toml"
    monkeypatch.setenv("AGENT_CREW_CHIEF_GLOBAL_PREFS", str(custom))
    assert default_global_path() == custom


def test_default_global_path_is_the_home_config_file(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_CREW_CHIEF_GLOBAL_PREFS", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert (
        default_global_path()
        == tmp_path / ".config" / "agent-crew-chief" / "preferences.toml"
    )


def test_load_prefs_reads_the_global_file_named_by_the_env_var(tmp_path, monkeypatch):
    global_file = tmp_path / "global.toml"
    global_file.write_text(
        '[[tools]]\nname="mycli"\nkind="claude-code"\nbin="mycli"\n', encoding="utf-8"
    )
    monkeypatch.setenv("AGENT_CREW_CHIEF_GLOBAL_PREFS", str(global_file))

    p = load_prefs(tmp_path)

    assert next(t for t in p.tools if t["name"] == "mycli")["bin"] == "mycli"


def test_load_prefs_tolerates_a_missing_global_file(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "AGENT_CREW_CHIEF_GLOBAL_PREFS", str(tmp_path / "does-not-exist.toml")
    )
    assert load_prefs(tmp_path).explore_rate == 0.1


def test_first_match_wins_when_no_rule_is_forbidden(tmp_path):
    proj = tmp_path
    (proj / "docs/crew-chief").mkdir(parents=True)
    (proj / "docs/crew-chief/preferences.toml").write_text(
        '[[models]]\nmatch="opencode:x/*"\ncost="paid"\n'
        "\n"
        '[[models]]\nmatch="opencode:x/m"\ncost="free"\n',
        encoding="utf-8",
    )
    assert classify("opencode:x/m", load_prefs(proj, proj / "n.toml")) == (
        "paid",
        False,
    )


def test_global_forbidden_rule_beats_the_project_free_rule(tmp_path):
    global_file = tmp_path / "global.toml"
    global_file.write_text(
        '[[models]]\nmatch="opencode:x/bad-free"\ncost="forbidden"\n', encoding="utf-8"
    )
    proj = tmp_path / "proj"
    (proj / "docs/crew-chief").mkdir(parents=True)
    (proj / "docs/crew-chief/preferences.toml").write_text(
        '[[models]]\nmatch="opencode:x/*-free"\ncost="free"\n', encoding="utf-8"
    )

    cost, trains = classify("opencode:x/bad-free", load_prefs(proj, global_file))

    assert (cost, trains) == ("forbidden", False)


def test_forbidden_rule_reports_its_own_trains_flag(tmp_path):
    proj = tmp_path
    (proj / "docs/crew-chief").mkdir(parents=True)
    (proj / "docs/crew-chief/preferences.toml").write_text(
        '[[models]]\nmatch="opencode:x/*"\ncost="free"\n'
        "\n"
        '[[models]]\nmatch="opencode:x/m"\ncost="forbidden"\ntrains=true\n',
        encoding="utf-8",
    )
    assert classify("opencode:x/m", load_prefs(proj, proj / "n.toml")) == (
        "forbidden",
        True,
    )


def test_prefs_is_a_dataclass_with_expected_fields(tmp_path):
    p = load_prefs(tmp_path, tmp_path / "n.toml")
    assert isinstance(p, Prefs)
    assert p.strong_order == ["codex", "claude"]
    assert p.forbid_training is False
    assert p.sensitive_paths == []
    assert p.issue_tracker == "none"
    assert p.review_default == ""


def test_model_check_hours_defaults_to_24_and_can_be_set(tmp_path):
    assert load_prefs(tmp_path, tmp_path / "n.toml").model_check_hours == 24
    (tmp_path / "docs/crew-chief").mkdir(parents=True)
    (tmp_path / "docs/crew-chief/preferences.toml").write_text(
        "model_check_hours = 6\n", encoding="utf-8")
    assert load_prefs(tmp_path, tmp_path / "n.toml").model_check_hours == 6


@pytest.mark.parametrize("value", ["-1", '"daily"', "true"])
def test_model_check_hours_must_be_a_non_negative_number(tmp_path, value):
    (tmp_path / "docs/crew-chief").mkdir(parents=True)
    (tmp_path / "docs/crew-chief/preferences.toml").write_text(
        f"model_check_hours = {value}\n", encoding="utf-8")
    with pytest.raises(PrefsError, match="model_check_hours"):
        load_prefs(tmp_path, tmp_path / "n.toml")


def test_ignore_home_entries_defaults_to_empty_and_unions_global_and_project(
    tmp_path,
):
    assert load_prefs(tmp_path, tmp_path / "n.toml").ignore_home_entries == []
    g = tmp_path / "g.toml"
    g.write_text('ignore_home_entries = [".aider*", ".kiro"]\n', encoding="utf-8")
    proj = tmp_path / "proj"
    (proj / "docs/crew-chief").mkdir(parents=True)
    (proj / "docs/crew-chief/preferences.toml").write_text(
        'ignore_home_entries = [".kiro", ".grok"]\n', encoding="utf-8")
    assert load_prefs(proj, g).ignore_home_entries == [".aider*", ".kiro", ".grok"]


@pytest.mark.parametrize("value", ['".kiro"', "[1, 2]", "true"])
def test_ignore_home_entries_must_be_a_list_of_names(tmp_path, value):
    (tmp_path / "docs/crew-chief").mkdir(parents=True)
    (tmp_path / "docs/crew-chief/preferences.toml").write_text(
        f"ignore_home_entries = {value}\n", encoding="utf-8")
    with pytest.raises(PrefsError, match="ignore_home_entries"):
        load_prefs(tmp_path, tmp_path / "n.toml")


def test_quota_cooldown_min_defaults_to_60_and_can_be_set(tmp_path):
    assert load_prefs(tmp_path, tmp_path / "n.toml").quota_cooldown_min == 60
    (tmp_path / "docs/crew-chief").mkdir(parents=True)
    (tmp_path / "docs/crew-chief/preferences.toml").write_text(
        "quota_cooldown_min = 15\n", encoding="utf-8")
    assert load_prefs(tmp_path, tmp_path / "n.toml").quota_cooldown_min == 15


@pytest.mark.parametrize("value", ["-1", '"hour"', "true"])
def test_quota_cooldown_min_must_be_a_non_negative_number(tmp_path, value):
    (tmp_path / "docs/crew-chief").mkdir(parents=True)
    (tmp_path / "docs/crew-chief/preferences.toml").write_text(
        f"quota_cooldown_min = {value}\n", encoding="utf-8")
    with pytest.raises(PrefsError, match="quota_cooldown_min"):
        load_prefs(tmp_path, tmp_path / "n.toml")


# --- work types and exec_first ---------------------------------------------


def test_tool_work_types_default_by_kind():
    assert tool_work_types({"name": "opencode", "kind": "opencode"}) == (
        "code", "tests", "refactor", "docs", "data",
    )
    for kind in ("antigravity", "claude-code", "codex", "generic"):
        assert tool_work_types({"name": "x", "kind": kind}) == WORK_TYPES


def test_tool_work_types_explicit_list_wins_and_empty_means_all():
    assert tool_work_types(
        {"name": "x", "kind": "codex", "work_types": ["docs"]}
    ) == ("docs",)
    assert tool_work_types(
        {"name": "x", "kind": "opencode", "work_types": []}
    ) == WORK_TYPES


def test_work_types_loaded_from_preferences(tmp_path):
    (tmp_path / "docs/crew-chief").mkdir(parents=True)
    (tmp_path / "docs/crew-chief/preferences.toml").write_text(
        '[[tools]]\nname="writer"\nkind="claude-code"\nbin="w"\n'
        'work_types=["docs", "research"]\n', encoding="utf-8")
    prefs = load_prefs(tmp_path, tmp_path / "n.toml")
    writer = next(t for t in prefs.tools if t["name"] == "writer")
    assert tool_work_types(writer) == ("docs", "research")


@pytest.mark.parametrize("value", ['["docs", "poetry"]', '"docs"', "[1]"])
def test_unknown_work_type_is_a_one_line_error(tmp_path, value):
    (tmp_path / "docs/crew-chief").mkdir(parents=True)
    (tmp_path / "docs/crew-chief/preferences.toml").write_text(
        f'[[tools]]\nname="writer"\nkind="claude-code"\nbin="w"\n'
        f"work_types={value}\n", encoding="utf-8")
    with pytest.raises(PrefsError) as excinfo:
        load_prefs(tmp_path, tmp_path / "n.toml")
    message = str(excinfo.value)
    assert "preferences.toml" in message and "'writer'" in message
    assert "work_types" in message and "\n" not in message


def test_exec_first_defaults_to_opencode_and_project_wins(tmp_path):
    assert load_prefs(tmp_path, tmp_path / "n.toml").exec_first == ["opencode"]
    g = tmp_path / "g.toml"
    g.write_text('exec_first = ["cursor"]\n', encoding="utf-8")
    proj = tmp_path / "proj"
    (proj / "docs/crew-chief").mkdir(parents=True)
    assert load_prefs(proj, g).exec_first == ["cursor"]
    (proj / "docs/crew-chief/preferences.toml").write_text(
        "exec_first = []\n", encoding="utf-8")
    assert load_prefs(proj, g).exec_first == []


@pytest.mark.parametrize("value", ['"opencode"', "[1]", "true"])
def test_exec_first_must_be_a_list_of_tool_names(tmp_path, value):
    (tmp_path / "docs/crew-chief").mkdir(parents=True)
    (tmp_path / "docs/crew-chief/preferences.toml").write_text(
        f"exec_first = {value}\n", encoding="utf-8")
    with pytest.raises(PrefsError, match="exec_first"):
        load_prefs(tmp_path, tmp_path / "n.toml")
