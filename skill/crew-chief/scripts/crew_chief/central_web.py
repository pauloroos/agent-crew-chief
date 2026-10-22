"""Loopback-only web transport for the shared, read-only central snapshot.

Only fixed packaged assets and a public projection are exposed. Disk paths,
request bodies, credentials and raw registry/attempt records are never routes.
"""
from __future__ import annotations

from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
import queue
from pathlib import Path
import re
import secrets
import shlex
import threading

from . import briefcheck, central_config, central_controls, central_messages, central_registry, central_snapshot
from .central_backend import BackendError

_ASSETS = {"/": ("index.html", "text/html; charset=utf-8"),
           "/index.html": ("index.html", "text/html; charset=utf-8"),
           "/central.css": ("central.css", "text/css; charset=utf-8"),
           "/central.js": ("central.js", "text/javascript; charset=utf-8"),
           "/favicon.svg": ("favicon.svg", "image/svg+xml")}
_PATH = re.compile(r"[A-Za-z]:[/\\][^\s<>\"']*|\\\\[^\s<>\"']+|(?<![\w:<])/(?:[^\s<>\"']+)|~[/\\][^\s<>\"']*")
# The optional (escaped) quote after the key also catches JSON/dict fragments such as
# {"password": "x"} and {\"password\": \"x\"}; an Authorization value covers Basic/Token schemes.
_CREDENTIAL_TEXT = re.compile(r"(?i)(?:api[_-]?key|secret|password|passwd|passphrase|private[_-]?key|authorization|token)"
                              r"\\?[\"']?\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)|Bearer\s+[^\s,;]+")
_PRIVATE_KEYS = {"repo", "worktree", "path", "root", "request", "authorization", "session_id",
                 "password", "passwd", "secret", "token", "api_key", "credential", "credentials"}
_TOP = {"schema_version", "generated_at", "coordinators", "questions", "totals", "diagnostics", "alerts"}
_ROW = {"id", "repo_label", "cli", "backend", "state", "worktree_available", "stale", "accepted",
        "total", "active_pieces", "jobs", "metrics", "models", "scoreboard", "scoreboard_rows", "last_error", "continuity"}

# Bounded limits
_MAX_BODY_BYTES = 80 * 1024  # 80 KiB
_MAX_ACTION_IDS = 1024


def _private_key(key):
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    return (key.lower() in _PRIVATE_KEYS or normalized.endswith("token")
            or any(word in normalized for word in ("password", "passphrase", "secret", "credential", "apikey", "privatekey"))
            or normalized in ("authorization", "cookie", "setcookie"))


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("non-finite JSON")


def _public(value, depth=0):
    if depth > 12:
        return None
    if isinstance(value, str):
        text = value[:65536]
        # A private key/cookie may span lines; suppress its whole text field.
        if "PRIVATE KEY" in text or re.search(r"(?im)^\s*(?:set-)?cookie:", text):
            return "[redacted]"
        if _CREDENTIAL_TEXT.search(text) or any(pattern.search(text) for _, pattern in briefcheck._SECRET_RULES):
            return "[redacted]"
        # Do not leave a private path's space-delimited suffix in the response.
        return "[redacted]" if _PATH.search(text) else text
    if isinstance(value, dict):
        return {str(k): _public(v, depth + 1) for k, v in list(value.items())[:2048]
                if isinstance(k, str) and not _private_key(k) and not _PATH.search(k)}
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
        return self.headers.get_all("Origin", []) == ["http://" + self.headers.get("Host", "")]

    def _read_json_body(self):
        if self.headers.get_all("Content-Type", []) not in (["application/json"], ["application/json; charset=utf-8"]):
            return None, 415
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or self.headers.get("Transfer-Encoding") is not None:
            return None, 400
        try:
            if not lengths[0].isascii() or not lengths[0].isdigit():
                return None, 400
            length = int(lengths[0])
            if length > _MAX_BODY_BYTES:
                return None, 413
            raw = self.rfile.read(length)
            if len(raw) != length:
                return None, 400
            data = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
            return (data, None) if isinstance(data, dict) else (None, 400)
        except (OSError, ValueError, RecursionError):
            return None, 400

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
                    raw = central_snapshot.snapshot(self.server.root, datetime.now(timezone.utc).isoformat(), self.server.previous)
                    self.server.previous = raw
                    result = public_snapshot(raw)
                self._reply(200, result)
            except Exception:
                self._reply(503, {"error": "snapshot unavailable"})
            return
        if self.path == "/api/csrf":
            if self.headers.get("Origin") is not None and not self._origin_ok():
                self._reply(403, {"error": "origin refused"})
                return
            self._reply(200, {"csrf_token": self.server.csrf_token})
            return
        if self.path.startswith("/api/attach/"):
            attach_id = central_registry._canonical_uuid(self.path[len("/api/attach/"):])
            result = self.server.attach_results.get(attach_id) if attach_id else None
            self._reply(200, result) if result is not None else self._reply(404, {"error": "unknown attach"})
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
        routes = {"/api/message", "/api/answer", "/api/pause", "/api/resume", "/api/stop", "/api/attach"}
        if self.path not in routes:
            self._reply(404, {"error": "route not found"})
            return
        if not self._origin_ok():
            self._reply(403, {"error": "origin refused"})
            return
        body, error = self._read_json_body()
        if error:
            self._reply(error, {"error": "invalid request body"})
            return
        token = body.get("csrf_token")
        if not isinstance(token, str) or not token.isascii() or not secrets.compare_digest(token, self.server.csrf_token):
            self._reply(403, {"error": "token refused"})
            return
        fields = {"coordinator_id", "action_id", "csrf_token"}
        if self.path in ("/api/message", "/api/answer"):
            fields.add("text")
        if self.path == "/api/answer":
            fields.add("question_id")
        cid, aid = body.get("coordinator_id"), body.get("action_id")
        if set(body) != fields or central_registry._canonical_uuid(cid) is None or central_registry._canonical_uuid(aid) is None:
            self._reply(400, {"error": "invalid action fields"})
            return
        if "text" in fields:
            try:
                valid_text = (isinstance(body["text"], str) and bool(body["text"].strip())
                              and len(body["text"].encode("utf-8")) <= central_messages.BODY_MAX_BYTES)
            except UnicodeError:
                valid_text = False
            if not valid_text:
                self._reply(400, {"error": "invalid message text"})
                return
        if "question_id" in fields and central_registry._canonical_uuid(body["question_id"]) is None:
            self._reply(400, {"error": "invalid question"})
            return
        # Check membership before any action writes. Never expose registry paths.
        try:
            if cid not in central_registry.load_registry(self.server.root).get("coordinators", {}):
                self._reply(404, {"error": "unknown coordinator"})
                return
        except Exception:
            self._reply(503, {"error": "registry unavailable"})
            return
        signature = (self.path, {k: v for k, v in body.items() if k != "csrf_token"})
        with self.server.action_ids_lock:
            previous = self.server.action_ids.get(aid)
            if previous is not None:
                if previous[0] == signature:
                    self._reply(200, previous[1])
                else:
                    self._reply(409, {"error": "action id conflict"})
                return
            # Fail closed: evicting remembered actions could replay mutations.
            if len(self.server.action_ids) >= _MAX_ACTION_IDS:
                self._reply(503, {"error": "action session full; reopen the view"})
                return
            try:
                result = self._action(body)
            except queue.Full:
                self._reply(503, {"error": "terminal queue full"})
                return
            except (central_messages.MessageError, central_controls.ControlError, BackendError, ValueError):
                self._reply(400, {"error": "action refused"})
                return
            except Exception:
                self._reply(503, {"error": "action unavailable"})
                return
            self.server.action_ids[aid] = (signature, result)
            self._reply(200, result)

    def _action(self, body):
        cid = body["coordinator_id"]
        # Routed by the stored backend exactly as the CLI routes it (FR-017).
        row, backend = central_controls.registered_backend(self.server.root, cid)
        if backend is None:
            raise central_controls.ControlError("unknown coordinator")
        if self.path in ("/api/message", "/api/answer"):
            envelope = central_messages.owner_message(cid, body["text"], body.get("question_id"))
            envelope["id"] = body["action_id"]
            return {"ok": True, "message_id": backend.send(row, envelope)}
        if self.path in ("/api/pause", "/api/resume", "/api/stop"):
            return {"ok": True, "control_id": central_controls.request_control(self.server.root, cid, self.path.rsplit("/", 1)[1])}
        if self.server.attach_bridge_active:
            attach_id = body["action_id"]
            # Queued first: the terminal thread may publish its outcome as soon as it takes the request.
            self.server.attach_results[attach_id] = {"state": "queued"}
            try:
                self.server.attach_intents.put_nowait((cid, attach_id))
            except queue.Full:
                del self.server.attach_results[attach_id]
                raise
            return {"ok": True, "queued": True, "attach_id": attach_id}
        return {"ok": True, "queued": False, "command": attach_command(cid, self.server.central_dir)}


def attach_command(cid: str, central_dir=None) -> str:
    """The copyable ``acc attach`` line, quoted for this machine's shell.

    An explicit central dir becomes the absolute root ``acc`` opens (``~``
    expanded, a relative dir resolved against this view's directory). Under
    home it is shown as ``~/...``, which ``acc`` expands itself. A root outside
    home is shown in full: it is the owner's own ``--central-dir``, served only
    to this loopback browser, and without it the command opens another registry.
    """
    command = "acc attach " + cid
    if not central_dir:
        return command
    path = Path(os.path.abspath(central_config.resolve_root(central_dir, {})))
    try:
        shown = "~/" + path.relative_to(os.path.abspath(Path.home())).as_posix()
    except ValueError:
        shown = str(path)
    return command + " --central-dir " + _shell_quote(shown)


def _shell_quote(text: str) -> str:
    if os.name != "nt":
        return shlex.quote(text)
    if re.fullmatch(r"[\w.:\\/~-]+", text):
        return text
    # PowerShell, the Windows shell the docs use: a backtick keeps $ and ` literal.
    # shortcut: cmd.exe would expand %NAME% inside the quotes; upgrade if such roots appear.
    return '"' + re.sub(r"([`$])", r"`\1", text) + '"'


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
    server.action_ids = {}
    server.attach_intents = queue.Queue(maxsize=16)
    # Outcome of each queued attach, by its action id; bounded like action_ids.
    server.attach_results = {}
    server.action_ids_lock = threading.Lock()
    server.attach_bridge_active = bool(attach_bridge_active)
    server.central_dir = None  # an explicit --central-dir, echoed by the attach fallback
    return server
