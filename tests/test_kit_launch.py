"""Tests for kit injection into actual coordinator/executor/reviewer launch paths."""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure the skill scripts directory is on sys.path
ROOT = Path(__file__).resolve().parents[1]
SKILL_SCRIPTS = ROOT / "skill" / "crew-chief" / "scripts"
if str(SKILL_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SKILL_SCRIPTS))

from crew_chief import records
from crew_chief.brief import PONYTAIL_REVIEW_LINE
from crew_chief.config import Prefs
from crew_chief.central_rounds import _kit_preamble
from crew_chief.dispatch import dispatch
from crew_chief.tools import Executor

BACKENDS = ["claude-ghost", "claude", "codex", "opencode"]
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


# --- Coordinator rounds put the kit into the prompt the coordinator CLI receives ---


@pytest.mark.parametrize("backend", ["claude", "codex", "claude-ghost"])
def test_coordinator_round_prompt_carries_kit(tmp_path, backend):
    """The round prompt preamble carries the kit, with reseller rules for claude-ghost."""
    text = _kit_preamble({"cli": backend, "worktree": str(tmp_path)})
    assert "## Style (ponytail full)" in text
    if backend == "claude-ghost":
        assert "## Reseller instructions" in text
        assert "ACC_RESULT: DONE | DONE_WITH_CONCERNS | BLOCKED | NEEDS_CONTEXT" in text
    else:
        assert "## Reseller instructions" not in text


def test_coordinator_round_prompt_rtk_when_active(monkeypatch, tmp_path):
    """Coordinator prompt includes RTK instructions when rtk is active on PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    rtk_bin = bin_dir / "rtk"
    rtk_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    if os.name == "nt":
        rtk_cmd = bin_dir / "rtk.cmd"
        rtk_cmd.write_text(
            f'@echo off\r\n"{sys.executable}" "{rtk_bin}" %*\r\n',
            encoding="utf-8",
        )
    else:
        rtk_bin.chmod(0o755)

    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    text = _kit_preamble({"cli": "claude", "worktree": str(tmp_path)})
    assert "## RTK" in text
    assert "rtk pytest -q" in text


# --- Full Matrix: exec and review roles x backends ---


@pytest.mark.parametrize("backend", BACKENDS)
def test_matrix_exec_role(git_repo, fake_cli_env, backend):
    """Real dispatch for exec role puts kit into brief file for backend."""
    job = records.new_job(git_repo, f"Matrix Exec {backend}", "req", True, "reason")
    p_num = records.add_piece(job, "Matrix Task", "code", "low")
    records.save_job(git_repo, job)

    executor = _make_executor(backend)
    prefs = _make_prefs()

    attempt = dispatch(
        git_repo,
        job["id"],
        p_num,
        executor,
        "# Custom Task\nExecute minimal change.",
        prefs,
        role="exec",
    )
    assert attempt["status"] == "ok"
    worktree = Path(attempt["worktree"])
    brief_content = (worktree / ".captured_brief.md").read_text(encoding="utf-8")

    assert "## Style (ponytail full)" in brief_content
    if backend == "claude-ghost":
        assert "ACC_RESULT" in brief_content


@pytest.mark.parametrize("backend", BACKENDS)
def test_matrix_review_role(git_repo, fake_cli_env, backend):
    """Real dispatch for review role puts kit into brief file for backend."""
    job = records.new_job(git_repo, f"Matrix Rev {backend}", "req", True, "reason")
    p_num = records.add_piece(job, "Matrix Task", "code", "low")
    records.save_job(git_repo, job)

    # Need a target worktree from an exec run first
    exec_attempt = dispatch(
        git_repo,
        job["id"],
        p_num,
        _make_executor("claude"),
        "# Exec Task\nDo it.",
        _make_prefs(),
        role="exec",
    )
    target = Path(exec_attempt["worktree"])

    executor = _make_executor(backend)
    prefs = _make_prefs()

    attempt = dispatch(
        git_repo,
        job["id"],
        p_num,
        executor,
        "# Custom Review Task\nReview diff.",
        prefs,
        role="review",
        review_target=target,
    )
    brief_content = (target / ".captured_brief.md").read_text(encoding="utf-8")

    assert "## Style (ponytail full)" in brief_content
    assert PONYTAIL_REVIEW_LINE in brief_content
    if backend == "claude-ghost":
        assert "ACC_RESULT" in brief_content
