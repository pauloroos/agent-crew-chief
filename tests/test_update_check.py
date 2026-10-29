from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading
import time
import subprocess
import sys
from pathlib import Path

import pytest

from crew_chief import update_cache as cache
from crew_chief import update_check as check

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def deny_external_connections(monkeypatch):
    original = socket.getaddrinfo
    def resolve(host, *args, **kwargs):
        assert host in ('127.0.0.1', 'localhost', '::1'), host
        return original(host, *args, **kwargs)
    monkeypatch.setattr(socket, 'getaddrinfo', resolve)


@pytest.mark.parametrize('field', ['draft', 'prerelease'])
@pytest.mark.parametrize('value', [0, '', [], {}, None])
def test_malformed_release_flags(field, value):
    assert check.parse_release(json.dumps({'tag_name':'v0.4.4', field:value}).encode()) is None


def _worker(tmp_path, endpoint, setup=''):
    scripts = Path(check.__file__).parents[1]
    code = (f'import sys;sys.path.insert(0,{str(scripts)!r});'
            'from crew_chief import update_check as c;'
            'from datetime import datetime,timezone;from pathlib import Path;'
            'import socket,time;'
            'original=socket.getaddrinfo;'
            'socket.getaddrinfo=lambda host,*a,**kw: original(host,*a,**kw) if host=="127.0.0.1" else (_ for _ in ()).throw(AssertionError(host));'
            + setup +
            f'c.run_worker(Path({str(tmp_path)!r}),datetime.now(timezone.utc),endpoint={endpoint!r})')
    started = time.monotonic()
    child = subprocess.Popen([sys.executable, '-I', '-c', code], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        out, err = child.communicate(timeout=8)
        assert child.returncode == 0, err
        assert not out and not err
        assert 4 <= time.monotonic() - started < 7.5
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)


def test_worker_watchdog_bounds_blocked_dns(tmp_path):
    _worker(tmp_path, 'http://127.0.0.1:1/latest',
            'socket.getaddrinfo=lambda *a,**kw: time.sleep(30);')
    assert not cache.reserve_check(tmp_path, datetime.now(timezone.utc))


def test_worker_watchdog_bounds_continuous_trickle(tmp_path):
    done = threading.Event()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            try:
                self.send_response(200)
                self.send_header('Content-Length', '1000')
                self.end_headers()
                for _ in range(1000):
                    self.wfile.write(b' ')
                    self.wfile.flush()
                    if done.wait(.05):
                        break
            except OSError:
                pass
            finally:
                done.set()
    server = _Server(Handler)
    try:
        _worker(tmp_path, server.url)
    finally:
        done.set()
        server.close()
    assert not cache.reserve_check(tmp_path, datetime.now(timezone.utc))


class _Server:
    def __init__(self, handler):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        host, port = self.httpd.server_address
        return f"http://{host}:{port}/latest"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


def _handler(responses):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            item = responses.pop(0) if responses else (200, b"{}")
            status, body = item[:2]
            headers = item[2] if len(item) > 2 else {}
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if isinstance(body, (bytes, bytearray)):
                if len(body) > 4096:
                    self.wfile.write(bytes(body[:4096]))
                    self.wfile.flush()
                    time.sleep(0.1)
                    self.wfile.write(bytes(body[4096:]))
                else:
                    self.wfile.write(body)
            else:
                # trickle mode
                self.wfile.write(b'{"tag_name":"v0.4.4"')
                self.wfile.flush()
                time.sleep(5)
                self.wfile.write(b"}")

        def log_message(self, *args):
            pass

    return Handler


def test_fixed_endpoint():
    assert check.FIXED_ENDPOINT == "https://api.github.com/repos/pauloroos/agent-crew-chief/releases/latest"


def test_parse_release_valid_and_canonical():
    payload = b'{"tag_name":"v0.4.10","draft":false,"prerelease":false}'
    assert check.parse_release(payload) == "v0.4.10"
    assert check.parse_release(b'{"tag_name":"0.4.4"}') == "v0.4.4"


def test_parse_release_rejects_bad():
    cases = [
        b"not json",
        b"[]",
        b'{"draft":true,"tag_name":"v0.4.4"}',
        b'{"prerelease":true,"tag_name":"v0.4.4"}',
        b'{"tag_name":"v0.4.4-rc1"}',
        b'{"tag_name":"v01.2.3"}',
        b'{"tag_name":123}',
        b"x" * (64 * 1024 + 1),
    ]
    for case in cases:
        assert check.parse_release(case) is None, case[:40]


def test_is_newer_ordering():
    assert check.is_newer("v0.4.10", "0.4.3") is True
    assert check.is_newer("v0.4.4", "0.4.4") is False
    assert check.is_newer("v0.4.3", "0.4.4") is False
    assert check.is_newer("garbage", "0.4.4") is False
    assert check.is_newer("v0.4.4", "garbage") is False


def test_check_once_valid_stores_result(tmp_path):
    responses = [(200, b'{"tag_name":"v0.4.4","draft":false,"prerelease":false}')]
    server = _Server(_handler(responses))
    try:
        check.check_once(tmp_path, NOW, endpoint=server.url, timeout_s=2)
    finally:
        server.close()
    assert cache.cached_notice(tmp_path, "0.4.3", True, NOW) is not None
    assert not cache.reserve_check(tmp_path, NOW + timedelta(hours=1))


def test_check_once_equal_or_older_no_notice(tmp_path):
    for tag in ("v0.4.3", "v0.4.2"):
        root = tmp_path / tag
        root.mkdir()
        responses = [(200, ("{\"tag_name\":\"%s\"}" % tag).encode())]
        server = _Server(_handler(responses))
        try:
            check.check_once(root, NOW, endpoint=server.url, timeout_s=2)
        finally:
            server.close()
        assert cache.cached_notice(root, "0.4.3", True, NOW) is None


def test_check_once_draft_prerelease_invalid(tmp_path):
    for i, payload in enumerate((b'{"tag_name":"v0.4.4","draft":true}',
                                 b'{"tag_name":"v0.4.4","prerelease":true}',
                                 b'{"tag_name":"v0.4.4-rc1"}',
                                 b"not-json")):
        root = tmp_path / f"case{i}"
        root.mkdir()
        responses = [(200, payload)]
        server = _Server(_handler(responses))
        try:
            check.check_once(root, NOW, endpoint=server.url, timeout_s=2)
        finally:
            server.close()
        assert cache.cached_notice(root, "0.4.3", True, NOW) is None
        # throttled
        assert not cache.reserve_check(root, NOW + timedelta(hours=1))


def test_check_once_http_failures_silent_and_throttled(tmp_path):
    cases = [403, 429]
    for i, status in enumerate(cases):
        root = tmp_path / f"http{i}"
        root.mkdir()
        responses = [(status, b"{}")]
        server = _Server(_handler(responses))
        try:
            check.check_once(root, NOW, endpoint=server.url, timeout_s=2)
        finally:
            server.close()
        assert cache.cached_notice(root, "0.4.3", True, NOW) is None
        assert not cache.reserve_check(root, NOW + timedelta(hours=1))


def test_check_once_redirect_rejected(tmp_path):
    root = tmp_path / "redirect"
    root.mkdir()
    responses = [(302, b"", {"Location": "http://example.com/"})]
    server = _Server(_handler(responses))
    try:
        check.check_once(root, NOW, endpoint=server.url, timeout_s=2)
    finally:
        server.close()
    assert cache.cached_notice(root, "0.4.3", True, NOW) is None


def test_check_once_oversize_rejected(tmp_path):
    root = tmp_path / "big"
    root.mkdir()
    body = b'{"tag_name":"v0.4.4","pad":"' + b"x" * (64 * 1024) + b'"}'
    responses = [(200, body)]
    server = _Server(_handler(responses))
    try:
        check.check_once(root, NOW, endpoint=server.url, timeout_s=2)
    finally:
        server.close()
    assert cache.cached_notice(root, "0.4.3", True, NOW) is None


def test_check_once_trickle_timeout(tmp_path):
    root = tmp_path / "trickle"
    root.mkdir()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"tag_name":"v0.4.4"')
            self.wfile.flush()
            time.sleep(5)
            self.wfile.write(b"}")

        def log_message(self, *args):
            pass

    server = _Server(Handler)
    try:
        started = time.monotonic()
        check.check_once(root, NOW, endpoint=server.url, timeout_s=2)
        elapsed = time.monotonic() - started
    finally:
        server.close()
    assert elapsed < 4
    assert cache.cached_notice(root, "0.4.3", True, NOW) is None


def test_check_once_connection_failure_silent(tmp_path):
    root = tmp_path / "conn"
    root.mkdir()
    # connection refused
    check.check_once(root, NOW, endpoint="http://127.0.0.1:1/latest", timeout_s=1)
    assert cache.cached_notice(root, "0.4.3", True, NOW) is None


def test_check_once_future_now_skips(tmp_path):
    root = tmp_path / "future"
    root.mkdir()
    cache.reserve_check(root, NOW)
    # now earlier should not reserve again
    assert not cache.reserve_check(root, NOW - timedelta(seconds=1))


def test_parse_release_zero_version():
    assert check.parse_release(b'{"tag_name":"v0.0.0"}') == "v0.0.0"


def test_no_proxy_or_auth_headers(tmp_path):
    captured = {}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            captured["headers"] = dict(self.headers.items())
            body = b'{"tag_name":"v0.4.4"}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = _Server(Handler)
    try:
        check.check_once(tmp_path, NOW, endpoint=server.url, timeout_s=2)
    finally:
        server.close()
    h = {k.lower(): v for k, v in captured["headers"].items()}
    assert "authorization" not in h
    assert "proxy-authorization" not in h
    assert h.get("accept") == "application/json"
    assert "user-agent" in h
