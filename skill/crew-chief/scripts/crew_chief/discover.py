"""Discovery of the executors that exist on this machine right now."""

from __future__ import annotations

import os
import shutil

from crew_chief.config import Prefs, classify
from crew_chief.tools import Executor, list_models, tool_env


def discover(prefs: Prefs) -> tuple[list[Executor], list[str]]:
    """Executors available now, plus notes for every skipped tool.

    A tool is available when its ``bin`` is on PATH via ``shutil.which``,
    its declared env vars are satisfied and at least one of its models
    is not classified as ``forbidden``. Each executor's cost is
    ``classify("<tool>:<model>", prefs, cost_default)``.
    """
    executors: list[Executor] = []
    notes: list[str] = []

    for tool in prefs.tools:
        name = tool["name"]

        if shutil.which(tool["bin"]) is None:
            notes.append(f"{name}: bin '{tool['bin']}' not found on PATH")
            continue

        env = tool_env(tool)
        if env is None:
            missing = ", ".join(
                var for var in tool.get("env", []) if var not in os.environ
            )
            notes.append(f"{name}: missing required env var {missing}")
            continue

        models = list_models(tool)
        if not models:
            notes.append(f"{name}: no models listed")
            continue

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

    return executors, notes
