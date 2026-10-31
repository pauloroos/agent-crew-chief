#!/usr/bin/env python3
"""Install or update the ``crew-chief`` skill in a project.

Usage::

    python install.py <project> [--force-snippet] [--no-superpowers]
        [--no-ponytail]

What it does:

1. Copies ``skill/crew-chief/`` into ``<project>/.claude/skills/crew-chief/``,
   ``<project>/.agents/skills/crew-chief/`` and
   ``<project>/.gemini/skills/crew-chief/``, replacing earlier copies (files
   that differ only in CRLF are left as they are).
2. Inserts or replaces the snippet (``templates/snippet.md``) between
   ``<!-- BEGIN:agent-crew-chief -->`` and ``<!-- END:agent-crew-chief -->``:
   in ``AGENTS.md`` (created if missing), in ``CLAUDE.md`` (left alone when it
   only contains ``@AGENTS.md``), in ``GEMINI.md`` only if that file exists and
   in ``.cursor/rules/agent-crew-chief.mdc`` only if ``.cursor/`` exists (with
   YAML frontmatter ``alwaysApply: true``). In ``AGENTS.md``, a file that
   starts with another tool's managed block gets the snippet right after that
   block's END line; otherwise the snippet is appended at the end.
3. Copies the vendored skill kits into the same three roots, one folder per
   skill: superpowers (``vendor/superpowers/skills/``: test-driven-development,
   systematic-debugging, verification-before-completion) and Ponytail
   (``vendor/ponytail/skills/``: ponytail, ponytail-review). Each copy is
   marked with ``.installed-by-agent-crew-chief``; a folder without that
   marker belongs to someone else and is left alone. ``--no-superpowers`` and
   ``--no-ponytail`` skip a kit and leave any copy already there.

It writes nothing else: scores live in ``~/.crew-chief/``, outside the project.
A file without markers is left alone once the skill is installed in the project:
the markers were removed on purpose. ``--force-snippet`` inserts the snippet
anyway.

It prints every change and exits ``2`` with one line when ``<project>`` is not
an existing directory or a wanted kit's vendor folder is missing. Standard
library only, so ``python -I install.py`` works.
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

#: Name of the installed skill directory and the roots it is copied into.
SKILL_NAME = "crew-chief"
SKILL_ROOTS = (
    Path(".claude") / "skills",
    Path(".agents") / "skills",
    Path(".gemini") / "skills",
)

#: Vendored skill kits: kit name -> its skills under ``vendor/<kit>/skills/``.
VENDOR = REPO_ROOT / "vendor"
KITS = {
    "superpowers": (
        "test-driven-development",
        "systematic-debugging",
        "verification-before-completion",
    ),
    "ponytail": ("ponytail", "ponytail-review"),
}
KIT_MARKER = ".installed-by-agent-crew-chief"

CURSOR_RULE_RELATIVE = Path(".cursor") / "rules" / "agent-crew-chief.mdc"

#: Frontmatter written on top of the Cursor rule.
CURSOR_FRONTMATTER = (
    "---\n"
    "description: agent-crew-chief - runs work through parallel Orca agents and scores every model.\n"
    "alwaysApply: true\n"
    "---\n\n"
)


# --- file helpers ----------------------------------------------------------


def _read(path: Path) -> str:
    """The UTF-8 text of ``path``, or ``""`` when it does not exist."""
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def _write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` as UTF-8 with LF line endings."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _same_text(a: Path, b: Path) -> bool:
    return a.read_bytes().replace(b"\r\n", b"\n") == b.read_bytes().replace(b"\r\n", b"\n")


def _sync_tree(source: Path, destination: Path) -> None:
    """Make ``destination`` match ``source``, without caches; keep the kit marker.

    A file that differs only in CRLF is kept: a Windows checkout with
    ``core.autocrlf`` holds CRLF copies, and rewriting them with LF makes git
    list every one as modified with no diff.
    """
    wanted = {Path(KIT_MARKER)}
    for item in source.rglob("*"):
        relative = item.relative_to(source)
        if "__pycache__" in relative.parts or item.suffix == ".pyc":
            continue
        wanted.add(relative)
        target = destination / relative
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif not (target.is_file() and _same_text(item, target)):
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
    for item in sorted(destination.rglob("*"), reverse=True):
        if item.relative_to(destination) not in wanted:
            shutil.rmtree(item) if item.is_dir() else item.unlink()


def _missing_kit(kits: list[str]) -> Path | None:
    """The vendor folder of the first wanted kit that is missing, else None."""
    return next((VENDOR / k for k in kits if not (VENDOR / k / "skills").is_dir()), None)


def _install_kits(base: Path, kits: list[str]) -> list[str]:
    """Copy each skill of ``kits`` into every root, replacing only our copies."""
    report: list[str] = []
    for kit in kits:
        for name in KITS[kit]:
            for root in SKILL_ROOTS:
                destination = base / root / name
                relative = (root / name).as_posix()
                if destination.exists() and not (destination / KIT_MARKER).is_file():
                    report.append(
                        f"{relative} exists and was not installed by "
                        "agent-crew-chief, left alone"
                    )
                    continue
                _sync_tree(VENDOR / kit / "skills" / name, destination)
                (destination / KIT_MARKER).touch()
                report.append(f"copied {name} to {relative}")
    return report


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


def install(project: Path, force: bool, kits: list[str] | None = None) -> list[str]:
    """Install or update the skill in ``project`` and return the report lines."""
    destinations = [project / root / SKILL_NAME for root in SKILL_ROOTS]
    installed = any(destination.is_dir() for destination in destinations)

    report: list[str] = []
    for root, destination in zip(SKILL_ROOTS, destinations):
        _sync_tree(SKILL_SOURCE, destination)
        report.append(f"copied skill to {(root / SKILL_NAME).as_posix()}")
    report.extend(_install_kits(project, list(KITS) if kits is None else kits))

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

    return report


# --- entry point -----------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Run the installer for ``argv`` and return the exit code."""
    parser = argparse.ArgumentParser(
        prog="install.py",
        description="Install (or update) the crew-chief skill in a project.",
    )
    parser.add_argument("project", help="path to the target project")
    parser.add_argument(
        "--force-snippet",
        action="store_true",
        help="re-insert the snippet even if the markers were removed",
    )
    for kit in KITS:
        parser.add_argument(
            f"--no-{kit}", action="store_true", help=f"do not install the {kit} skills"
        )
    args = parser.parse_args(argv)

    if not SKILL_SOURCE.is_dir():
        print(f"install.py: skill source not found at {SKILL_SOURCE}", file=sys.stderr)
        return 2
    kits = [kit for kit in KITS if not getattr(args, f"no_{kit}")]
    missing = _missing_kit(kits)
    if missing:
        print(f"install.py: kit source not found at {missing}", file=sys.stderr)
        return 2
    project = Path(args.project).expanduser()
    if not project.is_dir():
        print(f"{project}: no such directory", file=sys.stderr)
        return 2

    for line in install(project, args.force_snippet, kits):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
