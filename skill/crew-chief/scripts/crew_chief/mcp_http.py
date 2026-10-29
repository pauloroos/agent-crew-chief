"""Authenticated loopback HTTP transport for the JSON-RPC 2.0 MCP server.

Implements FR-004 and FR-001: the server binds to ``127.0.0.1`` only, requires
a bearer token on every request, rejects non-loopback ``Host``/``Origin``
headers with 403, and never emits CORS headers.  The HTTP layer is transport
only — message handling is delegated to :meth:`MCPServer.dispatch`.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, TextIO

from .mcp_server import MCPServer

#: Hostnames that count as loopback for the ``Host`` header check.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

#: Maximum allowed HTTP request body size (10 MB).
MAX_PAYLOAD_SIZE = 10 * 1024 * 1024

_IS_WINDOWS = os.name == "nt"


def _is_allowed_host(host_header: str, allowed_hosts: frozenset[str] | set[str]) -> bool:
    """Return True when *host_header* names an allowed address (loopback or configured)."""
    if not host_header:
        return False
    name = host_header.split(":", 1)[0].strip()
    if name.startswith("[") and name.endswith("]"):
        name = name[1:-1]
    return name.lower() in allowed_hosts


class _MCPHTTPRequestHandler(BaseHTTPRequestHandler):
    """POST-only handler that enforces auth/host checks before dispatching."""

    #: Set by the factory returned from :func:`_make_handler_factory`.
    server_version = "ACCMCPHTTP/0.1"
    sys_version = ""  # suppress Python/x.y.z in the Server header

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Silence per-request logs; never log tokens or request details."""
        return

    # -- response helpers ---------------------------------------------------

    def _send_json(self, status: int, body: dict[str, Any]) -> None:
        """Write *body* as compact JSON with the given HTTP status code."""
        payload = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        # No CORS headers — deliberate per FR-001.
        self.end_headers()
        self.wfile.write(payload)

    def _reject(self, status: int, message: str) -> None:
        """Send a plain error response without CORS headers."""
        self._send_json(status, {"error": message})

    # -- request handling ---------------------------------------------------

    def do_POST(self) -> None:  # noqa: N802 — stdlib naming convention
        """Authenticate, validate origin, parse JSON-RPC and dispatch."""
        token = getattr(self.server, "_auth_token", "")
        origin_ok = self._check_origin()
        if not origin_ok:
            return  # response already sent

        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or not secrets.compare_digest(
            auth[7:].strip(), token
        ):
            self._reject(401, "Unauthorized: missing or invalid bearer token")
            return

        content_len_hdr = self.headers.get("Content-Length")
        if content_len_hdr is None:
            self._reject(400, "Bad Request: missing Content-Length header")
            return
        try:
            length = int(content_len_hdr)
        except (ValueError, TypeError):
            self._reject(400, "Bad Request: invalid Content-Length header")
            return

        if length < 0:
            self._reject(400, "Bad Request: negative Content-Length")
            return

        if length > MAX_PAYLOAD_SIZE:
            self._reject(413, "Payload Too Large: Content-Length exceeds maximum allowed size")
            return

        try:
            raw = self.rfile.read(length) if length > 0 else b""
        except Exception:
            self._reject(400, "Bad Request: unable to read body")
            return

        try:
            data = json.loads(raw) if raw else {}
        except Exception:
            self._send_json(
                200,
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": "Parse error"},
                },
            )
            return

        if not isinstance(data, dict):
            self._send_json(
                200,
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": -32600,
                        "message": "Invalid Request: message must be a JSON object",
                    },
                },
            )
            return

        mcp_server: MCPServer = self.server._mcp_server  # type: ignore[attr-defined]
        response = mcp_server.dispatch(data)
        if response is None:
            # Notification — acknowledge with 204 so the client knows we saw it.
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        self._send_json(200, response)

    # Reject every other verb explicitly so probes don't leak information.
    def do_GET(self) -> None:  # noqa: N802
        """Reject GET requests."""
        self._reject(405, "Method Not Allowed")

    def do_OPTIONS(self) -> None:  # noqa: N802
        """Reject OPTIONS (no CORS preflight support)."""
        self._reject(405, "Method Not Allowed")

    def do_PUT(self) -> None:  # noqa: N802
        """Reject PUT requests."""
        self._reject(405, "Method Not Allowed")

    def do_DELETE(self) -> None:  # noqa: N802
        """Reject DELETE requests."""
        self._reject(405, "Method Not Allowed")

    # -- security checks ----------------------------------------------------

    def _check_origin(self) -> bool:
        """Validate Host and Origin headers against allowed names.

        Returns True when the request should proceed. Sends a 403 response
        and returns False otherwise.
        """
        allowed = getattr(self.server, "_allowed_hosts", _LOOPBACK_HOSTS)
        host = self.headers.get("Host", "")
        if not _is_allowed_host(host, allowed):
            self._reject(403, "Forbidden: non-loopback Host header")
            return False

        origin = self.headers.get("Origin")
        if origin is not None:
            try:
                from urllib.parse import urlparse

                parsed = urlparse(origin)
                origin_host = parsed.hostname or ""
            except Exception:
                origin_host = ""
            if not _is_allowed_host(origin_host, allowed):
                self._reject(403, "Forbidden: non-loopback Origin header")
                return False

        return True


def serve_http(
    server: MCPServer | None = None,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    token: str | None = None,
    token_file: str | Path | None = None,
    allowed_hosts: set[str] | frozenset[str] | None = None,
    projects_dir: str | Path | None = None,
    _ready: dict[str, Any] | None = None,
    stdout: TextIO | None = None,
) -> int:
    """Run the MCP server over authenticated loopback HTTP.

    Parameters
    ----------
    server:
        The :class:`MCPServer` instance to dispatch messages to. A default
        instance is created when omitted.
    host:
        Bind address. Defaults to ``127.0.0.1``. Binding to non-loopback
        (such as ``0.0.0.0``) requires a token loaded from a 0600-permission file.
    port:
        TCP port. ``0`` lets the OS choose a free port.
    token:
        Bearer token required on every request.
    token_file:
        Path to a 0600-permission file containing the bearer token.
    allowed_hosts:
        Hostnames accepted in Host/Origin headers in addition to loopback and 'acc'.
    _ready:
        Optional dict populated with ``{"port": <int>, "token": <str>}`` once
        the server is accepting connections.
    stdout:
        Stream used to announce the listening address. Defaults to
        :data:`sys.stdout`.
    """
    resolved_token = token
    if token_file is not None:
        tf = Path(token_file).resolve()
        if not tf.is_file():
            raise ValueError(f"token_file does not exist or is not a file: {token_file}")
        if not _IS_WINDOWS:
            st = os.stat(tf)
            mode = st.st_mode & 0o777
            if mode != 0o600:
                raise ValueError(
                    f"token_file must have restricted permissions 0600, got {oct(mode)}"
                )
        else:
            warnings.warn(
                f"token_file permissions are not checked on Windows: {tf}",
                UserWarning,
                stacklevel=2,
            )
        file_token = tf.read_text(encoding="utf-8").strip()
        if not file_token:
            raise ValueError(f"token_file is empty: {token_file}")
        resolved_token = file_token

    # Verify non-loopback bind
    is_loopback = host.strip().lower() in ("127.0.0.1", "localhost", "::1")
    if not is_loopback:
        if token_file is None:
            raise ValueError(
                f"non-loopback bind address ({host}) requires a bearer token loaded from a 0600-permission file (--token-file)"
            )

    if server is None:
        server = MCPServer(projects_dir=projects_dir)
    elif projects_dir is not None and server.projects_dir is None:
        server.projects_dir = str(projects_dir)
    if resolved_token is None:
        resolved_token = secrets.token_urlsafe(32)

    out = stdout if stdout is not None else sys.stdout

    effective_allowed = set(_LOOPBACK_HOSTS) | {"acc"}
    if allowed_hosts:
        effective_allowed.update(h.lower() for h in allowed_hosts)

    class _BoundHandler(_MCPHTTPRequestHandler):
        """Handler subclass that carries references to the MCP server/token."""

    httpd = ThreadingHTTPServer((host, port), _BoundHandler)
    httpd._mcp_server = server  # type: ignore[attr-defined]
    httpd._auth_token = resolved_token  # type: ignore[attr-defined]
    httpd._allowed_hosts = frozenset(effective_allowed)  # type: ignore[attr-defined]

    bound_port = httpd.server_address[1]
    if _ready is not None:
        _ready["port"] = bound_port
        _ready["token"] = resolved_token

    out.write(f"MCP HTTP server listening on http://{host}:{bound_port}\n")
    out.flush()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()

    return 0