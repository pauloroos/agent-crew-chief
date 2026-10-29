"""Preferences loading, merging and validation for agent-crew-chief.

Preferences are read from an optional global file and from the project's
``docs/crew-chief/preferences.toml``. Scalar values are overridden key by key
(project over global over defaults); the ``[[tools]]`` and ``[[models]]``
tables are merged by ``name``/``match`` with the project winning.
"""

from __future__ import annotations

import fnmatch
import json
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import tiers
from .records import WORK_TYPES

# Fixed vocabularies (see global constraints).
#: Cost ladder, cheapest first; ``forbidden`` stays outside it.
TIER_ORDER = tiers.TIER_ORDER
COST_VALUES = (*TIER_ORDER, "forbidden")
KIND_VALUES = ("claude-code", "opencode", "codex", "antigravity", "generic")
ISSUE_TRACKERS = ("none", "github", "plane", "linear", "other")
CHANGELOG_VALUES = ("keep-a-changelog", "none")
#: How much Ponytail the briefs carry; see ``vendor/ponytail``.
PONYTAIL_LEVELS = ("lite", "full", "ultra", "off")
#: RTK command-prefix values.
RTK_VALUES = ("auto", "on", "off")
#: Allowed vendored Superpowers skill names and modes.
ALLOWED_SUPERPOWERS_SKILLS = (
    "test-driven-development",
    "systematic-debugging",
    "verification-before-completion",
)
SUPERPOWERS_VALUES = ("off", "auto", *ALLOWED_SUPERPOWERS_SKILLS)
#: Executor choice modes for ``executors_default`` (Phase 4 / FR-039).
EXECUTORS_DEFAULT_VALUES = ("own", "subagents", "free_first", "ask")
DEFAULT_EXECUTORS_DEFAULT = "ask"

# Location of the project preferences file, relative to the project root.
PREFS_RELATIVE_PATH = Path("docs") / "crew-chief" / "preferences.toml"

#: Environment variable that overrides the location of the global file.
GLOBAL_PREFS_ENV = "AGENT_CREW_CHIEF_GLOBAL_PREFS"

DEFAULT_STRONG_ORDER = ["codex", "claude"]
DEFAULT_EXPLORE_RATE = 0.1
DEFAULT_DISPATCH_TIMEOUT_S = 900
DEFAULT_MAX_RUNNING_EXECUTORS = 3
#: Hours after which ``pick`` and ``dispatch`` re-check the models seen.
DEFAULT_MODEL_CHECK_HOURS = 24
#: Minutes an executor stays out of ``pick`` after a ``quota`` attempt.
DEFAULT_QUOTA_COOLDOWN_MIN = 60
#: Economy alerts (``0`` turns each off): coordinator share of the tokens,
#: attempts per piece and minutes a job may take before ``close`` warns.
DEFAULT_ALERT_COORDINATOR_SHARE = 0.5
DEFAULT_ALERT_ATTEMPTS_PER_PIECE = 2.5
DEFAULT_ALERT_JOB_MINUTES = 180

#: Seconds one coordinator round may run before it is stopped.
DEFAULT_COORDINATOR_TIMEOUT_S = 4 * 3600

#: Markers of the block ``acc setup`` manages in the global preferences file.
SETUP_BEGIN = "# >>> acc setup >>>"
SETUP_END = "# <<< acc setup <<<"

#: Tools whose free executors come first on execution work (``exec_first``).
DEFAULT_EXEC_FIRST = ["opencode"]

#: ``close`` adds the job to the project CHANGELOG in the Keep a Changelog style.
DEFAULT_CHANGELOG = "keep-a-changelog"
#: Ponytail rules that go into every brief: lite, full or ultra (``off`` none).
DEFAULT_PONYTAIL_LEVEL = "full"
#: rtk command-prefix mode: auto (active if on PATH), on, or off.
DEFAULT_RTK = "auto"
#: Superpowers mode or skill: off (default), auto, or an allowed skill name.
DEFAULT_SUPERPOWERS = "off"
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


class Team(dict):
    """Resolved team table with dict access and attribute properties."""

    def __init__(self, *args, origins: dict[str, str] | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.setdefault("coordinators", [])
        self.setdefault("executors", [])
        self.setdefault("reviewers", [])
        self._origins: dict[str, str] = dict(origins or {})

    def origin(self, role: str) -> str:
        return self._origins.get(role, "")

    @property
    def origins(self) -> dict[str, str]:
        return self._origins

    @property
    def coordinators(self) -> list[str]:
        return self.get("coordinators", [])

    @property
    def executors(self) -> list[str]:
        return self.get("executors", [])

    @property
    def reviewers(self) -> list[str]:
        return self.get("reviewers", [])


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
    #: Ponytail rules the brief carries: lite, full or ultra (``off`` = none).
    ponytail_level: str = DEFAULT_PONYTAIL_LEVEL
    #: rtk command prefix setting: auto, on, or off.
    rtk: str = DEFAULT_RTK
    #: Superpowers skill injection: off, auto, or an allowed skill name.
    superpowers: str = DEFAULT_SUPERPOWERS
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
    #: Seconds one coordinator round may run (``acc launch``).
    coordinator_timeout_s: int = DEFAULT_COORDINATOR_TIMEOUT_S
    #: Executor choice default when no CLI flag or job record supplies one
    #: (Phase 4 / FR-039). One of ``own``, ``subagents``, ``free_first`` or
    #: ``ask``; ``ask`` means an interactive coordinator prompts once at job
    #: start and records the answer via ``job set``, while a non-interactive
    #: coordinator falls back to ``free_first`` and records that it did.
    executors_default: str = DEFAULT_EXECUTORS_DEFAULT
    max_running_executors: int = DEFAULT_MAX_RUNNING_EXECUTORS
    executor_lease_dir: str | None = None
    #: Per-project team table (T039 / US10 / FR-037).
    team: Team = field(default_factory=lambda: Team())
    team_coordinators: list[str] = field(default_factory=list)
    team_executors: list[str] = field(default_factory=list)
    team_reviewers: list[str] = field(default_factory=list)
    team_origins: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.team, Team):
            self.team = Team(self.team or {}, origins=self.team_origins)
        elif self.team_origins and not self.team.origins:
            self.team._origins = dict(self.team_origins)
        if "coordinators" not in self.team or not self.team["coordinators"]:
            if self.team_coordinators:
                self.team["coordinators"] = list(self.team_coordinators)
        if not self.team_coordinators and self.team.get("coordinators"):
            self.team_coordinators = list(self.team["coordinators"])

        if "executors" not in self.team or not self.team["executors"]:
            if self.team_executors:
                self.team["executors"] = list(self.team_executors)
        if not self.team_executors and self.team.get("executors"):
            self.team_executors = list(self.team["executors"])

        if "reviewers" not in self.team or not self.team["reviewers"]:
            if self.team_reviewers:
                self.team["reviewers"] = list(self.team_reviewers)
        if not self.team_reviewers and self.team.get("reviewers"):
            self.team_reviewers = list(self.team["reviewers"])

    @property
    def coordinators(self) -> list[str]:
        return self.team_coordinators

    @property
    def executors(self) -> list[str]:
        return self.team_executors

    @property
    def reviewers(self) -> list[str]:
        return self.team_reviewers


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


def _validate_cli_model_entry(path: Path, section: str, entry: Any) -> tuple[str, str]:
    if not isinstance(entry, str):
        raise PrefsError(
            f"{path}: {section} entry must be a 'cli:model' string, got {entry!r}"
        )
    cli, sep, model = entry.partition(":")
    cli = cli.strip()
    model = model.strip()
    if not sep or not cli or not model:
        raise PrefsError(
            f"{path}: {section} entry {entry!r} must be in 'cli:model' format"
        )
    return cli, model


def _get_known_tools(path: Path, data: dict[str, Any]) -> set[str]:
    names = {tool["name"] for tool in DEFAULT_TOOLS}
    names.add("claude-ghost")
    for tool in data.get("tools", []):
        if isinstance(tool, dict) and tool.get("name"):
            names.add(str(tool["name"]))
    seen_path = path.parent / "models-seen.json"
    if seen_path.is_file():
        try:
            seen_text = seen_path.read_text(encoding="utf-8")
            seen_data = json.loads(seen_text)
            if isinstance(seen_data, dict):
                for key in seen_data.get("models", {}).keys():
                    tool_name = key.partition(":")[0]
                    if tool_name:
                        names.add(tool_name)
        except Exception:
            pass
    return names


def _validate_team(path: Path, data: dict[str, Any]) -> None:
    if "team" in data:
        team = data["team"]
        if not isinstance(team, dict):
            raise PrefsError(f"{path}: [team] must be a table, got {team!r}")
        known_tools = _get_known_tools(path, data)

        for key in ("coordinators", "executors", "reviewers"):
            if key in team:
                items = team[key]
                if not isinstance(items, list):
                    raise PrefsError(
                        f"{path}: [team].{key} must be a list, got {items!r}"
                    )
                for entry in items:
                    cli, model = _validate_cli_model_entry(path, f"[team].{key}", entry)
                    if cli not in known_tools:
                        raise PrefsError(
                            f"{path}: [team].{key}: unknown tool {cli!r} in entry {entry!r}"
                        )



def _validate_file(
    path: Path, data: dict[str, Any], is_project: bool = False
) -> None:
    if is_project:
        if "max_running_executors" in data:
            raise PrefsError(
                f"{path}: 'max_running_executors' is a machine-level setting and "
                "cannot be configured in project preferences; configure it in "
                "global preferences (~/.config/agent-crew-chief/preferences.toml) instead"
            )
        if "executor_lease_dir" in data:
            raise PrefsError(
                f"{path}: 'executor_lease_dir' is a machine-level setting and "
                "cannot be configured in project preferences; configure it in "
                "global preferences (~/.config/agent-crew-chief/preferences.toml) instead"
            )

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

    executors_default = data.get("executors_default")
    if executors_default is not None and executors_default not in EXECUTORS_DEFAULT_VALUES:
        raise PrefsError(
            f"{path}: executors_default must be one of "
            f"{', '.join(EXECUTORS_DEFAULT_VALUES)}, got {executors_default!r}"
        )

    max_running_executors = data.get("max_running_executors")
    if max_running_executors is not None:
        if isinstance(max_running_executors, bool) or not isinstance(max_running_executors, int):
            raise PrefsError(
                f"{path}: max_running_executors must be an integer, got {max_running_executors!r}"
            )
        if max_running_executors < 1:
            raise PrefsError(
                f"{path}: max_running_executors must be 1 or more, got {max_running_executors!r}"
            )

    coordinator_timeout = data.get("coordinator_timeout_s")
    if coordinator_timeout is not None and (
        isinstance(coordinator_timeout, bool)
        or not isinstance(coordinator_timeout, int)
        or coordinator_timeout < 1
    ):
        raise PrefsError(
            f"{path}: coordinator_timeout_s must be a positive integer, got {coordinator_timeout!r}"
        )

    executor_lease_dir = data.get("executor_lease_dir")
    if executor_lease_dir is not None and (
        not isinstance(executor_lease_dir, str) or not executor_lease_dir.strip()
    ):
        raise PrefsError(
            f"{path}: executor_lease_dir must be a non-empty string, got {executor_lease_dir!r}"
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

    ponytail_level = data.get("ponytail_level")
    if ponytail_level is not None and ponytail_level not in PONYTAIL_LEVELS:
        raise PrefsError(
            f"{path}: ponytail_level must be one of "
            f"{', '.join(PONYTAIL_LEVELS)}, got {ponytail_level!r}"
        )

    rtk = data.get("rtk")
    if rtk is not None and rtk not in RTK_VALUES:
        raise PrefsError(
            f"{path}: rtk must be one of "
            f"{', '.join(RTK_VALUES)}, got {rtk!r}"
        )

    superpowers = data.get("superpowers")
    if superpowers is not None and superpowers not in SUPERPOWERS_VALUES:
        raise PrefsError(
            f"{path}: superpowers must be one of "
            f"{', '.join(SUPERPOWERS_VALUES)}, got {superpowers!r}"
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

    _validate_team(path, data)


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


def prefs_path(project: Path | str) -> Path:
    """Location of the project preferences file."""
    return Path(project) / PREFS_RELATIVE_PATH


def global_prefs_path() -> Path:
    """Location of the global preferences file."""
    return default_global_path()


def team_origin(
    project: Path | str | None,
    section: str,
    global_path: Path | str | None = None,
) -> str:
    """Determine team origin ('project_team', 'global_team', or '') for a section."""
    if project is not None:
        pfile = prefs_path(project)
        if pfile.is_file():
            data = _read_toml(pfile)
            team = data.get("team")
            if isinstance(team, dict) and team.get(section):
                return "project_team"
    gfile = Path(global_path) if global_path is not None else global_prefs_path()
    if gfile.is_file():
        gdata = _read_toml(gfile)
        gteam = gdata.get("team")
        if isinstance(gteam, dict) and gteam.get(section):
            return "global_team"
    return ""


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
        _validate_file(project_file, project_data, is_project=True)

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

    def pick_machine(key: str, default: Any) -> Any:
        env_val = os.environ.get(f"AGENT_CREW_CHIEF_{key.upper()}") or os.environ.get(f"ACC_{key.upper()}")
        if env_val is not None:
            return env_val
        if key in global_data:
            return global_data[key]
        if key in setup_data:
            return setup_data[key]
        return default

    proj_team = project_data.get("team") if isinstance(project_data.get("team"), dict) else {}
    glob_team = global_data.get("team") if isinstance(global_data.get("team"), dict) else {}

    coordinators_origin = ""
    coordinators: list[str] = []
    if "coordinators" in proj_team and proj_team["coordinators"] is not None:
        coordinators = list(proj_team["coordinators"])
        coordinators_origin = "project_team"
    elif "coordinators" in glob_team and glob_team["coordinators"] is not None:
        coordinators = list(glob_team["coordinators"])
        coordinators_origin = "global_team"

    executors_origin = ""
    executors: list[str] = []
    if "executors" in proj_team and proj_team["executors"] is not None:
        executors = list(proj_team["executors"])
        executors_origin = "project_team"
    elif "executors" in glob_team and glob_team["executors"] is not None:
        executors = list(glob_team["executors"])
        executors_origin = "global_team"

    reviewers_origin = ""
    reviewers: list[str] = []
    if "reviewers" in proj_team and proj_team["reviewers"] is not None:
        reviewers = list(proj_team["reviewers"])
        reviewers_origin = "project_team"
    elif "reviewers" in glob_team and glob_team["reviewers"] is not None:
        reviewers = list(glob_team["reviewers"])
        reviewers_origin = "global_team"

    team_origins = {
        "coordinators": coordinators_origin,
        "executors": executors_origin,
        "reviewers": reviewers_origin,
    }

    resolved_team = Team(
        {
            "coordinators": coordinators,
            "executors": executors,
            "reviewers": reviewers,
        },
        origins=team_origins,
    )

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
        ponytail_level=pick("ponytail_level", DEFAULT_PONYTAIL_LEVEL),
        rtk=pick("rtk", DEFAULT_RTK),
        superpowers=pick("superpowers", DEFAULT_SUPERPOWERS),
        changelog_path=pick("changelog_path", DEFAULT_CHANGELOG_PATH),
        cheap_budget_usd_day=float(pick("cheap_budget_usd_day", 0.0)),
        cheap_budget_usd_job=float(pick("cheap_budget_usd_job", 0.0)),
        setup_models=list(setup_data.get("models", [])),
        builtin_tiers=pick("builtin_tiers", True),
        coordinator_timeout_s=int(pick("coordinator_timeout_s", DEFAULT_COORDINATOR_TIMEOUT_S)),
        executors_default=pick("executors_default", DEFAULT_EXECUTORS_DEFAULT),
        max_running_executors=int(pick_machine("max_running_executors", DEFAULT_MAX_RUNNING_EXECUTORS)),
        executor_lease_dir=pick_machine("executor_lease_dir", None),
        team=resolved_team,
        team_coordinators=coordinators,
        team_executors=executors,
        team_reviewers=reviewers,
        team_origins=team_origins,
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
