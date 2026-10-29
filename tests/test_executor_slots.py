"""Tests for FR-025: machine-wide executor concurrency cap via file-backed leases."""

from __future__ import annotations

import json
import multiprocessing
import os
import tempfile
import time
from pathlib import Path

import pytest

from crew_chief.executor_slots import ExecutorSlots, LeaseError


def _holder_worker(
    lease_dir: str,
    max_running: int,
    hold_event,
    started_queue,
    finished_queue,
    active_counter,
    max_active_counter,
    counter_lock,
) -> None:
    """Worker process that acquires a slot, holds it until hold_event is set, and records metrics."""
    slots = ExecutorSlots(lease_dir=lease_dir, max_running=max_running)
    pid = os.getpid()
    try:
        slots.acquire(f"proc-{pid}", timeout_s=10.0)
        with counter_lock:
            active_counter.value += 1
            if active_counter.value > max_active_counter.value:
                max_active_counter.value = active_counter.value
        started_queue.put((pid, time.time()))

        # Wait until released
        hold_event.wait(timeout=10.0)

        with counter_lock:
            active_counter.value -= 1
        slots.release()
        finished_queue.put((pid, time.time()))
    except Exception as exc:
        finished_queue.put((pid, f"error: {exc}"))
        try:
            slots.release()
        except Exception:
            pass


def _orphan_worker(
    lease_dir: str,
    max_running: int,
    ready_conn: Any,
) -> None:
    """Worker process that acquires a slot and terminates abruptly without releasing."""
    slots = ExecutorSlots(lease_dir=lease_dir, max_running=max_running)
    pid = os.getpid()
    slots.acquire(f"orphan-{pid}", timeout_s=5.0)
    ready_conn.send(f"{pid}:{slots._held_slot}")
    ready_conn.close()
    os._exit(0)


def test_global_cap_across_concurrent_processes():
    """Verify that file-backed lease lock enforces max_running_executors across processes.
    
    Starts separate OS processes, holds 3 leases while surplus contends, measures
    max_active <= 3, and proves queued surplus starts only after a release.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        lease_dir = str(Path(tmpdir) / "leases")
        max_running = 3
        surplus_count = 2

        hold_event = multiprocessing.Event()
        counter_lock = multiprocessing.Lock()
        active_counter = multiprocessing.Value("i", 0)
        max_active_counter = multiprocessing.Value("i", 0)

        started_queue = multiprocessing.Queue()
        finished_queue = multiprocessing.Queue()

        # 1. Start 3 primary processes that will acquire and hold
        primary_procs = []
        for _ in range(max_running):
            p = multiprocessing.Process(
                target=_holder_worker,
                args=(
                    lease_dir,
                    max_running,
                    hold_event,
                    started_queue,
                    finished_queue,
                    active_counter,
                    max_active_counter,
                    counter_lock,
                ),
            )
            p.start()
            primary_procs.append(p)

        # Wait for all 3 primary processes to acquire their leases
        primary_starts = {}
        for _ in range(max_running):
            pid, start_time = started_queue.get(timeout=5.0)
            primary_starts[pid] = start_time

        assert len(primary_starts) == max_running
        with counter_lock:
            assert active_counter.value == 3
            assert max_active_counter.value == 3

        # 2. Start surplus processes while leases are still held
        surplus_procs = []
        for _ in range(surplus_count):
            p = multiprocessing.Process(
                target=_holder_worker,
                args=(
                    lease_dir,
                    max_running,
                    hold_event,
                    started_queue,
                    finished_queue,
                    active_counter,
                    max_active_counter,
                    counter_lock,
                ),
            )
            p.start()
            surplus_procs.append(p)

        # Give surplus time to contend and verify they are blocked
        time.sleep(0.4)
        assert started_queue.empty(), "Surplus processes must not acquire while leases are full"
        with counter_lock:
            assert active_counter.value == 3
            assert max_active_counter.value == 3

        # 3. Release primary leases
        hold_event.set()

        # 4. Surplus processes should now acquire
        surplus_starts = {}
        for _ in range(surplus_count):
            pid, start_time = started_queue.get(timeout=5.0)
            surplus_starts[pid] = start_time

        # Wait for all processes to finish
        for p in primary_procs + surplus_procs:
            p.join(timeout=10.0)

        # Collect all finished timestamps
        finished_times = {}
        for _ in range(max_running + surplus_count):
            pid, fin_time = finished_queue.get(timeout=5.0)
            finished_times[pid] = fin_time

        # Verify max concurrent active never exceeded max_running (3)
        with counter_lock:
            assert max_active_counter.value <= max_running, (
                f"Max active exceeded cap: {max_active_counter.value} > {max_running}"
            )

        # Verify every surplus started only AFTER at least one primary had finished
        earliest_finish = min(
            fin for pid, fin in finished_times.items() if pid in primary_starts
        )
        for pid, s_time in surplus_starts.items():
            assert s_time >= earliest_finish - 0.05, (
                f"Surplus process {pid} started at {s_time} before any lease was released ({earliest_finish})"
            )


def test_initial_max1_override():
    """Verify max_running_executors override of 1 enforces strict mutual exclusion."""
    with tempfile.TemporaryDirectory() as tmpdir:
        lease_dir = str(Path(tmpdir) / "leases")
        slots1 = ExecutorSlots(lease_dir=lease_dir, max_running=1)
        slots2 = ExecutorSlots(lease_dir=lease_dir, max_running=1)

        assert slots1.acquire("proc-1", timeout_s=1.0) is True

        # Second cannot acquire while first holds it
        with pytest.raises(LeaseError):
            slots2.acquire("proc-2", timeout_s=0.2)

        # After release, second succeeds
        slots1.release()
        assert slots2.acquire("proc-2", timeout_s=1.0) is True
        slots2.release()


def test_exception_and_timeout_leasefree():
    """Verify slots are freed on exceptions and timeouts without leaking files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        lease_dir = str(Path(tmpdir) / "leases")
        slots = ExecutorSlots(lease_dir=lease_dir, max_running=1)

        # 1. Exception during slot hold: finally release ensures slot is free
        try:
            slots.acquire("err-proc", timeout_s=1.0)
            raise RuntimeError("simulated error")
        except RuntimeError:
            slots.release()

        # Slot must be immediately acquirable again
        slots2 = ExecutorSlots(lease_dir=lease_dir, max_running=1)
        assert slots2.acquire("next-proc", timeout_s=1.0) is True

        # 2. Timeout trying to acquire: does not leak lock files
        slots3 = ExecutorSlots(lease_dir=lease_dir, max_running=1)
        with pytest.raises(LeaseError):
            slots3.acquire("timeout-proc", timeout_s=0.2)

        # Release holder
        slots2.release()

        # Slot can be acquired now
        assert slots3.acquire("timeout-proc", timeout_s=1.0) is True
        slots3.release()


def test_on_waiting_callback():
    """Verify on_waiting callback is triggered when surplus executor must wait."""
    with tempfile.TemporaryDirectory() as tmpdir:
        lease_dir = str(Path(tmpdir) / "leases")
        slots1 = ExecutorSlots(lease_dir=lease_dir, max_running=1)
        slots2 = ExecutorSlots(lease_dir=lease_dir, max_running=1)

        slots1.acquire("proc-1")

        waiting_called = []

        def _on_wait():
            waiting_called.append(True)
            # Release slot1 from within wait callback so slots2 can acquire
            slots1.release()

        acquired = slots2.acquire("proc-2", timeout_s=2.0, on_waiting=_on_wait)
        assert acquired is True
        assert len(waiting_called) == 1
        slots2.release()


def test_acquire_and_release_single_slot():
    """Single process can acquire and release a slot."""
    with tempfile.TemporaryDirectory() as tmpdir:
        lease_dir = str(Path(tmpdir) / "leases")
        slots = ExecutorSlots(lease_dir=lease_dir, max_running=3)

        acquired = slots.acquire("test-proc-1", timeout_s=2.0)
        assert acquired is True

        slots.release()


def test_max_running_enforced():
    """Cannot acquire more than max_running slots simultaneously."""
    with tempfile.TemporaryDirectory() as tmpdir:
        lease_dir = str(Path(tmpdir) / "leases")
        max_running = 2
        slots1 = ExecutorSlots(lease_dir=lease_dir, max_running=max_running)
        slots2 = ExecutorSlots(lease_dir=lease_dir, max_running=max_running)
        slots3 = ExecutorSlots(lease_dir=lease_dir, max_running=max_running)

        # First two should succeed
        assert slots1.acquire("proc-1", timeout_s=1.0) is True
        assert slots2.acquire("proc-2", timeout_s=1.0) is True

        # Third should timeout
        with pytest.raises(LeaseError):
            slots3.acquire("proc-3", timeout_s=0.5)

        # Cleanup
        slots1.release()
        slots2.release()


def test_stale_lock_reclamation():
    """Stale locks from dead processes are reclaimed."""
    with tempfile.TemporaryDirectory() as tmpdir:
        lease_dir = str(Path(tmpdir) / "leases")
        slots = ExecutorSlots(lease_dir=lease_dir, max_running=2)

        # Manually create a stale lock file with an old PID
        import json
        lock_path = Path(lease_dir) / "slot_0.lock"
        stale_data = {
            "pid": 999999,  # Non-existent PID
            "owner": "dead-proc",
            "acquired_at": time.time() - 3600,  # 1 hour ago
        }
        lock_path.write_text(json.dumps(stale_data))

        # Should be able to acquire since the lock is stale
        acquired = slots.acquire("live-proc", timeout_s=2.0)
        assert acquired is True

        slots.release()


def test_default_max_running_is_3():
    """Default max_running_executors is 3."""
    with tempfile.TemporaryDirectory() as tmpdir:
        lease_dir = str(Path(tmpdir) / "leases")
        slots = ExecutorSlots(lease_dir=lease_dir)

        # Should default to 3
        assert slots.max_running == 3


def test_reclaims_orphaned_slots():
    """Detects and reclaims slots held by dead PIDs automatically (T033, FR-025).

    Verifies that:
    1. An orphaned lease from an abruptly terminated process is reclaimed.
    2. A live process's lease is NOT reclaimed.
    3. Surplus processes queue when capacity is reached.
    4. Concurrent reclamation adheres to the global concurrency cap.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        lease_dir = str(Path(tmpdir) / "leases")
        max_running = 2

        hold_event = multiprocessing.Event()
        counter_lock = multiprocessing.Lock()
        active_counter = multiprocessing.Value("i", 0)
        max_active_counter = multiprocessing.Value("i", 0)

        started_queue = multiprocessing.Queue()
        finished_queue = multiprocessing.Queue()

        # 1. Start a live holder process
        live_proc = multiprocessing.Process(
            target=_holder_worker,
            args=(
                lease_dir,
                max_running,
                hold_event,
                started_queue,
                finished_queue,
                active_counter,
                max_active_counter,
                counter_lock,
            ),
        )
        live_proc.start()
        live_pid, _ = started_queue.get(timeout=5.0)

        with counter_lock:
            assert active_counter.value == 1

        # 2. Start an orphan holder process that acquires and exits abruptly
        r_conn, w_conn = multiprocessing.Pipe(duplex=False)
        orphan_proc = multiprocessing.Process(
            target=_orphan_worker,
            args=(lease_dir, max_running, w_conn),
        )
        orphan_proc.start()
        w_conn.close()
        line = r_conn.recv().strip()
        orphan_pid_str, orphan_slot_str = line.split(":")
        orphan_pid, orphan_slot = int(orphan_pid_str), int(orphan_slot_str)
        orphan_proc.join(timeout=5.0)
        r_conn.close()

        # Confirm orphan process is dead
        assert not ExecutorSlots._is_pid_alive(orphan_pid), f"PID {orphan_pid} should be dead"

        # Verify both lock files exist on disk before reclamation
        lock_files = list(Path(lease_dir).glob("slot_*.lock"))
        assert len(lock_files) == 2

        # 3. Start 2 surplus processes contending for the remaining slot
        # One should reclaim the orphaned slot; the other must queue
        surplus_procs = []
        for _ in range(2):
            p = multiprocessing.Process(
                target=_holder_worker,
                args=(
                    lease_dir,
                    max_running,
                    hold_event,
                    started_queue,
                    finished_queue,
                    active_counter,
                    max_active_counter,
                    counter_lock,
                ),
            )
            p.start()
            surplus_procs.append(p)

        # 4. First surplus process acquires the reclaimed orphan slot
        surplus1_pid, _ = started_queue.get(timeout=5.0)
        assert surplus1_pid != live_pid

        # Verify live holder lock is NOT reclaimed (its lock file still belongs to live_pid)
        live_locks = [
            f
            for f in Path(lease_dir).glob("slot_*.lock")
            if json.loads(f.read_text(encoding="utf-8")).get("pid") == live_pid
        ]
        assert len(live_locks) == 1, "Live process lease must not be reclaimed"

        # 5. Second surplus process must queue while capacity (2) is reached
        time.sleep(0.4)
        assert started_queue.empty(), "Surplus process must queue while slots are full"
        with counter_lock:
            assert active_counter.value == 2
            assert max_active_counter.value == 2

        # 6. Release held leases
        hold_event.set()

        # Now second surplus process acquires and starts
        surplus2_pid, _ = started_queue.get(timeout=5.0)
        assert surplus2_pid not in (live_pid, surplus1_pid)

        # Join all worker processes
        live_proc.join(timeout=10.0)
        for p in surplus_procs:
            p.join(timeout=10.0)

        # Collect finished queue
        finished = [finished_queue.get(timeout=5.0) for _ in range(3)]
        assert len(finished) == 3

        # 7. Concurrent reclaim cap check: max_active never exceeded max_running (2)
        with counter_lock:
            assert max_active_counter.value <= max_running, (
                f"Concurrency cap exceeded: {max_active_counter.value} > {max_running}"
            )


def test_concurrent_window_incomplete_lease_not_stolen():
    """Window 1 (P1-7): Fresh incomplete lease JSON is not reaped and slot is not stolen."""
    with tempfile.TemporaryDirectory() as tmpdir:
        lease_dir = Path(tmpdir) / "leases"
        lease_dir.mkdir()
        slots = ExecutorSlots(lease_dir=lease_dir, max_running=1)

        # Create an incomplete lock file for slot 0 (simulating concurrent open before write)
        lock_file = lease_dir / "slot_0.lock"
        lock_file.write_text("{ incomplete json", encoding="utf-8")

        # Second slots instance attempts to acquire with max_running=1
        slots2 = ExecutorSlots(lease_dir=lease_dir, max_running=1)
        with pytest.raises(LeaseError):
            slots2.acquire("proc-2", timeout_s=0.2)

        # Verify incomplete lock was NOT deleted
        assert lock_file.exists()


def test_concurrent_window_unpersisted_durable_attempt_not_stolen(tmp_path):
    """Window 2 (P1-7): Durable lease reserved before attempt persisted is not stolen."""
    from crew_chief import records

    repo = tmp_path / "repo"
    repo.mkdir()
    job = records.new_job(repo, "Test Window 2", "req", False, "test")
    records.add_piece(job, "p1", "code", "low")
    records.save_job(repo, job)

    lease_dir = tmp_path / "leases"
    slots1 = ExecutorSlots(lease_dir=lease_dir, max_running=1)
    slots2 = ExecutorSlots(lease_dir=lease_dir, max_running=1)

    # Proc 1 acquires durable lease for attempt 1
    acquired = slots1.acquire(
        "proc-1",
        timeout_s=1.0,
        durable=True,
        project=repo,
        job_id=job["id"],
        piece=1,
        attempt=1,
    )
    assert acquired is True

    # At this point, attempt 1 is NOT yet persisted in job record.
    # Proc 2 attempts to acquire with max_running=1; must not steal the slot.
    with pytest.raises(LeaseError):
        slots2.acquire("proc-2", timeout_s=0.2)

    # Proc 1 finishes persisting the attempt
    def add_att(j):
        records.add_attempt(j, 1, {"n": 1, "role": "exec", "status": "running"})

    records.update_job(repo, job["id"], add_att)

    # Proc 2 still cannot acquire while running
    with pytest.raises(LeaseError):
        slots2.acquire("proc-2", timeout_s=0.2)

    # When released, proc 2 can acquire
    slots1.release()
    assert slots2.acquire("proc-2", timeout_s=1.0) is True
    slots2.release()


def test_concurrency_race_both_windows_preserves_single_slot(tmp_path):
    """Concurrency test reproducing both windows with racing threads (P1-7)."""
    import threading
    from crew_chief import records

    repo = tmp_path / "repo"
    repo.mkdir()
    job = records.new_job(repo, "Test Concurrency", "req", False, "test")
    records.add_piece(job, "p1", "code", "low")
    records.save_job(repo, job)

    lease_dir = tmp_path / "leases"
    slots_holder = ExecutorSlots(lease_dir=lease_dir, max_running=1)
    slots_competitor = ExecutorSlots(lease_dir=lease_dir, max_running=1)

    # Step A: Incomplete file window race
    lock_file = lease_dir / "slot_0.lock"
    lock_file.touch()

    stolen_window1 = []

    def try_steal_w1():
        try:
            slots_competitor.acquire("th-competitor-1", timeout_s=0.15)
            stolen_window1.append(True)
        except LeaseError:
            pass

    t1 = threading.Thread(target=try_steal_w1)
    t1.start()
    t1.join()
    assert not stolen_window1, "Competitor must not steal slot during incomplete lock file window"

    # Step B: Durable attempt persistence window race
    # Release the dummy lock file
    lock_file.unlink(missing_ok=True)

    slots_holder.acquire(
        "holder-proc",
        timeout_s=1.0,
        durable=True,
        project=repo,
        job_id=job["id"],
        piece=1,
        attempt=1,
    )

    stolen_window2 = []

    def try_steal_w2():
        try:
            slots_competitor.acquire("th-competitor-2", timeout_s=0.15)
            stolen_window2.append(True)
        except LeaseError:
            pass

    t2 = threading.Thread(target=try_steal_w2)
    t2.start()
    t2.join()
    assert not stolen_window2, "Competitor must not steal durable lease before attempt is persisted"

    slots_holder.release()


# --- Round-2 P1-2 regression tests: race-free reclaim + no expiry while running ---


def _aba_reaper_worker(
    lease_dir: str,
    read_event,
    proceed_event,
    result_queue,
    active_counter,
    max_active_counter,
    counter_lock,
) -> None:
    """Stale reaper paused mid-decision (defect (a) reproducer).

    Reads a stale lease, pauses inside the stale verdict, and only then
    unlinks -- exactly the read -> decide-stale -> unlink race. By the time
    it resumes, a competitor has freshly acquired the same slot file.
    Holds the slot 0.5s so simultaneous holding is observable.
    """
    slots = ExecutorSlots(lease_dir=lease_dir, max_running=1)
    base_is_active = ExecutorSlots._is_lease_active

    def _paused(self, data):
        if data.get("owner") == "dead-old":
            read_event.set()
            proceed_event.wait(timeout=30)
        return base_is_active(self, data)

    slots._is_lease_active = _paused.__get__(slots, ExecutorSlots)
    try:
        slots.acquire("B", timeout_s=15.0)
        with counter_lock:
            active_counter.value += 1
            if active_counter.value > max_active_counter.value:
                max_active_counter.value = active_counter.value
        result_queue.put(("B", True))
        time.sleep(0.5)
        with counter_lock:
            active_counter.value -= 1
        slots.release()
    except LeaseError:
        result_queue.put(("B", "LeaseError"))
    except Exception as exc:  # pragma: no cover - diagnostic path
        result_queue.put(("B", f"error: {exc}"))


def test_stale_reclaim_never_removes_fresh_lease_spawn():
    """Defect (a): a stale-read reaper must never break single-slot holding.

    Uses the spawn start method (portable: fork/spawn on Linux/macOS/Windows).
    B pauses mid stale-decision on a dead lease while C acquires the slot;
    B resumes mid-hold. A shared counter tracks simultaneous holders: it
    must never exceed 1. Before the fix, B unlinks C's fresh lease and both
    hold the single slot (max reaches 2).
    """
    import threading

    ctx = multiprocessing.get_context("spawn")
    with tempfile.TemporaryDirectory() as tmpdir:
        lease_dir = Path(tmpdir) / "leases"
        lease_dir.mkdir(parents=True, exist_ok=True)
        (lease_dir / "slot_0.lock").write_text(
            json.dumps({"pid": 99999999, "owner": "dead-old"}), encoding="utf-8"
        )
        read_event = ctx.Event()
        proceed_event = ctx.Event()
        result_queue = ctx.Queue()
        counter_lock = ctx.Lock()
        active_counter = ctx.Value("i", 0)
        max_active_counter = ctx.Value("i", 0)
        proc = ctx.Process(
            target=_aba_reaper_worker,
            args=(
                str(lease_dir),
                read_event,
                proceed_event,
                result_queue,
                active_counter,
                max_active_counter,
                counter_lock,
            ),
        )
        proc.start()
        try:
            assert read_event.wait(timeout=30), "reaper never reached stale decision"
            # Resume the paused reaper while C is acquiring/holding, without
            # blocking the main thread (C may wait on the slot lock).
            timer = threading.Timer(0.5, proceed_event.set)
            timer.start()
            try:
                holder = ExecutorSlots(lease_dir=lease_dir, max_running=1)
                assert holder.acquire("C", timeout_s=15.0) is True
                with counter_lock:
                    active_counter.value += 1
                    if active_counter.value > max_active_counter.value:
                        max_active_counter.value = active_counter.value
                try:
                    time.sleep(1.5)  # B resumes mid-hold and tries to steal
                finally:
                    with counter_lock:
                        active_counter.value -= 1
                    holder.release()
            finally:
                timer.join(timeout=10)
            proc.join(timeout=30)
            if proc.is_alive():
                proc.terminate()
                proc.join(timeout=5)
            assert max_active_counter.value == 1, (
                "two owners held the single slot concurrently "
                f"(max_active={max_active_counter.value})"
            )
        finally:
            if proc.is_alive():
                proc.terminate()
                proc.join(timeout=5)


def _live_holder_worker(
    lease_dir: str,
    project_str: str,
    job_id: str,
    acquired_event,
    hold_event,
    result_queue,
) -> None:
    """Durable holder (defect (b) reproducer): acquires then holds past expiry."""
    slots = ExecutorSlots(lease_dir=lease_dir, max_running=1)
    try:
        ok = slots.acquire(
            "A",
            timeout_s=10.0,
            durable=True,
            project=project_str,
            job_id=job_id,
            piece=1,
            attempt=1,
            timeout_s_meta=0.3,
        )
        result_queue.put(("A-acquired", ok))
        acquired_event.set()
        hold_event.wait(timeout=30)
        try:
            current = json.loads(
                (Path(lease_dir) / "slot_0.lock").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            current = {"owner": f"unreadable: {exc}"}
        result_queue.put(("A-final-owner", current.get("owner")))
        slots.release()
    except Exception as exc:  # pragma: no cover - diagnostic path
        result_queue.put(("A", f"error: {exc}"))


def test_live_running_attempt_lease_does_not_expire_spawn(tmp_path):
    """Defect (b): a lease whose attempt is still running must not expire.

    Uses the spawn start method (portable: fork/spawn on Linux/macOS/Windows).
    A holds a durable lease with a persisted ``running`` attempt past its
    wall-clock ``timeout_s_meta``; B must not acquire while A still runs.
    """
    from crew_chief import records

    ctx = multiprocessing.get_context("spawn")
    project = tmp_path / "repo"
    project.mkdir()
    job = records.new_job(project, "Live native", "request", False, "review")
    records.add_piece(job, "p", "code", "low")
    records.add_attempt(
        job,
        1,
        {"n": 1, "role": "exec", "status": "running", "executor": "native:test/model"},
    )
    records.save_job(project, job)

    lease_dir = tmp_path / "leases"
    acquired_event = ctx.Event()
    hold_event = ctx.Event()
    result_queue = ctx.Queue()
    proc = ctx.Process(
        target=_live_holder_worker,
        args=(
            str(lease_dir),
            str(project),
            job["id"],
            acquired_event,
            hold_event,
            result_queue,
        ),
    )
    proc.start()
    try:
        assert acquired_event.wait(timeout=30), "holder A never acquired the slot"
        assert result_queue.get(timeout=30) == ("A-acquired", True)
        time.sleep(0.6)  # let the 0.3s wall-clock timeout elapse while A runs
        competitor = ExecutorSlots(lease_dir=lease_dir, max_running=1)
        try:
            with pytest.raises(LeaseError):
                competitor.acquire("B", timeout_s=1.0)
        finally:
            competitor.release()
        current = json.loads(
            (lease_dir / "slot_0.lock").read_text(encoding="utf-8")
        )
        assert current.get("owner") == "A", (
            f"live lease expired while attempt still running: {current}"
        )
    finally:
        hold_event.set()
        proc.join(timeout=30)
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=5)
    assert result_queue.get(timeout=30) == ("A-final-owner", "A")


