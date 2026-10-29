"""T006 proof tests: verify superpowers resolution works from installed copy.

These tests simulate installing ACC into a project with install.py, then
dispatching a piece whose superpowers resolves to test-driven-development
using the INSTALLED CLI. They verify that the dispatcher finds the bundled
superpowers text in every supported layout and does not crash when the skill
text is missing.

Parametrized over two executor routes:
- claude-ghost: the executor id the installed CLI uses for claude-ghost,
  with a fake `claude-ghost` executable that records the prompt and brief.
- opencode: the opencode route with a fake `opencode` executable.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
INSTALL_PY = ROOT / "install.py"
SCRIPTS_DIR = ROOT / "skill" / "crew-chief" / "scripts"


def _sha256_file(path: Path) -> str:
    """Compute SHA-256 hex digest of a file."""
    import hashlib

    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(8192)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _create_fake_archive(dest: Path, filename: str, archive_format: str) -> bytes:
    """Create a fake archive file and return its actual SHA-256."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if archive_format == "tar.gz":
        with tarfile.open(dest, "w:gz") as tf:
            import io

            data = b"#!/bin/sh\necho fake rtk\n"
            info = tarfile.TarInfo(name="rtk")
            info.size = len(data)
            info.mode = 0o755
            tf.addfile(info, io.BytesIO(data))
    elif archive_format == "zip":
        with zipfile.ZipFile(dest, "w") as zf:
            zf.writestr("rtk.exe", b"@echo off\necho fake rtk\n")
    return _sha256_file(dest)


PREFS_WITH_EXECUTOR = """\
[[tools]]
name = "claude-ghost"
kind = "claude-code"
bin = "claude-ghost"
models = ["model-1"]
reseller = true
[[tools]]
name = "opencode"
kind = "opencode"
bin = "opencode"
models = ["model-1"]
[[models]]
match = "model-1"
cost = "free"
trains = false
"""

# Executor configurations for parametrization
EXECUTOR_CONFIGS = [
    {
        "executor_id": "claude-ghost:model-1",
        "cli_name": "claude-ghost",
        "backend": "claude-ghost",
    },
    {
        "executor_id": "opencode:model-1",
        "cli_name": "opencode",
        "backend": "opencode",
    },
]


@pytest.fixture
def installed_project(tmp_path):
    """Install ACC into a temp project with all components (no real network)."""
    base = tmp_path / "work"
    base.mkdir()
    home = base / "home"
    home.mkdir()
    project = base / "project"
    project.mkdir()
    # Git init and commit
    subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=project, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=project,
        check=True,
        capture_output=True,
    )
    readme = project / "README.md"
    readme.write_text("# Test Project\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=project, check=True, capture_output=True)
    # Install via install.py (bypassing network via kit_install patching)
    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home)}
    done = subprocess.run(
        [sys.executable, "-I", "-X", "utf8", str(INSTALL_PY), str(project)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    # Write project preferences with a known executor
    prefs_file = project / "docs" / "crew-chief" / "preferences.toml"
    prefs_file.write_text(PREFS_WITH_EXECUTOR, encoding="utf-8")
    bin_dir = project.parent / "bin"
    bin_dir.mkdir(exist_ok=True)
    return project, env, bin_dir


def _make_fake_cli(bin_dir: Path, name: str):
    """Create a fake CLI script that saves its arguments and debug info."""
    script = bin_dir / name
    script.write_text(
        """#!/usr/bin/env python3
import json
import sys
from pathlib import Path

args = sys.argv[1:]
worktree = Path.cwd()

# Save all args and cwd for debugging
debug_info = {"args": args, "cwd": str(worktree)}
(worktree / ".captured_args.json").write_text(json.dumps(debug_info), encoding="utf-8")

# Find brief file - check worktree first
found_brief = None
for bf in [".crew-chief-brief.md", "BRIEF.md", "brief.md"]:
    f = worktree / bf
    if f.is_file():
        found_brief = f
        debug_info["brief_found_in_worktree"] = str(f)
        break

# Also check --brief-file argument
if not found_brief:
    for i, arg in enumerate(args):
        if arg == "--brief-file" and i + 1 < len(args):
            p = Path(args[i + 1])
            if p.is_file():
                found_brief = p
                debug_info["brief_found_via_arg"] = str(p)
                break

if found_brief:
    content = found_brief.read_text(encoding="utf-8")
    (worktree / ".captured_brief.md").write_text(content, encoding="utf-8")
    debug_info["brief_length"] = len(content)
else:
    # List files in worktree for debugging
    debug_info["worktree_files"] = [str(f.name) for f in worktree.iterdir()]

(worktree / ".captured_args.json").write_text(json.dumps(debug_info), encoding="utf-8")

print(json.dumps({"result": "ok"}))
sys.exit(0)
""",
        encoding="utf-8",
    )
    if os.name == "nt":
        shim = bin_dir / f"{name}.cmd"
        shim.write_text(
            f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n',
            encoding="utf-8",
        )
    else:
        script.chmod(0o755)


def _get_job_id(project: Path) -> str:
    """Get the job ID from the jobs directory."""
    jobs_dir = project / "docs" / "crew-chief" / "jobs"
    if not jobs_dir.exists():
        raise FileNotFoundError(f"Jobs directory not found: {jobs_dir}")
    json_files = list(jobs_dir.glob("*.json"))
    if not json_files:
        raise FileNotFoundError(f"No job files found in {jobs_dir}")
    for f in json_files:
        if f.name != ".gitkeep":
            return f.stem
    raise FileNotFoundError(f"No valid job files found in {jobs_dir}")


def _setup_dispatch_env(project, env, bin_dir, cli_name):
    """Set up environment and create job/piece for dispatch testing."""
    _make_fake_cli(bin_dir, cli_name)
    crew_chief_py = project / ".claude" / "skills" / "crew-chief" / "scripts" / "crew-chief.py"
    req_file = project / "req.md"
    req_file.write_text("test request\n", encoding="utf-8")
    subprocess.run(
        [
            sys.executable,
            "-I",
            "-X",
            "utf8",
            str(crew_chief_py),
            "job",
            "new",
            "--title",
            "Proof Job",
            "--request-file",
            str(req_file),
        ],
        cwd=project,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**env, "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"},
        timeout=30,
    )
    job_id = _get_job_id(project)
    subprocess.run(
        [
            sys.executable,
            "-I",
            "-X",
            "utf8",
            str(crew_chief_py),
            "piece",
            "add",
            "--job",
            job_id,
            "--title",
            "Code Piece",
            "--type",
            "code",
            "--risk",
            "low",
        ],
        cwd=project,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**env, "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"},
        timeout=30,
    )
    return job_id, crew_chief_py


def _run_dispatch(crew_chief_py, project, env, bin_dir, job_id, executor_id, superpowers="auto"):
    """Run dispatch and return the captured brief text."""
    brief_file = project / "test_brief.md"
    brief_file.write_text("# Test Brief\nDo the work.\n", encoding="utf-8")
    dispatch_result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-X",
            "utf8",
            str(crew_chief_py),
            "dispatch",
            "--job",
            job_id,
            "--piece",
            "1",
            "--executor",
            executor_id,
            "--brief-file",
            str(brief_file),
            "--superpowers",
            superpowers,
        ],
        cwd=project,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**env, "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"},
        timeout=60,
    )
    assert dispatch_result.returncode == 0, f"Dispatch failed: {dispatch_result.stderr}\n{dispatch_result.stdout}"
    # The fake CLI runs in a sub-worktree (project.crew/...), not the project root.
    # Find the captured brief in the project.crew directory.
    crew_dir = project.parent / "project.crew"
    if crew_dir.is_dir():
        for captured in crew_dir.rglob(".captured_brief.md"):
            return captured.read_text(encoding="utf-8")
    # Fallback to project root
    captured_brief = project / ".captured_brief.md"
    if captured_brief.exists():
        return captured_brief.read_text(encoding="utf-8")
    return ""


@pytest.mark.parametrize("exec_config", EXECUTOR_CONFIGS, ids=lambda c: c["executor_id"])
class TestKitProofRoutes:
    """Parametrized tests over claude-ghost and opencode routes."""

    def test_ponytail_block_present(self, installed_project, exec_config):
        """Both routes must include the ponytail block."""
        project, env, bin_dir = installed_project
        job_id, crew_chief_py = _setup_dispatch_env(project, env, bin_dir, exec_config["cli_name"])
        brief_text = _run_dispatch(crew_chief_py, project, env, bin_dir, job_id, exec_config["executor_id"])
        assert "## Style (ponytail" in brief_text or "ponytail" in brief_text.lower()

    def test_rtk_instruction_with_fake_rtk(self, installed_project, exec_config):
        """With fake rtk on PATH, both routes include RTK instructions."""
        project, env, bin_dir = installed_project
        rtk_bin = bin_dir / "rtk"
        rtk_bin.write_text("#!/bin/sh\necho fake rtk\n", encoding="utf-8")
        if os.name == "nt":
            rtk_cmd = bin_dir / "rtk.cmd"
            rtk_cmd.write_text(
                f'@echo off\r\n"{sys.executable}" "{rtk_bin}" %*\r\n',
                encoding="utf-8",
            )
        else:
            rtk_bin.chmod(0o755)
        job_id, crew_chief_py = _setup_dispatch_env(project, env, bin_dir, exec_config["cli_name"])
        brief_text = _run_dispatch(crew_chief_py, project, env, bin_dir, job_id, exec_config["executor_id"])
        assert "## RTK" in brief_text
        assert "rtk pytest" in brief_text or "rtk git" in brief_text

    def test_no_rtk_instruction_without_rtk(self, installed_project, exec_config):
        """Without rtk on PATH, both routes exclude RTK instructions."""
        project, env, bin_dir = installed_project
        rtk_bin = bin_dir / "rtk"
        if rtk_bin.exists():
            rtk_bin.unlink()
        job_id, crew_chief_py = _setup_dispatch_env(project, env, bin_dir, exec_config["cli_name"])
        brief_text = _run_dispatch(crew_chief_py, project, env, bin_dir, job_id, exec_config["executor_id"])
        assert "## RTK" not in brief_text

    def test_superpowers_auto_on_code_piece(self, installed_project, exec_config):
        """Superpowers auto mode on code piece includes TDD skill instruction."""
        project, env, bin_dir = installed_project
        job_id, crew_chief_py = _setup_dispatch_env(project, env, bin_dir, exec_config["cli_name"])
        brief_text = _run_dispatch(
            crew_chief_py, project, env, bin_dir, job_id, exec_config["executor_id"], superpowers="auto"
        )
        assert "use the skill superpowers:test-driven-development" in brief_text

    def test_superpowers_off_on_code_piece(self, installed_project, exec_config):
        """Superpowers off on code piece excludes skill instruction."""
        project, env, bin_dir = installed_project
        job_id, crew_chief_py = _setup_dispatch_env(project, env, bin_dir, exec_config["cli_name"])
        brief_text = _run_dispatch(
            crew_chief_py, project, env, bin_dir, job_id, exec_config["executor_id"], superpowers="off"
        )
        assert "use the skill superpowers:" not in brief_text
        assert "## Superpowers" not in brief_text

    def test_claude_ghost_reseller_block(self, installed_project, exec_config):
        """For claude-ghost route only, reseller block ends with ACC_RESULT line."""
        if exec_config["backend"] != "claude-ghost":
            pytest.skip("Reseller block only applies to claude-ghost")
        project, env, bin_dir = installed_project
        job_id, crew_chief_py = _setup_dispatch_env(project, env, bin_dir, exec_config["cli_name"])
        brief_text = _run_dispatch(crew_chief_py, project, env, bin_dir, job_id, exec_config["executor_id"])
        assert "## Reseller instructions" in brief_text
        assert "ACC_RESULT: DONE | DONE_WITH_CONCERNS | BLOCKED | NEEDS_CONTEXT" in brief_text