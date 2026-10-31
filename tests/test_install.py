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


# --- vendored skill kits ---------------------------------------------------

sys.path.insert(0, str(INSTALL.parent))
import install  # noqa: E402

ROOTS = (".claude/skills", ".agents/skills", ".gemini/skills")
KIT_SKILLS = [(kit, name) for kit, names in install.KITS.items() for name in names]


def test_vendor_kits_are_pinned_and_licensed():
    for kit, name in KIT_SKILLS:
        assert (install.VENDOR / kit / "LICENSE").is_file()
        assert (install.VENDOR / kit / "VERSION").is_file()
        assert (install.VENDOR / kit / "skills" / name / "SKILL.md").is_file()


def test_install_adds_every_kit_skill_with_marker(tmp_path):
    project = _project(tmp_path)

    result = _run(project)

    assert result.returncode == 0, result.stderr
    for root in ROOTS:
        for _, name in KIT_SKILLS:
            destination = project / root / name
            assert (destination / "SKILL.md").is_file()
            assert (destination / install.KIT_MARKER).is_file()


def test_reinstall_updates_own_kit_copy_and_keeps_marker(tmp_path):
    project = _project(tmp_path)
    assert _run(project).returncode == 0

    skill = project / ".claude" / "skills" / "systematic-debugging"
    (skill / "SKILL.md").write_text("stale\n", encoding="utf-8")
    (skill / "extra.md").write_text("old\n", encoding="utf-8")

    assert _run(project).returncode == 0

    source = install.VENDOR / "superpowers" / "skills" / "systematic-debugging"
    assert _read(skill / "SKILL.md") == _read(source / "SKILL.md")
    assert not (skill / "extra.md").exists()
    assert (skill / install.KIT_MARKER).is_file()


def test_install_leaves_foreign_kit_copy_alone(tmp_path):
    project = _project(tmp_path)
    mine = project / ".claude" / "skills" / "ponytail" / "SKILL.md"
    mine.parent.mkdir(parents=True)
    mine.write_text("mine", encoding="utf-8")

    result = _run(project)

    assert result.returncode == 0, result.stderr
    assert _read(mine) == "mine"
    assert not (mine.parent / install.KIT_MARKER).exists()
    assert any(
        ".claude/skills/ponytail" in line and "left alone" in line
        for line in result.stdout.splitlines()
    )
    assert (project / ".agents" / "skills" / "ponytail" / "SKILL.md").is_file()


def test_no_kit_flags_skip_and_keep_existing(tmp_path):
    for kit in install.KITS:
        project = tmp_path / kit
        project.mkdir()

        assert _run(project, f"--no-{kit}").returncode == 0
        for root in ROOTS:
            for name in install.KITS[kit]:
                assert not (project / root / name).exists()
            for other, name in KIT_SKILLS:
                if other != kit:
                    assert (project / root / name / "SKILL.md").is_file()

        assert _run(project).returncode == 0
        skill = project / ".claude" / "skills" / install.KITS[kit][0] / "SKILL.md"
        skill.write_text("kept\n", encoding="utf-8")
        assert _run(project, f"--no-{kit}").returncode == 0
        assert _read(skill) == "kept\n"


def test_missing_kit_source_exits_2_only_when_wanted(tmp_path, monkeypatch, capsys):
    project = _project(tmp_path)
    monkeypatch.setattr(install, "VENDOR", tmp_path / "broken-vendor")

    for kit in install.KITS:
        others = [f"--no-{other}" for other in install.KITS if other != kit]
        assert install.main([str(project), *others]) == 2
        stderr = capsys.readouterr().err.strip().splitlines()
        assert len(stderr) == 1
        assert "broken-vendor" in stderr[0]

    flags = [f"--no-{kit}" for kit in install.KITS]
    assert install.main([str(project), *flags]) == 0


def test_one_missing_kit_skill_stops_the_install(tmp_path, monkeypatch, capsys):
    import shutil

    vendor = tmp_path / "vendor"
    shutil.copytree(install.VENDOR, vendor)
    kit = next(iter(install.KITS))
    shutil.rmtree(vendor / kit / "skills" / install.KITS[kit][0])
    monkeypatch.setattr(install, "VENDOR", vendor)

    assert install.main([str(_project(tmp_path))]) == 2
    assert install.KITS[kit][0] in capsys.readouterr().err


# --- snippet ---------------------------------------------------------------


def test_installed_snippet_points_at_the_skill(tmp_path):
    project = _project(tmp_path)

    result = _run(project)

    assert result.returncode == 0, result.stderr
    text = _read(project / "AGENTS.md")
    assert ".agents/skills/crew-chief/SKILL.md" in text


