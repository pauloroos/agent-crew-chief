"""Tests for kit injection into actual dispatch briefs and attempt records."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import warnings
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure the skill scripts directory is on sys.path
ROOT = Path(__file__).resolve().parents[1]
SKILL_SCRIPTS = ROOT / "skill" / "crew-chief" / "scripts"
if str(SKILL_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SKILL_SCRIPTS))

from crew_chief import records
from crew_chief.brief import PONYTAIL_REVIEW_LINE, TaskMeta, render_exec_brief, render_review_brief
from crew_chief.config import Prefs
from crew_chief.dispatch import dispatch
from crew_chief.kit import (
    ALLOWED_SUPERPOWERS,
    detect_rtk,
    render_kit_brief,
    reset_kit_warnings,
    resolve_superpowers,
)
from crew_chief.speckit import SpecTask
from crew_chief.tools import Executor, build_command

BACKENDS = ["claude-ghost", "claude", "codex", "opencode", "orca"]
ROLES = ["coordinator", "exec", "review"]


FAKE_CLI_SCRIPT = """#!{executable}
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
worktree = Path.cwd()

# Find any brief file in worktree or referenced in arguments
for name in [".crew-chief-brief.md", "BRIEF.md", "brief.md"]:
    f = worktree / name
    if f.is_file():
        (worktree / ".captured_brief.md").write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
        break

for i, arg in enumerate(args):
    p = Path(arg)
    if ("BRIEF" in arg or "brief" in arg.lower()) and p.is_file():
        (worktree / ".captured_brief.md").write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
    if arg in ("-p", "--prompt") and i + 1 < len(args):
        (worktree / ".captured_prompt.txt").write_text(args[i + 1], encoding="utf-8")

# Make a change in the worktree for exec
out = worktree / "out.txt"
if not out.exists():
    out.write_text("change made\\n", encoding="utf-8")

# Output file support
for i, arg in enumerate(args):
    if arg == "-o" and i + 1 < len(args):
        Path(args[i + 1]).write_text("output ok\\n", encoding="utf-8")

print(json.dumps({{"result": "ok", "usage": {{"input_tokens": 10, "output_tokens": 10}}}}))
sys.exit(0)
"""


@pytest.fixture
def fake_cli_env(tmp_path, monkeypatch):
    """Set up fake CLIs on PATH and temporary HOME."""
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    for name in BACKENDS:
        script = bin_dir / name
        script.write_text(
            FAKE_CLI_SCRIPT.format(executable=sys.executable), encoding="utf-8"
        )
        if os.name == "nt":
            shim = bin_dir / f"{name}.cmd"
            shim.write_text(
                f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n',
                encoding="utf-8",
            )
        else:
            script.chmod(0o755)

    return {"home": home, "bin": bin_dir}


def _make_prefs(**overrides) -> Prefs:
    values = dict(
        tools=[],
        models=[],
        strong_order=[],
        explore_rate=0.1,
        forbid_training=False,
        sensitive_paths=[],
        issue_tracker="none",
        dispatch_timeout_s=900,
        review_default="",
        ponytail_level="full",
        rtk="auto",
        superpowers="off",
    )
    values.update(overrides)
    return Prefs(**values)


def _make_tool(name: str, **kwargs) -> dict:
    kind = (
        "claude-code"
        if name in ("claude", "claude-ghost")
        else "codex"
        if name == "codex"
        else "opencode"
        if name == "opencode"
        else "generic"
    )
    tool = {
        "name": name,
        "kind": kind,
        "bin": name,
        "models": ["model-1"],
    }
    if kind == "generic":
        tool["run_cmd"] = [name, "run", "--prompt", "{prompt}"]
    if name == "claude-ghost":
        tool["reseller"] = True
    tool.update(kwargs)
    return tool


def _make_executor(name: str = "claude", **tool_kwargs) -> Executor:
    tool = _make_tool(name, **tool_kwargs)
    return Executor(
        id=f"{name}:model-1", tool=tool, model="model-1", cost="free", trains=False
    )


# --- Gap 1: Duplicate ponytail ---


def test_real_dispatch_spec_exec_brief_no_duplicate_ponytail(git_repo, fake_cli_env):
    """Real dispatch with spec exec brief has exactly one ponytail block."""
    job = records.new_job(git_repo, "Test Job", "req", True, "reason")
    p_num = records.add_piece(job, "Task 1", "code", "low")
    records.save_job(git_repo, job)

    task = SpecTask("T001", False, True, "US1", "Task 1", "1", (), 1)
    meta = TaskMeta("code", "low", proof="true")
    spec_brief = render_exec_brief(task, meta, ["REF-1"], [], "feature-x", ponytail_level="full")

    # The spec brief already has ponytail
    assert "## Style (ponytail full)" in spec_brief

    executor = _make_executor("claude")
    prefs = _make_prefs(ponytail_level="full")

    attempt = dispatch(
        git_repo,
        job["id"],
        p_num,
        executor,
        spec_brief,
        prefs,
        role="exec",
    )
    assert attempt["status"] == "ok"

    worktree = Path(attempt["worktree"])
    brief_content = (worktree / ".captured_brief.md").read_text(encoding="utf-8")

    # Exactly one ponytail block must reach the agent
    assert brief_content.count("## Style (ponytail full)") == 1


def test_real_dispatch_custom_brief_has_exactly_one_ponytail(git_repo, fake_cli_env):
    """Real dispatch with custom brief file has exactly one ponytail block."""
    job = records.new_job(git_repo, "Test Job 2", "req", True, "reason")
    p_num = records.add_piece(job, "Task 2", "code", "low")
    records.save_job(git_repo, job)

    custom_brief = "# Custom Task\nDo something custom and specific.\n"
    assert "## Style (ponytail" not in custom_brief

    executor = _make_executor("codex")
    prefs = _make_prefs(ponytail_level="full")

    attempt = dispatch(
        git_repo,
        job["id"],
        p_num,
        executor,
        custom_brief,
        prefs,
        role="exec",
    )
    assert attempt["status"] == "ok"

    worktree = Path(attempt["worktree"])
    brief_content = (worktree / ".captured_brief.md").read_text(encoding="utf-8")

    # Exactly one ponytail block must reach the agent
    assert brief_content.count("## Style (ponytail full)") == 1


def test_real_dispatch_spec_review_brief_no_duplicate_ponytail(git_repo, fake_cli_env):
    """Real dispatch with spec review brief has exactly one ponytail block."""
    job = records.new_job(git_repo, "Test Job Review", "req", True, "reason")
    p_num = records.add_piece(job, "Task Review", "code", "low")
    records.save_job(git_repo, job)

    # First do an exec attempt to get a target
    exec_attempt = dispatch(
        git_repo,
        job["id"],
        p_num,
        _make_executor("claude"),
        "# Exec\nDo it.",
        _make_prefs(),
        role="exec",
    )
    target = Path(exec_attempt["worktree"])

    task = SpecTask("T002", False, True, "US1", "Task Review", "1", (), 1)
    meta = TaskMeta("code", "low", proof="true")
    spec_review_brief = render_review_brief(
        task, meta, ["REF-1"], [], str(target), "start", "end", ponytail_level="full"
    )

    executor = _make_executor("claude")
    prefs = _make_prefs(ponytail_level="full")

    attempt = dispatch(
        git_repo,
        job["id"],
        p_num,
        executor,
        spec_review_brief,
        prefs,
        role="review",
        review_target=target,
    )

    brief_content = (target / ".captured_brief.md").read_text(encoding="utf-8")
    assert brief_content.count("## Style (ponytail full)") == 1
    assert brief_content.count(PONYTAIL_REVIEW_LINE) == 1


# --- Gap 2: Role mismatch (dispatch role "review" vs "reviewer") ---


def test_real_dispatch_review_role_ponytail_review_line(git_repo, fake_cli_env):
    """Real dispatch with role='review' includes reviewer ponytail line."""
    job = records.new_job(git_repo, "Test Job Role", "req", True, "reason")
    p_num = records.add_piece(job, "Task Role", "code", "low")
    records.save_job(git_repo, job)

    exec_attempt = dispatch(
        git_repo,
        job["id"],
        p_num,
        _make_executor("claude"),
        "# Exec\nDo it.",
        _make_prefs(),
        role="exec",
    )
    target = Path(exec_attempt["worktree"])

    custom_review_brief = "# Review Task\nReview the work."
    attempt = dispatch(
        git_repo,
        job["id"],
        p_num,
        _make_executor("claude"),
        custom_review_brief,
        _make_prefs(ponytail_level="full"),
        role="review",
        review_target=target,
    )

    brief_content = (target / ".captured_brief.md").read_text(encoding="utf-8")
    assert PONYTAIL_REVIEW_LINE in brief_content


# --- Gap 4: Superpowers flags and precedence ---


def test_job_new_superpowers_flag(git_repo):
    """job new accepts and validates --superpowers, storing it on the job."""
    from crew_chief.cli_parser import build_parser

    parser = build_parser()
    req_file = git_repo / "req.md"
    req_file.write_text("request text\n", encoding="utf-8")

    # Valid superpower
    args = parser.parse_args([
        "job", "new",
        "--title", "Super Job",
        "--request-file", str(req_file),
        "--superpowers", "test-driven-development",
    ])
    assert args.superpowers == "test-driven-development"

    job = records.new_job(
        git_repo, "Super Job", "req", True, "reason", superpowers="test-driven-development"
    )
    assert job.get("superpowers") == "test-driven-development"

    # Invalid superpower raises
    with pytest.raises(ValueError, match="unknown superpowers"):
        records.new_job(git_repo, "Bad Job", "req", True, "reason", superpowers="invalid-skill")


def test_piece_add_superpowers_flag(git_repo):
    """piece add accepts and validates --superpowers, storing it on the piece."""
    from crew_chief.cli_parser import build_parser

    parser = build_parser()
    args = parser.parse_args([
        "piece", "add",
        "--job", "job-1",
        "--title", "Piece Super",
        "--type", "code",
        "--risk", "low",
        "--superpowers", "systematic-debugging",
    ])
    assert args.superpowers == "systematic-debugging"

    job = records.new_job(git_repo, "Job P", "req", True, "reason")
    p_num = records.add_piece(job, "P1", "code", "low", superpowers="systematic-debugging")
    piece = job["pieces"][p_num - 1]
    assert piece.get("superpowers") == "systematic-debugging"


def test_dispatch_superpowers_precedence(git_repo, fake_cli_env):
    """Precedence: dispatch > piece > job > prefs > off."""
    # 1. Job has systematic-debugging, piece has verification-before-completion, dispatch has TDD
    job = records.new_job(
        git_repo, "Job Prec", "req", True, "reason", superpowers="systematic-debugging"
    )
    p_num = records.add_piece(
        job, "P Prec", "code", "low", superpowers="verification-before-completion"
    )
    records.save_job(git_repo, job)

    executor = _make_executor("claude")
    prefs = _make_prefs(superpowers="off")

    # Dispatch override wins
    attempt = dispatch(
        git_repo,
        job["id"],
        p_num,
        executor,
        "# Task\nDo it.",
        prefs,
        superpowers="test-driven-development",
    )
    worktree = Path(attempt["worktree"])
    brief = (worktree / ".captured_brief.md").read_text(encoding="utf-8")
    assert "## Superpowers (test-driven-development)" in brief
    assert "systematic-debugging" not in brief
    assert "verification-before-completion" not in brief

    # 2. Without dispatch override, piece wins
    attempt2 = dispatch(
        git_repo,
        job["id"],
        p_num,
        executor,
        "# Task\nDo it.",
        prefs,
    )
    worktree2 = Path(attempt2["worktree"])
    brief2 = (worktree2 / ".captured_brief.md").read_text(encoding="utf-8")
    assert "## Superpowers (verification-before-completion)" in brief2


def test_superpowers_auto_mode_uses_piece_type():
    """Auto mode selects skill based on piece 'type' key."""
    # code/tests -> TDD
    mode, skill = resolve_superpowers(pref="auto", task_type="code")
    assert mode == "auto" and skill == "test-driven-development"

    mode, skill = resolve_superpowers(pref="auto", task_type="tests")
    assert mode == "auto" and skill == "test-driven-development"

    # bug -> systematic-debugging
    mode, skill = resolve_superpowers(pref="auto", task_type="bug")
    assert mode == "auto" and skill == "systematic-debugging"

    # docs -> off
    mode, skill = resolve_superpowers(pref="auto", task_type="docs")
    assert mode == "off" and skill is None


# --- Gap 5: Attempt record stores superpowers: {"mode": ..., "skill": ...} ---


def test_real_dispatch_records_superpowers_in_attempt(git_repo, fake_cli_env):
    """Every exec and review attempt stores superpowers: {'mode': ..., 'skill': ...}."""
    job = records.new_job(
        git_repo, "Job Att", "req", True, "reason", superpowers="test-driven-development"
    )
    p_num = records.add_piece(job, "P Att", "code", "low")
    records.save_job(git_repo, job)

    executor = _make_executor("claude")
    prefs = _make_prefs()

    # Exec attempt
    exec_att = dispatch(
        git_repo,
        job["id"],
        p_num,
        executor,
        "# Exec task\nDo it.",
        prefs,
    )
    assert "superpowers" in exec_att
    assert exec_att["superpowers"] == {
        "mode": "test-driven-development",
        "skill": "test-driven-development",
    }

    # Review attempt
    target = Path(exec_att["worktree"])
    rev_att = dispatch(
        git_repo,
        job["id"],
        p_num,
        executor,
        "# Review task\nReview it.",
        prefs,
        role="review",
        review_target=target,
    )
    assert "superpowers" in rev_att
    assert rev_att["superpowers"] == {
        "mode": "test-driven-development",
        "skill": "test-driven-development",
    }


# --- Gap 6: Brief line: use the skill superpowers:<skill> ---


def test_brief_contains_use_the_skill_line(git_repo, fake_cli_env):
    """When a skill is active, the brief contains the exact line: use the skill superpowers:<skill>."""
    job = records.new_job(git_repo, "Job Skill Line", "req", True, "reason")
    p_num = records.add_piece(
        job, "P Skill", "code", "low", superpowers="test-driven-development"
    )
    records.save_job(git_repo, job)

    executor = _make_executor("claude")
    attempt = dispatch(
        git_repo,
        job["id"],
        p_num,
        executor,
        "# Task\nDo it.",
        _make_prefs(),
    )
    worktree = Path(attempt["worktree"])
    brief = (worktree / ".captured_brief.md").read_text(encoding="utf-8")
    assert "use the skill superpowers:test-driven-development" in brief


# --- Gap 7: claude-ghost flag superpowers_flag ---


def test_claude_ghost_superpowers_flag_passed_only_when_declared(tmp_path):
    """Pass --superpowers only when tool definition declares superpowers_flag."""
    brief_file = tmp_path / "brief.md"
    output_file = tmp_path / "out.txt"
    brief_file.write_text("brief", encoding="utf-8")

    # 1. With superpowers_flag declared and superpowers active
    tool_with_flag = _make_tool("claude-ghost", superpowers_flag="--superpowers")
    cmd_with = build_command(
        tool_with_flag,
        "sonnet",
        tmp_path,
        "prompt text",
        brief_file,
        output_file,
        None,
        False,
        superpowers=True,
    )
    assert "--superpowers" in cmd_with

    # 2. Without superpowers_flag declared (default none)
    tool_without_flag = _make_tool("claude-ghost")
    cmd_without = build_command(
        tool_without_flag,
        "sonnet",
        tmp_path,
        "prompt text",
        brief_file,
        output_file,
        None,
        False,
        superpowers=True,
    )
    assert "--superpowers" not in cmd_without


# --- Gap 8: Reseller marker ---


def test_reseller_tool_gets_reseller_block(git_repo, fake_cli_env):
    """A tool definition with reseller = true gets the reseller block like claude-ghost."""
    job = records.new_job(git_repo, "Job Reseller", "req", True, "reason")
    p_num = records.add_piece(job, "P Reseller", "code", "low")
    records.save_job(git_repo, job)

    # Tool named 'custom-agent' but with reseller=True
    executor = _make_executor("codex", reseller=True)
    attempt = dispatch(
        git_repo,
        job["id"],
        p_num,
        executor,
        "# Task\nDo it.",
        _make_prefs(),
    )
    worktree = Path(attempt["worktree"])
    brief = (worktree / ".captured_brief.md").read_text(encoding="utf-8")

    assert "## Reseller instructions" in brief
    assert "concise written rationale" in brief
    assert "ACC_RESULT: DONE | DONE_WITH_CONCERNS | BLOCKED | NEEDS_CONTEXT" in brief


# --- Gap 9: rtk = 'on' with no rtk on PATH warns once ---


def test_rtk_on_no_binary_warns_once_and_renders_nothing(git_repo, fake_cli_env, monkeypatch):
    """rtk = 'on' with no rtk binary warns once per process and renders nothing."""
    orig_which = shutil.which
    monkeypatch.setattr(
        "crew_chief.kit.shutil.which",
        lambda cmd, *a, **kw: None if cmd == "rtk" else orig_which(cmd, *a, **kw),
    )
    reset_kit_warnings()
    job = records.new_job(git_repo, "Job RTK Warn", "req", True, "reason")
    p_num = records.add_piece(job, "P RTK", "code", "low")
    records.save_job(git_repo, job)

    prefs = _make_prefs(rtk="on")

    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")

        attempt1 = dispatch(
            git_repo,
            job["id"],
            p_num,
            _make_executor("claude"),
            "# Task 1\nDo it.",
            prefs,
        )
        # Second call in same process
        attempt2 = dispatch(
            git_repo,
            job["id"],
            p_num,
            _make_executor("claude"),
            "# Task 2\nDo it.",
            prefs,
        )

        rtk_warnings = [w for w in recorded if "rtk is configured 'on'" in str(w.message)]
        assert len(rtk_warnings) == 1

        brief1 = (Path(attempt1["worktree"]) / ".captured_brief.md").read_text(encoding="utf-8")
        assert "## RTK" not in brief1
        assert "rtk pytest" not in brief1
