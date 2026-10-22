"""Isolated real loopback HTTP tests; no coordinator or model is started."""
from contextlib import contextmanager
from http.client import HTTPConnection
import json
from pathlib import Path
import threading

import pytest

from crew_chief import central_snapshot
REAL_SNAPSHOT = central_snapshot.snapshot


@contextmanager
def running(tmp_path, monkeypatch, data=None):
    from crew_chief.central_web import create_server
    assets = tmp_path / "assets"
    assets.mkdir(exist_ok=True)
    for name in ("index.html", "central.css", "central.js"):
        (assets / name).write_text("asset-" + name, encoding="utf-8")
    (assets / "private.txt").write_text("PRIVATE", encoding="utf-8")
    sample = data or {"schema_version": 1, "generated_at": "2026-01-01", "coordinators": [],
                      "questions": [], "totals": {"tokens_total": 7}, "diagnostics": []}
    monkeypatch.setattr(central_snapshot, "snapshot", lambda *a, **kw: sample)
    server = create_server(tmp_path / "central", assets)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01})
    thread.start()
    try:
        yield server, sample
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)
        assert not thread.is_alive()


def request(server, path, *, host=None, method="GET"):
    conn = HTTPConnection(*server.server_address, timeout=3)
    try:
        headers = {} if host is None else {"Host": host}
        conn.request(method, path, headers=headers)
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        conn.close()


def test_snapshot_parity_read_only(tmp_path, monkeypatch):
    with running(tmp_path, monkeypatch) as (server, sample):
        before = sorted(str(p) for p in tmp_path.rglob("*"))
        status, headers, body = request(server, "/api/snapshot")
        assert status == 200
        assert json.loads(body) == sample
        assert headers["Cache-Control"] == "no-store"
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert "default-src 'self'" in headers["Content-Security-Policy"]
        assert not any(k.lower().startswith("access-control") for k in headers)
        assert sorted(str(p) for p in tmp_path.rglob("*")) == before
        assert server.server_address[0] == "127.0.0.1"


@pytest.mark.parametrize("path", ["/", "/index.html", "/central.css", "/central.js"])
def test_assets(tmp_path, monkeypatch, path):
    with running(tmp_path, monkeypatch) as (server, _):
        status, _, body = request(server, path)
        assert status == 200 and body.startswith(b"asset-")


@pytest.mark.parametrize("path", ["/../private.txt", "/%2e%2e/private.txt", "/%252e%252e/private.txt",
    "/central.js/../private.txt", "/private.txt", "/registry.json", "/central.js?file=private.txt",
    "/%2fcentral.js", "/central.js%00", "/" + "x" * 9000])
def test_disallowed_paths(tmp_path, monkeypatch, path):
    with running(tmp_path, monkeypatch) as (server, _):
        status, _, body = request(server, path)
        assert status in (400, 404, 414)
        assert b"PRIVATE" not in body and str(tmp_path).encode() not in body


@pytest.mark.parametrize("host", ["evil.example", "127.0.0.1.evil:1", "0.0.0.0", "localhost", "127.0.0.1:1"])
def test_host_rejected(tmp_path, monkeypatch, host):
    with running(tmp_path, monkeypatch) as (server, _):
        assert request(server, "/api/snapshot", host=host)[0] == 403


def test_public_projection_removes_private_fields_and_text(tmp_path, monkeypatch):
    secret = "sk-" + "a" * 36
    data = {"schema_version": 1, "generated_at": "2026-01-01", "repo": str(tmp_path),
        "coordinators": [{"id": "public-id", "repo_label": "demo", "worktree": str(tmp_path),
            "last_error": "failed C:\\private\\file and /home/private/file " + secret,
            "jobs": [{"id": "j", "title": "at /tmp/hidden token=abcdefgh123456", "request": secret}],
            "metrics": {"tokens_total": 17, "password": "abcdefgh123456"}}],
        "questions": [{"id": "q", "body": "open \\\\host\\share\\file", "coordinator_id": "public-id"}],
        "totals": {"tokens_total": 17}, "diagnostics": [{"message": str(tmp_path)}]}
    with running(tmp_path, monkeypatch, data) as (server, _):
        status, _, body = request(server, "/api/snapshot")
        assert status == 200
        text = body.decode()
        for private in (secret, "abcdefgh123456", "private", "hidden", "share", str(tmp_path)):
            assert private not in text
        result = json.loads(body)
        assert result["totals"]["tokens_total"] == 17
        assert result["coordinators"][0]["metrics"]["tokens_total"] == 17
        assert "worktree" not in result["coordinators"][0]
        assert data["coordinators"][0]["worktree"] == str(tmp_path)


def test_symlink_not_served(tmp_path, monkeypatch):
    with running(tmp_path, monkeypatch) as (server, _):
        target = tmp_path / "secret"
        target.write_text("PRIVATE", encoding="utf-8")
        asset = tmp_path / "assets" / "central.js"
        asset.unlink()
        try:
            asset.symlink_to(target)
        except OSError:
            pytest.skip("symlink permission unavailable")
        assert request(server, "/central.js")[0] == 404


def test_snapshot_error_is_generic(tmp_path, monkeypatch):
    with running(tmp_path, monkeypatch) as (server, _):
        def fail(*a, **kw):
            raise OSError(str(tmp_path))
        monkeypatch.setattr(central_snapshot, "snapshot", fail)
        status, _, body = request(server, "/api/snapshot")
        assert status == 503 and str(tmp_path).encode() not in body


def test_http_snapshot_preserves_injected_utc_clock(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from crew_chief import central_web
    with running(tmp_path, monkeypatch) as (server, _):
        class Clock:
            @staticmethod
            def now(tz):
                return datetime(2026, 1, 1, 0, 0, 10, tzinfo=timezone.utc)
        monkeypatch.setattr(central_web, "datetime", Clock)
        # Use the real shared snapshot: a double that ignores its clock would
        # hide the transport bug. Empty temporary registry is sufficient.
        # running() patches snapshot, so use the original function captured at import.
        monkeypatch.setattr(central_snapshot, "snapshot", REAL_SNAPSHOT)
        status, _, body = request(server, "/api/snapshot")
        assert status == 200 and json.loads(body)["generated_at"] == "2026-01-01T00:00:10+00:00"


def test_credential_key_variants_and_public_slashes():
    from crew_chief.central_web import public_snapshot
    data = {"schema_version": 1, "coordinators": [], "totals": {
        "tokens_total": 17, "total_tokens": 17, "auth_token": "opaque-value",
        "accessToken": "opaque-value", "model": "provider/model", "title": "a/b 2026/10/22"}}
    result = public_snapshot(data)["totals"]
    assert "auth_token" not in result and "accessToken" not in result
    assert result["tokens_total"] == result["total_tokens"] == 17
    assert result["model"] == "provider/model"
    assert result["title"] == "a/b 2026/10/22"


def test_socket_released(tmp_path, monkeypatch):
    import socket
    with running(tmp_path, monkeypatch) as (server, _):
        address = server.server_address
        assert request(server, "/")[0] == 200
    with socket.socket() as sock:
        sock.bind(address)


@pytest.mark.parametrize("port", [-1, 65536, True, "80"])
def test_invalid_port_refused(tmp_path, port):
    from crew_chief.central_web import create_server
    with pytest.raises(ValueError):
        create_server(tmp_path, tmp_path, port)
