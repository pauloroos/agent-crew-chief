"""``ACC smoke``: start the app the crew built, call its routes and screenshot its screens next to the prototypes,
before the pull request (22/11: a missing library, a route the screen called that did not exist and a renamed login
variable only showed up after the merge, one deploy round each).

The project says how, in ``.crew-chief/smoke.toml``::

    start = "uv run uvicorn app.main:app --port {port}"   # {port}: a free port ACC picks
    ready = "/healthz"                                    # polled until it answers below 500
    ready_timeout = 90                                     # seconds (default 60)
    env = { APP_ENV = "test" }                             # added to the app's environment
    routes = ["/", "/api/v1/items"]                        # each must answer, and below 500
    preview = "ssh host dev-rdl preview {repo} {branch}"   # optional: last stdout line = the preview URL
    preview_teardown = "ssh host dev-rdl preview-apagar {repo} {branch}"
    preview_headers = { "CF-Access-Client-Id" = "$CF_ACCESS_CLIENT_ID" }   # $VARS from the environment

    [[screen]]
    path = "/"
    prototype = "docs/prototype/index.html"                # optional, relative to the repo
    widths = [390, 1440]

Local stage always; preview stage when ``preview`` is set (skipped with --no-preview). Screens are shot with a
Chromium-family browser's own ``--headless --screenshot`` (CREW_CHIEF_BROWSER, else one found on PATH, the usual
Windows/macOS places or the Playwright cache): no browser = screens skipped, never a failure. The preview stage
checks routes only (a CLI screenshot cannot send the preview's headers).
"""

from __future__ import annotations

import glob
import html
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

CONFIG = Path(".crew-chief") / "smoke.toml"
KEYS = {"start", "ready", "ready_timeout", "env", "routes", "screen", "preview", "preview_teardown",
        "preview_headers", "preview_timeout"}


class SmokeError(Exception):
    """A bad smoke.toml; the message names the problem."""


def load(repo: Path) -> dict | None:
    """The validated smoke.toml of ``repo``, or None when the project has none."""
    path = repo / CONFIG
    if not path.is_file():
        return None
    try:
        cfg = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise SmokeError(f"{path}: {exc}") from exc
    extra = set(cfg) - KEYS
    if extra:
        raise SmokeError(f"{path}: unknown keys {sorted(extra)}")
    if not isinstance(cfg.get("start"), str) or "{port}" not in cfg["start"]:
        raise SmokeError(f"{path}: start must be a command with {{port}}")
    for key in ("routes",):
        if not all(isinstance(r, str) and r.startswith("/") for r in cfg.get(key, [])):
            raise SmokeError(f"{path}: {key} are paths starting with /")
    for s in cfg.get("screen", []):
        if not isinstance(s.get("path"), str) or not s["path"].startswith("/"):
            raise SmokeError(f"{path}: every [[screen]] needs path = \"/...\"")
        if not all(isinstance(w, int) and 240 <= w <= 3840 for w in s.get("widths", [390, 1440])):
            raise SmokeError(f"{path}: screen widths are pixels from 240 to 3840")
    return cfg


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def get(url: str, headers: dict | None = None, timeout: float = 15) -> tuple[int | None, str]:
    """(status, note): status None when nothing answered."""
    req = urllib.request.Request(url, headers={"User-Agent": "agent-crew-chief-smoke", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (local or the project's preview)
            return r.status, ""
    except urllib.error.HTTPError as exc:
        return exc.code, ""
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return None, str(getattr(exc, "reason", exc))[:200]


def find_browser() -> str | None:
    """A Chromium-family browser that takes --headless --screenshot."""
    named = os.environ.get("CREW_CHIEF_BROWSER")
    if named:
        return named if (shutil.which(named) or Path(named).is_file()) else None
    for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "chrome", "msedge"):
        if shutil.which(name):
            return shutil.which(name)
    places = [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
              "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    cache = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or Path.home() / ".cache" / "ms-playwright")
    places += sorted(glob.glob(str(cache / "chromium*" / "*" / "chrome"))) + \
        sorted(glob.glob(str(cache / "chromium_headless_shell*" / "*" / "headless_shell")))
    return next((p for p in places if Path(p).is_file()), None)


def shot(browser: str, url: str, out: Path, width: int, height: int = 900) -> str:
    """Screenshot ``url`` into ``out``; '' when it worked, else why not."""
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [browser, "--headless", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
           f"--window-size={width},{height}", "--virtual-time-budget=4000", f"--screenshot={out}", url]
    try:
        subprocess.run(cmd, capture_output=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return str(exc)[:200]
    return "" if out.is_file() and out.stat().st_size else "the browser wrote no image"


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, check=False)
    else:
        try:
            os.killpg(proc.pid, 15)
        except OSError:
            proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def _routes(base: str, routes: list[str], headers: dict | None = None) -> list[dict]:
    out = []
    for path in routes:
        status, note = get(base + path, headers)
        out.append({"path": path, "status": status, "ok": status is not None and status < 500, "note": note})
    return out


def _expand(headers: dict) -> dict:
    """$VARS in header values from the environment (a missing one leaves the header out)."""
    out = {}
    for k, v in (headers or {}).items():
        value = re.sub(r"\$([A-Z_][A-Z0-9_]*)", lambda m: os.environ.get(m.group(1), "\0"), str(v))
        if "\0" not in value:
            out[str(k)] = value
    return out


def local(repo: Path, cfg: dict, out: Path) -> dict:
    """Start the app, wait for ``ready``, call the routes, shoot the screens, stop it."""
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    log = out / "app.log"
    out.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, **{str(k): str(v) for k, v in (cfg.get("env") or {}).items()}, "PORT": str(port)}
    extra = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32" else {"start_new_session": True}
    with open(log, "wb") as fh:
        proc = subprocess.Popen(cfg["start"].replace("{port}", str(port)), shell=True, cwd=repo, env=env,  # noqa: S602
                                stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **extra)
    result = {"stage": "local", "base": base, "routes": [], "screens": [], "ok": False}
    try:
        limit = time.monotonic() + float(cfg.get("ready_timeout", 60))
        ready = cfg.get("ready", "/")
        while True:
            if proc.poll() is not None:
                result["error"] = f"the app exited (rc {proc.returncode}) before {ready} answered"
                break
            status, _ = get(base + ready, timeout=3)
            if status is not None and status < 500:
                break
            if time.monotonic() > limit:
                result["error"] = f"{ready} did not answer below 500 in {cfg.get('ready_timeout', 60)}s"
                break
            time.sleep(1)
        if "error" not in result:
            result["routes"] = _routes(base, [ready] + [r for r in cfg.get("routes", []) if r != ready])
            browser = find_browser()
            for s in cfg.get("screen", []):
                for w in s.get("widths", [390, 1440]):
                    name = re.sub(r"[^a-z0-9]+", "-", s["path"].lower()).strip("-") or "root"
                    entry = {"path": s["path"], "width": w}
                    if not browser:
                        entry["note"] = "no browser found (CREW_CHIEF_BROWSER): screen skipped"
                    else:
                        app_png = out / f"{name}-{w}-app.png"
                        entry["app"], entry["app_note"] = app_png.name, shot(browser, base + s["path"], app_png, w)
                        if s.get("prototype"):
                            proto = (repo / s["prototype"]).resolve()
                            proto_png = out / f"{name}-{w}-prototype.png"
                            entry["prototype"] = proto_png.name
                            entry["prototype_note"] = (shot(browser, proto.as_uri(), proto_png, w) if proto.is_file()
                                                       else f"prototype not found: {s['prototype']}")
                    result["screens"].append(entry)
            result["ok"] = all(r["ok"] for r in result["routes"])
    finally:
        _stop(proc)
    result["log_tail"] = log.read_text(encoding="utf-8", errors="replace")[-1500:] if log.is_file() else ""
    return result


def preview(repo: Path, cfg: dict, branch: str) -> dict:
    """Run the project's preview command, call the routes on the URL it prints, tear it down."""
    subst = lambda c: c.replace("{repo}", repo.name).replace("{branch}", branch)  # noqa: E731
    result = {"stage": "preview", "routes": [], "ok": False}
    try:
        run = subprocess.run(subst(cfg["preview"]), shell=True, cwd=repo, capture_output=True, text=True,  # noqa: S602
                             timeout=float(cfg.get("preview_timeout", 900)), check=False)
    except subprocess.TimeoutExpired:
        result["error"] = "the preview command timed out"
        return result
    lines = [x.strip() for x in run.stdout.splitlines() if x.strip()]
    url = lines[-1] if lines else ""
    if run.returncode != 0 or not re.match(r"https?://", url):
        result["error"] = f"the preview command failed (rc {run.returncode}): {(run.stderr or run.stdout)[-300:]}"
        return result
    result["base"] = url.rstrip("/")
    try:
        ready = cfg.get("ready", "/")
        result["routes"] = _routes(result["base"], [ready] + [r for r in cfg.get("routes", []) if r != ready],
                                   _expand(cfg.get("preview_headers")))
        result["ok"] = all(r["ok"] for r in result["routes"])
    finally:
        if cfg.get("preview_teardown"):
            subprocess.run(subst(cfg["preview_teardown"]), shell=True, cwd=repo, capture_output=True,  # noqa: S602
                           timeout=300, check=False)
    return result


def report(stages: list[dict], out: Path) -> str:
    """Write smoke.json and smoke.html into ``out``; return a short text summary for the PR body."""
    (out / "smoke.json").write_text(json.dumps(stages, ensure_ascii=False, indent=2), encoding="utf-8")
    rows, lines = [], []
    for st in stages:
        verdict = "ok" if st["ok"] else "FAILED"
        lines.append(f"- {st['stage']}: {verdict}" + (f" ({st['error']})" if st.get("error") else ""))
        for r in st.get("routes", []):
            lines.append(f"  - {r['path']} -> {r['status'] if r['status'] is not None else 'no answer'}"
                         + (f" ({r['note']})" if r.get("note") else ""))
            rows.append(f"<tr><td>{st['stage']}</td><td>{html.escape(r['path'])}</td><td>{r['status']}</td></tr>")
    shots = []
    for st in stages:
        for s in st.get("screens", []):
            cells = "".join(f"<td>{html.escape(k)}<br><img src=\"{html.escape(s[k])}\" width=\"{min(s['width'], 640)}\"></td>"
                            for k in ("prototype", "app") if s.get(k) and not s.get(k + "_note"))
            notes = " ".join(html.escape(s[k]) for k in ("note", "app_note", "prototype_note") if s.get(k))
            shots.append(f"<h3>{html.escape(s['path'])} @ {s['width']}px</h3><table><tr>{cells}</tr></table>{notes}")
    (out / "smoke.html").write_text(
        "<!doctype html><meta charset=utf-8><title>ACC smoke</title><h1>ACC smoke</h1>"
        "<table border=1><tr><th>stage</th><th>route</th><th>status</th></tr>" + "".join(rows) + "</table>"
        + "".join(shots), encoding="utf-8")
    return "\n".join(lines)


def run(repo: Path, out: Path, branch: str | None, with_preview: bool = True) -> tuple[int, str]:
    """(exit code, summary): 0 passed or nothing configured, 1 a stage failed, 2 a bad smoke.toml."""
    try:
        cfg = load(repo)
    except SmokeError as exc:
        return 2, str(exc)
    if cfg is None:
        return 0, f"no {CONFIG.as_posix()}: smoke test not configured for this project (say so in the PR)"
    out.mkdir(parents=True, exist_ok=True)
    stages = [local(repo, cfg, out)]
    if with_preview and cfg.get("preview") and stages[0]["ok"]:
        stages.append(preview(repo, cfg, branch or _branch(repo)))
    summary = report(stages, out) + f"\nreport: {(out / 'smoke.html').as_posix()}"
    return (0 if all(s["ok"] for s in stages) else 1), summary


def _branch(repo: Path) -> str:
    try:
        return subprocess.run(["git", "-C", str(repo), "branch", "--show-current"], capture_output=True, text=True,
                              check=False).stdout.strip()
    except OSError:
        return ""
