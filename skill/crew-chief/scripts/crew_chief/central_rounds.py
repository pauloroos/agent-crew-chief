"""One bounded, resumable headless coordinator round (FR-009, FR-024, FR-025).

A round composes the accepted pieces: the persisted ``request.md``, the
checkpoint of the latest round file, :mod:`central_messages` for the durable
inbox/acknowledgments, :func:`coordinator_protocol.render_round` for the prompt
and result frame, and the injected adapter for argv plus event normalization.
It never probes or executes a model CLI itself.

``spawn(argv, *, cwd, env, stdin, stdout, stderr)`` is injected and must return
an owned ``subprocess.Popen``-like child immediately: a writable binary
``stdin``, readable binary ``stdout``/``stderr``, ``wait(timeout)`` raising
``subprocess.TimeoutExpired``, ``poll()`` and ``pid``.  The round writes the
prompt into ``stdin`` itself under one deadline, so a child that never reads
cannot block the round.  On POSIX the child must lead its own session so a
timeout kills only its tree.

Round file ``rounds/<round-uuid>.json``, schema version 1 (stable for usage
backfill):

``id``, ``coordinator_id``, ``state`` (``running``/``completed``/``failed``),
``started_at``, ``finished_at``, ``timeout_s``, ``argv``, ``cwd``,
``session_requested``, ``session_id``, ``returncode``, ``timed_out``,
``truncated{stdout,stderr}``, ``logs{stdout,stderr}`` (relative refs),
``usage{tokens_in,tokens_out,tokens_total,estimated,model,quota_family}``,
``active_job_id``, ``job_ids``, ``result`` (validated ACC_ROUND or ``null``),
``checkpoint{state,active_job_id,job_ids,blocked_task_refs,round_id,updated_at}``,
``checkpoint_diagnostics`` (newer round files that could not be read),
``consumed_message_ids``, ``acknowledged_message_ids``, ``error``.
"""

from __future__ import annotations

import heapq
import json
import math
import os
import signal
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import central_config, central_messages, central_registry, coordinator_protocol
from .records import write_atomic

__all__ = ["LOG_MAX_BYTES", "ROUND_READ_MAX_BYTES", "RoundError", "run_round"]

#: Per-stream log cap; excess is drained and discarded, never buffered.
LOG_MAX_BYTES = 10 * 1024 * 1024
#: Bounded read for one request or round metadata file.
ROUND_READ_MAX_BYTES = 1024 * 1024
#: Round files read for the previous checkpoint.
MAX_ROUND_FILES = 256
#: Bounded wait for the drain threads and the prompt writer.
_JOIN_GRACE_S = 5.0
#: Bounded wait for the owned child after it was terminated.
_CLEANUP_GRACE_S = 5.0
#: Bounded number of unreadable round files reported per round.
_MAX_DIAGNOSTICS = 8
_CHUNK = 64 * 1024
_MAX_ERROR_CHARS = 500


class RoundError(ValueError):
    """A round cannot be prepared or persisted; nothing is acked."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _diag(value: Any) -> str:
    """One bounded diagnostic line, never a raw multi-line transcript."""
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:_MAX_ERROR_CHARS]


def _last_line(text: Any) -> str:
    if not isinstance(text, str):
        return ""
    for line in reversed(text.splitlines()):
        if line.strip():
            return _diag(line)
    return ""


def _uuid(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return None
    return str(parsed) if str(parsed) == value else None


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _timeout(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RoundError("timeout_s must be a number")
    timeout = float(value)
    if not math.isfinite(timeout) or timeout <= 0:
        raise RoundError("timeout_s must be finite and positive")
    return timeout


def _worktree(record: dict) -> Path:
    value = record.get("worktree")
    if not isinstance(value, str) or not value:
        raise RoundError("record worktree is required")
    cwd = Path(value)
    if not cwd.is_dir():
        raise RoundError(f"worktree is missing: {cwd}")
    return cwd


def _requested_session(record: dict) -> str | None:
    value = record.get("session_id")
    if value is None:
        return None
    if not isinstance(value, str):
        raise RoundError("session_id must be null or text")
    return value.strip() or None


def _job_binding(record: dict) -> tuple[str | None, list[str]]:
    """The active job and job list the round starts from."""
    active = record.get("active_job_id")
    jobs = record.get("job_ids")
    return (
        active if isinstance(active, str) and active else None,
        [job for job in jobs if isinstance(job, str) and job]
        if isinstance(jobs, list)
        else [],
    )


def _round_record(record: dict) -> dict:
    """The record with the canonical coordinator settings resolved."""
    value = dict(record)
    authorization = value.get("authorization")
    authorization = dict(authorization) if isinstance(authorization, dict) else {}
    settings = authorization.get("settings")
    settings = dict(settings) if isinstance(settings, dict) else {}
    settings.setdefault("coordinator_unrestricted", True)
    authorization["settings"] = settings
    value["authorization"] = authorization
    return value


def _child_env(root: Path, record: dict) -> dict:
    """The caller environment plus the injected root and coordinator identity."""
    env = dict(os.environ)
    env[central_config.CENTRAL_DIR_ENV] = str(root)
    cli = record.get("cli")
    if isinstance(cli, str) and cli:
        env[central_config.COORDINATOR_ENV] = cli
    return env


def _subdir(coord: Path, name: str) -> Path:
    """Refuse a redirected rounds/logs directory before any write."""
    path = coord / name
    base = coord.resolve(strict=False)
    if path.is_symlink() or not path.resolve(strict=False).is_relative_to(base):
        raise RoundError(f"{path}: redirected {name} directory refused")
    if path.exists() and not path.is_dir():
        raise RoundError(f"{path}: {name} is not a directory")
    path.mkdir(exist_ok=True)
    if path.resolve(strict=False).parent != base:
        raise RoundError(f"{path}: redirected {name} directory refused")
    return path


def _read_text(path: Path, max_bytes: int, label: str) -> str:
    if path.is_symlink():
        raise RoundError(f"{path}: symbolic link refused")
    try:
        data = central_messages._read_bounded(path, max_bytes)
    except (OSError, central_messages.MessageError) as exc:
        raise RoundError(f"cannot read the {label}: {exc}") from exc
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise RoundError(f"the {label} is not valid UTF-8 text: {exc}") from exc


def _checkpoint(rounds_dir: Path) -> tuple[dict, list[str]]:
    """The newest readable round checkpoint; other rounds stay on disk.

    The newest files are selected by modification time before any read, so a
    long history cannot hide the real predecessor.  Candidates that cannot be
    read are reported instead of being dropped silently.
    """
    newest: list[tuple[int, str]] = []
    for path in rounds_dir.glob("*.json"):
        if path.is_symlink():
            continue
        try:
            mtime = path.stat().st_mtime_ns
        except OSError:
            continue
        item = (mtime, str(path))
        if len(newest) < MAX_ROUND_FILES:
            heapq.heappush(newest, item)
        elif item > newest[0]:
            heapq.heapreplace(newest, item)
    diagnostics: list[str] = []
    for _, name in sorted(newest, reverse=True):
        path = Path(name)
        try:
            data = json.loads(_read_text(path, ROUND_READ_MAX_BYTES, "round metadata"))
        except (RoundError, OSError, ValueError, RecursionError) as exc:
            diagnostics.append(f"{path.name}: {_diag(str(exc))}")
            continue
        checkpoint = data.get("checkpoint") if isinstance(data, dict) else None
        if isinstance(checkpoint, dict):
            return dict(checkpoint), _bounded_diagnostics(diagnostics)
    return {}, _bounded_diagnostics(diagnostics)


def _bounded_diagnostics(diagnostics: list[str]) -> list[str]:
    if len(diagnostics) <= _MAX_DIAGNOSTICS:
        return diagnostics
    extra = len(diagnostics) - _MAX_DIAGNOSTICS
    return diagnostics[:_MAX_DIAGNOSTICS] + [
        f"{extra} more round files could not be read"
    ]


class _Drain(threading.Thread):
    """Read one binary pipe to EOF, keeping at most :data:`LOG_MAX_BYTES`."""

    def __init__(self, handle: Any) -> None:
        super().__init__(daemon=True)
        self.handle = handle
        self._chunks: list[bytes] = []
        self._size = 0
        self.truncated = False

    def run(self) -> None:
        read = getattr(self.handle, "read1", None) or self.handle.read
        while True:
            try:
                chunk = read(_CHUNK)
            except (OSError, ValueError):
                return
            if not chunk:
                return
            if self._size >= LOG_MAX_BYTES:
                self.truncated = True
                continue
            room = LOG_MAX_BYTES - self._size
            self._chunks.append(chunk[:room])
            self._size += min(len(chunk), room)
            if len(chunk) > room:
                self.truncated = True

    def data(self) -> bytes:
        return b"".join(self._chunks)

    def text(self) -> str:
        return self.data().decode("utf-8", errors="replace")


class _Writer(threading.Thread):
    """Write the prompt into the child's stdin and close it."""

    def __init__(self, handle: Any, prompt: str) -> None:
        super().__init__(daemon=True)
        self.handle = handle
        self._prompt = prompt.encode("utf-8")
        self.error: str | None = None

    def run(self) -> None:
        try:
            self.handle.write(self._prompt)
            self.handle.flush()
        except (OSError, ValueError) as exc:
            self.error = _diag(str(exc))
        finally:
            _close_quietly(self.handle)


def _close_quietly(handle: Any) -> None:
    try:
        handle.close()
    except (OSError, ValueError):
        pass


def _release(handle: Any, busy: bool) -> None:
    """Close an owned pipe; a busy pipe is closed off the round's own path."""
    if handle is None:
        return
    if busy:
        threading.Thread(target=_close_quietly, args=(handle,), daemon=True).start()
    else:
        _close_quietly(handle)


def _terminate(child: Any) -> None:
    """Kill the owned live child tree; a persisted PID is never authority."""
    if isinstance(child, subprocess.Popen):
        pid = child.pid
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                capture_output=True,
            )
            return
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            child.kill()
        return
    for name in ("terminate", "kill"):
        method = getattr(child, name, None)
        if callable(method):
            try:
                method()
            except (OSError, ValueError):
                pass
            return


def _wait_bounded(child: Any, timeout: float = _CLEANUP_GRACE_S) -> None:
    """Wait for the owned child without waiting forever."""
    try:
        child.wait(timeout=timeout)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        pass


def _usage(events: dict) -> dict:
    """The normalized usage of one round; unknown counts stay unknown."""
    return {
        "tokens_in": _count(events.get("tokens_in")),
        "tokens_out": _count(events.get("tokens_out")),
        "tokens_total": _count(events.get("tokens_total")),
        "estimated": events.get("estimated") is True,
        "model": events["model"] if isinstance(events.get("model"), str) else None,
        "quota_family": (
            events["quota_family"]
            if isinstance(events.get("quota_family"), str) else None
        ),
    }


def _deliver(root: Path, coordinator_id: str, result: dict) -> None:
    """Store the round's questions and correlated answers in the outbox."""
    for question in result["questions"]:
        central_messages.send(root, coordinator_id, {
            "id": question["id"],
            "coordinator_id": coordinator_id,
            "direction": "out",
            "kind": "question",
            "reply_to": None,
            "task_refs": list(question["task_refs"]),
            "body": question["reason"],
        })
    for answer in result["answers"]:
        central_messages.send(root, coordinator_id, {
            "id": answer["id"],
            "coordinator_id": coordinator_id,
            "direction": "out",
            "kind": "answer",
            "reply_to": answer["reply_to"],
            "task_refs": [],
            "body": answer["body"],
        })


def _result(ok: bool, error: str | None, round_id: str | None,
            record: Any, round_meta: dict | None) -> dict:
    return {
        "ok": ok,
        "error": error,
        "round_id": round_id,
        "record": record,
        "round": round_meta,
    }


def run_round(record: dict, root: Path, adapter: object, spawn: object,
              timeout_s: float) -> dict:
    """Run one finite headless round and return ``{ok,error,round_id,record,round}``.

    The result file is durable before any acknowledgment; a failed, timed-out,
    mismatched or malformed round keeps its metadata/logs and acks nothing.
    """
    round_id = str(uuid.uuid4())
    started_at = _now()
    active_job_id, job_ids = _job_binding(record if isinstance(record, dict) else {})
    meta: dict[str, Any] = {
        "schema_version": 1,
        "id": round_id,
        "coordinator_id": None,
        "state": "running",
        "started_at": started_at,
        "finished_at": None,
        "timeout_s": None,
        "argv": [],
        "cwd": None,
        "session_requested": None,
        "session_id": None,
        "returncode": None,
        "timed_out": False,
        "truncated": {"stdout": False, "stderr": False},
        "logs": {"stdout": None, "stderr": None},
        "usage": None,
        "active_job_id": active_job_id,
        "job_ids": job_ids,
        "result": None,
        "checkpoint": {},
        "checkpoint_diagnostics": [],
        "consumed_message_ids": [],
        "acknowledged_message_ids": [],
        "error": None,
    }
    if not isinstance(record, dict):
        return _result(False, "record must be an object", None, record, None)

    root_path = Path(root)
    try:
        coordinator_id = _uuid(record.get("id"))
        if coordinator_id is None:
            raise RoundError("record id must be a canonical UUID")
        timeout = _timeout(timeout_s)
        cwd = _worktree(record)
        requested = _requested_session(record)
        for name in ("headless", "parse_events"):
            if not callable(getattr(adapter, name, None)):
                raise RoundError(f"adapter.{name} must be callable")
        if not callable(spawn):
            raise RoundError("spawn must be callable")
        coord = central_messages._coordinator_dir(root_path, coordinator_id)
        if not coord.is_dir():
            raise RoundError(
                f"coordinator {coordinator_id} is not prepared under {root_path}"
            )
        rounds_dir = _subdir(coord, "rounds")
        logs_dir = _subdir(coord, "logs")
    except (RoundError, central_messages.MessageError, OSError) as exc:
        return _result(False, _diag(str(exc)), None, record, None)

    meta["coordinator_id"] = coordinator_id
    meta["timeout_s"] = timeout
    meta["cwd"] = str(cwd)
    meta["session_requested"] = requested
    meta["session_id"] = requested
    checkpoint, diagnostics = _checkpoint(rounds_dir)
    meta["checkpoint"] = checkpoint
    meta["checkpoint_diagnostics"] = diagnostics

    def persist(state: str, error: str | None) -> None:
        meta["state"] = state
        meta["error"] = error
        meta["finished_at"] = _now()
        write_atomic(
            rounds_dir / f"{round_id}.json",
            json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
        )

    def set_record(changes: dict) -> tuple[dict, str | None]:
        try:
            return central_registry.update_record(
                root_path, coordinator_id, changes
            ), None
        except central_registry.RegistryError as exc:
            return record, _diag(f"cannot update the coordinator record: {exc}")

    def failed(message: str) -> dict:
        try:
            persist("failed", message)
        except OSError as exc:
            message = _diag(f"{message}; cannot persist the round: {exc}")
        current, record_error = set_record(
            {"state": "failed", "last_error": message}
        )
        if record_error:
            message = _diag(f"{message}; {record_error}")
        return _result(False, message, round_id, current, meta)

    round_record = _round_record(record)
    try:
        request = _read_text(coord / "request.md", ROUND_READ_MAX_BYTES, "request")
        inbox = central_messages.pending(root_path, coordinator_id, "in")
        prompt = coordinator_protocol.render_round(
            round_record, request, inbox, meta["checkpoint"]
        )
    except (RoundError, central_messages.MessageError,
            coordinator_protocol.ProtocolError) as exc:
        return failed(_diag(str(exc)))

    try:
        argv = adapter.headless(round_record, prompt)
    except Exception as exc:
        return failed(_diag(f"cannot build the round command: {exc}"))
    if (not isinstance(argv, list) or not argv
            or any(not isinstance(part, str) or not part for part in argv)):
        return failed("adapter.headless must return a non-empty argv list of strings")
    meta["argv"] = list(argv)
    try:
        write_atomic(
            rounds_dir / f"{round_id}.json",
            json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
        )
    except OSError as exc:
        return _result(
            False, _diag(f"cannot persist the round: {exc}"), round_id, record, meta
        )

    try:
        child = spawn(
            argv,
            cwd=cwd,
            env=_child_env(root_path, record),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except Exception as exc:
        return failed(_diag(f"cannot start the coordinator CLI: {exc}"))
    if any(getattr(child, name, None) is None
           for name in ("stdin", "stdout", "stderr")):
        try:
            _terminate(child)
        except (AttributeError, OSError):
            pass
        return failed("spawn must return a child with piped stdin, stdout and stderr")

    drains = {"stdout": _Drain(child.stdout), "stderr": _Drain(child.stderr)}
    for drain in drains.values():
        drain.start()
    writer = _Writer(child.stdin, prompt)
    writer.start()

    # One deadline covers the prompt write, the child and the log drains.
    deadline = time.monotonic() + timeout
    timed_out = False
    try:
        try:
            child.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            timed_out = True
            _terminate(child)
            _wait_bounded(child)
        except BaseException:
            _terminate(child)
            _wait_bounded(child)
            raise
    finally:
        for drain in drains.values():
            drain.join(_JOIN_GRACE_S)
        writer.join(_JOIN_GRACE_S)
        _release(child.stdout, drains["stdout"].is_alive())
        _release(child.stderr, drains["stderr"].is_alive())
        _release(child.stdin, writer.is_alive())

    meta["timed_out"] = timed_out
    meta["returncode"] = getattr(child, "returncode", None)
    for name, drain in drains.items():
        path = logs_dir / f"{round_id}.{name}.log"
        try:
            with path.open("xb") as handle:
                handle.write(drain.data())
        except OSError as exc:
            return failed(_diag(f"cannot persist the {name} log: {exc}"))
        meta["logs"][name] = f"logs/{path.name}"
        meta["truncated"][name] = drain.truncated

    stdout_text = drains["stdout"].text()
    stderr_text = drains["stderr"].text()

    # The usage of a failed round is still evidence and is kept.
    events: dict = {}
    parse_error: str | None = None
    try:
        events = adapter.parse_events(stdout_text, stderr_text)
    except Exception as exc:
        parse_error = _diag(f"cannot parse the coordinator output: {exc}")
    else:
        if not isinstance(events, dict):
            events = {}
            parse_error = "adapter.parse_events must return an object"
    meta["usage"] = _usage(events)

    error: str | None = None
    if timed_out:
        error = (
            f"round timed out after {timeout:g}s; the owned child tree was "
            "terminated"
        )
    elif meta["returncode"] is None:
        error = "the coordinator CLI did not report an exit status"
    elif meta["returncode"] != 0:
        detail = _last_line(stderr_text)
        error = f"coordinator CLI exited {meta['returncode']}"
        if detail:
            error = f"{error}: {detail}"
    elif parse_error is not None:
        error = parse_error

    reported = events.get("session_id")
    if reported is not None and (not isinstance(reported, str) or not reported.strip()):
        error = error or "coordinator CLI reported an unusable session id"
        reported = None
    elif isinstance(reported, str):
        reported = reported.strip()
    meta["session_id"] = reported if reported is not None else requested

    if error is None and events.get("error"):
        error = _diag(events["error"]) or "coordinator CLI reported an error"
    if error is None:
        if requested is not None and reported != requested:
            error = f"session mismatch: requested {requested!r}, got {reported!r}"
        elif reported is None:
            error = "coordinator CLI reported no session id; the round cannot resume"

    result = None
    if error is None:
        try:
            result = coordinator_protocol.parse_round(events.get("text"))
        except Exception as exc:
            error = _diag(f"malformed round result: {exc}")

    consumed = list(result["consumed_message_ids"]) if result is not None else []
    pending_ids = {item["id"] for item in inbox}
    if result is not None:
        unknown = [item for item in consumed if item not in pending_ids]
        if unknown:
            error = _diag(
                "round consumed unknown message IDs: " + ", ".join(unknown)
            )
        meta["consumed_message_ids"] = consumed
        if error is None:
            # Only a validated result rebinds the active job.
            meta["result"] = result
            meta["active_job_id"] = result["active_job_id"]
            meta["job_ids"] = list(result["job_ids"])
            meta["checkpoint"] = {
                "state": result["state"],
                "active_job_id": result["active_job_id"],
                "job_ids": list(result["job_ids"]),
                "blocked_task_refs": list(result["blocked_task_refs"]),
                "round_id": round_id,
                "updated_at": _now(),
            }

    if error is not None:
        return failed(error)

    # The validated result is durable before anything is acknowledged.
    try:
        persist("completed", None)
    except OSError as exc:
        return _result(
            False, _diag(f"cannot persist the round: {exc}"), round_id, record, meta
        )

    try:
        _deliver(root_path, coordinator_id, result)
    except (central_messages.MessageError, OSError) as exc:
        return failed(_diag(f"cannot store the round messages: {exc}"))

    current, record_error = set_record({
        "session_id": reported,
        "state": result["state"],
        "active_job_id": result["active_job_id"],
        "job_ids": list(result["job_ids"]),
        "last_error": None,
    })
    if record_error:
        return failed(record_error)

    acknowledged: list[str] = []
    for message_id in consumed:
        try:
            central_messages.acknowledge(
                root_path, coordinator_id, message_id, round_id
            )
        except (central_messages.MessageError, OSError) as exc:
            meta["acknowledged_message_ids"] = acknowledged
            return failed(_diag(f"cannot acknowledge {message_id}: {exc}"))
        acknowledged.append(message_id)
    meta["acknowledged_message_ids"] = acknowledged

    try:
        persist("completed", None)
    except OSError as exc:
        return _result(
            False, _diag(f"cannot persist the round: {exc}"), round_id, current, meta
        )
    return _result(True, None, round_id, current, meta)
