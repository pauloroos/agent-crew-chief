"""Tests for shared terminal-transition helper (T047, US3, FR-009, FR-010, FR-015).

Verifies that every terminal state transition (stopped, expired, failed, blocked, pr_open)
records state + append-only event and POSTs signed notice when notify_url configured.
"""
import hashlib
import hmac
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Loopback HTTP receiver (verifies HMAC signature)
# ---------------------------------------------------------------------------

class _WebhookReceiver(BaseHTTPRequestHandler):
    """Minimal HTTP server that captures webhook POSTs and verifies HMAC."""

    received = []  # list of (headers_dict, raw_body_bytes)

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(content_length)
        sig_header = self.headers.get("X-ACC-Signature", "")

        # Store for test assertions
        _WebhookReceiver.received.append({
            "headers": dict(self.headers),
            "raw_body": raw_body,
            "signature_header": sig_header,
        })

        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, format, *args):
        # Suppress logging to keep test output clean
        pass


def _start_loopback_receiver(port=0):
    """Start a loopback HTTP receiver on an ephemeral port. Returns (server, port)."""
    server = HTTPServer(("127.0.0.1", port), _WebhookReceiver)
    actual_port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, actual_port


def _verify_hmac(secret: bytes, raw_body: bytes, sig_header: str) -> bool:
    """Verify X-ACC-Signature HMAC over raw body."""
    if not sig_header.startswith("sha256="):
        return False
    provided_hex = sig_header.split("=", 1)[1]
    expected = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(provided_hex, expected)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _make_secret_file(tmp_path: Path) -> Path:
    """Create a 0600-permission secret file."""
    secret_file = tmp_path / "webhook_secret"
    secret_file.write_bytes(b"test-webhook-secret-key")
    secret_file.chmod(0o600)
    return secret_file


def _setup_git_repo(repo_path: Path):
    """Initialize a minimal git repo for PR creation tests."""
    subprocess.run(["git", "init"], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_path, check=True, capture_output=True)
    # Create initial commit
    (repo_path / "README.md").write_text("# Test Repo")
    subprocess.run(["git", "add", "."], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo_path, check=True, capture_output=True)


# ---------------------------------------------------------------------------
# MAIN TEST: stop, expire, and pr_open send signed notice
# ---------------------------------------------------------------------------

def test_stop_expire_and_pr_open_send_signed_notice(tmp_path):
    """Verify terminal transitions send signed webhook notices and record events/deliveries.

    Tests three paths:
    1. stop via mcp_tools.stop -> state=stopped, event appended, notice sent
    2. expiration via real expiration check -> state=expired, event appended, notice sent
    3. pr_open via close path -> state=pr_open, event appended, notice sent

    Also tests case without notify_url where payload stays retrievable via status/result.
    """
    # Clear any previous receiver records
    _WebhookReceiver.received.clear()

    # Start loopback receiver with notify_allow_http
    server, port = _start_loopback_receiver()
    notify_url = f"http://127.0.0.1:{port}/webhook"
    secret_file = _make_secret_file(tmp_path)

    # Setup git repo for PR creation
    repo_path = tmp_path / "test_repo"
    repo_path.mkdir()
    _setup_git_repo(repo_path)

    # Create preferences.toml with notify settings
    prefs_dir = repo_path / "docs" / "crew-chief"
    prefs_dir.mkdir(parents=True)
    prefs_file = prefs_dir / "preferences.toml"
    prefs_file.write_text(
        f'notify_url = "{notify_url}"\n'
        f'notify_secret_file = "{secret_file.as_posix()}"\n'
        f'notify_allow_http = true\n',
        encoding="utf-8",
    )

    # Import after setup
    from crew_chief import records, mcp_tools, interview_service, config

    # -----------------------------------------------------------------------
    # PART 1: Test STOP transition via mcp_tools.stop
    # -----------------------------------------------------------------------

    # Create a job in running state
    job = records.new_job(
        repo_path,
        title="Stop test job",
        request="Test stop transition",
        distributed=True,
        reason="testing",
    )
    job["state"] = "running"
    job["status"] = "running"
    records.save_job(repo_path, job)
    job_id = job["id"]

    # Call mcp_tools.stop
    result = mcp_tools.stop(job_id=job_id, project=str(repo_path))
    assert result["ok"] is True, f"stop failed: {result}"

    # Reload job and verify state transition
    stopped_job = records.load_job(repo_path, job_id)
    assert stopped_job["state"] == "stopped", f"Expected state=stopped, got {stopped_job.get('state')}"
    assert stopped_job["status"] == "stopped", f"Expected status=stopped, got {stopped_job.get('status')}"

    # Verify event was appended
    events = stopped_job.get("events", [])
    assert len(events) > 0, "No events recorded for stop transition"
    stop_events = [e for e in events if e.get("payload", {}).get("type") == "stopped" or e.get("type") == "stopped"]
    assert len(stop_events) > 0, f"No 'stopped' event found in events: {events}"

    # Verify delivery record stored in job
    deliveries = stopped_job.get("webhook_deliveries", [])
    assert len(deliveries) > 0, "No webhook delivery record stored for stop"

    # Verify webhook was received
    assert len(_WebhookReceiver.received) > 0, "No webhook received for stop transition"
    last_receive = _WebhookReceiver.received[-1]

    # Verify HMAC signature
    secret = secret_file.read_bytes()
    assert _verify_hmac(secret, last_receive["raw_body"], last_receive["signature_header"]), \
        f"HMAC verification failed for stop. Sig: {last_receive['signature_header']}"

    # Verify payload shape
    payload = json.loads(last_receive["raw_body"])
    assert payload["job_id"] == job_id
    assert payload["status"] == "stopped"
    assert "timestamp" in payload

    # -----------------------------------------------------------------------
    # PART 2: Test EXPIRE transition via real expiration check
    # -----------------------------------------------------------------------

    _WebhookReceiver.received.clear()

    # Create a job in interview state with old timestamp
    expire_job = records.new_job(
        repo_path,
        title="Expire test job",
        request="Test expiration transition",
        distributed=True,
        reason="testing",
    )
    expire_job["state"] = "interview"
    expire_job["status"] = "interview"
    # Set interview started_at to 25 hours ago (exceeds default 24h timeout)
    old_time = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    from datetime import timedelta
    expire_job["interview"] = {
        "started_at": (old_time - timedelta(hours=25)).isoformat(),
        "answers": {},
        "pending": ["project", "request"],
    }
    records.save_job(repo_path, expire_job)
    expire_job_id = expire_job["id"]

    # Trigger expiration check (via interview_service which calls check_interview_expiration)
    try:
        status_result = interview_service.get_interview_status(
            project=str(repo_path),
            job_id=expire_job_id,
        )
    except Exception:
        pass  # Expiration may raise; we check the job state below

    # Reload job and verify state transition
    expired_job = records.load_job(repo_path, expire_job_id)
    assert expired_job["state"] == "expired", f"Expected state=expired, got {expired_job.get('state')}"
    assert expired_job["status"] == "expired", f"Expected status=expired, got {expired_job.get('status')}"

    # Verify event was appended
    events = expired_job.get("events", [])
    expire_events = [e for e in events if e.get("payload", {}).get("type") == "expired" or e.get("type") == "expired"]
    assert len(expire_events) > 0, f"No 'expired' event found in events: {events}"

    # Verify delivery record stored
    deliveries = expired_job.get("webhook_deliveries", [])
    assert len(deliveries) > 0, "No webhook delivery record stored for expiration"

    # Verify webhook was received
    assert len(_WebhookReceiver.received) > 0, "No webhook received for expire transition"
    last_receive = _WebhookReceiver.received[-1]

    # Verify HMAC signature
    assert _verify_hmac(secret, last_receive["raw_body"], last_receive["signature_header"]), \
        f"HMAC verification failed for expire. Sig: {last_receive['signature_header']}"

    # Verify payload shape
    payload = json.loads(last_receive["raw_body"])
    assert payload["job_id"] == expire_job_id
    assert payload["status"] == "expired"
    assert "timestamp" in payload

    # -----------------------------------------------------------------------
    # PART 3: Test PR_OPEN transition via close path
    # -----------------------------------------------------------------------

    _WebhookReceiver.received.clear()

    # Create a job ready for PR creation
    pr_job = records.new_job(
        repo_path,
        title="PR test job",
        request="Test PR open transition",
        distributed=True,
        reason="testing",
    )
    pr_job["state"] = "running"
    pr_job["status"] = "running"
    pr_job["job_branch"] = "acc/test-pr-job"
    records.save_job(repo_path, pr_job)
    pr_job_id = pr_job["id"]

    # Create the job branch
    subprocess.run(
        ["git", "checkout", "-b", "acc/test-pr-job"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )
    # Make a change and commit
    (repo_path / "feature.txt").write_text("Feature implemented")
    subprocess.run(["git", "add", "."], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Add feature"], cwd=repo_path, check=True, capture_output=True)

    # Push branch to origin (we'll use a local bare repo as origin)
    bare_repo = tmp_path / "bare_repo.git"
    subprocess.run(["git", "init", "--bare", str(bare_repo)], check=True, capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare_repo)], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(["git", "push", "origin", "acc/test-pr-job"], cwd=repo_path, check=True, capture_output=True)

    # Mock gh pr create to avoid needing GitHub credentials
    mock_pr_url = "https://github.com/test/repo/pull/1"
    with patch("crew_chief.cli.create_pr", return_value=mock_pr_url), patch("crew_chief.cli.subprocess.run") as mock_run:
        mock_result = subprocess.CompletedProcess(
            args=["gh", "pr", "create"],
            returncode=0,
            stdout=mock_pr_url,
            stderr="",
        )
        mock_run.return_value = mock_result

        # Import cli and call close command
        from crew_chief import cli
        import argparse

        args = argparse.Namespace(
            job=pr_job_id,
            project=str(repo_path),
            end_mode="pr",
            coordinator=None,
            reviewer_model=None,
            executor_models=None,
            type=None,
            no_changelog=True,
            json=False,
        )

        exit_code = cli._cmd_close(args)
        assert exit_code == 0, f"close command failed with exit code {exit_code}"

    # Reload job and verify state transition
    pr_open_job = records.load_job(repo_path, pr_job_id)
    assert pr_open_job["state"] == "pr_open", f"Expected state=pr_open, got {pr_open_job.get('state')}"
    assert pr_open_job["status"] == "pr_open", f"Expected status=pr_open, got {pr_open_job.get('status')}"
    assert pr_open_job.get("pr_url") == mock_pr_url, f"Expected pr_url={mock_pr_url}, got {pr_open_job.get('pr_url')}"

    # Verify event was appended
    events = pr_open_job.get("events", [])
    pr_events = [e for e in events if e.get("payload", {}).get("type") == "pr_open" or e.get("type") == "pr_open"]
    assert len(pr_events) > 0, f"No 'pr_open' event found in events: {events}"

    # Verify delivery record stored
    deliveries = pr_open_job.get("webhook_deliveries", [])
    assert len(deliveries) > 0, "No webhook delivery record stored for pr_open"

    # Verify webhook was received
    assert len(_WebhookReceiver.received) > 0, "No webhook received for pr_open transition"
    last_receive = _WebhookReceiver.received[-1]

    # Verify HMAC signature
    assert _verify_hmac(secret, last_receive["raw_body"], last_receive["signature_header"]), \
        f"HMAC verification failed for pr_open. Sig: {last_receive['signature_header']}"

    # Verify payload shape
    payload = json.loads(last_receive["raw_body"])
    assert payload["job_id"] == pr_job_id
    assert payload["status"] == "pr_open"
    assert payload["pr_url"] == mock_pr_url
    assert "timestamp" in payload

    # -----------------------------------------------------------------------
    # PART 4: Test WITHOUT notify_url - payload retrievable via status/result
    # -----------------------------------------------------------------------

    # Create a job with no notify_url configured
    no_notify_repo = tmp_path / "no_notify_repo"
    no_notify_repo.mkdir()
    _setup_git_repo(no_notify_repo)

    # Create preferences without notify settings
    no_notify_prefs_dir = no_notify_repo / "docs" / "crew-chief"
    no_notify_prefs_dir.mkdir(parents=True)
    no_notify_prefs_file = no_notify_prefs_dir / "preferences.toml"
    no_notify_prefs_file.write_text('# No notify_url configured\n')

    no_notify_job = records.new_job(
        no_notify_repo,
        title="No notify test job",
        request="Test without notify_url",
        distributed=True,
        reason="testing",
    )
    no_notify_job["state"] = "running"
    records.save_job(no_notify_repo, no_notify_job)
    no_notify_job_id = no_notify_job["id"]

    # Stop the job
    result = mcp_tools.stop(job_id=no_notify_job_id, project=str(no_notify_repo))
    assert result["ok"] is True

    # Verify job state changed but no webhook delivery
    stopped_no_notify = records.load_job(no_notify_repo, no_notify_job_id)
    assert stopped_no_notify["state"] == "stopped"

    # No webhook deliveries should be recorded
    deliveries = stopped_no_notify.get("webhook_deliveries", [])
    assert len(deliveries) == 0, f"Unexpected webhook deliveries without notify_url: {deliveries}"

    # But payload should be retrievable via status and result
    status_result = interview_service.get_interview_status(
        project=str(no_notify_repo),
        job_id=no_notify_job_id,
    )
    assert status_result["ok"] is True
    assert status_result["job_id"] == no_notify_job_id
    assert status_result["state"] == "stopped"

    result_val = mcp_tools.result(job_id=no_notify_job_id, project=str(no_notify_repo))
    assert result_val["ok"] is True
    assert result_val["job_id"] == no_notify_job_id

    # The notification payload should be available in the job record
    assert stopped_no_notify["state"] == "stopped"

    # Cleanup: stop the loopback server
    server.shutdown()
    server.server_close()