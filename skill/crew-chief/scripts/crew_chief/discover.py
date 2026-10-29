"""Discovery of the executors that exist on this machine right now."""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field

from crew_chief.config import Prefs, classify
from crew_chief.tools import Executor, list_models, missing_env, tool_env


@dataclass
class Discovery:
    """Everything one discovery pass learned.

    ``listed`` maps the name of every tool whose model listing succeeded to
    all the models it listed, ``forbidden`` ones included. A tool skipped for
    a missing bin, a missing env var or an empty listing is absent from it,
    so callers can tell "the model is gone" from "the tool was not asked".
    """

    executors: list[Executor]
    notes: list[str]
    listed: dict[str, list[str]] = field(default_factory=dict)


def discover(prefs: Prefs) -> tuple[list[Executor], list[str]]:
    """Executors available now, plus notes for every skipped tool.

    A tool is available when its ``bin`` is on PATH via ``shutil.which``,
    its declared env vars are satisfied and at least one of its models
    is not classified as ``forbidden``. Each executor's cost is
    ``classify("<tool>:<model>", prefs, cost_default)``.
    """
    result = discover_full(prefs)
    return result.executors, result.notes


def discover_full(prefs: Prefs) -> Discovery:
    """Like :func:`discover`, plus the models each listed tool advertised."""
    executors: list[Executor] = []
    notes: list[str] = []
    listed: dict[str, list[str]] = {}

    for tool in prefs.tools:
        name = tool["name"]

        if shutil.which(tool["bin"]) is None:
            notes.append(f"{name}: bin '{tool['bin']}' not found on PATH")
            continue

        env = tool_env(tool)
        if env is None:
            missing = ", ".join(missing_env(tool))
            notes.append(f"{name}: missing required env var {missing}")
            continue

        models = list_models(tool)
        if not models:
            notes.append(f"{name}: no models listed")
            continue
        listed[name] = list(models)

        blocked = 0
        for model in models:
            executor_id = f"{name}:{model}"
            cost, trains = classify(executor_id, prefs, tool.get("cost_default"))
            if cost == "forbidden":
                blocked += 1
                continue
            executors.append(
                Executor(
                    id=executor_id, tool=tool, model=model, cost=cost, trains=trains
                )
            )
        if blocked == len(models):
            notes.append(f"{name}: all models are forbidden")

    return Discovery(executors=executors, notes=notes, listed=listed)
