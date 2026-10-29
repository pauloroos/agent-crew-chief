"""Tests for durable webhook retries surviving process exit and supervisor drain (Known Issue #11)."""

import hashlib
import hmac
import json
import os
import subprocess
import sys
import threading
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, List
import uuid

import pytest

from crew_chief import records, central_registry, central_messages
from crew_chief.central_supervisor import supervise


# ---------------------------------------------------------------------------
# Loopback HTTP receiver
# ---------------------------------------------------------------------------

class _DurableWebhookReceiver(BaseHTTPRequestHandler):
    status_code = 200
    received: List[Dict[str, Any]] = []

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(content_length)
        sig_header = self.headers.get("X-ACC-Signature", "")

        _DurableWebhookReceiver.received.append({
            "headers": dict(self.headers),
            "raw_body": raw_body,
            "signature_header": sig_header,
        })

        self.send_response(self.status_code)
        self.end_headers()
        if self.status_code == 200:
            self.wfile.write(b"ok")
        else:
            self.wfile.write(b"error")

    def log_message(self, format, *args):
        pass


def _start_loopback_receiver(status_code=200):
    _DurableWebhookReceiver.received.clear()
    _DurableWebhookReceiver.status_code = status_code
    server = HTTPServer(("127.0.0.1", 0), _DurableWebhookReceiver)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port


def _verify_hmac(secret: bytes, raw_body: bytes, sig_header: str) -> bool:
    if not sig_header.startswith("sha256="):
        return False
    provided_hex = sig_header.split("=", 1)[1]
    expected = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(provided_hex, expected)


def _make_secret_file(tmp_path: Path) -> Path:
    secret_file = tmp_path / "notify_secret"
    secret_file.write_bytes(b"test-secret-key-for-durable-retries")
    secret_file.chmod(0o600)
    return secret_file


# ---------------------------------------------------------------------------
# Supervisor fakes
# ---------------------------------------------------------------------------

class FakeClock:
    def __init__(self, start=1_700_000_000.0) -> None:
        self.value = start
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
    def __init__(self, results) -> None:
        self.results = list(results)
        self.calls: list[dict] = []

    def __call__(self, record: dict, timeout_s: float) -> dict:
        self.calls.append({"record": dict(record), "timeout_s": timeout_s})
        item = self.results.pop(0)
        return item(record) if callable(item) else dict(item)


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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_pending_notice_survives_process_exit(tmp_path):
    """a) Terminal end notice survives process exit and is delivered by later CLI invocation."""
    server, port = _start_loopback_receiver(status_code=500)
    notify_url = f"http://127.0.0.1:{port}/webhook"
    secret_bytes = b"test-secret-key-for-durable-retries"
    secret_file = _make_secret_file(tmp_path)

    repo_path = tmp_path / "repo"
    repo_path.mkdir(exist_ok=True)

    scripts_dir = Path(__file__).resolve().parents[1] / "skill" / "crew-chief" / "scripts"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(scripts_dir)
    env["ACC_WEBHOOK_BACKOFF"] = "0"

    # Separate subprocess creates job and transitions to terminal state (failed)
    # The receiver answers 500, then the subprocess exits.
    code_transition = f"""
import sys
from pathlib import Path
from crew_chief import records, webhook

project = Path({repr(str(repo_path))})
job = records.new_job(project, title="Durable Notice Job", request="Test durable webhook", distributed=False, reason="test")
job_id = job["id"]

webhook.transition_terminal_state(
    job=job,
    project=project,
    job_id=job_id,
    new_state="failed",
    notify_url={repr(notify_url)},
    notify_secret_file={repr(str(secret_file))},
    notify_allow_http=True,
)
print("CREATED_JOB_ID:" + job_id)
"""
    res1 = subprocess.run(
        [sys.executable, "-c", code_transition],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    job_id = None
    for line in res1.stdout.splitlines():
        if line.startswith("CREATED_JOB_ID:"):
            job_id = line.split(":", 1)[1].strip()
            break
    assert job_id, f"job_id not found in stdout: {res1.stdout}"

    # Receiver got the initial attempt (500)
    assert len(_DurableWebhookReceiver.received) == 1

    # Switch receiver to 200
    _DurableWebhookReceiver.status_code = 200

    # Ensure the pending notice is due (e.g. next_due <= now)
    job_file = repo_path / "docs" / "crew-chief" / "jobs" / f"{job_id}.json"
    assert job_file.exists()
    job_data = json.loads(job_file.read_text(encoding="utf-8"))
    assert "pending_notices" in job_data or "pending_notice" in job_data, "No pending notice persisted in job record"

    # Run later real ACC CLI invocation as another subprocess touching the job: job show
    cli_entry = scripts_dir / "crew-chief.py"
    res2 = subprocess.run(
        [sys.executable, str(cli_entry), "job", "show", "--job", job_id, "--project", str(repo_path)],
        env=env,
        capture_output=True,
        text=True,
    )
    assert res2.returncode == 0, f"job show failed: {res2.stderr}"

    # Assert the receiver got the notice with valid HMAC over the raw body
    assert len(_DurableWebhookReceiver.received) >= 2, "Receiver did not receive drained notice"
    last_receipt = _DurableWebhookReceiver.received[-1]
    assert _verify_hmac(secret_bytes, last_receipt["raw_body"], last_receipt["signature_header"])

    payload = json.loads(last_receipt["raw_body"].decode("utf-8"))
    assert payload["job_id"] == job_id
    assert payload["status"] == "failed"

    # Assert job record shows it delivered
    updated_job = json.loads(job_file.read_text(encoding="utf-8"))
    assert any(d.get("success") is True for d in updated_job.get("webhook_deliveries", [])), "Job record does not show delivery success"


def test_delivered_notice_not_sent_twice(tmp_path):
    """b) A delivered notice is never sent again on subsequent CLI invocations."""
    server, port = _start_loopback_receiver(status_code=500)
    notify_url = f"http://127.0.0.1:{port}/webhook"
    secret_bytes = b"test-secret-key-for-durable-retries"
    secret_file = _make_secret_file(tmp_path)

    repo_path = tmp_path / "repo"
    repo_path.mkdir(exist_ok=True)

    scripts_dir = Path(__file__).resolve().parents[1] / "skill" / "crew-chief" / "scripts"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(scripts_dir)
    env["ACC_WEBHOOK_BACKOFF"] = "0"

    # Run transition with HTTP 500
    code_transition = f"""
import sys
from pathlib import Path
from crew_chief import records, webhook

project = Path({repr(str(repo_path))})
job = records.new_job(project, title="Durable Job", request="test", distributed=False, reason="test")
job_id = job["id"]

webhook.transition_terminal_state(
    job=job,
    project=project,
    job_id=job_id,
    new_state="stopped",
    notify_url={repr(notify_url)},
    notify_secret_file={repr(str(secret_file))},
    notify_allow_http=True,
)
print("CREATED_JOB_ID:" + job_id)
"""
    res1 = subprocess.run([sys.executable, "-c", code_transition], env=env, capture_output=True, text=True, check=True)
    job_id = None
    for line in res1.stdout.splitlines():
        if line.startswith("CREATED_JOB_ID:"):
            job_id = line.split(":", 1)[1].strip()
            break
    assert job_id

    # Switch receiver to 200
    _DurableWebhookReceiver.status_code = 200

    cli_entry = scripts_dir / "crew-chief.py"
    # First CLI invocation drains the notice
    subprocess.run([sys.executable, str(cli_entry), "job", "show", "--job", job_id, "--project", str(repo_path)], env=env, check=True)

    count_after_first = len(_DurableWebhookReceiver.received)
    assert count_after_first >= 2

    # Second CLI invocation touches the job again
    subprocess.run([sys.executable, str(cli_entry), "job", "show", "--job", job_id, "--project", str(repo_path)], env=env, check=True)

    # Must NOT send anything more
    assert len(_DurableWebhookReceiver.received) == count_after_first


def test_supervisor_drains_due_notice(tmp_path):
    """c) The central supervisor loop delivers a due pending notice."""
    server, port = _start_loopback_receiver(status_code=200)
    notify_url = f"http://127.0.0.1:{port}/webhook"
    secret_bytes = b"test-secret-key-for-durable-retries"
    secret_file = _make_secret_file(tmp_path)

    root, record, coord = prepared(tmp_path)
    worktree = Path(record["worktree"])

    job = records.new_job(worktree, title="Supervisor Job", request="test supervisor drain", distributed=False, reason="test")
    job_id = job["id"]
    central_registry.update_record(root, record["id"], {"job_ids": [job_id]})

    # Seed a due pending notice directly into the job record
    job_data = records.load_job(worktree, job_id)
    clock = FakeClock(1_700_000_000.0)
    pending_notice = {
        "id": "notice-sup-test-1",
        "notify_url": notify_url,
        "notify_secret_file": str(secret_file),
        "notify_allow_http": True,
        "payload": {
            "job_id": job_id,
            "status": "failed",
            "pr_url": None,
            "timestamp": "2026-10-28T00:00:00+00:00",
        },
        "attempts": 1,
        "max_attempts": 5,
        "next_due": datetime.fromtimestamp(1_700_000_005.0, tz=timezone.utc).isoformat(),
        "expires_at": datetime.fromtimestamp(1_700_086_400.0, tz=timezone.utc).isoformat(),
        "delivered": False,
    }
    job_data.setdefault("pending_notices", []).append(pending_notice)
    job_data["pending_notice"] = pending_notice
    records.save_job(worktree, job_data)

    # Advance clock past next_due
    clock.value = 1_700_000_020.0

    # Runner returns completed round to finish supervise()
    runner = FakeRunner([
        lambda rec: {
            "ok": True,
            "round_id": "round-sup-1",
            "record": {**rec, "state": "completed"},
            "round": {"session_id": "sess-sup-1"},
        }
    ])

    supervise(root, record["id"], clock, runner)

    # Assert receiver got the notice with valid HMAC
    assert len(_DurableWebhookReceiver.received) >= 1, "Supervisor did not deliver pending notice"
    receipt = _DurableWebhookReceiver.received[-1]
    assert _verify_hmac(secret_bytes, receipt["raw_body"], receipt["signature_header"])
    payload = json.loads(receipt["raw_body"].decode("utf-8"))
    assert payload["job_id"] == job_id
    assert payload["status"] == "failed"

    # Assert job record shows delivery
    updated_job = records.load_job(worktree, job_id)
    assert any(d.get("success") is True for d in updated_job.get("webhook_deliveries", []))
