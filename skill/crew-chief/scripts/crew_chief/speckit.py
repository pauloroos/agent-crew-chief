"""Spec-kit ``tasks.md`` grammar: parse tasks and tick their checkboxes.

The grammar is re-implemented here (tolerant, line based); nothing is
copied from spec-kit templates. A task line looks like::

    - [ ] T012 [P] [US1] Create User model in src/models/user.py

``[P]`` (parallel) and ``[USn]`` (user story) are optional and may come
in any order after the id. The phase is the last ``## ...`` heading
above the task. Dependencies are read from ``depends on T004, T007`` or
``(after T003)`` inside the task text. Lines that do not match are
ignored.

``mark_done`` edits only the one task line, so the user's BOM and line
endings (CRLF or LF, even mixed) survive untouched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_BOM = "\ufeff"
_TASK_RE = re.compile(r"^(\s*[-*]\s+\[)([ xX])(\]\s+)(T\d{3,})\b(.*)$")
_PHASE_RE = re.compile(r"^##\s+(.+?)\s*$")
_FLAG_RE = re.compile(r"^\s*\[(P|US\d+)\]")
_DEPENDS_RE = re.compile(
    r"\bdepends\s+on\s+(T\d{3,}(?:\s*(?:,|and|&)\s*T\d{3,})*)"
    r"|\(\s*after\s+(T\d{3,}(?:\s*(?:,|and|&)\s*T\d{3,})*)\s*\)",
    re.IGNORECASE,
)
_ID_RE = re.compile(r"\bT\d{3,}\b")


@dataclass(frozen=True)
class SpecTask:
    id: str
    done: bool
    parallel: bool
    story: str | None
    text: str
    phase: str
    depends: tuple[str, ...]
    line: int  # 0-based line index in the file


def _split_flags(rest: str) -> tuple[bool, str | None, str]:
    """Consume leading ``[P]`` / ``[USn]`` tokens; return flags and remaining text."""
    parallel = False
    story: str | None = None
    while True:
        match = _FLAG_RE.match(rest)
        if not match:
            break
        token = match.group(1)
        if token == "P":
            parallel = True
        else:
            story = token
        rest = rest[match.end():]
    return parallel, story, rest.strip()


def _depends(text: str) -> tuple[str, ...]:
    found: list[str] = []
    for match in _DEPENDS_RE.finditer(text):
        for task_id in _ID_RE.findall(match.group(1) or match.group(2)):
            if task_id not in found:
                found.append(task_id)
    return tuple(found)


def _lines(text: str) -> list[str]:
    """Split on ``\n``; a trailing ``\r`` stays on its line (CRLF-safe)."""
    return text.split("\n")


def parse_tasks(text: str) -> list[SpecTask]:
    """Return every task line of ``text`` in file order."""
    if text.startswith(_BOM):
        text = text[len(_BOM):]
    tasks: list[SpecTask] = []
    phase = ""
    for index, raw in enumerate(_lines(text)):
        line = raw.rstrip("\r")
        heading = _PHASE_RE.match(line)
        if heading:
            phase = heading.group(1)
            continue
        match = _TASK_RE.match(line)
        if not match:
            continue
        parallel, story, body = _split_flags(match.group(5))
        tasks.append(
            SpecTask(
                id=match.group(4),
                done=match.group(2) in ("x", "X"),
                parallel=parallel,
                story=story,
                text=body,
                phase=phase,
                depends=_depends(body),
                line=index,
            )
        )
    return tasks


def mark_done(text: str, task_id: str) -> str:
    """Tick ``task_id``'s checkbox (``- [X]``); change nothing else.

    Raises ``KeyError`` when the task does not exist. A task that is
    already ticked returns ``text`` unchanged.
    """
    lines = _lines(text)
    for index, raw in enumerate(lines):
        line = raw.lstrip(_BOM) if index == 0 else raw
        match = _TASK_RE.match(line.rstrip("\r"))
        if not match or match.group(4) != task_id:
            continue
        if match.group(2) in ("x", "X"):
            return text
        # The checkbox sits at a fixed offset; rewrite only that character.
        offset = len(raw) - len(line) + len(match.group(1))
        lines[index] = raw[:offset] + "X" + raw[offset + 1:]
        return "\n".join(lines)
    raise KeyError(task_id)
