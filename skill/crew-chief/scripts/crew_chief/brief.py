"""Briefs for spec-kit tasks: the minimum context an executor or reviewer gets.

A brief never carries the whole ``spec.md`` or the plan: only the task, the
acceptance criteria it points at (resolved text), the global constraints, the
interfaces, the files it may change and the proof. The coordinator renders one
per piece with ``brief render``; the headings are English and fixed so models
and tests can rely on them.
"""

from __future__ import annotations

from .sidecar import TaskMeta
from .speckit import SpecTask

STATUS_LINE = "End with one status line: DONE | DONE_WITH_CONCERNS | BLOCKED | NEEDS_CONTEXT"
NOT_RESTRICTED = "Not restricted — keep the change minimal"
TRUST_LINE = "Do not trust the executor's report; read the diff (git show HEAD)."


def _bullets(items: list[str], empty: str) -> list[str]:
    return [f"- {item}" for item in items] if items else [f"- {empty}"]


def _files(meta: TaskMeta) -> list[str]:
    return _bullets(list(meta.files), "") if meta.files else [NOT_RESTRICTED]


def _title(task: SpecTask) -> str:
    return f"{task.id} {task.text}"


def render_exec_brief(
    task: SpecTask,
    meta: TaskMeta,
    refs: list[str],
    constraints: list[str],
    feature: str,
) -> str:
    """The brief an executor receives for ``task``.

    ``refs`` are the resolved acceptance criteria (``"REF: text"``); refs the
    spec does not know arrive as ``"UNRESOLVED: <ref>"`` and are listed as is.
    """
    lines = [
        f"# {_title(task)}",
        f"Feature: {feature} · Phase: {task.phase}",
        "",
        "## Acceptance criteria",
        *_bullets(refs, "none listed — ask before guessing"),
        "",
        "## Global constraints",
        *_bullets(constraints, "none"),
        "",
    ]
    if meta.interfaces:
        lines += [
            "## Interfaces",
            *(f"- {name}: {signature}" for name, signature in meta.interfaces.items()),
            "",
        ]
    lines += ["## Files you may change", *_files(meta), "", "## Proof"]
    if meta.proof:
        lines.append(f"Command: `{meta.proof}`")
        if meta.expect:
            lines.append(f"Expect: {meta.expect}")
    else:
        lines.append("No proof command — say how you checked your work.")
    lines += ["", "## Report", STATUS_LINE]
    return "\n".join(lines) + "\n"


def render_review_brief(task: SpecTask, meta: TaskMeta, refs: list[str]) -> str:
    """The brief a reviewer receives: two separate verdicts, no trust in the report."""
    lines = [
        f"# Review {_title(task)}",
        "",
        TRUST_LINE,
        "",
        "## Acceptance criteria",
        *_bullets(refs, "none listed"),
        "",
        "## Files allowed",
        *_files(meta),
    ]
    if meta.proof:
        lines += ["", "## Proof", f"Command: `{meta.proof}`"]
        if meta.expect:
            lines.append(f"Expect: {meta.expect}")
    lines += [
        "",
        "## Spec verdict",
        "For each acceptance criterion above, answer `done | missing | misunderstood`.",
        "Then list `extra`: anything the diff does that no criterion asked for.",
        "",
        "## Quality verdict",
        "List findings as `Critical | Important | Minor`, each with file and line.",
        "Judge the code, not the spec: bugs, edge cases, tests, readability.",
    ]
    return "\n".join(lines) + "\n"


def render_converge_brief(spec_dir_rel: str) -> str:
    """The brief for the last pass: compare the code with the spec, list new gaps."""
    spec = f"{spec_dir_rel}/spec.md"
    tasks = f"{spec_dir_rel}/tasks.md"
    return "\n".join([
        f"# Converge {spec_dir_rel}",
        "",
        f"Compare the code with `{spec}` and `{tasks}`.",
        "Every requirement and acceptance criterion must have working code and a",
        "test; every task marked done must match what the code does.",
        "",
        "## Gaps",
        "List only gaps that are not already tasks, one per line:",
        "- [ ] T### <text> per <ref> (<gap-type>)",
        "",
        "If there are none, answer exactly: Converged",
        "",
    ])
