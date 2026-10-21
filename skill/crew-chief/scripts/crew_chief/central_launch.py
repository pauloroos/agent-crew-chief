"""Prepare authorized central coordinator launches.

This module turns an explicit request into a durable registry record plus a
coordinator worktree. It deliberately stops before starting any supervisor or
model: future tasks own that step. Preflight failures are persisted as a
failed coordinator state while the created worktree is preserved for
inspection.
"""

from __future__ import annotations

import os
import tomllib
import uuid
from pathlib import Path
from typing import Any

from . import central_config, central_preflight, central_registry, central_worktrees
from .central_config import CentralConfigError
from .central_preflight import PreflightError
from .central_registry import RegistryError
from .central_worktrees import WorktreeError
from .config import default_global_path

__all__ = ["adapter_for", "prepare_launch"]

_CLAUDE = "claude"
_CODEX = "codex"


def adapter_for(cli: str) -> Any:
    """Return the adapter module for one resolved coordinator CLI."""
    normalized = central_config._normalize_coordinator(cli, "cli")
    if normalized == _CLAUDE:
        from . import coordinator_claude

        return coordinator_claude
    if normalized == _CODEX:
        from . import coordinator_codex

        return coordinator_codex
    raise ValueError(f"unknown coordinator CLI {cli!r}")


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _option(options: dict[str, Any] | None, *names: str) -> Any:
    if not isinstance(options, dict):
        return None
    for name in names:
        if name in options:
            return options[name]
    # Also support dashed spellings derived from underscored names.
    for name in names:
        dashed = name.replace("_", "-")
        if dashed != name and dashed in options:
            return options[dashed]
    return None


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value) if value is not None else False


def _as_path(value: Any) -> Path | None:
    if value is None:
        return None
    return Path(value)


def prepare_launch(
    repo: Path,
    request_file: Path,
    options: dict[str, Any] | None,
    root: Path,
) -> dict[str, Any]:
    options = options or {}
    repo_path = Path(repo)
    root_path = Path(root)
    request_path = Path(request_file)

    # Read the request text (UTF-8 with optional BOM; newline style preserved).
    try:
        request_text = request_path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        return {
            "ok": False,
            "error": f"cannot read request file: {exc}",
            "coordinator_id": None,
            "id": None,
        }

    mode = _option(options, "mode", "launch_mode") or "direct-spec"
    approved = _as_bool(_option(options, "approved", "approved_spec", "approved-spec"))
    authorize_raw = _as_bool(
        _option(options, "authorize_raw", "authorize-raw", "authorize_raw_request")
    )
    spec_dir = _as_path(_option(options, "spec_dir", "spec-dir", "spec_directory"))
    consent = _as_bool(
        _option(
            options,
            "consent",
            "install_worktree_acc",
            "install-worktree-acc",
        )
    )
    release_root = _as_path(
        _option(options, "release_root", "release-root", "release", "release_root_path")
    )
    if release_root is None:
        release_root = repo_path

    base = _option(options, "base", "base_rev", "base-rev", "base_commit") or "HEAD"
    caller = _option(options, "caller") or os.environ.get("CREW_CHIEF_COORDINATOR")

    # Resolve preferences and coordinator settings.
    global_path = default_global_path()
    global_data = _read_toml(global_path)
    project_data = _read_toml(repo_path / "docs" / "crew-chief" / "preferences.toml")
    try:
        settings = central_config.resolve_settings(
            global_data, project_data, dict(options), caller
        )
    except (CentralConfigError, Exception) as exc:  # bounded diagnostic
        return {
            "ok": False,
            "error": str(exc),
            "coordinator_id": None,
            "id": None,
        }

    # Build authorization evidence container. Canonical settings live here.
    authorization: dict[str, Any] = {
        "mode": mode,
        "approved": approved,
        "authorize_raw": authorize_raw,
        "request_digest": None,
        "spec_digest": None,
        "authorized_at": None,
        "settings": settings,
    }
    # Allow explicit authorization flags supplied at the record level.
    if _option(options, "approved") is not None:
        authorization["approved"] = approved
    if _option(options, "authorize_raw") is not None or _option(options, "authorize-raw") is not None:
        authorization["authorize_raw"] = authorize_raw

    coordinator_id = str(uuid.uuid4())
    cli = settings["coordinator_default"]

    # Create the coordinator worktree (never mutates the source checkout).
    try:
        worktree_info = central_worktrees.create(repo_path, coordinator_id, base=base)
    except WorktreeError as exc:
        return {
            "ok": False,
            "error": str(exc),
            "coordinator_id": coordinator_id,
            "id": coordinator_id,
        }

    record: dict[str, Any] = {
        "schema_version": 1,
        "id": coordinator_id,
        "repo": worktree_info["repo"],
        "worktree": worktree_info["worktree"],
        "branch": worktree_info["branch"],
        "base_commit": worktree_info["base_commit"],
        "backend": settings["central_backend"],
        "cli": cli,
        "cli_version": "",
        "acc_version": "",
        "session_id": None,
        "mode": mode,
        "authorization": authorization,
        "job_ids": [],
        "active_job_id": None,
        "state": "created",
        "last_error": None,
        "supervisor_nonce": None,
    }

    # Register the durable record.
    try:
        registered_id = central_registry.register(root_path, record)
    except RegistryError as exc:
        return {
            "ok": False,
            "error": str(exc),
            "coordinator_id": coordinator_id,
            "id": coordinator_id,
            "worktree": worktree_info["worktree"],
            "branch": worktree_info["branch"],
            "base_commit": worktree_info["base_commit"],
        }
    coordinator_id = registered_id
    record["id"] = coordinator_id

    # Load the registered record back for canonical timestamps.
    try:
        loaded = central_registry.load_registry(root_path)
        registered_record = loaded["coordinators"].get(coordinator_id, record)
    except RegistryError:
        registered_record = record

    # Run preflight (never starts a model or supervisor).
    preflight_result = central_preflight.preflight(
        registered_record,
        request_text,
        spec_dir,
        consent,
        release_root,
    )

    # Merge preflight authorization evidence, preserving canonical settings.
    merged_authorization = dict(registered_record.get("authorization", authorization))
    merged_authorization["settings"] = settings
    pf_authorization = preflight_result.get("authorization")
    if isinstance(pf_authorization, dict):
        for key, value in pf_authorization.items():
            if key == "settings":
                continue
            merged_authorization[key] = value
    # Ensure mode/approval flags reflect the launch.
    merged_authorization["mode"] = mode
    merged_authorization["approved"] = approved
    merged_authorization["authorize_raw"] = authorize_raw

    if preflight_result.get("ok"):
        new_state = "created"
        last_error = None
    else:
        new_state = "failed"
        last_error = preflight_result.get("error")

    try:
        updated_record = central_registry.update_record(
            root_path,
            coordinator_id,
            {
                "authorization": merged_authorization,
                "state": new_state,
                "last_error": last_error,
                "cli": cli,
                "backend": settings["central_backend"],
                "mode": mode,
            },
        )
    except RegistryError:
        updated_record = dict(registered_record)
        updated_record["authorization"] = merged_authorization
        updated_record["state"] = new_state
        updated_record["last_error"] = last_error

    adapter = adapter_for(cli)

    result: dict[str, Any] = {
        "ok": bool(preflight_result.get("ok")),
        "error": preflight_result.get("error"),
        "coordinator_id": coordinator_id,
        "id": coordinator_id,
        "repo": updated_record.get("repo", worktree_info["repo"]),
        "worktree": updated_record.get("worktree", worktree_info["worktree"]),
        "branch": updated_record.get("branch", worktree_info["branch"]),
        "base_commit": updated_record.get("base_commit", worktree_info["base_commit"]),
        "cli": updated_record.get("cli", cli),
        "backend": updated_record.get("backend", settings["central_backend"]),
        "mode": updated_record.get("mode", mode),
        "session_id": updated_record.get("session_id", None),
        "state": updated_record.get("state", new_state),
        "last_error": updated_record.get("last_error", last_error),
        "authorization": merged_authorization,
        "adapter": adapter,
        "preflight": preflight_result,
        "record": updated_record,
        "spec_dir": preflight_result.get("spec_dir"),
        "probe": preflight_result.get("probe", {}),
        "acc": preflight_result.get("acc", {}),
    }
    return result