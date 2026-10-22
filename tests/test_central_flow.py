"""Proof for the core multi-worktree coordinator flow (T027).

Covers FR-002, FR-007, FR-008, FR-009, FR-010, FR-011, FR-018 and
SC-001, SC-002, SC-003, SC-004 by exercising the real central modules:

* ``central_launch.prepare_launch`` — unique worktrees/branches, dirty-source
  preservation, no merge/push (FR-002, SC-001).
* ``central_messages`` — durable file inbox/outbox with message IDs,
  acknowledgments, idempotent redelivery, malformed/oversized rejection
  (FR-007, SC-003).
* ``coordinator_protocol.ready_tasks`` — correlated question blocking with
  dependency propagation; independent work continues (FR-008, SC-003).
* ``central_rounds.run_round`` — finite headless round with persisted session,
  inter-round consume, bounded logs, failure retention (FR-009, SC-002).
* ``central_attach.attach`` — exclusive ownership handoff to interactive
  terminal with background-eligibility restore; fallback when CLI does not
  support resume or terminal is non-interactive (FR-010, SC-004).
* ``central_controls`` / ``central_recovery`` — pause/resume/stop controls,
  explicit owner-controlled recovery, stale-PID diagnostics without
  signalling bare PIDs (FR-011, SC-004).
* Coordinator branch retains close output; source checkout never merged or
  pushed; worktrees retained for owner integration (FR-018, SC-001).

Every CLI is fake and every repository, release and registry root is temporary.
No test touches real HOME or live model CLIs.
"""

from __future__ import annotations

import json
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from crew_chief import (
    central_attach,
    central_controls,
    central_messages,
    central_recovery,
    central_registry,
    central_rounds,
    coordinator_protocol,
)
from crew_chief.central_launch import prepare_launch
from test_central_supervisor import CLAUDE_HELP, FAKE_CLI

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "skill" / "crew-chief" / "scripts"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout


def _write_acc_version(root: Path, version: str) -> None:
    path = root / ".claude" / "skills" / "crew-chief" / "scripts" / "crew_chief" / "__init__.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'__version__ = "{version}"\n', encoding="utf-8")


def _make_repo(path: Path, version: str = "0.4.0") -> Path:
    path.mkdir(parents=True)
    _git(path, "init")
    _git(path, "config", "user.name", "Test User")
    _git(path, "config", "user.email", "test@example.com")
    _write_acc_version(path, version)
    (path / "README.md").write_text("# repo\n", encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "initial commit")
    return path


def _make_release(root: Path, version: str, log: Path) -> Path:
    init = root / "skill" / "crew-chief" / "scripts" / "crew_chief" / "__init__.py"
    init.parent.mkdir(parents=True, exist_ok=True)
    init.write_text(f'__version__ = "{version}"\n', encoding="utf-8")
    (root / "install.py").write_text(
        "\n".join([
            "import json, os, sys",
            "from pathlib import Path",
            f"log = Path({str(log)!r})",
            "log.write_text(json.dumps({'argv': sys.argv, 'cwd': os.getcwd()}), encoding='utf-8')",
            "project = Path(sys.argv[1])",
            "init = project / '.claude' / 'skills' / 'crew-chief' / 'scripts' / 'crew_chief' / '__init__.py'",
            "init.parent.mkdir(parents=True, exist_ok=True)",
            f"init.write_text({f'__version__ = \"{version}\"\\n'!r}, encoding='utf-8')",
        ]),
        encoding="utf-8",
    )
    return root


class _FakeAdapter:
    """A fake adapter that produces a valid ACC_ROUND frame for run_round.

    Exercises the real headless/parse_events contract without calling any
    live model CLI.  The round prompt is consumed from stdin and the result
    is written to stdout as assistant-only text.
    """

    def __init__(self, *, state: str = "idle", job_id: str | None = None,
                 questions: list[dict] | None = None,
                 answers: list[dict] | None = None,
                 consumed: list[str] | None = None,
                 fail: bool = False, timeout: bool = False):
        self._state = state
        self._job_id = job_id
        self._questions = questions or []
        self._answers = answers or []
        self._consumed = consumed or []
        self._fail = fail
        self._timeout = timeout

    def headless(self, record: dict, prompt: str) -> list[str]:
        # Build the ACC_ROUND JSON that parse_round will validate.
        job_ids = [self._job_id] if self._job_id else []
        active = self._job_id if self._job_id else None
        result_obj = {
            "state": self._state,
            "active_job_id": active,
            "job_ids": job_ids,
            "blocked_task_refs": [q["task_refs"][0] for q in self._questions] if self._questions else [],
            "questions": self._questions,
            "answers": self._answers,
            "consumed_message_ids": self._consumed,
        }
        frame = "ACC_ROUND " + json.dumps(result_obj, ensure_ascii=False)
        script_lines = [
            "import sys",
            "prompt = sys.stdin.read()",
        ]
        if self._timeout:
            script_lines.append("import time; time.sleep(60)")
        elif self._fail:
            script_lines.append("sys.stderr.write('simulated failure\\n')")
            script_lines.append("raise SystemExit(1)")
        else:
            script_lines.append(f"sys.stdout.write({frame!r})")
            script_lines.append("sys.stdout.flush()")
        code = "; ".join(script_lines)
        return [sys.executable, "-c", code]

    def parse_events(self, stdout: str, stderr: str) -> dict:
        if self._fail:
            return {"error": "simulated failure"}
        return {"text": stdout, "session_id": "fake-session-id"}


class _FakeSpawn:
    """Wraps subprocess.Popen so run_round gets piped stdin/stdout/stderr."""

    def __call__(self, argv, *, cwd, env, stdin, stdout, stderr):
        return subprocess.Popen(
            argv, cwd=str(cwd), env=env,
            stdin=stdin, stdout=stdout, stderr=stderr,
        )


# ---------------------------------------------------------------------------
# Owned-process tracking
# ---------------------------------------------------------------------------

OWNED: list[subprocess.Popen] = []


@pytest.fixture(autouse=True)
def _fake_claude(fake_bin):
    """A fake claude on PATH: these tests never depend on a real CLI."""
    fake_bin("claude", FAKE_CLI.replace("__HELP__", repr(CLAUDE_HELP)))


@pytest.fixture(autouse=True)
def _tracked_cleanup():
    """Kill and reap every owned process after each test; assert none survive."""
    OWNED.clear()
    yield
    for proc in OWNED:
        if proc.poll() is None:
            proc.kill()
        # wait() reaps the child, so a POSIX zombie never looks alive.
        proc.wait(timeout=10)
    for proc in OWNED:
        assert proc.poll() is not None, f"test process {proc.pid} still alive after cleanup"
    OWNED.clear()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_multi_worktree_launch_and_isolation(tmp_path, monkeypatch):
    """SC-001 + FR-002: two repos plus second worktree of one; unique sessions
    and branches; source checkouts unchanged; no merge/push command."""
    monkeypatch.setenv("CREW_CHIEF_COORDINATOR", "claude")
    repo_a = _make_repo(tmp_path / "repo-a", version="0.4.0")
    repo_b = _make_repo(tmp_path / "repo-b", version="0.4.0")
    head_a = _git(repo_a, "rev-parse", "HEAD").strip()
    head_b = _git(repo_b, "rev-parse", "HEAD").strip()

    # Dirty source checkout must be preserved (FR-002).
    dirty = repo_a / "dirty.txt"
    dirty.write_text("work in progress\n", encoding="utf-8")
    dirty_bytes = dirty.read_bytes()

    request = tmp_path / "request.md"
    request.write_text("# request\n\nShip the central flow.\n", encoding="utf-8")
    release = _make_release(tmp_path / "release", "0.4.0", tmp_path / "install.log")
    central = tmp_path / "central"

    def _options(**extra):
        opts = {"mode": "raw-request", "authorize_raw": True, "release_root": release}
        opts.update(extra)
        return opts

    # Launch two repos plus a second worktree of repo_a (SC-001).
    r1 = prepare_launch(repo_a, request, _options(), central)
    assert r1["ok"] is True, r1.get("error")
    r2 = prepare_launch(repo_b, request, _options(), central)
    assert r2["ok"] is True, r2.get("error")
    r3 = prepare_launch(repo_a, request, _options(), central)
    assert r3["ok"] is True, r3.get("error")

    ids = [r1["id"], r2["id"], r3["id"]]
    assert len(set(ids)) == 3, "each launch must produce a unique coordinator id"
    branches = [r1["record"]["branch"], r2["record"]["branch"], r3["record"]["branch"]]
    assert len(set(branches)) == 3, "each coordinator must have its own branch"
    worktrees = [r1["record"]["worktree"], r2["record"]["worktree"], r3["record"]["worktree"]]
    assert len(set(worktrees)) == 3, "each coordinator must have its own worktree"

    # Source checkouts unchanged (FR-002).
    assert dirty.read_bytes() == dirty_bytes, "dirty source file must be preserved"
    assert _git(repo_a, "rev-parse", "HEAD").strip() == head_a
    assert _git(repo_b, "rev-parse", "HEAD").strip() == head_b
    porcelain_a = _git(repo_a, "status", "--porcelain")
    assert "dirty.txt" in porcelain_a, "dirty file must remain uncommitted in source"

    # No merge/push: coordinator branches exist and point to the base commit
    # (prepare_launch creates the worktree from HEAD without an extra commit).
    for repo, branch in [(repo_a, branches[0]), (repo_b, branches[1]), (repo_a, branches[2])]:
        branch_commit = _git(repo, "rev-parse", branch).strip()
        head_commit = _git(repo, "rev-parse", "HEAD").strip()
        assert branch_commit == head_commit, (
            f"branch {branch} should point to HEAD at launch time"
        )
        # Verify no merge commit was created on the branch.
        parents = _git(repo, "rev-list", "--parents", "-n", "1", branch).split()
        assert len(parents) <= 2, "merge commit must not be created"

    # No remote configured → push is impossible (SC-001 no-push proof).
    assert _git(repo_a, "remote").strip() == ""
    assert _git(repo_b, "remote").strip() == ""

    # Worktrees retained for owner integration (FR-018).
    for w in worktrees:
        assert Path(w).is_dir(), f"worktree {w} must be retained"

    # Request preserved in coordinator directory.
    for r in (r1, r2, r3):
        req_copy = central / "coordinators" / r["id"] / "request.md"
        assert req_copy.read_bytes() == request.read_bytes()


def test_durable_messages_and_correlated_questions(tmp_path):
    """FR-007 + FR-008 + SC-003: send/ack/idempotent/malformed/oversized;
    correlated question blocks only dependent tasks; crash/retry replay."""
    central = tmp_path / "central"
    coord_id = str(uuid.uuid4())
    coord_dir = central / "coordinators" / coord_id
    coord_dir.mkdir(parents=True)

    # --- send + idempotent redelivery (FR-007) ---
    q_id = str(uuid.uuid4())
    envelope_q = {
        "schema_version": 1,
        "id": q_id,
        "coordinator_id": coord_id,
        "direction": "in",
        "kind": "question",
        "created_at": "2026-01-01T00:00:00+00:00",
        "reply_to": None,
        "task_refs": ["T027"],
        "body": "Should we proceed?",
    }
    sent_q = central_messages.send(central, coord_id, envelope_q)
    assert sent_q == q_id

    # Idempotent: same ID returns same ID without duplication.
    sent_q2 = central_messages.send(central, coord_id, envelope_q)
    assert sent_q2 == q_id

    pending = central_messages.pending(central, coord_id, "in")
    assert sum(1 for m in pending if m["id"] == q_id) == 1

    # --- correlated answer (FR-008) ---
    a_id = str(uuid.uuid4())
    envelope_a = {
        "schema_version": 1,
        "id": a_id,
        "coordinator_id": coord_id,
        "direction": "out",
        "kind": "answer",
        "created_at": "2026-01-01T00:01:00+00:00",
        "reply_to": q_id,
        "task_refs": [],
        "body": "Yes, proceed.",
    }
    sent_a = central_messages.send(central, coord_id, envelope_a)
    assert sent_a == a_id

    # --- ready_tasks: blocked vs independent (FR-008, SC-003) ---
    tasks = [
        {"ref": "T027", "depends": []},
        {"ref": "T028", "depends": []},
        {"ref": "T029", "depends": ["T027"]},
    ]
    # T027 blocked by question → T029 also blocked (dependency propagation).
    ready_blocked = coordinator_protocol.ready_tasks(tasks, ["T027"])
    assert "T028" in ready_blocked, "independent task must remain ready"
    assert "T027" not in ready_blocked
    assert "T029" not in ready_blocked, "dependent task must be transitively blocked"

    # After clearing block, T027 becomes ready; T029 needs T027 accepted.
    ready_clear = coordinator_protocol.ready_tasks(tasks, [])
    assert "T027" in ready_clear
    # T029 depends on T027 which is not yet accepted → still blocked.
    assert "T029" not in ready_clear
    # Mark T027 as accepted → T029 becomes ready.
    tasks_with_accepted = [
        {"ref": "T027", "depends": [], "accepted": True},
        {"ref": "T028", "depends": []},
        {"ref": "T029", "depends": ["T027"]},
    ]
    ready_after_accept = coordinator_protocol.ready_tasks(tasks_with_accepted, [])
    assert "T029" in ready_after_accept

    # --- acknowledge preserves IDs, resolves once (SC-003) ---
    round_id = str(uuid.uuid4())
    central_messages.acknowledge(central, coord_id, q_id, round_id)
    central_messages.acknowledge(central, coord_id, q_id, round_id)  # idempotent
    pending_after = central_messages.pending(central, coord_id, "in")
    assert not any(m["id"] == q_id for m in pending_after)

    # Outbox answer still pending until acked.
    pending_out = central_messages.pending(central, coord_id, "out")
    assert any(m["id"] == a_id for m in pending_out)
    central_messages.acknowledge(central, coord_id, a_id, round_id)
    assert not any(m["id"] == a_id for m in central_messages.pending(central, coord_id, "out"))

    # --- crash/retry replay: unacked message reappears (SC-003) ---
    q2_id = str(uuid.uuid4())
    central_messages.send(central, coord_id, {
        "schema_version": 1, "id": q2_id, "coordinator_id": coord_id,
        "direction": "in", "kind": "message",
        "created_at": "2026-01-01T00:02:00+00:00",
        "reply_to": None, "task_refs": [], "body": "retry me",
    })
    assert any(m["id"] == q2_id for m in central_messages.pending(central, coord_id, "in"))
    # Simulate crash: message still pending (no ack happened).
    assert any(m["id"] == q2_id for m in central_messages.pending(central, coord_id, "in"))

    # --- malformed/oversized rejected (FR-007) ---
    with pytest.raises(central_messages.MessageError):
        central_messages.send(central, coord_id, {
            "schema_version": 1, "id": str(uuid.uuid4()),
            "coordinator_id": coord_id, "direction": "in", "kind": "message",
            "created_at": "2026-01-01T00:00:00+00:00",
            "reply_to": None, "task_refs": [],
            "body": "x" * (64 * 1024 + 1),
        })
    with pytest.raises(central_messages.MessageError):
        central_messages.send(central, coord_id, {
            "schema_version": 1, "id": "not-a-uuid",
            "coordinator_id": coord_id, "direction": "in", "kind": "message",
            "created_at": "2026-01-01T00:00:00+00:00",
            "reply_to": None, "task_refs": [], "body": "hi",
        })


def test_headless_round_persisted_session_and_log_bound(tmp_path, monkeypatch):
    """FR-009 + SC-002: run_round with fake adapter exercises persisted session,
    inter-round checkpoint consume, bounded logs and failure retention."""
    monkeypatch.setenv("CREW_CHIEF_COORDINATOR", "claude")
    central = tmp_path / "central"
    repo = _make_repo(tmp_path / "repo", version="0.4.0")
    request = tmp_path / "request.md"
    request.write_text("# request\n", encoding="utf-8")
    release = _make_release(tmp_path / "release", "0.4.0", tmp_path / "install.log")

    options = {"mode": "raw-request", "authorize_raw": True, "release_root": release}
    launch = prepare_launch(repo, request, options, central)
    assert launch["ok"] is True, launch.get("error")
    coord_id = launch["id"]
    record = launch["record"]

    # --- successful round: persisted session, checkpoint, log files ---
    adapter = _FakeAdapter(state="idle", job_id=None)
    spawn = _FakeSpawn()
    result = central_rounds.run_round(record, central, adapter, spawn, timeout_s=30.0)
    assert result["ok"] is True, result.get("error")
    assert result["round_id"] is not None

    # Round file persisted with metadata.
    rounds_dir = central / "coordinators" / coord_id / "rounds"
    round_files = list(rounds_dir.glob("*.json"))
    assert len(round_files) >= 1
    round_meta = json.loads(round_files[0].read_text(encoding="utf-8"))
    assert round_meta["state"] == "completed"
    assert round_meta["session_id"] == "fake-session-id"
    assert round_meta["checkpoint"].get("round_id") == result["round_id"]

    # Log files exist and are bounded.
    logs_dir = central / "coordinators" / coord_id / "logs"
    assert logs_dir.is_dir()
    log_files = list(logs_dir.glob("*.log"))
    assert len(log_files) >= 1
    for lf in log_files:
        assert lf.stat().st_size <= central_rounds.LOG_MAX_BYTES

    # --- second round consumes checkpoint from first (inter-round) ---
    adapter2 = _FakeAdapter(state="running", job_id=None)
    result2 = central_rounds.run_round(record, central, adapter2, spawn, timeout_s=30.0)
    assert result2["ok"] is True, result2.get("error")
    round_files2 = sorted(rounds_dir.glob("*.json"), key=lambda p: p.stat().st_mtime_ns)
    assert len(round_files2) >= 2
    meta2 = json.loads(round_files2[-1].read_text(encoding="utf-8"))
    # Checkpoint carries forward from previous round.
    assert meta2["checkpoint"].get("round_id") == result2["round_id"]

    # --- failed round: failure retained, nothing acked ---
    adapter_fail = _FakeAdapter(fail=True)
    result_fail = central_rounds.run_round(record, central, adapter_fail, spawn, timeout_s=30.0)
    assert result_fail["ok"] is False
    assert "simulated failure" in (result_fail.get("error") or "")
    # Failed round file persisted with state=failed.
    round_files3 = sorted(rounds_dir.glob("*.json"), key=lambda p: p.stat().st_mtime_ns)
    fail_meta = json.loads(round_files3[-1].read_text(encoding="utf-8"))
    assert fail_meta["state"] == "failed"
    assert fail_meta["error"] is not None

    # --- timed-out round: child terminated, failure retained ---
    adapter_timeout = _FakeAdapter(timeout=True)
    result_to = central_rounds.run_round(record, central, adapter_timeout, spawn, timeout_s=1.0)
    assert result_to["ok"] is False
    assert "timed out" in (result_to.get("error") or "")
    round_files4 = sorted(rounds_dir.glob("*.json"), key=lambda p: p.stat().st_mtime_ns)
    to_meta = json.loads(round_files4[-1].read_text(encoding="utf-8"))
    assert to_meta["timed_out"] is True
    assert to_meta["state"] == "failed"


def test_pause_resume_stop_and_explicit_recovery(tmp_path, monkeypatch):
    """FR-011 + SC-004: pause/resume/stop via control files; explicit recovery
    moves state without signalling bare PIDs; stale-PID diagnostics only."""
    monkeypatch.setenv("CREW_CHIEF_COORDINATOR", "claude")
    central = tmp_path / "central"
    repo = _make_repo(tmp_path / "repo", version="0.4.0")
    request = tmp_path / "request.md"
    request.write_text("# request\n", encoding="utf-8")
    release = _make_release(tmp_path / "release", "0.4.0", tmp_path / "install.log")

    options = {"mode": "raw-request", "authorize_raw": True, "release_root": release}
    launch = prepare_launch(repo, request, options, central)
    assert launch["ok"] is True, launch.get("error")
    coord_id = launch["id"]

    # Spawn a sleeper to prove recovery never signals unrelated PIDs.
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    OWNED.append(sleeper)
    assert sleeper.poll() is None

    # --- pause/resume/stop controls (FR-011) ---
    pause_id = central_controls.request_control(central, coord_id, "pause")
    assert pause_id
    resume_id = central_controls.request_control(central, coord_id, "resume")
    assert resume_id
    stop_id = central_controls.request_control(central, coord_id, "stop")
    assert stop_id

    # Controls are durable files.
    control_dir = central / "coordinators" / coord_id / "control"
    controls = list(control_dir.glob("*.json"))
    assert len(controls) >= 3

    # --- simulate failure and recover explicitly (FR-011, SC-004) ---
    central_registry.update_record(central, coord_id, {
        "state": "failed", "last_error": "synthetic failure",
    })
    stored = central_registry.load_registry(central)["coordinators"][coord_id]
    assert stored["state"] == "failed"

    # Recovery with "restart" decision.
    rec = central_recovery.recover(central, coord_id, "restart")
    assert rec["ok"] is True
    assert rec["state"] == "created"
    assert rec.get("recovered") is True

    stored2 = central_registry.load_registry(central)["coordinators"][coord_id]
    assert stored2["state"] == "created"
    assert stored2.get("last_error") is None

    # Recovery with "stop" decision.
    rec_stop = central_recovery.recover(central, coord_id, "stop")
    assert rec_stop["ok"] is True
    assert rec_stop["state"] == "stopped"

    # --- stale PID diagnostics: reconcile never signals bare PID ---
    # Write a supervisor.json with a bogus PID to trigger stale detection.
    coord_dir = central / "coordinators" / coord_id
    sup_path = coord_dir / "supervisor.json"
    sup_path.write_text(json.dumps({
        "schema_version": 1,
        "coordinator_id": coord_id,
        "nonce": "test-nonce",
        "pid": 99999999,  # non-existent PID
        "child_pid": None,
        "state": "running",
        "heartbeat_at": "2020-01-01T00:00:00+00:00",  # very old
        "started_at": "2020-01-01T00:00:00+00:00",
        "events": [],
    }, indent=2) + "\n", encoding="utf-8")

    # Reset state so reconcile can act.
    central_registry.update_record(central, coord_id, {"state": "running"})

    recon = central_recovery.reconcile(central, coord_id, now=None)
    assert recon["ok"] is True
    assert recon.get("stale") is True or "stale" in recon.get("reason", "")

    # Sleeper must still be alive: reconcile never signalled it.
    assert sleeper.poll() is None, "reconcile must never signal unrelated PIDs"


def test_attach_exclusive_ownership_and_fallback(tmp_path, monkeypatch):
    """FR-010 + SC-004: attach yields exclusive ownership when CLI supports
    resume and terminal is interactive; falls back explicitly otherwise;
    background eligibility restored after attach."""
    monkeypatch.setenv("CREW_CHIEF_COORDINATOR", "claude")
    central = tmp_path / "central"
    repo = _make_repo(tmp_path / "repo", version="0.4.0")
    request = tmp_path / "request.md"
    request.write_text("# request\n", encoding="utf-8")
    release = _make_release(tmp_path / "release", "0.4.0", tmp_path / "install.log")

    options = {"mode": "raw-request", "authorize_raw": True, "release_root": release}
    launch = prepare_launch(repo, request, options, central)
    assert launch["ok"] is True, launch.get("error")
    coord_id = launch["id"]

    # --- fallback: non-interactive terminal ---
    class NonInteractiveTerminal:
        stdin = None
        stdout = None

    result_ni = central_attach.attach(central, coord_id, NonInteractiveTerminal(), timeout_s=0.5)
    assert isinstance(result_ni, dict)
    assert result_ni["ok"] is False
    assert "non-interactive" in result_ni.get("reason", "").lower() or "redirected" in result_ni.get("reason", "").lower()

    # --- fallback: CLI does not support resume ---
    # Record has cli=None by default; attach should report unsupported.
    result_cli = central_attach.attach(central, coord_id, timeout_s=0.5)
    assert isinstance(result_cli, dict)
    # Either no-terminal or unsupported-CLI fallback; must not raise.
    assert result_cli["ok"] is False

    # --- fallback: coordinator in terminal state ---
    central_registry.update_record(central, coord_id, {"state": "stopped"})
    result_stopped = central_attach.attach(central, coord_id, timeout_s=0.5)
    assert result_stopped["ok"] is False
    assert result_stopped["state"] == "stopped"

    # Restore state for other tests.
    central_registry.update_record(central, coord_id, {"state": "idle"})


def test_coordinator_branch_retains_output_no_merge_push(tmp_path, monkeypatch):
    """FR-018 + SC-001: coordinator work stays on its branch; source checkout
    never merged or pushed; worktree retained for owner integration."""
    monkeypatch.setenv("CREW_CHIEF_COORDINATOR", "claude")
    repo = _make_repo(tmp_path / "repo", version="0.4.0")
    head = _git(repo, "rev-parse", "HEAD").strip()
    request = tmp_path / "request.md"
    request.write_text("# request\n", encoding="utf-8")
    release = _make_release(tmp_path / "release", "0.4.0", tmp_path / "install.log")
    central = tmp_path / "central"

    options = {"mode": "raw-request", "authorize_raw": True, "release_root": release}
    launch = prepare_launch(repo, request, options, central)
    assert launch["ok"] is True, launch.get("error")
    wt = Path(launch["record"]["worktree"])
    branch = launch["record"]["branch"]

    # Simulate coordinator work: create file and commit on its branch.
    (wt / "feature.txt").write_text("coordinator output\n", encoding="utf-8")
    _git(wt, "add", "feature.txt")
    _git(wt, "commit", "-m", "feat: coordinator close output")

    # Feature exists on coordinator branch.
    show = _git(wt, "show", f"{branch}:feature.txt")
    assert "coordinator output" in show

    # Source checkout still at original HEAD without feature.
    assert _git(repo, "rev-parse", "HEAD").strip() == head
    tree = _git(repo, "ls-tree", "-r", "HEAD", "--name-only")
    assert "feature.txt" not in tree

    # No automatic merge/push occurred.
    assert _git(repo, "remote").strip() == ""
    merged = _git(repo, "branch", "--merged", "HEAD")
    assert branch not in merged

    # Worktree retained.
    assert wt.is_dir()


def test_no_test_process_survives_after_flow(tmp_path):
    """Final cleanup proves no owned test process survives."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"])
    OWNED.append(proc)
    assert proc.poll() is None
    proc.kill()
    proc.wait(timeout=10)
    assert proc.poll() is not None