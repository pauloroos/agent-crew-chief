"""Central registry: versioned coordinator records under a central root.

This module owns :file:`registry.json` — the versioned map of coordinator
records keyed by stable UUID — plus the bounded registry lock and the
per-coordinator owner leases that later phases build on. The root is always
injected by the caller (``central_config.resolve_root``); no setting is read
here.

Design, following the plan's "Configuration and registry" section (FR-006,
FR-022, FR-024):

- :file:`registry.json` holds ``{"schema_version": 1, "coordinators": {...}}``.
  One coordinator record per entry; stable UUID ids; every rewrite merges other
  processes' rows instead of losing them.
- Rows with unknown schema versions (or any row-level defect) produce a
  per-row error in :func:`load_registry` and are never overwritten: updates
  rewrite the file under the lock and keep such rows byte-value-identical, so
  corrupt or future records stay visible and recoverable instead of dropping
  history. Wholly unreadable registries (corrupt JSON, unknown top-level
  version, structurally broken, oversized) block writes until the owner
  recovers them explicitly rather than being replaced silently.
- Text is read as UTF-8 with an optional BOM and either newline style; every
  rewrite preserves the existing BOM and newline style and replaces the file
  atomically through :func:`records.write_atomic` (temp file in the same
  directory plus ``os.replace``).
- Reads are bounded: the registry file at 8 MiB, a single row at 1 MiB.
  Oversized data is reported rather than blocking the other rows.
- The lock (:file:`registry.lock`) and the coordinator owner leases
  (:file:`coordinators/<uuid>/supervisor.lock`) are :func:`records.file_lock`
  files whose content records the owner nonce, PID and acquisition time.
  Stale takeover is disabled: age or a PID alone never authorizes deleting or
  signalling another owner's lock, so a contested or abandoned lock is
  reported and recovered explicitly by the owner.
- Coordinator ids become path segments, so they must be canonical UUIDs and
  the addressed directories must sit under the root: traversal and symlink
  escapes are refused.
"""

from __future__ import annotations

import json
import math
import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .records import file_lock, write_atomic

__all__ = [
    "RECORD_MAX_BYTES",
    "REGISTRY_READ_MAX_BYTES",
    "RegistryError",
    "RegistryLockTimeout",
    "RegistryStructureError",
    "SCHEMA_VERSION",
    "load_registry",
    "owner_lease",
    "register",
    "update_record",
]

#: Registry schema version written by this module.
SCHEMA_VERSION = 1

#: Hard cap on the whole :file:`registry.json` read (plan: 8 MiB).
REGISTRY_READ_MAX_BYTES = 8 * 1024 * 1024

#: Hard cap on a single coordinator row read (plan: 1 MiB).
RECORD_MAX_BYTES = 1 * 1024 * 1024

#: Default bounded wait for the registry lock and owner leases.
LOCK_TIMEOUT_S = 60.0

_FILE = "registry.json"
_LOCK = "registry.lock"
_COORDINATORS_DIR = "coordinators"
_LEASE = "supervisor.lock"

#: Fields any non-blank string value must satisfy.
_REQUIRED_TEXT = (
    "branch", "base_commit", "backend", "cli", "mode", "state",
    "created_at", "updated_at",
)
#: String fields that may be empty (versions start unknown).
_OPTIONAL_TEXT = ("cli_version", "acc_version")
#: Fields stored as ``null`` or a non-blank string.
_NULLABLE_TEXT = ("session_id", "active_job_id", "last_error", "supervisor_nonce")
#: Fields addressed by the owner, never rewritten by an update.
_PATH_FIELDS = ("repo", "worktree")
#: Every field of the schema version above; extras are per-row errors.
_KNOWN_FIELDS = frozenset(
    ("schema_version", "id", *_PATH_FIELDS, *_REQUIRED_TEXT, *_OPTIONAL_TEXT,
     *_NULLABLE_TEXT, "authorization", "job_ids")
)
#: Fields an :func:`update_record` change must not touch.
_IMMUTABLE_FIELDS = frozenset(("id", "schema_version", "created_at"))


class RegistryError(Exception):
    """A registry file or record cannot be loaded, stored or updated."""


class RegistryStructureError(RegistryError):
    """The registry's top level does not match the supported schema."""


class RegistryLockTimeout(RegistryError):
    """A registry lock or lease stayed taken beyond the bounded wait."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- record schema ----------------------------------------------------------


def _canonical_uuid(value: Any) -> str | None:
    """Return ``value`` in its canonical form, or ``None`` when not a UUID."""
    if not isinstance(value, str):
        return None
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return None
    return str(parsed) if str(parsed) == value else None


def _validate_record(record: Any) -> list[str]:
    """Every schema problem of one coordinator record, in field order."""
    if not isinstance(record, dict):
        return [f"record is not an object (got {type(record).__name__})"]
    problems: list[str] = []

    extras = sorted(set(record) - _KNOWN_FIELDS)
    if extras:
        problems.append(f"unknown field(s): {', '.join(extras)}")

    canonical = _canonical_uuid(record.get("id"))
    if canonical is None:
        problems.append(f"id must be a canonical UUID string, got {record.get('id')!r}")
    if record.get("schema_version") != SCHEMA_VERSION:
        problems.append(
            f"schema_version must be {SCHEMA_VERSION}, got {record.get('schema_version')!r}"
        )

    for field in _PATH_FIELDS:
        value = record.get(field)
        if (
            not isinstance(value, str)
            or not value
            or "\x00" in value
            or not Path(value).is_absolute()
        ):
            problems.append(f"{field} must be a non-empty absolute local path string, got {value!r}")
    for field in _REQUIRED_TEXT:
        value = record.get(field)
        if not isinstance(value, str) or not value:
            problems.append(f"{field} must be a non-empty string, got {value!r}")
    for field in _OPTIONAL_TEXT:
        value = record.get(field)
        if not isinstance(value, str):
            problems.append(f"{field} must be a string, got {value!r}")
    for field in _NULLABLE_TEXT:
        value = record.get(field)
        if value is not None and (not isinstance(value, str) or not value):
            problems.append(f"{field} must be null or a non-empty string, got {value!r}")

    authorization = record.get("authorization")
    if not isinstance(authorization, dict):
        problems.append(f"authorization must be an object, got {authorization!r}")
    job_ids = record.get("job_ids")
    if not isinstance(job_ids, list) or any(
        not isinstance(job_id, str) or not job_id for job_id in job_ids
    ):
        problems.append(f"job_ids must be a list of non-empty strings, got {job_ids!r}")
    return problems


def _record_size(record: Any) -> int:
    try:
        return len(json.dumps(record, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):  # unserializable junk in a future row
        return REGISTRY_READ_MAX_BYTES + 1


# --- bounded, BOM-tolerant reads --------------------------------------------


def _registry_path(root: Path) -> Path:
    return Path(root) / _FILE


def _read_bounded(path: Path, max_bytes: int) -> bytes:
    """Read at most ``max_bytes`` bytes; an oversize file is refused.

    The unread prefix is never decoded, so a registry that outgrew its cap is
    reported instead of silently truncated or loaded into memory whole.
    """
    with path.open("rb") as handle:
        data = handle.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise RegistryError(
            f"{path}: exceeds the {max_bytes} byte read cap; explicit recovery required"
        )
    return data


def _registry_text(path: Path, max_bytes: int) -> tuple[str, bool]:
    """Return the registry text and whether it carried a UTF-8 BOM."""
    data = _read_bounded(path, max_bytes)
    had_bom = data.startswith(b"\xef\xbb\xbf")
    return data.decode("utf-8-sig"), had_bom


def _newline_style(text: str) -> str:
    """The file's dominant newline, defaulting to LF for new files."""
    return "\r\n" if "\r\n" in text else "\n"


class _Style:
    """A registry file's fetched text plus its BOM and newline style."""

    __slots__ = bom, data, newline

    def __init__(self, data: dict, *, bom: bool, newline: str):
        self.bom = bom
        self.data = data
        self.newline = newline


def _parse_registry(path: Path, text: str) -> dict:
    """Decode and structurally check a registry body loaded from disk."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RegistryError(f"{path}: invalid JSON at line {exc.lineno}: {exc.msg}") from exc
    if not isinstance(data, dict):
        raise RegistryStructureError(
            f"{path}: registry must be a JSON object, got {type(data).__name__}"
        )
    if data.get("schema_version") != SCHEMA_VERSION:
        raise RegistryStructureError(
            f"{path}: registry schema_version must be {SCHEMA_VERSION}, got "
            f"{data.get('schema_version')!r}; explicit recovery required"
        )
    coordinators = data.get("coordinators")
    if not isinstance(coordinators, dict):
        raise RegistryStructureError(
            f"{path}: registry 'coordinators' must be a map keyed by UUID, got {type(coordinators).__name__}"
        )
    return data


def _read_registry(path: Path) -> tuple[dict | None, _Style | None]:
    """Read the registry for a mutation, or ``(None, None)`` when missing.

    Corruption, oversized files and incompatible structures raise
    :class:`RegistryError` / :class:`RegistryStructureError` so mutations never
    replace a registry the owner must recover by hand, and unknown top-level
    schema versions are never overwritten.
    """
    if not path.exists():
        return None, None
    text, had_bom = _registry_text(path, REGISTRY_READ_MAX_BYTES)
    data = _parse_registry(path, text)
    return data, _Style(bom=had_bom, newline=_newline_style(text), data=data)


_FRESH: dict = {"schema_version": SCHEMA_VERSION, "coordinators": {}}


def _serialize(data: dict, style: _Style | None) -> str:
    """Render the registry in the file's own BOM and newline style."""
    body = json.dumps(data, indent=2, ensure_ascii=False)
    newline = style.newline if style is not None else "\n"
    text = body.replace("\n", newline) + newline
    if style is not None and style.bom:
        text = "﻿" + text
    blob_size = len(text.encode("utf-8"))
    if blob_size > REGISTRY_READ_MAX_BYTES:
        raise RegistryError(
            f"registry grew to {blob_size} bytes, beyond the "
            f"{REGISTRY_READ_MAX_BYTES} byte cap; explicit recovery required"
        )
    return text


def _write_registry(root: Path, data: dict, style: _Style | None) -> None:
    path = _registry_path(root)
    try:
        payload = _serialize(data, style)
    except TypeError as exc:  # a row handed us unserializable data
        raise RegistryError(f"{path}: cannot serialize registry: {exc}") from exc
    write_atomic(path, payload)


# --- registry rows and per-row errors ---------------------------------------


def _row_error(key: str, problems: list[str]) -> dict:
    return {
        "record": key,
        "message": f"record does not match registry schema v{SCHEMA_VERSION}: "
        + "; ".join(problems),
    }


def _structural_errors(data: dict) -> Iterator[dict]:
    """Whole-file problems as report rows; nothing else is checked here."""
    if data.get("schema_version") != SCHEMA_VERSION:
        yield {
            "record": "<registry>",
            "message": f"registry schema_version must be {SCHEMA_VERSION}, got {data.get('schema_version')!r}",
        }
    if not isinstance(data.get("coordinators"), dict):
        yield {
            "record": "<registry>",
            "message": "registry 'coordinators' must be a map keyed by UUID",
        }


def _load_rows(data: dict) -> tuple[dict[str, dict], list[dict]]:
    """Split rows into valid records and one report row per invalid one."""
    coordinators: dict[str, dict] = {}
    errors: list[dict] = []
    for key, row in data["coordinators"].items():
        problems = _validate_record(row)
        if isinstance(row, dict) and row.get("id") != key:
            problems.append(f"record id {row.get('id')!r} does not match its registry key {key!r}")
        if problems:
            errors.append(_row_error(key, problems))
        elif isinstance(row, dict):
            coordinators[key] = dict(row)
    for problem in _structural_errors(data):
        errors.append(problem)
    return coordinators, errors


# --- public API -------------------------------------------------------------


def load_registry(root: Path) -> dict:
    """Load the registry and report defective rows without dropping them.

    Returns ``{"schema_version": 1, "coordinators": {id: record}, "errors":
    [...]}`` where ``coordinators`` holds only rows matching schema version
    :data:`SCHEMA_VERSION` and ``errors`` carries one entry per defective row
    (recorded by its registry key, or ``"<registry>"`` for whole-file
    problems). A missing file loads as an empty registry; a corrupt or
    oversized one raises :class:`RegistryError` instead of a partial read.
    The row defects remain on disk; nothing here rewrites the file.
    """
    path = _registry_path(root)
    try:
        text, _ = _registry_text(path, REGISTRY_READ_MAX_BYTES)
    except FileNotFoundError:
        return {
            "schema_version": SCHEMA_VERSION,
            "coordinators": {},
            "errors": [],
        }
    data = _parse_registry(path, text)
    coordinators, errors = _load_rows(data)
    return {"schema_version": SCHEMA_VERSION, "coordinators": coordinators, "errors": errors}


def register(root: Path, record: dict, *, timeout_s: float = LOCK_TIMEOUT_S) -> str:
    """Add one coordinator record and return its stable id.

    ``record`` may omit ``id`` (a canonical UUID v4 is generated), the
    timestamps (set to now) and the optional runtime fields, which default per
    the plan's schema. The record is validated first and stored under the
    bounded registry lock, so concurrent registrations merge; an id that is
    already present — including one stored by another tool at an unknown
    schema version — is refused instead of overwritten.
    """
    root = Path(root)
    if not isinstance(record, dict):
        raise RegistryError(f"record must be a dict, got {type(record).__name__}")
    candidate = dict(record)
    candidate.setdefault("id", str(uuid.uuid4()))
    candidate.setdefault("created_at", _now_iso())
    candidate.setdefault("updated_at", candidate["created_at"])
    candidate.setdefault("session_id", None)
    candidate.setdefault("job_ids", [])
    candidate.setdefault("active_job_id", None)
    candidate.setdefault("last_error", None)
    candidate.setdefault("supervisor_nonce", None)
    candidate.setdefault("cli_version", "")
    candidate.setdefault("acc_version", "")
    problems = _validate_record(candidate)
    if problems:
        raise RegistryError(
            "invalid coordinator record: " + "; ".join(problems)
        )
    coordinator_id = candidate["id"]

    with _registry_lock(root, timeout_s):
        data, style = _read_registry(_registry_path(root))
        if data is None:
            data, style = dict(_FRESH), None
        coordinators = data["coordinators"]
        if coordinator_id in coordinators:
            raise RegistryError(
                f"coordinator {coordinator_id!r} is already registered; refusing to overwrite"
            )
        coordinators[coordinator_id] = candidate
        _write_registry(root, data, style)
    return coordinator_id


def update_record(
    root: Path,
    coordinator_id: Any,
    changes: dict,
    *,
    timeout_s: float = LOCK_TIMEOUT_S,
) -> dict:
    """Apply ``changes`` to one coordinator under the lock, atomically.

    ``coordinator_id`` must be the canonical UUID the record was registered
    with (path-like strings are refused). ``changes`` is a shallow merge; the
    immutable fields :data:`_IMMUTABLE_FIELDS` must not appear, and the merged
    record must still match the schema — ``updated_at`` is set to now. An
    incompatible, corrupt or missing target row is refused with the
    conflicting rows left exactly as they were, pending explicit recovery.
    """
    root = Path(root)
    canonical = _canonical_uuid(coordinator_id)
    if canonical is None:
        raise RegistryError(
            f"coordinator id must be a canonical UUID string, got {coordinator_id!r}"
        )
    coordinator_id = canonical
    if not isinstance(changes, dict):
        raise RegistryError(f"changes must be a dict, got {type(changes).__name__}")
    immutable = sorted(_IMMUTABLE_FIELDS & set(changes))
    if immutable:
        raise RegistryError(
            f"changes must not modify immutable field(s): {', '.join(immutable)}"
        )

    with _registry_lock(root, timeout_s):
        data, style = _read_registry(_registry_path(root))
        if data is None:
            raise RegistryError(f"no coordinator {coordinator_id!r} is registered")
        coordinators = data["coordinators"]
        if coordinator_id not in coordinators:
            raise RegistryError(
                f"no coordinator {coordinator_id!r} is registered under {root}"
            )
        row = coordinators[coordinator_id]
        problems = _validate_record(row)
        if not problems and isinstance(row, dict) and row.get("id") != coordinator_id:
            problems.append(f"record id {row.get('id')!r} does not match its registry key")
        if problems:
            raise RegistryError(
                f"coordinator {coordinator_id!r} record is incompatible and was left "
                f"unchanged: {'; '.join(problems)}; explicit recovery required"
            )
        merged = {**row, **changes}
        problems = _validate_record(merged)
        if problems:
            raise RegistryError(
                f"update for coordinator {coordinator_id!r} rejected: {'; '.join(problems)}"
            )
        merged["updated_at"] = _now_iso()
        coordinators[coordinator_id] = merged
        _write_registry(root, data, style)
    return dict(merged)


def owner_lease(
    root: Path,
    coordinator_id: Any,
    nonce: str,
    *,
    timeout_s: float = LOCK_TIMEOUT_S,
) -> Iterator[dict]:
    """Hold one coordinator's exclusive supervisor lease for the ``with`` block.

    The lease file ``coordinators/<uuid>/supervisor.lock`` records the owning
    nonce, PID and acquisition time. Acquiring for the same nonce from the
    same or another process renews nothing and simply re-enters; a different
    nonce waits up to ``timeout_s`` and then raises
    :class:`RegistryLockTimeout` naming the holder — the lock file is never
    deleted or rewritten for them, however old it looks, because age and a
    PID alone do not prove an owner gone. Recovery is explicit: the operator
    removes the lease file after checking :data:`_LEASE`'s payload. Yields a
    ``{"coordinator_id", "nonce", "pid", "path"}`` snapshot.
    """
    root = Path(root)
    nonce_str = string_nonce(nonce)
    path = _coordinator_lease_path(root, coordinator_id)
    snapshot = {
        "coordinator_id": _canonical_uuid(coordinator_id),
        "nonce": nonce_str,
        "pid": os.getpid(),
        "path": path,
    }

    def timeout_error(waited: float) -> Exception:
        holder = _read_lease(path)
        holder_text = f"nonce {holder['nonce']!r}, pid {holder.get('pid')!r}" if holder else "unknown owner"
        return RegistryLockTimeout(
            f"{path}: coordinator lease held by {holder_text} for {waited:g}s; "
            "age or PID alone never authorizes taking over or removing another "
            "owner's lock: recover explicitly after verifying the owner is gone"
        )

    if _read_lease(path) == nonce_str:
        yield snapshot
        return
    with file_lock(path, timeout_s, math.inf, timeout_error):
        write_atomic(
            path,
            json.dumps(
                {"nonce": nonce_str, "pid": os.getpid(), "acquired_at": _now_iso()},
                sort_keys=True,
            )
            + "\n",
        )
        yield snapshot


def string_nonce(nonce: Any) -> str:
    """Validate the owner nonce given to :func:`owner_lease`."""
    if not isinstance(nonce, str) or not nonce.strip() or "\x00" in nonce:
        raise RegistryError(f"owner nonce must be a non-empty string, got {nonce!r}")
    return nonce.strip()


def _coordinator_lease_path(root: Path, coordinator_id: Any) -> Path:
    """The lease file path, refusing traversal and symlink escapes."""
    canonical = _canonical_uuid(coordinator_id)
    if canonical is None:
        raise RegistryError(
            f"coordinator id must be a canonical UUID string, got {coordinator_id!r}"
        )
    current = Path(root)
    for part in (_COORDINATORS_DIR, canonical):
        current = current / part
        if current.is_symlink():
            raise RegistryError(
                f"{current}: coordinator path escapes the central root via a "
                "symbolic link; explicit recovery required"
            )
    current.mkdir(parents=True, exist_ok=True)
    return current / _LEASE


def _read_lease(path: Path) -> dict | None:
    """The lease payload, or ``None`` when absent/unreadable (never trusted).

    A lock written by :func:`records.file_lock` briefly holds only a PID; such
    snapshots come back ``None`` so contesting owners refuse instead of
    guessing whose lock they are looking at.
    """
    try:
        text, _ = _registry_text(path, RECORD_MAX_BYTES)
    except (RegistriesError_t := FileNotFoundError, NotADirectoryError):
        return None
    except (OSError, RegistryError):
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _registry_lock(root: Path, timeout_s: float) -> Iterator[Path]:
    """Hold :file:`registry.lock` for one registry.json rewrite.

    A :func:`records.file_lock` file whose stale takeover is disabled
    (``math.inf``): the lock records the owner's nonce, PID and acquisition
    time inside itself so a hung or crashed holder is recovered explicitly,
    never broken by mtime age or an unrelated PID.
    """
    path = Path(root) / _LOCK
    identity = {"nonce": uuid.uuid4().hex, "pid": os.getpid(), "acquired_at": _now_iso()}

    def timeout_error(waited: float) -> Exception:
        holder = _read_lease(path)
        holder_text = (
            f"nonce {holder['nonce']!r}, pid {holder.get('pid')!r}" if holder else "unknown owner"
        )
        return RegistryLockTimeout(
            f"{path}: registry lock held by {holder_text} for {waited:g}s; "
            "age or PID alone never authorizes taking over or removing another "
            "owner's lock: recover explicitly after verifying the owner is gone"
        )

    with file_lock(path, timeout_s, math.inf, timeout_error):
        write_atomic(path, json.dumps(identity, sort_keys=True) + "\n")
        yield path


# Keep the late alias so the name above reads exactly once.
RegistriesError_t = FileNotFoundError
