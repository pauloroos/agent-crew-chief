"""Loopback-only web transport for the shared, read-only central snapshot.

Only fixed packaged assets and a public projection are exposed. Disk paths,
request bodies, credentials and raw registry/attempt records are never routes.
"""
from __future__ import annotations

from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import re
import threading

from . import briefcheck, central_snapshot

_ASSETS = {"/": ("index.html", "text/html; charset=utf-8"),
           "/index.html": ("index.html", "text/html; charset=utf-8"),
           "/central.css": ("central.css", "text/css; charset=utf-8"),
           "/central.js": ("central.js", "text/javascript; charset=utf-8")}
_PATH = re.compile(r"[A-Za-z]:[/\\][^\s<>\"']*|\\\\[^\s<>\"']+|(?<![\w:])/(?:[^\s<>\"']+)|~[/\\][^\s<>\"']*")
_PRIVATE_KEYS = {"repo", "worktree", "path", "root", "request", "authorization", "session_id",
                 "password", "passwd", "secret", "token", "api_key", "credential", "credentials"}
_TOP = {"schema_version", "generated_at", "coordinators", "questions", "totals", "diagnostics", "alerts"}
_ROW = {"id", "repo_label", "cli", "backend", "state", "worktree_available", "stale", "accepted",
        "total", "active_pieces", "jobs", "metrics", "models", "scoreboard", "last_error", "continuity"}


def _public(value, depth=0):
    if depth > 12:
        return None
    if isinstance(value, str):
        text = value[:65536]
        # A private key/cookie may span lines; suppress its whole text field.
        if "PRIVATE KEY" in text or re.search(r"(?im)^\s*(?:set-)?cookie:", text):
            return "[redacted]"
        for _, pattern in briefcheck._SECRET_RULES:
            text = pattern.sub("[redacted]", text)
        return _PATH.sub("[redacted]", text)
    if isinstance(value, dict):
        return {str(k): _public(v, depth + 1) for k, v in list(value.items())[:2048]
                if isinstance(k, str) and k.lower() not in _PRIVATE_KEYS
                and not any(word in k.lower() for word in ("password", "secret", "credential", "api_key"))}
    if isinstance(value, list):
        return [_public(v, depth + 1) for v in value[:2048]]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value if value is None or isinstance(value, (int, float, bool)) else None


def public_snapshot(snapshot: dict) -> dict:
    """Keep shared public fields and counts, sanitizing text without mutation."""
    data = {k: v for k, v in snapshot.items() if k in _TOP}
    data["coordinators"] = [{k: v for k, v in row.items() if k in _ROW}
                            for row in data.get("coordinators", []) if isinstance(row, dict)]
    return _public(data)


class WebServer(ThreadingHTTPServer):
    # Non-daemon request threads are joined by server_close; each socket has
    # a finite timeout so a half-written local request cannot prevent cleanup.
    daemon_threads = False
    block_on_close = True

    def get_request(self):
        sock, address = super().get_request()
        sock.settimeout(2)
        return sock, address


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):
        pass

    def _reply(self, status, value, content_type="application/json; charset=utf-8"):
        body = json.dumps(value, ensure_ascii=True, allow_nan=False).encode() if not isinstance(value, bytes) else value
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _host_ok(self):
        port = self.server.server_port
        return self.headers.get_all("Host", []) in ([f"127.0.0.1:{port}"], [f"localhost:{port}"])

    def _guard(self):
        if not self._host_ok():
            self._reply(403, {"error": "host refused"})
            return False
        if len(self.path) > 2048:
            self._reply(414, {"error": "path too long"})
            return False
        return True

    def do_GET(self):
        if not self._guard():
            return
        if self.path == "/api/snapshot":
            try:
                with self.server.snapshot_lock:
                    raw = central_snapshot.snapshot(self.server.root, datetime.now(timezone.utc), self.server.previous)
                    self.server.previous = raw
                    result = public_snapshot(raw)
                self._reply(200, result)
            except Exception:
                self._reply(503, {"error": "snapshot unavailable"})
            return
        if self.path not in _ASSETS:
            self._reply(404, {"error": "route not found"})
            return
        name, content_type = _ASSETS[self.path]
        path = self.server.assets / name
        try:
            if path.is_symlink() or not path.resolve().is_relative_to(self.server.assets.resolve()):
                raise OSError("invalid asset")
            with path.open("rb") as source:
                body = source.read(1024 * 1024 + 1)
            if len(body) > 1024 * 1024:
                raise OSError("oversized asset")
        except OSError:
            self._reply(404, {"error": "asset unavailable"})
            return
        self._reply(200, body, content_type)

    def do_POST(self):
        if self._guard():
            self._reply(405, {"error": "action unavailable"})


def create_server(root: Path, assets: Path, port: int = 0) -> ThreadingHTTPServer:
    """Bind IPv4 loopback only. Caller owns serve_forever/shutdown/server_close."""
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("port must be an integer from 0 to 65535")
    server = WebServer(("127.0.0.1", port), _Handler)
    server.root = Path(root)
    server.assets = Path(assets)
    server.previous = None
    server.snapshot_lock = threading.Lock()
    return server
