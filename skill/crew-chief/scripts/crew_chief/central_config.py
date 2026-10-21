"""Central configuration resolution for agent-crew-chief.

This module resolves the central registry root and the coordinator settings
used by a launch. It performs no I/O: callers inject the home directory, the
environment and preference dictionaries.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

#: Environment variable naming the central registry root.
CENTRAL_DIR_ENV = "AGENT_CREW_CHIEF_CENTRAL_DIR"
#: Environment variable naming the calling CLI.
COORDINATOR_ENV = "CREW_CHIEF_COORDINATOR"

#: Supported coordinator CLIs, in canonical form.
SUPPORTED_COORDINATORS = ("claude", "codex")

#: Aliases accepted for each supported coordinator.
_COORDINATOR_ALIASES = {
    "claude": "claude",
    "claude-code": "claude",
    "claude_code": "claude",
    "claudecode": "claude",
    "codex": "codex",
}

_DEFAULTS = {
    "coordinator_default": "",
    "coordinator_unrestricted": True,
    "central_poll_s": 1.0,
    "central_round_timeout_s": 900,
    "central_backend": "default",
}

_COORDINATOR_OVERRIDE_KEYS = (
    "coordinator_default",
    "coordinator",
    "cli",
    "coordinator_cli",
    "launch_cli",
    "coordinator_override",
    "cli_override",
)

_UNRESTRICTED_OVERRIDE_KEYS = (
    "coordinator_unrestricted",
    "unrestricted",
)

_POLL_OVERRIDE_KEYS = (
    "central_poll_s",
    "central_poll_seconds",
    "poll_s",
    "poll",
    "poll_seconds",
)

_TIMEOUT_OVERRIDE_KEYS = (
    "central_round_timeout_s",
    "central_round_timeout",
    "round_timeout_s",
    "round_timeout",
)

_BACKEND_OVERRIDE_KEYS = (
    "central_backend",
    "backend",
)


class CentralConfigError(ValueError):
    """Raised when central configuration cannot be resolved."""


def _as_path(value: Any) -> Path:
    return value if isinstance(value, Path) else Path(value)


def resolve_root(explicit: Any = None, env: dict[str, str] | None = None, home: Any = None) -> Path:
    """Resolve the central registry root.

    Precedence: an explicit ``--central-dir`` value, then
    ``AGENT_CREW_CHIEF_CENTRAL_DIR`` in ``env``, then
    ``<home>/.config/agent-crew-chief/central/``.
    """
    if explicit:
        return _as_path(explicit)
    env = env or {}
    env_value = env.get(CENTRAL_DIR_ENV)
    if env_value:
        return _as_path(env_value)
    if home is None:
        home = Path.home()
    return _as_path(home) / ".config" / "agent-crew-chief" / "central"


def _first_present(mapping: dict[str, Any] | None, keys: tuple[str, ...]) -> tuple[bool, Any]:
    if not isinstance(mapping, dict):
        return False, None
    for key in keys:
        if key in mapping:
            return True, mapping[key]
    return False, None


def _resolve_scalar(
    global_data: dict[str, Any],
    project_data: dict[str, Any],
    overrides: dict[str, Any],
    keys: tuple[str, ...],
    default: Any,
) -> Any:
    found, value = _first_present(overrides, keys)
    if found:
        return value
    found, value = _first_present(project_data, keys)
    if found:
        return value
    found, value = _first_present(global_data, keys)
    if found:
        return value
    return default


def _normalize_coordinator(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    value = value.strip()
    if not value:
        return ""
    lowered = value.lower()
    if lowered in _COORDINATOR_ALIASES:
        return _COORDINATOR_ALIASES[lowered]
    return lowered


def _validate_timing(name: str, value: Any) -> Any:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CentralConfigError(
            f"{name} must be a number, got {value!r}"
        )
    if not math.isfinite(value):
        raise CentralConfigError(
            f"{name} must be finite, got {value!r}"
        )
    if value <= 0:
        raise CentralConfigError(
            f"{name} must be positive, got {value!r}"
        )
    return value


def resolve_settings(
    global_data: dict[str, Any],
    project_data: dict[str, Any],
    overrides: dict[str, Any],
    caller: str | None,
) -> dict[str, Any]:
    """Resolve coordinator settings from preferences and launch overrides.

    ``global_data`` and ``project_data`` are preference dictionaries;
    ``overrides`` are launch-flag values. Precedence is overrides, then
    project, then global, then the built-in default. An empty coordinator
    default falls back to ``caller`` (the calling CLI). Unknown coordinators
    and non-finite or non-positive timings raise :class:`CentralConfigError`.
    """
    global_data = global_data or {}
    project_data = project_data or {}
    overrides = overrides or {}

    raw_coordinator = _resolve_scalar(
        global_data, project_data, overrides, _COORDINATOR_OVERRIDE_KEYS, _DEFAULTS["coordinator_default"]
    )
    coordinator = _normalize_coordinator(raw_coordinator)
    if not coordinator:
        coordinator = _normalize_coordinator(caller)
    if not coordinator:
        raise CentralConfigError(
            "no coordinator CLI could be resolved; set --cli, "
            "coordinator_default, or CREW_CHIEF_COORDINATOR"
        )
    if coordinator not in SUPPORTED_COORDINATORS:
        raise CentralConfigError(
            f"unknown coordinator CLI {coordinator!r}; expected one of "
            f"{', '.join(SUPPORTED_COORDINATORS)}"
        )

    # unrestricted: launch flags may be expressed as restricted=True
    unrestricted_found, unrestricted_raw = _first_present(overrides, _UNRESTRICTED_OVERRIDE_KEYS)
    restricted_found, restricted_raw = _first_present(overrides, ("restricted",))
    if unrestricted_found:
        unrestricted = bool(unrestricted_raw)
    elif restricted_found:
        unrestricted = not bool(restricted_raw)
    else:
        value = _resolve_scalar(
            global_data, project_data, overrides,
            ("coordinator_unrestricted", "unrestricted"),
            _DEFAULTS["coordinator_unrestricted"],
        )
        unrestricted = bool(value)

    poll = _validate_timing(
        "central_poll_s",
        _resolve_scalar(global_data, project_data, overrides, _POLL_OVERRIDE_KEYS, _DEFAULTS["central_poll_s"]),
    )
    timeout = _validate_timing(
        "central_round_timeout_s",
        _resolve_scalar(global_data, project_data, overrides, _TIMEOUT_OVERRIDE_KEYS, _DEFAULTS["central_round_timeout_s"]),
    )
    backend = _resolve_scalar(global_data, project_data, overrides, _BACKEND_OVERRIDE_KEYS, _DEFAULTS["central_backend"])
    if not isinstance(backend, str) or not backend.strip():
        raise CentralConfigError(f"central_backend must be a non-empty string, got {backend!r}")
    backend = backend.strip()

    return {
        "coordinator_default": coordinator,
        "coordinator_unrestricted": unrestricted,
        "central_poll_s": poll,
        "central_round_timeout_s": timeout,
        "central_backend": backend,
    }
