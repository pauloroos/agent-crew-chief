"""Proof for idempotent coordinator usage accounting (T014, FR-013/025, SC-006).

Every fixture is a temporary central root, a temporary coordinator worktree
and a temporary job record; no live model CLI and no real home is touched.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from crew_chief import central_usage, economy, records
from crew_chief.central_usage import UsageError


# --- fixtures ---------------------------------------------------------------


MEASURED = {
    "tokens_in": 11,
    "tokens_out": 7,
    "tokens_total": 18,
    "estimated": False,
    "model": "fake-model",
    "quota_family": None,
}


def make_record(tmp_path: Path, worktree: Path, **overrides) -> dict:
    """A coordinator registry record with a worktree of its own."""
    record = {
        "schema_version": 1,
        "id": str(uuid.uuid4()),
        "repo": str(tmp_path / "repo"),
        "worktree": str(worktree),
        "branch": f"central/{uuid.uuid4()}",
        "base_commit": "0" * 40,
        "backend": "default",
        "cli": "claude",
        "cli_version": "",
        "acc_version": "",
        "session_id": None,
        "mode": "raw-request",
        "authorization": {
            "mode": "raw-request",
            "approved": False,
            "authorize_raw": True,
            "settings": {"coordinator_unrestricted": True},
        },
        "job_ids": [],
        "active_job_id": None,
        "state": "created",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "last_error": None,
        "supervisor_nonce": None,
    }
    record.update(overrides)
    return record


def make_job(worktree: Path, job_id: str) -> Path:
    """A job record inside the coordinator worktree."""
    directory = worktree / "docs" / "crew-chief" / "jobs"
    directory.mkdir(parents=True, exist_ok=True)
    job = {
        "id": job_id,
        "title": "central job",
        "request": "do the bounded thing",
        "created": "2026-01-01T00:00:00+00:00",
        "distributed": True,
        "reason": "test",
        "issue_ref": "",
        "pieces": [],
    }
    path = directory / f"{job_id}.json"
    path.write_text(json.dumps(job, indent=2) + "\n", encoding="utf-8")
    return path


def write_round(
    root: Path,
    record: dict,
    *,
    usage: dict | None = MEASURED,
    active_job_id: str | None = None,
    state: str = "completed",
    log: str | None = None,
    round_id: str | None = None,
    bom: bool = False,
    newline: str = "\n",
    error: str | None = None,
) -> tuple[str, Path]:
    """One round metadata file, shaped like the accepted round contract."""
    coord = root / "coordinators" / record["id"]
    rounds = coord / "rounds"
    rounds.mkdir(parents=True, exist_ok=True)
    round_id = round_id or str(uuid.uuid4())
    meta = {
        "schema_version": 1,
        "id": round_id,
        "coordinator_id": record["id"],
        "state": state,
        "started_at": "2026-01-01T00:00:00+00:00",
        "finished_at": "2026-01-01T00:05:00+00:00",
        "usage": usage,
        "active_job_id": active_job_id,
        "job_ids": [active_job_id] if active_job_id else [],
        "logs": {"stdout": None, "stderr": None},
        "error": error,
    }
    if log is not None:
        logs = coord / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        (logs / f"{round_id}.stdout.log").write_text(log, encoding="utf-8")
        meta["logs"]["stdout"] = f"logs/{round_id}.stdout.log"
    text = json.dumps(meta, ensure_ascii=False, indent=2) + "\n"
    if newline == "\r\n":
        text = text.replace("\n", "\r\n")
    path = rounds / f"{round_id}.json"
    path.write_bytes((b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8"))
    return round_id, path


def round_result(round_id: str) -> dict:
    """The result shape the accepted round module returns."""
    return {
        "ok": True,
        "error": None,
        "round_id": round_id,
        "record": None,
        "round": {},
    }


def read_round(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def usage_of(worktree: Path, job_id: str) -> list[dict]:
    return records.load_job(worktree, job_id).get("usage") or []


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    path = tmp_path / "worktree"
    path.mkdir()
    return path


# --- tests ------------------------------------------------------------------


def test_measured_usage_is_recorded_once_in_the_active_job(tmp_path, worktree):
    root = tmp_path / "central"
    record = make_record(tmp_path, worktree, active_job_id="job-1")
    make_job(worktree, "job-1")
    round_id, path = write_round(root, record, active_job_id="job-1")

    central_usage.record_round_usage(root, record, round_result(round_id))

    entries = usage_of(worktree, "job-1")
    assert len(entries) == 1
    entry = entries[0]
    assert entry["role"] == "coordinator"
    assert entry["tokens_in"] == 11
    assert entry["tokens_out"] == 7
    assert entry["tokens_total"] == 18
    assert entry["estimated"] is False
    assert entry["model"] == "fake-model"
    assert entry["central_event_id"] == f"central:{record['id']}:{round_id}"

    event = read_round(path)["usage_event"]
    assert event["state"] == "recorded"
    assert event["job_id"] == "job-1"

    # the job history is untouched
    assert records.load_job(worktree, "job-1")["title"] == "central job"

    # a repeated call never counts the same event twice
    central_usage.record_round_usage(root, record, round_result(round_id))
    assert len(usage_of(worktree, "job-1")) == 1


def test_total_only_usage_keeps_the_reported_total(tmp_path, worktree):
    root = tmp_path / "central"
    record = make_record(tmp_path, worktree, active_job_id="job-1")
    make_job(worktree, "job-1")
    usage = dict(MEASURED, tokens_in=None, tokens_out=None, tokens_total=42)
    round_id, _ = write_round(root, record, usage=usage, active_job_id="job-1")

    central_usage.record_round_usage(root, record, round_result(round_id))

    entry = usage_of(worktree, "job-1")[0]
    assert entry["tokens_total"] == 42
    assert entry["tokens_in"] is None
    assert entry["tokens_out"] is None
    assert entry["estimated"] is False


def test_missing_usage_stays_unknown(tmp_path, worktree):
    root = tmp_path / "central"
    record = make_record(tmp_path, worktree, active_job_id="job-1")
    make_job(worktree, "job-1")
    round_id, _ = write_round(root, record, usage=None, active_job_id="job-1")

    central_usage.record_round_usage(root, record, round_result(round_id))

    entry = usage_of(worktree, "job-1")[0]
    assert entry["tokens_in"] is None
    assert entry["tokens_out"] is None
    assert entry["tokens_total"] is None
    assert entry["estimated"] is False
    # unknown is unknown: no zero was invented
    figures = economy.job_economy(records.load_job(worktree, "job-1"), None)
    assert figures.coordinator_tokens == 0
    assert figures.usage_recorded is False


def test_estimated_usage_is_labelled(tmp_path, worktree):
    root = tmp_path / "central"
    record = make_record(tmp_path, worktree, active_job_id="job-1")
    make_job(worktree, "job-1")
    usage = dict(MEASURED, tokens_in=None, tokens_out=None, tokens_total=None)
    round_id, _ = write_round(
        root, record, usage=usage, active_job_id="job-1", log="x" * 400
    )

    central_usage.record_round_usage(root, record, round_result(round_id))

    entry = usage_of(worktree, "job-1")[0]
    assert entry["tokens_total"] == 100
    assert entry["estimated"] is True
    assert entry["tokens_in"] is None
    figures = economy.job_economy(records.load_job(worktree, "job-1"), None)
    assert figures.coordinator_estimated is True


def test_an_unknown_active_job_keeps_the_usage_pending(tmp_path, worktree):
    root = tmp_path / "central"
    record = make_record(tmp_path, worktree)
    round_id, path = write_round(root, record)

    central_usage.record_round_usage(root, record, round_result(round_id))

    assert read_round(path)["usage_event"]["state"] == "pending"
    assert read_round(path)["usage_event"]["job_id"] is None
    pending = central_usage.pending_usage(root, record)
    assert len(pending) == 1
    assert pending[0]["tokens_total"] == 18
    assert pending[0]["job_id"] is None
    assert pending[0]["role"] == "coordinator"


def test_backfill_records_the_pending_usage_once(tmp_path, worktree):
    root = tmp_path / "central"
    record = make_record(tmp_path, worktree)
    round_id, path = write_round(root, record)

    central_usage.record_round_usage(root, record, round_result(round_id))
    make_job(worktree, "job-1")
    record["active_job_id"] = "job-1"

    central_usage.backfill_usage(root, record)

    entries = usage_of(worktree, "job-1")
    assert len(entries) == 1
    assert entries[0]["tokens_total"] == 18
    assert entries[0]["central_event_id"] == f"central:{record['id']}:{round_id}"
    assert read_round(path)["usage_event"]["state"] == "recorded"
    assert central_usage.pending_usage(root, record) == []

    # a second backfill never counts the same event twice
    central_usage.backfill_usage(root, record)
    assert len(usage_of(worktree, "job-1")) == 1


def test_a_crash_after_the_job_update_never_counts_twice(tmp_path, worktree, monkeypatch):
    root = tmp_path / "central"
    record = make_record(tmp_path, worktree, active_job_id="job-1")
    make_job(worktree, "job-1")
    round_id, path = write_round(root, record, active_job_id="job-1")

    def crash(path, meta, bom, newline):
        raise UsageError("simulated crash before the round was marked")

    monkeypatch.setattr(central_usage, "_mark_recorded", crash)
    with pytest.raises(UsageError):
        central_usage.record_round_usage(root, record, round_result(round_id))

    # the job holds the entry once; the round is bound but not marked
    assert len(usage_of(worktree, "job-1")) == 1
    event = read_round(path)["usage_event"]
    assert event["state"] == "pending"
    assert event["job_id"] == "job-1"

    monkeypatch.undo()
    central_usage.backfill_usage(root, record)

    assert len(usage_of(worktree, "job-1")) == 1
    assert read_round(path)["usage_event"]["state"] == "recorded"


def test_a_job_switch_does_not_reassign_spent_usage(tmp_path, worktree):
    root = tmp_path / "central"
    record = make_record(tmp_path, worktree, active_job_id="job-1")
    round_id, path = write_round(root, record, active_job_id="job-1")

    # the job is not there yet: the usage stays pending, bound to job-1
    central_usage.record_round_usage(root, record, round_result(round_id))
    assert read_round(path)["usage_event"]["job_id"] == "job-1"

    make_job(worktree, "job-1")
    make_job(worktree, "job-2")
    record["active_job_id"] = "job-2"

    central_usage.backfill_usage(root, record)

    assert len(usage_of(worktree, "job-1")) == 1
    assert usage_of(worktree, "job-2") == []


def test_the_same_event_is_never_counted_in_two_jobs(tmp_path, worktree):
    root = tmp_path / "central"
    record = make_record(tmp_path, worktree, active_job_id="job-1")
    make_job(worktree, "job-1")
    make_job(worktree, "job-2")
    round_id, _ = write_round(root, record, active_job_id="job-1")

    central_usage.record_round_usage(root, record, round_result(round_id))
    record["active_job_id"] = "job-2"
    central_usage.record_round_usage(root, record, round_result(round_id))

    assert len(usage_of(worktree, "job-1")) == 1
    assert usage_of(worktree, "job-2") == []


def test_an_attributed_central_session_keeps_its_role(tmp_path, worktree):
    root = tmp_path / "central"
    record = make_record(tmp_path, worktree, active_job_id="job-1")
    make_job(worktree, "job-1")
    usage = dict(MEASURED, role="central")
    round_id, _ = write_round(root, record, usage=usage, active_job_id="job-1")

    central_usage.record_round_usage(root, record, round_result(round_id))

    entry = usage_of(worktree, "job-1")[0]
    assert entry["role"] == "central"
    assert entry["tokens_total"] == 18


def test_a_failed_round_keeps_its_usage(tmp_path, worktree):
    root = tmp_path / "central"
    record = make_record(tmp_path, worktree, active_job_id="job-1")
    make_job(worktree, "job-1")
    round_id, _ = write_round(
        root, record, state="failed", error="the CLI exited 1", active_job_id="job-1"
    )

    central_usage.record_round_usage(root, record, round_result(round_id))

    assert len(usage_of(worktree, "job-1")) == 1


@pytest.mark.parametrize("bad", ["../evil", "not-a-uuid", "1" * 40, ""])
def test_an_invalid_round_id_is_rejected(tmp_path, worktree, bad):
    root = tmp_path / "central"
    record = make_record(tmp_path, worktree, active_job_id="job-1")
    make_job(worktree, "job-1")

    with pytest.raises(UsageError):
        central_usage.record_round_usage(root, record, round_result(bad))


def test_a_round_bound_to_another_coordinator_is_rejected(tmp_path, worktree):
    root = tmp_path / "central"
    record = make_record(tmp_path, worktree, active_job_id="job-1")
    make_job(worktree, "job-1")
    round_id, path = write_round(root, record, active_job_id="job-1")
    meta = read_round(path)
    meta["coordinator_id"] = str(uuid.uuid4())
    path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")

    with pytest.raises(UsageError):
        central_usage.record_round_usage(root, record, round_result(round_id))


def test_a_corrupt_round_file_is_reported_and_kept(tmp_path, worktree):
    root = tmp_path / "central"
    record = make_record(tmp_path, worktree, active_job_id="job-1")
    make_job(worktree, "job-1")
    corrupt = root / "coordinators" / record["id"] / "rounds" / f"{uuid.uuid4()}.json"
    corrupt.parent.mkdir(parents=True, exist_ok=True)
    corrupt.write_text("{not json", encoding="utf-8")

    with pytest.warns(UserWarning):
        central_usage.backfill_usage(root, record)

    assert corrupt.exists()
    assert corrupt.read_text(encoding="utf-8") == "{not json"


def test_an_oversized_round_file_is_reported(tmp_path, worktree):
    root = tmp_path / "central"
    record = make_record(tmp_path, worktree, active_job_id="job-1")
    make_job(worktree, "job-1")
    big = root / "coordinators" / record["id"] / "rounds" / f"{uuid.uuid4()}.json"
    big.parent.mkdir(parents=True, exist_ok=True)
    big.write_text(" " * (1024 * 1024 + 16), encoding="utf-8")

    with pytest.warns(UserWarning):
        central_usage.backfill_usage(root, record)

    assert big.exists()


def test_the_round_metadata_keeps_its_bom_and_newlines(tmp_path, worktree):
    root = tmp_path / "central"
    record = make_record(tmp_path, worktree, active_job_id="job-1")
    make_job(worktree, "job-1")
    round_id, path = write_round(
        root, record, active_job_id="job-1", bom=True, newline="\r\n"
    )

    central_usage.record_round_usage(root, record, round_result(round_id))

    data = path.read_bytes()
    assert data.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" in data
    meta = read_round(path)
    assert meta["usage_event"]["state"] == "recorded"
    assert meta["schema_version"] == 1
    assert meta["id"] == round_id
    assert meta["state"] == "completed"


@pytest.mark.parametrize("field,value", [
    ("schema_version", 2), ("schema_version", True),
    ("id", str(uuid.uuid4())), ("id", None),
    ("coordinator_id", str(uuid.uuid4())), ("coordinator_id", None),
])
def test_history_binding_is_checked_before_pending_or_backfill(tmp_path, worktree, field, value):
    root = tmp_path / "central"
    record = make_record(tmp_path, worktree, active_job_id="job-1")
    make_job(worktree, "job-1")
    _, bad = write_round(root, record, active_job_id="job-1")
    good_id, _ = write_round(root, record, active_job_id="job-1")
    meta = read_round(bad)
    meta[field] = value
    bad.write_text(json.dumps(meta), encoding="utf-8")
    before = bad.read_bytes()
    with pytest.warns(UserWarning):
        assert [item["round_id"] for item in central_usage.pending_usage(root, record)] == [good_id]
    with pytest.warns(UserWarning):
        central_usage.backfill_usage(root, record)
    assert len(usage_of(worktree, "job-1")) == 1
    assert bad.read_bytes() == before


def test_running_round_is_not_accounted_early(tmp_path, worktree):
    root = tmp_path / "central"
    record = make_record(tmp_path, worktree, active_job_id="job-1")
    make_job(worktree, "job-1")
    round_id, path = write_round(root, record, active_job_id="job-1", state="running")
    before = path.read_bytes()
    central_usage.record_round_usage(root, record, round_result(round_id))
    central_usage.backfill_usage(root, record)
    assert central_usage.pending_usage(root, record) == []
    assert usage_of(worktree, "job-1") == []
    assert path.read_bytes() == before


@pytest.mark.parametrize("bad", ["../evil", "bad\njob", ""])
def test_invalid_original_job_never_falls_through_to_a_new_job(tmp_path, worktree, bad):
    root = tmp_path / "central"
    record = make_record(tmp_path, worktree, active_job_id="job-2")
    make_job(worktree, "job-2")
    round_id, _ = write_round(root, record, active_job_id=bad)
    with pytest.raises(UsageError):
        central_usage.record_round_usage(root, record, round_result(round_id))
    assert usage_of(worktree, "job-2") == []


@pytest.mark.parametrize("state", ["pending", "recorded"])
def test_foreign_usage_event_id_is_rejected(tmp_path, worktree, state):
    root = tmp_path / "central"
    record = make_record(tmp_path, worktree, active_job_id="job-1")
    make_job(worktree, "job-1")
    round_id, path = write_round(root, record, active_job_id="job-1")
    meta = read_round(path)
    meta["usage_event"] = {"event_id": "foreign-event", "state": state,
                           "role": "coordinator", "job_id": "job-1"}
    path.write_text(json.dumps(meta), encoding="utf-8")
    with pytest.raises(UsageError):
        central_usage.record_round_usage(root, record, round_result(round_id))
    assert usage_of(worktree, "job-1") == []


def test_unreported_central_usage_is_not_estimated_from_coordinator_output(tmp_path, worktree):
    root = tmp_path / "central"
    record = make_record(tmp_path, worktree, active_job_id="job-1")
    make_job(worktree, "job-1")
    usage = dict(MEASURED, role="central", tokens_in=None, tokens_out=None, tokens_total=None)
    round_id, _ = write_round(root, record, usage=usage, active_job_id="job-1", log="x" * 400)
    central_usage.record_round_usage(root, record, round_result(round_id))
    entry = usage_of(worktree, "job-1")[0]
    assert entry["role"] == "central"
    assert entry["tokens_total"] is None
    assert entry["estimated"] is False


def test_partial_reported_counts_do_not_become_a_smaller_estimated_total(tmp_path, worktree):
    root = tmp_path / "central"
    record = make_record(tmp_path, worktree, active_job_id="job-1")
    make_job(worktree, "job-1")
    usage = dict(MEASURED, tokens_in=1000, tokens_out=None, tokens_total=None)
    round_id, _ = write_round(root, record, usage=usage, active_job_id="job-1", log="x" * 400)
    central_usage.record_round_usage(root, record, round_result(round_id))
    entry = usage_of(worktree, "job-1")[0]
    assert entry["tokens_in"] == 1000
    assert entry["tokens_out"] is None
    assert entry["tokens_total"] is None
    assert entry["estimated"] is False


def test_redirected_job_storage_never_receives_a_lock_or_write(tmp_path, worktree):
    import os
    import subprocess

    root = tmp_path / "central"
    record = make_record(tmp_path, worktree, active_job_id="job-1")
    outside_job = make_job(tmp_path / "outside", "job-1")
    destination = worktree / "docs" / "crew-chief" / "jobs"
    destination.parent.mkdir(parents=True)
    target = outside_job.parent
    if os.name == "nt":
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(destination), str(target)],
                                capture_output=True, shell=False)
        assert result.returncode == 0
    else:
        destination.symlink_to(target, target_is_directory=True)
    round_id, _ = write_round(root, record, active_job_id="job-1")
    before = {p.name: p.read_bytes() for p in target.iterdir()}
    try:
        with pytest.raises(UsageError):
            central_usage.record_round_usage(root, record, round_result(round_id))
        assert {p.name: p.read_bytes() for p in target.iterdir()} == before
    finally:
        if os.name == "nt":
            os.rmdir(destination)
        else:
            destination.unlink()


def test_concurrent_first_bind_cannot_charge_two_jobs(tmp_path, worktree, monkeypatch):
    from contextlib import contextmanager
    import threading

    root = tmp_path / "central"
    record = make_record(tmp_path, worktree, active_job_id="job-1")
    make_job(worktree, "job-1")
    make_job(worktree, "job-2")
    round_id, path = write_round(root, record)
    first_at_bind = threading.Event()
    second_approaching = threading.Event()
    original_bind = central_usage._bind
    original_read = central_usage._read_round
    original_lock = records.file_lock
    errors = []

    def read(*args):
        value = original_read(*args)
        if threading.current_thread().name == "usage-second":
            second_approaching.set()
        return value

    def bind(*args):
        if threading.current_thread().name == "usage-first":
            first_at_bind.set()
            assert second_approaching.wait(5)
        return original_bind(*args)

    @contextmanager
    def lock(lock_path, *args, **kwargs):
        if Path(lock_path).parent == path.parent and threading.current_thread().name == "usage-second":
            second_approaching.set()
        with original_lock(lock_path, *args, **kwargs) as held:
            yield held

    def run(current):
        try:
            central_usage.record_round_usage(root, current, round_result(round_id))
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(central_usage, "_read_round", read)
    monkeypatch.setattr(central_usage, "_bind", bind)
    monkeypatch.setattr(records, "file_lock", lock)
    first = threading.Thread(name="usage-first", target=run, args=(record,))
    second = threading.Thread(name="usage-second", target=run,
                              args=(dict(record, active_job_id="job-2"),))
    first.start()
    try:
        assert first_at_bind.wait(5)
        second.start()
        first.join(10)
        second.join(10)
        assert not first.is_alive() and not second.is_alive()
        assert errors == []
        assert len(usage_of(worktree, "job-1")) == 1
        assert usage_of(worktree, "job-2") == []
    finally:
        second_approaching.set()
        first.join(10)
        if second.ident is not None:
            second.join(10)


def test_corrupt_job_is_reported_and_history_is_kept(tmp_path, worktree):
    root = tmp_path / "central"
    record = make_record(tmp_path, worktree, active_job_id="job-1")
    job_path = make_job(worktree, "job-1")
    job_path.write_text("{corrupt", encoding="utf-8")
    round_id, path = write_round(root, record, active_job_id="job-1")
    with pytest.raises(UsageError, match="job"):
        central_usage.record_round_usage(root, record, round_result(round_id))
    with pytest.warns(UserWarning, match="job"):
        central_usage.backfill_usage(root, record)
    assert job_path.read_text(encoding="utf-8") == "{corrupt"
    assert read_round(path)["usage_event"]["state"] == "pending"


def test_job_lock_failure_is_not_reported_as_an_absent_job(tmp_path, worktree, monkeypatch):
    root = tmp_path / "central"
    record = make_record(tmp_path, worktree, active_job_id="job-1")
    make_job(worktree, "job-1")
    round_id, _ = write_round(root, record, active_job_id="job-1")

    def unavailable(*args, **kwargs):
        raise records.JobLockTimeout("fake job lock timeout")

    monkeypatch.setattr(records, "update_job", unavailable)
    with pytest.raises(UsageError, match="job"):
        central_usage.record_round_usage(root, record, round_result(round_id))
