"""Tests for ``install.py`` — the installer/updater of the crew-chief skill.

Every test runs the installer as a real process (``sys.executable`` on
``install.py``) so that the documented invocation is the one under test. The
isolated-mode test keeps discovery hermetic by pointing ``PATH`` at an empty
directory: no executor CLI is installed in CI, and none is spawned locally.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import install
from crew_chief.config import classify, load_prefs

INSTALL = Path(__file__).resolve().parents[1] / "install.py"
VENDOR = Path(__file__).resolve().parents[1] / "vendor" / "ponytail"

BEGIN = "<!-- BEGIN:agent-crew-chief -->"
END = "<!-- END:agent-crew-chief -->"


def _run(project: Path, *extra: str) -> subprocess.CompletedProcess:
    """Run the installer on ``project`` and capture its output."""
    return subprocess.run(
        [sys.executable, "-I", str(INSTALL), str(project), *extra],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    return project


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _cut_block(text: str) -> str:
    """Return ``text`` with the crew-chief marker block removed."""
    start = text.index(BEGIN)
    end = text.index(END, start) + len(END)
    return text[:start] + text[end:]


# --- first install ---------------------------------------------------------


def test_install_creates_everything_once(tmp_path):
    project = _project(tmp_path)

    result = _run(project)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip()

    for relative in (".claude/skills/crew-chief", ".agents/skills/crew-chief"):
        root = project / relative
        assert (root / "SKILL.md").is_file()
        assert (root / "scripts" / "crew-chief.py").is_file()
        assert (root / "templates" / "preferences.example.toml").is_file()

    agents = _read(project / "AGENTS.md")
    assert agents.count(BEGIN) == 1
    assert agents.count(END) == 1
    assert "crew-chief" in agents

    claude = _read(project / "CLAUDE.md")
    assert claude.count(BEGIN) == 1

    assert (project / "docs" / "crew-chief" / "preferences.toml").is_file()
    assert (project / "docs" / "crew-chief" / "jobs" / ".gitkeep").is_file()

    assert not (project / "GEMINI.md").exists()
    assert not (project / ".cursor").exists()


def test_agents_md_snippet_goes_after_foreign_managed_block(tmp_path):
    project = _project(tmp_path)
    foreign = (
        "<!-- BEGIN:nextjs-agent-rules -->\n"
        "# Next.js rules\n"
        "<!-- END:nextjs-agent-rules -->\n"
        "\n"
        "# My agents\n"
        "\n"
        "Keep it tidy.\n"
    )
    (project / "AGENTS.md").write_text(foreign, encoding="utf-8")

    assert _run(project).returncode == 0

    text = _read(project / "AGENTS.md")
    assert text.count(BEGIN) == 1
    assert text.startswith("<!-- BEGIN:nextjs-agent-rules -->")
    assert text.index("<!-- END:nextjs-agent-rules -->") < text.index(BEGIN)
    assert "# My agents" in text


def test_install_leaves_other_skills_and_data_alone(tmp_path):
    project = _project(tmp_path)
    other = project / ".claude" / "skills" / "other"
    other.mkdir(parents=True)
    (other / "SKILL.md").write_text("# other skill\n", encoding="utf-8")
    scoreboard = project / "docs" / "crew-chief" / "scoreboard.md"
    scoreboard.parent.mkdir(parents=True)
    scoreboard.write_text("# Scoreboard\n\nkeep me\n", encoding="utf-8")

    assert _run(project).returncode == 0

    assert _read(other / "SKILL.md") == "# other skill\n"
    assert _read(scoreboard) == "# Scoreboard\n\nkeep me\n"


# --- reinstall -------------------------------------------------------------


def test_reinstall_keeps_prefs_and_jobs_and_replaces_snippet(tmp_path):
    project = _project(tmp_path)
    assert _run(project).returncode == 0

    prefs = project / "docs" / "crew-chief" / "preferences.toml"
    prefs.write_text(_read(prefs) + "\n# my edit\n", encoding="utf-8")
    job = project / "docs" / "crew-chief" / "jobs" / "20260101-0000-my-job.json"
    job.write_text("{}\n", encoding="utf-8")

    agents = project / "AGENTS.md"
    text = _read(agents)
    start = text.index(BEGIN)
    end = text.index(END) + len(END)
    agents.write_text(
        text[:start] + BEGIN + "\nstale body\n" + END + text[end:], encoding="utf-8"
    )

    result = _run(project)

    assert result.returncode == 0, result.stderr
    assert _read(prefs).endswith("# my edit\n")
    assert _read(job) == "{}\n"

    rewritten = _read(agents)
    assert rewritten.count(BEGIN) == 1
    assert "stale body" not in rewritten


def test_claude_md_with_only_agents_import_untouched(tmp_path):
    project = _project(tmp_path)
    claude = project / "CLAUDE.md"
    claude.write_text("@AGENTS.md\n", encoding="utf-8")

    assert _run(project).returncode == 0

    assert _read(claude) == "@AGENTS.md\n"
    assert BEGIN in _read(project / "AGENTS.md")


def test_force_snippet_reinserts_after_markers_removed(tmp_path):
    project = _project(tmp_path)
    assert _run(project).returncode == 0

    agents = project / "AGENTS.md"
    agents.write_text(
        "# Agents\n\n" + _cut_block(_read(agents)), encoding="utf-8"
    )
    assert BEGIN not in _read(agents)

    assert _run(project).returncode == 0
    assert BEGIN not in _read(agents)

    assert _run(project, "--force-snippet").returncode == 0
    assert BEGIN in _read(agents)


def test_gemini_and_cursor_only_when_present(tmp_path):
    bare = _project(tmp_path)
    assert _run(bare).returncode == 0
    assert not (bare / "GEMINI.md").exists()
    assert not (bare / ".cursor").exists()

    project = tmp_path / "with-files"
    project.mkdir()
    (project / "GEMINI.md").write_text("# Gemini\n", encoding="utf-8")
    (project / ".cursor" / "rules").mkdir(parents=True)

    assert _run(project).returncode == 0

    gemini = _read(project / "GEMINI.md")
    assert "# Gemini" in gemini
    assert gemini.count(BEGIN) == 1

    rule = _read(project / ".cursor" / "rules" / "agent-crew-chief.mdc")
    assert "alwaysApply: true" in rule
    assert "description:" in rule
    assert rule.count(BEGIN) == 1


# --- the vendored Ponytail -------------------------------------------------


def test_vendor_ponytail_is_pinned_and_licensed():
    assert "MIT" in _read(VENDOR / "LICENSE")
    assert _read(VENDOR / "VERSION").splitlines()[0] == "v5.3.2"
    for skill in install.PONYTAIL_SKILLS:
        assert (VENDOR / "skills" / skill / "SKILL.md").is_file()


def test_install_adds_ponytail_with_marker(tmp_path):
    project = _project(tmp_path)

    result = _run(project)

    assert result.returncode == 0, result.stderr
    for root in (".claude/skills", ".agents/skills"):
        for skill in install.PONYTAIL_SKILLS:
            destination = project / root / skill
            assert (destination / "SKILL.md").is_file()
            assert (destination / install.PONYTAIL_MARKER).is_file()
    assert not (project / ".claude" / "skills" / "crew-chief" / "vendor").exists()


def test_install_updates_own_ponytail(tmp_path):
    project = _project(tmp_path)
    assert _run(project).returncode == 0

    installed = project / ".claude" / "skills" / "ponytail" / "SKILL.md"
    installed.write_text("stale\n", encoding="utf-8")

    assert _run(project).returncode == 0

    assert _read(installed) == _read(VENDOR / "skills" / "ponytail" / "SKILL.md")


def test_install_leaves_foreign_ponytail_alone(tmp_path):
    project = _project(tmp_path)
    mine = project / ".claude" / "skills" / "ponytail" / "SKILL.md"
    mine.parent.mkdir(parents=True)
    mine.write_text("mine", encoding="utf-8")

    result = _run(project)

    assert result.returncode == 0, result.stderr
    assert _read(mine) == "mine"
    left_alone = [line for line in result.stdout.splitlines() if "left alone" in line]
    assert any("ponytail" in line for line in left_alone)
    assert (project / ".agents" / "skills" / "ponytail" / "SKILL.md").is_file()


def test_no_ponytail_skips_and_keeps_existing(tmp_path):
    project = _project(tmp_path)

    assert _run(project, "--no-ponytail").returncode == 0
    assert not (project / ".claude" / "skills" / "ponytail").exists()
    assert not (project / ".agents" / "skills" / "ponytail").exists()

    assert _run(project).returncode == 0
    assert _run(project, "--no-ponytail").returncode == 0

    assert (project / ".claude" / "skills" / "ponytail" / "SKILL.md").is_file()
    assert (project / ".agents" / "skills" / "ponytail" / "SKILL.md").is_file()


# --- the installed preferences template ------------------------------------


def test_installed_template_does_not_shadow_global_scalars(tmp_path, monkeypatch):
    project = _project(tmp_path)
    assert _run(project).returncode == 0

    global_file = tmp_path / "global.toml"
    global_file.write_text(
        'review_default = "x:y"\nstrong_order = ["a"]\n', encoding="utf-8"
    )
    monkeypatch.setenv("AGENT_CREW_CHIEF_GLOBAL_PREFS", str(global_file))

    prefs = load_prefs(project)

    assert prefs.review_default == "x:y"
    assert prefs.strong_order == ["a"]


def test_installed_template_preview_free_still_trains(tmp_path):
    project = _project(tmp_path)
    assert _run(project).returncode == 0

    prefs = load_prefs(project)

    assert classify("opencode:opencode-go/foo-preview-free", prefs) == ("free", True)


# --- the installed CLI -----------------------------------------------------


def test_installed_cli_runs_under_isolated_mode(tmp_path):
    project = _project(tmp_path)
    assert _run(project).returncode == 0

    empty = tmp_path / "empty-path"
    empty.mkdir()
    env = dict(os.environ)
    env["PATH"] = str(empty)

    cli = (
        project / ".agents" / "skills" / "crew-chief" / "scripts" / "crew-chief.py"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            str(cli),
            "discover",
            "--json",
            "--project",
            str(project),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert isinstance(payload["executors"], list)


# --- errors ----------------------------------------------------------------


def test_missing_project_exits_2_with_one_line(tmp_path):
    missing = tmp_path / "nope"

    result = _run(missing)

    assert result.returncode == 2
    assert len(result.stderr.strip().splitlines()) == 1
    assert "nope" in result.stderr


# --- snippet ---------------------------------------------------------------


def test_installed_snippet_declares_precedence_with_superpowers(tmp_path):
    project = _project(tmp_path)

    result = _run(project)

    assert result.returncode == 0, result.stderr
    text = _read(project / "AGENTS.md")
    assert (
        "If the superpowers skills are also installed, use them to discuss and "
        "design; use crew-chief to execute the plan."
    ) in text
