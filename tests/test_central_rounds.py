"""Proof for one bounded headless coordinator round (T012, FR-009/024/025, US2).

Every child is a real Python subprocess started by an injected fake ``spawn``;
no test probes PATH, runs a model CLI or depends on a supervisor.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

from crew_chief import central_messages, central_registry
from crew_chief.central_rounds import LOG_MAX_BYTES, run_round


# --- fakes ------------------------------------------------------------------

CHILD = r'''
import json, os, sys, time

directive = json.load(open(os.environ["FAKE_DIRECTIVE"], encoding="utf-8"))
capture = {
    "argv": sys.argv[1:],
    "cwd": os.getcwd(),
    "sentinel": os.environ.get("FAKE_SENTINEL"),
    "central_dir": os.environ.get("AGENT_CREW_CHIEF_CENTRAL_DIR"),
    "coordinator": os.environ.get("CREW_CHIEF_COORDINATOR"),
    "stdin": sys.stdin.read() if directive.get("read_stdin", True) else "",
}
with open(os.environ["FAKE_CAPTURE"], "w", encoding="utf-8") as handle:
    json.dump(capture, handle)

out, err = sys.stdout.buffer, sys.stderr.buffer
if directive.get("stdout"):
    out.write(directive["stdout"].encode("utf-8"))
if directive.get("stderr"):
    err.write(directive["stderr"].encode("utf-8"))
fill = directive.get("fill", {})
chunk = b"x" * 65536
for _ in range(fill.get("stdout", 0) // len(chunk)):
    out.write(chunk)
for _ in range(fill.get("stderr", 0) // len(chunk)):
    err.write(chunk)
out.flush()
err.flush()
if directive.get("sleep"):
    time.sleep(directive["sleep"])
sys.exit(int(directive.get("exit", 0)))
'''


class FakeAdapter:
    """Headless argv plus one canned normalized event dict."""

    def __init__(self, events: dict, argv: list[str]) -> None:
        self.events = events
        self.argv = argv
        self.prompts: list[str] = []
        self.records: list[dict] = []

    def headless(self, record: dict, prompt: str) -> list[str]:
        self.records.append(record)
        self.prompts.append(prompt)
        return list(self.argv)

    def parse_events(self, stdout: str, stderr: str) -> dict:
        return dict(self.events)


class FakeSpawn:
    """Start a real, owned child and assert the argv/cwd/env/stdin contract."""

    def __init__(self, tmp_path: Path, directive: dict, worktree: Path,
                 expect_argv: list[str]) -> None:
        self.script = tmp_path / "fake_child.py"
        self.script.write_text(CHILD, encoding="utf-8")
        self.directive = directive
        self.worktree = worktree
        self.expect_argv = expect_argv
        self.calls: list[dict] = []
        self.child: subprocess.Popen | None = None

    def __call__(self, argv, *, cwd, env, stdin=subprocess.PIPE,
                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin_text=None):
        self.calls.append(
            {"argv": list(argv), "cwd": Path(cwd), "env": dict(env), "stdin": stdin_text}
        )
        assert list(argv) == self.expect_argv
        assert Path(cwd) == self.worktree
        assert env["FAKE_SENTINEL"] == "sentinel"
        directive = self.directive
        Path(os.environ["FAKE_DIRECTIVE"]).write_text(
            json.dumps(directive), encoding="utf-8"
        )
        child = subprocess.Popen(
            [sys.executable, str(self.script), *argv],
            cwd=cwd,
            env=env,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            start_new_session=(os.name != "nt"),
        )
        if stdin_text is not None:
            # the retired synchronous contract, kept only while the red proof runs
            try:
                child.stdin.write(stdin_text.encode("utf-8"))
                child.stdin.close()
            except OSError:
                pass
        self.child = child
        return child

    def capture(self) -> dict:
        return json.loads(Path(os.environ["FAKE_CAPTURE"]).read_text(encoding="utf-8"))


class HeldPipeChild:
    """A fake child whose stdout pipe is held open by another writer."""

    def __init__(self) -> None:
        read_fd, write_fd = os.pipe()
        self.stdout = os.fdopen(read_fd, "rb")
        self._held = write_fd
        err_read, err_write = os.pipe()
        os.close(err_write)
        self.stderr = os.fdopen(err_read, "rb")
        in_read, in_write = os.pipe()
        self._in_read = in_read
        self.stdin = os.fdopen(in_write, "wb")
        self.returncode = None
        self.terminated = False
        threading.Thread(target=self._drain_stdin, daemon=True).start()

    def _drain_stdin(self) -> None:
        try:
            with os.fdopen(self._in_read, "rb") as handle:
                while handle.read(4096):
                    pass
        except OSError:
            pass

    def wait(self, timeout=None):
        if self.terminated:
            return self.returncode
        time.sleep(timeout if timeout is not None else 0.1)
        raise subprocess.TimeoutExpired("fake-cli", timeout)

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -9

    def release(self):
        """Close the held write end so the abandoned drain can finish."""
        os.close(self._held)


class FakeChildSpawn:
    """Return one pre-built fake child instead of starting a process."""

    def __init__(self, child) -> None:
        self.child = child
        self.calls: list[dict] = []

    def __call__(self, argv, *, cwd, env, stdin=subprocess.PIPE,
                 stdout=subprocess.PIPE, stderr=subprocess.PIPE):
        self.calls.append({"argv": list(argv), "cwd": Path(cwd), "env": dict(env)})
        return self.child


# --- helpers ----------------------------------------------------------------


def frame(**overrides) -> str:
    value = {
        "state": "idle",
        "active_job_id": None,
        "job_ids": [],
        "blocked_task_refs": [],
        "questions": [],
        "answers": [],
        "consumed_message_ids": [],
    }
    value.update(overrides)
    return "ACC_ROUND " + json.dumps(value)


def events(text: str | None = None, session_id: str | None = "sess-new", **overrides) -> dict:
    value = {
        "session_id": session_id,
        "text": frame() if text is None else text,
        "tokens_in": 11,
        "tokens_out": 7,
        "tokens_total": 18,
        "estimated": False,
        "model": "fake-model",
        "quota_family": None,
        "permission_denied": [],
        "error": None,
    }
    value.update(overrides)
    return value


def record_for(tmp_path: Path, session_id: str | None = None) -> dict:
    worktree = tmp_path / "worktree"
    worktree.mkdir(exist_ok=True)
    stamp = "2026-01-01T00:00:00+00:00"
    return {
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
        "session_id": session_id,
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


def prepared(tmp_path: Path, session_id: str | None = None, **overrides):
    root = tmp_path / "central"
    record = record_for(tmp_path, session_id)
    record.update(overrides)
    central_registry.register(root, record)
    coord = central_messages._coordinator_dir(root, record["id"], create=True)
    (coord / "request.md").write_bytes(
        b"\xef\xbb\xbfRun the bounded task.\r\nKeep it finite.\r\n"
    )
    return root, record, coord


def envelope(record: dict, **overrides) -> dict:
    value = {
        "schema_version": 1,
        "id": str(uuid.uuid4()),
        "coordinator_id": record["id"],
        "direction": "in",
        "kind": "message",
        "created_at": "2026-01-01T00:00:00+00:00",
        "reply_to": None,
        "task_refs": [],
        "body": "check the build",
    }
    value.update(overrides)
    return value


def send_in(root: Path, record: dict, **overrides) -> dict:
    env = envelope(record, **overrides)
    central_messages.send(root, record["id"], env)
    return env


def round_file(coord: Path, round_id: str) -> dict:
    return json.loads(
        (coord / "rounds" / f"{round_id}.json").read_text(encoding="utf-8")
    )


def ack_file(coord: Path, message_id: str) -> Path:
    return coord / "acknowledgments" / f"{message_id}.json"


@pytest.fixture(autouse=True)
def fake_child_env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("FAKE_DIRECTIVE", str(tmp_path / "directive.json"))
    monkeypatch.setenv("FAKE_CAPTURE", str(tmp_path / "capture.json"))
    monkeypatch.setenv("FAKE_SENTINEL", "sentinel")


def redirect(link: Path, target: Path) -> None:
    """A directory junction/symlink, skipped when the platform forbids it."""
    target.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except OSError:
        pass
    if os.name == "nt":
        made = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
        )
        if made.returncode == 0:
            return
    pytest.skip("this platform cannot create a directory link here")


# --- tests ------------------------------------------------------------------


def test_round_runs_child_resumes_captures_and_persists(tmp_path):
    root, record, coord = prepared(tmp_path)
    pending = send_in(root, record)
    owner_question = send_in(root, record, kind="question", body="which task first?")
    question_id = str(uuid.uuid4())
    answer_id = str(uuid.uuid4())
    result = frame(
        state="idle",
        active_job_id="job-1",
        job_ids=["job-1"],
        blocked_task_refs=["T001"],
        consumed_message_ids=[pending["id"]],
        questions=[{"id": question_id, "task_refs": ["T001"], "reason": "confirm scope"}],
        answers=[{"id": answer_id, "reply_to": owner_question["id"], "body": "T001"}],
    )
    adapter = FakeAdapter(events(result, session_id="sess-1"), ["fake-cli", "-p"])
    spawn = FakeSpawn(tmp_path, {"stdout": "{}"}, Path(record["worktree"]), adapter.argv)

    outcome = run_round(record, root, adapter, spawn, timeout_s=30)

    assert outcome["ok"] is True, outcome["error"]
    assert len(spawn.calls) == 1
    assert adapter.records[0]["session_id"] is None
    assert "ACC_ROUND" in adapter.prompts[0]
    assert "Do the thing" not in adapter.prompts[0]
    assert "Run the bounded task." in adapter.prompts[0]
    assert "check the build" in adapter.prompts[0]

    capture = spawn.capture()
    assert os.path.normcase(capture["cwd"]) == os.path.normcase(
        str(Path(record["worktree"]).resolve())
    )
    assert capture["sentinel"] == "sentinel"
    assert capture["argv"] == ["fake-cli", "-p"]
    assert capture["stdin"].startswith("Execute one bounded coordinator round")

    meta = round_file(coord, outcome["round_id"])
    assert meta["schema_version"] == 1
    assert meta["coordinator_id"] == record["id"]
    assert meta["state"] == "completed"
    assert meta["argv"] == ["fake-cli", "-p"]
    assert meta["session_requested"] is None
    assert meta["session_id"] == "sess-1"
    assert meta["usage"]["tokens_in"] == 11
    assert meta["usage"]["tokens_total"] == 18
    assert meta["usage"]["estimated"] is False
    assert meta["active_job_id"] == "job-1"
    assert meta["job_ids"] == ["job-1"]
    assert meta["result"]["consumed_message_ids"] == [pending["id"]]
    assert meta["checkpoint"]["round_id"] == outcome["round_id"]
    assert meta["checkpoint"]["active_job_id"] == "job-1"
    assert meta["logs"]["stdout"] == f"logs/{outcome['round_id']}.stdout.log"
    assert (coord / meta["logs"]["stdout"]).read_text(encoding="utf-8") == "{}"
    assert (coord / meta["logs"]["stderr"]).read_text(encoding="utf-8") == ""
    assert meta["acknowledged_message_ids"] == [pending["id"]]

    ack = json.loads(ack_file(coord, pending["id"]).read_text(encoding="utf-8"))
    assert ack["round_id"] == outcome["round_id"]
    assert not ack_file(coord, owner_question["id"]).exists()

    outbox = {item["kind"]: item for item in central_messages.pending(root, record["id"], "out")}
    assert outbox["question"]["id"] == question_id
    assert outbox["question"]["body"] == "confirm scope"
    assert outbox["question"]["task_refs"] == ["T001"]
    assert outbox["answer"]["id"] == answer_id
    assert outbox["answer"]["reply_to"] == owner_question["id"]
    assert outbox["answer"]["body"] == "T001"

    stored = central_registry.load_registry(root)["coordinators"][record["id"]]
    assert stored["session_id"] == "sess-1"
    assert stored["state"] == "idle"
    assert stored["active_job_id"] == "job-1"
    assert stored["job_ids"] == ["job-1"]
    assert stored["last_error"] is None
    stored_request = (coord / "request.md").read_bytes()
    assert stored_request.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" in stored_request


def test_round_resumes_the_persisted_session_exactly(tmp_path):
    root, record, coord = prepared(tmp_path, session_id="sess-existing")
    argv = ["fake-cli", "-p", "--resume", "sess-existing"]
    adapter = FakeAdapter(events(session_id="sess-existing"), argv)
    spawn = FakeSpawn(tmp_path, {"stdout": "{}"}, Path(record["worktree"]), argv)

    outcome = run_round(record, root, adapter, spawn, timeout_s=30)

    assert outcome["ok"] is True, outcome["error"]
    assert adapter.records[0]["session_id"] == "sess-existing"
    assert spawn.calls[0]["argv"] == argv
    meta = round_file(coord, outcome["round_id"])
    assert meta["session_requested"] == "sess-existing"
    assert meta["session_id"] == "sess-existing"


def test_session_mismatch_fails_without_acknowledging(tmp_path):
    root, record, coord = prepared(tmp_path, session_id="sess-existing")
    pending = send_in(root, record)
    adapter = FakeAdapter(
        events(frame(consumed_message_ids=[pending["id"]]), session_id="sess-other"),
        ["fake-cli", "-p"],
    )
    spawn = FakeSpawn(tmp_path, {"stdout": "{}"}, Path(record["worktree"]), adapter.argv)

    outcome = run_round(record, root, adapter, spawn, timeout_s=30)

    assert outcome["ok"] is False
    assert "session mismatch" in outcome["error"]
    assert not ack_file(coord, pending["id"]).exists()
    meta = round_file(coord, outcome["round_id"])
    assert meta["state"] == "failed"
    assert meta["result"] is None
    assert meta["error"] == outcome["error"]
    stored = central_registry.load_registry(root)["coordinators"][record["id"]]
    assert stored["session_id"] == "sess-existing"
    assert stored["state"] == "failed"


def test_timeout_terminates_the_owned_child_and_keeps_failure(tmp_path):
    root, record, coord = prepared(tmp_path)
    pending = send_in(root, record)
    adapter = FakeAdapter(events(frame(consumed_message_ids=[pending["id"]])), ["fake-cli"])
    spawn = FakeSpawn(
        tmp_path,
        {"stdout": "partial", "sleep": 30},
        Path(record["worktree"]),
        adapter.argv,
    )

    began = time.monotonic()
    outcome = run_round(record, root, adapter, spawn, timeout_s=0.5)
    elapsed = time.monotonic() - began

    assert outcome["ok"] is False
    assert "timed out" in outcome["error"]
    assert elapsed < 20
    assert spawn.child is not None and spawn.child.poll() is not None
    assert not ack_file(coord, pending["id"]).exists()
    meta = round_file(coord, outcome["round_id"])
    assert meta["state"] == "failed"
    assert meta["timed_out"] is True
    assert meta["result"] is None
    assert "partial" in (coord / meta["logs"]["stdout"]).read_text(encoding="utf-8")
    stored = central_registry.load_registry(root)["coordinators"][record["id"]]
    assert stored["state"] == "failed"


def test_giant_output_is_capped_while_both_pipes_drain(tmp_path):
    root, record, coord = prepared(tmp_path)
    argv = ["fake-cli"]

    huge_stderr = FakeSpawn(
        tmp_path,
        {"stdout": frame(session_id="sess-1"), "fill": {"stderr": 11 * 1024 * 1024}},
        Path(record["worktree"]),
        argv,
    )
    adapter = FakeAdapter(events(frame(), session_id="sess-1"), argv)
    began = time.monotonic()
    outcome = run_round(record, root, adapter, huge_stderr, timeout_s=120)
    assert time.monotonic() - began < 60
    assert outcome["ok"] is True, outcome["error"]
    meta = round_file(coord, outcome["round_id"])
    assert meta["truncated"]["stderr"] is True
    assert (coord / meta["logs"]["stderr"]).stat().st_size == LOG_MAX_BYTES

    resumed = dict(record, session_id="sess-1")
    huge_stdout = FakeSpawn(
        tmp_path,
        {"stdout": "", "fill": {"stdout": 11 * 1024 * 1024}},
        Path(record["worktree"]),
        argv,
    )
    adapter = FakeAdapter(events(frame(), session_id="sess-1"), argv)
    outcome = run_round(resumed, root, adapter, huge_stdout, timeout_s=120)
    meta = round_file(coord, outcome["round_id"])
    assert meta["truncated"]["stdout"] is True
    assert (coord / meta["logs"]["stdout"]).stat().st_size == LOG_MAX_BYTES
    assert huge_stdout.child is not None and huge_stdout.child.poll() is not None


def test_nonzero_exit_keeps_bounded_failure_and_never_acks(tmp_path):
    root, record, coord = prepared(tmp_path)
    pending = send_in(root, record)
    text = frame(consumed_message_ids=[pending["id"]])
    adapter = FakeAdapter(events(text, session_id="sess-1"), ["fake-cli"])
    spawn = FakeSpawn(
        tmp_path,
        {"stdout": text, "stderr": "boom: exploded", "exit": 3},
        Path(record["worktree"]),
        adapter.argv,
    )

    outcome = run_round(record, root, adapter, spawn, timeout_s=30)

    assert outcome["ok"] is False
    assert "exited 3" in outcome["error"]
    assert len(outcome["error"]) <= 500
    assert not ack_file(coord, pending["id"]).exists()
    meta = round_file(coord, outcome["round_id"])
    assert meta["returncode"] == 3
    assert meta["result"] is None
    assert "boom" in (coord / meta["logs"]["stderr"]).read_text(encoding="utf-8")
    stored = central_registry.load_registry(root)["coordinators"][record["id"]]
    assert stored["state"] == "failed"
    assert stored["last_error"] == outcome["error"]


def test_unknown_consumed_id_rejects_the_whole_round(tmp_path):
    root, record, coord = prepared(tmp_path)
    pending = send_in(root, record)
    unknown = str(uuid.uuid4())
    adapter = FakeAdapter(
        events(
            frame(consumed_message_ids=[pending["id"], unknown]),
            session_id="sess-1",
        ),
        ["fake-cli"],
    )
    spawn = FakeSpawn(tmp_path, {"stdout": "{}"}, Path(record["worktree"]), adapter.argv)

    outcome = run_round(record, root, adapter, spawn, timeout_s=30)

    assert outcome["ok"] is False
    assert unknown in outcome["error"]
    assert not ack_file(coord, pending["id"]).exists()
    assert not ack_file(coord, unknown).exists()
    meta = round_file(coord, outcome["round_id"])
    assert meta["result"] is None
    assert meta["acknowledged_message_ids"] == []


def test_malformed_result_fails_without_ack(tmp_path):
    root, record, coord = prepared(tmp_path)
    pending = send_in(root, record)
    adapter = FakeAdapter(events("I finished everything!", session_id="sess-1"), ["fake-cli"])
    spawn = FakeSpawn(tmp_path, {"stdout": "{}"}, Path(record["worktree"]), adapter.argv)

    outcome = run_round(record, root, adapter, spawn, timeout_s=30)

    assert outcome["ok"] is False
    assert "ACC_ROUND" in outcome["error"]
    assert not ack_file(coord, pending["id"]).exists()
    meta = round_file(coord, outcome["round_id"])
    assert meta["result"] is None
    assert meta["state"] == "failed"


def test_redirected_rounds_directory_is_refused_before_writing(tmp_path):
    root, record, coord = prepared(tmp_path)
    outside = tmp_path / "outside"
    redirect(coord / "rounds", outside)
    adapter = FakeAdapter(events(), ["fake-cli"])
    spawn = FakeSpawn(tmp_path, {"stdout": "{}"}, Path(record["worktree"]), adapter.argv)

    outcome = run_round(record, root, adapter, spawn, timeout_s=30)

    assert outcome["ok"] is False
    assert "rounds" in outcome["error"]
    assert list(outside.iterdir()) == []
    assert spawn.calls == []


def test_missing_request_is_a_bounded_failure(tmp_path):
    root, record, coord = prepared(tmp_path)
    (coord / "request.md").unlink()
    adapter = FakeAdapter(events(), ["fake-cli"])
    spawn = FakeSpawn(tmp_path, {"stdout": "{}"}, Path(record["worktree"]), adapter.argv)

    outcome = run_round(record, root, adapter, spawn, timeout_s=30)

    assert outcome["ok"] is False
    assert "request" in outcome["error"]
    assert len(outcome["error"]) <= 500
    assert spawn.calls == []
    assert round_file(coord, outcome["round_id"])["state"] == "failed"


def test_a_child_that_never_reads_the_prompt_cannot_hang_the_round(tmp_path):
    root, record, coord = prepared(tmp_path)
    # two large inbox bodies push the prompt past any pipe buffer
    send_in(root, record, body="x" * 50000)
    send_in(root, record, body="y" * 50000)
    adapter = FakeAdapter(events(), ["fake-cli"])
    spawn = FakeSpawn(
        tmp_path,
        {"stdout": "{}", "read_stdin": False, "sleep": 15},
        Path(record["worktree"]),
        adapter.argv,
    )

    began = time.monotonic()
    outcome = run_round(record, root, adapter, spawn, timeout_s=1.0)
    elapsed = time.monotonic() - began

    assert elapsed < 10
    assert outcome["ok"] is False
    assert "timed out" in outcome["error"]
    assert spawn.child is not None and spawn.child.poll() is not None
    meta = round_file(coord, outcome["round_id"])
    assert meta["timed_out"] is True
    assert meta["state"] == "failed"


def test_an_output_pipe_held_open_is_bounded_and_the_owned_child_is_cleaned(tmp_path):
    root, record, coord = prepared(tmp_path)
    adapter = FakeAdapter(events(), ["fake-cli"])
    child = HeldPipeChild()
    spawn = FakeChildSpawn(child)

    began = time.monotonic()
    outcome = run_round(record, root, adapter, spawn, timeout_s=0.5)
    elapsed = time.monotonic() - began

    try:
        assert elapsed < 20
        assert outcome["ok"] is False
        assert "timed out" in outcome["error"]
        assert child.terminated is True
        assert round_file(coord, outcome["round_id"])["timed_out"] is True
    finally:
        child.release()


def test_the_checkpoint_uses_the_newest_round_and_reports_unreadable_metadata(tmp_path):
    root, record, coord = prepared(tmp_path)
    rounds = coord / "rounds"
    rounds.mkdir()
    base = 1_700_000_000
    for index in range(300):
        path = rounds / f"{uuid.uuid4()}.json"
        path.write_text(
            json.dumps({"checkpoint": {"state": "idle", "round_id": f"round-{index}"}}),
            encoding="utf-8",
        )
        os.utime(path, (base + index, base + index))
    malformed = rounds / f"{uuid.uuid4()}.json"
    malformed.write_text("{not json", encoding="utf-8")
    os.utime(malformed, (base + 400, base + 400))
    oversized = rounds / f"{uuid.uuid4()}.json"
    oversized.write_bytes(b"x" * (1024 * 1024 + 10))
    os.utime(oversized, (base + 300, base + 300))
    adapter = FakeAdapter(events(), ["fake-cli"])
    spawn = FakeSpawn(tmp_path, {"stdout": "{}"}, Path(record["worktree"]), adapter.argv)

    outcome = run_round(record, root, adapter, spawn, timeout_s=30)

    assert outcome["ok"] is True, outcome["error"]
    meta = round_file(coord, outcome["round_id"])
    # The checkpoint fed to the prompt is the newest readable predecessor
    # (round-299); the completed round's own checkpoint replaces it afterward.
    assert "round-299" in adapter.prompts[0]
    diagnostics = meta["checkpoint_diagnostics"]
    assert any(malformed.name in item for item in diagnostics)
    assert any(oversized.name in item for item in diagnostics)


def test_a_failed_round_keeps_the_reported_usage_and_quota_family(tmp_path):
    root, record, coord = prepared(tmp_path)
    pending = send_in(root, record)
    adapter = FakeAdapter(
        events(session_id="sess-1", quota_family="claude-pro"), ["fake-cli"]
    )
    spawn = FakeSpawn(
        tmp_path,
        {"stdout": "{}", "stderr": "boom", "exit": 3},
        Path(record["worktree"]),
        adapter.argv,
    )

    outcome = run_round(record, root, adapter, spawn, timeout_s=30)

    assert outcome["ok"] is False
    assert "exited 3" in outcome["error"]
    assert not ack_file(coord, pending["id"]).exists()
    meta = round_file(coord, outcome["round_id"])
    assert meta["state"] == "failed"
    assert meta["usage"]["tokens_in"] == 11
    assert meta["usage"]["tokens_out"] == 7
    assert meta["usage"]["tokens_total"] == 18
    assert meta["usage"]["quota_family"] == "claude-pro"


def test_a_failed_round_keeps_the_starting_job_binding(tmp_path):
    root, record, coord = prepared(tmp_path, job_ids=["job-0"], active_job_id="job-0")
    adapter = FakeAdapter(events(), ["fake-cli"])
    spawn = FakeSpawn(
        tmp_path,
        {"stdout": "{}", "exit": 3},
        Path(record["worktree"]),
        adapter.argv,
    )

    outcome = run_round(record, root, adapter, spawn, timeout_s=30)

    assert outcome["ok"] is False
    meta = round_file(coord, outcome["round_id"])
    assert meta["active_job_id"] == "job-0"
    assert meta["job_ids"] == ["job-0"]


def test_the_child_runs_in_the_injected_root_with_the_selected_coordinator(tmp_path):
    root, record, coord = prepared(tmp_path)
    adapter = FakeAdapter(events(), ["fake-cli"])
    spawn = FakeSpawn(tmp_path, {"stdout": "{}"}, Path(record["worktree"]), adapter.argv)

    outcome = run_round(record, root, adapter, spawn, timeout_s=30)

    assert outcome["ok"] is True, outcome["error"]
    capture = spawn.capture()
    assert capture["central_dir"] == str(root)
    assert capture["coordinator"] == "claude"


def test_the_first_round_captures_the_session_for_the_next_round(tmp_path):
    root, record, coord = prepared(tmp_path)
    adapter = FakeAdapter(events(session_id="sess-first"), ["fake-cli"])
    spawn = FakeSpawn(tmp_path, {"stdout": "{}"}, Path(record["worktree"]), adapter.argv)

    outcome = run_round(record, root, adapter, spawn, timeout_s=30)

    assert outcome["ok"] is True, outcome["error"]
    assert outcome["record"]["session_id"] == "sess-first"

    resumed = FakeAdapter(events(session_id="sess-first"), ["fake-cli", "--resume", "sess-first"])
    again = FakeSpawn(tmp_path, {"stdout": "{}"}, Path(record["worktree"]), resumed.argv)

    second = run_round(outcome["record"], root, resumed, again, timeout_s=30)

    assert second["ok"] is True, second["error"]
    assert resumed.records[0]["session_id"] == "sess-first"
    meta = round_file(coord, second["round_id"])
    assert meta["session_requested"] == "sess-first"
    assert meta["session_id"] == "sess-first"


def test_estimated_usage_is_marked_and_unknown_counts_stay_none(tmp_path):
    root, record, coord = prepared(tmp_path)
    adapter = FakeAdapter(
        events(tokens_in=None, tokens_out=None, tokens_total=None, estimated=True),
        ["fake-cli"],
    )
    spawn = FakeSpawn(tmp_path, {"stdout": "{}"}, Path(record["worktree"]), adapter.argv)

    outcome = run_round(record, root, adapter, spawn, timeout_s=30)

    assert outcome["ok"] is True, outcome["error"]
    usage = round_file(coord, outcome["round_id"])["usage"]
    assert usage["estimated"] is True
    assert usage["tokens_in"] is None
    assert usage["tokens_out"] is None
    assert usage["tokens_total"] is None
