"""Architecture decision records (ADRs) under ``docs/decisions``.

The coordinator records small rulings in the job; the ones worth keeping are
promoted to one Markdown file each, numbered ``NNNN-<slug>.md``.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from .records import slug

#: Folder of the ADRs, relative to the project root.
DECISIONS_DIR = Path("docs") / "decisions"

_NUMBERED = re.compile(r"^(\d{4,})-.*\.md$")

#: Longest slug used in an ADR file name.
_SLUG_MAX = 50


def next_number(project: Path) -> int:
    """The number for the next ADR: highest ``NNNN-*.md`` plus one (1 when none)."""
    folder = Path(project) / DECISIONS_DIR
    highest = 0
    if folder.is_dir():
        for entry in folder.iterdir():
            match = _NUMBERED.match(entry.name)
            if match and entry.is_file():
                highest = max(highest, int(match.group(1)))
    return highest + 1


def _section(heading: str, body: str) -> list[str]:
    return ["", f"## {heading}", "", body.strip() or "_Not recorded._"]


def write_adr(
    project: Path,
    number: int,
    title: str,
    context: str,
    decision: str,
    alternatives: str,
    consequences: str,
    origin: str,
    today: date,
) -> Path:
    """Write ``docs/decisions/NNNN-<slug>.md`` and return its path.

    Raises :class:`FileExistsError` when an ADR with this number already exists.
    """
    folder = Path(project) / DECISIONS_DIR
    prefix = f"{number:04d}"
    if folder.is_dir():
        existing = sorted(folder.glob(f"{prefix}-*.md"))
        if existing:
            raise FileExistsError(f"ADR {prefix} already exists: {existing[0]}")
    folder.mkdir(parents=True, exist_ok=True)
    name = slug(title)[:_SLUG_MAX].strip("-") or "decision"
    path = folder / f"{prefix}-{name}.md"

    lines = [
        f"# {prefix}. {title}",
        "",
        "- Status: accepted",
        f"- Date: {today.isoformat()}",
        f"- Origin: {origin}",
    ]
    lines += _section("Context", context)
    lines += _section("Decision", decision)
    lines += _section("Alternatives considered", alternatives)
    lines += _section("Consequences", consequences)
    text = "\n".join(lines) + "\n"
    # Exclusive create: a concurrent writer with the same number fails loudly.
    with open(path, "x", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    return path
