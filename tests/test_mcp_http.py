"""Tests for the authenticated loopback HTTP MCP transport (T002).

Verifies FR-004 and FR-001: bearer-token authentication, loopback-only
binding, Host/Origin validation, and absence of CORS headers.
"""

from __future__ import annotations

import json
import os
import threading
import time
from http.client import HTTPConnection
from typing import Generator

import pytest


@pytest.fixture()
def http_server() -> Generator[dict, None, None]:
    """Start the HTTP MCP server on a free loopback port and yield its address."""
    from crew_chief.mcp_http import serve_http

    # Port 0 lets the OS pick a free port; serve_http must expose the bound
    # port via the returned dict or an attribute on the server object.
    result: dict = {}
    thread = threading.Thread(
        target=serve_http,
        kwargs={"port": 0, "token": "test-token-abc", "_ready": result},
        daemon=True,
    )
    thread.start()
    # Wait for the server to signal readiness.
    deadline = time.monotonic() + 5
    while "port" not in result and time.monotonic() < deadline:
        time.sleep(0.05)
    if "port" not in result:
        raise RuntimeError("HTTP server did not become ready in time")
    yield result
    # Signal shutdown by connecting and sending a special request, or rely on
    # daemon thread cleanup. The serve_http function should support graceful
    # stop; for now the daemon thread dies with the process.


def _post(
    host: str,
    port: int,
    body: bytes,
    *,
    token: str | None = "test-token-abc",
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, list[str]], bytes]:
    """Send a POST request and return (status, headers, body)."""
    conn = HTTPConnection(host, port, timeout=5)
    try:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if extra_headers:
            headers.update(extra_headers)
        conn.request("POST", "/", body=body, headers=headers)
        resp = conn.getresponse()
        resp_body = resp.read()
        resp_headers = {k.lower(): v for k, v in resp.getheaders()}
        return resp.status, resp_headers, resp_body
    finally:
        conn.close()


def test_bearer_token_auth_and_host_validation(http_server: dict) -> None:
    """Reject missing/invalid bearer tokens (401) and non-loopback Host (403).

    Also verifies that no CORS headers are emitted on any response.
    """
    port = http_server["port"]
    host = "127.0.0.1"
    valid_body = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "ping",
    }).encode()

    # --- 401 for missing Authorization header ---
    status, headers, body = _post(host, port, valid_body, token=None)
    assert status == 401, f"expected 401 for missing token, got {status}"
    assert b"Access-Control-Allow" not in body
    assert "access-control-allow-origin" not in headers

    # --- 401 for invalid bearer token ---
    status, headers, body = _post(host, port, valid_body, token="wrong-token")
    assert status == 401, f"expected 401 for wrong token, got {status}"
    assert "access-control-allow-origin" not in headers

    # --- 403 for non-loopback Host header ---
    status, headers, body = _post(
        host, port, valid_body, extra_headers={"Host": "evil.example.com"}
    )
    assert status == 403, f"expected 403 for non-loopback Host, got {status}"
    assert "access-control-allow-origin" not in headers

    # --- 403 for Origin header (even with valid token) ---
    status, headers, body = _post(
        host, port, valid_body, extra_headers={"Origin": "https://evil.example.com"}
    )
    assert status == 403, f"expected 403 for non-loopback Origin, got {status}"
    assert "access-control-allow-origin" not in headers

    # --- Valid request succeeds ---
    status, headers, body = _post(host, port, valid_body)
    assert status == 200, f"expected 200 for valid request, got {status}: {body!r}"
    data = json.loads(body)
    assert data.get("jsonrpc") == "2.0"
    assert data.get("id") == 1
    assert "result" in data
    # No CORS headers on success either.
    assert "access-control-allow-origin" not in headers


def test_http_network_amendment_security(tmp_path: any) -> None:
    """Verify owner network amendment: configurable host/port, 0.0.0.0 requires 0600 token file, Host acc:<port> allowed, payload limits (T002, FR-004, FR-001)."""
    import os
    from crew_chief.mcp_http import serve_http
    from crew_chief.cli import build_parser

    # 1. CLI parser accepts --host, --port, --token-file
    parser = build_parser()
    args = parser.parse_args(["mcp", "--http", "--host", "0.0.0.0", "--port", "8888", "--token-file", "/tmp/token"])
    assert args.http is True
    assert args.host == "0.0.0.0"
    assert args.port == 8888
    assert args.token_file == "/tmp/token"

    # 2. Refusal to bind to non-loopback without a 0600 token file
    with pytest.raises(ValueError, match="0600"):
        serve_http(host="0.0.0.0", port=0, token="insecure-token")

    # With permissions 0600 -> accepted
    token_file_valid = tmp_path / "valid_token.txt"
    token_file_valid.write_text("secret-token-123\n")
    os.chmod(token_file_valid, 0o600)
    ready: dict = {}
    thread = threading.Thread(
        target=serve_http,
        kwargs={"host": "127.0.0.1", "port": 0, "token_file": str(token_file_valid), "allowed_hosts": {"acc"}, "_ready": ready},
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 5
    while "port" not in ready and time.monotonic() < deadline:
        time.sleep(0.05)
    port = ready["port"]

    valid_token = "secret-token-123"
    valid_body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode()

    # 3. Configured Host acc:<port> is accepted
    status, headers, body = _post(
        "127.0.0.1", port, valid_body, token=valid_token, extra_headers={"Host": f"acc:{port}"}
    )
    assert status == 200, f"Expected 200 for Host acc:{port}, got {status}: {body!r}"
    assert "access-control-allow-origin" not in headers

    # Also Host acc without port
    status, headers, body = _post(
        "127.0.0.1", port, valid_body, token=valid_token, extra_headers={"Host": "acc"}
    )
    assert status == 200

    # 4. Bad host rejected with 403
    status, headers, body = _post(
        "127.0.0.1", port, valid_body, token=valid_token, extra_headers={"Host": "unauthorized.net"}
    )
    assert status == 403

    # 5. Bad origin rejected with 403
    status, headers, body = _post(
        "127.0.0.1", port, valid_body, token=valid_token, extra_headers={"Origin": "http://evil.org"}
    )
    assert status == 403

    # 6. Bad body length rejected (payload size limit or invalid Content-Length)
    # Huge Content-Length rejected
    huge_length_headers = {"Content-Length": "100000000"}  # 100MB
    status, headers, body = _post(
        "127.0.0.1", port, b"{}", token=valid_token, extra_headers=huge_length_headers
    )
    assert status in (400, 413)


@pytest.mark.skipif(
    os.name == "nt",
    reason="spec 003-hermes-pipeline FR-001/FR-004: 0600 mode bits are POSIX-only; Windows uses ACLs",
)
def test_http_token_file_insecure_permissions_rejected(tmp_path: any) -> None:
    """Token file with non-0600 permissions is rejected on POSIX (FR-001, FR-004)."""
    from crew_chief.mcp_http import serve_http

    token_file = tmp_path / "insecure_token.txt"
    token_file.write_text("secret-token-123\n")
    os.chmod(token_file, 0o644)
    with pytest.raises(ValueError, match="0600"):
        serve_http(host="0.0.0.0", port=0, token_file=str(token_file))


def test_http_token_file_windows_warning(tmp_path: any, monkeypatch: pytest.MonkeyPatch) -> None:
    """On Windows, serving with a token file emits a warning that permissions are not checked (FR-001, FR-004)."""
    from crew_chief import mcp_http

    token_file = tmp_path / "win_token.txt"
    token_file.write_text("secret-token-123\n")
    monkeypatch.setattr(mcp_http, "_IS_WINDOWS", True)
    ready: dict = {}
    with pytest.warns(UserWarning, match="token_file permissions are not checked on Windows"):
        thread = threading.Thread(
            target=mcp_http.serve_http,
            kwargs={"host": "127.0.0.1", "port": 0, "token_file": str(token_file), "_ready": ready},
            daemon=True,
        )
        thread.start()
        deadline = time.monotonic() + 5
        while "port" not in ready and time.monotonic() < deadline:
            time.sleep(0.05)
    assert "port" in ready
