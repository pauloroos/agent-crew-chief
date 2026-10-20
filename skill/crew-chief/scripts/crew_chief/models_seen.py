"""Registry of the models discovery has seen, and what changed since last time.

Executor CLIs add and drop models without notice (OpenCode rotates its
``*-free`` models often). ``docs/crew-chief/models-seen.json`` keeps, per
executor id, its ``cost``, ``trains``, ``first_seen``, ``last_seen`` (UTC ISO)
and whether it was ``present`` in the last discovery, plus a top-level
``last_check``. ``discover`` and ``job new`` always check; ``pick`` and
``dispatch`` check only when ``last_check`` is older than
``model_check_hours`` (default 24), so the coordinator hears at least once a
day which models are new, gone or changed.

The file is committed with the project, like the job records. Only a tool
whose model listing succeeded can make a model "gone": a tool skipped for a
missing bin, a missing env var or an empty listing leaves its models alone.
The first models recorded for a tool (on the first run, or when a tool is
added later) are a silent baseline, not a flood of "new" lines.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .discover import Discovery
from .records import RecordError, crew_chief_dir, write_atomic
from .tiers import TIER_ORDER
from .tools import Executor

#: File name of the registry inside ``docs/crew-chief``.
MODELS_SEEN_NAME = "models-seen.json"


@dataclass
class ModelChanges:
    """What one discovery pass changed in the registry.

    ``added`` and ``removed`` hold ``{"id", "cost", "trains"}``; ``changed``
    also carries ``was_cost`` and ``was_trains``. ``baseline`` counts the
    executors recorded silently (``None`` when there were none and the file
    already existed); ``baseline_tools`` names the tools seen for the first
    time on a later run. ``since`` is the previous run, ``error`` the reason
    the file was left untouched.
    """

    added: list[dict] = field(default_factory=list)
    removed: list[dict] = field(default_factory=list)
    changed: list[dict] = field(default_factory=list)
    baseline: int | None = None
    baseline_tools: list[str] = field(default_factory=list)
    since: str | None = None
    error: str | None = None


def models_seen_path(project: Path) -> Path:
    """Return ``project/docs/crew-chief/models-seen.json``."""
    return crew_chief_dir(project) / MODELS_SEEN_NAME


def load_seen(project: Path) -> dict | None:
    """The stored registry, or ``None`` when the file does not exist yet.

    Raises :class:`RecordError` naming the file when it is unreadable, not
    valid JSON or not shaped like a registry.
    """
    path = models_seen_path(project)
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RecordError(f"{path}: cannot read models registry: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RecordError(
            f"{path}: invalid JSON at line {exc.lineno} column {exc.colno}: {exc.msg}"
        ) from exc
    if not isinstance(data, dict) or not isinstance(data.get("models", {}), dict):
        raise RecordError(f"{path}: models registry is not a JSON object of models")
    return data


def save_seen(project: Path, snapshot: dict) -> None:
    """Write the registry atomically: sorted keys, indent 2, UTF-8, LF."""
    text = json.dumps(snapshot, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    write_atomic(models_seen_path(project), text)


def _tool_of(executor_id: str) -> str:
    """The tool name of ``<tool>:<model>``."""
    return executor_id.partition(":")[0]


def _brief(executor_id: str, cost: object, trains: object) -> dict:
    return {"id": executor_id, "cost": cost, "trains": bool(trains)}


def diff_models(
    previous: dict | None,
    executors: list[Executor],
    listed: dict[str, list[str]],
    now: str,
) -> tuple[dict, ModelChanges]:
    """Fold the current ``executors`` into ``previous``; pure.

    Returns the new registry and the changes. ``listed`` maps every tool
    whose listing succeeded to all the models it listed (``forbidden`` ones
    included), as in :class:`~crew_chief.discover.Discovery`. A model is
    ``removed`` only when it was present, its tool is in ``listed`` and the
    tool did not list it; a model that is listed but filtered out as
    ``forbidden`` is left as it was.
    """
    old_models = dict((previous or {}).get("models") or {})
    models = copy.deepcopy(old_models)
    known_tools = {_tool_of(executor_id) for executor_id in old_models}
    changes = ModelChanges(since=(previous or {}).get("last_check"))
    baseline_ids: list[str] = []
    baseline_tools: set[str] = set()

    current = {executor.id: executor for executor in executors}
    for executor_id in sorted(current):
        executor = current[executor_id]
        entry = models.get(executor_id)
        if not isinstance(entry, dict):
            models[executor_id] = {
                "cost": executor.cost,
                "trains": executor.trains,
                "first_seen": now,
                "last_seen": now,
                "present": True,
            }
            tool_name = _tool_of(executor_id)
            if tool_name in known_tools:
                changes.added.append(
                    _brief(executor_id, executor.cost, executor.trains)
                )
            else:
                baseline_ids.append(executor_id)
                baseline_tools.add(tool_name)
            continue

        was_present = bool(entry.get("present"))
        was_cost, was_trains = entry.get("cost"), bool(entry.get("trains"))
        entry.update(
            cost=executor.cost, trains=executor.trains, last_seen=now, present=True
        )
        entry.setdefault("first_seen", now)
        if not was_present:
            changes.added.append(_brief(executor_id, executor.cost, executor.trains))
        elif was_cost != executor.cost or was_trains != executor.trains:
            changes.changed.append(
                {
                    **_brief(executor_id, executor.cost, executor.trains),
                    "was_cost": was_cost,
                    "was_trains": was_trains,
                }
            )

    listed_ids = {f"{tool}:{model}" for tool, names in listed.items() for model in names}
    for executor_id in sorted(models):
        entry = models[executor_id]
        if executor_id in current or not isinstance(entry, dict):
            continue
        if not entry.get("present"):
            continue
        if _tool_of(executor_id) in listed and executor_id not in listed_ids:
            entry["present"] = False
            changes.removed.append(
                _brief(executor_id, entry.get("cost"), entry.get("trains"))
            )

    if previous is None or baseline_ids:
        changes.baseline = len(baseline_ids)
        if previous is not None:
            changes.baseline_tools = sorted(baseline_tools)

    return {"last_check": now, "models": models}, changes


def check_due(project: Path, hours: float, now: datetime | None = None) -> bool:
    """Whether the registry's ``last_check`` is ``hours`` or more old.

    Also true when the file is missing, unreadable or has no parseable
    ``last_check``, so a broken registry is reported instead of hidden. A
    naive timestamp is read as UTC. Never raises.
    """
    try:
        previous = load_seen(project)
    except RecordError:
        return True
    if previous is None:
        return True
    try:
        last = datetime.fromisoformat(str(previous.get("last_check")))
    except ValueError:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    return current - last >= timedelta(hours=hours)


def check_models(
    project: Path, discovery: Discovery, now: str | None = None
) -> ModelChanges:
    """Update ``models-seen.json`` from ``discovery`` and return the changes.

    An unreadable registry is never overwritten: the changes carry the
    reason in ``error`` and the file stays as it was.
    """
    stamp = now or datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        previous = load_seen(project)
    except RecordError as exc:
        return ModelChanges(error=str(exc))
    snapshot, changes = diff_models(
        previous, discovery.executors, discovery.listed, stamp
    )
    save_seen(project, snapshot)
    return changes


def _yes_no(value: object) -> str:
    return "yes" if value else "no"


def _tier_index(cost: object) -> int:
    """Position of ``cost`` on the ladder (``free`` first); others come last."""
    return TIER_ORDER.index(cost) if cost in TIER_ORDER else len(TIER_ORDER)


def render_changes(changes: ModelChanges) -> list[str]:
    """Human-readable lines for ``changes``, cheapest tier first.

    Any tier change is reported as ``now <tier>: <id> (was <tier>, ...)``,
    for example ``now cheap`` or ``now included`` after a rule moved a model
    off ``paid``.
    """
    if changes.error:
        return [f"models: not updated: {changes.error}"]

    lines: list[str] = []
    if changes.baseline is not None:
        line = f"models: baseline: {changes.baseline} executors recorded"
        if changes.baseline_tools:
            line += f" (new tool: {', '.join(changes.baseline_tools)})"
        lines.append(line)

    details: list[str] = []
    for item in sorted(changes.added, key=lambda c: _tier_index(c["cost"])):
        details.append(
            f"new {item['cost']}: {item['id']} "
            f"(trains={_yes_no(item['trains'])})"
        )
    cost_changes = [c for c in changes.changed if c["cost"] != c["was_cost"]]
    for item in sorted(
        cost_changes, key=lambda c: (_tier_index(c["cost"]), c["id"])
    ):
        details.append(
            f"now {item['cost']}: {item['id']} "
            f"(was {item['was_cost']}, trains={_yes_no(item['trains'])})"
        )
    for item in sorted(
        changes.removed, key=lambda c: (_tier_index(c["cost"]), c["id"])
    ):
        details.append(f"gone: {item['id']} (was {item['cost']})")
    for item in changes.changed:
        if item["cost"] == item["was_cost"]:
            details.append(
                f"trains changed: {item['id']} "
                f"(now trains={_yes_no(item['trains'])}, cost={item['cost']})"
            )

    if details:
        lines.append(f"models: changes since {changes.since}")
        lines.extend(details)
    elif changes.baseline is None:
        lines.append(f"models: no changes since {changes.since}")
    return lines


def changes_payload(changes: ModelChanges) -> dict:
    """The ``changes`` object of ``discover --json``."""
    return {
        "since": changes.since,
        "baseline": changes.baseline,
        "baseline_tools": list(changes.baseline_tools),
        "added": [dict(item) for item in changes.added],
        "removed": [dict(item) for item in changes.removed],
        "changed": [dict(item) for item in changes.changed],
        "error": changes.error,
    }
