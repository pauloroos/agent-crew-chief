"""v0.4.1 release gate: installed-bundle fake end-to-end flow (T040).

Covers FR-016, FR-017, FR-019, FR-021, FR-023, SC-008, SC-009, SC-010 and
SC-011. ``install.py`` installs the bundle into a temporary project and the
``acc`` wrapper into a temporary bin directory; the installed command then
launches one coordinator with the default backend (Orca hidden from PATH) and
one with a fake ``orca`` on PATH, both driven by a fake ``claude``, messages
the Orca coordinator after Orca is removed, and serves the web view on an
ephemeral 127.0.0.1 port whose snapshot is fetched over HTTP. HOME,
USERPROFILE, APPDATA and LOCALAPPDATA point at a temporary home, and every
process the flow starts is reaped before the test ends. The remaining checks
inspect the packaged sources, the CI matrix and the public release documents.
"""
from __future__ import annotations

import ast
import json
import os
import queue
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from http.client import HTTPConnection
from pathlib import Path

import pytest

from test_central_backend_cli import FAKE_ORCA
from test_central_supervisor import CLAUDE_HELP, FAKE_CLI

REPO_ROOT = Path(__file__).resolve().parents[1]
CHANGELOG = REPO_ROOT / "CHANGELOG.md"
README = REPO_ROOT / "README.md"
TUTORIAL = REPO_ROOT / "docs" / "tutorials" / "04-central.md"
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"
SCRIPTS = REPO_ROOT / "skill" / "crew-chief" / "scripts"
PACKAGE = SCRIPTS / "crew_chief"
WEB_ASSETS = REPO_ROOT / "skill" / "crew-chief" / "web"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


# ---------------------------------------------------------------------------
# Installed-bundle fake end-to-end flow (FR-016, FR-017, SC-009, SC-010)
# ---------------------------------------------------------------------------


def _alive(pid: int) -> bool:
    """Whether process ``pid`` still runs, without signalling it."""
    if os.name == "nt":
        import ctypes

        kernel = ctypes.windll.kernel32
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            kernel.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait(predicate, timeout_s: float = 60.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return predicate()


def _readline(stream, timeout_s: float = 30.0) -> str:
    """One line from ``stream``, or "" when nothing arrives in time."""
    lines: queue.Queue = queue.Queue()
    threading.Thread(target=lambda: lines.put(stream.readline()), daemon=True).start()
    try:
        return lines.get(timeout=timeout_s)
    except queue.Empty:
        return ""


def _registry(central: Path) -> dict:
    path = central / "registry.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def _supervisor_pid(central: Path, coordinator_id: str) -> int | None:
    path = central / "coordinators" / coordinator_id / "supervisor.json"
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("pid")
    except (OSError, ValueError):
        return None


def _get(url: str):
    """GET ``url`` directly over loopback, never through a proxy."""
    host, port, path = re.fullmatch(r"http://([\d.]+):(\d+)(/.*)", url).groups()
    conn = HTTPConnection(host, int(port), timeout=10)
    try:
        conn.request("GET", path)
        response = conn.getresponse()
        return response.status, response.getheader("Content-Type", ""), response.read()
    finally:
        conn.close()


def test_installed_bundle_fake_flow_default_orca_and_web(tmp_path, git_repo, fake_bin):
    home = tmp_path / "home"
    home.mkdir()
    central = tmp_path / "central"
    bin_dir = fake_bin("claude", FAKE_CLI.replace("__HELP__", repr(CLAUDE_HELP)))
    fake_bin("browser", "import sys, os\n"
             "open(os.environ['FAKE_BROWSER_LOG'], 'a', encoding='utf-8').write(sys.argv[1] + '\\n')\n")
    browser_log = tmp_path / "browser.log"
    orca_log = tmp_path / "orca-calls.jsonl"
    # Only the test's fake may resolve: drop any Orca installed on this machine.
    path = os.pathsep.join(part for part in os.environ["PATH"].split(os.pathsep)
                           if part and shutil.which("orca", path=part) is None)
    env = {**os.environ, "PATH": path, "AGENT_CREW_CHIEF_CENTRAL_DIR": str(central),
           "CREW_CHIEF_COORDINATOR": "claude", "FAKE_ORCA_LOG": str(orca_log),
           "FAKE_BROWSER_LOG": str(browser_log),
           "BROWSER": str(bin_dir / ("browser.cmd" if os.name == "nt" else "browser")),
           "XDG_CONFIG_HOME": str(home / ".config")}
    for name in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA"):
        env[name] = str(home)
    env.pop("FAKE_ORCA_MODE", None)

    def run(*argv: str) -> subprocess.CompletedProcess:
        return subprocess.run(list(argv), capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=env, timeout=120)

    acc_bin = tmp_path / "acc-bin"
    installed = run(sys.executable, "-I", str(REPO_ROOT / "install.py"), str(git_repo),
                    "--command", "--bin-dir", str(acc_bin))
    assert installed.returncode == 0, installed.stderr
    wrapper = acc_bin / ("acc.cmd" if os.name == "nt" else "acc")
    entry = SCRIPTS / "crew-chief.py"
    assert str(entry) in wrapper.read_text(encoding="utf-8")
    for args in (("add", "-A"), ("commit", "-q", "-m", "install crew-chief")):
        assert run("git", "-C", str(git_repo), *args).returncode == 0
    request = tmp_path / "request.md"
    request.write_text("# request\n\nA synthetic gate request.\n", encoding="utf-8")

    pids: list[int] = []
    web = None
    try:
        def launch(*extra: str) -> dict:
            done = run(str(wrapper), "launch", "--json", "--repo", str(git_repo),
                       "--request-file", str(request), "--mode", "raw-request",
                       "--authorize-raw", *extra)
            assert done.returncode == 0, done.stderr
            cid = json.loads(done.stdout)["coordinator_id"]
            # The fake claude completes the first round; the supervisor then exits.
            assert _wait(lambda: _registry(central)["coordinators"][cid]["state"] == "completed"), \
                _registry(central)["coordinators"][cid]
            pid = _supervisor_pid(central, cid)
            assert isinstance(pid, int)
            pids.append(pid)
            assert _wait(lambda: not _alive(pid), 30), f"supervisor {pid} still alive"
            return _registry(central)["coordinators"][cid]

        # Default mode: no Orca anywhere on PATH.
        assert shutil.which("orca", path=env["PATH"]) is None
        default = launch()
        assert default["backend"] == "default"
        assert not orca_log.exists()

        # Orca mode: a fake orca creates the worktree; the core runs the lifecycle.
        fake_bin("orca", FAKE_ORCA)
        assert shutil.which("orca", path=env["PATH"]) is not None
        orca = launch("--backend", "orca")
        assert orca["backend"] == "orca"
        assert Path(orca["worktree"]).name == f"orca-central-{orca['id']}"
        calls = [json.loads(line) for line in orca_log.read_text(encoding="utf-8").splitlines()]
        assert sum(call[:2] == ["worktree", "create"] for call in calls) == 1

        # Orca removed later: the owner still reaches the Orca coordinator.
        for stub in bin_dir.glob("orca*"):
            stub.unlink()
        orca_log.unlink()
        sent = run(str(wrapper), "message", orca["id"], "--text", "after orca")
        assert sent.returncode == 0, sent.stderr
        assert not orca_log.exists()

        # Web view: the same command line as the wrapper, owned directly so it can be reaped.
        web = subprocess.Popen(
            [sys.executable, "-I", str(entry), "central", "--web", "--port", "0"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", env=env,
        )
        url = _readline(web.stdout).strip()
        assert re.fullmatch(r"http://127\.0\.0\.1:\d+/", url), (url, web.poll())
        assert _wait(lambda: browser_log.is_file(), 30)
        assert browser_log.read_text(encoding="utf-8").split() == [url]
        status, kind, page = _get(url)
        assert status == 200 and kind.startswith("text/html") and b"central.js" in page
        status, kind, body = _get(url + "api/snapshot")
        assert status == 200 and kind.startswith("application/json")
        rows = {row["id"]: row for row in json.loads(body)["coordinators"]}
        assert {cid: (row["backend"], row["state"]) for cid, row in rows.items()} == {
            default["id"]: ("default", "completed"), orca["id"]: ("orca", "completed")}
        assert str(tmp_path).encode() not in body
    finally:
        if web is not None:
            web.terminate()
            web.wait(timeout=10)
            web.stdout.close()
            web.stderr.close()
        for pid in pids:
            if _alive(pid):
                os.kill(pid, signal.SIGTERM)
    port = int(url.rsplit(":", 1)[1].rstrip("/"))
    with socket.socket() as sock:
        sock.settimeout(0.5)
        assert sock.connect_ex(("127.0.0.1", port)) != 0
    assert not any(_alive(pid) for pid in pids)


@pytest.mark.parametrize("layout", [".claude/skills", ".agents/skills"])
def test_web_view_serves_its_assets_from_an_installed_skill(tmp_path, git_repo, fake_bin, layout):
    """``acc central --web`` run from a project's installed skill finds its packaged assets."""
    bin_dir = fake_bin("browser", "raise SystemExit(0)\n")
    installed = subprocess.run([sys.executable, "-I", str(REPO_ROOT / "install.py"), str(git_repo)],
                               capture_output=True, text=True, timeout=120)
    assert installed.returncode == 0, installed.stderr
    entry = git_repo / layout / "crew-chief" / "scripts" / "crew-chief.py"
    env = {**os.environ, "AGENT_CREW_CHIEF_CENTRAL_DIR": str(tmp_path / "central"),
           "BROWSER": str(bin_dir / ("browser.cmd" if os.name == "nt" else "browser"))}
    web = subprocess.Popen([sys.executable, "-I", str(entry), "central", "--web", "--port", "0"],
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           text=True, encoding="utf-8", env=env)
    try:
        url = _readline(web.stdout).strip()
        assert re.fullmatch(r"http://127\.0\.0\.1:\d+/", url), (url, web.poll())
        for path, kind in (("", "text/html"), ("central.css", "text/css"), ("central.js", "text/javascript")):
            status, served, _ = _get(url + path)
            assert (status, served.split(";")[0]) == (200, kind), path
    finally:
        web.terminate()
        web.wait(timeout=10)
        web.stdout.close()
        web.stderr.close()


# ---------------------------------------------------------------------------
# Documented fake-browser smoke procedure (SC-009, SC-011)
# ---------------------------------------------------------------------------


def test_documented_fake_browser_smoke_runs_and_releases_its_socket():
    command = "python tests/test_central_web_assets.py --smoke"
    assert command in _read(TUTORIAL)
    smoke = subprocess.Popen(
        [sys.executable, str(REPO_ROOT / "tests" / "test_central_web_assets.py"), "--smoke"],
        cwd=str(REPO_ROOT), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, encoding="utf-8",
    )
    try:
        line = _readline(smoke.stdout)
        match = re.match(r"(http://127\.0\.0\.1:(\d+)/)", line)
        assert match, (line, smoke.poll())
        url, port = match.group(1), int(match.group(2))
        status, kind, page = _get(url)
        assert status == 200 and kind.startswith("text/html")
        status, kind, body = _get(url + "api/snapshot")
        snapshot = json.loads(body)
        assert snapshot["questions"][0]["body"] == "<img src=x onerror=alert(1)>"
        assert snapshot["coordinators"][0]["repo_label"] == "demo"
    finally:
        smoke.terminate()
        smoke.wait(timeout=10)
        smoke.stdout.close()
        smoke.stderr.close()
    with socket.socket() as sock:
        sock.settimeout(0.5)
        assert sock.connect_ex(("127.0.0.1", port)) != 0


# ---------------------------------------------------------------------------
# FR-016 / SC-009: packaged web assets
# ---------------------------------------------------------------------------


def test_web_assets_packaged_without_external_references():
    for name in ("index.html", "central.css", "central.js"):
        text = _read(WEB_ASSETS / name)
        assert not re.search(r"(?i)(?:src|href)\s*=\s*[\"']?(?:https?:)?//", text), name
        assert not re.search(r"(?i)@import|url\(\s*[\"']?(?:https?:)?//", text), name


# ---------------------------------------------------------------------------
# FR-019: runtime requires only the standard library
# ---------------------------------------------------------------------------


def _imported_roots(path: Path) -> set[str]:
    roots = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"), str(path))):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def test_runtime_imports_only_the_standard_library():
    sources = [*PACKAGE.rglob("*.py"), SCRIPTS / "crew-chief.py", REPO_ROOT / "install.py"]
    assert len(sources) > 20
    allowed = set(sys.stdlib_module_names) | {"crew_chief", "__future__"}
    foreign = {f"{path.name}: {name}" for path in sources
               for name in _imported_roots(path) - allowed}
    assert not foreign, sorted(foreign)


# ---------------------------------------------------------------------------
# SC-008: three-OS CI matrix
# ---------------------------------------------------------------------------


def _matrix(key: str) -> set[str]:
    match = re.search(rf"^\s*{key}:\s*\[([^\]]*)\]", _read(CI), re.MULTILINE)
    assert match, f"ci.yml has no {key} matrix"
    return {item.strip().strip("\"'") for item in match.group(1).split(",")}


def test_ci_matrix_covers_three_oses_and_both_pythons():
    assert _matrix("os") == {"ubuntu-latest", "windows-latest", "macos-latest"}
    assert {"3.11", "3.13"} <= _matrix("python")
    assert re.search(r"^\s*run:\s*python -m pytest\b", _read(CI), re.MULTILINE)


# ---------------------------------------------------------------------------
# FR-021 / FR-023: public artifacts
# ---------------------------------------------------------------------------

_PRIVATE_PATTERNS = [
    re.compile(r"(?i)/home/[a-z]"),
    re.compile(r"(?i)/users/[a-z]"),
    re.compile(r"(?i)c:\\users\\[a-z]"),
    re.compile(r"(?i)api[_-]?key\s*[:=]\s*\S+"),
    re.compile(r"(?i)password\s*[:=]\s*\S+"),
    re.compile(r"(?i)token\s*[:=]\s*[A-Za-z0-9_\-]{16,}"),
]
_PUBLIC_DOCS = (CHANGELOG, README, REPO_ROOT / "README.pt-BR.md", TUTORIAL,
                REPO_ROOT / "docs" / "tutorials" / "04-central.pt-BR.md")


def test_no_private_data_in_release_artifacts():
    for doc in _PUBLIC_DOCS:
        text = _read(doc)
        for pattern in _PRIVATE_PATTERNS:
            match = pattern.search(text)
            assert match is None, f"{doc.name} leaks {match.group()!r}"


def test_no_ai_coauthor_trailers_in_release_docs():
    for doc in _PUBLIC_DOCS:
        text = _read(doc)
        for marker in ("Co-Authored-By:", "Generated with Claude", "AI-assisted"):
            assert marker not in text, f"{doc.name} contains AI trailer {marker!r}"


# ---------------------------------------------------------------------------
# Version and changelog
# ---------------------------------------------------------------------------


def test_version_is_041():
    from crew_chief import __version__
    assert __version__ == "0.4.1", f"expected 0.4.1, got {__version__}"


def test_changelog_has_041_entry():
    text = _read(CHANGELOG)
    assert "## [0.4.1]" in text, "CHANGELOG missing [0.4.1] heading"
    start = text.index("## [0.4.1]")
    end = text.find("\n## [", start + 1)
    section = (text[start:end] if end != -1 else text[start:]).lower()
    for word in ("web", "orca", "release gate"):
        assert word in section, f"0.4.1 changelog missing {word!r}"
