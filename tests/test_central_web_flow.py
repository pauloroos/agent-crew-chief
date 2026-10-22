"""HTTP/terminal parity with real temporary records; never a live coordinator."""
from contextlib import nullcontext
from datetime import datetime, timezone
from http.client import HTTPConnection
import io
import json
from pathlib import Path
import socket
import threading
import uuid

import pytest
from crew_chief import (central_messages, central_registry, central_screen, central_snapshot,
                        central_terminal, central_web, records)

CID = "00000000-0000-4000-8000-000000000001"
NOW = "2026-01-01T00:00:10+00:00"


@pytest.fixture
def live(tmp_path, monkeypatch):
    root, worktree = tmp_path / "central", tmp_path / "worktree"
    worktree.mkdir()
    job = records.new_job(worktree, "Synthetic job", "Synthetic request", True, "isolated integration")
    job["pieces"] = [{"n": 1, "ref": "T001", "type": "code", "title": "Accepted piece",
        "accepted": {"attempt": 1, "branch": "fake", "at": NOW}, "attempts": [{"n": 1,
            "role": "exec", "executor": "opencode:demo-free", "reported_model": "demo-model",
            "cost": "free", "status": "ok", "tokens_total": 100, "tokens_in": 80,
            "tokens_out": 20, "started": "2026-01-01T00:00:00+00:00", "duration_s": 2,
            "tokens_estimated": False}]},
        {"n": 2, "ref": "T002", "type": "code", "title": "Pending piece", "attempts": []}]
    records.save_job(worktree, job)
    record = {"schema_version": 1, "id": CID, "repo": str(tmp_path / "demo"),
        "worktree": str(worktree), "branch": "fake", "base_commit": "0" * 40,
        "backend": "default", "cli": "codex", "cli_version": "fake", "acc_version": "0.4.0",
        "session_id": None, "mode": "approved-spec", "authorization": {"raw": False,
            "request_digest": "synthetic", "at": NOW}, "job_ids": [job["id"]],
        "active_job_id": job["id"], "state": "running", "last_error": None,
        "created_at": NOW, "updated_at": NOW}
    central_registry.register(root, record)
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromisoformat(NOW)
    monkeypatch.setattr(central_web, "datetime", FixedDatetime)
    assets = Path(__file__).resolve().parents[1] / "skill/crew-chief/web"
    server = central_web.create_server(root, assets)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, name="flow-http")
    thread.start()
    try:
        yield server, worktree, job
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)
        assert not thread.is_alive() and server.socket.fileno() == -1
        with socket.socket() as sock:
            sock.settimeout(.3)
            assert sock.connect_ex(server.server_address) != 0


def http(server, path, payload=None, headers=None):
    conn = HTTPConnection(*server.server_address, timeout=4)
    try:
        if payload is None:
            conn.request("GET", path, headers=headers or {})
        else:
            data = {"csrf_token": server.csrf_token, "coordinator_id": CID,
                    "action_id": str(uuid.uuid4()), **payload}
            hdr = {"Origin": f"http://127.0.0.1:{server.server_port}", "Content-Type": "application/json", **(headers or {})}
            conn.request("POST", path, body=json.dumps(data).encode(), headers=hdr)
        response = conn.getresponse()
        body = response.read()
        return response.status, json.loads(body) if "json" in response.getheader("Content-Type", "") else body
    finally:
        conn.close()


def tree_bytes(directory):
    return {str(p.relative_to(directory)): p.read_bytes() for p in directory.rglob("*") if p.is_file()}


def test_real_snapshot_parity_assets_and_read_only(live):
    server, worktree, job = live
    before = tree_bytes(server.root), tree_bytes(worktree)
    terminal = central_snapshot.snapshot(server.root, NOW)
    status, web = http(server, "/api/snapshot")
    assert status == 200 and web == central_web.public_snapshot(terminal)
    assert web["totals"]["accepted_pieces"] == 1
    assert web["totals"]["pieces"] == 2 and web["totals"]["total_tokens"] == 100
    assert web["coordinators"][0]["jobs"][0]["id"] == job["id"]
    assert web["coordinators"][0]["models"][0]["tokens_total"] == 100
    assert central_terminal.render(web, plain=True) == central_terminal.render(terminal, plain=True)
    for route in ("/", "/central.css", "/central.js"):
        assert http(server, route)[0] == 200
    assert before == (tree_bytes(server.root), tree_bytes(worktree))


def test_http_message_question_answer_refresh(live):
    server, _, _ = live
    payload = {"text": "owner message", "action_id": str(uuid.uuid4())}
    first = http(server, "/api/message", payload)
    assert first[0] == 200 and http(server, "/api/message", payload) == first
    qid = central_messages.send(server.root, CID, {"direction": "out", "kind": "question", "body": "Choose?", "task_refs": ["T002"]})
    assert http(server, "/api/snapshot")[1]["questions"][0]["id"] == qid
    answer = {"question_id": qid, "text": "Proceed", "action_id": str(uuid.uuid4())}
    result = http(server, "/api/answer", answer)
    assert result[0] == 200 and http(server, "/api/answer", answer) == result
    pending = list(central_messages.pending(server.root, CID, "in"))
    assert len(pending) == 2
    assert next(p for p in pending if p["kind"] == "answer")["reply_to"] == qid
    snap = http(server, "/api/snapshot")[1]
    assert snap["questions"] == [] and snap["totals"]["questions"] == 0
    raw = central_snapshot.snapshot(server.root, NOW)
    assert raw["totals"]["total_tokens"] == snap["totals"]["total_tokens"] == 100


def test_cooperative_pause_resume_and_terminal_parity(live):
    server, _, _ = live
    for action, state in (("pause", "paused"), ("resume", "running")):
        payload = {"action_id": str(uuid.uuid4())}
        result = http(server, "/api/" + action, payload)
        assert result[0] == 200 and http(server, "/api/" + action, payload) == result
        # HTTP only writes the durable request. This fake consumer simulates
        # the accepted supervisor boundary; it never starts a coordinator.
        assert http(server, "/api/snapshot")[1]["coordinators"][0]["state"] != state
        control = server.root / "coordinators" / CID / "control" / (result[1]["control_id"] + ".json")
        assert json.loads(control.read_text())["command"] == action
        central_registry.update_record(server.root, CID, {"state": state})
        snap = http(server, "/api/snapshot")[1]
        raw = central_snapshot.snapshot(server.root, NOW)
        assert snap["coordinators"][0]["state"] == raw["coordinators"][0]["state"] == state
        assert central_terminal.render(snap, plain=True) == central_terminal.render(raw, plain=True)
    assert len(list((server.root / "coordinators" / CID / "control").glob("*.json"))) == 2


def test_http_attach_consumed_only_by_terminal_loop(live, monkeypatch):
    server, _, _ = live
    fallback = http(server, "/api/attach", {})[1]
    assert fallback["queued"] is False and fallback["command"] == "acc attach " + CID
    attached, results = [], []
    class Stream(io.StringIO):
        def isatty(self):
            return True
    terminal_io = type("IO", (), {"stdin": Stream(), "stdout": Stream()})()
    def key(**kwargs):
        assert server.attach_bridge_active
        payload = {"action_id": str(uuid.uuid4())}
        first = http(server, "/api/attach", payload)
        assert first[0] == 200 and first[1]["queued"] is True
        assert http(server, "/api/attach", payload) == first
        results.append(first)
        return None if len(results) == 1 else "q"
    monkeypatch.setattr(central_screen, "terminal_input", lambda **kw: nullcontext())
    monkeypatch.setattr(central_screen, "read_key", key)
    monkeypatch.setattr(central_screen.central_attach, "attach", lambda root, cid, io, **kw: attached.append((root, cid, io)))
    assert central_screen.run_screen(server.root, io=terminal_io, web_server=server) == 0
    assert len(attached) == 1 and attached[0] == (server.root, CID, terminal_io)
    assert not server.attach_bridge_active
    # The second queued intent remains unconsumed after q; no hidden session.
    assert server.attach_intents.qsize() == 1
    assert http(server, "/api/attach", {})[1]["command"] == "acc attach " + CID


@pytest.mark.parametrize("route,headers,payload", [
    ("/api/message", {"Origin": "https://hostile.example"}, {"text": "hello"}),
    ("/api/message", {"Host": "hostile.example"}, {"text": "hello"}),
    ("/api/message", {}, {"csrf_token": "wrong", "text": "hello"}),
    ("/api/attach", {}, {"argv": ["cmd"], "path": "elsewhere"}),
    ("/api/pause", {}, {"coordinator_id": str(uuid.uuid4())}),
])
def test_hostile_http_never_mutates_records(live, route, headers, payload):
    server, worktree, _ = live
    before = tree_bytes(server.root), tree_bytes(worktree)
    assert http(server, route, payload, headers)[0] in (400, 403, 404)
    assert before == (tree_bytes(server.root), tree_bytes(worktree))


def test_traversal_html_and_private_text_through_http(live):
    server, _, _ = live
    for path in ("/../registry.json", "/%2e%2e/registry.json", "/%252e%252e/registry.json", "/central.js?path=registry.json"):
        status, body = http(server, path)
        assert status == 404 and body == {"error": "route not found"}
    html = "<img src=x onerror=alert(1)>"
    qid = central_messages.send(server.root, CID, {"direction": "out", "kind": "question", "body": html})
    central_registry.update_record(server.root, CID, {"last_error": 'at "C:\\Private Person\\file" password=alpha beta gamma'})
    public = http(server, "/api/snapshot")[1]
    assert next(q for q in public["questions"] if q["id"] == qid)["body"] == html
    assert public["coordinators"][0]["last_error"] == "[redacted]"
    assert "worktree" not in public["coordinators"][0]
    # Decode JSON first: escaped backslashes cannot hide a leaking path.
    def strings(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for child in value.values():
                yield from strings(child)
        elif isinstance(value, list):
            for child in value:
                yield from strings(child)
    text = " ".join(strings(public))
    assert "Private Person" not in text and "beta gamma" not in text and str(server.root) not in text
    js = http(server, "/central.js")[1].decode()
    assert "textContent" in js and "innerHTML" not in js
