"""Launch preflight: authorization, CLI capabilities, worktree ACC and spec copy.

``preflight`` runs before a launch and never starts a model or a supervisor.
It returns a plain dict so the caller (``central_launch``) can persist the
authorization evidence on a failed or successful launch:

``ok``
    True only when every check passed.
``error``
    One bounded diagnostic line when ``ok`` is false, else ``None``.
``mode``
    The requested launch mode (``approved-spec``, ``direct-spec`` or
    ``raw-request``).
``cli``
    The coordinator CLI named by the record.
``probe``
    ``{"cli", "available", "version", "error"}`` from the adapter's probe.
``acc``
    ``{"installed", "release", "action"}`` where ``action`` is ``none``,
    ``installed`` or ``updated``; a declined update leaves ``missing`` or
    ``mismatch`` and never changes the source checkout or global preferences.
``spec_dir``
    The worktree path holding the selected spec files, usable by later
    rounds; ``None`` for raw-request or when nothing was copied.
``authorization``
    Persisted evidence: mode, approval/raw flags, request digest, spec digest
    and the recorded UTC time.

Only ``spec.md``, ``plan.md``, ``tasks.md`` and ``crew.toml`` travel into the
worktree, after the existing brief secret/sensitive-path checks. Neighbours,
symlinks, junctions, traversal and collisions are refused.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import briefcheck, coordinator_claude, coordinator_codex
from .config import load_prefs, PrefsError
from .dispatch import git_identity_note

__all__ = ["MODES", "SPEC_FILES", "PreflightError", "preflight"]

#: Launch modes the coordinator launches support.
MODES = ("approved-spec", "direct-spec", "raw-request")
#: The only spec files copied into the coordinator worktree.
SPEC_FILES = ("spec.md", "plan.md", "tasks.md", "crew.toml")

#: Cap on the request and on one selected spec file.
MAX_TEXT_BYTES = 1024 * 1024
#: The selected installer is explicit and local; a wedged run must not hang.
INSTALL_TIMEOUT_S = 600
#: Findings quoted in one diagnostic line.
_MAX_FINDINGS = 3

_CLI_MODULES = {"claude": coordinator_claude, "codex": coordinator_codex}
_VERSION = re.compile(r"(?m)^__version__\s*=\s*['\"]([^'\"]+)['\"]")
_MAX_ERROR_CHARS = 500


class PreflightError(ValueError):
    """A launch cannot proceed; the message is the bounded diagnostic."""


class _InstalledMismatch(PreflightError):
    """An explicit update can reconcile installed copies."""


def preflight(
    record: dict,
    request: str,
    spec_dir: Path | None,
    consent: bool,
    release_root: Path,
) -> dict:
    """Check one launch and return the evidence described in the module docstring.

    ``consent`` is the explicit ``--install-worktree-acc`` decision: when the
    worktree ACC is missing or differs from ``release_root``'s version, False
    declines and nothing is installed. ``release_root`` is the selected
    released bundle; only its ``install.py`` may run, and only in the worktree.
    """
    result: dict[str, Any] = {
        "ok": False,
        "error": None,
        "mode": None,
        "cli": None,
        "probe": {},
        "acc": {"installed": None, "release": None, "action": "none"},
        "spec_dir": None,
        "authorization": {},
    }
    try:
        _check(record, request, spec_dir, consent, release_root, result)
    except (PreflightError, OSError, PrefsError) as error:
        result["error"] = _bounded(error)
        return result
    result["ok"] = True
    return result


def _bounded(text: Any) -> str:
    """One bounded diagnostic line."""
    return " ".join(str(text).split())[:_MAX_ERROR_CHARS]


def _check(
    record: dict,
    request: str,
    spec_dir: Path | None,
    consent: bool,
    release_root: Path,
    result: dict,
) -> None:
    mode = _mode(record)
    result["mode"] = mode
    authorization = _authorization(record)
    unrestricted = _unrestricted(authorization)
    if not isinstance(consent, bool):
        raise PreflightError("consent must be a boolean")

    _require_evidence(mode, authorization)
    worktree = _worktree(record)
    sensitive = load_prefs(Path(record.get("repo") or worktree)).sensitive_paths
    request_digest = _request_digest(request, mode, sensitive)
    _compare(authorization, "request_digest", request_digest, "request")

    spec_digest: str | None = None
    selected: tuple[Path, dict[str, bytes]] | None = None
    if mode in ("approved-spec", "direct-spec"):
        selected = _select_spec(spec_dir, mode, sensitive)
        spec_digest = _digest(selected[1])
        _compare(authorization, "spec_digest", spec_digest, "selected spec")

    repo = Path(record.get("repo") or worktree)
    identity = git_identity_note(repo)
    if identity:
        raise PreflightError(identity)

    cli = _probe_cli(record, unrestricted, result)
    result["cli"] = cli
    worktree = _worktree(record)
    _ensure_acc(worktree, release_root, consent, result)

    if selected is not None:
        result["spec_dir"] = _copy_spec(worktree, *selected)

    result["authorization"] = {
        "mode": mode,
        "approved": authorization.get("approved") is True,
        "authorize_raw": authorization.get("authorize_raw") is True,
        "request_digest": request_digest,
        "spec_digest": spec_digest,
        "authorized_at": _authorized_at(authorization),
    }


# --- mode, authorization and request ---------------------------------------


def _mode(record: Any) -> str:
    if not isinstance(record, dict):
        raise PreflightError("a coordinator record is required")
    mode = record.get("mode")
    if mode not in MODES:
        raise PreflightError(
            f"mode must be one of {', '.join(MODES)}, got {mode!r}"
        )
    return mode


def _authorization(record: dict) -> dict:
    authorization = record.get("authorization")
    if not isinstance(authorization, dict):
        raise PreflightError(f"authorization must be an object, got {authorization!r}")
    return authorization


def _unrestricted(authorization: dict) -> bool:
    """The resolved ``coordinator_unrestricted`` setting, defaulting to True."""
    settings = authorization.get("settings", {})
    if not isinstance(settings, dict):
        raise PreflightError("authorization settings must be an object")
    value = settings.get("coordinator_unrestricted", True)
    if not isinstance(value, bool):
        raise PreflightError(
            f"coordinator_unrestricted must be a boolean, got {value!r}"
        )
    return value


def _request_digest(request: Any, mode: str, sensitive: list[str]) -> str:
    if not isinstance(request, str) or not request.strip():
        raise PreflightError(f"{mode} requires a non-empty request")
    try:
        data = request.encode("utf-8")
    except UnicodeError as exc:
        raise PreflightError("request is not valid UTF-8 text") from exc
    if len(data) > MAX_TEXT_BYTES:
        raise PreflightError(f"request exceeds {MAX_TEXT_BYTES} UTF-8 bytes")
    findings = briefcheck.check_brief(request, sensitive)
    if findings:
        raise PreflightError(
            "request contains private data: " + "; ".join(findings[:_MAX_FINDINGS])
        )
    return hashlib.sha256(data).hexdigest()


def _require_evidence(mode: str, authorization: dict) -> None:
    if mode == "approved-spec" and authorization.get("approved") is not True:
        raise PreflightError(
            "approved-spec requires an explicit approval record (approved=true)"
        )
    if mode == "raw-request" and authorization.get("authorize_raw") is not True:
        raise PreflightError(
            "raw-request requires explicit authorization (authorize_raw=true)"
        )


def _compare(authorization: dict, key: str, digest: str, label: str) -> None:
    """Reject a recorded digest that no longer matches the current content."""
    recorded = authorization.get(key)
    if recorded is None:
        return
    if not isinstance(recorded, str) or not recorded:
        raise PreflightError(f"recorded {key} must be a non-empty string")
    if recorded != digest:
        raise PreflightError(
            f"{label} changed since it was authorized; ask the owner before launching"
        )


def _authorized_at(authorization: dict) -> str:
    value = authorization.get("authorized_at")
    if isinstance(value, str) and value:
        return value
    return datetime.now(timezone.utc).isoformat()


# --- selected spec ----------------------------------------------------------


def _select_spec(spec_dir: Path | None, mode: str, sensitive: list[str]) -> tuple[Path, dict[str, bytes]]:
    """The resolved selected directory and the four validated file payloads."""
    if spec_dir is None:
        raise PreflightError(f"{mode} requires a selected spec directory")
    source = Path(spec_dir)
    if briefcheck.path_is_sensitive(str(source), sensitive):
        raise PreflightError("selected spec is in a configured sensitive path")
    try:
        if source.is_symlink():
            raise PreflightError(f"selected spec directory is a symbolic link: {source}")
        if _is_reparse_point(source):
            raise PreflightError(
                f"selected spec directory is a link or junction: {source}"
            )
        root = source.resolve(strict=True)
    except OSError as exc:
        raise PreflightError(f"selected spec directory is not readable: {exc}")
    if not root.is_dir():
        raise PreflightError(f"selected spec directory not found: {source}")
    if briefcheck.path_is_sensitive(str(root), sensitive):
        raise PreflightError("selected spec is in a configured sensitive path")

    files: dict[str, bytes] = {}
    for name in SPEC_FILES:
        path = root / name
        if path.is_symlink():
            raise PreflightError(f"spec file is a symbolic link: {name}")
        if not path.is_file():
            raise PreflightError(f"selected spec is missing {name}")
        try:
            resolved = path.resolve(strict=True)
            data = _read_bounded(path)
        except OSError as exc:
            raise PreflightError(f"cannot read spec file {name}: {exc}")
        if resolved.parent != root:
            raise PreflightError(f"spec file escapes the selected directory: {name}")
        if len(data) > MAX_TEXT_BYTES:
            raise PreflightError(f"spec file {name} exceeds {MAX_TEXT_BYTES} bytes")
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise PreflightError(f"spec file {name} is not valid UTF-8 text") from exc
        findings = briefcheck.check_brief(text, sensitive)
        if findings:
            raise PreflightError(
                f"spec file {name} contains private data: "
                + "; ".join(findings[:_MAX_FINDINGS])
            )
        files[name] = data
    return root, files


def _is_reparse_point(path: Path) -> bool:
    """True for a Windows junction or other reparse point.

    ``Path.is_symlink`` is false for a directory junction, so a Windows
    alias would otherwise be followed silently.
    """
    try:
        info = os.lstat(path)
    except OSError:
        return False
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(getattr(info, "st_file_attributes", 0) & flag)


def _digest(files: dict[str, bytes]) -> str:
    """Digest over the four selected files in a fixed order."""
    digest = hashlib.sha256()
    for name in SPEC_FILES:
        digest.update(name.encode("ascii"))
        digest.update(b"\0")
        digest.update(files[name])
        digest.update(b"\0")
    return digest.hexdigest()


def _copy_spec(worktree: Path, source: Path, files: dict[str, bytes]) -> str:
    """Copy the selected files under ``<worktree>/specs/<name>``."""
    worktree_root = worktree.resolve(strict=True)
    if _is_within(source, worktree_root):
        return str(source)
    if source.name in ("", ".", ".."):
        raise PreflightError(f"selected spec directory has no usable name: {source}")
    specs = worktree / "specs"
    destination = specs / source.name
    _contained(destination, worktree_root)
    if destination.exists() or destination.is_symlink():
        raise PreflightError(f"spec copy destination already exists: {destination}")
    destination.mkdir(parents=True)
    for name, data in files.items():
        with (destination / name).open("xb") as handle:
            handle.write(data)
    return str(destination)


def _contained(path: Path, root: Path) -> None:
    """Refuse redirected parents, including dangling Windows junctions."""
    root = root.resolve(strict=True)
    if path.resolve(strict=False) != root / path.relative_to(root):
        raise PreflightError("destination escapes the coordinator worktree through a link")


def _read_bounded(path: Path) -> bytes:
    with path.open("rb") as handle:
        data = handle.read(MAX_TEXT_BYTES + 1)
    if len(data) > MAX_TEXT_BYTES:
        raise PreflightError(f"{path.name} exceeds {MAX_TEXT_BYTES} bytes")
    return data


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


# --- CLI capabilities -------------------------------------------------------


def _probe_cli(record: dict, unrestricted: bool, result: dict) -> str:
    cli = record.get("cli")
    if cli not in _CLI_MODULES:
        raise PreflightError(f"unknown coordinator CLI {cli!r}")
    module = _CLI_MODULES[cli]
    try:
        info = module.probe(record) if cli == "codex" else module.probe()
    except Exception as exc:  # a probe must fail as a diagnostic, never a crash
        raise PreflightError(f"{cli} capability probe failed: {exc}")
    if not isinstance(info, dict):
        raise PreflightError(f"{cli} capability probe returned no result")

    result["probe"] = {
        "cli": cli,
        "available": info.get("available") is True,
        "version": info.get("version") or None,
        "error": _bounded(info["error"]) if info.get("error") else None,
    }
    if not result["probe"]["available"]:
        detail = result["probe"]["error"] or "not found on PATH"
        raise PreflightError(f"{cli} CLI is not available: {detail}")

    capabilities = info.get("capabilities") or {}
    session = record.get("session_id")
    if cli == "claude":
        required = ["stream_json", "verbose"]
        if session is not None:
            required.append("resume")
        required.append("bypass_permissions" if unrestricted else "dontAsk")
        missing = [name for name in required if not capabilities.get(name)]
        if missing:
            raise PreflightError(
                f"missing claude capabilities: {', '.join(missing)}"
            )
    else:
        if info.get("supported") is not True:
            raise PreflightError(_bounded(info.get("error")) or "missing codex capabilities")
        if session is not None and not capabilities.get("resume_json"):
            raise PreflightError("missing codex capabilities: resume_json")
    return cli


# --- worktree ACC -----------------------------------------------------------


def _worktree(record: dict) -> Path:
    value = record.get("worktree")
    if not isinstance(value, str) or not value:
        raise PreflightError("a coordinator worktree is required")
    path = Path(value)
    if not path.is_absolute() or not path.is_dir():
        raise PreflightError(f"coordinator worktree is not an existing directory: {value}")
    return path.resolve(strict=True)


def _release_version(release_root: Path) -> str:
    root = Path(release_root)
    init = root / "skill" / "crew-chief" / "scripts" / "crew_chief" / "__init__.py"
    version = _read_version(init)
    if version is None:
        raise PreflightError(f"selected release has no readable ACC version: {init}")
    return version


def _installed_version(worktree: Path) -> str | None:
    """The version installed in the worktree, refusing disagreeing copies."""
    found: dict[str, str] = {}
    for skills in (".claude/skills", ".agents/skills"):
        init = worktree / skills / "crew-chief" / "scripts" / "crew_chief" / "__init__.py"
        version = _read_version(init)
        if version is not None:
            found[str(init)] = version
    if not found:
        return None
    versions = set(found.values())
    if len(versions) > 1:
        raise _InstalledMismatch(
            "installed worktree ACC copies disagree: "
            + ", ".join(sorted(versions))
        )
    return versions.pop()


def _read_version(init: Path) -> str | None:
    """``__version__`` from an ACC package, or ``None`` when unreadable."""
    try:
        if init.is_symlink() or not init.is_file():
            return None
        text = _read_bounded(init).decode("utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return None
    match = _VERSION.search(text)
    return match.group(1) if match else None


def _ensure_acc(
    worktree: Path, release_root: Path, consent: bool, result: dict
) -> None:
    root = worktree.resolve(strict=True)
    # The selected installer writes instructions, preferences and both skills.
    for name in (".claude/skills/crew-chief", ".agents/skills/crew-chief",
                 "docs/crew-chief", "AGENTS.md", "CLAUDE.md", "GEMINI.md",
                 ".cursor/rules/agent-crew-chief.mdc"):
        _contained(root / name, root)
    release = _release_version(release_root)
    try:
        installed = _installed_version(worktree)
    except _InstalledMismatch:
        if not consent:
            raise
        installed = "inconsistent copies"
    result["acc"] = {"installed": installed, "release": release, "action": "none"}
    if installed == release:
        return
    action = "missing" if installed is None else "mismatch"
    result["acc"]["action"] = action
    if not consent:
        state = (
            "no worktree ACC is installed"
            if installed is None
            else f"worktree ACC {installed} differs from selected release {release}"
        )
        raise PreflightError(
            f"{state}; pass --install-worktree-acc to install it in the coordinator worktree"
        )
    _install(worktree, release_root)
    updated = _installed_version(worktree)
    if updated != release:
        raise PreflightError(
            f"the selected installer did not install ACC {release} in the worktree"
        )
    result["acc"] = {
        "installed": updated,
        "release": release,
        "action": "installed" if action == "missing" else "updated",
    }


def _install(worktree: Path, release_root: Path) -> None:
    """Run only the selected released installer, only in the worktree."""
    root = Path(release_root)
    installer = root / "install.py"
    try:
        root_resolved = root.resolve(strict=True)
        installer_resolved = installer.resolve(strict=True)
    except OSError as exc:
        raise PreflightError(f"selected release has no readable installer: {exc}")
    if not installer_resolved.is_file():
        raise PreflightError(f"selected release has no installer: {installer}")
    if not _is_within(installer_resolved, root_resolved):
        raise PreflightError("selected release installer escapes the release root")
    try:
        proc = subprocess.run(
            [sys.executable, "-I", str(installer), str(worktree)],
            cwd=str(worktree),
            stdin=subprocess.DEVNULL,
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=INSTALL_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PreflightError(f"the selected installer could not run: {exc}")
    if proc.returncode != 0:
        detail = _bounded(proc.stderr or proc.stdout or "")
        raise PreflightError(
            f"install.py failed with exit {proc.returncode}"
            + (f": {detail}" if detail else "")
        )
