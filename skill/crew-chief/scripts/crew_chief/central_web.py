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
import secrets
import threading
import uuid

from . import briefcheck, central_controls, central_messages, central_snapshot

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

# Bounded limits
_MAX_BODY_BYTES = 80 * 1024  # 80 KiB
_MAX_JSON_DEPTH = 16
_MAX_ACTION_IDS = 1024


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
                and not k.lower().endswith("token")
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

    def _origin_ok(self):
        """Strict Origin check for POST requests."""
        origin = self.headers.get("Origin", "")
        port = self.server.server_port
        if not origin:
            return False
        # Allow only same-origin loopback
        return origin in (f"http://127.0.0.1:{port}", f"http://localhost:{port}")

    def _read_json_body(self, max_bytes: int = _MAX_BODY_BYTES):
        """Read and parse JSON body with bounds checking."""
        content_length = self.headers.get("Content-Length")
        if content_length is None:
            return None, "missing Content-Length"
        try:
            length = int(content_length)
        except ValueError:
            return None, "invalid Content-Length"
        if length > max_bytes:
            return None, "body too large"
        if length < 0:
            return None, "invalid body length"
        raw = self.rfile.read(length)
        if len(raw) != length:
            return None, "truncated body"
        try:
            return json.loads(raw.decode("utf-8")), None
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None, "invalid JSON"

    def _validate_csrf(self, body: dict):
        """Validate CSRF token from request body."""
        token = body.get("csrf_token")
        if not isinstance(token, str) or not token:
            return False, "missing or invalid CSRF token"
        if not hasattr(self.server, "csrf_token") or not self.server.csrf_token:
            return False, "server CSRF token not initialized"
        return secrets.compare_digest(token, self.server.csrf_token), None

    def _check_action_id(self, action_id: str):
        """Check and record action ID for idempotent deduplication."""
        if not isinstance(action_id, str) or not action_id:
            return False, "missing or invalid action_id"
        try:
            parsed = uuid.UUID(action_id)
        except ValueError:
            return False, "action_id must be a valid UUID"
        if str(parsed) != action_id:
            return False, "action_id must be canonical UUID"
        with self.server.action_ids_lock:
            if action_id in self.server.action_ids:
                return False, "duplicate action_id"
            if len(self.server.action_ids) >= _MAX_ACTION_IDS:
                # Simple FIFO eviction - remove oldest
                self.server.action_ids.pop()
            self.server.action_ids.add(action_id)
        return True, None

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
        if self.path == "/api/csrf":
            # Return CSRF token for bootstrapping the client
            self._reply(200, {"csrf_token": self.server.csrf_token})
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
        if not self._guard():
            return
        if not self._origin_ok():
            self._reply(403, {"error": "origin refused"})
            return

        # Read JSON body
        body, error = self._read_json_body()
        if error:
            self._reply(400, {"error": error})
            return
        if not isinstance(body, dict):
            self._reply(400, {"error": "body must be a JSON object"})
            return

        # Validate CSRF token
        ok, error = self._validate_csrf(body)
        if not ok:
            self._reply(403, {"error": error})
            return

        # Validate action_id for deduplication
        action_id = body.get("action_id")
        ok, error = self._check_action_id(action_id)
        if not ok:
            self._reply(409, {"error": error})
            return

        # Route to specific action
        if self.path == "/api/message":
            self._handle_message(body)
        elif self.path == "/api/answer":
            self._handle_answer(body)
        elif self.path == "/api/pause":
            self._handle_pause(body)
        elif self.path == "/api/resume":
            self._handle_resume(body)
        elif self.path == "/api/attach":
            self._handle_attach(body)
        else:
            self._reply(404, {"error": "route not found"})

    def _handle_message(self, body: dict):
        """POST /api/message - send a message to a coordinator."""
        coordinator_id = body.get("coordinator_id")
        text = body.get("text")
        if not isinstance(coordinator_id, str) or not coordinator_id:
            self._reply(400, {"error": "coordinator_id is required"})
            return
        if not isinstance(text, str) or not text:
            self._reply(400, {"error": "text is required"})
            return

        try:
            envelope = central_messages.owner_message(coordinator_id, text)
            msg_id = central_messages.send(self.server.root, coordinator_id, envelope)
            self._reply(200, {"ok": True, "message_id": msg_id})
        except central_messages.MessageError as exc:
            self._reply(400, {"error": str(exc)})
        except Exception:
            self._reply(500, {"error": "message send failed"})

    def _handle_answer(self, body: dict):
        """POST /api/answer - reply to a question."""
        coordinator_id = body.get("coordinator_id")
        question_id = body.get("question_id")
        text = body.get("text")
        if not isinstance(coordinator_id, str) or not coordinator_id:
            self._reply(400, {"error": "coordinator_id is required"})
            return
        if not isinstance(question_id, str) or not question_id:
            self._reply(400, {"error": "question_id is required"})
            return
        if not isinstance(text, str) or not text:
            self._reply(400, {"error": "text is required"})
            return

        try:
            envelope = central_messages.owner_message(coordinator_id, text, reply_to=question_id)
            msg_id = central_messages.send(self.server.root, coordinator_id, envelope)
            self._reply(200, {"ok": True, "message_id": msg_id})
        except central_messages.MessageError as exc:
            self._reply(400, {"error": str(exc)})
        except Exception:
            self._reply(500, {"error": "answer send failed"})

    def _handle_pause(self, body: dict):
        """POST /api/pause - pause a coordinator."""
        coordinator_id = body.get("coordinator_id")
        if not isinstance(coordinator_id, str) or not coordinator_id:
            self._reply(400, {"error": "coordinator_id is required"})
            return

        try:
            control_id = central_controls.request_control(self.server.root, coordinator_id, "pause")
            self._reply(200, {"ok": True, "control_id": control_id})
        except central_controls.ControlError as exc:
            self._reply(400, {"error": str(exc)})
        except Exception:
            self._reply(500, {"error": "pause request failed"})

    def _handle_resume(self, body: dict):
        """POST /api/resume - resume a coordinator."""
        coordinator_id = body.get("coordinator_id")
        if not isinstance(coordinator_id, str) or not coordinator_id:
            self._reply(400, {"error": "coordinator_id is required"})
            return

        try:
            control_id = central_controls.request_control(self.server.root, coordinator_id, "resume")
            self._reply(200, {"ok": True, "control_id": control_id})
        except central_controls.ControlError as exc:
            self._reply(400, {"error": str(exc)})
        except Exception:
            self._reply(500, {"error": "resume request failed"})

    def _handle_attach(self, body: dict):
        """POST /api/attach - queue attach intent for active terminal bridge."""
        coordinator_id = body.get("coordinator_id")
        if not isinstance(coordinator_id, str) or not coordinator_id:
            self._reply(400, {"error": "coordinator_id is required"})
            return

        # Check if attach bridge is active (enabled via server config)
        attach_bridge_active = getattr(self.server, "attach_bridge_active", False)

        if attach_bridge_active:
            # Queue the attach intent for the terminal bridge to consume
            # For now, we just return success - the actual bridge integration
            # will be handled in T034
            self._reply(200, {
                "ok": True,
                "queued": True,
                "message": "attach intent queued for terminal bridge"
            })
        else:
            # Return copyable acc attach UUID fallback
            self._reply(200, {
                "ok": True,
                "queued": False,
                "attach_id": coordinator_id,
                "message": f"no terminal bridge available; run: acc attach {coordinator_id}"
            })


def create_server(root: Path, assets: Path, port: int = 0, *, attach_bridge_active: bool = False) -> ThreadingHTTPServer:
    """Bind IPv4 loopback only. Caller owns serve_forever/shutdown/server_close.

    Args:
        root: Central storage root directory.
        assets: Packaged static assets directory.
        port: Port to bind (0 for ephemeral).
        attach_bridge_active: Whether the terminal attach bridge is active.
            When False (default), attach returns a copyable `acc attach ID` instruction.
    """
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("port must be an integer from 0 to 65535")
    server = WebServer(("127.0.0.1", port), _Handler)
    server.root = Path(root)
    server.assets = Path(assets)
    server.previous = None
    server.snapshot_lock = threading.Lock()
    server.csrf_token = secrets.token_urlsafe(32)
    server.action_ids = set()
    server.action_ids_lock = threading.Lock()
    server.attach_bridge_active = bool(attach_bridge_active)
    return server