"""Tests for ``install_machine_wide.py`` — machine-wide skill deployment.

Every test runs the installer as a real process so that the documented
invocation is the one under test.  The four CLI paths are:

* ``~/.claude/skills/crew-chief/`` (Claude Code)
* ``~/.claude-ghost/`` link (Claude ghost routing)
* ``~/.agents/skills/crew-chief/`` (Codex and OpenCode)
* Gemini CLI discovery path plus ``GEMINI.md`` reference
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

INSTALL_MW = Path(__file__).resolve().parents[1] / "skill" / "crew-chief" / "scripts" / "install_machine_wide.py"


def _run(*extra: str, env_override: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Run the machine-wide installer and capture its output."""
    env = os.environ.copy()
    if env_override:
        env.update(env_override)
    args = list(extra)
    return subprocess.run(
        [sys.executable, "-I", str(INSTALL_MW), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def test_installs_to_all_four_cli_paths(tmp_path):
    """Machine-wide install deploys to all four CLI paths idempotently."""
    home = tmp_path / "home"
    home.mkdir()

    result = _run("--dry-run", "--home", str(home))

    assert result.returncode == 0, result.stderr
    # Dry-run reports what would be installed without writing
    assert "claude" in result.stdout.lower() or ".claude" in result.stdout
    assert "agents" in result.stdout.lower() or ".agents" in result.stdout
    assert "gemini" in result.stdout.lower() or "GEMINI" in result.stdout

    # Now do the real install
    result = _run("--home", str(home))

    assert result.returncode == 0, result.stderr

    # Check all four paths
    claude_skill = home / ".claude" / "skills" / "crew-chief"
    agents_skill = home / ".agents" / "skills" / "crew-chief"
    gemini_skill = home / ".gemini" / "skills" / "crew-chief"

    assert (claude_skill / "SKILL.md").is_file(), f"Claude skill not found at {claude_skill}"
    assert (claude_skill / "scripts" / "crew-chief.py").is_file()
    assert (agents_skill / "SKILL.md").is_file(), f"Agents skill not found at {agents_skill}"
    assert (agents_skill / "scripts" / "crew-chief.py").is_file()
    assert (gemini_skill / "SKILL.md").is_file(), f"Gemini skill not found at {gemini_skill}"
    assert (gemini_skill / "scripts" / "crew-chief.py").is_file()

    # Check GEMINI.md reference exists
    gemini_md = home / "GEMINI.md"
    assert gemini_md.is_file(), f"GEMINI.md not found at {gemini_md}"
    assert "crew-chief" in _read(gemini_md)

    # Idempotent second run
    result2 = _run("--home", str(home))
    assert result2.returncode == 0, result2.stderr

# --- vendored skill kits ---------------------------------------------------

import install_machine_wide  # noqa: E402

KITS = install_machine_wide.repo_install.KITS
MARKER = install_machine_wide.repo_install.KIT_MARKER
ROOTS = (".claude/skills", ".agents/skills", ".gemini/skills")


def test_installs_kits_with_marker_and_leaves_foreign_copy(tmp_path):
    home = tmp_path / "home"
    foreign = home / ".agents" / "skills" / "test-driven-development" / "SKILL.md"
    foreign.parent.mkdir(parents=True)
    foreign.write_text("mine", encoding="utf-8")

    result = _run("--home", str(home))

    assert result.returncode == 0, result.stderr
    for root in ROOTS:
        for names in KITS.values():
            for name in names:
                destination = home / root / name
                if destination == foreign.parent:
                    continue
                assert (destination / "SKILL.md").is_file()
                assert (destination / MARKER).is_file()
    assert _read(foreign) == "mine"
    assert not (foreign.parent / MARKER).exists()
    assert "left alone" in result.stdout


def test_no_kit_flags_skip_each_kit(tmp_path):
    for kit, names in KITS.items():
        home = tmp_path / kit

        assert _run("--home", str(home), f"--no-{kit}").returncode == 0

        for root in ROOTS:
            for name in names:
                assert not (home / root / name).exists()


def test_missing_kit_source_exits_2_only_when_wanted(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        install_machine_wide.repo_install, "VENDOR", tmp_path / "broken-vendor"
    )
    home = str(tmp_path / "home")

    assert install_machine_wide.main(["--home", home, "--dry-run"]) == 2
    stderr = capsys.readouterr().err.strip().splitlines()
    assert len(stderr) == 1
    assert "broken-vendor" in stderr[0]

    flags = [f"--no-{kit}" for kit in KITS]
    assert install_machine_wide.main(["--home", home, "--dry-run", *flags]) == 0
