"""Supervise finite headless coordinator rounds (FR-009, FR-011, FR-024, SC-004).

One detached Python supervisor owns the coordinator's session: it acquires the
per-coordinator owner lease, starts a bounded round only for the first round, an
eligible task or new messages, consumes the inbox between rounds, persists
heartbeats and state in ``supervisor.json`` and stops when the work is
completed, the explicit stop is requested or a round failed.

Ownership rules (plan: "Supervision, ownership and recovery"):

* Only a locally acquired :func:`central_registry.owner_lease` owns the
  coordinator. A persisted PID or a leftover lock is diagnostics, never kill or
  takeover authority: a contended lease is reported and the supervisor refuses
  to start.
* A failed round is held for explicit recovery; failed, running and uncertain
  records are never restarted silently.
* Pause and stop are cooperative: the current bounded round always finishes and
  the durable ``control/<uuid>.json`` command is applied between rounds. An
  unchanged-state loop pauses with a diagnostic instead of spending model calls.
* Round logs are capped by :mod:`central_rounds` at 10 MiB per stream; the
  supervisor keeps the log payloads of the newest
  :data:`LOG_RETENTION_ROUNDS` rounds and prunes older ones while preserving
  every round's metadata, messages, usage and job history.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import (
    central_config,
    central_launch,
    central_messages,
    central_registry,
    central_rounds,
    central_usage,
    records,
)
from .records import write_atomic

__all__ = [
    "HEARTBEAT_S",
    "LEASE_STALE_S",
    "LOG_MAX_BYTES",
    "LOG_RETENTION_ROUNDS",
    "SupervisorError",
    "start_supervisor",
    "supervise",
    "supervisor_main",
]

#: The owner lease is stale after ten seconds without a heartbeat.
LEASE_STALE_S = 10.0
#: Heartbeats are persisted once per second, also while a round runs.
HEARTBEAT_S = 1.0
#: Per-stream round log cap, owned by the accepted round module.
LOG_MAX_BYTES = central_rounds.LOG_MAX_BYTES
#: Round payload logs kept on disk; metadata of older rounds is preserved.
LOG_RETENTION_ROUNDS = 20

#: Bounded read for one event, control or supervisor record.
READ_MAX_BYTES = 1024 * 1024
#: Bound on control commands consumed per poll.
MAX_CONTROLS = 32
#: Bound on persisted supervisor events.
MAX_EVENTS = 50
#: Defaults when the record carries no resolved central settings.
POLL_DEFAULT_S = 1.0
TIMEOUT_DEFAULT_S = 900.0
_MAX_ERROR_CHARS = 500
# Hidden Windows start: constants kept local so they are testable anywhere.
_CREATE_NO_WINDOW = 0x08000000
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_STARTF_USESHOWWINDOW = 0x00000001
_SW_HIDE = 0

#: Records a supervisor may take over; anything else waits for explicit recovery.
_STARTABLE = frozenset({"created", "paused", "idle", "waiting"})
#: States that hold the supervisor without spending model calls.
_HOLD = frozenset({"paused", "quota", "attached"})
#: Commands a durable control file may route to the live owner.
_CONTROL_COMMANDS = ("pause", "resume", "stop")

_BOOTSTRAP = (
    "import sys\n"
    "from crew_chief.central_supervisor import supervisor_main\n"
    "raise SystemExit(supervisor_main(sys.argv[1:]))\n"
)


class SupervisorError(ValueError):
    """A supervisor cannot be started, owned or continued safely."""


# --- small helpers ----------------------------------------------------------


def _diag(value: Any) -> str:
    """One bounded diagnostic line, never a raw multi-line transcript."""
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:_MAX_ERROR_CHARS]


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def _bounded_number(value: Any, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return default
    return float(value)


class _Clock:
    """Production clock: the supervisor owns no time source of its own."""

    def time(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


def _coordinator_dir(root: Path, coordinator_id: str) -> Path:
    try:
        return central_messages._coordinator_dir(root, coordinator_id)
    except central_messages.MessageError as exc:
        raise SupervisorError(str(exc)) from exc


def _subdir(coord: Path, name: str) -> Path:
    try:
        return central_rounds._subdir(coord, name)
    except central_rounds.RoundError as exc:
        raise SupervisorError(str(exc)) from exc


def _stored_record(root: Path, coordinator_id: str) -> dict:
    registry = central_registry.load_registry(root)
    record = registry["coordinators"].get(coordinator_id)
    if record is None:
        errors = [
            error.get("message", "")
            for error in registry["errors"]
            if error.get("record") == coordinator_id
        ]
        detail = "; ".join(item for item in errors if item)
        raise SupervisorError(
            f"no usable coordinator {coordinator_id} is registered"
            + (f": {detail}" if detail else "")
        )
    return record


def _settings(record: dict) -> tuple[float, float]:
    """The resolved poll interval and round timeout of one coordinator."""
    authorization = record.get("authorization")
    settings = authorization.get("settings") if isinstance(authorization, dict) else None
    settings = settings if isinstance(settings, dict) else {}
    return (
        _bounded_number(settings.get("central_poll_s"), POLL_DEFAULT_S),
        _bounded_number(settings.get("central_round_timeout_s"), TIMEOUT_DEFAULT_S),
    )


def _read_json(path: Path) -> Any:
    if path.is_symlink():
        raise SupervisorError(f"{path.name}: symbolic link refused")
    data = central_messages._read_bounded(path, READ_MAX_BYTES)
    return json.loads(data.decode("utf-8-sig"))


# --- control files ----------------------------------------------------------


def _control_command(path: Path) -> str:
    """Validate one durable control file before it can steer the owner."""
    try:
        value = _read_json(path)
    except (OSError, UnicodeDecodeError, ValueError, RecursionError,
            central_messages.MessageError) as exc:
        raise SupervisorError(f"unreadable control: {exc}") from exc
    if not isinstance(value, dict):
        raise SupervisorError("control must be an object")
    if central_messages._canonical_uuid(path.stem) is None or value.get("id") != path.stem:
        raise SupervisorError("control id does not match its filename")
    command = value.get("command")
    if command not in _CONTROL_COMMANDS:
        raise SupervisorError(f"unknown control command {command!r}")
    return command


def _control_paths(control_dir: Path) -> list[Path]:
    """The bounded control files, in the order they were requested."""
    items: list[tuple[int, str, Path]] = []
    for path in control_dir.glob("*.json"):
        try:
            mtime = path.stat().st_mtime_ns
        except OSError:
            continue
        items.append((mtime, path.name, path))
    items.sort()
    return [path for _, _, path in items[:MAX_CONTROLS]]


# --- the durable supervisor record -----------------------------------------


class _Status:
    """``supervisor.json``: ownership evidence, state, round and heartbeat."""

    def __init__(self, root: Path, coordinator_id: str, coord: Path,
                 clock: Any, nonce: str) -> None:
        self.root = root
        self.id = coordinator_id
        self.coord = coord
        self.clock = clock
        self.nonce = nonce
        self.path = coord / "supervisor.json"
        self.state: str | None = None
        self.error: str | None = None
        self.round_id: str | None = None
        self.child_pid: int | None = None
        self.started_at = _iso(clock.time())
        self.heartbeat_at = self.started_at
        self.events: list[dict] = []
        self._seen: set[str] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _payload(self) -> dict:
        return {
            "schema_version": 1,
            "coordinator_id": self.id,
            "nonce": self.nonce,
            "pid": os.getpid(),
            "child_pid": self.child_pid,
            "state": self.state,
            "round_id": self.round_id,
            "started_at": self.started_at,
            "heartbeat_at": self.heartbeat_at,
            "error": self.error,
            "events": self.events[-MAX_EVENTS:],
        }

    def _write(self) -> None:
        write_atomic(
            self.path,
            json.dumps(self._payload(), ensure_ascii=False, indent=2) + "\n",
        )

    def _event(self, state: str | None, note: str | None) -> None:
        if not note and state == self.state:
            return
        self.events.append({
            "at": _iso(self.clock.time()),
            "state": state,
            "note": _diag(note) if note else None,
        })
        self.events = self.events[-MAX_EVENTS:]

    def start(self) -> None:
        previous_pid = None
        if self.path.exists() or self.path.is_symlink():
            try:
                previous = _read_json(self.path)
            except (OSError, ValueError, SupervisorError):
                previous = None
            if isinstance(previous, dict) and isinstance(previous.get("pid"), int):
                previous_pid = previous["pid"]
        note = "supervisor acquired the coordinator lease"
        if previous_pid is not None and previous_pid != os.getpid():
            note = (
                f"previous supervisor pid {previous_pid} is diagnostics only; "
                "it is never signalled or treated as the owner"
            )
        with self._lock:
            self.state = "starting"
            self._event("starting", note)
            self.heartbeat_at = _iso(self.clock.time())
            self._write()
        central_registry.update_record(self.root, self.id, {
            "state": "starting",
            "supervisor_nonce": self.nonce,
            "last_error": None,
        })
        self._thread = threading.Thread(target=self._heartbeat, daemon=True)
        self._thread.start()

    def _heartbeat(self) -> None:
        while not self._stop.wait(HEARTBEAT_S):
            with self._lock:
                self.heartbeat_at = _iso(self.clock.time())
                try:
                    self._write()
                except OSError:
                    return

    def note(self, state: str | None, text: str) -> None:
        with self._lock:
            self._event(state, text)
            self.heartbeat_at = _iso(self.clock.time())
            self._write()

    def set_state(self, state: str, error: str | None = None,
                  note: str | None = None) -> None:
        with self._lock:
            changed = state != self.state or error != self.error
            self._event(state, note or error)
            self.state = state
            self.error = error
            self.heartbeat_at = _iso(self.clock.time())
            self._write()
        if changed:
            central_registry.update_record(self.root, self.id, {
                "state": state,
                "last_error": error,
            })

    def consume(self, control_dir: Path) -> str | None:
        """Apply each durable control file once; the newest command wins."""
        requested = None
        for path in _control_paths(control_dir):
            if path.name in self._seen:
                continue
            self._seen.add(path.name)
            try:
                command = _control_command(path)
            except SupervisorError as exc:
                self.note(self.state, f"ignored control {path.name}: {exc}")
                continue
            requested = command
            self.note(self.state, f"control {command} requested")
        return requested

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=HEARTBEAT_S * 2 + 1)


# --- the production runner --------------------------------------------------


class _Runner:
    """Run one accepted round and record its usage in the active job."""

    def __init__(self, root: Path, status: _Status) -> None:
        self.root = root
        self.status = status

    def __call__(self, record: dict, timeout_s: float) -> dict:
        adapter = central_launch.adapter_for(record.get("cli"))

        def spawn(*args, **kwargs):
            child = subprocess.Popen(*args, **kwargs)
            self.status.child_pid = child.pid
            return child

        try:
            result = central_rounds.run_round(record, self.root, adapter, spawn, timeout_s)
        finally:
            self.status.child_pid = None
        if isinstance(result, dict):
            try:
                central_usage.record_round_usage(
                    self.root, result.get("record") or record, result
                )
            except (central_usage.UsageError, central_registry.RegistryError,
                    OSError) as exc:
                # The usage stays pending in the round metadata and can be
                # backfilled; it must never turn a finished round into a crash.
                self.status.note(self.status.state, f"usage is pending: {exc}")
        return result


# --- log retention ----------------------------------------------------------


def _prune_logs(coord: Path, rounds_dir: Path, logs_dir: Path, clock: Any) -> None:
    """Delete only log payloads beyond the retention window, keep metadata."""
    entries: list[tuple[int, str, Path]] = []
    for path in rounds_dir.glob("*.json"):
        if path.is_symlink() or central_messages._canonical_uuid(path.stem) is None:
            continue
        try:
            entries.append((path.stat().st_mtime_ns, path.name, path))
        except OSError:
            continue
    entries.sort(reverse=True)
    base = logs_dir.resolve(strict=False)
    for _, _, path in entries[LOG_RETENTION_ROUNDS:]:
        try:
            meta, bom, newline = central_usage._read_round(path)
        except (OSError, central_usage.UsageError) as exc:
            warnings.warn(f"{path}: log retention skipped: {exc}", UserWarning, stacklevel=2)
            continue
        logs = meta.get("logs")
        if not isinstance(logs, dict):
            continue
        pruned = False
        for stream in ("stdout", "stderr"):
            ref = logs.get(stream)
            if not isinstance(ref, str) or not ref:
                continue
            target = (coord / ref)
            if target.is_symlink() or not target.resolve(strict=False).is_relative_to(base):
                warnings.warn(
                    f"{target}: redirected log refused", UserWarning, stacklevel=2
                )
                continue
            try:
                target.unlink(missing_ok=True)
            except OSError as exc:
                warnings.warn(f"{target}: cannot prune log: {exc}", UserWarning, stacklevel=2)
                continue
            logs[stream] = None
            pruned = True
        if not pruned:
            continue
        meta["logs_pruned"] = True
        meta["logs_pruned_at"] = _iso(clock.time())
        try:
            central_usage._write_round(path, meta, bom, newline)
        except central_usage.UsageError as exc:
            warnings.warn(f"{path}: log retention skipped: {exc}", UserWarning, stacklevel=2)


# --- eligibility ------------------------------------------------------------


def _eligibility(work_state: str | None, pending: list) -> str | None:
    """Why a round should start now, or ``None`` for a zero-call idle poll."""
    if pending:
        return f"{len(pending)} new message(s)"
    if work_state is None:
        return "first round"
    if work_state == "running":
        return "eligible task"
    return None


def _names(value: Any) -> tuple:
    return tuple(item for item in value) if isinstance(value, list) else ()


def _texts(value: Any) -> tuple:
    """The unique text entries of a list, sorted; anything else is empty."""
    if not isinstance(value, list):
        return ()
    return tuple(sorted({item for item in value if isinstance(item, str) and item}))


#: A job id that can be read from the worktree without escaping it.
_JOB_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


def _job_progress(worktree: Any, job_ids: tuple[str, ...]) -> tuple:
    """The real progress of the coordinator's jobs: accepted, pending, open attempts.

    The same job ids are not progress: an accepted piece, a piece still pending
    or an attempt still open are. A missing or unreadable job record is no
    progress, never a crash.
    """
    accepted = pending = open_attempts = 0
    if not isinstance(worktree, str) or not worktree:
        return (accepted, pending, open_attempts)
    for job_id in job_ids:
        if not _JOB_ID.fullmatch(job_id):
            continue
        try:
            job = records.load_job(Path(worktree), job_id)
        except (records.RecordError, OSError):
            continue
        for piece in job.get("pieces") or []:
            if not isinstance(piece, dict):
                continue
            if piece.get("accepted"):
                accepted += 1
                continue
            pending += 1
            for attempt in piece.get("attempts") or []:
                if isinstance(attempt, dict):
                    open_attempts += 1
    return (accepted, pending, open_attempts)


def _signature(work_state: str, meta: dict, record: dict) -> tuple:
    """What one round reported as work; identical values mean no progress."""
    checkpoint = meta.get("checkpoint")
    checkpoint = checkpoint if isinstance(checkpoint, dict) else {}
    result = meta.get("result")
    result = result if isinstance(result, dict) else {}
    job_ids = tuple(sorted(
        set(_texts(checkpoint.get("job_ids"))) | set(_texts(record.get("job_ids")))
    ))
    return (
        work_state,
        checkpoint.get("active_job_id"),
        _names(checkpoint.get("job_ids")),
        _names(checkpoint.get("blocked_task_refs")),
        _names(meta.get("consumed_message_ids")),
        len(result.get("questions") or ()),
        len(result.get("answers") or ()),
        _job_progress(record.get("worktree"), job_ids),
    )


# --- bootstrap --------------------------------------------------------------


def _bootstrap_env(root: Path) -> dict:
    env = dict(os.environ)
    env[central_config.CENTRAL_DIR_ENV] = str(root)
    package_root = str(Path(__file__).resolve().parent.parent)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = package_root + (os.pathsep + existing if existing else "")
    return env


def _hidden_start_kwargs() -> dict:
    """A detached start that opens no console window on Windows (FR-020)."""
    if os.name != "nt":
        return {"start_new_session": True}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= _STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = _SW_HIDE
    return {
        "creationflags": _CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP,
        "startupinfo": startupinfo,
    }


def start_supervisor(record: dict, root: Path) -> dict:
    """Start one detached, hidden supervisor for a startable coordinator.

    Returns ``{ok, error, coordinator_id, pid, argv}``. A record that is
    failed, running or otherwise uncertain is refused: restarting it needs
    explicit recovery, never a silent retry.
    """
    root = Path(root)
    outcome: dict = {
        "ok": False,
        "error": None,
        "coordinator_id": None,
        "pid": None,
        "argv": [],
    }
    try:
        coordinator_id = central_messages._canonical_uuid(
            record.get("id") if isinstance(record, dict) else None
        )
        if coordinator_id is None:
            raise SupervisorError("record id must be a canonical UUID string")
        outcome["coordinator_id"] = coordinator_id
        saved = _stored_record(root, coordinator_id)
        state = saved.get("state")
        if state not in _STARTABLE:
            raise SupervisorError(
                f"coordinator {coordinator_id} is {state!r}; a supervisor only "
                f"takes over {', '.join(sorted(_STARTABLE))} coordinators and "
                "never silently restarts failed or uncertain work: explicit "
                "recovery required"
            )
        worktree = saved.get("worktree")
        if not isinstance(worktree, str) or not Path(worktree).is_dir():
            raise SupervisorError(f"the coordinator worktree is missing: {worktree!r}")
        argv = [
            sys.executable,
            "-I",
            "-c",
            "import sys; sys.path.insert(0, " + repr(str(Path(__file__).resolve().parent.parent)) + ");\n" + _BOOTSTRAP,
            "--central-dir",
            str(root),
            "--coordinator",
            coordinator_id,
        ]
        child = subprocess.Popen(
            argv,
            cwd=worktree,
            env=_bootstrap_env(root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            **_hidden_start_kwargs(),
        )
    except (
        SupervisorError,
        central_registry.RegistryError,
        central_messages.MessageError,
        OSError,
    ) as exc:
        outcome["error"] = _diag(str(exc)) or type(exc).__name__
        return outcome
    outcome.update(ok=True, pid=getattr(child, "pid", None), argv=argv)
    return outcome


def _parse_argv(argv: list[str]) -> tuple[Path, str]:
    if (
        not isinstance(argv, list)
        or len(argv) != 4
        or argv[0] != "--central-dir"
        or argv[2] != "--coordinator"
    ):
        raise SupervisorError("expected --central-dir DIR --coordinator UUID")
    coordinator_id = central_messages._canonical_uuid(argv[3])
    if coordinator_id is None:
        raise SupervisorError("--coordinator must be a canonical UUID")
    return Path(argv[1]), coordinator_id


def supervisor_main(argv: list[str]) -> int:
    """Entry point of the detached supervisor process; never starts a model."""
    try:
        root, coordinator_id = _parse_argv(argv)
    except SupervisorError as exc:
        print(f"crew-chief supervisor: {_diag(str(exc))}", file=sys.stderr)
        return 2
    try:
        supervise(root, coordinator_id, _Clock(), None)
    except (
        SupervisorError,
        central_registry.RegistryError,
        central_messages.MessageError,
        central_rounds.RoundError,
        OSError,
    ) as exc:
        print(f"crew-chief supervisor: {_diag(str(exc))}", file=sys.stderr)
        return 1
    return 0


# --- the supervised loop ----------------------------------------------------


def supervise(root: Path, coordinator_id: str, clock: Any, runner: Any) -> None:
    """Own one coordinator until completed, explicitly stopped or held.

    ``clock`` provides ``time()`` epoch seconds and ``sleep(seconds)``;
    ``runner(record, timeout_s)`` returns an accepted
    :func:`central_rounds.run_round` result. A contended lease, a redirected
    directory or an uncertain record raises :class:`SupervisorError` before any
    model call.
    """
    root = Path(root)
    coordinator_id = central_messages._canonical_uuid(coordinator_id)
    if coordinator_id is None:
        raise SupervisorError("coordinator id must be a canonical UUID string")
    coord = _coordinator_dir(root, coordinator_id)
    rounds_dir = _subdir(coord, "rounds")
    logs_dir = _subdir(coord, "logs")
    control_dir = _subdir(coord, "control")
    record = _stored_record(root, coordinator_id)
    state = record.get("state")
    if state not in _STARTABLE:
        raise SupervisorError(
            f"coordinator {coordinator_id} is {state!r}; explicit recovery required"
        )
    clock = clock if clock is not None else _Clock()
    poll_s, timeout_s = _settings(record)
    nonce = uuid.uuid4().hex
    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(
                central_registry.owner_lease(root, coordinator_id, nonce, timeout_s=0)
            )
        except central_registry.RegistryError as exc:
            raise SupervisorError(str(exc)) from exc
        # State inspected before acquisition can change while another owner exits.
        record = _stored_record(root, coordinator_id)
        state = record.get("state")
        if state not in _STARTABLE:
            raise SupervisorError(f"coordinator is {state!r}; explicit recovery required")
        poll_s, timeout_s = _settings(record)
        status = _Status(root, coordinator_id, coord, clock, nonce)
        if runner is None:
            runner = _Runner(root, status)
        status.start()
        _backfill(root, record, status, rounds_dir)
        checkpoint, diagnostics = central_rounds._checkpoint(rounds_dir)
        for diagnostic in diagnostics:
            status.note("starting", f"round checkpoint: {diagnostic}")
        work_state = checkpoint.get("state") if isinstance(checkpoint, dict) else None
        if not isinstance(work_state, str):
            work_state = state if state in ("idle", "waiting") else None
        if state == "paused":
            status.set_state("paused", note="paused coordinator resumed supervision")
        last_signature = None
        try:
            while True:
                requested = status.consume(control_dir)
                if requested == "stop":
                    status.set_state("stopped", note="stop requested by the owner")
                    return
                if requested == "resume" and status.state == "paused":
                    status.set_state(work_state or "created", note="resume requested")
                elif requested == "pause" and status.state not in _HOLD:
                    status.set_state(
                        "paused",
                        note="pause requested; the current bounded round finished",
                    )
                if status.state in _HOLD:
                    clock.sleep(poll_s)
                    continue
                pending = central_messages.pending(root, coordinator_id, "in")
                reason = _eligibility(work_state, pending)
                if reason is None:
                    status.set_state(work_state or "idle")
                    clock.sleep(poll_s)
                    continue

                status.set_state("running", note=reason)
                result = runner(record, timeout_s)
                if not isinstance(result, dict):
                    status.set_state("failed", "the runner returned no round result")
                    return
                if isinstance(result.get("record"), dict):
                    record = result["record"]
                meta = result.get("round")
                meta = meta if isinstance(meta, dict) else {}
                round_id = result.get("round_id")
                if isinstance(round_id, str):
                    status.round_id = round_id
                status.note("running", f"round {round_id} finished")
                _prune_logs(coord, rounds_dir, logs_dir, clock)

                if result.get("ok") is not True:
                    error = _diag(result.get("error")) or "the round failed"
                    status.set_state("failed", error)
                    return
                reported = record.get("state")
                work_state = reported if isinstance(reported, str) else "idle"
                signature = _signature(work_state, meta, record)
                if work_state == "running" and signature == last_signature:
                    status.set_state(
                        "paused",
                        note=(
                            "no progress: consecutive rounds reported the same "
                            "state, jobs and messages; paused for review"
                        ),
                    )
                    last_signature = signature
                    continue
                last_signature = signature
                if work_state == "completed":
                    status.set_state("completed", note="all work completed")
                    return
                if work_state == "failed":
                    status.set_state(
                        "failed",
                        record.get("last_error") or "the coordinator reported a failure",
                    )
                    return
                status.set_state(work_state)
        finally:
            status.close()


def _backfill(root: Path, record: dict, status: _Status, rounds_dir: Path) -> None:
    """Reconcile usage a previous supervisor could not record before exiting.

    The backfill rewrites every round metadata; the file time is restored so a
    metadata rewrite never reorders the rounds and the log retention window.
    """
    stamps: dict[Path, tuple[int, int]] = {}
    for path in rounds_dir.glob("*.json"):
        try:
            stat = path.stat()
        except OSError:
            continue
        stamps[path] = (stat.st_atime_ns, stat.st_mtime_ns)
    try:
        central_usage.backfill_usage(root, record)
    except (central_usage.UsageError, central_messages.MessageError,
            central_registry.RegistryError, OSError) as exc:
        status.note("starting", f"usage backfill deferred: {exc}")
    for path, (atime, mtime) in stamps.items():
        try:
            os.utime(path, ns=(atime, mtime))
        except OSError:
            continue
