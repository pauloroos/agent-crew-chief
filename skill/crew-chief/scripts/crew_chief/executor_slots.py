"""Machine-wide executor concurrency cap via file-backed leases (FR-025).

Enforces max_running_executors across all coordinators using directory-based
lease files. Surplus executors queue in waiting state until a slot becomes
available. Stale locks from dead processes are automatically reclaimed.
"""

from __future__ import annotations

import json
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

_INCOMPLETE_GRACE_PERIOD_S = 10.0
_CREATION_GRACE_PERIOD_S = 10.0


class LeaseError(Exception):
    """Raised when a lease cannot be acquired within the timeout."""


class ExecutorSlots:
    """File-backed lease lock enforcing global executor concurrency cap."""

    def __init__(self, lease_dir: str | Path | None = None, max_running: int = 3) -> None:
        self.max_running = max(1, int(max_running))
        if lease_dir:
            self._lease_dir = Path(lease_dir)
        else:
            env_dir = (
                os.environ.get("AGENT_CREW_CHIEF_EXECUTOR_LEASE_DIR")
                or os.environ.get("ACC_EXECUTOR_LEASE_DIR")
            )
            self._lease_dir = Path(env_dir) if env_dir else Path.home() / ".acc" / "executor_leases"
        self._lease_dir.mkdir(parents=True, exist_ok=True)
        self._held_slot: int | None = None
        self._held_lock_path: Path | None = None

    def acquire(
        self,
        owner: str,
        timeout_s: float = 300.0,
        poll_interval_s: float = 0.05,
        on_waiting: Callable[[], None] | None = None,
        *,
        durable: bool = False,
        project: str | Path | None = None,
        job_id: str | None = None,
        piece: int | None = None,
        attempt: int | None = None,
        worktree: str | Path | None = None,
        timeout_s_meta: float | None = None,
    ) -> bool:
        """Acquire a slot lease, blocking up to timeout_s.

        Calls on_waiting() once if the slot cannot be acquired immediately.
        Returns True on success. Raises LeaseError on timeout.
        """
        deadline = time.monotonic() + timeout_s
        notified_waiting = False

        while time.monotonic() < deadline:
            # Reap + claim under one exclusive lock file so a stale-read
            # reaper can never unlink a lease acquired concurrently: the
            # whole reap -> list -> claim sequence is atomic across
            # processes and threads.
            with self._global_lock():
                # Reap stale locks first
                self._reap_stale_locks()

                # Count active leases
                active_leases = self._list_active_leases()

                if len(active_leases) < self.max_running:
                    # Try to claim an open slot
                    for slot_id in range(self.max_running):
                        lock_path = self._lease_dir / f"slot_{slot_id}.lock"
                        if not lock_path.exists():
                            try:
                                self._try_claim_lock(
                                    lock_path,
                                    owner,
                                    slot_id,
                                    durable=durable,
                                    project=project,
                                    job_id=job_id,
                                    piece=piece,
                                    attempt=attempt,
                                    worktree=worktree,
                                    timeout_s_meta=timeout_s_meta,
                                )
                                return True
                            except OSError:
                                # Another process claimed it concurrently
                                continue

            # If we reached here, all slots are held or contentious
            if on_waiting is not None and not notified_waiting:
                notified_waiting = True
                try:
                    on_waiting()
                except Exception:
                    pass

            time.sleep(poll_interval_s)

        raise LeaseError(
            f"Could not acquire executor slot within {timeout_s}s "
            f"(max_running={self.max_running}, active={len(self._list_active_leases())})"
        )

    def release(self, owner: str | None = None) -> None:
        """Release the currently held slot lease or the lease matching owner."""
        with self._global_lock():
            if owner is not None:
                for lock_file in sorted(self._lease_dir.glob("slot_*.lock")):
                    try:
                        data = json.loads(lock_file.read_text(encoding="utf-8"))
                        if data.get("owner") == owner:
                            lock_file.unlink(missing_ok=True)
                    except (json.JSONDecodeError, OSError):
                        pass
            if self._held_lock_path is not None:
                try:
                    self._held_lock_path.unlink(missing_ok=True)
                except OSError:
                    pass
                finally:
                    self._held_lock_path = None
                    self._held_slot = None

    @contextmanager
    def lease(
        self,
        owner: str,
        timeout_s: float = 300.0,
        poll_interval_s: float = 0.05,
        on_waiting: Callable[[], None] | None = None,
    ) -> Iterator[ExecutorSlots]:
        """Context manager for acquiring and releasing a lease slot."""
        self.acquire(
            owner,
            timeout_s=timeout_s,
            poll_interval_s=poll_interval_s,
            on_waiting=on_waiting,
        )
        try:
            yield self
        finally:
            self.release()

    @contextmanager
    def _global_lock(self) -> Iterator[None]:
        """Exclusive interprocess mutex for the reap -> list -> claim sequence.

        POSIX uses ``fcntl.flock``; Windows uses ``msvcrt.locking`` (signal 0
        must never be used there, and ``fcntl`` does not exist). Each holder
        opens its own file description, so threads in one process serialize
        against each other exactly like separate processes do.
        """
        self._lease_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self._lease_dir / ".slots.lock"
        with open(lock_path, "a+b") as init_fh:
            if init_fh.tell() == 0:
                init_fh.write(b"\x00")
                init_fh.flush()
        fh = open(lock_path, "r+b")
        try:
            if sys.platform == "win32":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            try:
                if sys.platform == "win32":
                    import msvcrt

                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            finally:
                fh.close()

    def _try_claim_lock(
        self,
        lock_path: Path,
        owner: str,
        slot_id: int,
        *,
        durable: bool = False,
        project: str | Path | None = None,
        job_id: str | None = None,
        piece: int | None = None,
        attempt: int | None = None,
        worktree: str | Path | None = None,
        timeout_s_meta: float | None = None,
    ) -> None:
        """Atomically claim a lock file for the given slot."""
        fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        try:
            lock_data = {
                "pid": os.getpid(),
                "owner": owner,
                "slot_id": slot_id,
                "acquired_at": time.time(),
                "durable": durable,
                "project": str(project) if project else None,
                "job_id": job_id,
                "piece": piece,
                "attempt": attempt,
                "worktree": str(worktree) if worktree else None,
                "timeout_s": timeout_s_meta or 3600.0,
            }
            os.write(fd, json.dumps(lock_data).encode("utf-8"))
        finally:
            os.close(fd)

        self._held_slot = slot_id
        self._held_lock_path = lock_path

    def _list_active_leases(self) -> list[dict]:
        """Return list of valid (non-stale) lease files."""
        leases = []
        for lock_file in sorted(self._lease_dir.glob("slot_*.lock")):
            try:
                data = json.loads(lock_file.read_text(encoding="utf-8"))
                if self._is_lease_active(data):
                    leases.append(data)
            except (json.JSONDecodeError, OSError):
                try:
                    stat = lock_file.stat()
                    if time.time() - stat.st_mtime < _INCOMPLETE_GRACE_PERIOD_S:
                        # Fresh incomplete file being written: treat as active lease
                        leases.append({"active": True, "lock_file": str(lock_file)})
                except OSError:
                    pass
        return leases

    def _reap_stale_locks(self) -> None:
        """Remove lock files whose owning process or attempt is no longer alive.

        A lease is removed only when its bytes are unchanged since the stale
        verdict: if a competitor freshly acquired the slot between our read
        and our unlink, the content differs and the fresh lease is kept.
        (Through :meth:`acquire` the reap -> claim sequence additionally
        holds :meth:`_global_lock`, which closes the window entirely.)
        """
        for lock_file in sorted(self._lease_dir.glob("slot_*.lock")):
            try:
                raw = lock_file.read_bytes()
            except OSError:
                continue
            try:
                data = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                try:
                    stat = lock_file.stat()
                    if time.time() - stat.st_mtime < _INCOMPLETE_GRACE_PERIOD_S:
                        # Fresh incomplete file being written: do not remove
                        continue
                    self._remove_if_unchanged(lock_file, raw)
                except OSError:
                    pass
                continue
            try:
                if self._is_lease_active(data):
                    continue
            except OSError:
                continue
            self._remove_if_unchanged(lock_file, raw)

    @staticmethod
    def _remove_if_unchanged(lock_file: Path, raw: bytes) -> None:
        """Unlink *lock_file* only if its bytes still equal *raw*.

        Guards against the stale-read race: never remove a lease that was
        (re)written after we judged the old content stale.
        """
        try:
            if lock_file.read_bytes() != raw:
                return
            lock_file.unlink(missing_ok=True)
        except OSError:
            pass

    def _is_lease_active(self, data: dict) -> bool:
        """Check if a lease is active (either process-backed or durable attempt)."""
        if data.get("durable"):
            return self._is_attempt_alive(data)
        pid = data.get("pid")
        if pid:
            return self._is_pid_alive(pid)
        return self._is_attempt_alive(data)

    @staticmethod
    def _is_attempt_alive(data: dict) -> bool:
        """Check if a durable attempt lease is still alive and running.

        Expiry is liveness-based, never wall-clock-based for a live attempt:
        a persisted ``running`` attempt (job not terminal, piece not
        accepted, project/worktree present) keeps its slot no matter how old
        the lease is. The native starter process exits right after acquiring,
        so no heartbeat is possible -- the records are the liveness signal.
        The wall clock is only a backstop for leases with no job linkage
        (and no live owner) where no liveness signal exists.
        """
        now = time.time()
        acquired_at = data.get("acquired_at")
        timeout_s = data.get("timeout_s") or 3600.0
        expired = bool(acquired_at and (now - acquired_at > timeout_s))

        project_str = data.get("project")
        job_id = data.get("job_id")
        piece_n = data.get("piece")
        attempt_n = data.get("attempt")
        worktree_str = data.get("worktree")

        creator_pid = data.get("pid")
        owner_alive = bool(creator_pid and ExecutorSlots._is_pid_alive(creator_pid))
        is_fresh = bool(
            acquired_at and (now - acquired_at <= _CREATION_GRACE_PERIOD_S)
        )

        # Without job linkage there is no attempt-liveness signal: a live
        # owner (or a just-created lease) never expires; only an ownerless
        # lease falls back to the wall clock.
        if not project_str or not job_id:
            if owner_alive or is_fresh:
                return True
            return not expired

        project_path = Path(project_str)
        if not project_path.exists():
            return False

        # Grace period for worktree creation (10 seconds)
        if (
            worktree_str
            and (now - (acquired_at or 0) > 10.0)
            and not Path(worktree_str).exists()
        ):
            return False

        try:
            from . import records
            job = records.load_job(project_path, job_id)
        except Exception:
            return False

        if (
            job.get("state") in ("blocked", "failed", "closed")
            or job.get("status") in ("blocked", "failed", "closed")
        ):
            return False

        for p in job.get("pieces") or []:
            if p.get("n") == piece_n:
                if p.get("accepted"):
                    return False
                for att in p.get("attempts") or []:
                    if att.get("n") == attempt_n:
                        return att.get("status") == "running"
                if owner_alive or is_fresh:
                    return True
                return False

        if owner_alive or is_fresh:
            return True
        return False

    @staticmethod
    def _is_pid_alive(pid: int) -> bool:
        """Check if a process with the given PID is still running."""
        from crew_chief._os_compat import pid_alive

        return pid_alive(pid)
