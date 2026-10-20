"""Tests for the cost ladder, the built-in patterns and classify precedence."""

from __future__ import annotations

from crew_chief import config, tiers
from crew_chief.config import classify, load_prefs, model_cap


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")
    return path


def _setup_block(body: str) -> str:
    return f"# >>> acc setup >>>\n{body}# <<< acc setup <<<\n"


def _load(tmp_path, global_text="", project_text=""):
    global_file = tmp_path / "global.toml"
    global_file.unlink(missing_ok=True)
    if global_text:
        _write(global_file, global_text)
    project = tmp_path / "proj"
    if project_text:
        _write(project / "docs/crew-chief/preferences.toml", project_text)
    else:
        project.mkdir(exist_ok=True)
    return load_prefs(project, global_file)


def test_tier_order_and_cost_values():
    assert config.TIER_ORDER == ("free", "included", "cheap", "paid")
    assert tiers.TIER_ORDER == config.TIER_ORDER
    assert config.COST_VALUES == ("free", "included", "cheap", "paid", "forbidden")


def test_builtin_classifies_openrouter_free_and_paid(tmp_path):
    prefs = _load(tmp_path)
    assert classify("opencode:openrouter/deepseek/x:free", prefs) == ("free", False)
    assert classify("opencode:openrouter/deepseek/x", prefs) == ("cheap", False)


def test_builtin_rules_cover_the_documented_patterns(tmp_path):
    prefs = _load(tmp_path)
    assert classify("opencode:go/longcat-preview-free", prefs) == ("free", True)
    assert classify("opencode:x/contributor-model", prefs) == ("free", True)
    assert classify("opencode:opencode/big-pickle-free", prefs) == ("free", False)
    assert classify("opencode:opencode-go/kimi-free", prefs) == ("free", False)
    assert classify("antigravity:gemini-3-flash", prefs) == ("free", False)
    assert classify("opencode:ghostcli/opus", prefs) == ("paid", False)
    assert classify("codex:gpt-5", prefs) == ("paid", False)
    assert classify("claude:sonnet", prefs) == ("paid", False)


def test_unmatched_executor_falls_to_tool_default_then_paid(tmp_path):
    prefs = _load(tmp_path)
    assert classify("cursor:m", prefs, tool_default="free") == ("free", False)
    assert classify("cursor:m", prefs) == ("paid", False)


def test_builtin_tiers_can_be_turned_off(tmp_path):
    prefs = _load(tmp_path, global_text="builtin_tiers = false\n")
    assert prefs.builtin_tiers is False
    assert classify("antigravity:m", prefs) == ("paid", False)


def test_user_rule_beats_setup_block_beats_builtin(tmp_path):
    executor = "opencode:openrouter/deepseek/x"
    user = '[[models]]\nmatch = "opencode:openrouter/*"\ncost = "free"\n'
    setup = _setup_block('[[models]]\nmatch = "opencode:openrouter/*"\ncost = "included"\n')

    prefs = _load(tmp_path, global_text=user + setup)
    assert classify(executor, prefs) == ("free", False)

    prefs = _load(tmp_path, global_text=setup)
    assert classify(executor, prefs) == ("included", False)

    prefs = _load(tmp_path)
    assert classify(executor, prefs) == ("cheap", False)


def test_project_rule_beats_global_user_rule_and_setup_block(tmp_path):
    executor = "claude:sonnet"
    global_text = (
        '[[models]]\nmatch = "claude:*"\ncost = "cheap"\n'
        + _setup_block('[[models]]\nmatch = "claude:*"\ncost = "included"\n')
    )
    project = '[[models]]\nmatch = "claude:*"\ncost = "free"\n'
    prefs = _load(tmp_path, global_text, project)
    assert classify(executor, prefs) == ("free", False)


def test_forbidden_anywhere_wins(tmp_path):
    executor = "claude:sonnet"
    setup = _setup_block('[[models]]\nmatch = "claude:*"\ncost = "forbidden"\n')
    project = '[[models]]\nmatch = "claude:sonnet"\ncost = "free"\n'
    prefs = _load(tmp_path, global_text=setup, project_text=project)
    assert classify(executor, prefs) == ("forbidden", False)


def test_old_paid_rule_is_tier_4(tmp_path):
    prefs = _load(
        tmp_path,
        global_text='[[models]]\nmatch = "opencode:openrouter/*"\ncost = "paid"\n',
    )
    assert classify("opencode:openrouter/deepseek/x", prefs) == ("paid", False)
    assert config.TIER_ORDER[-1] == "paid"


def test_forbid_training_applies_to_a_setup_or_builtin_rule(tmp_path):
    prefs = _load(
        tmp_path,
        global_text="forbid_training = true\n"
        + _setup_block('[[models]]\nmatch = "opencode:x/*"\ncost = "free"\ntrains = true\n'),
    )
    assert classify("opencode:x/m", prefs) == ("forbidden", True)
    assert classify("opencode:go/longcat-preview-free", prefs) == ("forbidden", True)


def test_model_cap_follows_classify_precedence(tmp_path):
    user = '[[models]]\nmatch = "opencode:a/*"\ncost = "cheap"\ncap_usd_day = 2\n'
    setup = _setup_block(
        '[[models]]\nmatch = "opencode:a/*"\ncost = "cheap"\ncap_usd_day = 9\n'
        '[[models]]\nmatch = "opencode:b/*"\ncost = "cheap"\ncap_usd_day = 0.5\n'
    )
    prefs = _load(tmp_path, global_text=user + setup)
    assert model_cap("opencode:a/m", prefs) == 2.0
    assert model_cap("opencode:b/m", prefs) == 0.5
    assert model_cap("opencode:openrouter/x", prefs) is None
    assert model_cap("opencode:none/x", prefs) is None
