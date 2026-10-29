"""Prepare authorized central coordinator launches.

This module turns an explicit request into a durable registry record plus a
coordinator worktree. It deliberately stops before starting any supervisor or
model: a later task owns that step. A failed preflight is persisted as a
failed coordinator state while the created worktree is preserved for
inspection, and the source checkout is never touched.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import central_config, central_preflight, central_registry, central_worktrees
from . import coordinator_claude, coordinator_codex
from .config import PrefsError, load_prefs
from .central_messages import MessageError, _coordinator_dir
from .project_resolver import ProjectResolutionError, resolve_project_path

__all__ = [
    "adapter_for",
    "launch_backend",
    "prepare_launch",
    "select_backend",
]

#: Boolean launch flags; only a real boolean is accepted.
_FLAGS = ("approved", "authorize_raw", "install_worktree_acc")
#: The backend that needs no optional executable.
_BACKEND = "default"


def adapter_for(cli: str) -> Any:
    """Return the adapter module for one resolved coordinator CLI."""
    name = central_config.resolve_settings({}, {}, {"cli": cli}, None)["coordinator_default"]
    if name == "claude":
        return coordinator_claude
    if name == "codex":
        return coordinator_codex
    raise ValueError(f"unknown coordinator CLI {cli!r}")


def _failure(
    error: str,
    coordinator_id: str | None = None,
    record: dict[str, Any] | None = None,
    adapter: Any = None,
) -> dict[str, Any]:
    """One failed launch, with the same shape as a successful one."""
    return {
        "ok": False,
        "error": error,
        "id": coordinator_id,
        "record": record,
        "adapter": adapter,
    }


def _read_request(path: Path) -> tuple[bytes, str | None]:
    """The request bytes verbatim, or a bounded diagnostic."""
    try:
        raw = central_preflight._read_bounded(path)
    except (OSError, central_preflight.PreflightError) as exc:
        return b"", f"cannot read request file: {exc}"
    if len(raw) > central_preflight.MAX_TEXT_BYTES:
        return b"", f"request exceeds {central_preflight.MAX_TEXT_BYTES} UTF-8 bytes"
    try:
        raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        return b"", f"request file is not valid UTF-8 text: {exc}"
    return raw, None


def _preference_tier(prefs: Any) -> dict[str, Any]:
    """The resolved central scalars as one preference tier."""
    return {
        "coordinator_default": prefs.coordinator_default,
        "coordinator_unrestricted": prefs.coordinator_unrestricted,
        "central_poll_s": prefs.central_poll_s,
        "central_round_timeout_s": prefs.central_round_timeout_s,
        "central_backend": prefs.central_backend,
    }


def _caller(options: dict[str, Any]) -> str | None:
    """The calling CLI, explicit or from the environment."""
    return options.get("caller") or os.environ.get(central_config.COORDINATOR_ENV)


def _settings(repo: Path, options: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """Launch settings from flags, then preferences; or a diagnostic."""
    # Preferences are validated, never swallowed: a malformed file is an error.
    try:
        prefs = load_prefs(repo)
    except PrefsError as exc:
        return None, str(exc)
    try:
        return central_config.resolve_settings(
            {}, _preference_tier(prefs), options, _caller(options)
        ), None
    except central_config.CentralConfigError as exc:
        return None, str(exc)


def select_backend(root: Path, name: Any, **_ignored: Any) -> Any:
    """The backend called ``name``; only the default backend exists."""
    from .central_backend import BackendError, default_backend

    if name != _BACKEND:
        raise BackendError(f"unknown backend {name!r}; expected {_BACKEND!r}")
    return default_backend(root)


def launch_backend(repo: Path, options: dict[str, Any], root: Path) -> tuple[Any, str | None]:
    """Resolve the backend of one launch once: the flag, else preferences.

    Returns ``(backend, None)`` or ``(None, diagnostic)``.
    """
    from .central_backend import BackendError

    settings, error = _settings(Path(repo), options)
    if error:
        return None, error
    try:
        return select_backend(root, settings["central_backend"]), None
    except BackendError as exc:
        return None, str(exc)


def prepare_launch(
    repo: Path,
    request_file: Path,
    options: dict[str, Any] | None,
    root: Path,
    *,
    worktree_creator: Callable[..., dict] | None = None,
) -> dict[str, Any]:
    """Prepare one authorized coordinator launch.

    Returns ``{"ok", "error", "id", "record", "adapter"}``: the durable
    coordinator record, the selected adapter and a bounded diagnostic. The
    launch stops before any supervisor or model is started; a failed preflight
    keeps the worktree and the record for inspection.

    ``worktree_creator`` is an internal backend seam. It replaces only checkout
    creation; all validation, authorization and persistence below remain shared.
    """
    options = options or {}
    if not isinstance(options, dict):
        return _failure("options must be a dict")
    repo_path = Path(repo)
    root_path = Path(root)

    # Enforce project directory restriction (T031, FR-024)
    try:
        prefs = load_prefs(repo_path)
    except PrefsError as exc:
        return _failure(str(exc))

    projects_dir = options.get("projects_dir") or prefs.projects_dir
    if projects_dir is not None:
        try:
            repo_path = resolve_project_path(str(repo_path), projects_dir)
        except ProjectResolutionError as exc:
            return _failure(str(exc))

    request_raw, error = _read_request(Path(request_file))
    if error:
        return _failure(error)
    request_text = request_raw.decode("utf-8-sig")

    mode = options.get("mode") or "direct-spec"
    if mode not in central_preflight.MODES:
        return _failure(
            f"mode must be one of {', '.join(central_preflight.MODES)}, got {mode!r}"
        )

    flags: dict[str, bool] = {}
    for name in _FLAGS:
        value = options.get(name)
        if value is None:
            flags[name] = False
        elif isinstance(value, bool):
            flags[name] = value
        else:
            return _failure(f"{name} must be a boolean, got {value!r}")

    release_root = options.get("release_root")
    if release_root is None:
        return _failure(
            "release_root is required: select the released bundle explicitly"
        )
    release_root = Path(release_root)

    spec_dir = options.get("spec_dir")
    spec_dir = Path(spec_dir) if spec_dir is not None else None

    executors = options.get("executors")
    if executors is not None:
        if executors not in ("own", "subagents", "free_first"):
            return _failure(
                f"executors must be one of own, subagents, free_first; got {executors!r}"
            )

    settings, error = _settings(repo_path, options)
    if error:
        return _failure(error)
    if executors is not None:
        settings["executors"] = executors
    if options.get("model"):
        settings["model"] = options["model"]
    if options.get("end_mode"):
        settings["end_mode"] = options["end_mode"]

    backend = settings["central_backend"]
    if backend != _BACKEND:
        return _failure(
            f"backend {backend!r} cannot be prepared by the default backend; "
            "select it explicitly, there is no fallback"
        )

    coordinator_id = str(uuid.uuid4())
    cli = settings["coordinator_default"]
    try:
        worktree_info = (worktree_creator or central_worktrees.create)(
            repo_path, coordinator_id, base=options.get("base") or "HEAD"
        )
    except central_worktrees.WorktreeError as exc:
        return _failure(str(exc), coordinator_id)

    now = datetime.now(timezone.utc).isoformat()
    authorization: dict[str, Any] = {
        "mode": mode,
        "approved": flags["approved"],
        "authorize_raw": flags["authorize_raw"],
        "request_digest": None,
        "spec_digest": None,
        "authorized_at": None,
        "settings": settings,
    }
    record: dict[str, Any] = {
        "schema_version": 1,
        "id": coordinator_id,
        "repo": worktree_info["repo"],
        "worktree": worktree_info["worktree"],
        "branch": worktree_info["branch"],
        "base_commit": worktree_info["base_commit"],
        "backend": backend,
        "cli": cli,
        "cli_version": "",
        "acc_version": "",
        "session_id": None,
        "mode": mode,
        "authorization": authorization,
        "job_ids": [],
        "active_job_id": None,
        "state": "created",
        "created_at": now,
        "updated_at": now,
        "last_error": None,
        "supervisor_nonce": None,
    }
    try:
        central_registry.register(root_path, record)
    except central_registry.RegistryError as exc:
        return _failure(str(exc), coordinator_id)

    # The preflight never starts a model or a supervisor.
    preflight = central_preflight.preflight(
        record, request_text, spec_dir, flags["install_worktree_acc"], release_root
    )
    ok = bool(preflight.get("ok"))
    state = "created" if ok else "failed"
    last_error = None if ok else preflight.get("error")

    merged = dict(authorization)
    evidence = preflight.get("authorization")
    if isinstance(evidence, dict):
        for key, value in evidence.items():
            if key != "settings":
                merged[key] = value
    merged["settings"] = settings
    if preflight.get("spec_dir"):
        merged["spec_path"] = preflight["spec_dir"]

    # For free-text requests (raw-request), enter mandatory SDD spec phase (FR-032, US7)
    if ok and mode == "raw-request":
        from . import sdd_gate
        worktree_path = Path(worktree_info["worktree"])
        spec_path, _ = sdd_gate.enter_spec_phase(
            worktree_path,
            title=options.get("title") or "",
            request_text=request_text,
            spec_dir=options.get("spec_dir"),
        )
        merged["phase"] = "spec"
        merged["spec_path"] = str(spec_path)

    probe = preflight.get("probe") or {}
    acc = preflight.get("acc") or {}
    cli_version = probe.get("version") or ""
    acc_version = acc.get("installed") or ""

    if ok:
        # Only a validated request is persisted; a refused one may hold
        # private data that never reached the checks.
        try:
            directory = _coordinator_dir(root_path, coordinator_id, create=True)
            stored_request = directory / "request.md"
            if stored_request.is_symlink() or stored_request.resolve().parent != directory.resolve():
                raise MessageError("request path escapes the central coordinator directory")
            with stored_request.open("xb") as handle:
                handle.write(request_raw)
        except (OSError, MessageError) as exc:
            ok, state = False, "failed"
            last_error = f"cannot persist the request: {exc}"

    try:
        updated = central_registry.update_record(
            root_path,
            coordinator_id,
            {
                "authorization": merged,
                "state": state,
                "last_error": last_error,
                "cli": cli,
                "backend": backend,
                "mode": mode,
                "cli_version": cli_version,
                "acc_version": acc_version,
            },
        )
    except central_registry.RegistryError as exc:
        return _failure(
            f"cannot update the coordinator record: {exc}", coordinator_id, record
        )

    return {
        "ok": ok,
        "error": last_error,
        "id": coordinator_id,
        "record": updated,
        "adapter": adapter_for(cli),
    }
