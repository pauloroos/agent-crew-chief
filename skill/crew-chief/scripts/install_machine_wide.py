#!/usr/bin/env python3
"""Install the crew-chief skill machine-wide across all supported CLI paths.

Usage::

    python install_machine_wide.py [--dry-run] [--home DIR] [--no-superpowers]
        [--no-ponytail]

Installs to:
* ``~/.claude/skills/crew-chief/`` (Claude Code)
* ``~/.claude-ghost/`` link (Claude ghost routing)
* ``~/.agents/skills/crew-chief/`` (Codex and OpenCode)
* ``~/.gemini/skills/crew-chief/`` plus ``~/GEMINI.md`` reference (Gemini CLI)

It also copies the vendored superpowers and Ponytail skills into
``~/.claude/skills/``, ``~/.agents/skills/`` and ``~/.gemini/skills/`` with
``install.py``'s rule: each copy carries ``.installed-by-agent-crew-chief`` and
a folder without that marker is left alone. ``--no-superpowers`` and
``--no-ponytail`` skip a kit.

The install is idempotent: running it again updates existing copies without
duplicating files. ``--dry-run`` reports what would be installed without
writing anything.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

#: Where this repository keeps the skill source.
REPO_ROOT = Path(__file__).resolve().parents[3]
SKILL_SOURCE = REPO_ROOT / "skill" / "crew-chief"

sys.path.insert(0, str(REPO_ROOT))
import install as repo_install  # noqa: E402  (the kit copy lives in the repo's install.py)

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


def install_machine_wide(
    home: Path, dry_run: bool = False, kits: list[str] | None = None
) -> list[str]:
    """Install the skill and the kits to all CLI paths and return report lines."""
    kits = list(repo_install.KITS) if kits is None else kits
    report: list[str] = []

    for relative_path in CLI_PATHS:
        destination = home / relative_path
        cli_name = relative_path.parts[0]  # .claude, .agents, .gemini

        if dry_run:
            report.append(f"would install crew-chief to ~/{relative_path}")
        else:
            repo_install._sync_tree(SKILL_SOURCE, destination)
            report.append(f"installed crew-chief to ~/{relative_path}")

    if dry_run:
        for kit in kits:
            report.extend(
                f"would install {name} to ~/{(root / name).as_posix()}"
                for name in repo_install.KITS[kit]
                for root in repo_install.SKILL_ROOTS
            )
    else:
        report.extend(repo_install._install_kits(home, kits))
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
    for kit in repo_install.KITS:
        parser.add_argument(
            f"--no-{kit}", action="store_true", help=f"do not install the {kit} skills"
        )
    args = parser.parse_args(argv)

    home = Path(args.home).expanduser() if args.home else Path.home()

    if not SKILL_SOURCE.is_dir():
        print(f"install_machine_wide.py: skill source not found at {SKILL_SOURCE}", file=sys.stderr)
        return 2
    kits = [kit for kit in repo_install.KITS if not getattr(args, f"no_{kit}")]
    missing = repo_install._missing_kit(kits)
    if missing:
        print(f"install_machine_wide.py: kit source not found at {missing}", file=sys.stderr)
        return 2

    report = install_machine_wide(home, args.dry_run, kits)

    for line in report:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())