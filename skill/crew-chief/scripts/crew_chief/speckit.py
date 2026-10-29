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

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from . import records

if TYPE_CHECKING:
    from .sidecar import Sidecar

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


_STORY_RE = re.compile(r"^#{2,4}\s+User Story\s+(\d+)\s*[-–—:]\s*(.+?)\s*$")
_HEADING_RE = re.compile(r"^#{1,6}\s")
_SCENARIOS_RE = re.compile(r"Acceptance Scenarios")
_SCENARIO_RE = re.compile(r"^\s*\d+[.)]\s+(.+?)\s*$")
_REQ_RE = re.compile(
    r"^\s*[-*]\s+\*{0,2}((?:FR|SC)-\d{3,})\*{0,2}\s*:\s*\*{0,2}\s*(.+?)\s*$"
)


def parse_spec_refs(text: str) -> dict[str, str]:
    """Index a ``spec.md`` by reference.

    Keys: ``US<n>`` (story title with priority), ``US<n>/AC<m>`` (the
    m-th Given/When/Then item under that story's ``Acceptance Scenarios``),
    ``FR-###`` and ``SC-###`` (requirement and success-criterion text).
    The first occurrence of a key wins.
    """
    if text.startswith(_BOM):
        text = text[len(_BOM):]
    index: dict[str, str] = {}
    story: str | None = None
    in_scenarios = False
    count = 0
    for raw in _lines(text):
        line = raw.rstrip("\r")
        heading = _STORY_RE.match(line)
        if heading:
            story = f"US{heading.group(1)}"
            index.setdefault(story, heading.group(2))
            in_scenarios = False
            count = 0
            continue
        if _HEADING_RE.match(line):
            story = None
            in_scenarios = False
            continue
        if story and _SCENARIOS_RE.search(line):
            in_scenarios = True
            continue
        if in_scenarios:
            item = _SCENARIO_RE.match(line)
            if item and "Given" in item.group(1):
                count += 1
                index.setdefault(f"{story}/AC{count}", item.group(1))
                continue
        req = _REQ_RE.match(line)
        if req:
            index.setdefault(req.group(1), req.group(2))
    return index


def resolve_refs(
    refs: list[str], index: dict[str, str]
) -> tuple[list[str], list[str]]:
    """Look ``refs`` up in ``index``.

    Returns ``(found, missing)``: ``found`` holds ``"REF: text"`` lines in
    the order given, ``missing`` the refs the index does not know.
    """
    found: list[str] = []
    missing: list[str] = []
    for ref in refs:
        if ref in index:
            found.append(f"{ref}: {index[ref]}")
        else:
            missing.append(ref)
    return found, missing


# -- locating the spec folder ------------------------------------------------


class SpecError(ValueError):
    """A spec folder problem; the message is one line (the CLI exits 2)."""


FEATURE_JSON = Path(".specify") / "feature.json"


def find_spec_dir(project: Path, spec_dir: str | None) -> Path:
    """Return the spec folder: ``spec_dir`` if given, else spec-kit's pointer.

    The pointer is ``.specify/feature.json`` (key ``feature_directory``, or
    ``path``). Relative paths resolve against ``project``. The folder must
    exist; ``project`` is never written to.
    """
    project = Path(project)
    if not spec_dir:
        pointer = project / FEATURE_JSON
        if not pointer.is_file():
            raise SpecError(
                f"no spec folder: pass --spec-dir or create {FEATURE_JSON.as_posix()}"
            )
        try:
            data = json.loads(pointer.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            raise SpecError(f"{FEATURE_JSON.as_posix()}: cannot read: {exc}") from None
        value = data.get("feature_directory") or data.get("path") if isinstance(data, dict) else None
        if not isinstance(value, str) or not value:
            raise SpecError(
                f"{FEATURE_JSON.as_posix()}: no 'feature_directory' or 'path' key"
            )
        spec_dir = value
    folder = project / spec_dir
    if not folder.is_dir() and (project / "specs" / spec_dir).is_dir():
        folder = project / "specs" / spec_dir
    if not folder.is_dir():
        raise SpecError(f"spec folder not found: {spec_dir}")
    return folder


# -- guesses for tasks the sidecar does not describe --------------------------

_TESTS_RE = re.compile(r"(^|[\s/\\(])tests?[/\\]|\btests?\b", re.IGNORECASE)
_DOCS_RE = re.compile(r"(^|[\s/\\(])docs[/\\]|\.md\b", re.IGNORECASE)
_RESEARCH_RE = re.compile(r"^(research|investigate|evaluate)\b", re.IGNORECASE)
_HIGH_RISK_RE = re.compile(
    r"contracts[/\\]|data-model|migration|\bauth|secret", re.IGNORECASE
)


def guess_type(task: SpecTask) -> str:
    """Work type of a task from its text: tests, docs, research or code."""
    if _TESTS_RE.search(task.text):
        return "tests"
    if _DOCS_RE.search(task.text):
        return "docs"
    if _RESEARCH_RE.match(task.text):
        return "research"
    return "code"


def guess_risk(task: SpecTask) -> str:
    """Risk of a task: high for contracts/data/auth/secrets, else by phase."""
    if _HIGH_RISK_RE.search(task.text):
        return "high"
    if "foundational" in task.phase.lower():
        return "medium"
    if task.parallel:
        return "low"
    return "medium"


# -- readiness ----------------------------------------------------------------


def readiness(
    job: dict, tasks: list[SpecTask], sidecar: "Sidecar"
) -> dict[str, str]:
    """Which spec tasks of ``job`` may start.

    Maps every task id that has a live piece (``piece["ref"]``, stale ones ignored) and is not accepted
    to ``""`` (ready) or the reason it waits: ``depends on T012`` (first
    dependency not accepted; dependencies come from the sidecar, falling back
    to the task text) or ``phase '<name>' incomplete`` (an earlier phase has
    a piece that is not accepted yet). A task ticked in ``tasks.md`` counts
    as accepted.
    """
    pieces = {}
    for task in tasks:
        piece = records.piece_for_ref(job, task.id)
        if piece is not None:
            pieces[task.id] = piece
    ticked = {task.id for task in tasks if task.done}

    def accepted(task_id: str) -> bool:
        piece = pieces.get(task_id)
        return task_id in ticked or bool(piece and piece.get("accepted"))

    phases: list[str] = []
    for task in tasks:
        if task.phase not in phases:
            phases.append(task.phase)

    result: dict[str, str] = {}
    for task in tasks:
        if task.id not in pieces or accepted(task.id):
            continue
        meta = sidecar.tasks.get(task.id)
        depends = list(meta.depends) if meta and meta.depends else list(task.depends)
        reason = ""
        for dependency in depends:
            if not accepted(dependency):
                reason = f"depends on {dependency}"
                break
        if not reason:
            for earlier in phases[: phases.index(task.phase)]:
                if any(
                    other.phase == earlier
                    and other.id in pieces
                    and not accepted(other.id)
                    for other in tasks
                ):
                    reason = f"phase '{earlier}' incomplete"
                    break
        result[task.id] = reason
    return result
