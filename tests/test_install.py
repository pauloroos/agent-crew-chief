"""Tests for ``install.py`` — the installer/updater of the crew-chief skill.

Every test runs the installer as a real process (``sys.executable`` on
``install.py``) so that the documented invocation is the one under test.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

INSTALL = Path(__file__).resolve().parents[1] / "install.py"

BEGIN = "<!-- BEGIN:agent-crew-chief -->"
END = "<!-- END:agent-crew-chief -->"


def _run(project: Path, *extra: str) -> subprocess.CompletedProcess:
    """Run the installer on ``project`` and capture its output."""
    args = list(extra)
    return subprocess.run(
        [sys.executable, "-I", str(INSTALL), str(project), *args],
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
        assert (root / "templates" / "snippet.md").is_file()

    agents = _read(project / "AGENTS.md")
    assert agents.count(BEGIN) == 1
    assert agents.count(END) == 1
    assert "crew-chief" in agents

    claude = _read(project / "CLAUDE.md")
    assert claude.count(BEGIN) == 1

    assert not (project / "docs").exists()

    assert not (project / "GEMINI.md").exists()
    assert not (project / ".cursor").exists()


def test_deploys_to_all_four_cli_paths(tmp_path):
    project = _project(tmp_path)
    (project / "GEMINI.md").write_text("# Gemini\n", encoding="utf-8")

    result = _run(project)

    assert result.returncode == 0, result.stderr

    for relative in (
        ".claude/skills/crew-chief",
        ".agents/skills/crew-chief",
        ".gemini/skills/crew-chief",
    ):
        root = project / relative
        assert (root / "SKILL.md").is_file()
        assert (root / "scripts" / "crew-chief.py").is_file()
        assert (root / "templates" / "snippet.md").is_file()

    gemini = _read(project / "GEMINI.md")
    assert gemini.count(BEGIN) == 1
    assert gemini.count(END) == 1
    assert "crew-chief" in gemini

    agents = _read(project / "AGENTS.md")
    assert agents.count(BEGIN) == 1
    assert agents.count(END) == 1
    assert "crew-chief" in agents

    claude = _read(project / "CLAUDE.md")
    assert claude.count(BEGIN) == 1
    assert claude.count(END) == 1
    assert "crew-chief" in claude


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


def test_reinstall_replaces_the_snippet_and_stale_skill_files(tmp_path):
    project = _project(tmp_path)
    assert _run(project).returncode == 0

    skill = project / ".agents" / "skills" / "crew-chief"
    stale = skill / "scripts" / "crew_chief" / "dispatch.py"
    stale.write_text("old\n", encoding="utf-8")
    crlf = skill / "SKILL.md"
    crlf.write_bytes(crlf.read_bytes().replace(b"\n", b"\r\n"))

    agents = project / "AGENTS.md"
    text = _read(agents)
    start = text.index(BEGIN)
    end = text.index(END) + len(END)
    agents.write_text(
        text[:start] + BEGIN + "\nstale body\n" + END + text[end:], encoding="utf-8"
    )

    result = _run(project)

    assert result.returncode == 0, result.stderr
    assert not stale.exists()
    assert b"\r\n" in crlf.read_bytes()  # a CRLF checkout of the same text is left alone

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
            "score",
            "--json",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == []


# --- errors ----------------------------------------------------------------


def test_missing_project_exits_2_with_one_line(tmp_path):
    missing = tmp_path / "nope"

    result = _run(missing)

    assert result.returncode == 2
    assert len(result.stderr.strip().splitlines()) == 1
    assert "nope" in result.stderr


# --- snippet ---------------------------------------------------------------


def test_installed_snippet_points_at_the_skill(tmp_path):
    project = _project(tmp_path)

    result = _run(project)

    assert result.returncode == 0, result.stderr
    text = _read(project / "AGENTS.md")
    assert ".agents/skills/crew-chief/SKILL.md" in text


