"""JSON-RPC 2.0 MCP (Model Context Protocol) server implementation.

Provides standard stdio transport handling newline-delimited JSON-RPC messages.
"""

from __future__ import annotations

import contextlib
import json
import sys
from typing import Any, Callable, TextIO

from . import __version__

LATEST_PROTOCOL_VERSION = "2024-11-05"
SUPPORTED_PROTOCOL_VERSIONS = {LATEST_PROTOCOL_VERSION}

# Standard JSON-RPC 2.0 error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

TOOLS_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "start_job",
        "description": "Start a new coordinator job or begin an interactive interview.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Project name or directory path"},
                "request": {"type": "string", "description": "Request description"},
                "coordinator": {"type": "string", "description": "Coordinator model or identifier"},
                "executors": {"type": "string", "description": "Executor mode: own, subagents, or free_first"},
                "end_mode": {"type": "string", "description": "End mode: pr or pr_review"},
                "title": {"type": "string", "description": "Optional job title"},
            },
        },
    },
    {
        "name": "questions",
        "description": "List open interview questions for a job or coordinators.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "Job ID"},
                "project": {"type": "string", "description": "Project path"},
            },
        },
    },
    {
        "name": "answer",
        "description": "Submit an answer to an interview question or send a coordinator message.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "Job ID"},
                "project": {"type": "string", "description": "Project path"},
                "key": {"type": "string", "description": "Question key being answered"},
                "value": {"type": "string", "description": "Answer value"},
            },
        },
    },
    {
        "name": "status",
        "description": "Check current state and pending questions for a job.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "Job ID"},
                "project": {"type": "string", "description": "Project path"},
            },
        },
    },
    {
        "name": "events",
        "description": "Replay strictly monotonic append-only events using cursor pagination.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "Job ID"},
                "cursor": {"type": "integer", "description": "Sequence cursor", "default": 0},
                "project": {"type": "string", "description": "Project path"},
            },
        },
    },
    {
        "name": "result",
        "description": "Retrieve results for a finished job (PR URL, summary, tests).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "Job ID"},
                "coordinator_id": {"type": "string", "description": "Coordinator ID"},
                "project": {"type": "string", "description": "Project path"},
            },
        },
    },
    {
        "name": "stop",
        "description": "Stop execution of a job or coordinator.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "Job ID"},
                "coordinator_id": {"type": "string", "description": "Coordinator ID"},
                "project": {"type": "string", "description": "Project path"},
            },
        },
    },
    {
        "name": "list_jobs",
        "description": "List existing jobs and coordinators.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Project path"},
            },
        },
    },
]


class MCPServer:
    """JSON-RPC 2.0 server for Model Context Protocol."""

    def __init__(
        self,
        name: str = "agent-crew-chief",
        version: str = __version__,
        project: str | None = None,
        projects_dir: str | Path | None = None,
        central_dir: str | Path | None = None,
    ) -> None:
        self.name = name
        self.version = version
        self.project = project
        self.projects_dir = str(projects_dir) if projects_dir is not None else None
        self.central_dir = str(central_dir) if central_dir is not None else None
        self.initialized = False
        self.client_info: dict[str, Any] | None = None
        self._handlers: dict[str, Callable[[Any], Any]] = {
            "initialize": self._handle_initialize,
            "ping": self._handle_ping,
            "tools/list": self._handle_tools_list,
            "tools/call": self._handle_tools_call,
        }
        self._notification_handlers: dict[str, Callable[[Any], None]] = {
            "notifications/initialized": self._handle_initialized,
            "initialized": self._handle_initialized,
        }

    def register_handler(self, method: str, handler: Callable[[Any], Any]) -> None:
        """Register a request handler for a given JSON-RPC method."""
        self._handlers[method] = handler

    def register_notification(
        self, method: str, handler: Callable[[Any], None]
    ) -> None:
        """Register a notification handler for a given JSON-RPC method."""
        self._notification_handlers[method] = handler

    def _handle_initialize(self, params: Any) -> dict[str, Any]:
        """Handle MCP initialize request and return protocol handshake result."""
        requested_version = ""
        if isinstance(params, dict):
            requested_version = str(params.get("protocolVersion", ""))
            client = params.get("clientInfo")
            if isinstance(client, dict):
                self.client_info = client

        protocol_version = (
            requested_version
            if requested_version in SUPPORTED_PROTOCOL_VERSIONS
            else LATEST_PROTOCOL_VERSION
        )

        return {
            "protocolVersion": protocol_version,
            "capabilities": {
                "tools": {},
            },
            "serverInfo": {
                "name": self.name,
                "version": self.version,
            },
        }

    def _handle_initialized(self, params: Any) -> None:
        """Handle MCP initialized notification from the client."""
        self.initialized = True

    def _handle_ping(self, params: Any) -> dict[str, Any]:
        """Handle MCP ping request."""
        return {}

    def _handle_tools_list(self, params: Any) -> dict[str, Any]:
        """Handle tools/list request returning registered tools."""
        return {"tools": TOOLS_DEFINITIONS}

    def _handle_tools_call(self, params: Any) -> dict[str, Any]:
        """Handle tools/call request executing the specified tool handler."""
        if not isinstance(params, dict):
            raise ValueError("Invalid params for tools/call")
        name = params.get("name")
        raw_arguments = params.get("arguments", {})
        if not isinstance(raw_arguments, dict):
            raw_arguments = {}

        arguments = dict(raw_arguments)
        # Untrusted client may not supply projects_dir or root kwargs to replace policy/storage context
        arguments.pop("projects_dir", None)
        arguments.pop("root", None)

        if self.projects_dir is not None:
            arguments["projects_dir"] = self.projects_dir

        if self.central_dir is not None:
            arguments["central_dir"] = self.central_dir

        if self.project and "project" not in arguments:
            arguments["project"] = self.project

        from . import mcp_tools, mcp_errors

        allowed_names = {t.get("name") for t in TOOLS_DEFINITIONS}
        if not isinstance(name, str) or name not in allowed_names:
            detail = f"Unknown tool: {name!r}"
            return {
                "content": [{"type": "text", "text": detail}],
                "isError": True,
                "ok": False,
                "error": detail,
            }

        fn = getattr(mcp_tools, name, None)
        if fn is None or not callable(fn):
            detail = f"Unknown tool: {name!r}"
            return {
                "content": [{"type": "text", "text": detail}],
                "isError": True,
                "ok": False,
                "error": detail,
            }

        tool_def = next(
            (t for t in TOOLS_DEFINITIONS if t.get("name") == name), None
        )
        if tool_def is not None:
            schema_props = (
                tool_def.get("inputSchema", {}).get("properties", {}) or {}
            )
            allowed = set(schema_props.keys()) | {
                "projects_dir",
                "central_dir",
                "project",
            }
            unknown = sorted(set(arguments.keys()) - allowed)
            if unknown:
                detail = (
                    f"Unknown argument(s) for tool {name!r}: "
                    f"{', '.join(unknown)}"
                )
                return {
                    "content": [{"type": "text", "text": detail}],
                    "isError": True,
                    "ok": False,
                    "error": detail,
                }

        try:
            res = fn(**arguments)
            is_err = bool(res.get("isError", False) or (res.get("ok") is False))
            content_text = json.dumps(res, ensure_ascii=False)
            return {
                "content": [{"type": "text", "text": content_text}],
                "isError": is_err,
                **res,
            }
        except Exception as exc:
            sanitized = mcp_errors.sanitize_error(exc)
            return {
                "content": [{"type": "text", "text": sanitized["message"]}],
                "isError": True,
                "error": sanitized,
                "ok": False,
            }

    def dispatch(self, data: dict[str, Any]) -> dict[str, Any] | None:
        """Dispatch a single parsed JSON-RPC message object.

        Returns a response dict if the message is a request, or None if it is
        a notification or response that requires no reply.
        """
        req_id = data.get("id")
        is_notification = "id" not in data

        if data.get("jsonrpc") != "2.0":
            if is_notification:
                return None
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": INVALID_REQUEST,
                    "message": "Invalid Request: missing or invalid jsonrpc version",
                },
            }

        method = data.get("method")
        if not isinstance(method, str) or not method:
            if is_notification:
                return None
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": INVALID_REQUEST,
                    "message": "Invalid Request: missing or invalid method",
                },
            }

        params = data.get("params")

        if is_notification:
            notif_handler = self._notification_handlers.get(method)
            if notif_handler is not None:
                try:
                    notif_handler(params)
                except Exception:
                    pass
            return None

        handler = self._handlers.get(method)
        if handler is None:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": METHOD_NOT_FOUND,
                    "message": f"Method not found: {method}",
                },
            }

        try:
            result = handler(params)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": result,
            }
        except Exception as exc:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": INTERNAL_ERROR,
                    "message": f"Internal error: {exc}",
                },
            }

    def handle_message(self, raw: str) -> dict[str, Any] | None:
        """Parse raw line and return response dict if message expects a reply."""
        try:
            data = json.loads(raw)
        except Exception:
            return {
                "jsonrpc": "2.0",
                "id": None,
                "error": {
                    "code": PARSE_ERROR,
                    "message": "Parse error",
                },
            }

        if not isinstance(data, dict):
            return {
                "jsonrpc": "2.0",
                "id": None,
                "error": {
                    "code": INVALID_REQUEST,
                    "message": "Invalid Request: message must be a JSON object",
                },
            }

        return self.dispatch(data)


def serve_stdio(
    server: MCPServer | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    *,
    projects_dir: str | Path | None = None,
) -> int:
    """Run the MCP server over stdio newline-delimited JSON-RPC.

    Reads lines from stdin until EOF, dispatches messages to ``server``,
    and writes responses to stdout delimited by newlines.
    """
    if server is None:
        server = MCPServer(projects_dir=projects_dir)
    elif projects_dir is not None and server.projects_dir is None:
        server.projects_dir = str(projects_dir)
    inp = stdin if stdin is not None else sys.stdin
    out = stdout if stdout is not None else sys.stdout

    while True:
        line = inp.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        with contextlib.redirect_stdout(sys.stderr):
            response = server.handle_message(line)
        if response is not None:
            out.write(json.dumps(response, separators=(",", ":"), ensure_ascii=False) + "\n")
            out.flush()

    return 0
