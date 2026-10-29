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
import re
import shutil
import uuid
import warnings
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .records import write_atomic
from .central_registry import RegistryError, _read_prefix, _registry_lock

__all__ = [
    "ATTACHMENT_MAX_FILES",
    "ATTACHMENT_MAX_FILE_BYTES",
    "ATTACHMENT_MAX_TOTAL_BYTES",
    "BODY_MAX_BYTES",
    "ENVELOPE_MAX_BYTES",
    "ENVELOPE_READ_MAX_BYTES",
    "MessageError",
    "acknowledge",
    "attachment_paths",
    "owner_message",
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
#: Attachment caps (T048): 20 files, 10 MiB each, 25 MiB per message.
ATTACHMENT_MAX_FILES = 20
ATTACHMENT_MAX_FILE_BYTES = 10 * 1024 * 1024
ATTACHMENT_MAX_TOTAL_BYTES = 25 * 1024 * 1024
#: Sanitized attachment base name cap, before the "<index>-" prefix.
ATTACHMENT_MAX_NAME_CHARS = 100

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
        "attachments",
    }
)
_REQUIRED_KEYS = frozenset(
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
    data = _read_prefix(path, max_bytes + 1)
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
        if not current.resolve(strict=False).is_relative_to(root_resolved):
            raise MessageError(
                f"{current}: coordinator path escapes the central root; explicit recovery required"
            )
        if create:
            current.mkdir(parents=True, exist_ok=True)
    for sub in _SUBDIRS:
        subpath = current / sub
        if subpath.is_symlink():
            raise MessageError(
                f"{subpath}: coordinator path escapes the central root via a "
                "symbolic link; explicit recovery required"
            )
        if not subpath.resolve(strict=False).is_relative_to(root_resolved):
            raise MessageError(
                f"{subpath}: coordinator path escapes the central root; explicit recovery required"
            )
        if create:
            subpath.mkdir(parents=True, exist_ok=True)
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


# --- attachments (T048) ------------------------------------------------------

#: Text/markdown/source extensions whose bytes must decode as UTF-8 with no NUL.
#: svg is intentionally excluded (vector markup is not accepted as an attachment).
_TEXT_EXTS = frozenset({
    "txt", "md", "markdown", "py", "js", "mjs", "cjs", "ts", "tsx", "jsx",
    "json", "toml", "yaml", "yml", "ini", "cfg", "css", "scss", "html",
    "htm", "xml", "sh", "bash", "ps1", "bat", "cmd", "rs", "go", "java",
    "kt", "c", "h", "cpp", "hpp", "cs", "rb", "php", "sql", "csv", "tsv",
    "log", "diff", "patch", "r", "swift", "lua", "pl",
})

#: Windows reserved stems, checked case-insensitively without the extension.
_WINDOWS_RESERVED = frozenset({
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
})

#: Stored attachment types.
_ATTACHMENT_TYPES = frozenset({"png", "jpg", "gif", "webp", "pdf", "text"})

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_UNSAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._ \-]")


def _attachment_kind(filename: str, data: bytes) -> str | None:
    """The stored type for ``(filename, data)``, or ``None`` when refused."""
    name = filename if isinstance(filename, str) else ""
    stripped = name.rstrip(". ")
    ext = stripped.rsplit(".", 1)[-1].lower() if "." in stripped else ""
    if ext == "jpeg":
        ext = "jpg"
    if ext in ("png", "jpg", "gif", "webp", "pdf"):
        if ext == "png" and data.startswith(b"\x89PNG\r\n\x1a\n"):
            return "png"
        if ext == "jpg" and data.startswith(b"\xff\xd8\xff"):
            return "jpg"
        if ext == "gif" and (data.startswith(b"GIF87a") or data.startswith(b"GIF89a")):
            return "gif"
        if ext == "webp" and len(data) >= 12 and data[0:4] == b"RIFF" and data[8:12] == b"WEBP":
            return "webp"
        if ext == "pdf" and data.startswith(b"%PDF-"):
            return "pdf"
        return None
    if ext in _TEXT_EXTS:
        if b"\x00" in data:
            return None
        try:
            data.decode("utf-8")
        except (UnicodeDecodeError, ValueError):
            return None
        return "text"
    return None


def _sanitize_attachment_base(original: str) -> str:
    """Sanitize one original filename to at most 100 chars (no index prefix)."""
    name = original.replace("\\", "/").split("/")[-1]
    if not name or name in (".", ".."):
        name = "file"
    # Control characters are removed; anything outside the allowlist becomes _.
    name = _CONTROL_RE.sub("", name)
    name = _UNSAFE_NAME_RE.sub("_", name)
    if not name:
        name = "file"
    # No leading dot, no trailing dot/space (Windows-safe).
    name = name.lstrip(".")
    if not name:
        name = "file"
    name = name.rstrip(". ")
    if not name:
        name = "file"
    stem = name.split(".", 1)[0] if "." in name else name
    if stem.upper() in _WINDOWS_RESERVED:
        name = "_" + name
    if len(name) > ATTACHMENT_MAX_NAME_CHARS:
        if "." in name and not name.startswith("."):
            stem_part, ext_part = name.rsplit(".", 1)
            ext_with_dot = "." + ext_part
            if len(ext_with_dot) >= ATTACHMENT_MAX_NAME_CHARS:
                name = name[:ATTACHMENT_MAX_NAME_CHARS]
            else:
                name = stem_part[: ATTACHMENT_MAX_NAME_CHARS - len(ext_with_dot)] + ext_with_dot
        else:
            name = name[:ATTACHMENT_MAX_NAME_CHARS]
        name = name.rstrip(". ") or "file"
    return name


def _raw_attachment_name(item: Any) -> str:
    if isinstance(item, (tuple, list)) and len(item) == 2 and isinstance(item[0], str):
        return item[0]
    if isinstance(item, dict) and isinstance(item.get("name"), str):
        return item["name"]
    return "attachment"


def _split_raw_attachment(item: Any) -> tuple[str, bytes | None, bool]:
    """Return ``(name, data, is_raw)``; metadata-only items have ``data=None``."""
    if isinstance(item, (tuple, list)) and len(item) == 2:
        name, data = item
        if isinstance(name, str) and isinstance(data, (bytes, bytearray)):
            return name, bytes(data), True
        return name if isinstance(name, str) else "attachment", None, True
    if isinstance(item, dict):
        name = item.get("name")
        for key in ("data", "bytes", "content"):
            value = item.get(key)
            if isinstance(value, (bytes, bytearray)):
                return name if isinstance(name, str) else "attachment", bytes(value), True
        if isinstance(name, str) and isinstance(item.get("size"), int) and isinstance(item.get("type"), str):
            return name, None, False
        return name if isinstance(name, str) else "attachment", None, True
    return "attachment", None, True


def _prepare_attachments(raw: Any) -> tuple[list[dict], list[tuple[str, bytes]]]:
    """Validate raw attachments; return ``(metadata, [(final_name, data)])``."""
    if not isinstance(raw, (list, tuple)):
        raise MessageError("attachments must be a list of (name, bytes) pairs")
    items = list(raw)
    if len(items) > ATTACHMENT_MAX_FILES:
        raise MessageError(
            f"{_raw_attachment_name(items[ATTACHMENT_MAX_FILES])}: "
            f"too many files (at most {ATTACHMENT_MAX_FILES} files per message)"
        )
    metadata: list[dict] = []
    prepared: list[tuple[str, bytes]] = []
    total = 0
    for index, item in enumerate(items, start=1):
        name, data, is_raw = _split_raw_attachment(item)
        display = name if isinstance(name, str) and name else f"attachment {index}"
        if not isinstance(name, str) or not name:
            raise MessageError(f"{display}: invalid name")
        if not is_raw or data is None:
            # Metadata-only input is never accepted for a new message: files
            # would be missing. Name the file per the fixed vocabulary.
            raise MessageError(f"{display}: type not accepted")
        if not isinstance(data, bytes):
            raise MessageError(f"{display}: type not accepted")
        if len(data) > ATTACHMENT_MAX_FILE_BYTES:
            raise MessageError(
                f"{display}: file too large (at most 10 MiB per file)"
            )
        total += len(data)
        if total > ATTACHMENT_MAX_TOTAL_BYTES:
            raise MessageError(
                f"{display}: message too large (at most 25 MiB per message)"
            )
        kind = _attachment_kind(name, data)
        if kind is None:
            raise MessageError(f"{display}: type not accepted")
        base = _sanitize_attachment_base(name)
        final = f"{index}-{base}"
        metadata.append({"name": final, "size": len(data), "type": kind})
        prepared.append((final, data))
    return metadata, prepared


def _validate_attachment_metadata(value: Any) -> list[dict]:
    if not isinstance(value, list):
        raise MessageError("attachments must be a list")
    if len(value) > ATTACHMENT_MAX_FILES:
        raise MessageError("attachments exceeds the bounded item count")
    seen: set[str] = set()
    total = 0
    result: list[dict] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"name", "size", "type"}:
            raise MessageError("attachment entries must be {name, size, type} objects")
        name, size, kind = item["name"], item["size"], item["type"]
        if not isinstance(name, str) or not name or len(name) > 128:
            raise MessageError(f"attachment name is invalid: {name!r}")
        if "/" in name or "\\" in name or "\x00" in name or name.startswith("."):
            raise MessageError(f"attachment name is invalid: {name!r}")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0 or size > ATTACHMENT_MAX_FILE_BYTES:
            raise MessageError(f"attachment size is invalid: {name!r}")
        if kind not in _ATTACHMENT_TYPES:
            raise MessageError(f"attachment type is invalid: {name!r}")
        if name in seen:
            raise MessageError(f"duplicate attachment name: {name!r}")
        seen.add(name)
        total += size
        if total > ATTACHMENT_MAX_TOTAL_BYTES:
            raise MessageError("attachments exceed 25 MiB per message")
        result.append({"name": name, "size": size, "type": kind})
    return result


def _attachments_base(root: Path, coordinator_id: str, *, create: bool = False) -> Path:
    coord = _coordinator_dir(root, coordinator_id, create=create)
    base = coord / "attachments"
    if base.is_symlink():
        raise MessageError(
            f"{base}: coordinator path escapes the central root via a "
            "symbolic link; explicit recovery required"
        )
    root_resolved = Path(root).resolve(strict=False)
    try:
        resolved = base.resolve(strict=False)
    except (OSError, ValueError) as exc:
        raise MessageError(f"{base}: cannot resolve attachments path: {exc}") from exc
    coord_resolved = coord.resolve(strict=False)
    if resolved != coord_resolved / "attachments":
        raise MessageError(f"{base}: coordinator path escapes the central root")
    if not resolved.is_relative_to(root_resolved):
        raise MessageError(f"{base}: coordinator path escapes the central root")
    if base.exists() and not base.is_dir():
        raise MessageError(f"{base}: attachments is not a directory")
    if create:
        base.mkdir(parents=True, exist_ok=True)
    return base


def _store_attachment_files(root: Path, coordinator_id: str, message_id: str,
                             prepared: list[tuple[str, bytes]]) -> None:
    """Write validated attachment bytes; refuse symlinks, clean up on failure."""
    if _canonical_uuid(message_id) is None:
        raise MessageError(f"message id must be a canonical UUID string, got {message_id!r}")
    base = _attachments_base(root, coordinator_id, create=True)
    message_dir = base / message_id
    if message_dir.is_symlink():
        raise MessageError(f"{message_dir}: symbolic link refused; explicit recovery required")
    try:
        if message_dir.resolve(strict=False).parent != base.resolve(strict=False):
            raise MessageError(f"{message_dir}: path escapes the central root")
    except (OSError, ValueError) as exc:
        raise MessageError(f"{message_dir}: cannot resolve path: {exc}") from exc
    try:
        if message_dir.exists():
            if not message_dir.is_dir():
                raise MessageError(f"{message_dir}: attachments directory is not a directory")
            shutil.rmtree(message_dir, ignore_errors=True)
        message_dir.mkdir(parents=True, exist_ok=False)
        for final_name, data in prepared:
            if "/" in final_name or "\\" in final_name or not final_name:
                raise MessageError(f"attachment name is invalid: {final_name!r}")
            target = message_dir / final_name
            if target.is_symlink():
                raise MessageError(f"{target}: symbolic link refused")
            try:
                if target.resolve(strict=False).parent != message_dir.resolve(strict=False):
                    raise MessageError(f"{target}: path escapes the central root")
            except (OSError, ValueError) as exc:
                raise MessageError(f"{target}: cannot resolve path: {exc}") from exc
            with target.open("xb") as handle:
                handle.write(data)
    except Exception:
        shutil.rmtree(message_dir, ignore_errors=True)
        raise


def _remove_attachment_files(root: Path, coordinator_id: str, message_id: str) -> None:
    try:
        base = _attachments_base(root, coordinator_id, create=False)
    except MessageError:
        return
    message_dir = base / message_id
    try:
        if message_dir.is_symlink() or not message_dir.is_dir():
            return
        if message_dir.resolve(strict=False).parent != base.resolve(strict=False):
            return
    except (OSError, ValueError):
        return
    shutil.rmtree(message_dir, ignore_errors=True)


def attachment_paths(root: Path, coordinator_id: str, message: dict) -> dict:
    """Copy ``message`` adding absolute ``path`` to each attachment (no writes)."""
    copied = dict(message)
    attachments = message.get("attachments")
    if not attachments:
        return copied
    if not isinstance(attachments, list):
        return copied
    enriched = []
    for item in attachments:
        if not isinstance(item, dict):
            continue
        entry = dict(item)
        name = entry.get("name")
        if isinstance(name, str) and name and "/" not in name and "\\" not in name:
            entry["path"] = str(
                (Path(root) / _COORDINATORS_DIR / coordinator_id
                 / "attachments" / str(message.get("id")) / name).absolute()
            )
        enriched.append(entry)
    copied["attachments"] = enriched
    return copied


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

    if "body" not in envelope:
        raise MessageError("body is required")
    body = envelope["body"]
    if not isinstance(body, str):
        raise MessageError(f"body must be text, got {type(body).__name__}")
    if _utf8_size(body) > BODY_MAX_BYTES:
        raise MessageError(f"body exceeds {BODY_MAX_BYTES} UTF-8 bytes")

    attachments_value = envelope.get("attachments", None)
    attachments: list[dict] | None = None
    if attachments_value is not None:
        if isinstance(attachments_value, list) and len(attachments_value) == 0:
            attachments = None
        else:
            attachments = _validate_attachment_metadata(attachments_value)

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
    if attachments is not None:
        normalized["attachments"] = attachments

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

    if set(envelope) != _KNOWN_KEYS and set(envelope) != _REQUIRED_KEYS:
        missing = sorted(_REQUIRED_KEYS - set(envelope))
        extra = sorted(set(envelope) - _KNOWN_KEYS)
        problems = []
        if missing:
            problems.append(f"missing field(s): {', '.join(missing)}")
        if extra:
            problems.append(f"unknown field(s): {', '.join(extra)}")
        raise MessageError("; ".join(problems) or "invalid envelope fields")

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

    attachments: list[dict] | None = None
    if "attachments" in envelope:
        attachments = _validate_attachment_metadata(envelope.get("attachments"))

    try:
        envelope_bytes = len(json.dumps(envelope, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise MessageError(f"envelope cannot be serialized: {exc}") from exc
    if envelope_bytes > ENVELOPE_MAX_BYTES:
        raise MessageError(f"envelope exceeds {ENVELOPE_MAX_BYTES} UTF-8 bytes")

    result = {
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
    if attachments is not None:
        result["attachments"] = attachments
    return result


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
    except (ValueError, RecursionError) as exc:
        raise MessageError(f"{path}: invalid JSON: {exc}") from exc
    envelope = _validate_stored(obj)
    expected_direction = {"inbox": "in", "outbox": "out"}.get(path.parent.name)
    if (envelope["id"] != path.stem
            or envelope["coordinator_id"] != path.parent.parent.name
            or envelope["direction"] != expected_direction):
        raise MessageError(f"{path}: envelope does not match its filename/coordinator/direction")
    return envelope


def _warn(path: Path, error: object) -> None:
    warnings.warn(f"{path}: {error}", UserWarning, stacklevel=2)


def _safe_glob(directory: Path) -> Iterator[Path]:
    if not directory.exists():
        return
    if directory.is_symlink():
        raise MessageError(f"{directory}: symbolic link refused")
    try:
        yield from directory.iterdir()
    except OSError as exc:
        _warn(directory, exc)


def _envelopes(root: Path, coordinator_id: str) -> Iterator[dict]:
    coord = _coordinator_dir(root, coordinator_id)
    for box in ("inbox", "outbox"):
        for path in _safe_glob(coord / box):
            if path.suffix != ".json":
                continue
            try:
                yield _read_envelope(path, root)
            except (MessageError, OSError) as exc:
                _warn(path, exc)


@contextmanager
def _message_lock(root: Path):
    # Registry's bounded, nonce-owned mutex protects check-and-create as well
    # as the atomic replacement. A supervisor's owner_lease is never borrowed.
    try:
        with _registry_lock(root, 60.0):
            yield
    except RegistryError as exc:
        raise MessageError(str(exc)) from exc


def _find(root: Path, coordinator_id: str, identifier: str) -> dict | None:
    coord = _coordinator_dir(root, coordinator_id)
    found = []
    for box in ("inbox", "outbox"):
        path = coord / box / f"{identifier}.json"
        if path.is_symlink() or path.exists():
            found.append(_read_envelope(path, root))
    if len(found) > 1:
        raise MessageError(f"message {identifier}: duplicate ID across inbox/outbox")
    return found[0] if found else None


def _ack(path: Path) -> dict:
    if path.is_symlink():
        raise MessageError(f"{path}: symbolic ack refused")
    try:
        value = json.loads(_read_bounded(path, ENVELOPE_READ_MAX_BYTES).decode("utf-8-sig"))
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        raise MessageError(f"{path}: invalid ack: {exc}") from exc
    if not isinstance(value, dict) or set(value) != {"message_id", "round_id", "consumed_at"}:
        raise MessageError(f"{path}: invalid ack fields")
    if value["message_id"] != path.stem or _canonical_uuid(value["message_id"]) is None:
        raise MessageError(f"{path}: invalid ack message ID")
    if _canonical_uuid(value["round_id"]) is None:
        raise MessageError(f"{path}: invalid ack round ID")
    try:
        when = datetime.fromisoformat(value["consumed_at"].replace("Z", "+00:00"))
        if when.tzinfo is None:
            raise ValueError("missing timezone")
    except (ValueError, TypeError, AttributeError) as exc:
        raise MessageError(f"{path}: invalid ack time") from exc
    return value


def owner_message(coordinator_id: str, body: str, reply_to: str | None = None,
                  attachments: Any = None, *, question_id: str | None = None) -> dict:
    """The inbound envelope for an owner's text; a reply answers a stored question.

    ``attachments`` is an optional iterable of ``(name, bytes)`` pairs (or
    ``{name, data}`` dicts); raw bytes are validated and stored by :func:`send`.
    """
    if question_id is not None:
        reply_to = question_id
    envelope = {
        "coordinator_id": coordinator_id,
        "direction": "in",
        "kind": "answer" if reply_to else "message",
        "reply_to": reply_to,
        "body": body,
    }
    if attachments is not None:
        items = list(attachments)
        if items:
            packed = []
            for item in items:
                if isinstance(item, (tuple, list)) and len(item) == 2:
                    name, data = item
                    packed.append({"name": name, "data": bytes(data) if isinstance(data, (bytes, bytearray)) else data})
                elif isinstance(item, dict):
                    entry = dict(item)
                    for key in ("bytes", "content"):
                        if key in entry and "data" not in entry:
                            entry["data"] = entry.pop(key)
                    packed.append(entry)
                else:
                    packed.append(item)
            envelope["attachments"] = packed
    return envelope


def send(root: Path, coordinator_id: str, envelope: dict) -> str:
    """Persist once by ID; equivalent correlated answer retries reuse one ID."""
    root = Path(root)
    raw_attachments = envelope.get("attachments", None) if isinstance(envelope, dict) else None
    prepared: list[tuple[str, bytes]] | None = None
    metadata: list[dict] | None = None
    envelope_for_validation = envelope
    if isinstance(envelope, dict) and raw_attachments is not None:
        if isinstance(raw_attachments, list) and len(raw_attachments) == 0:
            envelope_for_validation = {k: v for k, v in envelope.items() if k != "attachments"}
        else:
            metadata, prepared = _prepare_attachments(raw_attachments)
            envelope_for_validation = dict(envelope)
            envelope_for_validation["attachments"] = metadata
    normalized = _validate_input(envelope_for_validation, coordinator_id)
    with _message_lock(root):
        coord = _coordinator_dir(root, coordinator_id, create=True)
        existing = _find(root, coordinator_id, normalized["id"])
        if existing is not None:
            if envelope.get("created_at") is None:
                normalized["created_at"] = existing["created_at"]
            if existing == normalized:
                return existing["id"]
            raise MessageError("message ID already exists with a different envelope")
        if normalized["kind"] == "answer":
            reply = normalized["reply_to"]
            question = _find(root, coordinator_id, reply) if reply else None
            if question is None or question["kind"] != "question":
                raise MessageError("answer reply_to must identify a stored question")
            for previous in _envelopes(root, coordinator_id):
                if previous["kind"] == "answer" and previous["reply_to"] == reply:
                    fields = ("coordinator_id", "direction", "kind", "reply_to", "task_refs", "body", "attachments")
                    if all(previous.get(key) == normalized.get(key) for key in fields):
                        return previous["id"]
                    raise MessageError("question already has a different answer")
        if prepared is not None:
            try:
                _store_attachment_files(root, coordinator_id, normalized["id"], prepared)
            except MessageError:
                raise
            except Exception as exc:
                raise MessageError(f"cannot store attachments: {exc}") from exc
        box = "inbox" if normalized["direction"] == "in" else "outbox"
        path = coord / box / f'{normalized["id"]}.json'
        try:
            write_atomic(path, json.dumps(normalized, ensure_ascii=False, indent=2) + "\n")
        except Exception:
            if prepared is not None:
                _remove_attachment_files(root, coordinator_id, normalized["id"])
            raise
    return normalized["id"]


def pending(root: Path, coordinator_id: str, direction: str) -> list[dict]:
    """Read unconsumed messages; invalid acks warn and cannot hide delivery."""
    if direction not in _VALID_DIRECTIONS:
        raise MessageError("invalid direction")
    root = Path(root)
    coord = _coordinator_dir(root, coordinator_id)
    result = []
    for item in _envelopes(root, coordinator_id):
        if item["direction"] != direction:
            continue
        path = coord / "acknowledgments" / f'{item["id"]}.json'
        if path.exists() or path.is_symlink():
            try:
                _ack(path)
            except MessageError as exc:
                _warn(path, exc)
            else:
                continue
        result.append(item)
    return sorted(result, key=lambda item: (item["created_at"], item["id"]))


def acknowledge(root: Path, coordinator_id: str, message_id: str, round_id: str) -> None:
    """Preserve the first valid consumption, never overwrite a corrupt history."""
    root = Path(root)
    if _canonical_uuid(message_id) is None or _canonical_uuid(round_id) is None:
        raise MessageError("message and round IDs must be canonical UUIDs")
    coord = _coordinator_dir(root, coordinator_id)
    with _message_lock(root):
        if _find(root, coordinator_id, message_id) is None:
            raise MessageError("message does not exist for this coordinator")
        path = coord / "acknowledgments" / f"{message_id}.json"
        if path.exists() or path.is_symlink():
            _ack(path)  # Corrupt acknowledgments require explicit recovery.
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"message_id": message_id, "round_id": round_id, "consumed_at": _now_iso()}
        write_atomic(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def questions(root: Path) -> list[dict]:
    """Read open correlated questions, reporting bad rows without losing others."""
    root = Path(root)
    coordinators = root / _COORDINATORS_DIR
    if coordinators.is_symlink():
        raise MessageError(f"{coordinators}: symbolic link refused")
    result = []
    for entry in _safe_glob(coordinators):
        if _canonical_uuid(entry.name) is None:
            _warn(entry, "invalid coordinator UUID")
            continue
        try:
            rows = list(_envelopes(root, entry.name))
        except MessageError as exc:
            _warn(entry, exc)
            continue
        by_id = {item["id"]: item for item in rows}
        answered = set()
        for answer in rows:
            if answer["kind"] == "answer":
                question = by_id.get(answer["reply_to"])
                if question is not None and question["kind"] == "question":
                    answered.add(question["id"])
                else:
                    _warn(entry, "answer has invalid question correlation")
        result.extend(item for item in rows
                      if item["kind"] == "question" and item["id"] not in answered)
    return sorted(result, key=lambda item: (item["created_at"], item["id"]))
