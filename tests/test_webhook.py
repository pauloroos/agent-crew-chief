"""Tests for the webhook sender (T023, US3, FR-015)."""
import hashlib
import hmac
import json
import os
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest


def _make_job_record(status="pr_open", pr_url=None):
    """Create a minimal job record for webhook testing."""
    return {
        "job_id": "test-job-123",
        "status": status,
        "pr_url": pr_url or "https://github.com/example/repo/pull/42",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _compute_hmac(secret: bytes, body: str) -> str:
    """Compute HMAC-SHA256 signature."""
    return f"sha256={hmac.new(secret, body.encode('utf-8'), hashlib.sha256).hexdigest()}"


@pytest.mark.parametrize(
    "status",
    ["pr_open", "failed", "stopped", "expired", "blocked"],
)
def test_sends_on_pr_open_failed_stopped_expired_blocked(status, tmp_path):
    """POSTs { job_id, status, pr_url, timestamp } to notify_url on terminal or blocked states."""
    from crew_chief.webhook import send_webhook_notice

    secret = b"test-secret-key"
    secret_file = tmp_path / "test_notify_secret"
    secret_file.write_bytes(secret)
    secret_file.chmod(0o600)

    job_record = _make_job_record(status=status)
    notify_url = "https://example.com/webhook"

    with patch("crew_chief.webhook.urllib.request.urlopen") as mock_urlopen:
        mock_response = MagicMock()
        mock_response.getcode.return_value = 200
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=None)
        mock_urlopen.return_value = mock_response

        result = send_webhook_notice(
            job_record=job_record,
            notify_url=notify_url,
            notify_secret_file=str(secret_file),
        )

        assert result["success"] is True
        mock_urlopen.assert_called_once()

        # Verify the request was built correctly
        call_args = mock_urlopen.call_args
        req = call_args[0][0]  # First positional argument is the Request object
        assert req.method == "POST"
        # header_items() returns list of (name, value) tuples with case-normalized names
        headers_dict = {k.lower(): v for k, v in req.header_items()}
        assert "x-acc-signature" in headers_dict
        sig_header = headers_dict["x-acc-signature"]
        assert sig_header.startswith("sha256=")

        # Verify payload shape
        body_data = req.data.decode("utf-8")
        payload = json.loads(body_data)
        assert "job_id" in payload
        assert "status" in payload
        assert "pr_url" in payload
        assert "timestamp" in payload
        assert payload["status"] == status

        # Verify HMAC signature matches
        expected_sig = _compute_hmac(secret, body_data)
        assert sig_header == expected_sig


def test_hmac_sha256_signature_header(tmp_path):
    """Header X-ACC-Signature: sha256=<hex> computed over raw body using secret from 0600 file (FR-016)."""
    from crew_chief.webhook import send_webhook_notice

    secret = b"restricted-webhook-secret-42"
    secret_file = tmp_path / "notify_secret"
    secret_file.write_bytes(secret)
    secret_file.chmod(0o600)

    job_record = _make_job_record(status="pr_open", pr_url="https://github.com/example/repo/pull/101")
    notify_url = "https://example.com/webhook"

    # 1. Successful send with 0600 permissions (or default file mode on Windows, e.g. 0666): header and signature verification
    with patch("crew_chief.webhook.urllib.request.urlopen") as mock_urlopen:
        mock_response = MagicMock()
        mock_response.getcode.return_value = 200
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=None)
        mock_urlopen.return_value = mock_response

        result = send_webhook_notice(
            job_record=job_record,
            notify_url=notify_url,
            notify_secret_file=str(secret_file),
        )

        assert result["success"] is True
        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]

        headers_dict = {k.lower(): v for k, v in req.header_items()}
        assert "x-acc-signature" in headers_dict
        sig_header = headers_dict["x-acc-signature"]

        raw_body_bytes = req.data
        expected_digest = hmac.new(secret, raw_body_bytes, hashlib.sha256).hexdigest()
        assert sig_header == f"sha256={expected_digest}"

    # 2. Insecure file permissions (0644, 0666, 0777) are tested in
    # test_insecure_file_permissions_rejected_on_posix with skipif(os.name == "nt") per FR-016.
    # On Windows (nt), mode bits cannot be restricted to 0600 via chmod;
    # a 0666 temp file still sends (tested in test_windows_secret_file_permissions_unrestricted).

    # 3. Missing secret file MUST be rejected and never send
    missing_file = tmp_path / "missing_secret_file"
    with patch("crew_chief.webhook.urllib.request.urlopen") as mock_urlopen:
        result = send_webhook_notice(
            job_record=job_record,
            notify_url=notify_url,
            notify_secret_file=str(missing_file),
        )
        assert result["success"] is False
        assert "cannot read" in result["reason"]
        mock_urlopen.assert_not_called()

    # 4. Directory as secret file MUST be rejected and never send
    dir_secret = tmp_path / "secret_directory"
    dir_secret.mkdir()
    dir_secret.chmod(0o600)
    with patch("crew_chief.webhook.urllib.request.urlopen") as mock_urlopen:
        result = send_webhook_notice(
            job_record=job_record,
            notify_url=notify_url,
            notify_secret_file=str(dir_secret),
        )
        assert result["success"] is False
        mock_urlopen.assert_not_called()

    # 5. End-to-end verification with a real local HTTP receiver (unmocked)
    import http.server
    import threading

    secret_file.chmod(0o600)
    received = []

    class _SignatureReceiverHandler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            content_length = int(self.headers.get("Content-Length", 0))
            raw_body = self.rfile.read(content_length)
            received.append({
                "headers": dict(self.headers),
                "body": raw_body,
            })
            self.send_response(200)
            self.end_headers()

        def log_message(self, format, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), _SignatureReceiverHandler)
    port = server.server_port
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    try:
        res = send_webhook_notice(
            job_record=job_record,
            notify_url=f"http://127.0.0.1:{port}/webhook",
            notify_secret_file=str(secret_file),
            notify_allow_http=True,
        )
        assert res["success"] is True
        assert len(received) == 1
        rec_headers = {k.lower(): v for k, v in received[0]["headers"].items()}
        assert "x-acc-signature" in rec_headers
        rec_sig = rec_headers["x-acc-signature"]
        expected_sig = f"sha256={hmac.new(secret, received[0]['body'], hashlib.sha256).hexdigest()}"
        assert rec_sig == expected_sig
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX-only: Windows chmod cannot enforce 0600 mode bits per spec FR-016",
)
def test_insecure_file_permissions_rejected_on_posix(tmp_path):
    """Insecure file permissions (0644, 0666, 0777) MUST be rejected and never send on POSIX (FR-016)."""
    from crew_chief.webhook import send_webhook_notice

    secret = b"restricted-webhook-secret-42"
    secret_file = tmp_path / "notify_secret_insecure"
    secret_file.write_bytes(secret)

    job_record = _make_job_record(status="pr_open", pr_url="https://github.com/example/repo/pull/101")
    notify_url = "https://example.com/webhook"

    for unsafe_mode in [0o644, 0o666, 0o777]:
        secret_file.chmod(unsafe_mode)
        with patch("crew_chief.webhook.urllib.request.urlopen") as mock_urlopen:
            result = send_webhook_notice(
                job_record=job_record,
                notify_url=notify_url,
                notify_secret_file=str(secret_file),
            )
            assert result["success"] is False
            assert "0600" in result["reason"]
            # Ensure secret and signature are not leaked in reason
            assert secret.decode("utf-8") not in result["reason"]
            assert "sha256=" not in result["reason"]
            mock_urlopen.assert_not_called()


def test_windows_secret_file_permissions_unrestricted(tmp_path):
    """On Windows (nt), mode bits cannot be restricted to 0600 via chmod; 0666 temp file still sends (FR-016)."""
    from crew_chief.webhook import send_webhook_notice

    secret = b"restricted-webhook-secret-42"
    secret_file = tmp_path / "notify_secret_win"
    secret_file.write_bytes(secret)
    # 0666 simulates typical Windows temp file mode (where chmod is a no-op for mode bits)
    secret_file.chmod(0o666)

    job_record = _make_job_record(status="pr_open", pr_url="https://github.com/example/repo/pull/101")
    notify_url = "https://example.com/webhook"

    with patch("crew_chief.webhook.urllib.request.urlopen") as mock_urlopen:
        mock_response = MagicMock()
        mock_response.getcode.return_value = 200
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=None)
        mock_urlopen.return_value = mock_response

        if os.name == "nt":
            # On native Windows, the 0600 mode gate is not enforced per FR-016
            result = send_webhook_notice(
                job_record=job_record,
                notify_url=notify_url,
                notify_secret_file=str(secret_file),
            )
        else:
            # On POSIX test runners, patch os.name to simulate Windows behavior
            with patch("crew_chief.webhook.os.name", "nt"):
                result = send_webhook_notice(
                    job_record=job_record,
                    notify_url=notify_url,
                    notify_secret_file=str(secret_file),
                )

        assert result["success"] is True
        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        headers_dict = {k.lower(): v for k, v in req.header_items()}
        assert "x-acc-signature" in headers_dict


def test_no_send_when_notify_url_not_set(tmp_path):
    """Does not POST when notify_url is absent."""
    from crew_chief.webhook import send_webhook_notice

    job_record = _make_job_record()

    with patch("crew_chief.webhook.urllib.request.urlopen") as mock_urlopen:
        result = send_webhook_notice(
            job_record=job_record,
            notify_url=None,
            notify_secret_file=str(tmp_path / "nonexistent"),
        )

        assert result["success"] is False
        assert result["reason"] == "notify_url not configured"
        mock_urlopen.assert_not_called()


def test_https_required_unless_allow_http(tmp_path):
    """Rejects non-HTTPS URLs unless notify_allow_http = true in preferences (T026, FR-018)."""
    from crew_chief import config
    from crew_chief.webhook import send_webhook_notice

    job_record = _make_job_record()
    local_http_url = "http://localhost:8080/webhook"
    container_http_url = "http://hermes:8080/webhook"
    container_https_url = "https://hermes:8080/webhook"
    external_https_url = "https://example.com/webhook"

    secret = b"test-secret-key"
    secret_file = tmp_path / "test_notify_secret_http"
    secret_file.write_bytes(secret)
    secret_file.chmod(0o600)

    # 1. Without allow_http: rejects both local and non-local container HTTP URLs
    for http_url in [local_http_url, container_http_url, "http://acc:8080/webhook"]:
        with patch("crew_chief.webhook.urllib.request.urlopen") as mock_urlopen:
            result = send_webhook_notice(
                job_record=job_record,
                notify_url=http_url,
                notify_secret_file=str(tmp_path / "nonexistent"),
                notify_allow_http=False,
            )

            assert result["success"] is False
            assert "HTTPS required" in result["reason"]
            mock_urlopen.assert_not_called()

    # 2. With allow_http = True: allows local and non-local container HTTP URLs
    for http_url in [local_http_url, container_http_url, "http://acc:8080/webhook"]:
        with patch("crew_chief.webhook.urllib.request.urlopen") as mock_urlopen:
            mock_response = MagicMock()
            mock_response.getcode.return_value = 200
            mock_response.__enter__ = MagicMock(return_value=mock_response)
            mock_response.__exit__ = MagicMock(return_value=None)
            mock_urlopen.return_value = mock_response

            result = send_webhook_notice(
                job_record=job_record,
                notify_url=http_url,
                notify_secret_file=str(secret_file),
                notify_allow_http=True,
            )

            assert result["success"] is True
            mock_urlopen.assert_called_once()
            req = mock_urlopen.call_args[0][0]
            assert req.full_url == http_url

    # 3. Default HTTPS URLs succeed without notify_allow_http (notify_allow_http=False)
    for https_url in [container_https_url, external_https_url]:
        with patch("crew_chief.webhook.urllib.request.urlopen") as mock_urlopen:
            mock_response = MagicMock()
            mock_response.getcode.return_value = 200
            mock_response.__enter__ = MagicMock(return_value=mock_response)
            mock_response.__exit__ = MagicMock(return_value=None)
            mock_urlopen.return_value = mock_response

            result = send_webhook_notice(
                job_record=job_record,
                notify_url=https_url,
                notify_secret_file=str(secret_file),
                notify_allow_http=False,
            )

            assert result["success"] is True
            mock_urlopen.assert_called_once()
            req = mock_urlopen.call_args[0][0]
            assert req.full_url == https_url

    # 4. Preference-to-sender path: verify preferences.toml notify_allow_http loading and wiring
    proj_opt_in = tmp_path / "proj_opt_in"
    prefs_file_in = proj_opt_in / "docs" / "crew-chief" / "preferences.toml"
    prefs_file_in.parent.mkdir(parents=True)
    prefs_file_in.write_text("notify_allow_http = true\n")

    prefs_in = config.load_prefs(proj_opt_in)
    assert prefs_in.notify_allow_http is True

    with patch("crew_chief.webhook.urllib.request.urlopen") as mock_urlopen:
        mock_response = MagicMock()
        mock_response.getcode.return_value = 200
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=None)
        mock_urlopen.return_value = mock_response

        res = send_webhook_notice(
            job_record=job_record,
            notify_url=container_http_url,
            notify_secret_file=str(secret_file),
            notify_allow_http=prefs_in.notify_allow_http,
        )
        assert res["success"] is True
        mock_urlopen.assert_called_once()

    # Preference-to-sender path: default / false in preferences.toml enforces HTTPS
    proj_default = tmp_path / "proj_default"
    prefs_file_def = proj_default / "docs" / "crew-chief" / "preferences.toml"
    prefs_file_def.parent.mkdir(parents=True)
    prefs_file_def.write_text("notify_allow_http = false\n")

    prefs_def = config.load_prefs(proj_default)
    assert prefs_def.notify_allow_http is False

    with patch("crew_chief.webhook.urllib.request.urlopen") as mock_urlopen:
        res = send_webhook_notice(
            job_record=job_record,
            notify_url=container_http_url,
            notify_secret_file=str(secret_file),
            notify_allow_http=prefs_def.notify_allow_http,
        )
        assert res["success"] is False
        assert "HTTPS required" in res["reason"]
        mock_urlopen.assert_not_called()


def test_redirects_not_followed(tmp_path):
    """Redirects are not followed; receiver must never receive redirected POST and only 2xx counts as success (FR-016)."""
    import http.server
    import threading
    from crew_chief.webhook import send_webhook_notice

    job_record = _make_job_record()
    secret = b"test-secret-key"
    secret_file = tmp_path / "test_notify_secret_redirect"
    secret_file.write_bytes(secret)
    secret_file.chmod(0o600)

    # Local receiver tracking actual requests made by urllib
    received_requests = []

    class _RedirectHandler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            received_requests.append(("POST", self.path))
            if self.path == "/webhook":
                self.send_response(302)
                self.send_header("Location", f"http://{self.server.server_address[0]}:{self.server.server_port}/destination")
                self.end_headers()
            elif self.path == "/destination":
                self.send_response(200)
                self.end_headers()

        def do_GET(self):
            received_requests.append(("GET", self.path))
            if self.path == "/destination":
                self.send_response(200)
                self.end_headers()

        def log_message(self, format, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), _RedirectHandler)
    port = server.server_port
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    try:
        notify_url = f"http://127.0.0.1:{port}/webhook"
        result = send_webhook_notice(
            job_record=job_record,
            notify_url=notify_url,
            notify_secret_file=str(secret_file),
            notify_allow_http=True,
        )

        assert result["success"] is False
        assert "302" in result["reason"]
        # Ensure destination was NEVER contacted (redirect was not followed)
        assert not any(p == "/destination" for _, p in received_requests)
    finally:
        server.shutdown()
        server.server_close()


def test_timeout_capped_at_10_seconds(tmp_path):
    """Request timeout is capped at 10 seconds."""
    from crew_chief.webhook import send_webhook_notice

    job_record = _make_job_record()
    notify_url = "https://example.com/webhook"

    secret = b"test-secret-key"
    secret_file = tmp_path / "test_notify_secret_timeout"
    secret_file.write_bytes(secret)
    secret_file.chmod(0o600)

    with patch("crew_chief.webhook.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = TimeoutError("Connection timed out")

        result = send_webhook_notice(
            job_record=job_record,
            notify_url=notify_url,
            notify_secret_file=str(secret_file),
        )

        assert result["success"] is False
        # Verify timeout parameter was passed
        mock_urlopen.assert_called_once()
        call_kwargs = mock_urlopen.call_args
        assert call_kwargs[1].get("timeout") == 10


def test_backoff_30s_2m_10m_30m_1h_max_5_retries(tmp_path):
    """Retries on non-2xx or timeout (>10s) with schedule 30s, 2m, 10m, 30m, 1h; max 5 retries; never alters job state."""
    import copy
    from crew_chief.webhook import send_webhook_notice, WebhookRetryScheduler

    secret = b"test-secret-key"
    secret_file = tmp_path / "test_notify_secret_backoff"
    secret_file.write_bytes(secret)
    secret_file.chmod(0o600)

    # 1. Non-2xx (HTTP 500) failure path with exact schedule, call count, and timeout check
    job_record_500 = _make_job_record(status="pr_open")
    job_record_500_copy = copy.deepcopy(job_record_500)
    notify_url = "https://example.com/webhook"

    call_count_500 = 0
    sleep_durations_500 = []
    timeouts_500 = []

    def mock_urlopen_500(req, timeout=10):
        nonlocal call_count_500
        call_count_500 += 1
        timeouts_500.append(timeout)
        raise urllib.error.HTTPError(req.full_url, 500, "Internal Server Error", {}, None)

    def mock_sleeper_500(duration):
        sleep_durations_500.append(duration)

    with patch("crew_chief.webhook.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = mock_urlopen_500

        result = send_webhook_notice(
            job_record=job_record_500,
            notify_url=notify_url,
            notify_secret_file=str(secret_file),
            _sleeper=mock_sleeper_500,
        )

        assert result["success"] is False
        assert "HTTP 500" in result["reason"]
        # Initial attempt + 5 retries = 6 total calls
        assert call_count_500 == 6
        # Verify sleep durations: 30s, 120s, 600s, 1800s, 3600s
        assert sleep_durations_500 == [30, 120, 600, 1800, 3600]
        # Each request timeout must be exactly 10 seconds
        assert timeouts_500 == [10] * 6
        # Job state immutability
        assert job_record_500 == job_record_500_copy

    # 2. Timeout / URLError path with controlled time and state immutability
    job_record_timeout = _make_job_record(status="failed")
    job_record_timeout_copy = copy.deepcopy(job_record_timeout)

    call_count_timeout = 0
    sleep_durations_timeout = []
    timeouts_timeout = []

    def mock_urlopen_timeout(req, timeout=10):
        nonlocal call_count_timeout
        call_count_timeout += 1
        timeouts_timeout.append(timeout)
        raise TimeoutError("Request timed out")

    def mock_sleeper_timeout(duration):
        sleep_durations_timeout.append(duration)

    with patch("crew_chief.webhook.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = mock_urlopen_timeout

        result_timeout = send_webhook_notice(
            job_record=job_record_timeout,
            notify_url=notify_url,
            notify_secret_file=str(secret_file),
            _sleeper=mock_sleeper_timeout,
        )

        assert result_timeout["success"] is False
        assert "timed out" in result_timeout["reason"]
        assert call_count_timeout == 6
        assert sleep_durations_timeout == [30, 120, 600, 1800, 3600]
        assert timeouts_timeout == [10] * 6
        assert job_record_timeout == job_record_timeout_copy

    # 3. Non-2xx status code via getcode (e.g. 503) path
    job_record_503 = _make_job_record(status="blocked")
    job_record_503_copy = copy.deepcopy(job_record_503)

    call_count_503 = 0
    sleep_durations_503 = []

    mock_resp_503 = MagicMock()
    mock_resp_503.getcode.return_value = 503
    mock_resp_503.__enter__ = MagicMock(return_value=mock_resp_503)
    mock_resp_503.__exit__ = MagicMock(return_value=None)

    def mock_urlopen_503(req, timeout=10):
        nonlocal call_count_503
        call_count_503 += 1
        return mock_resp_503

    def mock_sleeper_503(duration):
        sleep_durations_503.append(duration)

    with patch("crew_chief.webhook.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = mock_urlopen_503

        result_503 = send_webhook_notice(
            job_record=job_record_503,
            notify_url=notify_url,
            notify_secret_file=str(secret_file),
            _sleeper=mock_sleeper_503,
        )

        assert result_503["success"] is False
        assert "HTTP 503" in result_503["reason"]
        assert call_count_503 == 6
        assert sleep_durations_503 == [30, 120, 600, 1800, 3600]
        assert job_record_503 == job_record_503_copy

    # 4. Production asynchronous sender integration: WebhookRetryScheduler schedules up to 5 retries
    job_record_sched = _make_job_record(status="stopped")
    job_record_sched_copy = copy.deepcopy(job_record_sched)

    scheduled_retries = []

    class FakeTimer:
        def __init__(self, interval, function, args=None, kwargs=None):
            self.interval = interval
            self.function = function
            self.args = args or []
            self.kwargs = kwargs or {}
            self.started = False
            self.cancelled = False

        def start(self):
            self.started = True
            scheduled_retries.append(self)

        def cancel(self):
            self.cancelled = True

        def run(self):
            self.function(*self.args, **self.kwargs)

    custom_scheduler = WebhookRetryScheduler(timer_factory=FakeTimer)
    call_count_sched = 0

    def mock_urlopen_sched(req, timeout=10):
        nonlocal call_count_sched
        call_count_sched += 1
        raise TimeoutError("Simulated socket timeout")

    with patch("crew_chief.webhook.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = mock_urlopen_sched

        # Initial call should return immediately without blocking
        res_sched = send_webhook_notice(
            job_record=job_record_sched,
            notify_url=notify_url,
            notify_secret_file=str(secret_file),
            _retry_scheduler=custom_scheduler,
        )

        assert res_sched["success"] is False
        assert call_count_sched == 1
        assert len(scheduled_retries) == 1
        assert scheduled_retries[0].interval == 30

        # Step through scheduled retries
        intervals_observed = []
        for expected_interval in [30, 120, 600, 1800, 3600]:
            timer = scheduled_retries[-1]
            assert timer.started is True
            intervals_observed.append(timer.interval)
            # Execute retry
            timer.run()

        # All 5 retries were scheduled and executed (total 6 attempts: initial + 5 retries)
        assert call_count_sched == 6
        assert intervals_observed == [30, 120, 600, 1800, 3600]
        assert len(scheduled_retries) == 5  # No 6th retry scheduled
        assert job_record_sched == job_record_sched_copy


def test_send_webhook_notice_does_not_block_caller_on_failure(tmp_path):
    """Regression test: calling send_webhook_notice on failure must not block synchronously for backoff durations."""
    import time
    from crew_chief.webhook import send_webhook_notice

    secret = b"test-secret-key"
    secret_file = tmp_path / "test_notify_secret_nonblocking"
    secret_file.write_bytes(secret)
    secret_file.chmod(0o600)

    job_record = _make_job_record(status="pr_open")
    notify_url = "https://example.com/webhook"

    with patch("crew_chief.webhook.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = TimeoutError("Connection timed out")

        t0 = time.monotonic()
        # On un-repaired synchronous sender, this hangs for 6150 seconds or until test timeout.
        # Calling path must remain responsive (< 2 seconds).
        result = send_webhook_notice(
            job_record=job_record,
            notify_url=notify_url,
            notify_secret_file=str(secret_file),
        )
        elapsed = time.monotonic() - t0

        assert elapsed < 2.0
        assert result["success"] is False


