"""Tests for SDD enforcement on free-text requests (T028, US7, FR-032)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from crew_chief import coordinator_claude
from crew_chief.central_launch import prepare_launch
from crew_chief.sdd_gate import (
    ensure_spec_directory,
    enter_spec_phase,
    is_spec_phase,
    next_spec_number,
    spec_slug,
)

RELEASE_VERSION = "0.4.0"

CLAUDE_HELP = """Usage: claude [options] [command] [prompt]

Options:
  -p, --print                       Print response and exit
  --output-format <format>          Output format (text, json, stream-json)
  --verbose                         Verbose output
  -r, --resume [sessionId]          Resume a conversation
  --permission-mode <mode>          Permission mode (choices: "default", "dontAsk", "plan")
  --dangerously-skip-permissions    Bypass all permission checks
"""


@pytest.fixture(autouse=True)
def isolated_probes(monkeypatch):
    monkeypatch.setattr(coordinator_claude, "_PROBED", {})


@pytest.fixture(autouse=True)
def calling_cli(monkeypatch):
    monkeypatch.setenv("CREW_CHIEF_COORDINATOR", "claude")


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout


def _write_acc_version(root: Path, version: str) -> Path:
    path = root / ".claude" / "skills" / "crew-chief" / "scripts" / "crew_chief" / "__init__.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'__version__ = "{version}"\n', encoding="utf-8")
    return path


def _make_repo(path: Path, version: str = RELEASE_VERSION) -> Path:
    path.mkdir(parents=True)
    _git(path, "init")
    _git(path, "config", "user.name", "Test User")
    _git(path, "config", "user.email", "test@example.com")
    _write_acc_version(path, version)
    (path / "README.md").write_text("# repo\n", encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "initial commit")
    return path


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return _make_repo(tmp_path / "repo")


def _make_release(root: Path, version: str) -> Path:
    init = root / "skill" / "crew-chief" / "scripts" / "crew_chief" / "__init__.py"
    init.parent.mkdir(parents=True, exist_ok=True)
    init.write_text(f'__version__ = "{version}"\n', encoding="utf-8")
    (root / "install.py").write_text(
        "\n".join([
            "import sys",
            "from pathlib import Path",
            "project = Path(sys.argv[1])",
            "init = project / '.claude' / 'skills' / 'crew-chief' / 'scripts' / 'crew_chief' / '__init__.py'",
            "init.parent.mkdir(parents=True, exist_ok=True)",
            f"init.write_text({init.read_text(encoding='utf-8')!r}, encoding='utf-8')",
            "",
        ]),
        encoding="utf-8",
    )
    return root


@pytest.fixture
def release(tmp_path: Path) -> Path:
    return _make_release(tmp_path / "release", RELEASE_VERSION)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "central"


def _fake_claude(fake_bin) -> None:
    script = "\n".join([
        "import sys",
        f"HELP = {CLAUDE_HELP!r}",
        "if '--version' in sys.argv[1:]:",
        "    print('1.2.3 (Claude Code)')",
        "elif '--help' in sys.argv[1:]:",
        "    print(HELP)",
        "else:",
        "    print('unexpected call')",
        "    raise SystemExit(2)",
        "",
    ])
    fake_bin("claude", script)


def test_free_text_creates_specs_directory(repo, release, root, fake_bin, tmp_path):
    """Jobs from free text (MCP, CLI, agent) enter spec phase; creates specs/NNN-<slug>/ with spec.md, plan.md, tasks.md if absent."""
    _fake_claude(fake_bin)

    # Pre-populate repo with an existing spec 001-existing to verify numbering increments to 002
    existing_spec = repo / "specs" / "001-existing"
    existing_spec.mkdir(parents=True)
    (existing_spec / "spec.md").write_text("# Existing\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "add existing spec 001")

    req_file = tmp_path / "request.md"
    req_file.write_text("# Add User Authentication\n\nSupport password and token auth.\n", encoding="utf-8")

    opts = {
        "mode": "raw-request",
        "authorize_raw": True,
        "release_root": release,
    }

    result = prepare_launch(repo, req_file, opts, root)
    assert result["ok"] is True, result.get("error")

    record = result["record"]
    worktree = Path(record["worktree"])

    # 1. Job enters spec phase
    assert is_spec_phase(record) is True
    assert record["authorization"].get("phase") == "spec"
    spec_path_str = record["authorization"].get("spec_path")
    assert spec_path_str is not None

    spec_dir = Path(spec_path_str)
    assert spec_dir.is_dir()
    assert spec_dir.parent == worktree / "specs"

    # 2. Sequential numbering NNN-<slug>: increments after 001 -> 002
    assert spec_dir.name == "002-add-user-authentication"

    # 3. spec.md, plan.md, tasks.md created if absent
    spec_md = spec_dir / "spec.md"
    plan_md = spec_dir / "plan.md"
    tasks_md = spec_dir / "tasks.md"
    assert spec_md.is_file()
    assert plan_md.is_file()
    assert tasks_md.is_file()

    # Content checks
    assert "Add User Authentication" in spec_md.read_text(encoding="utf-8")
    assert plan_md.read_text(encoding="utf-8").strip()
    assert tasks_md.read_text(encoding="utf-8").strip()

    # 4. If absent: existing files are preserved and not overwritten
    spec_md.write_text("# Custom Spec Content That Must Not Be Overwritten\n", encoding="utf-8")
    spec_dir_recreated = ensure_spec_directory(worktree, title="Add User Authentication")
    assert (spec_dir_recreated / "spec.md").read_text(encoding="utf-8") == "# Custom Spec Content That Must Not Be Overwritten\n"


def test_explicit_spec_jobs_preserve_explicit_spec_dir(repo, release, root, fake_bin, tmp_path):
    """Explicit-spec jobs preserve their provided spec directory without auto-generating free-text specs."""
    _fake_claude(fake_bin)

    explicit_spec = tmp_path / "explicit-spec"
    explicit_spec.mkdir()
    for name in ("spec.md", "plan.md", "tasks.md", "crew.toml"):
        (explicit_spec / name).write_text(f"# {name}\n", encoding="utf-8")

    req_file = tmp_path / "request.md"
    req_file.write_text("# Direct Spec Launch\n", encoding="utf-8")

    opts = {
        "mode": "direct-spec",
        "spec_dir": str(explicit_spec),
        "release_root": release,
    }

    result = prepare_launch(repo, req_file, opts, root)
    assert result["ok"] is True, result.get("error")

    record = result["record"]
    worktree = Path(record["worktree"])

    # Explicit spec copied to specs/explicit-spec
    expected_spec = worktree / "specs" / "explicit-spec"
    assert expected_spec.is_dir()
    assert (expected_spec / "spec.md").read_text(encoding="utf-8") == "# spec.md\n"
    # No auto-numbered free-text spec directory created
    auto_dirs = [d for d in (worktree / "specs").iterdir() if d.is_dir() and d.name != "explicit-spec"]
    assert len(auto_dirs) == 0


def test_enter_spec_phase_and_is_spec_phase(tmp_path):
    """enter_spec_phase sets phase and spec_path on dictionaries and directories."""
    proj = tmp_path / "project"
    proj.mkdir()

    # Dictionary job record
    job_record = {
        "id": "20261027-1200-test-task",
        "worktree": str(proj),
        "authorization": {},
    }
    spec_path, updated = enter_spec_phase(job_record, title="Payment Integration")
    assert spec_path.is_dir()
    assert spec_path.name == "001-payment-integration"
    assert updated["phase"] == "spec"
    assert updated["authorization"]["phase"] == "spec"
    assert updated["authorization"]["spec_path"] == str(spec_path)
    assert is_spec_phase(updated) is True

    # Path directly
    path_res, state_dict = enter_spec_phase(proj, title="Second Feature")
    assert path_res.name == "002-second-feature"
    assert state_dict["phase"] == "spec"


def test_sdd_gate_slug_and_numbering(tmp_path):
    """next_spec_number and spec_slug handle edge cases gracefully."""
    specs_dir = tmp_path / "specs"
    assert next_spec_number(specs_dir) == 1

    specs_dir.mkdir()
    assert next_spec_number(specs_dir) == 1

    (specs_dir / "005-feature").mkdir()
    (specs_dir / "012-another").mkdir()
    (specs_dir / "random-dir").mkdir()
    assert next_spec_number(specs_dir) == 13

    assert spec_slug("") == "free-text-task"
    assert spec_slug("   ") == "free-text-task"
    assert spec_slug("Special Characters & Accents: Café!") == "special-characters-accents-cafe"


def test_blocks_code_dispatch_without_reviewed_spec_tasks(git_repo, fake_bin):
    """T029 — dispatch rejects non-docs pieces when spec tasks are absent or lack review by a distinct model."""
    from crew_chief.config import load_prefs
    from crew_chief.dispatch import dispatch
    from crew_chief.records import add_attempt, add_piece, mark_accepted, new_job, save_job
    from crew_chief.sdd_gate import enter_spec_phase
    from crew_chief.tools import Executor

    repo = git_repo
    fake_bin("stub", "from pathlib import Path\nPath('out.py').write_text('x=1\\n', encoding='utf-8')\n")
    tool = {"name": "stub", "kind": "generic", "bin": "stub", "run_cmd": ["stub", "{prompt}"], "models": ["m"]}
    executor = Executor(id="stub:m", tool=tool, model="m", cost="free", trains=False)

    prefs_file = repo / "docs" / "crew-chief" / "preferences.toml"
    prefs_file.parent.mkdir(parents=True, exist_ok=True)
    prefs_file.write_text("[team]\ncoordinators = ['claude:sonnet']\n", encoding="utf-8")
    prefs = load_prefs(repo)

    # 1. Negative case: Coordinator unknown in spec-backed job -> refused with actionable note
    job1 = new_job(repo, "Job 1", "task", False, "test")
    job1["project"] = str(repo)
    enter_spec_phase(job1, title="Spec One")
    piece1 = add_piece(job1, "code piece", "code", "low", ref="T001")
    # Coordinator is not set on job and not in env
    import os
    old_coord = os.environ.pop("CREW_CHIEF_COORDINATOR", None)
    try:
        save_job(repo, job1)
        res1 = dispatch(repo, job1["id"], piece1, executor, "write code", prefs)
        assert res1["status"] == "brief_refused", res1
        assert "coordinator" in res1["note"].lower()
    finally:
        if old_coord:
            os.environ["CREW_CHIEF_COORDINATOR"] = old_coord

    # 2. Negative case: Spec-backed job with coordinator set, but no tasks.md imported
    job2 = new_job(repo, "Job 2", "task", False, "test")
    job2["phase"] = "spec"
    job2["spec_dir"] = "specs/002-missing"
    job2["coordinator"] = "claude:sonnet"
    piece2 = add_piece(job2, "code piece", "code", "low", ref="T001")
    save_job(repo, job2)
    # specs directory does not exist or has no tasks.md
    res2 = dispatch(repo, job2["id"], piece2, executor, "write code", prefs)
    assert res2["status"] == "brief_refused", res2
    assert "spec tasks not imported" in res2["note"]

    # 3. Negative case: tasks.md exists, docs piece touching tasks.md exists, but NO review attempt
    job3 = new_job(repo, "Job 3", "task", False, "test")
    job3["project"] = str(repo)
    spec_dir, _ = enter_spec_phase(job3, title="Spec Three")
    job3["coordinator"] = "claude:sonnet"
    docs_p3 = add_piece(job3, "Review specs/001-spec-three/tasks.md", "docs", "low")
    code_p3 = add_piece(job3, "implement code", "code", "low", ref="T001")
    save_job(repo, job3)
    res3 = dispatch(repo, job3["id"], code_p3, executor, "write code", prefs)
    assert res3["status"] == "brief_refused", res3
    assert "not reviewed by distinct model" in res3["note"]

    # 4. Negative case: review only by coordinator's own model
    job4 = new_job(repo, "Job 4", "task", False, "test")
    job4["project"] = str(repo)
    enter_spec_phase(job4, title="Spec Four")
    job4["coordinator"] = "claude:sonnet"
    docs_p4 = add_piece(job4, "tasks.md review", "docs", "low")
    # Add review attempt by the same model as coordinator (claude:sonnet)
    att4 = {
        "n": 1,
        "executor": "claude:sonnet",
        "reported_model": "sonnet",
        "role": "review",
        "status": "ok",
        "duration_s": 1.0,
        "note": "looks good",
        "branch": "crew/job4-p1-a1",
    }
    add_attempt(job4, docs_p4, att4)
    mark_accepted(job4, docs_p4, att4)
    code_p4 = add_piece(job4, "code task without ref", "code", "low")
    save_job(repo, job4)
    res4 = dispatch(repo, job4["id"], code_p4, executor, "write code", prefs)
    assert res4["status"] == "brief_refused", res4
    assert "not reviewed by distinct model" in res4["note"]

    # 5. Negative case: review of an unrelated docs piece (touches README, not tasks.md)
    job5 = new_job(repo, "Job 5", "task", False, "test")
    job5["project"] = str(repo)
    enter_spec_phase(job5, title="Spec Five")
    job5["coordinator"] = "claude:sonnet"
    unrelated_docs = add_piece(job5, "Update README.md", "docs", "low")
    att5 = {
        "n": 1,
        "executor": "codex:o3",
        "reported_model": "o3",
        "role": "review",
        "status": "ok",
        "duration_s": 1.0,
        "branch": "crew/job5-p1-a1",
        "diffstat": [{"file": "README.md", "added": 1, "deleted": 0}],
    }
    add_attempt(job5, unrelated_docs, att5)
    mark_accepted(job5, unrelated_docs, att5)
    code_p5 = add_piece(job5, "code task", "code", "low", ref="T001")
    save_job(repo, job5)
    res5 = dispatch(repo, job5["id"], code_p5, executor, "write code", prefs)
    assert res5["status"] == "brief_refused", res5
    assert "not reviewed by distinct model" in res5["note"]

    # 6. Negative case: failed review of spec tasks docs piece
    job6 = new_job(repo, "Job 6", "task", False, "test")
    job6["project"] = str(repo)
    enter_spec_phase(job6, title="Spec Six")
    job6["coordinator"] = "claude:sonnet"
    docs_p6 = add_piece(job6, "tasks.md spec review", "docs", "low")
    att6 = {
        "n": 1,
        "executor": "codex:o3",
        "reported_model": "o3",
        "role": "review",
        "status": "failed",
        "duration_s": 1.0,
        "note": "review failed",
    }
    add_attempt(job6, docs_p6, att6)
    code_p6 = add_piece(job6, "code task", "code", "low", ref="T001")
    save_job(repo, job6)
    res6 = dispatch(repo, job6["id"], code_p6, executor, "write code", prefs)
    assert res6["status"] == "brief_refused", res6
    assert "not reviewed by distinct model" in res6["note"]

    # 7. Positive case: docs piece touching tasks.md accepted after OK review by distinct model
    job7 = new_job(repo, "Job 7", "task", False, "test")
    job7["project"] = str(repo)
    enter_spec_phase(job7, title="Spec Seven")
    job7["coordinator"] = "claude:sonnet"
    docs_p7 = add_piece(job7, "Spec breakdown tasks.md", "docs", "low")
    att7 = {
        "n": 1,
        "executor": "codex:o3",
        "reported_model": "o3",
        "role": "review",
        "status": "ok",
        "duration_s": 1.0,
        "branch": "crew/job7-p1-a1",
    }
    add_attempt(job7, docs_p7, att7)
    mark_accepted(job7, docs_p7, att7)
    code_p7 = add_piece(job7, "implement T001", "code", "low", ref="T001")
    save_job(repo, job7)
    res7 = dispatch(repo, job7["id"], code_p7, executor, "write code", prefs)
    assert res7["status"] == "ok", res7

    # 8. Docs pieces and review dispatches are allowed even before spec tasks reviewed
    job8 = new_job(repo, "Job 8", "task", False, "test")
    job8["project"] = str(repo)
    enter_spec_phase(job8, title="Spec Eight")
    job8["coordinator"] = "claude:sonnet"
    docs_p8 = add_piece(job8, "Draft tasks.md", "docs", "low")
    save_job(repo, job8)
    res8 = dispatch(repo, job8["id"], docs_p8, executor, "write docs", prefs)
    assert res8["status"] == "ok", res8

    # 9. Legacy job (no spec phase / not in spec phase) is not gated
    job9 = new_job(repo, "Legacy Job", "task", False, "test")
    job9["spec_dir"] = "specs/001-legacy"
    piece9 = add_piece(job9, "legacy code", "code", "low", ref="T012")
    save_job(repo, job9)
    res9 = dispatch(repo, job9["id"], piece9, executor, "write code", prefs)
    assert res9["status"] == "ok", res9



