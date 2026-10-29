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
    (tmp_path / "docs/crew-chief/preferences.toml").write_text('[[models]]\nmatch="*"\ncost="bogus"\n', encoding="utf-8")
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


def test_tool_work_types_default_to_every_type_for_every_kind():
    for kind in ("opencode", "antigravity", "claude-code", "codex", "generic"):
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


# --- changelog ---------------------------------------------------------------


def test_changelog_prefs_default_and_can_be_set(tmp_path):
    prefs = load_prefs(tmp_path, tmp_path / "n.toml")
    assert prefs.changelog == "keep-a-changelog"
    assert prefs.changelog_path == "CHANGELOG.md"
    (tmp_path / "docs/crew-chief").mkdir(parents=True)
    (tmp_path / "docs/crew-chief/preferences.toml").write_text(
        'changelog = "none"\nchangelog_path = "docs/CHANGES.md"\n',
        encoding="utf-8")
    prefs = load_prefs(tmp_path, tmp_path / "n.toml")
    assert prefs.changelog == "none" and prefs.changelog_path == "docs/CHANGES.md"


def test_changelog_pref_rejects_unknown_value(tmp_path):
    (tmp_path / "docs/crew-chief").mkdir(parents=True)
    (tmp_path / "docs/crew-chief/preferences.toml").write_text(
        'changelog = "git-log"\n', encoding="utf-8")
    with pytest.raises(PrefsError, match="changelog"):
        load_prefs(tmp_path, tmp_path / "n.toml")


@pytest.mark.parametrize("value", ['""', "3", "true"])
def test_changelog_path_must_be_a_non_empty_string(tmp_path, value):
    (tmp_path / "docs/crew-chief").mkdir(parents=True)
    (tmp_path / "docs/crew-chief/preferences.toml").write_text(
        f"changelog_path = {value}\n", encoding="utf-8")
    with pytest.raises(PrefsError, match="changelog_path"):
        load_prefs(tmp_path, tmp_path / "n.toml")


def test_cap_usd_day_validated_and_read(tmp_path):
    (tmp_path / "docs/crew-chief").mkdir(parents=True)
    path = tmp_path / "docs/crew-chief/preferences.toml"
    path.write_text(
        '[[models]]\nmatch = "opencode:openrouter/a"\ncost = "cheap"\ncap_usd_day = 1.5\n',
        encoding="utf-8")
    prefs = load_prefs(tmp_path, tmp_path / "n.toml")
    assert prefs.models[0]["cap_usd_day"] == 1.5
    for bad in ("-1", '"3"', "true"):
        path.write_text(
            f'[[models]]\nmatch = "x"\ncost = "cheap"\ncap_usd_day = {bad}\n',
            encoding="utf-8")
        with pytest.raises(PrefsError, match="cap_usd_day"):
            load_prefs(tmp_path, tmp_path / "n.toml")


def test_cheap_budget_prefs_default_zero_and_reject_negative(tmp_path):
    prefs = load_prefs(tmp_path, tmp_path / "n.toml")
    assert prefs.cheap_budget_usd_day == 0.0 and prefs.cheap_budget_usd_job == 0.0
    assert prefs.setup_models == [] and prefs.builtin_tiers is True
    g = tmp_path / "g.toml"
    g.write_text("cheap_budget_usd_day = 2\ncheap_budget_usd_job = 0.5\n", encoding="utf-8")
    prefs = load_prefs(tmp_path, g)
    assert prefs.cheap_budget_usd_day == 2.0 and prefs.cheap_budget_usd_job == 0.5
    for key in ("cheap_budget_usd_day", "cheap_budget_usd_job"):
        g.write_text(f"{key} = -1\n", encoding="utf-8")
        with pytest.raises(PrefsError, match=key):
            load_prefs(tmp_path, g)


SETUP_GLOBAL = (
    'explore_rate = 0.3\n'
    '[[models]]\nmatch = "opencode:mine/*"\ncost = "free"\n'
    '# >>> acc setup >>>\n'
    'cheap_budget_usd_day = 3\n'
    'explore_rate = 0.9\n'
    'forbid_training = true\n'
    '[[models]]\nmatch = "claude:*"\ncost = "included"\n'
    '# <<< acc setup <<<\n'
    '[[models]]\nmatch = "opencode:late/*"\ncost = "cheap"\n'
)


def test_setup_block_parsed_from_global_file(tmp_path):
    g = tmp_path / "g.toml"
    g.write_text(SETUP_GLOBAL, encoding="utf-8")
    prefs = load_prefs(tmp_path, g)
    assert [r["match"] for r in prefs.setup_models] == ["claude:*"]
    # Rules outside the block stay with the user's own models.
    assert [r["match"] for r in prefs.models] == ["opencode:mine/*", "opencode:late/*"]
    # Scalars: the user's text outside the block wins; the block fills the rest.
    assert prefs.explore_rate == 0.3
    assert prefs.cheap_budget_usd_day == 3.0 and prefs.forbid_training is True


def test_project_scalar_beats_setup_block_scalar(tmp_path):
    g = tmp_path / "g.toml"
    g.write_text(SETUP_GLOBAL, encoding="utf-8")
    (tmp_path / "docs/crew-chief").mkdir(parents=True)
    (tmp_path / "docs/crew-chief/preferences.toml").write_text(
        "cheap_budget_usd_day = 0\n", encoding="utf-8")
    assert load_prefs(tmp_path, g).cheap_budget_usd_day == 0.0


def test_setup_block_handles_crlf_and_reports_block_errors(tmp_path):
    g = tmp_path / "g.toml"
    g.write_bytes(SETUP_GLOBAL.replace("\n", "\r\n").encode("utf-8"))
    prefs = load_prefs(tmp_path, g)
    assert [r["match"] for r in prefs.setup_models] == ["claude:*"]
    g.write_text(
        '# >>> acc setup >>>\n[[models]]\nmatch = "x"\ncost = "nope"\n# <<< acc setup <<<\n',
        encoding="utf-8")
    with pytest.raises(PrefsError, match="invalid cost"):
        load_prefs(tmp_path, g)


def test_lone_setup_marker_is_just_a_comment(tmp_path):
    g = tmp_path / "g.toml"
    g.write_text('# >>> acc setup >>>\nexplore_rate = 0.4\n', encoding="utf-8")
    prefs = load_prefs(tmp_path, g)
    assert prefs.explore_rate == 0.4 and prefs.setup_models == []


# --- ponytail ----------------------------------------------------------------


def test_ponytail_level_defaults_to_full(tmp_path):
    proj = tmp_path
    (proj / "docs/crew-chief").mkdir(parents=True)
    (proj / "docs/crew-chief/preferences.toml").write_text(
        "explore_rate = 0.2\n", encoding="utf-8")
    assert load_prefs(proj, proj / "n.toml").ponytail_level == "full"


def test_ponytail_level_project_overrides_global(tmp_path):
    g = tmp_path / "g.toml"
    g.write_text('ponytail_level = "lite"\n', encoding="utf-8")
    proj = tmp_path / "proj"
    (proj / "docs/crew-chief").mkdir(parents=True)
    (proj / "docs/crew-chief/preferences.toml").write_text(
        'ponytail_level = "off"\n', encoding="utf-8")
    assert load_prefs(proj, g).ponytail_level == "off"


@pytest.mark.parametrize("value", ['"FULL"', "3"])
def test_ponytail_level_rejects_unknown_value(tmp_path, value):
    (tmp_path / "docs/crew-chief").mkdir(parents=True)
    (tmp_path / "docs/crew-chief/preferences.toml").write_text(
        f"ponytail_level = {value}\n", encoding="utf-8")
    with pytest.raises(PrefsError) as excinfo:
        load_prefs(tmp_path, tmp_path / "n.toml")
    message = str(excinfo.value)
    assert "ponytail_level" in message and "preferences.toml" in message


# --- team table (T039, US10, FR-037) -----------------------------------------


def test_team_table_coordinators_executors_reviewers(tmp_path):
    # Global preferences defines fallback coordinators and team defaults
    g = tmp_path / "global.toml"
    g.write_text(
        'coordinator_fallback = ["codex:gpt-6.1-sol", "claude:sonnet"]\n'
        '[team]\n'
        'coordinators = ["codex:gpt-6.1-sol"]\n'
        'executors = ["opencode:omni/equipe"]\n'
        'reviewers = ["codex:gpt-6.1-sol"]\n',
        encoding="utf-8",
    )

    # Project with explicit [team] declaring coordinators, executors, and reviewers
    proj = tmp_path / "proj"
    (proj / "docs/crew-chief").mkdir(parents=True)
    (proj / "docs/crew-chief/preferences.toml").write_text(
        '[[tools]]\n'
        'name = "claude-ghost"\n'
        'kind = "claude-code"\n'
        'bin = "claude-ghost"\n'
        '\n'
        '[team]\n'
        'coordinators = ["claude-ghost:claude-opus-5-5", "codex:gpt-6.1-sol"]\n'
        'executors = ["opencode:omni/gratis", "claude-ghost:claude-opus-5-5"]\n'
        'reviewers = ["claude-ghost:claude-sonnet-5", "codex:gpt-6.1-sol"]\n',
        encoding="utf-8",
    )

    prefs = load_prefs(proj, global_path=g)

    # Exposes parsed and validated team table
    assert prefs.team["coordinators"] == [
        "claude-ghost:claude-opus-5-5",
        "codex:gpt-6.1-sol",
    ]
    assert prefs.team["executors"] == [
        "opencode:omni/gratis",
        "claude-ghost:claude-opus-5-5",
    ]
    assert prefs.team["reviewers"] == [
        "claude-ghost:claude-sonnet-5",
        "codex:gpt-6.1-sol",
    ]

    # Accessible via both dict and convenience fields/properties
    assert prefs.team_coordinators == prefs.team["coordinators"]
    assert prefs.team_executors == prefs.team["executors"]
    assert prefs.team_reviewers == prefs.team["reviewers"]
    assert prefs.coordinators == prefs.team["coordinators"]
    assert prefs.executors == prefs.team["executors"]
    assert prefs.reviewers == prefs.team["reviewers"]

    # Project coordinator list overrides global fallback
    assert prefs.team["coordinators"][0] == "claude-ghost:claude-opus-5-5"
    assert prefs.team.origin("coordinators") == "project_team"
    assert prefs.team.origin("executors") == "project_team"
    assert prefs.team.origin("reviewers") == "project_team"
    assert prefs.team_origins == {
        "coordinators": "project_team",
        "executors": "project_team",
        "reviewers": "project_team",
    }

    # When project omits [team], merges over global fallbacks
    empty_proj = tmp_path / "empty_proj"
    (empty_proj / "docs/crew-chief").mkdir(parents=True)
    prefs_fallback = load_prefs(empty_proj, global_path=g)
    assert prefs_fallback.team["coordinators"] == ["codex:gpt-6.1-sol"]
    assert prefs_fallback.team["executors"] == ["opencode:omni/equipe"]
    assert prefs_fallback.team["reviewers"] == ["codex:gpt-6.1-sol"]
    assert prefs_fallback.team.origin("coordinators") == "global_team"
    assert prefs_fallback.team.origin("executors") == "global_team"
    assert prefs_fallback.team.origin("reviewers") == "global_team"


    # Legacy projects with no [team] anywhere produce empty lists
    prefs_legacy = load_prefs(empty_proj, global_path=tmp_path / "empty_global.toml")
    assert prefs_legacy.team["coordinators"] == []
    assert prefs_legacy.team["executors"] == []
    assert prefs_legacy.team["reviewers"] == []
    assert prefs_legacy.team.origin("coordinators") == ""
    assert prefs_legacy.team.origin("executors") == ""
    assert prefs_legacy.team.origin("reviewers") == ""


def test_team_table_must_be_table(tmp_path):
    (tmp_path / "docs/crew-chief").mkdir(parents=True)
    (tmp_path / "docs/crew-chief/preferences.toml").write_text(
        'team = "not-a-table"\n', encoding="utf-8"
    )
    with pytest.raises(PrefsError, match=r"\[team\] must be a table"):
        load_prefs(tmp_path, tmp_path / "n.toml")


@pytest.mark.parametrize("field_name", ["coordinators", "executors", "reviewers"])
def test_team_fields_must_be_lists(tmp_path, field_name):
    (tmp_path / "docs/crew-chief").mkdir(parents=True)
    (tmp_path / "docs/crew-chief/preferences.toml").write_text(
        f"[team]\n{field_name} = 'not-a-list'\n", encoding="utf-8"
    )
    with pytest.raises(PrefsError, match=rf"\[team\]\.{field_name} must be a list"):
        load_prefs(tmp_path, tmp_path / "n.toml")


def test_team_entry_must_be_cli_model_format(tmp_path):
    (tmp_path / "docs/crew-chief").mkdir(parents=True)
    f = tmp_path / "docs/crew-chief/preferences.toml"
    f.write_text('[team]\ncoordinators = ["claude"]\n', encoding="utf-8")
    with pytest.raises(PrefsError, match="must be in 'cli:model' format"):
        load_prefs(tmp_path, tmp_path / "n.toml")

    f.write_text('[team]\ncoordinators = [123]\n', encoding="utf-8")
    with pytest.raises(PrefsError, match="must be a 'cli:model' string"):
        load_prefs(tmp_path, tmp_path / "n.toml")


def test_team_executor_and_reviewer_tools_must_be_known(tmp_path):
    (tmp_path / "docs/crew-chief").mkdir(parents=True)
    f = tmp_path / "docs/crew-chief/preferences.toml"
    f.write_text('[team]\nexecutors = ["fake_tool:model"]\n', encoding="utf-8")
    with pytest.raises(PrefsError, match="unknown tool 'fake_tool'"):
        load_prefs(tmp_path, tmp_path / "n.toml")

    f.write_text('[team]\nreviewers = ["fake_tool:model"]\n', encoding="utf-8")
    with pytest.raises(PrefsError, match="unknown tool 'fake_tool'"):
        load_prefs(tmp_path, tmp_path / "n.toml")


def test_team_claude_ghost_supported_without_server_login(tmp_path):
    (tmp_path / "docs/crew-chief").mkdir(parents=True)
    (tmp_path / "docs/crew-chief/preferences.toml").write_text(
        '[team]\n'
        'coordinators = ["claude-ghost:claude-opus-5-5"]\n'
        'executors = ["claude-ghost:claude-opus-5-5"]\n'
        'reviewers = ["claude-ghost:claude-sonnet-5"]\n',
        encoding="utf-8",
    )
    prefs = load_prefs(tmp_path, tmp_path / "n.toml")
    assert prefs.team["coordinators"] == ["claude-ghost:claude-opus-5-5"]
    assert prefs.team["executors"] == ["claude-ghost:claude-opus-5-5"]
    assert prefs.team["reviewers"] == ["claude-ghost:claude-sonnet-5"]


# --- projects_dir (T031, FR-024) ---------------------------------------------


def test_max_running_executors_defaults_to_3_and_can_be_set(tmp_path):
    f = tmp_path / "global.toml"
    f.write_text("max_running_executors = 5\n", encoding="utf-8")
    prefs = load_prefs(tmp_path, f)
    assert prefs.max_running_executors == 5

    # Default without configuration
    prefs_default = load_prefs(tmp_path, tmp_path / "empty.toml")
    assert prefs_default.max_running_executors == 3


@pytest.mark.parametrize("value", [0, -1, -5, "three", True, False, 2.5])
def test_max_running_executors_must_be_positive_integer(tmp_path, value):
    f = tmp_path / "global.toml"
    toml_val = str(value).lower() if isinstance(value, bool) else repr(value)
    f.write_text(f"max_running_executors = {toml_val}\n", encoding="utf-8")
    with pytest.raises(PrefsError, match="max_running_executors"):
        load_prefs(tmp_path, f)


def test_global_cap_3_project_tries_10(tmp_path):
    """Project-local max_running_executors cannot override or raise global cap."""
    global_file = tmp_path / "global.toml"
    global_file.write_text("max_running_executors = 3\n", encoding="utf-8")
    proj_dir = tmp_path / "project"
    pref_dir = proj_dir / "docs" / "crew-chief"
    pref_dir.mkdir(parents=True, exist_ok=True)
    (pref_dir / "preferences.toml").write_text("max_running_executors = 10\n", encoding="utf-8")
    with pytest.raises(PrefsError, match=r"max_running_executors.*(machine|global)"):
        load_prefs(proj_dir, global_file)


def test_project_tries_different_lease_dir(tmp_path):
    """Project-local executor_lease_dir cannot split machine lease directory."""
    global_file = tmp_path / "global.toml"
    global_file.write_text("max_running_executors = 3\n", encoding="utf-8")
    proj_dir = tmp_path / "project"
    pref_dir = proj_dir / "docs" / "crew-chief"
    pref_dir.mkdir(parents=True, exist_ok=True)
    (pref_dir / "preferences.toml").write_text("executor_lease_dir = '/tmp/other_leases'\n", encoding="utf-8")
    with pytest.raises(PrefsError, match=r"executor_lease_dir.*(machine|global)"):
        load_prefs(proj_dir, global_file)


def test_unchanged_custom_global_config(tmp_path):
    """Trusted operator custom global configuration is preserved."""
    custom_leases = tmp_path / "custom_leases"
    global_file = tmp_path / "global.toml"
    global_file.write_text(
        f"max_running_executors = 5\nexecutor_lease_dir = '{custom_leases}'\n",
        encoding="utf-8",
    )
    proj_dir = tmp_path / "project"
    proj_dir.mkdir(parents=True, exist_ok=True)
    prefs = load_prefs(proj_dir, global_file)
    assert prefs.max_running_executors == 5
    assert prefs.executor_lease_dir == str(custom_leases)



