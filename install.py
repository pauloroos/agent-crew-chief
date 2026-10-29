#!/usr/bin/env python3
"""Install or update the ``crew-chief`` skill in a project.

Usage::

    python install.py <project> [--force-snippet] [--no-ponytail]
        [--with-rtk] [--no-superpowers]
    python install.py --command [--bin-dir DIR]

What it does:

1. Copies ``skill/crew-chief/`` into ``<project>/.claude/skills/crew-chief/``,
   ``<project>/.agents/skills/crew-chief/`` and
   ``<project>/.gemini/skills/crew-chief/``, replacing earlier copies.
2. Inserts or replaces the snippet (``templates/snippet.md``) between
   ``<!-- BEGIN:agent-crew-chief -->`` and ``<!-- END:agent-crew-chief -->``:
   in ``AGENTS.md`` (created if missing), in ``CLAUDE.md`` (left alone when it
   only contains ``@AGENTS.md``), in ``GEMINI.md`` only if that file exists and
   in ``.cursor/rules/agent-crew-chief.mdc`` only if ``.cursor/`` exists (with
   YAML frontmatter ``alwaysApply: true``). In ``AGENTS.md``, a file that
   starts with another tool's managed block gets the snippet right after that
   block's END line; otherwise the snippet is appended at the end.
3. Creates ``docs/crew-chief/preferences.toml`` from the example only if it is
   missing, plus ``docs/crew-chief/jobs/.gitkeep``.
4. Never touches an existing ``preferences.toml``, ``jobs/``,
   ``scoreboard.md`` or ``models-seen.json``, and never edits other skills,
   including a Ponytail it did not install.
5. Copies the vendored Ponytail skills (``vendor/ponytail/skills/``) into the
   same two roots, each marked with ``.installed-by-agent-crew-chief``; a
   Ponytail without that marker is left alone. ``--no-ponytail`` skips the
   step.
6. With ``--with-rtk``, downloads rtk-oss (Apache-2.0 v1.1.0) if not already
   on PATH and verifies SHA-256 checksums against the manifest and the
   release checksums.txt. Without it nothing is downloaded.
7. Installs vendored superpowers skills (test-driven-development,
   systematic-debugging, verification-before-completion). ``--no-superpowers``
   skips this step.

``--command`` installs only the user-scoped ``acc`` wrapper for this released
bundle (Windows ``acc.cmd``, Linux/macOS executable ``acc``) into
``--bin-dir`` or the platform's user bin directory, without a project. It
never creates project files, never edits global preferences and never changes
``PATH``: it prints where the wrapper went and leaves ``PATH`` to the owner.
Combining ``--command`` with a project installs both.

A file without markers is left alone once the skill is installed in the project
(``.claude/skills/crew-chief``, ``.agents/skills/crew-chief`` or
``.gemini/skills/crew-chief`` already exists): the markers were removed on
purpose. ``--force-snippet`` inserts the snippet anyway.

It prints every change and exits ``2`` with one line when ``<project>`` is not
an existing directory. Standard library only, so ``python -I install.py`` works.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

#: Markers that delimit the managed snippet.
MARKER_BEGIN = "<!-- BEGIN:agent-crew-chief -->"
MARKER_END = "<!-- END:agent-crew-chief -->"

#: Where this repository keeps the skill and its templates.
REPO_ROOT = Path(__file__).resolve().parent
SKILL_SOURCE = REPO_ROOT / "skill" / "crew-chief"
SNIPPET_SOURCE = SKILL_SOURCE / "templates" / "snippet.md"
PREFS_SOURCE = SKILL_SOURCE / "templates" / "preferences.example.toml"

#: Name of the installed skill directory and the roots it is copied into.
SKILL_NAME = "crew-chief"
SKILL_ROOTS = (
    Path(".claude") / "skills",
    Path(".agents") / "skills",
    Path(".gemini") / "skills",
)

#: The vendored Ponytail skills and the marker put in every copy we install.
PONYTAIL_SOURCE = REPO_ROOT / "vendor" / "ponytail" / "skills"
PONYTAIL_SKILLS = ("ponytail", "ponytail-review")
PONYTAIL_MARKER = ".installed-by-agent-crew-chief"

#: Project-relative paths the installer creates (never overwrites).
PREFS_RELATIVE = Path("docs") / "crew-chief" / "preferences.toml"
JOBS_GITKEEP_RELATIVE = Path("docs") / "crew-chief" / "jobs" / ".gitkeep"
CURSOR_RULE_RELATIVE = Path(".cursor") / "rules" / "agent-crew-chief.mdc"

#: Frontmatter written on top of the Cursor rule.
CURSOR_FRONTMATTER = (
    "---\n"
    "description: agent-crew-chief - the coordinator splits non-trivial tasks and dispatches them to the project's executor CLIs.\n"
    "alwaysApply: true\n"
    "---\n\n"
)

_COPY_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc")


# --- file helpers ----------------------------------------------------------


def _read(path: Path) -> str:
    """The UTF-8 text of ``path``, or ``""`` when it does not exist."""
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def _write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` as UTF-8 with LF line endings."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _copy_skill(destination: Path) -> None:
    """Replace ``destination`` with a fresh copy of the skill directory."""
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(SKILL_SOURCE, destination, ignore=_COPY_IGNORE)


def _block(snippet: str) -> str:
    """The snippet wrapped in the managed BEGIN/END markers."""
    return f"{MARKER_BEGIN}\n{snippet}\n{MARKER_END}\n"


# --- snippet merging -------------------------------------------------------


def _after_foreign_block(text: str) -> int | None:
    """Index just after the END line of a managed block at the top, else None."""
    if not text.startswith("<!-- BEGIN:"):
        return None
    end = text.find("<!-- END:")
    if end == -1:
        return None
    newline = text.find("\n", end)
    return len(text) if newline == -1 else newline + 1


def _insert(text: str, block: str) -> str:
    """Append ``block``, or place it right after a foreign block at the top."""
    position = _after_foreign_block(text)
    if position is None:
        return text.rstrip("\n") + "\n\n" + block
    head = text[:position].rstrip("\n")
    tail = text[position:].lstrip("\n")
    merged = head + "\n\n" + block
    return f"{merged}\n{tail}" if tail else merged


def _merge(text: str, block: str, installed: bool, force: bool) -> tuple[str, str]:
    """Insert, replace or skip the marked block in ``text``.

    Returns ``(new_text, action)``; the action is ``created`` (the file had no
    content), ``inserted``, ``updated``, ``unchanged`` or ``skipped`` (the
    markers were removed on purpose and ``force`` is off).
    """
    if MARKER_BEGIN in text and MARKER_END in text:
        start = text.index(MARKER_BEGIN)
        end = text.index(MARKER_END, start) + len(MARKER_END)
        new_text = text[:start] + block.rstrip("\n") + text[end:]
        return new_text, ("updated" if new_text != text else "unchanged")
    if not text.strip():
        return block, "created"
    if installed and not force:
        return text, "skipped"
    return _insert(text, block), "inserted"


def _strip_frontmatter(text: str) -> str:
    """The part of ``text`` after an optional YAML frontmatter block."""
    if not text.startswith("---"):
        return text
    close = text.find("\n---", 3)
    if close == -1:
        return ""
    newline = text.find("\n", close + 1)
    if newline == -1:
        return ""
    return text[newline + 1 :].lstrip("\n")


# --- install steps ---------------------------------------------------------


def _describe(path: Path, project: Path, action: str) -> str:
    """One report line for a managed file."""
    relative = path.relative_to(project).as_posix()
    if action == "created":
        return f"created {relative} with the crew-chief snippet"
    if action == "inserted":
        return f"inserted the crew-chief snippet into {relative}"
    if action == "updated":
        return f"updated the crew-chief snippet in {relative}"
    if action == "skipped":
        return (
            f"left {relative} alone (markers removed; "
            "pass --force-snippet to re-insert)"
        )
    return f"{relative} unchanged"


def _install_cursor_rule(
    project: Path, block: str, installed: bool, force: bool
) -> str:
    """Write the Cursor rule (frontmatter plus snippet) and return a report line."""
    path = project / CURSOR_RULE_RELATIVE
    text = _read(path)
    body, action = _merge(_strip_frontmatter(text), block, installed, force)
    candidate = CURSOR_FRONTMATTER + body
    relative = CURSOR_RULE_RELATIVE.as_posix()
    if action == "skipped":
        return f"left {relative} alone (markers removed; pass --force-snippet)"
    if candidate != text:
        _write(path, candidate)
    return f"{relative}: {action}"


def _install_data(project: Path) -> list[str]:
    """Create the preferences file and jobs placeholder when they are missing."""
    report: list[str] = []
    prefs = project / PREFS_RELATIVE
    if prefs.is_file():
        report.append(f"{PREFS_RELATIVE.as_posix()} kept")
    else:
        _write(prefs, _read(PREFS_SOURCE))
        report.append(f"created {PREFS_RELATIVE.as_posix()}")
    gitkeep = project / JOBS_GITKEEP_RELATIVE
    if gitkeep.is_file():
        report.append(f"{JOBS_GITKEEP_RELATIVE.as_posix()} kept")
    else:
        _write(gitkeep, "")
        report.append(f"created {JOBS_GITKEEP_RELATIVE.as_posix()}")
    return report


def _install_ponytail(project: Path) -> list[str]:
    """Copy the vendored Ponytail skills, replacing only copies we made."""
    report: list[str] = []
    for root in SKILL_ROOTS:
        for name in PONYTAIL_SKILLS:
            destination = project / root / name
            relative = (root / name).as_posix()
            if destination.exists() and not (destination / PONYTAIL_MARKER).is_file():
                report.append(
                    f"ponytail: {relative} exists and was not installed by "
                    "agent-crew-chief, left alone"
                )
                continue
            if destination.exists():
                shutil.rmtree(destination)
            shutil.copytree(PONYTAIL_SOURCE / name, destination, ignore=_COPY_IGNORE)
            _write(destination / PONYTAIL_MARKER, "")
            report.append(f"copied {name} to {relative}")
    return report


def install(project: Path, force: bool, ponytail: bool = True) -> list[str]:
    """Install or update the skill in ``project`` and return the report lines."""
    destinations = [project / root / SKILL_NAME for root in SKILL_ROOTS]
    installed = any(destination.is_dir() for destination in destinations)

    report: list[str] = []
    for root, destination in zip(SKILL_ROOTS, destinations):
        _copy_skill(destination)
        report.append(f"copied skill to {(root / SKILL_NAME).as_posix()}")

    if ponytail:
        report.extend(_install_ponytail(project))

    block = _block(_read(SNIPPET_SOURCE).strip("\n"))

    managed = (
        (project / "AGENTS.md", True),
        (project / "CLAUDE.md", True),
        (project / "GEMINI.md", False),
    )
    for path, create in managed:
        if not create and not path.is_file():
            continue
        text = _read(path)
        if path.name == "CLAUDE.md" and text.strip() == "@AGENTS.md":
            report.append("left CLAUDE.md alone (it only imports @AGENTS.md)")
            continue
        new_text, action = _merge(text, block, installed, force)
        if action in ("created", "inserted", "updated"):
            _write(path, new_text)
        report.append(_describe(path, project, action))

    if (project / ".cursor").is_dir():
        report.append(_install_cursor_rule(project, block, installed, force))

    report.extend(_install_data(project))
    return report


# --- entry point -----------------------------------------------------------


#: Where the released bundle's ``crew_chief`` package lives.
SCRIPTS_DIR = SKILL_SOURCE / "scripts"


def _install_command_wrapper(bin_dir: Path | None) -> list[str]:
    """Install the ``acc`` wrapper for this bundle and return the report lines."""
    from crew_chief.command_install import (
        default_bin_dir,
        install_command,
        path_hint,
    )

    directory = Path(bin_dir).expanduser() if bin_dir else default_bin_dir()
    target = install_command(
        REPO_ROOT,
        directory,
        Path(sys.executable),
        sys.platform,
        replace_owned=True,
    )
    return [f"installed {target}", path_hint(directory)]


def main(argv: list[str] | None = None) -> int:
    """Run the installer for ``argv`` and return the exit code."""
    parser = argparse.ArgumentParser(
        prog="install.py",
        description="Install (or update) the crew-chief skill in a project.",
    )
    parser.add_argument(
        "project", nargs="?", help="path to the target project"
    )
    parser.add_argument(
        "--force-snippet",
        action="store_true",
        help="re-insert the snippet even if the markers were removed",
    )
    parser.add_argument(
        "--no-ponytail",
        action="store_true",
        help="do not install the vendored Ponytail skills",
    )
    parser.add_argument(
        "--with-rtk",
        action="store_true",
        help="download and install rtk-oss (off by default)",
    )
    parser.add_argument("--no-rtk", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--no-superpowers",
        action="store_true",
        help="do not install superpowers skills",
    )
    parser.add_argument(
        "--command",
        action="store_true",
        help="install only the user-scoped `acc` wrapper for this bundle",
    )
    parser.add_argument(
        "--bin-dir",
        help="directory that receives the `acc` wrapper (default: user bin)",
    )
    args = parser.parse_args(argv)

    if args.bin_dir and not args.command:
        parser.error("--bin-dir requires --command")
    if args.project is None and not args.command:
        parser.error("give a project to install into, or --command")

    if not SKILL_SOURCE.is_dir():
        print(f"install.py: skill source not found at {SKILL_SOURCE}", file=sys.stderr)
        return 2
    if args.project and not args.no_ponytail and not PONYTAIL_SOURCE.is_dir():
        print(
            f"install.py: ponytail source not found at {PONYTAIL_SOURCE}",
            file=sys.stderr,
        )
        return 2

    project = Path(args.project).expanduser() if args.project else None
    if project is not None and not project.is_dir():
        print(f"{project}: no such directory", file=sys.stderr)
        return 2

    report = install(project, args.force_snippet, not args.no_ponytail) if project else []

    # Install kit components (rtk, superpowers, ponytail) if not opted out
    if project is not None:
        if str(SCRIPTS_DIR) not in sys.path:
            sys.path.insert(0, str(SCRIPTS_DIR))
        from crew_chief.kit_install import install_kit
        kit_report = install_kit(
            project_dir=project,
            do_install_rtk=args.with_rtk and not args.no_rtk,
            do_install_superpowers=not args.no_superpowers,
            do_install_ponytail=not args.no_ponytail,
        )
        report.extend(kit_report)

    if args.command:
        if not (SCRIPTS_DIR / "crew_chief").is_dir():
            print(
                f"install.py: skill source not found at {SKILL_SOURCE}",
                file=sys.stderr,
            )
            return 2
        if str(SCRIPTS_DIR) not in sys.path:
            sys.path.insert(0, str(SCRIPTS_DIR))
        from crew_chief.command_install import CommandInstallError

        try:
            report += _install_command_wrapper(args.bin_dir)
        except CommandInstallError as error:
            print(f"install.py: {error}", file=sys.stderr)
            return 2

    for line in report:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
