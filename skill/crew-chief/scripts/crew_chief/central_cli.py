"""Public central CLI commands: launch, central, message, attach, pause, resume, stop, recover (T019).

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
``handle_central(args) -> int``
``add_central_subcommands(subparsers) -> None``
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path
from typing import Any

from . import (
    central_attach,
    central_config,
    central_controls,
    central_launch,
    central_messages,
    central_recovery,
    central_registry,
    central_screen,
    central_supervisor,
)

__all__ = [
    "add_central_subcommands",
    "handle_attach",
    "handle_central",
    "handle_control",
    "handle_launch",
    "handle_message",
]


#: The released bundle this command runs from (holds install.py and skill/).
_RELEASE_ROOT = Path(__file__).resolve().parents[4]


def _central_dir(args: argparse.Namespace) -> Path:
    """``--central-dir``, else the environment, else ``~/.config/agent-crew-chief/central``."""
    return central_config.resolve_root(getattr(args, "central_dir", None), os.environ)


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

    spec_dir = getattr(args, "spec_dir", None)
    if mode == "approved-spec" and not getattr(args, "approved", False):
        print("error: --approved is required for approved-spec mode", file=sys.stderr)
        return 2
    if mode in ("approved-spec", "direct-spec") and not spec_dir:
        print(f"error: --spec-dir is required for {mode} mode", file=sys.stderr)
        return 2
    if mode == "raw-request" and not getattr(args, "authorize_raw", False):
        print("error: --authorize-raw is required for raw-request mode", file=sys.stderr)
        return 2
    if mode not in ("approved-spec", "direct-spec", "raw-request"):
        print(f"error: unknown launch mode {mode!r}", file=sys.stderr)
        return 2

    # An unset flag is None so preferences, not a hard-coded default, decide.
    options: dict[str, Any] = {
        "mode": mode,
        "approved": bool(getattr(args, "approved", False)),
        "authorize_raw": bool(getattr(args, "authorize_raw", False)),
        "install_worktree_acc": bool(getattr(args, "install_worktree_acc", False)),
        "spec_dir": spec_dir,
        "base": getattr(args, "base", None),
        "cli": getattr(args, "cli", None),
        "backend": getattr(args, "backend", None),
        "restricted": True if getattr(args, "restricted", False) else None,
        "unrestricted": True if getattr(args, "unrestricted", False) else None,
        "release_root": Path(getattr(args, "release_root", None) or _RELEASE_ROOT),
    }

    prepared = central_launch.prepare_launch(repo, request_file, options, root)
    coordinator_id = prepared.get("id")
    if not prepared.get("ok"):
        where = f" (coordinator {coordinator_id})" if coordinator_id else ""
        print(f"error: launch failed{where}: {prepared.get('error')}", file=sys.stderr)
        return 1

    started = central_supervisor.start_supervisor(prepared["record"], root)
    if not started.get("ok"):
        print(
            f"error: supervisor start failed for {coordinator_id}: {started.get('error')}",
            file=sys.stderr,
        )
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
        canonical = central_messages._canonical_uuid(coordinator_id)
        registered = canonical in central_registry.load_registry(root).get("coordinators", {})
    except central_registry.RegistryError as exc:
        print(f"error: message failed: {exc}", file=sys.stderr)
        return 1
    if not registered:
        print(f"error: no coordinator {coordinator_id!r} registered", file=sys.stderr)
        return 2
    try:
        message_id = central_messages.send(
            root,
            coordinator_id,
            central_messages.owner_message(coordinator_id, text, reply_to),
        )
    except (central_messages.MessageError, OSError) as exc:
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
        print(f"session of {coordinator_id} closed; back in the background")
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
        # Recovery is an owner decision on the registry; controls only steer live owners.
        try:
            row = central_registry.load_registry(root).get("coordinators", {}).get(coordinator_id)
        except central_registry.RegistryError as exc:
            print(f"error: recover failed: {exc}", file=sys.stderr)
            return 1
        if not isinstance(row, dict):
            print(f"error: no coordinator {coordinator_id!r} registered", file=sys.stderr)
            return 2
        state = row.get("state")
        if state not in {"failed", "stopped"}:
            print(
                f"error: coordinator is {state!r}; recover applies only to failed/stopped",
                file=sys.stderr,
            )
            return 2
        try:
            result = central_recovery.recover(root, coordinator_id, "restart")
        except central_recovery.RecoveryError as exc:
            print(f"error: recover failed: {exc}", file=sys.stderr)
            return 2
        if not result.get("ok"):
            print(f"error: recover failed: {result.get('reason')}", file=sys.stderr)
            return 1
        if getattr(args, "json", False):
            print(json.dumps({"ok": True, "action": "recover", "state": result.get("state")}, indent=2))
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
# central (terminal screen)
# ---------------------------------------------------------------------------


def handle_central(args: argparse.Namespace, browser_open: Any = None) -> int:
    """Open the refreshing central screen, or serve the web view."""
    web = bool(getattr(args, "web", False))
    if getattr(args, "port", None) is not None and not web:
        print("error: --port requires --web", file=sys.stderr)
        return 2
    if web and (getattr(args, "json", False) or getattr(args, "plain", False)
                or getattr(args, "once", False)):
        print("error: --web is incompatible with --json, --plain and --once", file=sys.stderr)
        return 2
    if web:
        port = getattr(args, "port", None)
        if port is None:
            port = 0
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            print(f"error: invalid port: {port!r}", file=sys.stderr)
            return 2
        poll_raw = getattr(args, "poll", 1.0)
        try:
            poll = float(poll_raw if poll_raw is not None else 1.0)
        except (TypeError, ValueError):
            print(f"error: invalid poll interval: {poll_raw!r}", file=sys.stderr)
            return 2
        if not math.isfinite(poll) or poll <= 0:
            print(f"error: invalid poll interval: {poll_raw!r}", file=sys.stderr)
            return 2
        if browser_open is None:
            browser_open = getattr(args, "browser_open", None)
        root = _central_dir(args)
        release_root = Path(getattr(args, "release_root", None) or _RELEASE_ROOT)
        assets = release_root / "skill" / "crew-chief" / "web"
        return central_screen.run_web(
            root,
            {"poll": poll},
            assets=assets,
            port=port,
            browser_open=browser_open,
        )
    options = {"once": args.once, "plain": args.plain, "poll": args.poll}
    return central_screen.run_screen(_central_dir(args), options)



# ---------------------------------------------------------------------------
# supervisor (private entry)
# ---------------------------------------------------------------------------


def _handle_supervisor(args: argparse.Namespace) -> int:
    """Private entry used by detached supervisors; not part of the public CLI."""
    return central_supervisor.supervisor_main(
        ["--central-dir", args.root, "--coordinator", args.coordinator_id]
    )


# ---------------------------------------------------------------------------
# parser wiring
# ---------------------------------------------------------------------------


def add_central_subcommands(subparsers: Any, common: Any = None) -> None:
    """Register the central subcommands on an existing argparse subparsers group.

    ``common`` (``--project``/``--json``) is shared with the other commands
    when given.
    """
    parents = [common] if common is not None else []
    launch_parser = subparsers.add_parser(
        "launch", parents=parents, help="start a new coordinator"
    )
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
    launch_parser.add_argument(
        "--release-root",
        help="released ACC bundle the worktree must match (default: this one)",
    )
    launch_parser.add_argument("--central-dir", help="override central storage directory")
    launch_parser.set_defaults(handler=handle_launch)

    central_parser = subparsers.add_parser(
        "central", parents=parents, help="watch every coordinator in one screen"
    )
    central_parser.add_argument(
        "--once", action="store_true", help="print one snapshot and exit"
    )
    central_parser.add_argument(
        "--plain", action="store_true", help="no colors or screen clearing"
    )
    central_parser.add_argument(
        "--poll", type=float, default=1.0, help="seconds between refreshes"
    )
    central_parser.add_argument(
        "--web", action="store_true", help="serve the loopback web view"
    )
    central_parser.add_argument(
        "--port", type=int, default=None, help="port for --web (default: ephemeral)"
    )
    central_parser.add_argument("--central-dir", help="override central storage directory")
    central_parser.set_defaults(handler=handle_central)

    message_parser = subparsers.add_parser("message", parents=parents, help="send a message to a coordinator")
    message_parser.add_argument("id", help="coordinator id")
    message_parser.add_argument("--text", required=True, help="message body")
    message_parser.add_argument("--reply-to", help="question id this replies to")
    message_parser.add_argument("--central-dir", help="override central storage directory")
    message_parser.set_defaults(handler=handle_message)

    attach_parser = subparsers.add_parser(
        "attach", parents=parents, help="enter a coordinator conversation interactively"
    )
    attach_parser.add_argument("id", help="coordinator id")
    attach_parser.add_argument(
        "--timeout", type=float, default=30.0, help="seconds to wait for yield"
    )
    attach_parser.add_argument("--central-dir", help="override central storage directory")
    attach_parser.set_defaults(handler=handle_attach)

    for action in ("pause", "resume", "stop", "recover"):
        ctrl = subparsers.add_parser(action, parents=parents, help=f"{action} a coordinator")
        ctrl.add_argument("id", help="coordinator id")
        ctrl.add_argument("--central-dir", help="override central storage directory")
        ctrl.set_defaults(handler=handle_control, action=action)

    # Private supervisor entry — never documented in public help.
    sup = subparsers.add_parser("_supervisor", help=argparse.SUPPRESS)
    sup.add_argument("--coordinator-id", required=True)
    sup.add_argument("--root", required=True)
    sup.set_defaults(handler=_handle_supervisor)