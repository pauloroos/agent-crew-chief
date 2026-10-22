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
    "stall_minutes": 40,
    "on_coordinator_quota": "ask",
}

#: Canonical preference keys, as documented in the plan.
_PREF_COORDINATOR = ("coordinator_default",)
_PREF_UNRESTRICTED = ("coordinator_unrestricted",)
_PREF_POLL = ("central_poll_s",)
_PREF_TIMEOUT = ("central_round_timeout_s",)
_PREF_BACKEND = ("central_backend",)
_PREF_STALL = ("stall_minutes",)
_PREF_POLICY = ("on_coordinator_quota",)

#: Launch override names accepted from the command line. Each tuple also keeps
#: the canonical preference key, so a caller may pass either spelling.
_LAUNCH_COORDINATOR = ("cli",) + _PREF_COORDINATOR
_LAUNCH_UNRESTRICTED = ("unrestricted",) + _PREF_UNRESTRICTED
_LAUNCH_RESTRICTED = ("restricted",)
_LAUNCH_POLL = ("poll",) + _PREF_POLL
_LAUNCH_TIMEOUT = ("round_timeout_s",) + _PREF_TIMEOUT
_LAUNCH_BACKEND = ("backend",) + _PREF_BACKEND


class CentralConfigError(ValueError):
    """Raised when central configuration cannot be resolved."""


def _as_path(value: Any) -> Path:
    # Expanded here so a copied "~/..." works in shells that do not expand it (cmd.exe).
    return Path(value).expanduser()


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


def _first_present(
    mapping: dict[str, Any] | None,
    keys: tuple[str, ...],
    *,
    ignore_none: bool = False,
) -> tuple[bool, Any, str]:
    """Return whether a key is present, its value, and the key itself.

    The key is ``""`` when nothing matches. ``ignore_none`` treats ``None``
    like an absent value, which is what an argparse namespace looks like when
    an optional flag was not supplied.
    """
    if not isinstance(mapping, dict):
        return False, None, ""
    for key in keys:
        if key in mapping:
            value = mapping[key]
            if ignore_none and value is None:
                continue
            return True, value, key
    return False, None, ""


def _resolve_scalar(
    global_data: dict[str, Any],
    project_data: dict[str, Any],
    overrides: dict[str, Any],
    launch_keys: tuple[str, ...],
    preference_keys: tuple[str, ...],
    default: Any,
) -> tuple[Any, str]:
    """Resolve a scalar and return it with the name used in error messages.

    Launch overrides win, then project preferences, then global preferences,
    then the built-in default. ``None`` launch values are skipped because an
    argparse namespace reports unset flags as ``None``.
    """
    found, value, key = _first_present(overrides, launch_keys, ignore_none=True)
    if found:
        return value, key
    found, value, key = _first_present(project_data, preference_keys)
    if found:
        return value, key
    found, value, key = _first_present(global_data, preference_keys)
    if found:
        return value, key
    return default, preference_keys[0]


def _normalize_coordinator(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise CentralConfigError(
            f"{label} must be a string naming a coordinator CLI, got {value!r}"
        )
    value = value.strip()
    if not value:
        return ""
    lowered = value.lower()
    if lowered in _COORDINATOR_ALIASES:
        return _COORDINATOR_ALIASES[lowered]
    return lowered


def _validate_bool(label: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise CentralConfigError(f"{label} must be a boolean, got {value!r}")
    return value


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


#: ``on_coordinator_quota`` values that name no target CLI.
_QUOTA_POLICIES = ("ask", "wait")
#: Prefix of the ``handoff:<cli>`` quota policy.
_HANDOFF_PREFIX = "handoff:"


def _normalize_policy(value: Any, label: str) -> str:
    """Return the canonical ``on_coordinator_quota`` value.

    ``ask`` and ``wait`` stand alone; ``handoff:<cli>`` names an explicitly
    chosen target CLI, which must be one of :data:`SUPPORTED_COORDINATORS`.
    """
    if not isinstance(value, str):
        raise CentralConfigError(f"{label} must be a string, got {value!r}")
    value = value.strip().lower()
    if value in _QUOTA_POLICIES:
        return value
    if value.startswith(_HANDOFF_PREFIX):
        target = _normalize_coordinator(value[len(_HANDOFF_PREFIX):], label)
        if target in SUPPORTED_COORDINATORS:
            return _HANDOFF_PREFIX + target
        raise CentralConfigError(
            f"{label} must name a supported coordinator CLI after "
            f"'handoff:', expected one of {', '.join(SUPPORTED_COORDINATORS)}, "
            f"got {value!r}"
        )
    raise CentralConfigError(
        f"{label} must be 'ask', 'wait' or 'handoff:<cli>', got {value!r}"
    )


def resolve_continuity_settings(
    global_data: dict[str, Any],
    project_data: dict[str, Any],
    overrides: dict[str, Any],
) -> dict[str, Any]:
    """Resolve the coordinator continuity preferences (FR-027, FR-028).

    Launch overrides win, then project preferences, then global preferences,
    then the built-in defaults (``stall_minutes = 40``,
    ``on_coordinator_quota = "ask"``). ``stall_minutes`` must be a finite
    positive number; the policy must be ``ask``, ``wait`` or
    ``handoff:<cli>`` with a supported target. Invalid values raise
    :class:`CentralConfigError`; the input dictionaries are left untouched.
    """
    global_data = global_data or {}
    project_data = project_data or {}
    overrides = overrides or {}

    stall, _ = _resolve_scalar(
        global_data, project_data, overrides,
        _PREF_STALL, _PREF_STALL, _DEFAULTS["stall_minutes"],
    )
    stall = _validate_timing("stall_minutes", stall)
    policy, policy_label = _resolve_scalar(
        global_data, project_data, overrides,
        _PREF_POLICY, _PREF_POLICY, _DEFAULTS["on_coordinator_quota"],
    )
    return {
        "stall_minutes": stall,
        "on_coordinator_quota": _normalize_policy(policy, policy_label),
    }


def resolve_settings(
    global_data: dict[str, Any],
    project_data: dict[str, Any],
    overrides: dict[str, Any],
    caller: str | None,
) -> dict[str, Any]:
    """Resolve coordinator settings from preferences and launch overrides.

    ``global_data`` and ``project_data`` are preference dictionaries;
    ``overrides`` are launch-flag values. Precedence is overrides, then
    project, then global, then the built-in default. ``None`` launch values are
    ignored, the way an argparse namespace reports an unset flag, so they never
    mask a project or global setting. An empty coordinator default falls back
    to ``caller`` (the calling CLI). Unknown coordinators, non-string
    coordinator values, non-boolean permission flags and non-finite or
    non-positive timings raise :class:`CentralConfigError`.
    """
    global_data = global_data or {}
    project_data = project_data or {}
    overrides = overrides or {}

    raw_coordinator, coordinator_label = _resolve_scalar(
        global_data, project_data, overrides,
        _LAUNCH_COORDINATOR, _PREF_COORDINATOR,
        _DEFAULTS["coordinator_default"],
    )
    coordinator = _normalize_coordinator(raw_coordinator, coordinator_label)
    if not coordinator:
        if caller is not None:
            coordinator = _normalize_coordinator(caller, COORDINATOR_ENV)
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

    # unrestricted: launch flags may also be expressed as restricted=True.
    unrestricted_found, unrestricted_raw, unrestricted_label = _first_present(
        overrides, _LAUNCH_UNRESTRICTED, ignore_none=True
    )
    restricted_found, restricted_raw, restricted_label = _first_present(
        overrides, _LAUNCH_RESTRICTED, ignore_none=True
    )
    if unrestricted_found and restricted_found:
        # Both flags supplied: validate both strictly and reject the
        # contradictory combinations. None means "unset", so only a supplied
        # pair is compared.
        unrestricted = _validate_bool(unrestricted_label, unrestricted_raw)
        restricted = _validate_bool(restricted_label, restricted_raw)
        if unrestricted == restricted:
            raise CentralConfigError(
                "unrestricted and restricted are contradictory; "
                "supply only one of the two flags"
            )
        # Consistent pair: unrestricted=True/restricted=False or
        # unrestricted=False/restricted=True both resolve to `unrestricted`.
    elif unrestricted_found:
        unrestricted = _validate_bool(unrestricted_label, unrestricted_raw)
    elif restricted_found:
        unrestricted = not _validate_bool(restricted_label, restricted_raw)
    else:
        raw_unrestricted, pref_label = _resolve_scalar(
            global_data, project_data, overrides,
            _LAUNCH_UNRESTRICTED, _PREF_UNRESTRICTED,
            _DEFAULTS["coordinator_unrestricted"],
        )
        unrestricted = _validate_bool(pref_label, raw_unrestricted)

    poll, _ = _resolve_scalar(
        global_data, project_data, overrides,
        _LAUNCH_POLL, _PREF_POLL, _DEFAULTS["central_poll_s"],
    )
    poll = _validate_timing("central_poll_s", poll)
    timeout, _ = _resolve_scalar(
        global_data, project_data, overrides,
        _LAUNCH_TIMEOUT, _PREF_TIMEOUT, _DEFAULTS["central_round_timeout_s"],
    )
    timeout = _validate_timing("central_round_timeout_s", timeout)
    backend, _ = _resolve_scalar(
        global_data, project_data, overrides,
        _LAUNCH_BACKEND, _PREF_BACKEND, _DEFAULTS["central_backend"],
    )
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
