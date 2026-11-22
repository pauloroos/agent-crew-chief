"""ACC smoke: start the app, call the routes, shoot the screens (22/11: errors only seen after the merge)."""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from crew_chief import smoke
from crew_chief.cli import main

PY = f'"{sys.executable}"'
APP = '''import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        code = 500 if self.path == "/quebrado" else 404 if self.path == "/sumiu" else 200
        self.send_response(code); self.end_headers(); self.wfile.write(b"ok")
    def log_message(self, *a): pass
HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
'''


def _repo(tmp_path: Path, toml: str) -> Path:
    repo = tmp_path / "loja"
    (repo / ".crew-chief").mkdir(parents=True)
    (repo / "app.py").write_text(APP, encoding="utf-8")
    (repo / ".crew-chief" / "smoke.toml").write_text(toml, encoding="utf-8")
    return repo


@pytest.fixture(autouse=True)
def no_browser(monkeypatch):
    monkeypatch.setenv("CREW_CHIEF_BROWSER", "nao-existe-este-navegador")


def test_app_up_routes_answer(tmp_path):
    repo = _repo(tmp_path, f'start = \'{PY} app.py {{port}}\'\nready = "/healthz"\nroutes = ["/", "/api"]\n'
                           '[[screen]]\npath = "/"\nwidths = [390]\n')
    code, summary = smoke.run(repo, tmp_path / "out", "feature/x")
    assert code == 0, summary
    assert "/api -> 200" in summary and "local: ok" in summary
    data = json.loads((tmp_path / "out" / "smoke.json").read_text(encoding="utf-8"))
    assert data[0]["screens"][0]["note"].startswith("no browser found")  # screens skipped, never a failure
    assert (tmp_path / "out" / "smoke.html").is_file()


def test_a_listed_route_that_is_missing_fails(tmp_path):
    # 22/11: the screen called routes the API did not have
    repo = _repo(tmp_path, f"start = '{PY} app.py {{port}}'\nroutes = [\"/sumiu\"]\n")
    code, summary = smoke.run(repo, tmp_path / "out", None)
    assert code == 1 and "/sumiu -> 404" in summary


def test_preview_without_its_header_variable_fails(tmp_path, monkeypatch):
    monkeypatch.delenv("PREVIEW_TOKEN", raising=False)
    repo = _repo(tmp_path, f"start = '{PY} app.py {{port}}'\npreview = \"echo http://127.0.0.1:9\"\n"
                           "preview_headers = { \"X-Token\" = \"$PREVIEW_TOKEN\" }\n")
    code, summary = smoke.run(repo, tmp_path / "out", "feature/z")
    assert code == 1 and "PREVIEW_TOKEN" in summary


def test_a_route_with_500_fails(tmp_path):
    repo = _repo(tmp_path, f'start = \'{PY} app.py {{port}}\'\nroutes = ["/quebrado"]\n')
    code, summary = smoke.run(repo, tmp_path / "out", None)
    assert code == 1 and "/quebrado -> 500" in summary and "local: FAILED" in summary


def test_an_app_that_dies_at_start_fails_with_its_log(tmp_path):
    # 22/11: ModuleNotFoundError: No module named 'jwt' at import time
    repo = _repo(tmp_path, f'start = \'{PY} -c "import jwt_que_nao_existe" {{port}}\'\nready_timeout = 20\n')
    code, summary = smoke.run(repo, tmp_path / "out", None)
    assert code == 1 and "the app exited" in summary and "jwt_que_nao_existe" in summary  # log tail in the PR
    data = json.loads((tmp_path / "out" / "smoke.json").read_text(encoding="utf-8"))
    assert "jwt_que_nao_existe" in data[0]["log_tail"]


def test_no_config_is_not_a_failure_and_a_bad_one_is_rc_2(tmp_path):
    repo = tmp_path / "vazio"; repo.mkdir()
    code, summary = smoke.run(repo, tmp_path / "out", None)
    assert code == 0 and "not configured" in summary
    bad = _repo(tmp_path, 'start = "sem porta"\n')
    assert smoke.run(bad, tmp_path / "out2", None)[0] == 2


class _Preview(BaseHTTPRequestHandler):
    def do_GET(self):
        ok = self.headers.get("X-Token") == "segredo"
        self.send_response(200 if ok else 503); self.end_headers()

    def log_message(self, *a):
        pass


def test_preview_stage_calls_the_routes_on_the_url_with_headers_and_tears_down(tmp_path, monkeypatch):
    srv = HTTPServer(("127.0.0.1", 0), _Preview)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    monkeypatch.setenv("PREVIEW_TOKEN", "segredo")
    marca = tmp_path / "apagado.txt"
    repo = _repo(tmp_path, f'start = \'{PY} app.py {{port}}\'\n'
                           f'preview = \'{PY} -c "print(\\"preparando\\"); print(\\"{url}\\")"\'\n'
                           f'preview_teardown = \'{PY} -c "open(r\\"{marca}\\", \\"w\\").write(\\"{{branch}}\\")"\'\n'
                           'preview_headers = { "X-Token" = "$PREVIEW_TOKEN" }\n')
    code, summary = smoke.run(repo, tmp_path / "out", "feature/y")
    srv.shutdown()
    assert code == 0, summary
    assert "preview: ok" in summary and marca.read_text(encoding="utf-8") == "feature/y"
    assert "- preview:" not in smoke.run(repo, tmp_path / "out2", "feature/y", with_preview=False)[1]


def test_cli_smoke(tmp_path, capsys):
    repo = _repo(tmp_path, f'start = \'{PY} app.py {{port}}\'\n')
    assert main(["smoke", "--repo", str(repo), "--out", str(tmp_path / "out")]) == 0
    assert "local: ok" in capsys.readouterr().out
