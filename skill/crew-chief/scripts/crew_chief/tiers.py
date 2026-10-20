"""Built-in cost-tier patterns: the last stop before a tool's ``cost_default``.

Executor ids look like ``<tool>:<model>``. The rules below are matched with
fnmatch, first match wins, and use the same shape as a ``[[models]]`` entry
(``match``, ``cost`` and optionally ``trains``). ``acc setup`` promotes the
subscription CLIs from ``paid`` to ``included`` when the user has a plan.
Kept as plain data so it is easy to update.
"""

from __future__ import annotations

#: The cost ladder, cheapest first. ``forbidden`` is outside the ladder.
TIER_ORDER = ("free", "included", "cheap", "paid")

BUILTIN_RULES: tuple[dict, ...] = (
    {"match": "*-preview-free", "cost": "free", "trains": True},
    {"match": "*contributor*", "cost": "free", "trains": True},
    {"match": "opencode:opencode/*-free", "cost": "free"},
    {"match": "opencode:opencode-go/*-free", "cost": "free"},
    {"match": "opencode:openrouter/*:free", "cost": "free"},
    {"match": "antigravity:*", "cost": "free"},
    {"match": "opencode:openrouter/*", "cost": "cheap"},
    {"match": "opencode:ghostcli/*", "cost": "paid"},
    {"match": "codex:*", "cost": "paid"},
    {"match": "claude:*", "cost": "paid"},
)
