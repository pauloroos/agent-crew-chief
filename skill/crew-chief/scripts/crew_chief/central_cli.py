"""Public central CLI commands: launch, message, attach, pause, resume, stop, recover (T019).

This module owns the argparse handlers for the ``acc`` central subcommands.
Each handler validates its arguments, delegates to the appropriate lifecycle
module and returns an exit code. The supervisor entry point is private and
not exposed through the public parser.

Public API
----------
``handle_launch(args) -> int``
``handle_message(args) -> int``
``handle_attach(args) -> int``
``handle_control(args) -> int``
``add_central_subcommands(subparsers) -> None``
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import (
    central_attach,
    central_controls,
    central_launch,
    central_messages,
    central_registry,
    central_supervisor,
)

__all__ = [
    "add_central_subcommands",
    "handle_attach",
    "handle_control",
    "handle_launch",
    "handle_message",
]


def _project_root(args: argparse.Namespace) -> Path:
    return Path(getattr(args, "project", ".") or ".")


def _central_dir(args: argparse.Namespace) -> Path:
    explicit = getattr(args, "central_dir", None)
    if explicit:
        return Path(explicit)
    return _project_root(args) / ".crew-chief"


# ---------------------------------------------------------------------------
# launch
# ---------------------------------------------------------------------------


def handle_launch(args: argparse.Namespace) -> int:
    """Validate mode/authorization and start one coordinator supervisor."""
    root = _central_dir(args)
    repo = Path(args.repo)
    request_file = Path(args.request_file)
    mode = args.mode

    if not repo.exists():
        print(f"error: repo path does not exist: {repo}", file=sys.stderr)
        return 2
    if not request_file.is_file():
        print(f"error: request file not found: {request_file}", file=sys.stderr)
        return 2

    authorization: dict[str, Any] = {"mode": mode}
    if mode == "approved-spec":
        if not getattr(args, "approved", False):
            print(
                "error: --approved is required for approved-spec mode",
                file=sys.stderr,
            )
            return 2
        spec_dir = getattr(args, "spec_dir", None)
        if not spec_dir:
            print(
                "error: --spec-dir is required for approved-spec mode",
                file=sys.stderr,
            )
            return 2
        authorization["spec_dir"] = str(Path(spec_dir))
        authorization["approved"] = True
    elif mode == "direct-spec":
        spec_dir = getattr(args, "spec_dir", None)
        if not spec_dir:
            print(
                "error: --spec-dir is required for direct-spec mode",
                file=sys.stderr,
            )
            return 2
        authorization["spec_dir"] = str(Path(spec_dir))
    elif mode == "raw-request":
        if not getattr(args, "authorize_raw", False):
            print(
                "error: --authorize-raw is required for raw-request mode",
                file=sys.stderr,
            )
            return 2
        authorization["authorize_raw"] = True
    else:
        print(f"error: unknown launch mode {mode!r}", file=sys.stderr)
        return 2

    cli = getattr(args, "cli", None) or "claude"
    backend = getattr(args, "backend", None) or "default"
    base = getattr(args, "base", None)
    restricted = getattr(args, "restricted", False)
    unrestricted = getattr(args, "unrestricted", False)
    install_worktree_acc = getattr(args, "install_worktree_acc", False)

    try:
        record = central_launch.prepare_launch(
            root=root,
            repo=repo,
            request_file=request_file,
            mode=mode,
            authorization=authorization,
            cli=cli,
            backend=backend,
            base=base,
            restricted=restricted,
            unrestricted=unrestricted,
            install_worktree_acc=install_worktree_acc,
        )
    except Exception as exc:
        print(f"error: launch failed: {exc}", file=sys.stderr)
        return 1

    coordinator_id = record.get("id")
    if not coordinator_id:
        print("error: launch produced no coordinator id", file=sys.stderr)
        return 1

    try:
        central_supervisor.start_supervisor(record, root)
    except Exception as exc:
        print(f"error: supervisor start failed: {exc}", file=sys.stderr)
        return 1

    if getattr(args, "json", False):
        print(json.dumps({"ok": True, "coordinator_id": coordinator_id}, indent=2))
    else:
        print(f"launched coordinator {coordinator_id}")
    return 0


# ---------------------------------------------------------------------------
# message
# ---------------------------------------------------------------------------


def handle_message(args: argparse.Namespace) -> int:
    """Send a text message to a coordinator's inbox."""
    root = _central_dir(args)
    coordinator_id = args.id
    text = args.text
    reply_to = getattr(args, "reply_to", None)

    try:
        message_id = central_messages.send(
            root=root,
            coordinator_id=coordinator_id,
            text=text,
            reply_to=reply_to,
        )
    except Exception as exc:
        print(f"error: message failed: {exc}", file=sys.stderr)
        return 1

    if getattr(args, "json", False):
        print(json.dumps({"ok": True, "message_id": message_id}, indent=2))
    else:
        print(f"sent message {message_id}")
    return 0


# ---------------------------------------------------------------------------
# attach
# ---------------------------------------------------------------------------


def handle_attach(args: argparse.Namespace) -> int:
    """Yield exclusive ownership to the current terminal for interactive resume."""
    root = _central_dir(args)
    coordinator_id = args.id
    timeout_s = float(getattr(args, "timeout", 30) or 30)

    class _StdTerminal:
        stdin = sys.stdin
        stdout = sys.stdout

    result = central_attach.attach(root, coordinator_id, _StdTerminal(), timeout_s)
    ok = result.get("ok") is True

    if getattr(args, "json", False):
        print(json.dumps(result, indent=2, default=str))
    elif ok:
        print(f"attached to {coordinator_id}")
    else:
        reason = result.get("reason", "unknown")
        print(f"attach failed: {reason}", file=sys.stderr)
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# control (pause / resume / stop / recover)
# ---------------------------------------------------------------------------


def handle_control(args: argparse.Namespace) -> int:
    """Route pause, resume, stop or recover to the live owner via control files."""
    root = _central_dir(args)
    coordinator_id = args.id
    action = args.action

    if action == "recover":
        # Recovery is a registry state transition; controls only steer live owners.
        try:
            record = central_registry.load_registry(root)
            coordinators = record.get("coordinators", {})
            row = coordinators.get(coordinator_id)
            if not isinstance(row, dict):
                print(
                    f"error: no coordinator {coordinator_id!r} registered",
                    file=sys.stderr,
                )
                return 2
            state = row.get("state")
            if state not in {"failed", "stopped"}:
                print(
                    f"error: coordinator is {state!r}; recover applies only to failed/stopped",
                    file=sys.stderr,
                )
                return 2
            central_registry.update_record(root, coordinator_id, {"state": "created"})
        except Exception as exc:
            print(f"error: recover failed: {exc}", file=sys.stderr)
            return 1
        if getattr(args, "json", False):
            print(json.dumps({"ok": True, "action": "recover"}, indent=2))
        else:
            print(f"recovered {coordinator_id}")
        return 0

    if action not in central_controls.VALID_COMMANDS:
        print(
            f"error: unknown control action {action!r}; expected one of {sorted(central_controls.VALID_COMMANDS)}",
            file=sys.stderr,
        )
        return 2

    try:
        message_id = central_controls.request_control(root, coordinator_id, action)
    except Exception as exc:
        print(f"error: {action} failed: {exc}", file=sys.stderr)
        return 1

    if getattr(args, "json", False):
        print(
            json.dumps({"ok": True, "action": action, "control_id": message_id}, indent=2)
        )
    else:
        print(f"{action} requested for {coordinator_id}")
    return 0


# ---------------------------------------------------------------------------
# supervisor (private entry)
# ---------------------------------------------------------------------------


def _handle_supervisor(args: argparse.Namespace) -> int:
    """Private entry used by detached supervisors; not part of the public CLI."""
    return central_supervisor.supervisor_main(
        [getattr(args, "coordinator_id", ""), getattr(args, "root", "")]
    )


# ---------------------------------------------------------------------------
# parser wiring
# ---------------------------------------------------------------------------


def add_central_subcommands(subparsers: Any) -> None:
    """Register the central subcommands on an existing argparse subparsers group."""
    launch_parser = subparsers.add_parser("launch", help="start a new coordinator")
    launch_parser.add_argument("--repo", required=True, help="path to the source repository")
    launch_parser.add_argument(
        "--request-file", required=True, help="path to the request/spec file"
    )
    launch_parser.add_argument(
        "--mode",
        required=True,
        choices=("approved-spec", "direct-spec", "raw-request"),
        help="launch authorization mode",
    )
    launch_parser.add_argument("--spec-dir", help="spec directory (required for spec modes)")
    launch_parser.add_argument(
        "--approved", action="store_true", help="confirm approved-spec authorization"
    )
    launch_parser.add_argument(
        "--authorize-raw", action="store_true", help="confirm raw-request authorization"
    )
    launch_parser.add_argument("--cli", help="coordinator CLI (default: claude)")
    launch_parser.add_argument("--backend", help="execution backend (default: default)")
    launch_parser.add_argument("--base", help="base revision for the coordinator branch")
    launch_parser.add_argument(
        "--restricted", action="store_true", help="run in restricted mode"
    )
    launch_parser.add_argument(
        "--unrestricted", action="store_true", help="run in unrestricted mode"
    )
    launch_parser.add_argument(
        "--install-worktree-acc",
        action="store_true",
        help="install ACC into the worktree without prompting",
    )
    launch_parser.add_argument("--central-dir", help="override central storage directory")
    launch_parser.set_defaults(handler=handle_launch)

    message_parser = subparsers.add_parser("message", help="send a message to a coordinator")
    message_parser.add_argument("id", help="coordinator id")
    message_parser.add_argument("--text", required=True, help="message body")
    message_parser.add_argument("--reply-to", help="question id this replies to")
    message_parser.add_argument("--central-dir", help="override central storage directory")
    message_parser.set_defaults(handler=handle_message)

    attach_parser = subparsers.add_parser(
        "attach", help="enter a coordinator conversation interactively"
    )
    attach_parser.add_argument("id", help="coordinator id")
    attach_parser.add_argument(
        "--timeout", type=float, default=30.0, help="seconds to wait for yield"
    )
    attach_parser.add_argument("--central-dir", help="override central storage directory")
    attach_parser.set_defaults(handler=handle_attach)

    for action in ("pause", "resume", "stop", "recover"):
        ctrl = subparsers.add_parser(action, help=f"{action} a coordinator")
        ctrl.add_argument("id", help="coordinator id")
        ctrl.add_argument("--central-dir", help="override central storage directory")
        ctrl.set_defaults(handler=handle_control, action=action)

    # Private supervisor entry — never documented in public help.
    sup = subparsers.add_parser("_supervisor", help=argparse.SUPPRESS)
    sup.add_argument("--coordinator-id", required=True)
    sup.add_argument("--root", required=True)
    sup.set_defaults(handler=_handle_supervisor)