"""Coordinator usage accounting in the active job (FR-013, FR-025, SC-006).

One coordinator round reports what the model CLI spent on it.  This module
records that usage in the job the round belongs to, exactly once, and keeps
the accounting honest when the job is not known yet, when the process crashes
half way and when the coordinator switches jobs.

The rules:

* The event id is stable: ``central:<coordinator id>:<round id>``.  Every
  usage entry carries it, so one round can never be counted twice, in the
  same job or in another one.
* The round's usage payload is authoritative.  Counts the CLI did not report
  stay ``None``; a sum is derived only when both components are known.  When
  no count was reported, coordinator output can provide a marked
  ``characters / 4`` estimate. Central-session usage must be reported.
* The target job is bound in the round metadata *before* the job is updated.
  A crash, or a job switch, after that point can no longer reassign usage
  that was already spent.
* Until a job is known the usage stays pending in the round metadata;
  :func:`backfill_usage` counts it once, when the job appears.
* The job record is changed through :func:`records.update_job`, so the entry
  is appended under the job lock and the duplicate check is atomic.
"""

from __future__ import annotations

import json
import os
import re
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import central_messages, central_rounds, records

__all__ = [
    "UsageError",
    "backfill_usage",
    "pending_usage",
    "read_pending_usage",
    "record_round_usage",
]

#: Bounded read of one round metadata file.
ROUND_READ_MAX_BYTES = 1024 * 1024

#: The roles a usage entry may carry (see :data:`records.USAGE_ROLES`).
ROLES = ("coordinator", "central")

#: Characters per token in the fallback estimate.
CHARS_PER_TOKEN = 4


class UsageError(ValueError):
    """A usage event cannot be read, bound or recorded."""


# --- small helpers ----------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _count(value: Any) -> int | None:
    """A non-negative token count, or ``None`` when it is unknown."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _job_id(value: Any) -> str | None:
    """A job id safe to use as a file name, or ``None``."""
    if not isinstance(value, str) or re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,127}", value) is None:
        return None
    return value


def _coordinator_of(record: Any) -> str:
    """The canonical coordinator id of a registry record."""
    if not isinstance(record, dict):
        raise UsageError("the coordinator record must be an object")
    coordinator_id = central_messages._canonical_uuid(record.get("id"))
    if coordinator_id is None:
        raise UsageError(
            "the coordinator id must be a canonical UUID string, "
            f"got {record.get('id')!r}"
        )
    return coordinator_id


def _worktree_of(record: dict) -> Path:
    """The worktree that holds the coordinator's job records."""
    value = record.get("worktree")
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        raise UsageError(
            f"the coordinator record has no absolute worktree path, got {value!r}"
        )
    return Path(value)


# --- the round metadata -----------------------------------------------------


def _coord_dir(root: Path, coordinator_id: str) -> Path:
    try:
        return central_messages._coordinator_dir(root, coordinator_id)
    except central_messages.MessageError as exc:
        raise UsageError(str(exc)) from exc


def _rounds_dir(root: Path, coordinator_id: str) -> Path:
    """The rounds directory, guarded like the accepted round module."""
    try:
        return central_rounds._subdir(_coord_dir(root, coordinator_id), "rounds")
    except (central_messages.MessageError, central_rounds.RoundError) as exc:
        raise UsageError(str(exc)) from exc


def _round_path(root: Path, coordinator_id: str, round_id: str) -> Path:
    """The metadata file of one round, refused when it escapes the root."""
    rounds = _rounds_dir(root, coordinator_id)
    path = rounds / f"{round_id}.json"
    base = rounds.resolve(strict=False)
    if path.is_symlink() or not path.resolve(strict=False).is_relative_to(base):
        raise UsageError(f"{path}: the round path escapes the coordinator directory")
    return path


def _read_round(path: Path) -> tuple[dict, bool, str]:
    """The round metadata, whether it had a BOM and its newline style."""
    try:
        data = central_messages._read_bounded(path, ROUND_READ_MAX_BYTES)
    except (OSError, central_messages.MessageError) as exc:
        raise UsageError(f"cannot read the round metadata: {exc}") from exc
    bom = data.startswith(b"\xef\xbb\xbf")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise UsageError(f"the round metadata is not valid UTF-8: {exc}") from exc
    newline = "\r\n" if "\r\n" in text else "\n"
    try:
        meta = json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise UsageError(f"the round metadata is not valid JSON: {exc}") from exc
    if not isinstance(meta, dict):
        raise UsageError("the round metadata is not a JSON object")
    return meta, bom, newline


def _write_round(path: Path, meta: dict, bom: bool, newline: str) -> None:
    """Write the round metadata back, keeping its BOM and newline style."""
    payload = json.dumps(meta, ensure_ascii=False, indent=2)
    if newline == "\r\n":
        payload = payload.replace("\n", "\r\n")
    text = ("\ufeff" if bom else "") + payload + newline
    try:
        records.write_atomic(path, text)
    except OSError as exc:
        raise UsageError(f"cannot persist the round metadata: {exc}") from exc


def _round_paths(root: Path, coordinator_id: str) -> list[Path]:
    """Every round file of a coordinator with a canonical UUID filename, oldest first."""
    items: list[tuple[str, Path]] = []
    try:
        rounds_dir = _rounds_dir(root, coordinator_id)
    except UsageError as exc:
        warnings.warn(str(exc), UserWarning, stacklevel=2)
        return []
    for path in rounds_dir.glob("*.json"):
        if _is_reparse(path):
            warnings.warn(f"{path}: symbolic link refused", UserWarning, stacklevel=2)
            continue
        if central_messages._canonical_uuid(path.stem) is None:
            warnings.warn(
                f"{path}: round filename is not a canonical UUID", UserWarning, stacklevel=2
            )
            continue
        try:
            meta, _bom, _newline = _read_round(path)
            started = str(meta.get("started_at") or "")
        except UsageError:
            started = ""
        items.append((started, path))
    items.sort(key=lambda item: (item[0], item[1].name))
    return [path for _started, path in items]


def _round_lock_path(path: Path) -> Path:
    return path.parent / f"{path.stem}.lock"


# --- the usage of one round -------------------------------------------------


def _payload(meta: dict) -> dict:
    """The usage of one round: role, counts, estimate flag and model."""
    usage = meta.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    role = usage.get("role") or "coordinator"
    if role not in ROLES:
        raise UsageError(
            f"unknown usage role {role!r} (expected one of {', '.join(ROLES)})"
        )
    model = usage.get("model")
    family = usage.get("quota_family")
    return {
        "role": role,
        "tokens_in": _count(usage.get("tokens_in")),
        "tokens_out": _count(usage.get("tokens_out")),
        "tokens_total": _count(usage.get("tokens_total")),
        "estimated": usage.get("estimated") is True,
        "model": model if isinstance(model, str) else None,
        "quota_family": family if isinstance(family, str) else None,
        "chars": _count(usage.get("chars")),
    }


def _log_chars(coord: Path, meta: dict) -> int | None:
    """The characters of the round's stdout log, when it is readable."""
    logs = meta.get("logs")
    ref = logs.get("stdout") if isinstance(logs, dict) else None
    if not isinstance(ref, str) or not ref:
        return None
    path = coord / ref
    base = coord.resolve(strict=False)
    if path.is_symlink() or not path.resolve(strict=False).is_relative_to(base):
        return None
    try:
        data = central_messages._read_bounded(path, central_rounds.LOG_MAX_BYTES)
    except (OSError, central_messages.MessageError):
        return None
    return len(data.decode("utf-8", errors="replace"))


def _numbers(payload: dict, chars: int | None) -> dict:
    """The counts to record: what the CLI reported, else a marked estimate."""
    tokens_in = payload["tokens_in"]
    tokens_out = payload["tokens_out"]
    total = payload["tokens_total"]
    estimated = payload["estimated"]
    if tokens_in is not None and tokens_out is not None:
        derived = tokens_in + tokens_out
        return {
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "tokens_total": derived if total is None else total,
            "estimated": estimated,
            # the job entry can only be built by records.add_usage when the
            # reported total is the sum of the two components
            "sum_ok": total is None or total == derived,
        }
    if (total is None and tokens_in is None and tokens_out is None
            and chars is not None and payload["role"] == "coordinator"):
        guess = chars // CHARS_PER_TOKEN
        total = guess or None
        estimated = True
    return {
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "tokens_total": total,
        "estimated": estimated,
        "sum_ok": False,
    }


def _note(numbers: dict, round_id: str) -> str:
    """The note of the usage entry: what the number is and where it is from."""
    if numbers["tokens_total"] is None:
        return f"coordinator round {round_id}: no usage was reported"
    if numbers["estimated"]:
        return f"coordinator round {round_id}: estimated from the round output"
    return f"coordinator round {round_id}"


def _entry(
    job: dict, numbers: dict, payload: dict, event_id: str, note: str
) -> bool:
    """Append the usage entry to the job; ``False`` when it is already there."""
    entries = job.get("usage")
    if entries is None:
        entries = []
        job["usage"] = entries
    elif not isinstance(entries, list):
        raise UsageError("the job usage is not a list")
    for entry in entries:
        if isinstance(entry, dict) and entry.get("central_event_id") == event_id:
            return False
    if numbers["sum_ok"]:
        before = len(entries)
        records.add_usage(
            job,
            payload["role"],
            numbers["tokens_in"],
            numbers["tokens_out"],
            numbers["estimated"],
            note,
        )
        entry = entries[before]
    else:
        entry = {
            "role": payload["role"],
            "tokens_in": numbers["tokens_in"],
            "tokens_out": numbers["tokens_out"],
            "tokens_total": numbers["tokens_total"],
            "estimated": numbers["estimated"],
            "note": note,
            "at": _now(),
        }
        entries.append(entry)
    entry["central_event_id"] = event_id
    entry["model"] = payload["model"]
    entry["quota_family"] = payload["quota_family"]
    return True


def _is_reparse(path: Path) -> bool:
    """True when ``path`` is a symlink or a Windows reparse point/junction."""
    if path.is_symlink():
        return True
    if os.name == "nt":
        try:
            import ctypes

            attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
            if attrs != -1 and attrs & 0x400:
                return True
        except Exception:
            pass
    return False


def _guard_job_storage(worktree: Path, job_id: str) -> None:
    """Refuse redirected job storage before any lock or write."""
    safe = _job_id(job_id)
    if safe is None:
        raise UsageError(f"the job id is not a safe job id, got {job_id!r}")
    base = worktree.resolve(strict=False)
    current = worktree
    for part in ("docs", "crew-chief", "jobs"):
        current = current / part
        if _is_reparse(current):
            raise UsageError(f"{current}: redirected job storage refused")
        try:
            resolved = current.resolve(strict=False)
        except OSError as exc:
            raise UsageError(f"{current}: cannot resolve job storage: {exc}") from exc
        if not resolved.is_relative_to(base):
            raise UsageError(f"{current}: job storage escapes the worktree")
    if current.exists() and not current.is_dir():
        raise UsageError(f"{current}: job storage is not a directory")
    job_path = current / f"{safe}.json"
    if _is_reparse(job_path):
        raise UsageError(f"{job_path}: redirected job file refused")
    try:
        if not job_path.resolve(strict=False).is_relative_to(base):
            raise UsageError(f"{job_path}: job file escapes the worktree")
    except OSError as exc:
        raise UsageError(f"{job_path}: cannot resolve job file: {exc}") from exc


def _apply_job(
    record: dict,
    job_id: str,
    payload: dict,
    numbers: dict,
    event_id: str,
    note: str,
) -> bool | None:
    """Add the usage entry to the job; ``None`` when the job is not there yet."""
    worktree = _worktree_of(record)
    _guard_job_storage(worktree, job_id)

    def change(job: dict) -> bool:
        if job.get("id") != job_id:
            raise UsageError("the job record id does not match its storage filename")
        return _entry(job, numbers, payload, event_id, note)

    try:
        return bool(records.update_job(worktree, job_id, change))
    except (records.RecordError, OSError) as exc:
        if isinstance(exc, records.RecordError) and isinstance(exc.__cause__, FileNotFoundError):
            return None
        detail = " ".join(str(exc).split())[:500]
        raise UsageError(f"cannot record usage for job {job_id!r}: {detail}") from exc


# --- the recording ----------------------------------------------------------


def _event(meta: dict) -> dict:
    """The usage event block of a round, empty when it has none."""
    event = meta.get("usage_event")
    if event is None:
        return {}
    if not isinstance(event, dict):
        raise UsageError("the usage event must be an object")
    expected = f"central:{meta.get('coordinator_id')}:{meta.get('id')}"
    if event.get("event_id") != expected:
        raise UsageError("the usage event id does not match its coordinator and round")
    if event.get("state") not in ("pending", "recorded"):
        raise UsageError("the usage event state must be pending or recorded")
    if event.get("role") != _payload(meta)["role"]:
        raise UsageError("the usage event role does not match its round")
    job_id = event.get("job_id")
    if job_id is not None and _job_id(job_id) is None:
        raise UsageError("the usage event job id is invalid")
    if event["state"] == "recorded" and job_id is None:
        raise UsageError("a recorded usage event must identify its job")
    return dict(event)


def _target_job(meta: dict, record: dict) -> str | None:
    """The job the usage belongs to: the round's own job, else the current one.

    A non-null but unusable binding is rejected instead of being silently
    skipped, so usage can never fall through to a different job.
    """
    for source in (meta, record):
        key = "active_job_id"
        value = source.get(key) if isinstance(source, dict) else None
        if value is None:
            continue
        job = _job_id(value)
        if job is None:
            raise UsageError(
                f"the active job id is not a safe job id, got {value!r}"
            )
        return job
    return None


def _bind(
    path: Path, meta: dict, bom: bool, newline: str, event: dict, job_id: str
) -> None:
    """Persist the target binding before the job is updated."""
    meta["usage_event"] = {
        "event_id": event["event_id"],
        "role": event["role"],
        "job_id": job_id,
        "state": "pending",
        "bound_at": event.get("bound_at") or _now(),
        "recorded_at": None,
    }
    _write_round(path, meta, bom, newline)


def _mark_recorded(path: Path, meta: dict, bom: bool, newline: str) -> None:
    """Mark the round's usage as counted in its job."""
    event = _event(meta)
    event["state"] = "recorded"
    event["recorded_at"] = _now()
    meta["usage_event"] = event
    _write_round(path, meta, bom, newline)


def _record_round(
    record: dict,
    coord: Path,
    path: Path,
    meta: dict,
    bom: bool,
    newline: str,
) -> None:
    """Count one round's usage in its job, exactly once."""
    event = _event(meta)
    if event.get("state") == "recorded":
        return
    if meta.get("state") == "running":
        return
    round_id = str(meta.get("id") or path.stem)
    payload = _payload(meta)
    raw_event_id = event.get("event_id")
    if raw_event_id is None:
        event_id = f"central:{coord.name}:{round_id}"
    elif not isinstance(raw_event_id, str) or not raw_event_id:
        raise UsageError(f"the usage event id is not a non-empty string, got {raw_event_id!r}")
    else:
        event_id = raw_event_id
    bound = event.get("job_id")
    if bound is not None:
        job_id = _job_id(bound)
        if job_id is None:
            raise UsageError(f"the round binds an unusable job id {bound!r}")
    else:
        job_id = _target_job(meta, record)
    if job_id is None:
        # the job is unknown: the usage stays pending in the round metadata
        if not event:
            meta["usage_event"] = {
                "event_id": f"central:{coord.name}:{round_id}",
                "role": payload["role"],
                "job_id": None,
                "state": "pending",
                "bound_at": None,
                "recorded_at": None,
            }
            _write_round(path, meta, bom, newline)
        return
    event["event_id"] = event_id
    event["role"] = payload["role"]
    _bind(path, meta, bom, newline, event, job_id)
    chars = payload["chars"]
    if chars is None and payload["role"] == "coordinator":
        chars = _log_chars(coord, meta)
    numbers = _numbers(payload, chars)
    added = _apply_job(
        record, job_id, payload, numbers, event_id, _note(numbers, round_id)
    )
    if added is None:
        return  # the job is not there yet: the usage stays pending
    _mark_recorded(path, meta, bom, newline)


def _check_binding(meta: dict, coordinator_id: str, round_id: str) -> None:
    """Refuse a round metadata that belongs to another round or coordinator."""
    if not isinstance(meta, dict):
        raise UsageError("the round metadata is not a JSON object")
    stored = meta.get("id")
    if stored != round_id:
        raise UsageError(
            f"the round metadata belongs to round {stored!r}, not {round_id!r}"
        )
    owner = meta.get("coordinator_id")
    if owner != coordinator_id:
        raise UsageError(f"the round metadata belongs to coordinator {owner!r}")
    if type(meta.get("schema_version")) is not int or meta["schema_version"] != 1:
        raise UsageError(
            f"the round metadata schema_version must be 1, got "
            f"{meta.get('schema_version')!r}"
        )


def _round_of(round_result: Any) -> str | None:
    """The canonical round id of a round result or its embedded round."""
    if not isinstance(round_result, dict):
        raise UsageError("the round result must be an object")
    value = round_result.get("round_id")
    if value is None:
        embedded = round_result.get("round")
        if isinstance(embedded, dict):
            value = embedded.get("round_id")
            if value is None:
                value = embedded.get("id")
    if value is None:
        return None
    canonical = central_messages._canonical_uuid(value)
    if canonical is None:
        raise UsageError(
            f"the round id must be a canonical UUID string, got {value!r}"
        )
    return canonical


def record_round_usage(root: Path, record: dict, round_result: dict) -> None:
    """Record the usage of one finished round in the job it belongs to.

    The round's usage is counted exactly once: the entry carries the stable
    event id of the round, and the round metadata remembers that it was
    counted.  While no job is known the usage stays pending in the round
    metadata and :func:`backfill_usage` counts it later.
    """
    if record is None and isinstance(round_result, dict):
        candidate = round_result.get("record")
        if isinstance(candidate, dict):
            record = candidate
    coordinator_id = _coordinator_of(record)
    round_id = _round_of(round_result)
    if round_id is None:
        return  # the round never ran: there is no usage to record
    path = _round_path(root, coordinator_id, round_id)
    coord = _coord_dir(root, coordinator_id)
    lock_path = _round_lock_path(path)

    def timeout_error(waited: float) -> UsageError:
        return UsageError(
            f"{lock_path}: round {round_id!r} is locked by another crew-chief "
            f"process (waited {waited:g}s)"
        )

    with records.file_lock(lock_path, records.LOCK_TIMEOUT_S, records.LOCK_STALE_S, timeout_error):
        meta, bom, newline = _read_round(path)
        _check_binding(meta, coordinator_id, round_id)
        _record_round(record, coord, path, meta, bom, newline)


def backfill_usage(root: Path, record: dict) -> None:
    """Count every pending round's usage in the job it belongs to, once.

    A round that cannot be read is reported and left alone: the history of
    the coordinator is never deleted to make a backfill succeed.
    """
    coordinator_id = _coordinator_of(record)
    coord = _coord_dir(root, coordinator_id)
    for path in _round_paths(root, coordinator_id):
        lock_path = _round_lock_path(path)
        try:
            with records.file_lock(
                lock_path, records.LOCK_TIMEOUT_S, records.LOCK_STALE_S,
                lambda waited: UsageError(
                    f"{lock_path}: round {path.stem!r} is locked by another "
                    f"crew-chief process (waited {waited:g}s)"
                ),
            ):
                meta, bom, newline = _read_round(path)
                _check_binding(meta, coordinator_id, path.stem)
                if meta.get("state") == "running":
                    continue
                _record_round(record, coord, path, meta, bom, newline)
        except UsageError as exc:
            warnings.warn(f"{path}: {exc}", UserWarning, stacklevel=2)


def _rounds_dir_readonly(root: Path, coordinator_id: str) -> Path | None:
    """The rounds directory without creating it, or ``None`` when absent.

    Mirrors the guards of :func:`_rounds_dir` but never makes directories,
    takes locks or writes: a missing directory simply has no pending usage.
    """
    coord = _coord_dir(root, coordinator_id)
    rounds = coord / "rounds"
    base = coord.resolve(strict=False)
    try:
        if _is_reparse(rounds):
            raise UsageError(f"{rounds}: redirected rounds directory refused")
        if rounds.is_symlink() or not rounds.resolve(strict=False).is_relative_to(base):
            raise UsageError(f"{rounds}: redirected rounds directory refused")
    except OSError as exc:
        raise UsageError(f"{rounds}: cannot resolve rounds directory: {exc}") from exc
    if not rounds.exists():
        return None
    if not rounds.is_dir():
        raise UsageError(f"{rounds}: rounds is not a directory")
    try:
        if rounds.resolve(strict=False).parent != base:
            raise UsageError(f"{rounds}: redirected rounds directory refused")
    except OSError as exc:
        raise UsageError(f"{rounds}: cannot resolve rounds directory: {exc}") from exc
    return rounds


def read_pending_usage(root: Path, record: dict) -> list[dict]:
    """The usage events that are not counted in a job yet, without side effects.

    The same events as :func:`pending_usage` but lock-free and read-only:
    no lock files are taken, nothing is created or written, and a rounds
    directory that is absent simply yields no events.  A round file that
    cannot be read is reported with :func:`warnings.warn` and skipped, so a
    read-only caller such as the snapshot can turn it into a diagnostic.
    """
    coordinator_id = _coordinator_of(record)
    coord = _coord_dir(root, coordinator_id)
    try:
        rounds_dir = _rounds_dir_readonly(root, coordinator_id)
    except UsageError as exc:
        warnings.warn(str(exc), UserWarning, stacklevel=2)
        return []
    if rounds_dir is None:
        return []
    try:
        base = rounds_dir.resolve(strict=False)
    except OSError as exc:
        warnings.warn(f"{rounds_dir}: cannot resolve rounds directory: {exc}", UserWarning, stacklevel=2)
        return []
    try:
        candidates = sorted(rounds_dir.glob("*.json"), key=lambda item: item.name)
    except OSError as exc:
        warnings.warn(f"{rounds_dir}: cannot list round files: {exc}", UserWarning, stacklevel=2)
        return []
    ordered: list[tuple[str, Path]] = []
    for path in candidates:
        try:
            if _is_reparse(path):
                raise UsageError(f"{path}: symbolic link refused")
            if central_messages._canonical_uuid(path.stem) is None:
                raise UsageError(f"{path}: round filename is not a canonical UUID")
            try:
                resolved = path.resolve(strict=False)
            except OSError as exc:
                raise UsageError(f"{path}: cannot resolve round file: {exc}") from exc
            if not resolved.is_relative_to(base):
                raise UsageError(f"{path}: the round path escapes the coordinator directory")
            if not path.is_file():
                raise UsageError(f"{path}: the round path is not a regular file")
            meta, _bom, _newline = _read_round(path)
            _check_binding(meta, coordinator_id, path.stem)
            started = str(meta.get("started_at") or "")
        except UsageError as exc:
            warnings.warn(f"{path}: {exc}", UserWarning, stacklevel=2)
            continue
        ordered.append((started, path))
    ordered.sort(key=lambda item: (item[0], item[1].name))
    pending: list[dict] = []
    for _started, path in ordered:
        try:
            meta, _bom, _newline = _read_round(path)
            _check_binding(meta, coordinator_id, path.stem)
        except UsageError as exc:
            warnings.warn(f"{path}: {exc}", UserWarning, stacklevel=2)
            continue
        if meta.get("state") == "running":
            continue
        try:
            event = _event(meta)
            if event.get("state") == "recorded":
                continue
            payload = _payload(meta)
        except UsageError as exc:
            warnings.warn(f"{path}: {exc}", UserWarning, stacklevel=2)
            continue
        round_id = str(meta.get("id") or path.stem)
        chars = payload["chars"]
        if chars is None and payload["role"] == "coordinator":
            chars = _log_chars(coord, meta)
        numbers = _numbers(payload, chars)
        event_id = event.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            event_id = f"central:{coordinator_id}:{round_id}"
        pending.append(
            {
                "event_id": event_id,
                "round_id": round_id,
                "coordinator_id": coordinator_id,
                "job_id": event.get("job_id"),
                "role": payload["role"],
                "tokens_in": numbers["tokens_in"],
                "tokens_out": numbers["tokens_out"],
                "tokens_total": numbers["tokens_total"],
                "estimated": numbers["estimated"],
                "model": payload["model"],
                "quota_family": payload["quota_family"],
                "started_at": meta.get("started_at"),
            }
        )
    return pending


def pending_usage(root: Path, record: dict) -> list[dict]:
    """The usage events that are not counted in a job yet.

    Each entry carries the stable event id, so the caller can tell a pending
    event from one that is already part of a job.
    """
    coordinator_id = _coordinator_of(record)
    coord = _coord_dir(root, coordinator_id)
    pending: list[dict] = []
    for path in _round_paths(root, coordinator_id):
        lock_path = _round_lock_path(path)
        try:
            with records.file_lock(
                lock_path, records.LOCK_TIMEOUT_S, records.LOCK_STALE_S,
                lambda waited: UsageError(
                    f"{lock_path}: round {path.stem!r} is locked by another "
                    f"crew-chief process (waited {waited:g}s)"
                ),
            ):
                meta, _bom, _newline = _read_round(path)
                _check_binding(meta, coordinator_id, path.stem)
        except UsageError as exc:
            warnings.warn(f"{path}: {exc}", UserWarning, stacklevel=2)
            continue
        if meta.get("state") == "running":
            continue
        try:
            event = _event(meta)
            if event.get("state") == "recorded":
                continue
            payload = _payload(meta)
        except UsageError as exc:
            warnings.warn(f"{path}: {exc}", UserWarning, stacklevel=2)
            continue
        round_id = str(meta.get("id") or path.stem)
        chars = payload["chars"]
        if chars is None and payload["role"] == "coordinator":
            chars = _log_chars(coord, meta)
        numbers = _numbers(payload, chars)
        event_id = event.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            event_id = f"central:{coordinator_id}:{round_id}"
        pending.append(
            {
                "event_id": event_id,
                "round_id": round_id,
                "coordinator_id": coordinator_id,
                "job_id": event.get("job_id"),
                "role": payload["role"],
                "tokens_in": numbers["tokens_in"],
                "tokens_out": numbers["tokens_out"],
                "tokens_total": numbers["tokens_total"],
                "estimated": numbers["estimated"],
                "model": payload["model"],
                "quota_family": payload["quota_family"],
                "started_at": meta.get("started_at"),
            }
        )
    return pending
