"""Tests for tool kinds: model listing, command building, output parsing."""

from __future__ import annotations

from crew_chief.tools import (
    QUOTA_PATTERNS,
    Executor,
    build_command,
    list_models,
    missing_env,
    parse_reported_model,
    tool_env,
    wants_effort,
)

# --- build_command ---------------------------------------------------------


def test_build_claude_code_review_disallows_writes(tmp_path):
    tool = {"name": "claude", "kind": "claude-code", "bin": "claude", "effort": False}
    cmd = build_command(
        tool, "sonnet", tmp_path, "do it", tmp_path / "brief.md",
        tmp_path / "out.json", "high", True,
    )
    assert "--disallowedTools=Bash,Edit,Write,NotebookEdit" in cmd
    assert "--effort" not in cmd


def test_build_claude_code_effort_flag_when_enabled(tmp_path):
    tool = {"name": "claude", "kind": "claude-code", "bin": "claude", "effort": True}
    cmd = build_command(
        tool, "sonnet", tmp_path, "p", tmp_path / "b.md",
        tmp_path / "o.json", "high", False,
    )
    assert cmd[-2:] == ["--effort", "high"]
    assert "--disallowedTools=Bash,Edit,Write,NotebookEdit" not in cmd


def test_build_codex_effort_and_sandbox(tmp_path):
    tool = {"name": "codex", "kind": "codex", "bin": "codex"}
    cmd = build_command(
        tool, "gpt-x", tmp_path, "write tests", tmp_path / "brief.md",
        tmp_path / "out.json", "high", False,
    )
    assert "workspace-write" in cmd
    assert 'model_reasoning_effort="high"' in cmd
    assert cmd[-1] == "write tests"


def test_build_codex_review_is_read_only(tmp_path):
    tool = {"name": "codex", "kind": "codex", "bin": "codex"}
    cmd = build_command(
        tool, "gpt-x", tmp_path, "p", tmp_path / "b.md", tmp_path / "o.json",
        None, True,
    )
    assert "read-only" in cmd
    assert "-c" not in cmd


def test_build_opencode_command(tmp_path):
    tool = {"name": "opencode", "kind": "opencode", "bin": "opencode"}
    cmd = build_command(
        tool, "oc/m", tmp_path, "p", tmp_path / "b.md", tmp_path / "o.json",
        None, False,
    )
    assert cmd[1:] == ["run", "-m", "oc/m", "p"]


def test_build_antigravity_review_uses_plan_mode(tmp_path):
    tool = {"name": "antigravity", "kind": "antigravity", "bin": "agy"}
    review_cmd = build_command(
        tool, "m", tmp_path, "p", tmp_path / "b.md", tmp_path / "o.json",
        "high", True,
    )
    mode_at = review_cmd.index("--mode")
    assert review_cmd[mode_at:mode_at + 2] == ["--mode", "plan"]
    assert review_cmd[-2:] == ["--effort", "high"]
    work_cmd = build_command(
        tool, "m", tmp_path, "p", tmp_path / "b.md", tmp_path / "o.json",
        None, False,
    )
    assert "--mode" not in work_cmd and "--effort" not in work_cmd


def test_build_generic_formats_placeholders(tmp_path):
    wt = tmp_path / "my wt"
    tool = {
        "name": "cursor", "kind": "generic", "bin": "cursor-agent",
        "run_cmd": ["cursor-agent", "-p", "--model", "{model}", "{worktree}",
                    "{brief_file}", "{output_file}", "{prompt}"],
    }
    cmd = build_command(
        tool, "g", wt, "p", tmp_path / "b.md", tmp_path / "o.json", None, False,
    )
    assert str(wt) in cmd
    assert all(isinstance(arg, str) for arg in cmd)
    assert cmd[-1] == "p"


# --- list_models -----------------------------------------------------------


def test_list_models_antigravity_parses_tabs(fake_bin):
    fake_bin(
        "agy",
        "print('Fetching available models...')\n"
        "print('m-a\\tModel A')\n"
        "print('m-b\\tModel B')\n",
    )
    tool = {"name": "antigravity", "kind": "antigravity", "bin": "agy"}
    assert list_models(tool) == ["m-a", "m-b"]


def test_list_models_opencode_one_id_per_line(fake_bin):
    fake_bin("opencode", "print('oc/free\\n\\noc/paid')\n")
    tool = {"name": "opencode", "kind": "opencode", "bin": "opencode"}
    assert list_models(tool) == ["oc/free", "oc/paid"]


def test_list_models_fixed_lists_for_codex_and_claude():
    assert list_models({"name": "claude", "kind": "claude-code", "bin": "claude",
                        "models": ["sonnet", "opus"]}) == ["sonnet", "opus"]
    assert list_models({"name": "codex", "kind": "codex", "bin": "codex",
                        "models": []}) == []


def test_list_models_generic_list_cmd_else_models(fake_bin):
    fake_bin("lister", "print('g-a\\ng-b')\n")
    assert list_models({"name": "g", "kind": "generic", "bin": "gbin",
                        "list_cmd": ["lister"], "models": ["ignored"]}) == ["g-a", "g-b"]
    assert list_models({"name": "g", "kind": "generic", "bin": "gbin",
                        "models": ["g-c"]}) == ["g-c"]


def test_list_models_never_raises_on_cli_failure():
    tool = {"name": "ghost", "kind": "opencode", "bin": "definitely-not-here-xyz"}
    assert list_models(tool) == []


# --- parse_reported_model --------------------------------------------------


def test_parse_reported_model_claude_json():
    assert parse_reported_model(
        {"kind": "claude-code"}, '{"modelUsage": {"claude-x": {}}}'
    ) == "claude-x"


def test_parse_reported_model_antigravity_and_garbage():
    assert parse_reported_model({"kind": "antigravity"}, '{"model": "gemini-2.5"}') == "gemini-2.5"
    assert parse_reported_model({"kind": "antigravity"}, "not json") is None
    assert parse_reported_model({"kind": "opencode"}, '{"modelUsage": {"x": {}}}') is None


# --- QUOTA_PATTERNS --------------------------------------------------------


def test_quota_patterns_match_case_insensitive():
    for needle in ("Error 429", "Quota exceeded", "RATE LIMIT", "usage limit", "Too Many Requests"):
        assert any(p.search(needle) for p in QUOTA_PATTERNS), needle
    assert not any(p.search("all good") for p in QUOTA_PATTERNS)


# --- tool_env --------------------------------------------------------------


def test_tool_env_returns_copy_with_declared_names(monkeypatch):
    monkeypatch.setenv("CREWCHIEF_TEST_PRESENT", "value")
    env = tool_env({"name": "t", "kind": "generic", "bin": "b",
                    "env": ["CREWCHIEF_TEST_PRESENT"]})
    assert env is not None and env["CREWCHIEF_TEST_PRESENT"] == "value"


def test_tool_env_missing_name_returns_none(monkeypatch):
    monkeypatch.delenv("CREWCHIEF_TEST_ABSENT", raising=False)
    assert tool_env({"name": "t", "kind": "generic", "bin": "b",
                     "env": ["CREWCHIEF_TEST_ABSENT"]}) is None


def test_missing_env_names_only_the_truly_missing(monkeypatch):
    monkeypatch.setenv("CREWCHIEF_PRESENT_X", "value")
    monkeypatch.delenv("CREWCHIEF_MISSING_Y", raising=False)
    assert missing_env({"name": "t", "kind": "generic", "bin": "b",
                        "env": ["CREWCHIEF_PRESENT_X", "CREWCHIEF_MISSING_Y"]}) \
        == ["CREWCHIEF_MISSING_Y"]


def test_missing_env_is_empty_when_all_names_are_present(monkeypatch):
    monkeypatch.setenv("CREWCHIEF_PRESENT_X", "value")
    assert missing_env({"name": "t", "kind": "generic", "bin": "b",
                        "env": ["CREWCHIEF_PRESENT_X"]}) == []


def test_missing_env_is_empty_without_declared_names(monkeypatch):
    assert missing_env({"name": "t", "kind": "generic", "bin": "b"}) == []


# --- wants_effort / Executor ----------------------------------------------


def test_wants_effort_defaults_by_kind():
    assert wants_effort({"kind": "codex"}) is True
    assert wants_effort({"kind": "antigravity"}) is True
    assert wants_effort({"kind": "claude-code"}) is False
    assert wants_effort({"kind": "opencode"}) is False
    assert wants_effort({"kind": "generic"}) is False
    assert wants_effort({"kind": "codex", "effort": False}) is False


def test_executor_is_a_plain_dataclass():
    tool = {"name": "t", "kind": "opencode", "bin": "b"}
    ex = Executor(id="t:m", tool=tool, model="m", cost="free", trains=True)
    assert (ex.id, ex.model, ex.cost, ex.trains) == ("t:m", "m", "free", True)
    ex.cost = "paid"  # mutable: a frozen dataclass would raise
    assert ex.cost == "paid"
