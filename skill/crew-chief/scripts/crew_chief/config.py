"""Preferences loading, merging and validation for agent-crew-chief.

Preferences are read from an optional global file and from the project's
``docs/crew-chief/preferences.toml``. Scalar values are overridden key by key
(project over global over defaults); the ``[[tools]]`` and ``[[models]]``
tables are merged by ``name``/``match`` with the project winning.
"""

from __future__ import annotations

import fnmatch
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Fixed vocabularies (see global constraints).
COST_VALUES = ("free", "paid", "forbidden")
KIND_VALUES = ("claude-code", "opencode", "codex", "antigravity", "generic")
ISSUE_TRACKERS = ("none", "github", "plane", "linear", "other")

# Location of the project preferences file, relative to the project root.
PREFS_RELATIVE_PATH = Path("docs") / "crew-chief" / "preferences.toml"

#: Environment variable that overrides the location of the global file.
GLOBAL_PREFS_ENV = "AGENT_CREW_CHIEF_GLOBAL_PREFS"

DEFAULT_STRONG_ORDER = ["codex", "claude"]
DEFAULT_EXPLORE_RATE = 0.1
DEFAULT_DISPATCH_TIMEOUT_S = 900
#: Hours after which ``pick`` and ``dispatch`` re-check the models seen.
DEFAULT_MODEL_CHECK_HOURS = 24
#: Minutes an executor stays out of ``pick`` after a ``quota`` attempt.
DEFAULT_QUOTA_COOLDOWN_MIN = 60

#: Default tool definitions. Preferences merge over these by ``name``
#: (project over global over defaults).
DEFAULT_TOOLS: list[dict] = [
    {"name": "opencode", "kind": "opencode", "bin": "opencode"},
    {
        "name": "antigravity",
        "kind": "antigravity",
        "bin": "agy",
        "effort": True,
        "cost_default": "free",
    },
    {"name": "codex", "kind": "codex", "bin": "codex", "models": [], "effort": True},
    {
        "name": "claude",
        "kind": "claude-code",
        "bin": "claude",
        "models": ["sonnet", "opus", "haiku"],
    },
    {
        "name": "cursor",
        "kind": "generic",
        "bin": "cursor-agent",
        "models": [],
        "run_cmd": ["cursor-agent", "-p", "--force", "--model", "{model}", "{prompt}"],
    },
]


class PrefsError(Exception):
    """Raised when a preferences file is invalid; names the file and key/line."""


@dataclass
class Prefs:
    """Resolved preferences for one dispatch."""

    tools: list[dict]
    models: list[dict]
    strong_order: list[str]
    explore_rate: float
    forbid_training: bool
    sensitive_paths: list[str]
    issue_tracker: str
    dispatch_timeout_s: int
    review_default: str
    model_check_hours: float = DEFAULT_MODEL_CHECK_HOURS
    quota_cooldown_min: float = DEFAULT_QUOTA_COOLDOWN_MIN
    #: Extra home entries (fnmatch patterns) the containment check ignores, on
    #: top of ``worktree.AGENT_HOME_ENTRIES``; global and project lists add up.
    ignore_home_entries: list[str] = field(default_factory=list)


def _read_toml(path: Path) -> dict[str, Any]:
    """Read and parse one TOML file, converting errors to :class:`PrefsError`."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PrefsError(f"{path}: cannot read preferences file: {exc}") from exc
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        line = getattr(exc, "lineno", None)
        where = f"line {line}: " if line else ""
        raise PrefsError(f"{path}: invalid TOML at {where}{exc}") from exc
    return data


def _validate_tool(path: Path, tool: dict[str, Any]) -> None:
    name = tool.get("name")
    if not isinstance(name, str) or not name:
        raise PrefsError(f"{path}: [[tools]] entry is missing a 'name'")
    kind = tool.get("kind")
    if kind not in KIND_VALUES:
        raise PrefsError(
            f"{path}: tool {name!r}: invalid kind {kind!r} "
            f"(expected one of {', '.join(KIND_VALUES)})"
        )
    if kind == "generic":
        run_cmd = tool.get("run_cmd")
        if not isinstance(run_cmd, list) or not run_cmd:
            raise PrefsError(
                f"{path}: tool {name!r}: kind 'generic' requires a non-empty "
                f"'run_cmd' list"
            )


def _validate_model(path: Path, rule: dict[str, Any]) -> None:
    match = rule.get("match")
    if not isinstance(match, str) or not match:
        raise PrefsError(f"{path}: [[models]] entry is missing a 'match' pattern")
    cost = rule.get("cost")
    if cost not in COST_VALUES:
        raise PrefsError(
            f"{path}: [[models]] rule {match!r}: invalid cost {cost!r} "
            f"(expected one of {', '.join(COST_VALUES)})"
        )


def _validate_file(path: Path, data: dict[str, Any]) -> None:
    explore_rate = data.get("explore_rate")
    if explore_rate is not None:
        if isinstance(explore_rate, bool) or not isinstance(explore_rate, (int, float)):
            raise PrefsError(
                f"{path}: explore_rate must be a number, got {explore_rate!r}"
            )
        if not 0 <= explore_rate <= 1:
            raise PrefsError(
                f"{path}: explore_rate must be between 0 and 1, got {explore_rate!r}"
            )

    for key in ("model_check_hours", "quota_cooldown_min"):
        value = data.get(key)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise PrefsError(f"{path}: {key} must be a number, got {value!r}")
        if value < 0:
            raise PrefsError(f"{path}: {key} must be 0 or more, got {value!r}")

    ignore_home = data.get("ignore_home_entries")
    if ignore_home is not None and (
        not isinstance(ignore_home, list)
        or not all(isinstance(name, str) and name for name in ignore_home)
    ):
        raise PrefsError(
            f"{path}: ignore_home_entries must be a list of entry names, "
            f"got {ignore_home!r}"
        )

    issue_tracker = data.get("issue_tracker")
    if issue_tracker is not None and issue_tracker not in ISSUE_TRACKERS:
        raise PrefsError(
            f"{path}: issue_tracker must be one of {', '.join(ISSUE_TRACKERS)}, "
            f"got {issue_tracker!r}"
        )

    for tool in data.get("tools", []):
        _validate_tool(path, tool)
    for rule in data.get("models", []):
        _validate_model(path, rule)


def _merge_by_key(
    base: list[dict], override: list[dict], key: str
) -> list[dict]:
    """Merge two lists of tables by ``key``; entries in ``override`` win."""
    merged: dict[str, dict] = {entry[key]: entry for entry in base}
    for entry in override:
        merged[entry[key]] = entry
    return list(merged.values())


def _merge_models(
    global_models: list[dict], project_models: list[dict]
) -> list[dict]:
    """Merge model rules by ``match``; project rules win and are tried first."""
    seen: set[str] = set()
    merged: list[dict] = []
    for rule in [*project_models, *global_models]:
        match = rule["match"]
        if match in seen:
            continue
        seen.add(match)
        merged.append(rule)
    return merged


def default_global_path() -> Path:
    """Where the optional global preferences file lives by default.

    ``AGENT_CREW_CHIEF_GLOBAL_PREFS`` overrides the path; otherwise it is
    ``~/.config/agent-crew-chief/preferences.toml``.
    """
    override = os.environ.get(GLOBAL_PREFS_ENV)
    if override:
        return Path(override)
    return Path.home() / ".config" / "agent-crew-chief" / "preferences.toml"


def load_prefs(project: Path, global_path: Path | None = None) -> Prefs:
    """Load and merge preferences for ``project``.

    Reads the optional global file and the project's
    ``docs/crew-chief/preferences.toml``, validates both and returns the
    resolved :class:`Prefs`. ``global_path=None`` means
    :func:`default_global_path`; a missing global file is fine.
    """
    project = Path(project)
    project_file = project / PREFS_RELATIVE_PATH

    if global_path is None:
        global_path = default_global_path()
    global_data: dict[str, Any] = {}
    if global_path is not None:
        global_path = Path(global_path)
        if global_path.is_file():
            global_data = _read_toml(global_path)
            _validate_file(global_path, global_data)

    project_data: dict[str, Any] = {}
    if project_file.is_file():
        project_data = _read_toml(project_file)
        _validate_file(project_file, project_data)

    tools = _merge_by_key(
        _merge_by_key(DEFAULT_TOOLS, global_data.get("tools", []), "name"),
        project_data.get("tools", []),
        "name",
    )
    models = _merge_models(
        global_data.get("models", []), project_data.get("models", [])
    )

    def pick(key: str, default: Any) -> Any:
        if key in project_data:
            return project_data[key]
        if key in global_data:
            return global_data[key]
        return default

    return Prefs(
        tools=tools,
        models=models,
        strong_order=pick("strong_order", list(DEFAULT_STRONG_ORDER)),
        explore_rate=pick("explore_rate", DEFAULT_EXPLORE_RATE),
        forbid_training=pick("forbid_training", False),
        sensitive_paths=pick("sensitive_paths", []),
        issue_tracker=pick("issue_tracker", "none"),
        dispatch_timeout_s=pick("dispatch_timeout_s", DEFAULT_DISPATCH_TIMEOUT_S),
        review_default=pick("review_default", ""),
        model_check_hours=pick("model_check_hours", DEFAULT_MODEL_CHECK_HOURS),
        quota_cooldown_min=pick("quota_cooldown_min", DEFAULT_QUOTA_COOLDOWN_MIN),
        ignore_home_entries=list(
            dict.fromkeys(
                [
                    *global_data.get("ignore_home_entries", []),
                    *project_data.get("ignore_home_entries", []),
                ]
            )
        ),
    )


def classify(
    executor_id: str, prefs: Prefs, tool_default: str | None = None
) -> tuple[str, bool]:
    """Classify an executor id as ``(cost, trains)``.

    A ``cost = "forbidden"`` rule at any level (project or global) wins over
    every other match, so an operator can forbid an executor globally even
    when the project grants it. Otherwise the first matching ``[[models]]``
    rule (fnmatch on the executor id) wins. With no match the result is
    ``(tool_default or "paid", False)``. When ``prefs.forbid_training`` is set
    and the matched rule trains, the cost becomes ``"forbidden"``.
    """
    first: dict | None = None
    for rule in prefs.models:
        if not fnmatch.fnmatchcase(executor_id, rule["match"]):
            continue
        if rule["cost"] == "forbidden":
            return "forbidden", bool(rule.get("trains", False))
        if first is None:
            first = rule
    if first is None:
        return (tool_default or "paid"), False
    cost = first["cost"]
    trains = bool(first.get("trains", False))
    if prefs.forbid_training and trains:
        cost = "forbidden"
    return cost, trains
