"""Asset and rendering tests for the packaged central web view.

Browser smoke steps (copy into the phase report; no live coordinator):
1. From the checkout run: python tests/test_central_web_assets.py --smoke
   It prints an ephemeral loopback URL and serves synthetic snapshots only.
2. Open http://127.0.0.1:<port>/ in a browser; confirm cards, questions, job
   history, model token charts, scoreboard and totals render from /api/snapshot.
3. The demo includes HTML-like question text: confirm it appears literally,
   never as markup. Inspect at a narrow width and with keyboard navigation.
4. Confirm no external scripts or stylesheets are loaded (DevTools Network).
5. Confirm POST controls stay disabled (buttons disabled) until T033.
6. Stop the server and confirm the listening socket is released.

These tests use temporary homes/config roots and fake snapshots only.
"""
from contextlib import contextmanager
from http.client import HTTPConnection
import json
from pathlib import Path
import re
import threading
import shutil
import subprocess
import sys

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skill/crew-chief/scripts"))

import pytest

from crew_chief import central_snapshot


ROOT = Path(__file__).resolve().parents[1]
PACKAGED_ASSETS = ROOT / "skill" / "crew-chief" / "web"


@contextmanager
def running(tmp_path, monkeypatch, assets=None, data=None):
    from crew_chief.central_web import create_server

    asset_dir = Path(assets) if assets is not None else (tmp_path / "assets")
    asset_dir.mkdir(parents=True, exist_ok=True)
    if assets is None:
        for name in ("index.html", "central.css", "central.js"):
            (asset_dir / name).write_text("asset-" + name, encoding="utf-8")
        (asset_dir / "private.txt").write_text("PRIVATE", encoding="utf-8")

    sample = data or {
        "schema_version": 1,
        "generated_at": "2026-01-01T00:00:00+00:00",
        "coordinators": [
            {
                "id": "c1",
                "repo_label": "demo",
                "cli": "claude",
                "state": "running",
                "accepted": 2,
                "total": 3,
                "active_pieces": [{"ref": "P1", "title": "piece", "work_type": "code",
                                   "executor": "claude", "status": "running", "elapsed_s": 5}],
                "jobs": [{"id": "j1", "title": "job", "state": "done", "start": "s", "end": "e"}],
                "metrics": {"free_piece_share": {"value": 0.5}, "free_token_share": {"value": 0.25},
                            "mean_score": {"value": 0.9}},
                "models": [{"role": "exec", "model": "m", "tier": "free", "tokens_in": 1,
                            "tokens_out": 2, "tokens_total": 3, "coverage": "measured"}],
                "scoreboard": "scoreboard text",
            }
        ],
        "questions": [{"id": "q1", "body": "question body", "coordinator_id": "c1"}],
        "totals": {"coordinators": 1, "jobs": 1, "questions": 1, "pieces": 3,
                   "accepted_pieces": 2, "mean_score": 0.9, "total_tokens": 3,
                   "tier_tokens": {"free": 3, "paid": 0}},
        "diagnostics": [{"record": "r", "message": "diag"}],
        "alerts": [],
    }
    monkeypatch.setattr(central_snapshot, "snapshot", lambda *a, **kw: sample)
    server = create_server(tmp_path / "central", asset_dir)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        yield server, sample
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)
        assert not thread.is_alive()


def request(server, path, *, host=None, method="GET"):
    conn = HTTPConnection(*server.server_address, timeout=3)
    try:
        headers = {} if host is None else {"Host": host}
        conn.request(method, path, headers=headers)
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        conn.close()


def test_packaged_assets_exist():
    for name in ("index.html", "central.css", "central.js"):
        path = PACKAGED_ASSETS / name
        assert path.is_file(), f"missing packaged asset: {path}"
        assert path.read_text(encoding="utf-8").strip()


def test_packaged_assets_served(tmp_path, monkeypatch):
    with running(tmp_path, monkeypatch, assets=PACKAGED_ASSETS) as (server, _):
        for path in ("/", "/index.html", "/central.css", "/central.js"):
            status, headers, body = request(server, path)
            assert status == 200, path
            assert body, path


def test_no_external_resources():
    for name in ("index.html", "central.css", "central.js"):
        text = (PACKAGED_ASSETS / name).read_text(encoding="utf-8")
    html = (PACKAGED_ASSETS / "index.html").read_text(encoding="utf-8")
    assert "http://" not in html and "https://" not in html
    assert "cdn" not in html.lower()
    js = (PACKAGED_ASSETS / "central.js").read_text(encoding="utf-8")
    css = (PACKAGED_ASSETS / "central.css").read_text(encoding="utf-8")
    assert "http://" not in js and "https://" not in js
    assert "http://" not in css and "https://" not in css
    # no external script src
    for m in re.finditer(r"<script[^>]*src=[\"']([^\"']+)[\"']", html):
        src = m.group(1)
        assert src.startswith("/") or src.startswith("./"), src


def test_safe_text_insertion():
    js = (PACKAGED_ASSETS / "central.js").read_text(encoding="utf-8")
    assert "textContent" in js
    assert "innerHTML" not in js
    assert "document.write" not in js
    assert "eval(" not in js


def test_controls_disabled_in_html():
    html = (PACKAGED_ASSETS / "index.html").read_text(encoding="utf-8")
    assert "disabled" in html


def test_chart_and_state_hooks():
    js = (PACKAGED_ASSETS / "central.js").read_text(encoding="utf-8")
    for token in ("models", "tokens_total", "tier", "scoreboard", "questions", "jobs"):
        assert token in js, token


def test_snapshot_parity_rendered(tmp_path, monkeypatch):
    with running(tmp_path, monkeypatch, assets=PACKAGED_ASSETS) as (server, sample):
        status, _, body = request(server, "/api/snapshot")
        assert status == 200
        assert json.loads(body)["coordinators"][0]["models"][0]["tokens_total"] == 3


def test_xss_text_not_markup(tmp_path, monkeypatch):
    secret = "<script>alert(1)</script>"
    data = {
        "schema_version": 1, "generated_at": "2026-01-01", "coordinators": [],
        "questions": [{"id": "q", "body": secret, "coordinator_id": "c"}],
        "totals": {"tokens_total": 1}, "diagnostics": [{"message": secret}],
    }
    with running(tmp_path, monkeypatch, assets=PACKAGED_ASSETS, data=data) as (server, _):
        status, _, body = request(server, "/api/snapshot")
        assert status == 200
        # JSON is data, and the browser inserts it only through textContent.
        assert headers_content_type(server).startswith("application/json")
        assert json.loads(body)["questions"][0]["body"].startswith("<script>")


def headers_content_type(server):
    return request(server, "/api/snapshot")[1]["Content-Type"]


def render_js(sample):
    node = shutil.which("node")
    if not node:
        pytest.skip("optional Node DOM runtime unavailable")
    runner = r'''
const fs = require('fs'), vm = require('vm');
const fixture = JSON.parse(fs.readFileSync(0, 'utf8'));
const created = [];
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.style = {}; this._text = ''; }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(x => x.textContent).join(' '); }
  get firstChild() { return this.children[0]; }
  appendChild(x) { this.children.push(x); return x; }
  removeChild(x) { this.children.splice(this.children.indexOf(x), 1); }
  set innerHTML(x) { throw new Error('unsafe markup insertion'); }
  setAttribute(k, v) { this[k] = v; }
  addEventListener() {}
}
const elements = {};
const document = {
  getElementById(id) { return elements[id] ||= new Element('section'); },
  createElement(tag) { created.push(tag); return new Element(tag); },
};
const calls = [];
const fetch = async (url) => {
  calls.push(url);
  if (url !== '/api/snapshot') throw new Error('unexpected request');
  return {ok: true, json: async () => fixture};
};
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), {document, fetch, setTimeout() {}, clearTimeout() {}});
setImmediate(() => console.log(JSON.stringify({
  text: Object.fromEntries(Object.entries(elements).map(([k, v]) => [k, v.textContent])), created, calls
})));
'''
    completed = subprocess.run([node, "-e", runner, str(PACKAGED_ASSETS / "central.js")],
        input=json.dumps(sample), capture_output=True, text=True, timeout=10, check=True)
    return json.loads(completed.stdout)


def test_dom_renders_actual_snapshot_safely():
    malicious = "<img src=x onerror=alert(1)>"
    sample = {"coordinators": [{"id": "c1", "repo_label": malicious, "state": "running",
        "accepted": 1, "total": 2, "active_pieces": [{"ref": "T1", "title": "piece"}],
        "jobs": [{"id": "j1", "title": "history title"}], "scoreboard": "score 85",
        "models": [{"model": "unknown-model", "role": "exec", "tier": "free", "tokens_total": None}]}],
        "questions": [{"id": "q1", "body": malicious}], "totals": {"total_tokens": None}}
    result = render_js(sample)
    assert malicious in result["text"]["coordinators"] and malicious in result["text"]["questions"]
    assert "img" not in result["created"] and "script" not in result["created"]
    assert "history title" in result["text"]["history"]
    assert "score 85" in result["text"]["scoreboard"]
    assert "unknown tok" in result["text"]["charts"]
    assert "unknown" in result["text"]["totals"]
    assert result["calls"] == ["/api/snapshot"]


def test_chart_uses_deduplicated_shared_totals():
    model = {"model": "shared-model", "role": "exec", "tier": "free", "tokens_total": 7}
    sample = {"coordinators": [{"models": [model]}, {"models": [model]}],
        "totals": {"models": [model], "tokens_by_tier": {"free": 7}}, "questions": []}
    result = render_js(sample)
    assert result["text"]["charts"].count("shared-model") == 1
    assert "free: 7" in result["text"]["charts"]


def test_empty_dom_states():
    result = render_js({"coordinators": [], "questions": [], "totals": {}})["text"]
    assert "No pending questions" in result["questions"]
    assert "No job history" in result["history"]
    assert "No scoreboard data" in result["scoreboard"]


def test_multi_coordinator_chart_does_not_guess_missing_aggregate():
    model = {"model": "same-model", "role": "exec", "tier": "free", "tokens_total": 7}
    result = render_js({"coordinators": [{"models": [model]}, {"models": [model]}], "totals": {}})
    assert "Aggregate model data unavailable" in result["text"]["charts"]
    assert "same-model" not in result["text"]["charts"]


def smoke():
    """Manual fake-browser fixture; never loads a real registry or launches CLIs."""
    from tempfile import TemporaryDirectory
    from unittest.mock import patch
    from crew_chief.central_web import create_server
    fixture = {"schema_version": 1, "generated_at": "synthetic demo", "coordinators": [{
        "id": "00000000-0000-4000-8000-000000000001", "repo_label": "demo", "cli": "fake",
        "state": "waiting", "accepted": 1, "total": 2, "jobs": [{"id": "demo", "title": "Demo job"}],
        "scoreboard": "Demo model: 85", "models": [{"role": "exec", "model": "demo-model",
        "tier": "free", "tokens_total": 42}]}], "questions": [{"id": "demo-question",
        "body": "<img src=x onerror=alert(1)>", "coordinator_id": "00000000-0000-4000-8000-000000000001"}],
        "totals": {"coordinators": 1, "jobs": 1, "questions": 1, "total_tokens": 42}}
    with TemporaryDirectory(prefix="acc-demo-") as directory, patch.object(central_snapshot, "snapshot", return_value=fixture):
        server = create_server(Path(directory), PACKAGED_ASSETS)
        print(f"http://127.0.0.1:{server.server_port}/  (Ctrl-C to close)", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()


if __name__ == "__main__" and sys.argv[1:] == ["--smoke"]:
    smoke()


def test_socket_released(tmp_path, monkeypatch):
    import socket
    with running(tmp_path, monkeypatch, assets=PACKAGED_ASSETS) as (server, _):
        address = server.server_address
    with socket.socket() as sock:
        sock.bind(address)
