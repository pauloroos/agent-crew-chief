"""The project CHANGELOG entry that ``close`` writes (Keep a Changelog).

``insert_entry`` edits the text of the user's file line by line: it adds the
entry to ``## [Unreleased]`` (creating the section and the ``### <category>``
heading when missing), never touches a released ``## [x.y.z]`` section, keeps
the file's end of line and BOM, and does nothing when the job's
``<!-- crew:<id> -->`` marker is already in the file.
"""

from __future__ import annotations

import re

from .records import split_title

#: Conventional commit type -> Keep a Changelog category. Other types
#: (``docs``, ``test``, ``chore``) get no entry.
CATEGORY_BY_COMMIT = {
    "feat": "Added",
    "fix": "Fixed",
    "refactor": "Changed",
    "perf": "Changed",
}

#: Keep a Changelog 1.1 order of the ``###`` headings of a section.
_CATEGORY_ORDER = ("Added", "Changed", "Deprecated", "Removed", "Fixed", "Security")

_HEADER = (
    "# Changelog",
    "",
    "All notable changes to this project are documented in this file.",
    "",
    "The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).",
)

_MARKER = re.compile(r"<!-- crew:\S+ -->")
_UNRELEASED = re.compile(r"^##\s+\[?unreleased\]?(\s|$)", re.IGNORECASE)
#: A released section: ``## [1.2.0]`` (Keep a Changelog) or ``## 1.2.0`` /
#: ``## v1.2.0`` (changesets and similar tools).
_RELEASE = re.compile(r"^##\s+(\[|v?\d+\.\d+)")
_SECTION = re.compile(r"^##\s")
_CATEGORY = re.compile(r"^###\s+(.*?)\s*$")
_HEADING = re.compile(r"^#{1,3}\s")
_LINK_REFERENCE = re.compile(r"^\s{0,3}\[[^\]]+\]:\s")


def entry_line(job: dict) -> str:
    """``- <title> (<spec_dir>, ADR 0007, ...). <!-- crew:<id> -->`` for the job.

    The parentheses hold only what the job has: its spec folder and the
    numbers of the ADRs it recorded.
    """
    _, _, rest = split_title(str(job.get("title", "")))
    title = " ".join(rest.split()).rstrip(".")
    notes = []
    if job.get("spec_dir"):
        notes.append(str(job["spec_dir"]))
    notes.extend(f"ADR {number}" for number in dict.fromkeys(job.get("decisions") or []))
    suffix = f" ({', '.join(notes)})" if notes else ""
    return f"- {title}{suffix}. <!-- crew:{job.get('id', '')} -->"


def _split(text: str) -> list[str]:
    """Lines of ``text``, each keeping its own line ending."""
    return re.findall(r"[^\n]*\n|[^\n]+", text)


def _body(line: str) -> str:
    return line.rstrip("\r\n")


def _eol(text: str) -> str:
    crlf = text.count("\r\n")
    return "\r\n" if crlf and crlf * 2 >= text.count("\n") else "\n"


def _content_end(lines: list[str], start: int, end: int) -> int:
    """Index after the last content line of ``lines[start:end]``.

    Trailing blank lines and link reference definitions (``[x]: url``) are not
    content: entries go above them.
    """
    index = end
    while index > start and (
        not _body(lines[index - 1]).strip()
        or _LINK_REFERENCE.match(_body(lines[index - 1]))
    ):
        index -= 1
    return index


def _unreleased_range(lines: list[str]) -> tuple[int, int] | None:
    for start, line in enumerate(lines):
        if _UNRELEASED.match(_body(line)):
            end = next(
                (i for i in range(start + 1, len(lines))
                 if _SECTION.match(_body(lines[i]))),
                len(lines),
            )
            return start, end
    return None


def _terminate(lines: list[str], index: int, eol: str) -> None:
    """Give the line before ``index`` an end of line when it has none."""
    if 0 < index <= len(lines) and not lines[index - 1].endswith("\n"):
        lines[index - 1] += eol


def _with_unreleased(lines: list[str], eol: str) -> None:
    """Add an empty ``## [Unreleased]`` before the first release, or at the end."""
    heading = [f"## [Unreleased]{eol}", eol]
    first_release = next(
        (i for i, line in enumerate(lines) if _RELEASE.match(_body(line))), None
    )
    if first_release is not None:
        lines[first_release:first_release] = heading
        return
    index = _content_end(lines, 0, len(lines))
    _terminate(lines, index, eol)
    added = ([eol] if index else []) + heading[:1]
    if index < len(lines):  # link references follow: keep a blank line before them
        added.append(eol)
    lines[index:index] = added


def insert_entry(text: str | None, category: str, line: str) -> str:
    """``text`` with ``line`` added under ``### <category>`` of Unreleased.

    ``text=None`` starts a new file with a Keep a Changelog header. The marker
    ``<!-- crew:<id> -->`` of ``line`` already being anywhere in ``text``
    returns ``text`` unchanged.
    """
    if text is None:
        lines = [f"{row}\n" for row in _HEADER]
        lines += ["\n", "## [Unreleased]\n", "\n", f"### {category}\n", f"{line}\n"]
        return "".join(lines)

    marker = _MARKER.search(line)
    if marker and marker.group(0) in text:
        return text

    bom = "\ufeff" if text.startswith("\ufeff") else ""
    lines = _split(text[len(bom):])
    eol = _eol(text)

    if _unreleased_range(lines) is None:
        _with_unreleased(lines, eol)
    start, end = _unreleased_range(lines)  # type: ignore[misc]

    # The category's block inside Unreleased, and where a new one would go.
    block: tuple[int, int] | None = None
    later_heading: int | None = None
    wanted = category.lower()
    rank = {name.lower(): i for i, name in enumerate(_CATEGORY_ORDER)}
    for index in range(start + 1, end):
        match = _CATEGORY.match(_body(lines[index]))
        if not match:
            continue
        name = match.group(1).lower()
        if name == wanted:
            stop = next(
                (i for i in range(index + 1, end)
                 if _HEADING.match(_body(lines[i]))),
                end,
            )
            block = (index, stop)
            break
        if later_heading is None and rank.get(name, 99) > rank.get(wanted, 98):
            later_heading = index

    if block is not None:
        at = _content_end(lines, block[0] + 1, block[1])
        _terminate(lines, at, eol)
        lines.insert(at, f"{line}{eol}")
    elif later_heading is not None:
        lines[later_heading:later_heading] = [
            f"### {category}{eol}", f"{line}{eol}", eol,
        ]
    else:
        at = _content_end(lines, start + 1, end)
        _terminate(lines, at, eol)
        new = [eol, f"### {category}{eol}", f"{line}{eol}"]
        if at < len(lines) and _body(lines[at]).strip() and _SECTION.match(
            _body(lines[at])
        ):
            new.append(eol)
        lines[at:at] = new
    return bom + "".join(lines)
