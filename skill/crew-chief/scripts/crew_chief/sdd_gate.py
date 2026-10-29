"""SDD gate: mandatory spec-driven development phase for free-text requests.

Enforces FR-032 / US7: requests from free text (MCP, CLI, agent) enter the spec
phase, creating ``specs/NNN-<slug>/`` with ``spec.md``, ``plan.md``, and
``tasks.md`` if absent before implementation dispatches proceed.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from . import records

__all__ = [
    "check_spec_gate",
    "ensure_spec_directory",
    "enter_spec_phase",
    "is_spec_phase",
    "next_spec_number",
    "spec_slug",
]

_DIR_NUM_RE = re.compile(r"^(\d+)(?:-.*)?$")


def next_spec_number(specs_root: Path) -> int:
    """Return the next 1-based sequential spec number for ``specs_root``."""
    if not specs_root.is_dir():
        return 1
    existing_nums: list[int] = []
    for entry in specs_root.iterdir():
        if entry.is_dir():
            match = _DIR_NUM_RE.match(entry.name)
            if match:
                try:
                    existing_nums.append(int(match.group(1)))
                except ValueError:
                    pass
    if not existing_nums:
        return 1
    return max(existing_nums) + 1


def _extract_title(title: str, request_text: str) -> str:
    """Determine a human-readable title from explicit title or request text."""
    if title and title.strip():
        return title.strip()
    for line in request_text.splitlines():
        cleaned = line.strip().lstrip("#").strip()
        if cleaned:
            return cleaned[:80]
    return "free-text-task"


def spec_slug(title_or_request: str) -> str:
    """Generate a clean slug for a spec directory name."""
    s = records.slug(title_or_request)
    if not s or s == "job":
        return "free-text-task"
    return s


def ensure_spec_directory(
    root: Path | str,
    title: str = "",
    request_text: str = "",
) -> Path:
    """Create ``specs/NNN-<slug>/`` with starter documents if absent.

    If a spec directory matching ``<slug>`` already exists under ``specs/``,
    it is returned without creating a new numbered directory.
    """
    root_path = Path(root)
    specs_root = root_path / "specs"
    specs_root.mkdir(parents=True, exist_ok=True)

    derived_title = _extract_title(title, request_text)
    slug = spec_slug(derived_title)

    # Check if a spec directory with this slug already exists
    for entry in specs_root.iterdir():
        if entry.is_dir():
            match = _DIR_NUM_RE.match(entry.name)
            if match and entry.name.endswith(f"-{slug}"):
                spec_dir = entry
                break
    else:
        num = next_spec_number(specs_root)
        spec_dir = specs_root / f"{num:03d}-{slug}"
        spec_dir.mkdir(parents=True, exist_ok=True)

    spec_md = spec_dir / "spec.md"
    plan_md = spec_dir / "plan.md"
    tasks_md = spec_dir / "tasks.md"

    if not spec_md.exists():
        overview = request_text.strip() if request_text.strip() else derived_title
        spec_md.write_text(
            f"# Specification: {derived_title}\n\n"
            f"## Overview\n{overview}\n\n"
            f"## User Stories\n\n"
            f"## Requirements\n",
            encoding="utf-8",
        )

    if not plan_md.exists():
        plan_md.write_text(
            f"# Implementation Plan: {derived_title}\n\n"
            f"## Proposed Architecture\n\n"
            f"## Phases\n",
            encoding="utf-8",
        )

    if not tasks_md.exists():
        tasks_md.write_text(
            f"# Tasks: {derived_title}\n\n"
            f"## Phase 1: Planning\n\n"
            f"- [ ] T001 Initial spec review and breakdown\n",
            encoding="utf-8",
        )

    return spec_dir


def enter_spec_phase(
    target: Path | str | dict[str, Any],
    title: str = "",
    request_text: str = "",
    *,
    spec_dir: str | Path | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Enter mandatory SDD spec phase for a path, coordinator record, or job.

    Creates ``specs/NNN-<slug>/`` if absent, records the spec path and sets
    phase to ``"spec"``.
    """
    if isinstance(target, dict):
        base_dir = (
            target.get("worktree")
            or target.get("repo")
            or target.get("project")
            or "."
        )
        if spec_dir is not None:
            spec_path = Path(spec_dir)
            if not spec_path.is_absolute():
                spec_path = Path(base_dir) / spec_path
            spec_path.mkdir(parents=True, exist_ok=True)
        else:
            effective_title = title or target.get("title", "")
            effective_req = request_text or target.get("request", "")
            spec_path = ensure_spec_directory(
                Path(base_dir),
                title=effective_title,
                request_text=effective_req,
            )

        if "authorization" in target and isinstance(target["authorization"], dict):
            target["authorization"]["phase"] = "spec"
            target["authorization"]["spec_path"] = str(spec_path)

        target["phase"] = "spec"
        target["spec_dir"] = str(spec_path)
        return spec_path, target

    root_path = Path(target)
    if spec_dir is not None:
        spec_path = Path(spec_dir)
        if not spec_path.is_absolute():
            spec_path = root_path / spec_path
        spec_path.mkdir(parents=True, exist_ok=True)
    else:
        spec_path = ensure_spec_directory(
            root_path,
            title=title,
            request_text=request_text,
        )
    return spec_path, {"phase": "spec", "spec_dir": str(spec_path)}


def is_spec_phase(target: dict[str, Any]) -> bool:
    """Check whether a coordinator record or job is in the spec phase."""
    if not isinstance(target, dict):
        return False
    auth = target.get("authorization")
    if isinstance(auth, dict) and auth.get("phase") == "spec":
        return True
    return target.get("phase") == "spec"


def _norm_model(s: str) -> str:
    s = str(s).strip()
    if s.startswith("native:"):
        rest = s[len("native:"):]
        if "/" in rest:
            cli, model = rest.split("/", 1)
            return f"{cli}:{model}"
    return s


def _matches_identity(a: str, b: str) -> bool:
    a = _norm_model(a)
    b = _norm_model(b)
    if not a or not b:
        return False
    if a == b:
        return True
    a_parts = a.split(":", 1)
    b_parts = b.split(":", 1)
    if len(a_parts) == 2 and len(b_parts) == 2:
        return a_parts[1] == b_parts[1]
    if len(a_parts) == 2 and len(b_parts) == 1:
        return b in (a_parts[0], a_parts[1])
    if len(a_parts) == 1 and len(b_parts) == 2:
        return a in (b_parts[0], b_parts[1])
    return False


def _piece_touches_tasks_md(piece: dict[str, Any]) -> bool:
    title = piece.get("title") or ""
    if "tasks.md" in title:
        return True
    for attempt in piece.get("attempts") or []:
        for diff in attempt.get("diffstat") or []:
            f = diff.get("file") or ""
            if f.endswith("tasks.md") or f == "tasks.md":
                return True
    for key in ("files", "allowed_files"):
        val = piece.get(key) or []
        if isinstance(val, (list, tuple, set)):
            if any("tasks.md" in str(x) for x in val):
                return True
    task_text = piece.get("task_text") or ""
    if "tasks.md" in task_text:
        return True
    return False


def check_spec_gate(
    project: Path | str,
    job: dict[str, Any],
    piece: int,
    *,
    role: str = "exec",
) -> str | None:
    """T029 / FR-033: Block code dispatch until spec tasks reviewed.

    Returns ``None`` if dispatch is permitted, or a string diagnostic
    describing the refusal reason.
    """
    if role == "review":
        return None

    if not is_spec_phase(job):
        return None

    piece_entry = None
    for entry in job.get("pieces") or []:
        if entry.get("n") == piece:
            piece_entry = entry
            break

    piece_type = piece_entry.get("type") if piece_entry else None
    if piece_type == "docs":
        return None

    # Determine coordinator identity from job or CREW_CHIEF_COORDINATOR env
    coord_id = (
        job.get("coordinator")
        or job.get("coordinator_id")
        or os.environ.get("CREW_CHIEF_COORDINATOR", "")
    ).strip()
    if not coord_id:
        return "coordinator model unknown: cannot verify distinctness of spec review"

    proj = Path(project)

    # Verify spec tasks imported (tasks.md exists in spec directory)
    spec_dir = (
        job.get("spec_dir")
        or (job.get("authorization") or {}).get("spec_path")
        or job.get("spec_path")
    )
    tasks_path: Path | None = None
    if spec_dir:
        p = Path(spec_dir)
        tasks_path = p if p.is_absolute() else (proj / p)
        if tasks_path.is_dir():
            tasks_path = tasks_path / "tasks.md"
    else:
        specs_root = proj / "specs"
        if specs_root.is_dir():
            subdirs = [d for d in specs_root.iterdir() if d.is_dir()]
            for d in subdirs:
                cand = d / "tasks.md"
                if cand.is_file():
                    tasks_path = cand
                    break
            if not tasks_path and subdirs:
                tasks_path = subdirs[0] / "tasks.md"

    if not tasks_path or not tasks_path.is_file():
        specs_root = proj / "specs"
        if not specs_root.is_dir() or not any(d.is_dir() for d in specs_root.iterdir()):
            return "spec tasks not imported: no spec directory found"
        return "spec tasks not imported: tasks.md not found"

    # Verify at least one accepted docs piece touching tasks.md reviewed by distinct model
    for entry in job.get("pieces") or []:
        if entry.get("type") != "docs":
            continue
        if not _piece_touches_tasks_md(entry):
            continue
        if not entry.get("accepted"):
            continue
        for att in entry.get("attempts") or []:
            if att.get("role") == "review" and att.get("status") == "ok":
                rev_identities = [
                    str(x).strip()
                    for x in (att.get("reported_model"), att.get("executor"))
                    if x and str(x).strip()
                ]
                if not rev_identities:
                    continue
                if any(_matches_identity(x, coord_id) for x in rev_identities):
                    continue
                return None

    return "spec tasks not reviewed by distinct model"

