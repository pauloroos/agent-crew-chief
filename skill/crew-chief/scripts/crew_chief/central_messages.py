"""Durable coordinator inboxes and outboxes (FR-007, FR-008, FR-022, FR-024).

This module owns the per-coordinator ``inbox/``, ``outbox/`` and
``acknowledgments/`` directories under a central root.  Every message is one
JSON envelope addressed by a canonical UUID; delivery is at least once and
consumption is idempotent through acknowledgment files.

The root is always injected by the caller (``central_config.resolve_root``);
no setting is read here.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .records import write_atomic

__all__ = [
    "BODY_MAX_BYTES",
    "ENVELOPE_MAX_BYTES",
    "ENVELOPE_READ_MAX_BYTES",
    "MAX_BODY_BYTES",
    "MAX_ENVELOPE_BYTES",
    "MAX_MESSAGE_BYTES",
    "MESSAGE_BODY_MAX_BYTES",
    "MESSAGE_MAX_BYTES",
    "MESSAGE_READ_MAX_BYTES",
    "MessageError",
    "MessagesError",
    "CentralMessageError",
    "CentralMessagesError",
    "EnvelopeError",
    "acknowledge",
    "pending",
    "questions",
    "send",
]

# --- constants --------------------------------------------------------------

#: Plain-text body cap (plan: 64 KiB UTF-8).
BODY_MAX_BYTES = 64 * 1024
#: Whole-envelope cap (plan: 80 KiB).
ENVELOPE_MAX_BYTES = 80 * 1024
#: Bounded read cap for a single stored envelope (plan: 1 MiB per record).
ENVELOPE_READ_MAX_BYTES = 1 * 1024 * 1024

# Aliases kept for callers/tests that spell the caps differently.
MESSAGE_BODY_MAX_BYTES = BODY_MAX_BYTES
MAX_BODY_BYTES = BODY_MAX_BYTES
MESSAGE_MAX_BYTES = ENVELOPE_MAX_BYTES
MAX_ENVELOPE_BYTES = ENVELOPE_MAX_BYTES
MAX_MESSAGE_BYTES = ENVELOPE_MAX_BYTES
MESSAGE_READ_MAX_BYTES = ENVELOPE_READ_MAX_BYTES

_VALID_DIRECTIONS = ("in", "out")
_VALID_KINDS = ("message", "question", "answer", "status", "control")
_KNOWN_KEYS = frozenset(
    {
        "schema_version",
        "id",
        "coordinator_id",
        "direction",
        "kind",
        "created_at",
        "reply_to",
        "task_refs",
        "body",
    }
)
_SUBDIRS = ("inbox", "outbox", "acknowledgments")
_COORDINATORS_DIR = "coordinators"


class MessageError(Exception):
    """A message envelope cannot be validated, stored, read or acknowledged."""


# Aliases for callers that use a different spelling.
MessagesError = MessageError
CentralMessageError = MessageError
CentralMessagesError = MessageError
EnvelopeError = MessageError


SCHEMA_VERSION = 1


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_uuid(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return None
    return str(parsed) if str(parsed) == value else None


def _read_bounded(path: Path, max_bytes: int) -> bytes:
    with path.open("rb") as handle:
        data = handle.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise MessageError(
            f"{path}: exceeds the {max_bytes} byte read cap; explicit recovery required"
        )
    return data


def _utf8_size(text: str) -> int:
    try:
        return len(text.encode("utf-8"))
    except UnicodeError as exc:
        raise MessageError(f"text is not valid UTF-8: {exc}") from exc


def _coordinator_dir(root: Path, coordinator_id: Any, *, create: bool = False) -> Path:
    canonical = _canonical_uuid(coordinator_id)
    if canonical is None:
        raise MessageError(
            f"coordinator id must be a canonical UUID string, got {coordinator_id!r}"
        )
    root = Path(root)
    root_resolved = root.resolve(strict=False)
    current = root
    for part in (_COORDINATORS_DIR, canonical):
        current = current / part
        if current.is_symlink():
            raise MessageError(
                f"{current}: coordinator path escapes the central root via a "
                "symbolic link; explicit recovery required"
            )
        if create:
            current.mkdir(parents=True, exist_ok=True)
        if not current.resolve(strict=False).is_relative_to(root_resolved):
            raise MessageError(
                f"{current}: coordinator path escapes the central root; explicit recovery required"
            )
    if create:
        for sub in _SUBDIRS:
            subpath = current / sub
            if subpath.is_symlink():
                raise MessageError(
                    f"{subpath}: coordinator path escapes the central root via a "
                    "symbolic link; explicit recovery required"
                )
            subpath.mkdir(parents=True, exist_ok=True)
            if not subpath.resolve(strict=False).is_relative_to(root_resolved):
                raise MessageError(
                    f"{subpath}: coordinator path escapes the central root; explicit recovery required"
                )
    return current


def _validate_task_refs(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise MessageError(f"task_refs must be a list, got {type(value).__name__}")
    if len(value) > 1024:
        raise MessageError("task_refs exceeds the bounded item count")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item:
            raise MessageError(f"task_refs must contain non-empty strings, got {item!r}")
        if "\x00" in item:
            raise MessageError("task_refs must not contain NUL bytes")
        result.append(item)
    return result


def _validate_input(envelope: Any, coordinator_id: str) -> dict:
    """Validate and normalize an envelope handed to :func:`send`."""
    if not isinstance(envelope, dict):
        raise MessageError(f"envelope must be a dict, got {type(envelope).__name__}")

    unknown = sorted(set(envelope) - _KNOWN_KEYS)
    if unknown:
        raise MessageError(f"unknown envelope field(s): {', '.join(unknown)}")

    canonical_coordinator = _canonical_uuid(coordinator_id)
    if canonical_coordinator is None:
        raise MessageError(
            f"coordinator id must be a canonical UUID string, got {coordinator_id!r}"
        )

    env_coordinator = envelope.get("coordinator_id", canonical_coordinator)
    env_coordinator_canonical = _canonical_uuid(env_coordinator)
    if env_coordinator_canonical is None or env_coordinator_canonical != canonical_coordinator:
        raise MessageError(
            f"envelope coordinator_id {env_coordinator!r} does not match {coordinator_id!r}"
        )

    schema_version = envelope.get("schema_version", SCHEMA_VERSION)
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != SCHEMA_VERSION
    ):
        raise MessageError(
            f"schema_version must be {SCHEMA_VERSION}, got {schema_version!r}"
        )

    message_id = envelope.get("id")
    if message_id is None:
        message_id = str(uuid.uuid4())
    message_id_canonical = _canonical_uuid(message_id)
    if message_id_canonical is None:
        raise MessageError(f"id must be a canonical UUID string, got {message_id!r}")
    message_id = message_id_canonical

    direction = envelope.get("direction")
    if direction not in _VALID_DIRECTIONS:
        raise MessageError(f"direction must be one of {_VALID_DIRECTIONS}, got {direction!r}")

    kind = envelope.get("kind")
    if kind not in _VALID_KINDS:
        raise MessageError(f"kind must be one of {_VALID_KINDS}, got {kind!r}")

    created_at = envelope.get("created_at")
    if created_at is None:
        created_at = _now_iso()
    if not isinstance(created_at, str) or not created_at:
        raise MessageError(f"created_at must be a non-empty string, got {created_at!r}")

    reply_to = envelope.get("reply_to", None)
    if reply_to is not None:
        reply_to_canonical = _canonical_uuid(reply_to)
        if reply_to_canonical is None:
            raise MessageError(f"reply_to must be null or a canonical UUID, got {reply_to!r}")
        reply_to = reply_to_canonical

    task_refs = envelope.get("task_refs", [])
    task_refs = _validate_task_refs(task_refs)

    body = envelope.get("body", _MISSING := object())
    if body is _MISSING:
        raise MessageError("body is required")
    if not isinstance(body, str):
        raise MessageError(f"body must be text, got {type(body).__name__}")
    if _utf8_size(body) > BODY_MAX_BYTES:
        raise MessageError(f"body exceeds {BODY_MAX_BYTES} UTF-8 bytes")

    normalized = {
        "schema_version": SCHEMA_VERSION,
        "id": message_id,
        "coordinator_id": canonical_coordinator,
        "direction": direction,
        "kind": kind,
        "created_at": created_at,
        "reply_to": reply_to,
        "task_refs": task_refs,
        "body": body,
    }

    # Whole-envelope cap.
    try:
        envelope_bytes = len(json.dumps(normalized, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise MessageError(f"envelope cannot be serialized: {exc}") from exc
    if envelope_bytes > ENVELOPE_MAX_BYTES:
        raise MessageError(f"envelope exceeds {ENVELOPE_MAX_BYTES} UTF-8 bytes")

    return normalized


def _validate_stored(envelope: Any, *, coordinator_id: str | None = None,
                     direction: str | None = None) -> dict:
    """Validate an envelope read from disk; missing fields are rejected."""
    if not isinstance(envelope, dict):
        raise MessageError(f"envelope must be an object, got {type(envelope).__name__}")

    if set(envelope) != _KNOWN_KEYS:
        missing = sorted(_KNOWN_KEYS - set(envelope))
        extra = sorted(set(envelope) - _KNOWN_KEYS)
        problems = []
        if missing:
            problems.append(f"missing field(s): {', '.join(missing)}")
        if extra:
            problems.append(f"unknown field(s): {', '.join(extra)}")
        raise MessageError("; ".join(problems))

    schema_version = envelope.get("schema_version")
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != SCHEMA_VERSION
    ):
        raise MessageError(f"schema_version must be {SCHEMA_VERSION}, got {schema_version!r}")

    message_id = _canonical_uuid(envelope.get("id"))
    if message_id is None or message_id != envelope.get("id"):
        raise MessageError(f"id must be a canonical UUID string, got {envelope.get('id')!r}")

    env_coordinator = _canonical_uuid(envelope.get("coordinator_id"))
    if env_coordinator is None:
        raise MessageError(
            f"coordinator_id must be a canonical UUID string, got {envelope.get('coordinator_id')!r}"
        )
    if coordinator_id is not None:
        expected = _canonical_uuid(coordinator_id)
        if expected is None or env_coordinator != expected:
            raise MessageError(
                f"envelope coordinator_id {envelope.get('coordinator_id')!r} "
                f"does not match {coordinator_id!r}"
            )

    direction_value = envelope.get("direction")
    if direction_value not in _VALID_DIRECTIONS:
        raise MessageError(f"direction must be one of {_VALID_DIRECTIONS}, got {direction_value!r}")
    if direction is not None and direction_value != direction:
        raise MessageError(
            f"envelope direction {direction_value!r} does not match {direction!r}"
        )

    kind = envelope.get("kind")
    if kind not in _VALID_KINDS:
        raise MessageError(f"kind must be one of {_VALID_KINDS}, got {kind!r}")

    created_at = envelope.get("created_at")
    if not isinstance(created_at, str) or not created_at:
        raise MessageError(f"created_at must be a non-empty string, got {created_at!r}")

    reply_to = envelope.get("reply_to")
    if reply_to is not None:
        reply_to_canonical = _canonical_uuid(reply_to)
        if reply_to_canonical is None or reply_to_canonical != reply_to:
            raise MessageError(f"reply_to must be null or a canonical UUID, got {reply_to!r}")

    task_refs = _validate_task_refs(envelope.get("task_refs"))

    body = envelope.get("body")
    if not isinstance(body, str):
        raise MessageError(f"body must be text, got {type(body).__name__}")
    if _utf8_size(body) > BODY_MAX_BYTES:
        raise MessageError(f"body exceeds {BODY_MAX_BYTES} UTF-8 bytes")

    try:
        envelope_bytes = len(json.dumps(envelope, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise MessageError(f"envelope cannot be serialized: {exc}") from exc
    if envelope_bytes > ENVELOPE_MAX_BYTES:
        raise MessageError(f"envelope exceeds {ENVELOPE_MAX_BYTES} UTF-8 bytes")

    return {
        "schema_version": SCHEMA_VERSION,
        "id": message_id,
        "coordinator_id": env_coordinator,
        "direction": direction_value,
        "kind": kind,
        "created_at": created_at,
        "reply_to": reply_to,
        "task_refs": list(task_refs),
        "body": body,
    }


def _read_envelope(path: Path, root: Path) -> dict:
    if path.is_symlink():
        raise MessageError(f"{path}: symbolic link refused; explicit recovery required")
    root_resolved = Path(root).resolve(strict=False)
    try:
        if not path.resolve(strict=False).is_relative_to(root_resolved):
            raise MessageError(f"{path}: path escapes the central root")
    except (OSError, ValueError) as exc:
        raise MessageError(f"{path}: cannot resolve path: {exc}") from exc
    if not path.is_file():
        raise MessageError(f"{path}: message file is missing")
    data = _read_bounded(path, ENVELOPE_READ_MAX_BYTES)
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise MessageError(f"{path}: invalid UTF-8: {exc}") from exc
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MessageError(
            f"{path}: invalid JSON at line {exc.lineno}: {exc.msg}"
        ) from exc
    return _validate_stored(obj)


def _safe_glob(directory: Path) -> Iterator[Path]:
    if not directory.is_dir():
        return
    try:
        entries = sorted(directory.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.is_symlink():
            raise MessageError(
                f"{entry}: symbolic link refused; explicit recovery required"
            )
        yield entry


def send(root: Path, coordinator_id: str, envelope: dict) -> str:
    """Validate and durably store one envelope; return its message id."""
    root = Path(root)
    normalized = _validate_input(envelope, coordinator_id)

    coord_dir = _coordinator_dir(root, coordinator_id, create=True)
    direction = normalized["direction"]
    subdir = coord_dir / ("inbox" if direction == "in" else "outbox")
    subdir.mkdir(parents=True, exist_ok=True)
    if subdir.is_symlink():
        raise MessageError(f"{subdir}: symbolic link refused")

    message_id = normalized["id"]
    path = subdir / f"{message_id}.json"
    root_resolved = root.resolve(strict=False)
    if path.is_symlink():
        raise MessageError(f"{path}: symbolic link refused")
    try:
        if not path.resolve(strict=False).is_relative_to(root_resolved):
            raise MessageError(f"{path}: path escapes the central root")
    except (OSError, ValueError) as exc:
        raise MessageError(f"{path}: cannot resolve path: {exc}") from exc

    # Idempotent redelivery: a matching envelope already on disk wins.
    if path.exists():
        try:
            existing = _read_envelope(path, root)
        except MessageError:
            raise
        if existing == normalized:
            return message_id
        raise MessageError(
            f"message {message_id!r} already exists with a different envelope"
        )

    payload = json.dumps(normalized, indent=2, ensure_ascii=False) + "\n"
    write_atomic(path, payload)
    return message_id


def pending(root: Path, coordinator_id: str, direction: str) -> list[dict]:
    """Return unacknowledged envelopes for one coordinator and direction."""
    if direction not in _VALID_DIRECTIONS:
        raise MessageError(
            f"direction must be one of {_VALID_DIRECTIONS}, got {direction!r}"
        )
    root = Path(root)
    coord_dir = _coordinator_dir(root, coordinator_id, create=False)
    subdir = coord_dir / ("inbox" if direction == "in" else "outbox")
    ack_dir = coord_dir / "acknowledgments"

    results: list[dict] = []
    if not subdir.exists():
        return results

    acked: set[str] = set()
    if ack_dir.exists() and ack_dir.is_dir() and not ack_dir.is_symlink():
        for ack_path in _safe_glob(ack_dir):
            if not ack_path.is_file() or ack_path.suffix != ".json":
                continue
            stem = ack_path.stem
            if _canonical_uuid(stem) is None:
                continue
            try:
                data = _read_bounded(ack_path, ENVELOPE_READ_MAX_BYTES)
                obj = json.loads(data.decode("utf-8-sig"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, MessageError):
                # A corrupt ack never hides a message; redeliver it.
                continue
            if isinstance(obj, dict):
                acked.add(stem)

    for path in _safe_glob(subdir):
        if not path.is_file() or path.suffix != ".json":
            continue
        if path.is_symlink():
            continue
        stem = path.stem
        if _canonical_uuid(stem) is None:
            continue
        try:
            envelope = _read_envelope(path, root)
        except MessageError:
            # Isolate corrupt/stale/incompatible records; keep them on disk.
            continue
        if envelope["id"] != stem:
            continue
        if envelope["coordinator_id"] != _canonical_uuid(coordinator_id):
            continue
        if envelope["direction"] != direction:
            continue
        if stem in acked:
            continue
        results.append(dict(envelope))

    results.sort(key=lambda item: (item.get("created_at", ""), item.get("id", "")))
    return results


def acknowledge(root: Path, coordinator_id: str, message_id: str, round_id: str) -> None:
    """Record that ``message_id`` was consumed in ``round_id``."""
    root = Path(root)
    message_uuid = _canonical_uuid(message_id)
    if message_uuid is None:
        raise MessageError(f"message id must be a canonical UUID string, got {message_id!r}")
    round_uuid = _canonical_uuid(round_id)
    if round_uuid is None:
        raise MessageError(f"round id must be a canonical UUID string, got {round_id!r}")

    coord_dir = _coordinator_dir(root, coordinator_id, create=False)
    # The message must exist in either box before it can be acknowledged.
    found = False
    for sub in ("inbox", "outbox"):
        candidate = coord_dir / sub / f"{message_uuid}.json"
        if candidate.is_file() and not candidate.is_symlink():
            try:
                _read_envelope(candidate, root)
                found = True
                break
            except MessageError:
                continue
    if not found:
        raise MessageError(
            f"message {message_uuid!r} does not exist for coordinator {coordinator_id!r}"
        )

    ack_dir = coord_dir / "acknowledgments"
    ack_dir.mkdir(parents=True, exist_ok=True)
    if ack_dir.is_symlink():
        raise MessageError(f"{ack_dir}: symbolic link refused")
    ack_path = ack_dir / f"{message_uuid}.json"
    if ack_path.is_symlink():
        raise MessageError(f"{ack_path}: symbolic link refused")

    now = _now_iso()
    payload = {
        "message_id": message_uuid,
        "id": message_uuid,
        "round_id": round_uuid,
        "consumed_round_id": round_uuid,
        "round": round_uuid,
        "acknowledged_at": now,
        "consumed_at": now,
        "time": now,
    }
    write_atomic(ack_path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def questions(root: Path) -> list[dict]:
    """Return open (unanswered) question envelopes across all coordinators."""
    root = Path(root)
    root_resolved = root.resolve(strict=False)
    coordinators_dir = root / _COORDINATORS_DIR
    results: list[dict] = []

    if not coordinators_dir.exists() or not coordinators_dir.is_dir():
        return results
    if coordinators_dir.is_symlink():
        raise MessageError(f"{coordinators_dir}: symbolic link refused")

    for coord_entry in _safe_glob(coordinators_dir):
        if not coord_entry.is_dir():
            continue
        coordinator_id = coord_entry.name
        if _canonical_uuid(coordinator_id) is None:
            continue
        try:
            coord_dir = _coordinator_dir(root, coordinator_id, create=False)
        except MessageError:
            continue
        if not coord_dir.resolve(strict=False).is_relative_to(root_resolved):
            continue

        inbox_dir = coord_dir / "inbox"
        outbox_dir = coord_dir / "outbox"

        # Correlate answers to questions by reply_to.
        answered: set[str] = set()
        for directory in (inbox_dir, outbox_dir):
            if not directory.exists() or not directory.is_dir() or directory.is_symlink():
                continue
            for path in _safe_glob(directory):
                if not path.is_file() or path.suffix != ".json":
                    continue
                try:
                    envelope = _read_envelope(path, root)
                except MessageError:
                    continue
                if envelope.get("kind") == "answer" and envelope.get("reply_to") is not None:
                    answered.add(str(envelope["reply_to"]))

        if not outbox_dir.exists() or not outbox_dir.is_dir() or outbox_dir.is_symlink():
            continue
        for path in _safe_glob(outbox_dir):
            if not path.is_file() or path.suffix != ".json":
                continue
            try:
                envelope = _read_envelope(path, root)
            except MessageError:
                continue
            if envelope.get("kind") != "question":
                continue
            if envelope.get("id") in answered:
                continue
            results.append(dict(envelope))

    results.sort(key=lambda item: (item.get("created_at", ""), item.get("id", "")))
    return results


# Module-level imports retained for callers and tests that reference them.
