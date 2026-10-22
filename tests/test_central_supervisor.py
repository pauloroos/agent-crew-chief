"""Proof for the detached coordinator supervisor (T015, FR-009/011/024, SC-004).

Every supervisor loop is driven by an injected fake clock and fake runner; one
end-to-end test starts a real detached supervisor against a fake ``claude`` on
PATH. No test probes a live model CLI, writes global preferences or signals an
unrelated process.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from crew_chief import central_messages, central_registry, central_supervisor
from crew_chief.central_supervisor import start_supervisor, supervise

# --- fakes ------------------------------------------------------------------

CLAUDE_HELP = """Usage: claude [options]
  -p
  --resume SESSION
  --output-format stream-json
  --verbose
  --dangerously-skip-permissions
"""

FAKE_CLI = '''
import json, os, sys

HELP = __HELP__
CAPTURE = os.environ.get("FAKE_CLI_CAPTURE")

args = sys.argv[1:]
if "--version" in args:
    print("1.2.3 (Claude Code)")
    raise SystemExit(0)
if "--help" in args:
    print(HELP)
    raise SystemExit(0)
prompt = sys.stdin.read()
if CAPTURE:
    with open(CAPTURE, "w", encoding="utf-8") as handle:
        json.dump({"argv": args, "prompt": prompt}, handle)
context = {}
if "CONTEXT_JSON" in prompt:
    context = json.loads(prompt.split("CONTEXT_JSON", 1)[1])
frame = {
    "state": "completed",
    "active_job_id": None,
    "job_ids": [],
    "blocked_task_refs": [],
    "questions": [],
    "answers": [],
    "consumed_message_ids": [item["id"] for item in context.get("inbox") or []],
}
print(json.dumps({
    "type": "result",
    "session_id": "sess-boot",
    "result": "ACC_ROUND " + json.dumps(frame),
    "usage": {"input_tokens": 5, "output_tokens": 3},
}))
'''


class FakeClock:
    """Epoch seconds that only advance when the supervisor sleeps."""

    def __init__(self) -> None:
        self.value = 1_700_000_000.0
        self.sleeps = 0
        self.on_sleep = None

    def time(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps += 1
        self.value += seconds
        if self.on_sleep is not None:
            self.on_sleep(self.sleeps)


class FakeRunner:
    """Return canned round results and remember every record it was given."""

    def __init__(self, results) -> None:
        self.results = list(results)
        self.calls: list[dict] = []

    def __call__(self, record: dict, timeout_s: float) -> dict:
        self.calls.append({"record": dict(record), "timeout_s": timeout_s})
        item = self.results.pop(0)
        return item(record) if callable(item) else dict(item)


# --- helpers ----------------------------------------------------------------


def record_for(tmp_path: Path, **overrides) -> dict:
    worktree = tmp_path / "worktree"
    worktree.mkdir(exist_ok=True)
    stamp = "2026-01-01T00:00:00+00:00"
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
        "created_at": stamp,
        "updated_at": stamp,
        "last_error": None,
        "supervisor_nonce": None,
    }
    record.update(overrides)
    return record


def prepared(tmp_path: Path, **overrides):
    root = tmp_path / "central"
    record = record_for(tmp_path, **overrides)
    central_registry.register(root, record)
    coord = central_messages._coordinator_dir(root, record["id"], create=True)
    (coord / "request.md").write_bytes(b"Run the bounded task.\r\n")
    return root, record, coord


def stored(root: Path, record: dict) -> dict:
    return central_registry.load_registry(root)["coordinators"][record["id"]]


def supervisor_file(coord: Path) -> dict:
    return json.loads((coord / "supervisor.json").read_text(encoding="utf-8"))


def send_control(coord: Path, command: str, order: int = 0) -> dict:
    payload = {
        "schema_version": 1,
        "id": str(uuid.uuid4()),
        "command": command,
        "requested_at": "2026-01-01T00:00:00+00:00",
    }
    directory = coord / "control"
    directory.mkdir(exist_ok=True)
    path = directory / f"{payload['id']}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    stamp = 1_700_000_000 + order
    os.utime(path, (stamp, stamp))
    return payload


def round_result(
    record: dict,
    *,
    ok: bool = True,
    state: str = "running",
    session_id: str | None = None,
    error: str | None = None,
    round_id: str | None = None,
    meta: dict | None = None,
) -> dict:
    new = dict(record)
    if session_id is not None:
        new["session_id"] = session_id
    new["state"] = state if ok else "failed"
    rid = round_id or str(uuid.uuid4())
    round_meta = {
        "schema_version": 1,
        "id": rid,
        "state": "completed" if ok else "failed",
        "consumed_message_ids": [],
        "result": None,
        "checkpoint": {
            "state": state,
            "active_job_id": None,
            "job_ids": [],
            "blocked_task_refs": [],
            "round_id": rid,
        },
    }
    if meta:
        round_meta.update(meta)
    return {
        "ok": ok,
        "error": error,
        "round_id": rid,
        "record": new,
        "round": round_meta,
    }


def wait_for(predicate, timeout: float = 60.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


# --- start_supervisor -------------------------------------------------------


@pytest.mark.parametrize("state", ["failed", "running", "quota", "attached", "completed"])
def test_a_supervisor_never_restarts_failed_or_uncertain_work(tmp_path, monkeypatch, state):
    root, record, _coord = prepared(tmp_path, state=state, last_error="held for review")
    spawned = []
    monkeypatch.setattr(
        central_supervisor.subprocess, "Popen",
        lambda *args, **kwargs: spawned.append((args, kwargs)),
    )

    outcome = start_supervisor(record, root)

    assert outcome["ok"] is False
    assert "explicit recovery" in outcome["error"]
    assert spawned == []


def test_start_supervisor_bootstraps_a_detached_hidden_interpreter(tmp_path, monkeypatch):
    root, record, _coord = prepared(tmp_path)
    calls = []

    def fake_popen(argv, **kwargs):
        calls.append({"argv": list(argv), "kwargs": kwargs})
        return SimpleNamespace(pid=4321)

    monkeypatch.setattr(central_supervisor.subprocess, "Popen", fake_popen)

    outcome = start_supervisor(record, root)

    assert outcome["ok"] is True, outcome["error"]
    assert outcome["pid"] == 4321
    assert len(calls) == 1
    argv = calls[0]["argv"]
    kwargs = calls[0]["kwargs"]
    assert argv[0] == sys.executable
    assert argv[1] == "-I"
    assert argv[2] == "-c"
    assert "supervisor_main" in argv[3]
    assert argv[4:6] == ["--central-dir", str(root)]
    assert argv[6:8] == ["--coordinator", record["id"]]
    assert Path(kwargs["cwd"]) == Path(record["worktree"])
    assert kwargs["env"]["PYTHONPATH"].split(os.pathsep)[0] == str(
        Path(central_supervisor.__file__).resolve().parent.parent
    )
    assert kwargs["env"]["AGENT_CREW_CHIEF_CENTRAL_DIR"] == str(root)
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["stdout"] == subprocess.DEVNULL
    assert kwargs["stderr"] == subprocess.DEVNULL
    if os.name == "nt":
        assert kwargs["creationflags"] & 0x08000000  # CREATE_NO_WINDOW
        assert kwargs["startupinfo"].dwFlags & 0x00000001  # STARTF_USESHOWWINDOW
        assert kwargs["startupinfo"].wShowWindow == 0  # SW_HIDE
    else:
        assert kwargs["start_new_session"] is True


# --- the supervised loop ----------------------------------------------------


def test_rounds_reuse_the_persisted_session_and_apply_the_resolved_timeout(tmp_path):
    root, record, coord = prepared(
        tmp_path,
        authorization={
            "mode": "raw-request",
            "authorized_at": None,
            "settings": {
                "coordinator_unrestricted": True,
                "central_poll_s": 0.25,
                "central_round_timeout_s": 12.5,
            },
        },
    )
    final_id = str(uuid.uuid4())
    runner = FakeRunner([
        lambda rec: round_result(rec, state="running", session_id="sess-1"),
        lambda rec: round_result(
            rec, state="completed", session_id="sess-1", round_id=final_id
        ),
    ])

    supervise(root, record["id"], FakeClock(), runner)

    assert len(runner.calls) == 2
    assert runner.calls[0]["record"]["session_id"] is None
    assert runner.calls[1]["record"]["session_id"] == "sess-1"
    assert runner.calls[0]["timeout_s"] == 12.5
    assert stored(root, record)["state"] == "completed"
    state = supervisor_file(coord)
    assert state["schema_version"] == 1
    assert state["coordinator_id"] == record["id"]
    assert state["pid"] == os.getpid()
    assert state["state"] == "completed"
    assert state["nonce"]
    assert state["heartbeat_at"] >= state["started_at"]
    assert [event["state"] for event in state["events"]][:2] == ["starting", "running"]
    assert state["round_id"] == final_id
    assert not (coord / "supervisor.lock").exists()


def test_a_heartbeat_is_persisted_while_a_round_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(central_supervisor, "HEARTBEAT_S", 0.02)
    root, record, coord = prepared(tmp_path)
    path = coord / "supervisor.json"
    seen = []

    def runner(rec, timeout_s):
        first = supervisor_file(coord)["heartbeat_at"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = supervisor_file(coord)
            if current["heartbeat_at"] != first:
                seen.append((first, current["heartbeat_at"]))
                break
            time.sleep(0.01)
        return round_result(rec, state="completed", session_id="sess-1")

    supervise(root, record["id"], None, runner)

    assert seen and seen[0][1] > seen[0][0]
    assert path.exists()


def test_idle_polling_makes_no_model_calls_and_stops_only_on_request(tmp_path):
    root, record, coord = prepared(tmp_path, state="idle")
    clock = FakeClock()
    runner = FakeRunner([])
    clock.on_sleep = lambda count: send_control(coord, "stop", order=count)

    supervise(root, record["id"], clock, runner)

    assert runner.calls == []
    assert clock.sleeps >= 1
    assert stored(root, record)["state"] == "stopped"
    events = supervisor_file(coord)["events"]
    assert any(event["state"] == "stopped" for event in events)
    assert any(event["state"] == "idle" for event in events)


def test_a_new_message_wakes_an_idle_supervisor(tmp_path):
    root, record, _coord = prepared(tmp_path, state="idle")
    central_messages.send(root, record["id"], {
        "schema_version": 1,
        "id": str(uuid.uuid4()),
        "coordinator_id": record["id"],
        "direction": "in",
        "kind": "message",
        "created_at": "2026-01-01T00:00:00+00:00",
        "reply_to": None,
        "task_refs": [],
        "body": "check the build",
    })
    runner = FakeRunner([lambda rec: round_result(rec, state="completed", session_id="s1")])

    supervise(root, record["id"], FakeClock(), runner)

    assert len(runner.calls) == 1


def test_a_failed_round_is_held_without_a_retry(tmp_path):
    root, record, coord = prepared(tmp_path)
    runner = FakeRunner([
        lambda rec: round_result(rec, ok=False, error="coordinator CLI exited 3", state="failed"),
    ])

    supervise(root, record["id"], FakeClock(), runner)

    assert len(runner.calls) == 1
    saved = stored(root, record)
    assert saved["state"] == "failed"
    assert saved["last_error"] == "coordinator CLI exited 3"
    assert supervisor_file(coord)["state"] == "failed"
    assert supervisor_file(coord)["error"] == "coordinator CLI exited 3"


def test_two_identical_running_rounds_pause_instead_of_looping(tmp_path):
    root, record, coord = prepared(tmp_path)

    def same(rec):
        return round_result(
            rec,
            state="running",
            session_id="sess-1",
            meta={
                "checkpoint": {
                    "state": "running",
                    "active_job_id": "job-1",
                    "job_ids": ["job-1"],
                    "blocked_task_refs": [],
                    "round_id": str(uuid.uuid4()),
                }
            },
        )

    clock = FakeClock()
    runner = FakeRunner([same, same])
    clock.on_sleep = lambda count: send_control(coord, "stop", order=count)

    supervise(root, record["id"], clock, runner)

    assert len(runner.calls) == 2
    events = supervisor_file(coord)["events"]
    paused = [event for event in events if event["state"] == "paused"]
    assert paused and "no progress" in (paused[0]["note"] or "")
    assert stored(root, record)["state"] == "stopped"


def test_a_productive_job_keeps_the_supervisor_running(tmp_path):
    """Two rounds on the same job that really progressed must not pause.

    Both rounds report the same job ids: the progress is in the job record,
    where a piece was accepted between them.
    """
    job_id = "20260101-0000-demo"
    root, record, coord = prepared(tmp_path, job_ids=[job_id])
    jobs = Path(record["worktree"]) / "docs" / "crew-chief" / "jobs"
    jobs.mkdir(parents=True)
    job = {
        "id": job_id,
        "title": "demo",
        "request": "do the work",
        "created": "2026-01-01T00:00:00+00:00",
        "distributed": True,
        "reason": "central",
        "issue_ref": "",
        "pieces": [{
            "n": 1,
            "title": "piece one",
            "type": "code",
            "risk": "low",
            "attempts": [],
            "corrections": [],
            "escalations": [],
            "grades": {},
        }],
    }
    path = jobs / f"{job_id}.json"
    path.write_text(json.dumps(job), encoding="utf-8")
    checkpoint = {
        "state": "running",
        "active_job_id": job_id,
        "job_ids": [job_id],
        "blocked_task_refs": [],
        "round_id": str(uuid.uuid4()),
    }

    def first(rec):
        return round_result(rec, state="running", session_id="sess-1",
                            meta={"checkpoint": dict(checkpoint)})

    def second(rec):
        piece = job["pieces"][0]
        piece["attempts"] = [
            {"n": 1, "role": "exec", "status": "ok", "branch": "crew/demo"}
        ]
        piece["accepted"] = {
            "attempt": 1,
            "branch": "crew/demo",
            "at": "2026-01-01T00:01:00+00:00",
        }
        path.write_text(json.dumps(job), encoding="utf-8")
        return round_result(rec, state="running", session_id="sess-1",
                            meta={"checkpoint": dict(checkpoint)})

    def third(rec):
        return round_result(rec, state="completed", session_id="sess-1")

    clock = FakeClock()
    clock.on_sleep = lambda count: send_control(coord, "stop", order=count)
    runner = FakeRunner([first, second, third])

    supervise(root, record["id"], clock, runner)

    assert len(runner.calls) == 3
    events = supervisor_file(coord)["events"]
    assert not [event for event in events if event["state"] == "paused"]
    assert stored(root, record)["state"] == "completed"


def test_pause_waits_for_the_round_then_resume_continues_the_session(tmp_path):
    root, record, coord = prepared(tmp_path)

    def first(rec):
        send_control(coord, "pause", order=1)
        return round_result(rec, state="running", session_id="sess-1")

    def second(rec):
        return round_result(rec, state="completed", session_id="sess-1")

    clock = FakeClock()
    clock.on_sleep = lambda count: send_control(coord, "resume", order=2)
    runner = FakeRunner([first, second])

    supervise(root, record["id"], clock, runner)

    assert len(runner.calls) == 2
    assert runner.calls[1]["record"]["session_id"] == "sess-1"
    states = [event["state"] for event in supervisor_file(coord)["events"]]
    assert "paused" in states and "resumed" not in states
    assert stored(root, record)["state"] == "completed"
    assert clock.sleeps == 1


def test_a_persisted_pid_is_never_signalled_and_a_paused_checkpoint_is_reconciled(
    tmp_path, monkeypatch
):
    root, record, coord = prepared(tmp_path, state="paused")
    rounds = coord / "rounds"
    rounds.mkdir(exist_ok=True)
    (rounds / f"{uuid.uuid4()}.json").write_text(
        json.dumps({"checkpoint": {"state": "running", "job_ids": ["job-1"]}}),
        encoding="utf-8",
    )
    (coord / "supervisor.json").write_text(
        json.dumps({"schema_version": 1, "pid": 999999, "nonce": "stale"}),
        encoding="utf-8",
    )
    signalled = []
    monkeypatch.setattr(
        central_supervisor.os, "kill",
        lambda pid, sig: signalled.append(("kill", pid, sig)), raising=False,
    )
    monkeypatch.setattr(
        central_supervisor.subprocess, "run",
        lambda argv, **kwargs: signalled.append(("run", argv)),
    )

    def resumed(rec):
        send_control(coord, "resume", order=1)
        return round_result(rec, state="completed", session_id="sess-1")

    clock = FakeClock()
    clock.on_sleep = lambda count: send_control(coord, "resume", order=1)
    runner = FakeRunner([resumed])

    supervise(root, record["id"], clock, runner)

    assert len(runner.calls) == 1
    assert signalled == []
    events = supervisor_file(coord)["events"]
    assert any("999999" in (event["note"] or "") for event in events)


def test_hostile_or_unreadable_controls_are_reported_not_applied(tmp_path):
    root, record, coord = prepared(tmp_path, state="idle")
    control = coord / "control"
    control.mkdir(exist_ok=True)
    (control / "not-a-uuid.json").write_text("{}", encoding="utf-8")
    unknown = str(uuid.uuid4())
    (control / f"{unknown}.json").write_text(
        json.dumps({"id": unknown, "command": "detonate"}), encoding="utf-8"
    )
    oversized = str(uuid.uuid4())
    (control / f"{oversized}.json").write_bytes(b"x" * (1024 * 1024 + 10))
    clock = FakeClock()
    clock.on_sleep = lambda count: send_control(coord, "stop", order=count)

    supervise(root, record["id"], clock, FakeRunner([]))

    assert stored(root, record)["state"] == "stopped"
    notes = [event["note"] or "" for event in supervisor_file(coord)["events"]]
    assert any("not-a-uuid.json" in note for note in notes)
    assert any("detonate" in note for note in notes)
    assert any(oversized in note for note in notes)


def test_log_retention_keeps_twenty_rounds_and_preserves_metadata(tmp_path, monkeypatch):
    assert central_supervisor.LOG_MAX_BYTES == 10 * 1024 * 1024
    assert central_supervisor.LOG_RETENTION_ROUNDS == 20
    monkeypatch.setattr(central_supervisor, "LOG_RETENTION_ROUNDS", 2)
    root, record, coord = prepared(tmp_path)
    rounds = coord / "rounds"
    logs = coord / "logs"
    rounds.mkdir(exist_ok=True)
    logs.mkdir(exist_ok=True)
    old = []
    for index in range(4):
        round_id = str(uuid.uuid4())
        old.append(round_id)
        (logs / f"{round_id}.stdout.log").write_text(f"round {index}", encoding="utf-8")
        (logs / f"{round_id}.stderr.log").write_text("", encoding="utf-8")
        meta = {
            "schema_version": 1,
            "id": round_id,
            "coordinator_id": record["id"],
            "state": "completed",
            "usage": {"tokens_total": index},
            "consumed_message_ids": [],
            "logs": {
                "stdout": f"logs/{round_id}.stdout.log",
                "stderr": f"logs/{round_id}.stderr.log",
            },
        }
        path = rounds / f"{round_id}.json"
        path.write_text(json.dumps(meta), encoding="utf-8")
        stamp = 1_700_000_000 + index
        os.utime(path, (stamp, stamp))
    runner = FakeRunner([lambda rec: round_result(rec, state="completed", session_id="s1")])

    supervise(root, record["id"], FakeClock(), runner)

    kept, pruned = old[2:], old[:2]
    for round_id in kept:
        assert (logs / f"{round_id}.stdout.log").exists()
        assert json.loads((rounds / f"{round_id}.json").read_text(encoding="utf-8"))["logs"]
    for round_id in pruned:
        assert not (logs / f"{round_id}.stdout.log").exists()
        assert not (logs / f"{round_id}.stderr.log").exists()
        meta = json.loads((rounds / f"{round_id}.json").read_text(encoding="utf-8"))
        assert meta["logs"] == {"stdout": None, "stderr": None}
        assert meta["logs_pruned"] is True
        assert meta["usage"] == {"tokens_total": old.index(round_id)}
        assert meta["state"] == "completed"
        assert meta["consumed_message_ids"] == []


def test_the_usage_backfill_never_reorders_the_round_history(tmp_path, monkeypatch):
    """The usage backfill rewrites the round metadata; the order must survive it.

    The round ids sort in the reverse of their creation order, so a rewrite
    that reorders the rounds by name cannot pass by luck.
    """
    monkeypatch.setattr(central_supervisor, "LOG_RETENTION_ROUNDS", 2)
    root, record, coord = prepared(tmp_path)
    rounds = coord / "rounds"
    logs = coord / "logs"
    rounds.mkdir(exist_ok=True)
    logs.mkdir(exist_ok=True)
    old = []
    for index in range(4):
        round_id = str(uuid.UUID(int=3 - index))
        old.append(round_id)
        (logs / f"{round_id}.stdout.log").write_text(f"round {index}", encoding="utf-8")
        meta = {
            "schema_version": 1,
            "id": round_id,
            "coordinator_id": record["id"],
            "state": "completed",
            "usage": {"tokens_total": index},
            "consumed_message_ids": [],
            "logs": {"stdout": f"logs/{round_id}.stdout.log"},
        }
        path = rounds / f"{round_id}.json"
        path.write_text(json.dumps(meta), encoding="utf-8")
        stamp = 1_700_000_000 + index
        os.utime(path, (stamp, stamp))
    runner = FakeRunner([lambda rec: round_result(rec, state="completed", session_id="s1")])

    supervise(root, record["id"], FakeClock(), runner)

    for round_id in old[:2]:
        assert not (logs / f"{round_id}.stdout.log").exists()
        meta = json.loads((rounds / f"{round_id}.json").read_text(encoding="utf-8"))
        assert meta["logs"] == {"stdout": None}
    for round_id in old[2:]:
        assert (logs / f"{round_id}.stdout.log").exists()


# --- the real detached bootstrap -------------------------------------------


def test_the_detached_supervisor_runs_a_fake_cli_round_and_consumes_messages(
    tmp_path, fake_bin, monkeypatch
):
    root, record, coord = prepared(tmp_path)
    message_id = str(uuid.uuid4())
    central_messages.send(root, record["id"], {
        "schema_version": 1,
        "id": message_id,
        "coordinator_id": record["id"],
        "direction": "in",
        "kind": "message",
        "created_at": "2026-01-01T00:00:00+00:00",
        "reply_to": None,
        "task_refs": [],
        "body": "check the build",
    })
    capture = tmp_path / "cli-capture.json"
    monkeypatch.setenv("FAKE_CLI_CAPTURE", str(capture))
    fake_bin("claude", FAKE_CLI.replace("__HELP__", repr(CLAUDE_HELP)))

    outcome = start_supervisor(record, root)

    assert outcome["ok"] is True, outcome["error"]
    assert wait_for(lambda: stored(root, record)["state"] == "completed")
    assert wait_for(lambda: not (coord / "supervisor.lock").exists())
    saved = stored(root, record)
    assert saved["session_id"] == "sess-boot"
    assert saved["state"] == "completed"

    round_files = list((coord / "rounds").glob("*.json"))
    assert len(round_files) == 1
    meta = json.loads(round_files[0].read_text(encoding="utf-8"))
    assert meta["state"] == "completed"
    assert meta["session_id"] == "sess-boot"
    assert meta["consumed_message_ids"] == [message_id]
    assert (coord / meta["logs"]["stdout"]).exists()
    assert (coord / "acknowledgments" / f"{message_id}.json").exists()

    seen = json.loads(capture.read_text(encoding="utf-8"))
    assert "--output-format" in seen["argv"]
    assert "stream-json" in seen["argv"]
    assert "CONTEXT_JSON" in seen["prompt"]
    assert "check the build" in seen["prompt"]

    state = supervisor_file(coord)
    assert state["state"] == "completed"
    assert state["pid"] != os.getpid()
    assert state["nonce"]


def test_state_is_rechecked_after_exclusive_lease_acquisition(tmp_path, monkeypatch):
    from contextlib import contextmanager
    root, record, coord = prepared(tmp_path)
    original = central_registry.owner_lease

    @contextmanager
    def raced(*args, **kwargs):
        with original(*args, **kwargs) as lease:
            central_registry.update_record(root, record['id'], {'state': 'completed'})
            yield lease

    monkeypatch.setattr(central_registry, 'owner_lease', raced)
    runner = FakeRunner([lambda rec: round_result(rec, state='completed', session_id='unexpected')])
    with pytest.raises(central_supervisor.SupervisorError, match='recovery|completed'):
        supervise(root, record['id'], FakeClock(), runner)
    assert runner.calls == []
    assert central_registry.load_registry(root)['coordinators'][record['id']]['state'] == 'completed'


def test_bootstrap_uses_isolated_python_even_with_hostile_pythonpath(tmp_path, monkeypatch):
    root, record, coord = prepared(tmp_path)
    monkeypatch.setenv('PYTHONPATH', str(tmp_path / 'untrusted'))
    monkeypatch.setattr(central_supervisor.subprocess, 'Popen', lambda *a, **k: SimpleNamespace(pid=123))
    result = start_supervisor(record, root)
    assert result['ok']
    assert result['argv'][1] == '-I'
