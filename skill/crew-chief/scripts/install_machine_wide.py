#!/usr/bin/env python3
"""Install the crew-chief skill machine-wide across all supported CLI paths.

Usage::

    python install_machine_wide.py [--dry-run] [--home DIR]

Installs to:
* ``~/.claude/skills/crew-chief/`` (Claude Code)
* ``~/.claude-ghost/`` link (Claude ghost routing)
* ``~/.agents/skills/crew-chief/`` (Codex and OpenCode)
* ``~/.gemini/skills/crew-chief/`` plus ``~/GEMINI.md`` reference (Gemini CLI)

The install is idempotent: running it again updates existing copies without
duplicating files. ``--dry-run`` reports what would be installed without
writing anything.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

#: Where this repository keeps the skill source.
REPO_ROOT = Path(__file__).resolve().parents[3]
SKILL_SOURCE = REPO_ROOT / "skill" / "crew-chief"

#: The four CLI paths relative to HOME.
CLI_PATHS = [
    Path(".claude") / "skills" / "crew-chief",
    Path(".agents") / "skills" / "crew-chief",
    Path(".gemini") / "skills" / "crew-chief",
]

#: GEMINI.md marker block.
MARKER_BEGIN = "<!-- BEGIN:agent-crew-chief -->"
MARKER_END = "<!-- END:agent-crew-chief -->"

GEMINI_SNIPPET = f"""{MARKER_BEGIN}
# crew-chief skill

This project uses the crew-chief coordinator skill installed at:
`~/.gemini/skills/crew-chief/`

To load ACC in any repo, type: "load ACC, do X"

{MARKER_END}
"""


def _copy_skill(source: Path, destination: Path) -> None:
    """Replace destination with a fresh copy of the skill directory."""
    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


def _write_gemini_md(home: Path) -> None:
    """Write or update GEMINI.md with the crew-chief snippet."""
    gemini_md = home / "GEMINI.md"
    if gemini_md.is_file():
        text = gemini_md.read_text(encoding="utf-8")
        if MARKER_BEGIN in text and MARKER_END in text:
            start = text.index(MARKER_BEGIN)
            end = text.index(MARKER_END, start) + len(MARKER_END)
            new_text = text[:start] + GEMINI_SNIPPET.strip() + text[end:]
            gemini_md.write_text(new_text, encoding="utf-8")
        else:
            gemini_md.write_text(text.rstrip("\n") + "\n\n" + GEMINI_SNIPPET, encoding="utf-8")
    else:
        gemini_md.write_text(GEMINI_SNIPPET, encoding="utf-8")


def _create_ghost_link(home: Path) -> None:
    """Create ~/.claude-ghost symlink pointing to ~/.claude/skills/crew-chief."""
    ghost = home / ".claude-ghost"
    target = home / ".claude" / "skills" / "crew-chief"
    if ghost.exists() or ghost.is_symlink():
        ghost.unlink()
    try:
        ghost.symlink_to(target, target_is_directory=True)
    except OSError:
        if sys.platform != "win32":
            raise
        # Symlinks need Developer Mode or admin on Windows; a junction does not.
        import _winapi

        _winapi.CreateJunction(str(target), str(ghost))


def install_machine_wide(home: Path, dry_run: bool = False) -> list[str]:
    """Install the skill to all four CLI paths and return report lines."""
    report: list[str] = []

    for relative_path in CLI_PATHS:
        destination = home / relative_path
        cli_name = relative_path.parts[0]  # .claude, .agents, .gemini

        if dry_run:
            report.append(f"would install crew-chief to ~/{relative_path}")
        else:
            _copy_skill(SKILL_SOURCE, destination)
            report.append(f"installed crew-chief to ~/{relative_path}")

    if not dry_run:
        _create_ghost_link(home)
        report.append("created ~/.claude-ghost link")
        _write_gemini_md(home)
        report.append("updated ~/GEMINI.md")

    return report


def main(argv: list[str] | None = None) -> int:
    """Run the installer and return the exit code."""
    parser = argparse.ArgumentParser(
        prog="install_machine_wide.py",
        description="Install the crew-chief skill machine-wide across all supported CLI paths.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be installed without writing anything",
    )
    parser.add_argument(
        "--home",
        default=None,
        help="home directory to install into (default: current user's home)",
    )
    parser.add_argument(
        "--no-superpowers",
        action="store_true",
        help="do not install superpowers skills",
    )
    parser.add_argument(
        "--no-ponytail",
        action="store_true",
        help="do not install the vendored Ponytail skills",
    )
    args = parser.parse_args(argv)

    home = Path(args.home).expanduser() if args.home else Path.home()

    if not SKILL_SOURCE.is_dir():
        print(f"install_machine_wide.py: skill source not found at {SKILL_SOURCE}", file=sys.stderr)
        return 2

    report = install_machine_wide(home, args.dry_run)

    # Install kit components (superpowers, ponytail) if not opted out
    scripts_dir = REPO_ROOT / "skill" / "crew-chief" / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    from crew_chief.kit_install import install_kit
    kit_report = install_kit(
        project_dir=None,  # machine-wide mode
        do_install_superpowers=not args.no_superpowers,
        do_install_ponytail=not args.no_ponytail,
        dry_run=args.dry_run,
        home=home,
    )
    report.extend(kit_report)

    for line in report:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())