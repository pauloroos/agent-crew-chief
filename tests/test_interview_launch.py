"""Tests for interview completion queuing and launching detached (T046, US1, US2, FR-009, FR-013).

Verifies:
- While interview questions are pending, nothing is launched (state remains interview).
- When the interview completes (via start_job with all answers or last answer),
  the job moves to queued (never ready) with a lifecycle event, and is launched
  detached through the existing canonical launch path.
- State transitions queued -> running when launch succeeds, recording coordinator_id
  and supervisor reference in the job and events.
- Request file is written inside the project's records area (docs/crew-chief/jobs), not /tmp.
- Launch arguments properly map coordinator (cli:model), executors mode, and end_mode.
- Launch failure transitions the job to failed with an event and sends a webhook notice
  when notify_url is configured.
"""

from __future__ import annotations

import hashlib
import hmac
import http.server
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any
import pytest

from crew_chief import mcp_tools, records
from crew_chief.webhook import _compute_signature

RELEASE_VERSION = "0.4.0"

CLAUDE_HELP = """Usage: claude [options] [command] [prompt]

Options:
  -p, --print                       Print response and exit
  --output-format <format>          Output format (text, json, stream-json)
  --verbose                         Verbose output
  -r, --resume [sessionId]          Resume a conversation
  --permission-mode <mode>          Permission mode (choices: "default", "dontAsk", "plan")
  --dangerously-skip-permissions    Bypass all permission checks
"""


def _git(repo: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return res.stdout


def _setup_git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init")
    _git(path, "config", "user.name", "Test User")
    _git(path, "config", "user.email", "test@example.com")
    init = path / ".claude" / "skills" / "crew-chief" / "scripts" / "crew_chief" / "__init__.py"
    init.parent.mkdir(parents=True, exist_ok=True)
    init.write_text(f'__version__ = "{RELEASE_VERSION}"\n', encoding="utf-8")
    (path / "README.md").write_text("# Test Repo\n", encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "initial commit")
    return path


class _WebhookHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")
        sig = self.headers.get("X-ACC-Signature", "")
        self.server.received_requests.append({
            "path": self.path,
            "headers": dict(self.headers),
            "body": body,
            "signature": sig,
            "json": json.loads(body) if body else {},
        })
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status": "ok"}')

    def log_message(self, format, *args):
        pass


class _WebhookServer(http.server.HTTPServer):
    def __init__(self, addr):
        super().__init__(addr, _WebhookHandler)
        self.received_requests = []


@pytest.fixture
def webhook_receiver():
    server = _WebhookServer(("127.0.0.1", 0))
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, f"http://127.0.0.1:{port}/webhook"
    server.shutdown()
    server.server_close()


def test_completed_interview_queues_and_launches_detached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, webhook_receiver):
    """Interview completion queues and launches detached with state transitions queued->running."""
    # Ensure Orca env vars are unset
    for var in ("ORCA_CLI_COMMAND", "ORCA_WORKTREE_ID", "ORCA_WORKSPACE_ID"):
        monkeypatch.delenv(var, raising=False)

    fake_bin_dir = tmp_path / "bin"
    fake_bin_dir.mkdir(parents=True, exist_ok=True)
    claude_bin = fake_bin_dir / "claude"
    claude_bin.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"HELP = {CLAUDE_HELP!r}\n"
        "if '--version' in sys.argv[1:]:\n"
        "    print('1.2.3 (Claude Code)')\n"
        "elif '--help' in sys.argv[1:]:\n"
        "    print(HELP)\n"
        "else:\n"
        "    print('mock claude ran')\n"
        "    sys.exit(0)\n",
        encoding="utf-8",
    )
    if os.name == "nt":
        shim = fake_bin_dir / "claude.cmd"
        shim.write_text(
            f'@echo off\r\n"{sys.executable}" "{claude_bin}" %*\r\n',
            encoding="utf-8",
        )
    else:
        claude_bin.chmod(0o755)

    old_path = os.environ.get("PATH", "")
    monkeypatch.setenv("PATH", f"{fake_bin_dir}{os.pathsep}{old_path}")

    # Set up isolated central_dir and projects_dir
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    central_dir = tmp_path / "central"
    central_dir.mkdir()
    monkeypatch.setenv("AGENT_CREW_CHIEF_CENTRAL_DIR", str(central_dir))

    repo = _setup_git_repo(projects_dir / "my-repo")

    # Monkeypatch the lowest process-spawning boundary (Popen in central_supervisor)
    spawned_processes = []
    from crew_chief import central_supervisor

    real_popen = central_supervisor.subprocess.Popen

    class FakeChild:
        def __init__(self, argv, *args, **kwargs):
            self.pid = 98765
            self.argv = argv
            spawned_processes.append(self)

        def poll(self):
            return None

    def fake_popen(*args, **kwargs):
        argv = kwargs.get("args") if "args" in kwargs else (args[0] if args else None)
        if isinstance(argv, (list, tuple)) and any("--coordinator" in str(x) for x in argv):
            return FakeChild(argv, *args, **kwargs)
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(central_supervisor.subprocess, "Popen", fake_popen)

    # 1. Start job with partial answers (questions pending)
    # Must NOT launch detached; state remains interview
    req_text = "Implement detached launcher for completed interview"
    start_res = mcp_tools.start_job(
        project=str(repo),
        request=req_text,
        projects_dir=str(projects_dir),
    )
    assert start_res["ok"] is True
    job_id = start_res["job_id"]
    assert start_res["state"] == "interview"
    assert len(spawned_processes) == 0, "No process must be spawned while questions remain pending"

    job = records.load_job(repo, job_id)
    assert job["state"] == "interview"
    assert "coordinator_id" not in job or job["coordinator_id"] is None
    assert "supervisor" not in job or job["supervisor"] is None

    # Answer intermediate questions
    mcp_tools.answer(
        project=str(repo),
        job_id=job_id,
        key="coordinator",
        value="claude:sonnet-3.5",
        projects_dir=str(projects_dir),
    )
    assert len(spawned_processes) == 0

    mcp_tools.answer(
        project=str(repo),
        job_id=job_id,
        key="executors",
        value="own",
        projects_dir=str(projects_dir),
    )
    assert len(spawned_processes) == 0

    # 2. Answer the final question -> interview completes
    t_before = time.time()
    ans_res = mcp_tools.answer(
        project=str(repo),
        job_id=job_id,
        key="end_mode",
        value="pr_review",
        projects_dir=str(projects_dir),
    )
    t_elapsed = time.time() - t_before
    assert t_elapsed < 10.0, f"MCP answer call took {t_elapsed}s, must return within 10s"

    assert ans_res["ok"] is True
    # The return payload or job state must be running (having passed through queued)
    assert ans_res["state"] in ("queued", "running")

    # Verify process was spawned at lowest boundary
    assert len(spawned_processes) == 1
    spawned = spawned_processes[0]
    assert spawned.pid == 98765

    # Reload job record and verify state, events, request file, and launch args
    updated_job = records.load_job(repo, job_id)
    assert updated_job["state"] == "running"
    assert updated_job.get("status") == "running"
    first_coord_id = updated_job.get("coordinator_id")
    assert first_coord_id is not None
    assert updated_job.get("supervisor") is not None

    # Repeating the final answer must be idempotent: launch exactly once per job,
    # never relaunch or replace the tracked coordinator.
    repeat_ans = mcp_tools.answer(
        project=str(repo),
        job_id=job_id,
        key="end_mode",
        value="pr_review",
        projects_dir=str(projects_dir),
    )
    assert repeat_ans["ok"] is True
    assert repeat_ans["state"] == "running"
    assert len(spawned_processes) == 1, "Repeating answer must NOT spawn another coordinator process"
    job_after_repeat = records.load_job(repo, job_id)
    assert job_after_repeat["state"] == "running"
    assert job_after_repeat.get("coordinator_id") == first_coord_id

    # Verify request file inside project's records area (docs/crew-chief/jobs), not /tmp
    jobs_area = repo / "docs" / "crew-chief" / "jobs"
    req_files = list(jobs_area.glob(f"*{job_id}*request*.md"))
    assert len(req_files) >= 1, f"Expected request file in {jobs_area}, found none"
    req_file = req_files[0]
    assert str(req_file).startswith(str(jobs_area))
    assert req_file.read_text(encoding="utf-8").strip() == req_text.strip()

    # Verify launch args (cli/model, executors, end_mode)
    launch_args = updated_job.get("launch_args", {})
    assert launch_args.get("cli") == "claude"
    assert launch_args.get("model") == "sonnet-3.5"
    assert launch_args.get("executors") == "own"
    assert launch_args.get("end_mode") == "pr_review"

    # Verify lifecycle events include queued and running (never ready)
    event_types = []
    event_states = []
    for ev in updated_job.get("events", []):
        payload = ev.get("payload") if isinstance(ev, dict) and "payload" in ev else ev
        if isinstance(payload, dict):
            event_types.append(payload.get("type"))
            if "state" in payload:
                event_states.append(payload["state"])

    assert "ready" not in event_states, f"Job must NEVER enter ready state: {event_states}"
    assert "queued" in event_types or "queued" in event_states
    assert "running" in event_types or "running" in event_states

    # 3. Test start_job with every answer present -> completes immediately
    spawned_processes.clear()
    start_complete = mcp_tools.start_job(
        project=str(repo),
        request="Direct complete job",
        coordinator="claude:haiku",
        executors="free_first",
        end_mode="pr",
        projects_dir=str(projects_dir),
    )
    assert start_complete["ok"] is True
    assert start_complete["state"] in ("queued", "running")
    assert len(spawned_processes) == 1

    complete_job = records.load_job(repo, start_complete["job_id"])
    assert complete_job["state"] == "running"
    assert complete_job.get("coordinator_id") is not None

    # 4. Launch failure case -> transitions to failed with event and notice
    server, webhook_url = webhook_receiver
    secret_file = tmp_path / "notify_secret"
    secret_file.write_text("topsecretkey123", encoding="utf-8")
    secret_file.chmod(0o600)

    # Configure notification prefs in repo
    prefs_file = repo / "docs" / "crew-chief" / "preferences.toml"
    prefs_file.write_text(
        f'notify_url = "{webhook_url}"\n'
        f'notify_secret_file = "{secret_file.as_posix()}"\n'
        f'notify_allow_http = true\n',
        encoding="utf-8",
    )

    # Make launch fail by raising during backend start / supervisor start
    def fail_popen(*args, **kwargs):
        argv = kwargs.get("args") if "args" in kwargs else (args[0] if args else None)
        if isinstance(argv, (list, tuple)) and any("--coordinator" in str(x) for x in argv):
            raise OSError("Simulated process spawn failure")
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(central_supervisor.subprocess, "Popen", fail_popen)

    fail_job_res = mcp_tools.start_job(
        project=str(repo),
        request="Job that will fail to launch",
        coordinator="claude:haiku",
        executors="own",
        end_mode="pr",
        projects_dir=str(projects_dir),
    )
    assert fail_job_res["ok"] is True or fail_job_res.get("isError") is True
    failed_job_id = fail_job_res["job_id"]

    failed_job = records.load_job(repo, failed_job_id)
    assert failed_job["state"] == "failed"
    assert failed_job.get("status") == "failed"

    failed_event_types = [
        ev.get("payload", {}).get("type") if isinstance(ev.get("payload"), dict) else ev.get("type")
        for ev in failed_job.get("events", [])
    ]
    assert "failed" in failed_event_types

    # Wait for webhook delivery to loopback receiver
    deadline = time.time() + 5.0
    while not server.received_requests and time.time() < deadline:
        time.sleep(0.05)

    assert len(server.received_requests) >= 1
    req = server.received_requests[0]
    assert req["json"]["job_id"] == failed_job_id
    assert req["json"]["status"] == "failed"
    expected_sig = _compute_signature(b"topsecretkey123", req["body"])
    assert req["signature"] == expected_sig


def test_stop_job_stops_launched_coordinator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """mcp_tools.stop(job_id=...) stops launched coordinator via canonical control path and marks job stopped."""
    for var in ("ORCA_CLI_COMMAND", "ORCA_WORKTREE_ID", "ORCA_WORKSPACE_ID"):
        monkeypatch.delenv(var, raising=False)

    fake_bin_dir = tmp_path / "bin"
    fake_bin_dir.mkdir(parents=True, exist_ok=True)
    claude_bin = fake_bin_dir / "claude"
    claude_bin.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"HELP = {CLAUDE_HELP!r}\n"
        "if '--version' in sys.argv[1:]:\n"
        "    print('1.2.3 (Claude Code)')\n"
        "elif '--help' in sys.argv[1:]:\n"
        "    print(HELP)\n"
        "else:\n"
        "    print('mock claude ran')\n"
        "    sys.exit(0)\n",
        encoding="utf-8",
    )
    if os.name == "nt":
        shim = fake_bin_dir / "claude.cmd"
        shim.write_text(
            f'@echo off\r\n"{sys.executable}" "{claude_bin}" %*\r\n',
            encoding="utf-8",
        )
    else:
        claude_bin.chmod(0o755)

    old_path = os.environ.get("PATH", "")
    monkeypatch.setenv("PATH", f"{fake_bin_dir}{os.pathsep}{old_path}")

    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    central_dir = tmp_path / "central"
    central_dir.mkdir()
    monkeypatch.setenv("AGENT_CREW_CHIEF_CENTRAL_DIR", str(central_dir))

    repo = _setup_git_repo(projects_dir / "my-repo")

    spawned_processes = []
    from crew_chief import central_supervisor

    real_popen = central_supervisor.subprocess.Popen

    class FakeChild:
        def __init__(self, argv, *args, **kwargs):
            self.pid = 98765
            self.argv = argv
            spawned_processes.append(self)

        def poll(self):
            return None

    def fake_popen(*args, **kwargs):
        argv = kwargs.get("args") if "args" in kwargs else (args[0] if args else None)
        if isinstance(argv, (list, tuple)) and any("--coordinator" in str(x) for x in argv):
            return FakeChild(argv, *args, **kwargs)
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(central_supervisor.subprocess, "Popen", fake_popen)

    # 1. Start and launch a job detached
    start_res = mcp_tools.start_job(
        project=str(repo),
        request="Job to test stop control",
        coordinator="claude:sonnet-3.5",
        executors="own",
        end_mode="pr_review",
        projects_dir=str(projects_dir),
    )
    assert start_res["ok"] is True
    job_id = start_res["job_id"]
    job = records.load_job(repo, job_id)
    assert job["state"] == "running"
    coord_id = job.get("coordinator_id")
    assert coord_id is not None
    assert len(spawned_processes) == 1

    # 2. Call mcp_tools.stop(job_id=...)
    t_before = time.time()
    stop_res = mcp_tools.stop(
        job_id=job_id,
        project=str(repo),
        projects_dir=str(projects_dir),
    )
    t_elapsed = time.time() - t_before
    assert t_elapsed < 10.0, f"mcp_tools.stop took {t_elapsed}s, must return within 10s"
    assert stop_res["ok"] is True
    assert stop_res["state"] == "stopped"

    # Assert canonical stop control was invoked for the recorded coordinator_id
    # (control record written)
    control_dir = central_dir / "coordinators" / coord_id / "control"
    control_files = list(control_dir.glob("*.json"))
    assert len(control_files) >= 1, f"Expected control file in {control_dir}, found none"
    ctrl_data = json.loads(control_files[0].read_text(encoding="utf-8"))
    assert ctrl_data.get("command") == "stop"
    assert ctrl_data.get("coordinator_id") == coord_id

    # Assert job state stopped in reloaded record
    updated_job = records.load_job(repo, job_id)
    assert updated_job["state"] == "stopped"
    assert updated_job["status"] == "stopped"

    # Assert exactly one stopped event
    stopped_events = [
        e for e in updated_job.get("events", [])
        if (e.get("payload", {}).get("type") == "stopped" if isinstance(e.get("payload"), dict) else e.get("type") == "stopped")
    ]
    assert len(stopped_events) == 1, f"Expected exactly 1 stopped event, got: {stopped_events}"
    ev_payload = stopped_events[0].get("payload") if isinstance(stopped_events[0].get("payload"), dict) else stopped_events[0]
    assert "control" in ev_payload
    assert ev_payload["control"]["ok"] is True

    # 3. Test coordinator stop failure case: still marked stopped but failure recorded in event/result
    from crew_chief import central_cli
    monkeypatch.setattr(central_cli, "handle_control", lambda *a, **kw: 1)

    start_res2 = mcp_tools.start_job(
        project=str(repo),
        request="Job where coordinator stop fails",
        coordinator="claude:sonnet-3.5",
        executors="own",
        end_mode="pr_review",
        projects_dir=str(projects_dir),
    )
    job_id2 = start_res2["job_id"]
    stop_res2 = mcp_tools.stop(
        job_id=job_id2,
        project=str(repo),
        projects_dir=str(projects_dir),
    )
    assert stop_res2["ok"] is True
    assert stop_res2["state"] == "stopped"

    job2 = records.load_job(repo, job_id2)
    assert job2["state"] == "stopped"
    assert job2["status"] == "stopped"
    stopped_events2 = [
        e for e in job2.get("events", [])
        if (e.get("payload", {}).get("type") == "stopped" if isinstance(e.get("payload"), dict) else e.get("type") == "stopped")
    ]
    assert len(stopped_events2) == 1
    ev2_payload = stopped_events2[0].get("payload") if isinstance(stopped_events2[0].get("payload"), dict) else stopped_events2[0]
    assert "control" in ev2_payload
    assert ev2_payload["control"]["ok"] is False
    assert "error" in ev2_payload

    # 4. Interview-only job (never launched) keeps current behavior
    start_interview = mcp_tools.start_job(
        project=str(repo),
        request="Interview only job",
        projects_dir=str(projects_dir),
    )
    interview_job_id = start_interview["job_id"]
    assert start_interview["state"] == "interview"

    stop_interview = mcp_tools.stop(
        job_id=interview_job_id,
        project=str(repo),
        projects_dir=str(projects_dir),
    )
    assert stop_interview["ok"] is True
    assert stop_interview["state"] == "stopped"
    int_job = records.load_job(repo, interview_job_id)
    assert int_job["state"] == "stopped"

