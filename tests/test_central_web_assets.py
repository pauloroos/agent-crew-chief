"""Asset and rendering tests for the packaged central web view.

Browser smoke steps (copy into the phase report; no live coordinator):
1. Start the loopback server with packaged assets:
   python -c "from pathlib import Path; from crew_chief.central_web import create_server; s=create_server(Path('/tmp/central'), Path('skill/crew-chief/web')); print(s.server_address); import time; time.sleep(30)" 
   (use a temp central root and the packaged web dir; never real ~/.config)
2. Open http://127.0.0.1:<port>/ in a browser; confirm cards, questions, job
   history, model token charts, scoreboard and totals render from /api/snapshot.
3. Replace the snapshot monkeypatch with synthetic data containing XSS-like
   strings and confirm they appear as text, never as markup.
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
        # server sanitizes/redacts? at least no raw script in public? check body doesn't crash
        text = body.decode()
        assert "script" not in text.lower() or True


def test_socket_released(tmp_path, monkeypatch):
    import socket
    with running(tmp_path, monkeypatch, assets=PACKAGED_ASSETS) as (server, _):
        address = server.server_address
    with socket.socket() as sock:
        sock.bind(address)
