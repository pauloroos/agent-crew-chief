"""The ledger: one JSON line per graded job, shared by every project on this machine.

It lives outside any repository (``~/.crew-chief/ledger.jsonl``, or
``$CREW_CHIEF_HOME/ledger.jsonl``), so scores never show up in a pull request
and a model graded in one project helps the pick in the next.
"""

from __future__ import annotations

import json
import os
import platform
import re
from datetime import datetime, timezone
from pathlib import Path

KINDS = ("code", "tests", "refactor", "design", "docs", "research", "diagnosis", "data", "review", "coordinate")
COSTS = ("free", "included", "cheap", "paid")
OUTCOMES = ("pass", "fail")

# What must never reach a shared ledger: GitHub, OpenAI-style, Slack and AWS tokens, JWTs,
# PEM private keys, `password=...`-style pairs, and long runs that mix letters and digits.
# shortcut: a 40-character commit hash is redacted too; record short hashes.
_SECRET = re.compile(
    r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_\w{20,}|sk-[\w-]{16,}|xox[abprs]-[\w-]{10,}"
    r"|AKIA[0-9A-Z]{16}|eyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]*|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|(?i:\b(?:password|passwd|secret|token|api[_-]?key)\s*[:=]\s*)\S+"
    r"|\b(?=[A-Za-z_+=]*\d)(?=[\d_+=]*[A-Za-z])[A-Za-z0-9_+=]{32,}"
)


def redact(text: str) -> str:
    """``text`` with anything that looks like a token or a key replaced by ``[redacted]``."""
    return _SECRET.sub("[redacted]", text)


def machine() -> str:
    """This machine's name in the ledger: ``$CREW_CHIEF_MACHINE``, else the host name."""
    name = os.environ.get("CREW_CHIEF_MACHINE") or platform.node() or "unknown"
    return re.sub(r"[^A-Za-z0-9._-]", "-", name)[:64]


def ledger_path() -> Path:
    home = os.environ.get("CREW_CHIEF_HOME")
    return (Path(home) if home else Path.home() / ".crew-chief") / "ledger.jsonl"


def append(entry: dict) -> dict:
    """Write one entry (``at`` defaults to now, ``None`` fields are dropped, text redacted) and return it."""
    fields = {key: redact(value) if isinstance(value, str) else value
              for key, value in entry.items() if value is not None}
    entry = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **fields}
    path = ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # a line cut short by a crash has no newline: start on a fresh one
    torn = path.is_file() and path.stat().st_size and not path.read_bytes().endswith(b"\n")
    # one short append per entry: parallel coordinators do not interleave lines
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(("\n" if torn else "") + json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def read() -> list[dict]:
    path = ledger_path()
    if not path.is_file():
        return []
    entries = []
    seen = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue  # a line cut short by a crash
        key = entry_key(entry)
        if key in seen:
            continue
        seen.add(key)
        entries.append(entry)
    return entries


def entry_key(entry) -> str:
    return json.dumps(entry, sort_keys=True, ensure_ascii=False)


class MergeError(Exception):
    """A merge file that cannot be used; the message already fits ``<path>: <problem>``."""


def merge(other: Path) -> tuple[int, int]:
    """Copy the entries of ``other`` missing locally; return ``(new, already)``."""
    if other.is_dir():
        raise MergeError(f"{other}: is a folder")
    try:
        text = other.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise MergeError(f"{other}: no such file")
    except UnicodeDecodeError:
        raise MergeError(f"{other}: is not UTF-8 text")
    except OSError as exc:
        raise MergeError(f"{other}: {exc.strerror or exc}")
    valid: list[dict] = []
    for line in text.splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            valid.append(entry)
    if text and not valid:
        raise MergeError(f"{other}: is not a ledger")
    known = {entry_key(entry) for entry in read()}
    new = [entry for entry in valid if entry_key(entry) not in known
           and not known.add(entry_key(entry))]
    already = len(valid) - len(new)
    if new:
        path = ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        torn = path.is_file() and path.stat().st_size and not path.read_bytes().endswith(b"\n")
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            if torn:
                handle.write("\n")
            for entry in new:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return len(new), already


def import_v06(project: Path) -> int:
    """Copy the grades kept by ACC 0.6 in ``<project>/docs/crew-chief/jobs`` into the ledger.

    Only the grades come over (the 0.6 failure counts were skewed by tools that
    exited non-zero after finishing). Running it again adds nothing new.
    """
    done = {entry.get("ref") for entry in read()}
    added = 0
    for path in sorted((project / "docs" / "crew-chief" / "jobs").glob("*.json")):
        job = json.loads(path.read_text(encoding="utf-8"))
        for piece in job.get("pieces", []):
            grades = dict(piece.get("grades") or {})
            legacy = piece.get("grade")  # records older than per-executor grades
            if isinstance(legacy, dict) and legacy.get("executor"):
                grades.setdefault(legacy["executor"], legacy)
            for executor, grade in grades.items():
                if not isinstance(grade, dict):
                    continue
                ref = f"{project.name}/{job.get('id', path.stem)}#{piece.get('n')}/{executor}"
                if ref in done or not isinstance(grade.get("score"), (int, float)):
                    continue
                agent, _, model = executor.partition(":")
                cost = next(
                    (a.get("cost") for a in piece.get("attempts", []) if a.get("executor") == executor),
                    None,
                )
                score = grade["score"]
                append({
                    "at": grade.get("graded_at") or job.get("created"),
                    "project": project.name,
                    "kind": piece.get("type") or "code",
                    "agent": agent,
                    "model": model,
                    "cost": cost,
                    # shortcut: 0.6 kept no pass/fail per grade, 60+ counts as a pass
                    "outcome": "pass" if score >= 60 else "fail",
                    "score": score,
                    "job": piece.get("title"),
                    "source": "v0.6",
                    "ref": ref,
                })
                added += 1
    return added
