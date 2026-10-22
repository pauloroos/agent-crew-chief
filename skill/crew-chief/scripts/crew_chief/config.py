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

from . import central_config
from . import tiers
from .records import WORK_TYPES

# Fixed vocabularies (see global constraints).
#: Cost ladder, cheapest first; ``forbidden`` stays outside it.
TIER_ORDER = tiers.TIER_ORDER
COST_VALUES = (*TIER_ORDER, "forbidden")
KIND_VALUES = ("claude-code", "opencode", "codex", "antigravity", "generic")
ISSUE_TRACKERS = ("none", "github", "plane", "linear", "other")
CHANGELOG_VALUES = ("keep-a-changelog", "none")

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
#: Economy alerts (``0`` turns each off): coordinator share of the tokens,
#: attempts per piece and minutes a job may take before ``close`` warns.
DEFAULT_ALERT_COORDINATOR_SHARE = 0.5
DEFAULT_ALERT_ATTEMPTS_PER_PIECE = 2.5
DEFAULT_ALERT_JOB_MINUTES = 180

#: Central coordination scalars (001-central). An empty
#: ``coordinator_default`` falls back to the calling CLI (``CREW_CHIEF_COORDINATOR``)
#: when a launch resolves its coordinator; the timings must be finite and
#: positive; ``central_backend`` labels the launch in the central registry.
DEFAULT_COORDINATOR_DEFAULT = ""
DEFAULT_COORDINATOR_UNRESTRICTED = True
DEFAULT_CENTRAL_POLL_S = 1.0
DEFAULT_CENTRAL_ROUND_TIMEOUT_S = 900
DEFAULT_CENTRAL_BACKEND = "default"
#: Minutes without job progress *and* child output before a central round is
#: considered stalled (``0`` is invalid, unlike the economy alerts), and the
#: policy applied on a coordinator quota or stall: ``ask``, ``wait`` or
#: ``handoff:<cli>``.
DEFAULT_STALL_MINUTES = 40
DEFAULT_ON_COORDINATOR_QUOTA = "ask"

#: Markers of the block ``acc setup`` manages in the global preferences file.
SETUP_BEGIN = "# >>> acc setup >>>"
SETUP_END = "# <<< acc setup <<<"

#: Tools whose free executors come first on execution work (``exec_first``).
DEFAULT_EXEC_FIRST = ["opencode"]

#: ``close`` adds the job to the project CHANGELOG in the Keep a Changelog style.
DEFAULT_CHANGELOG = "keep-a-changelog"
#: The project CHANGELOG, relative to the project root.
DEFAULT_CHANGELOG_PATH = "CHANGELOG.md"

#: Work types each tool kind takes when the tool sets no ``work_types``.
#: Every kind takes every type: any free model may execute, review or think,
#: and the execute/think split lives in ``scoring.pick`` (``exec_first`` and
#: no automatic paid escalation on ``records.EXEC_TYPES``). A tool narrows
#: itself with an explicit ``work_types`` list. See :func:`tool_work_types`.
DEFAULT_WORK_TYPES_BY_KIND: dict[str, tuple[str, ...]] = {
    "opencode": WORK_TYPES,
    "antigravity": WORK_TYPES,
    "claude-code": WORK_TYPES,
    "codex": WORK_TYPES,
    "generic": WORK_TYPES,
}

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
    #: Economy alert limits; ``0`` disables the alert (see ``economy.py``).
    alert_coordinator_share: float = DEFAULT_ALERT_COORDINATOR_SHARE
    alert_attempts_per_piece: float = DEFAULT_ALERT_ATTEMPTS_PER_PIECE
    alert_job_minutes: float = DEFAULT_ALERT_JOB_MINUTES
    #: Extra home entries (fnmatch patterns) the containment check ignores, on
    #: top of ``worktree.AGENT_HOME_ENTRIES``; global and project lists add up.
    ignore_home_entries: list[str] = field(default_factory=list)
    #: Tools whose free executors are tried first on execution work types.
    exec_first: list[str] = field(default_factory=lambda: list(DEFAULT_EXEC_FIRST))
    #: ``keep-a-changelog`` (``close`` writes the entry) or ``none``.
    changelog: str = DEFAULT_CHANGELOG
    #: Path of the project CHANGELOG, relative to the project root.
    changelog_path: str = DEFAULT_CHANGELOG_PATH
    #: Paid-by-use spending limits in US dollars (``0`` = no cheap models).
    cheap_budget_usd_day: float = 0.0
    cheap_budget_usd_job: float = 0.0
    #: ``[[models]]`` rules of the ``acc setup`` block (global file), kept
    #: apart: they rank below the user's own rules and above the built-ins.
    setup_models: list[dict] = field(default_factory=list)
    #: Whether :data:`tiers.BUILTIN_RULES` take part in :func:`classify`.
    builtin_tiers: bool = True
    #: Central coordination scalars (001-central). An empty
    #: ``coordinator_default`` falls back to the calling CLI; timings must be
    #: finite and positive; ``central_backend`` labels the launch.
    coordinator_default: str = DEFAULT_COORDINATOR_DEFAULT
    coordinator_unrestricted: bool = DEFAULT_COORDINATOR_UNRESTRICTED
    central_poll_s: float = DEFAULT_CENTRAL_POLL_S
    central_round_timeout_s: float = DEFAULT_CENTRAL_ROUND_TIMEOUT_S
    central_backend: str = DEFAULT_CENTRAL_BACKEND
    #: Minutes without job progress and child output before a round is stalled,
    #: and the policy on a coordinator quota or stall (``ask``, ``wait`` or
    #: ``handoff:<cli>``). Both are validated by ``central_config``.
    stall_minutes: float = DEFAULT_STALL_MINUTES
    on_coordinator_quota: str = DEFAULT_ON_COORDINATOR_QUOTA


def tool_work_types(tool: dict) -> tuple[str, ...]:
    """Work types ``tool`` takes, in :data:`WORK_TYPES` order.

    An explicit ``work_types`` list wins, and an empty one means every type;
    a tool without the key falls back to :data:`DEFAULT_WORK_TYPES_BY_KIND`
    for its kind (all types for a kind not listed there).
    """
    explicit = tool.get("work_types")
    if explicit is not None:
        if not explicit:
            return WORK_TYPES
        return tuple(work_type for work_type in WORK_TYPES if work_type in explicit)
    return DEFAULT_WORK_TYPES_BY_KIND.get(tool.get("kind"), WORK_TYPES)


def _read_toml(path: Path, split_setup: bool = False) -> Any:
    """Read and parse one TOML file, converting errors to :class:`PrefsError`.

    With ``split_setup`` the managed ``acc setup`` block is cut out of the
    text (its lines are blanked so error line numbers stay right) and the
    result is ``(data, setup_data)``; ``setup_data`` is ``{}`` without a block.
    """
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise PrefsError(f"{path}: cannot read preferences file: {exc}") from exc
    if split_setup:
        rest, block = _split_setup_block(text)
        data = _parse_toml(path, rest)
        setup_data = _parse_toml(path, block) if block is not None else {}
        return data, setup_data
    return _parse_toml(path, text)


def _split_setup_block(text: str) -> tuple[str, str | None]:
    """Cut the first complete ``acc setup`` block out of ``text``.

    Returns ``(rest, block)``: ``rest`` keeps one blank line per line removed,
    ``block`` is the text between the markers (``None`` when there is no
    complete block; a lone marker is then just a TOML comment).
    """
    lines = text.splitlines(keepends=True)
    begin = next(
        (i for i, line in enumerate(lines) if line.strip() == SETUP_BEGIN), None
    )
    if begin is None:
        return text, None
    end = next(
        (i for i in range(begin + 1, len(lines)) if lines[i].strip() == SETUP_END),
        None,
    )
    if end is None:
        return text, None
    block = "".join(lines[begin + 1 : end])
    blanks = ["\n"] * (end - begin + 1)
    return "".join([*lines[:begin], *blanks, *lines[end + 1 :]]), block


def _parse_toml(path: Path, text: str) -> dict[str, Any]:
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
    work_types = tool.get("work_types")
    if work_types is not None:
        if not isinstance(work_types, list):
            raise PrefsError(
                f"{path}: tool {name!r}: work_types must be a list of work "
                f"types, got {work_types!r}"
            )
        for work_type in work_types:
            if work_type not in WORK_TYPES:
                raise PrefsError(
                    f"{path}: tool {name!r}: unknown work type {work_type!r} in "
                    f"work_types (expected one of {', '.join(WORK_TYPES)})"
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
    cap = rule.get("cap_usd_day")
    if cap is not None:
        if isinstance(cap, bool) or not isinstance(cap, (int, float)) or cap < 0:
            raise PrefsError(
                f"{path}: [[models]] rule {match!r}: cap_usd_day must be a "
                f"number of 0 or more, got {cap!r}"
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

    for key in (
        "model_check_hours",
        "quota_cooldown_min",
        "alert_coordinator_share",
        "alert_attempts_per_piece",
        "alert_job_minutes",
        "cheap_budget_usd_day",
        "cheap_budget_usd_job",
    ):
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

    builtin_tiers = data.get("builtin_tiers")
    if builtin_tiers is not None and not isinstance(builtin_tiers, bool):
        raise PrefsError(
            f"{path}: builtin_tiers must be true or false, got {builtin_tiers!r}"
        )

    exec_first = data.get("exec_first")
    if exec_first is not None and (
        not isinstance(exec_first, list)
        or not all(isinstance(name, str) and name for name in exec_first)
    ):
        raise PrefsError(
            f"{path}: exec_first must be a list of tool names, got {exec_first!r}"
        )

    changelog = data.get("changelog")
    if changelog is not None and changelog not in CHANGELOG_VALUES:
        raise PrefsError(
            f"{path}: changelog must be one of {', '.join(CHANGELOG_VALUES)}, "
            f"got {changelog!r}"
        )
    changelog_path = data.get("changelog_path")
    if changelog_path is not None and (
        not isinstance(changelog_path, str) or not changelog_path.strip()
    ):
        raise PrefsError(
            f"{path}: changelog_path must be a non-empty path, "
            f"got {changelog_path!r}"
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

    # Central coordination scalars are validated through the accepted
    # central_config.resolve_settings interface (injected supported fallback
    # caller, validation only), and the continuity scalars through
    # resolve_continuity_settings.
    try:
        central_config.resolve_settings(data, {}, {}, "claude")
        central_config.resolve_continuity_settings(data, {}, {})
    except central_config.CentralConfigError as exc:
        raise PrefsError(f"{path}: {exc}") from exc


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
    setup_data: dict[str, Any] = {}
    if global_path is not None:
        global_path = Path(global_path)
        if global_path.is_file():
            global_data, setup_data = _read_toml(global_path, split_setup=True)
            _validate_file(global_path, global_data)
            _validate_file(global_path, setup_data)

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
        if key in setup_data:
            return setup_data[key]
        return default

    continuity = central_config.resolve_continuity_settings({}, {}, {
        "stall_minutes": pick("stall_minutes", DEFAULT_STALL_MINUTES),
        "on_coordinator_quota": pick("on_coordinator_quota", DEFAULT_ON_COORDINATOR_QUOTA),
    })

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
        alert_coordinator_share=pick(
            "alert_coordinator_share", DEFAULT_ALERT_COORDINATOR_SHARE
        ),
        alert_attempts_per_piece=pick(
            "alert_attempts_per_piece", DEFAULT_ALERT_ATTEMPTS_PER_PIECE
        ),
        alert_job_minutes=pick("alert_job_minutes", DEFAULT_ALERT_JOB_MINUTES),
        exec_first=list(pick("exec_first", DEFAULT_EXEC_FIRST)),
        changelog=pick("changelog", DEFAULT_CHANGELOG),
        changelog_path=pick("changelog_path", DEFAULT_CHANGELOG_PATH),
        cheap_budget_usd_day=float(pick("cheap_budget_usd_day", 0.0)),
        cheap_budget_usd_job=float(pick("cheap_budget_usd_job", 0.0)),
        setup_models=list(setup_data.get("models", [])),
        builtin_tiers=pick("builtin_tiers", True),
        coordinator_default=pick("coordinator_default", DEFAULT_COORDINATOR_DEFAULT),
        coordinator_unrestricted=pick(
            "coordinator_unrestricted", DEFAULT_COORDINATOR_UNRESTRICTED
        ),
        central_poll_s=pick("central_poll_s", DEFAULT_CENTRAL_POLL_S),
        central_round_timeout_s=pick(
            "central_round_timeout_s", DEFAULT_CENTRAL_ROUND_TIMEOUT_S
        ),
        central_backend=pick("central_backend", DEFAULT_CENTRAL_BACKEND),
        stall_minutes=continuity["stall_minutes"],
        on_coordinator_quota=continuity["on_coordinator_quota"],
        ignore_home_entries=list(
            dict.fromkeys(
                [
                    *setup_data.get("ignore_home_entries", []),
                    *global_data.get("ignore_home_entries", []),
                    *project_data.get("ignore_home_entries", []),
                ]
            )
        ),
    )


def _rule_layers(prefs: Prefs) -> list[list[dict]]:
    """Rule lists in precedence order: user, setup block, built-ins."""
    layers = [prefs.models, prefs.setup_models]
    if prefs.builtin_tiers:
        layers.append(list(tiers.BUILTIN_RULES))
    return layers


def _first_match(executor_id: str, prefs: Prefs) -> dict | None:
    for layer in _rule_layers(prefs):
        for rule in layer:
            if fnmatch.fnmatchcase(executor_id, rule["match"]):
                return rule
    return None


def classify(
    executor_id: str, prefs: Prefs, tool_default: str | None = None
) -> tuple[str, bool]:
    """Classify an executor id as ``(cost, trains)``.

    A ``cost = "forbidden"`` rule in any layer wins over every other match, so
    an operator can forbid an executor globally even when the project grants
    it. Otherwise the first matching rule (fnmatch on the executor id) wins:
    the user's own ``[[models]]`` (project, then global outside the setup
    block), then the ``acc setup`` block, then :data:`tiers.BUILTIN_RULES`.
    With no match the result is ``(tool_default or "paid", False)``. When
    ``prefs.forbid_training`` is set and the matched rule trains, the cost
    becomes ``"forbidden"``.
    """
    for layer in _rule_layers(prefs):
        for rule in layer:
            if rule["cost"] == "forbidden" and fnmatch.fnmatchcase(
                executor_id, rule["match"]
            ):
                return "forbidden", bool(rule.get("trains", False))
    first = _first_match(executor_id, prefs)
    if first is None:
        return (tool_default or "paid"), False
    cost = first["cost"]
    trains = bool(first.get("trains", False))
    if prefs.forbid_training and trains:
        cost = "forbidden"
    return cost, trains


def model_cap(executor_id: str, prefs: Prefs) -> float | None:
    """``cap_usd_day`` of the first rule matching ``executor_id``, else ``None``.

    Uses the same precedence as :func:`classify`.
    """
    rule = _first_match(executor_id, prefs)
    if rule is None or rule.get("cap_usd_day") is None:
        return None
    return float(rule["cap_usd_day"])
