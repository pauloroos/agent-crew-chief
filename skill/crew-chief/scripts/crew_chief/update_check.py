"""Bounded check of GitHub's latest stable release; the only external HTTP."""

from __future__ import annotations

import http.client
import json
import os
import ssl
import subprocess
import sys
import threading
from pathlib import Path
from urllib.parse import urlsplit

from . import update_cache as _cache

FIXED_ENDPOINT = "https://api.github.com/repos/pauloroos/agent-crew-chief/releases/latest"
_MAX_RESPONSE_BYTES = 64 * 1024
_USER_AGENT = "agent-crew-chief"
_ACCEPT = "application/json"


def parse_release(payload: object) -> str | None:
    """Return the canonical stable version from a GitHub release payload."""
    if isinstance(payload, (bytes, bytearray)):
        if len(payload) > _MAX_RESPONSE_BYTES:
            return None
        try:
            text = bytes(payload).decode("utf-8-sig")
        except (UnicodeDecodeError, ValueError):
            return None
    elif isinstance(payload, str):
        if len(payload.encode("utf-8")) > _MAX_RESPONSE_BYTES:
            return None
        text = payload
    else:
        return None
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    if any(data.get(flag, False) is not False for flag in ('draft', 'prerelease')):
        return None
    tag = data.get("tag_name")
    if not isinstance(tag, str):
        return None
    version = _cache.version_tuple(tag)
    if version is None:
        return None
    return "v" + ".".join(map(str, version))


def is_newer(latest: object, current: object) -> bool:
    new = _cache.version_tuple(latest)
    old = _cache.version_tuple(current)
    if new is None or old is None:
        return False
    return new > old


def _fetch(endpoint: object, timeout_s: float) -> bytes:
    url = str(endpoint)
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("unsupported scheme")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("credentials in URL are not allowed")
    host = parsed.hostname
    if not host:
        raise ValueError("missing host")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path = path + "?" + parsed.query
    timeout = float(timeout_s)
    if parsed.scheme == "https":
        conn = http.client.HTTPSConnection(
            host, port, timeout=timeout, context=ssl.create_default_context()
        )
    else:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request(
            "GET",
            path,
            headers={"User-Agent": _USER_AGENT, "Accept": _ACCEPT},
        )
        response = conn.getresponse()
        status = response.status
        if 300 <= status < 400:
            raise ValueError("redirects are not allowed")
        if status != 200:
            raise ValueError("unexpected status %d" % status)
        data = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(data) > _MAX_RESPONSE_BYTES:
            raise ValueError("response too large")
        return data
    finally:
        try:
            conn.close()
        except Exception:
            pass


def check_once(config_dir, now, *, endpoint=FIXED_ENDPOINT, timeout_s=2) -> None:
    """Reserve, fetch once, store a successful stable version; always silent."""
    root = Path(config_dir)
    stamp = _cache._time(now)
    if stamp is None:
        return
    if not _cache.reserve_check(root, now):
        return
    checked_at = stamp.isoformat()
    try:
        payload = _fetch(endpoint, timeout_s)
    except Exception:
        # The persisted reservation already represents this failed attempt.
        return
    version = parse_release(payload)
    if version is not None:
        _cache.store_result(root, checked_at, version)


def run_worker(config_dir, now, *, endpoint=FIXED_ENDPOINT) -> None:
    """Run only in a disposable worker: DNS and trickled reads share a deadline."""
    watchdog = threading.Timer(5, os._exit, args=(0,))
    watchdog.daemon = True
    watchdog.start()
    try:
        check_once(config_dir, now, endpoint=endpoint)
    finally:
        watchdog.cancel()


def _hidden_start_kwargs() -> dict:
    """A detached start that opens no console window on Windows (FR-020)."""
    if os.name != "nt":
        return {"start_new_session": True}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= 0x00000001  # STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0  # SW_HIDE
    return {
        "creationflags": 0x08000000 | 0x00000200,  # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
        "startupinfo": startupinfo,
    }


_SAFE_ENV_KEYS = (
    "PATH", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "TEMP", "TMP",
    "HOME", "USERPROFILE", "COMSPEC", "PATHEXT",
)


def _minimal_env() -> dict:
    """Minimal, controlled environment: no tokens, proxies or unrelated vars."""
    env: dict[str, str] = {}
    for key in _SAFE_ENV_KEYS:
        value = os.environ.get(key)
        if value is not None:
            env[key] = value
    try:
        global_prefs = _default_global_prefs_path()
    except Exception:
        global_prefs = None
    if global_prefs is not None:
        env["AGENT_CREW_CHIEF_GLOBAL_PREFS"] = str(global_prefs)
    return env


def _default_global_prefs_path():
    # Late import to avoid any circular import with config.
    from . import config as _config
    return _config.default_global_path()


def _spawn_update_worker(config_dir, *, project=None) -> object:
    """Spawn the detached, bounded update worker; never wait for it."""
    entry = Path(__file__).resolve().parents[1] / "crew-chief.py"
    argv = [sys.executable, "-I", str(entry), "_check-updates"]
    if project is not None:
        argv += ["--project", str(Path(project).expanduser().resolve())]
    env = _minimal_env()
    kwargs = _hidden_start_kwargs()
    child = subprocess.Popen(
        argv,
        cwd=(str(Path(project).expanduser().resolve()) if project is not None else None),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        **kwargs,
    )
    return child


def schedule_check(config_dir, enabled, *, spawn=None, now=None) -> None:
    """Schedule a detached update check when enabled; never waits for it."""
    if enabled is not True:
        return
    config_dir = Path(config_dir)
    if spawn is None:
        spawn = _spawn_update_worker
    if now is None:
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
    try:
        if not _cache.check_due(config_dir, now):
            return
        spawn(config_dir)
    except Exception:
        return
    return None
