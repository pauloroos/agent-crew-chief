"""Real loopback actions against fake registry identities and temporary inboxes."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.client import HTTPConnection
import json
from pathlib import Path
import threading
import uuid
import shutil
import subprocess

import pytest
from crew_chief import central_messages, central_registry, central_web

CID = "00000000-0000-4000-8000-000000000001"


@contextmanager
def running(tmp_path, monkeypatch):
    monkeypatch.setattr(central_registry, "load_registry",
                        lambda *a, **k: {"coordinators": {CID: {"id": CID, "backend": "default"}}})
    with serving(tmp_path / "central", tmp_path) as server:
        yield server


@contextmanager
def serving(root, assets):
    server = central_web.create_server(root, assets)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01})
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)
        assert not thread.is_alive()


def exchange(server, path, body=None, *, headers=None, method="POST", raw=None):
    headers = dict(headers or {})
    if method == "POST":
        headers.setdefault("Origin", f"http://127.0.0.1:{server.server_port}")
        headers.setdefault("Content-Type", "application/json")
        if raw is None:
            body = dict(body or {})
            body.setdefault("coordinator_id", CID)
            body.setdefault("csrf_token", server.csrf_token)
            body.setdefault("action_id", str(uuid.uuid4()))
            raw = json.dumps(body).encode()
    conn = HTTPConnection(*server.server_address, timeout=4)
    try:
        conn.request(method, path, body=raw, headers=headers)
        response = conn.getresponse()
        return response.status, json.loads(response.read())
    finally:
        conn.close()


def test_bootstrap_and_private_errors(tmp_path, monkeypatch):
    with running(tmp_path, monkeypatch) as server:
        status, data = exchange(server, "/api/csrf", method="GET")
        assert status == 200 and data["csrf_token"] == server.csrf_token
        assert len(data["csrf_token"]) >= 32
        assert exchange(server, "/api/csrf", method="GET", headers={"Origin": "https://evil.example"})[0] == 403
        assert not server.root.exists()


@pytest.mark.parametrize("token", [None, "wrong", 17])
def test_bad_token_has_no_effect(tmp_path, monkeypatch, token):
    with running(tmp_path, monkeypatch) as server:
        assert exchange(server, "/api/message", {"text": "hello", "csrf_token": token})[0] == 403
        assert not server.root.exists()


@pytest.mark.parametrize("headers", [{"Origin": ""}, {"Origin": "https://evil.example"},
    {"Origin": "null"}, {"Host": "evil.example"}, {"Content-Type": "text/plain"}])
def test_host_origin_and_type(tmp_path, monkeypatch, headers):
    with running(tmp_path, monkeypatch) as server:
        assert exchange(server, "/api/message", {"text": "hello"}, headers=headers)[0] in (400, 403, 415)
        assert not server.root.exists()


@pytest.mark.parametrize("payload", [{"coordinator_id": str(uuid.uuid4()), "text": "hello"},
    {"coordinator_id": "../../bad", "text": "hello"}, {"text": "hello", "argv": ["cmd"]},
    {"text": "hello", "path": "private"}, {"text": 7}, {"text": "x" * 65537}, {"action_id": "bad", "text": "hello"}])
def test_invalid_shape_and_unknown_coordinator(tmp_path, monkeypatch, payload):
    with running(tmp_path, monkeypatch) as server:
        status, data = exchange(server, "/api/message", payload)
        assert status in (400, 404, 413)
        assert str(tmp_path) not in json.dumps(data)
        assert not server.root.exists()


@pytest.mark.parametrize("raw", [b"[]", b"{bad", b' {"text":NaN}', b"[" * 1100 + b"]" * 1100,
    b'{"text":"first","text":"second"}', b"x" * (80 * 1024 + 1)], ids=lambda raw: "bytes-" + str(len(raw)))
def test_malformed_or_oversized(tmp_path, monkeypatch, raw):
    with running(tmp_path, monkeypatch) as server:
        assert exchange(server, "/api/message", raw=raw)[0] in (400, 413)
        assert not server.root.exists()


def test_message_deduplicates_concurrent_retries_and_conflicts(tmp_path, monkeypatch):
    with running(tmp_path, monkeypatch) as server:
        payload = {"action_id": str(uuid.uuid4()), "text": "hello"}
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: exchange(server, "/api/message", payload), range(4)))
        assert all(status == 200 for status, _ in results)
        assert all(data == results[0][1] for _, data in results)
        assert len(list(central_messages.pending(server.root, CID, "in"))) == 1
        assert exchange(server, "/api/message", {**payload, "text": "different"})[0] == 409


def test_answer_correlates_once(tmp_path, monkeypatch):
    with running(tmp_path, monkeypatch) as server:
        qid = central_messages.send(server.root, CID, {"direction": "out", "kind": "question", "body": "Choose", "task_refs": ["T1"]})
        first = exchange(server, "/api/answer", {"question_id": qid, "text": "yes"})
        second = exchange(server, "/api/answer", {"question_id": qid, "text": "yes"})
        assert first[0] == second[0] == 200 and first[1]["message_id"] == second[1]["message_id"]
        answers = list(central_messages.pending(server.root, CID, "in"))
        assert len(answers) == 1 and answers[0]["reply_to"] == qid
        assert exchange(server, "/api/answer", {"question_id": str(uuid.uuid4()), "text": "yes"})[0] == 400


@pytest.mark.parametrize("action", ["pause", "resume"])
def test_controls_once(tmp_path, monkeypatch, action):
    with running(tmp_path, monkeypatch) as server:
        payload = {"action_id": str(uuid.uuid4())}
        assert exchange(server, "/api/" + action, payload)[0] == 200
        assert exchange(server, "/api/" + action, payload)[0] == 200
        files = list(server.root.rglob("control/*.json"))
        assert len(files) == 1 and json.loads(files[0].read_text())["command"] == action


def test_attach_is_intent_or_copyable_fallback(tmp_path, monkeypatch):
    with running(tmp_path, monkeypatch) as server:
        status, data = exchange(server, "/api/attach")
        assert status == 200 and data["queued"] is False
        assert data["command"] == "acc attach " + CID
        assert server.attach_intents.empty()
        server.attach_bridge_active = True
        payload = {"action_id": str(uuid.uuid4())}
        assert exchange(server, "/api/attach", payload)[1]["queued"] is True
        assert exchange(server, "/api/attach", payload)[1]["queued"] is True
        assert server.attach_intents.get_nowait() == (CID, payload["action_id"])
        assert server.attach_intents.empty()


def test_a_fast_bridge_result_is_not_overwritten_by_queued(tmp_path, monkeypatch):
    """The queued state is written before the terminal thread can take the request."""
    import queue

    from crew_chief import central_screen

    with running(tmp_path, monkeypatch) as server:
        server.attach_bridge_active = True
        monkeypatch.setattr(central_screen, "_attach", lambda *a, **k: {"ok": False, "reason": "coordinator is stopped"})

        class EagerQueue(queue.Queue):
            def put_nowait(self, item):  # the terminal thread drains right after the put
                super().put_nowait(item)
                central_screen._drain_attach_intents(server.root, server, None)

        server.attach_intents = EagerQueue(maxsize=16)
        aid = str(uuid.uuid4())
        assert exchange(server, "/api/attach", {"action_id": aid})[1]["queued"] is True
        result = server.attach_results[aid]
        assert result["state"] == "failed" and "coordinator is stopped" in result["reason"]
        assert result["command"].startswith("acc attach " + CID)


def test_a_full_attach_queue_leaves_no_queued_result(tmp_path, monkeypatch):
    with running(tmp_path, monkeypatch) as server:
        server.attach_bridge_active = True
        for _ in range(16):
            assert exchange(server, "/api/attach")[0] == 200
        aid = str(uuid.uuid4())
        assert exchange(server, "/api/attach", {"action_id": aid})[0] == 503
        assert aid not in server.attach_results


def test_dedup_limit_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(central_web, "_MAX_ACTION_IDS", 1)
    with running(tmp_path, monkeypatch) as server:
        first = {"action_id": str(uuid.uuid4()), "text": "hello"}
        assert exchange(server, "/api/message", first)[0] == 200
        assert exchange(server, "/api/message", {"text": "second"})[0] == 503
        assert exchange(server, "/api/message", first)[0] == 200
        assert len(list(central_messages.pending(server.root, CID, "in"))) == 1


def test_public_privacy_short_credentials_and_space_paths():
    data = {"coordinators": [], "totals": {"api-key": "opaque", "APIKey": "opaque", "tokens_total": 7},
        "questions": [{"body": 'open "C:\\Private Person\\file" password=abc'}, {"body": "Bearer abc"}]}
    result = central_web.public_snapshot(data)
    encoded = json.dumps(result)
    assert "opaque" not in encoded and "abc" not in encoded and "Person" not in encoded
    assert result["totals"]["tokens_total"] == 7


def test_labelled_secret_with_spaces_never_leaves_fragments():
    phrase = "password=alpha beta gamma"
    data = {"coordinators": [], "questions": [{"body": phrase}]}
    result = central_web.public_snapshot(data)["questions"][0]["body"]
    for fragment in ("alpha", "beta", "gamma"):
        assert fragment not in result


def test_browser_actions_are_bootstrapped_and_guarded():
    js = (Path(__file__).resolve().parents[1] / "skill/crew-chief/web/central.js").read_text(encoding="utf-8")
    assert '"/api/csrf"' in js and "csrf_token" in js
    assert "randomUUID" in js and "action_id" in js
    for action in ("message", "answer", "pause", "resume", "stop", "attach"):
        assert '"/api/' + action + '"' in js
    assert "innerHTML" not in js and "textContent" in js


def test_invalid_unicode_is_refused(tmp_path, monkeypatch):
    with running(tmp_path, monkeypatch) as server:
        assert exchange(server, "/api/message", {"text": "\ud800"})[0] == 400
        assert not server.root.exists()


@pytest.mark.parametrize("headers", [{"Content-Length": "-1"}, {"Content-Length": "999999999"},
    {"Transfer-Encoding": "chunked"}])
def test_invalid_framing(tmp_path, monkeypatch, headers):
    with running(tmp_path, monkeypatch) as server:
        assert exchange(server, "/api/message", raw=b"{}", headers=headers)[0] in (400, 413)
        assert not server.root.exists()


def test_duplicate_security_headers(tmp_path, monkeypatch):
    with running(tmp_path, monkeypatch) as server:
        for duplicate in ("Host", "Origin"):
            conn = HTTPConnection(*server.server_address, timeout=3)
            try:
                conn.putrequest("POST", "/api/message", skip_host=True)
                for key, value in {"Host": f"127.0.0.1:{server.server_port}",
                    "Origin": f"http://127.0.0.1:{server.server_port}", "Content-Length": "2",
                    "Content-Type": "application/json"}.items():
                    conn.putheader(key, value)
                    if key == duplicate:
                        conn.putheader(key, value)
                conn.endheaders(b"{}")
                response = conn.getresponse()
                assert response.status == 403
                response.read()
            finally:
                conn.close()
        assert not server.root.exists()


def test_attach_queue_bounded_and_no_error_paths_leak(tmp_path, monkeypatch):
    with running(tmp_path, monkeypatch) as server:
        server.attach_bridge_active = True
        for _ in range(16):
            assert exchange(server, "/api/attach")[0] == 200
        assert exchange(server, "/api/attach")[0] == 503
        assert server.attach_intents.qsize() == 16
        def fail(*a, **k):
            raise central_messages.MessageError(str(tmp_path) + " password=abc")
        monkeypatch.setattr(central_messages, "send", fail)
        status, data = exchange(server, "/api/message", {"text": "hello"})
        assert status == 400 and str(tmp_path) not in json.dumps(data) and "abc" not in json.dumps(data)


def test_browser_enables_only_after_token_and_reuses_retry_id():
    node = shutil.which("node")
    if not node:
        pytest.skip("optional Node runtime unavailable")
    js = Path(__file__).resolve().parents[1] / "skill/crew-chief/web/central.js"
    runner = r'''
const fs=require('fs'), vm=require('vm'), assert=require('assert');
const elements={}, calls=[];
class Element {
  constructor(tag) {this.tag=tag;this.children=[];this.style={};this.listeners={};this.disabled=true;this._value='';this._text='';}
  set textContent(v) {this._text=String(v);this.children=[];}
  get textContent() {return this._text + this.children.map(c=>c.textContent).join(' ');}
  get firstChild() {return this.children[0];}
  get value() {return this._value || (this.children[0] && this.children[0].value) || '';}
  set value(v) {this._value=v;}
  appendChild(c) {c.parentNode=this;this.children.push(c);if(c.id) elements[c.id]=c;return c;}
  insertBefore(c) {this.appendChild(c);}
  removeChild(c) {this.children.splice(this.children.indexOf(c),1);}
  setAttribute(k,v) {this[k]=v;}
  addEventListener(k,f) {this.listeners[k]=f;}
  set innerHTML(v) {throw new Error('unsafe DOM');}
}
const toolbar=new Element('div');
const document={getElementById(id){if(!elements[id]) {elements[id]=new Element('div');toolbar.appendChild(elements[id]);} return elements[id];},
 createElement(tag){return new Element(tag);}};
let tokenResolve, fail=true, ids=0;
const csrf=new Promise(r=>tokenResolve=r);
const fixture={coordinators:[{id:'coord',repo_label:'demo'}],questions:[],totals:{}};
const fetch=async (url, opts={})=>{
 calls.push({url,opts});
 if(url==='/api/csrf') return csrf;
 if(url==='/api/snapshot') return {ok:true,json:async()=>fixture};
 if(url==='/api/message' && fail) {fail=false;throw new Error('lost response');}
 return {ok:true,json:async()=>({ok:true,queued:false,command:'acc attach coord'})};
};
const window={crypto:{randomUUID(){return 'action-'+(++ids);}},prompt(){return 'hello';}};
vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),{document,window,fetch,setTimeout(){},clearTimeout(){}});
const tick=()=>new Promise(setImmediate);
(async()=>{
 await tick();
 assert(elements['btn-message'].disabled);
 tokenResolve({ok:true,json:async()=>({csrf_token:'server-token'})});
 await tick(); await tick();
 assert(!elements['btn-message'].disabled);
 elements['btn-message'].listeners.click(); await tick();await tick();
 elements['btn-message'].listeners.click(); await tick();await tick();
 const posts=calls.filter(c=>c.url==='/api/message');
 assert.equal(posts.length,2);
 const bodies=posts.map(p=>JSON.parse(p.opts.body));
 assert.equal(bodies[0].action_id,bodies[1].action_id);
 assert.equal(bodies[0].csrf_token,'server-token');
 assert.equal(bodies[0].coordinator_id,'coord');
 assert.equal(bodies[0].text,'hello');
 assert(calls.every(c=>c.url.startsWith('/api/')));
 console.log('browser actions passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
    result = subprocess.run([node, "-e", runner, str(js)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert "browser actions passed" in result.stdout


def test_actions_route_by_the_stored_backend_like_the_cli(tmp_path, monkeypatch):
    """FR-017: the web routes as ``acc``: Orca rows need no Orca, unknown backends are refused."""
    from test_central_backend_cli import _stored_row

    monkeypatch.setenv("PATH", str(tmp_path / "empty"))  # no Orca anywhere
    root = tmp_path / "central"
    orca, mystery = _stored_row(tmp_path, "orca"), _stored_row(tmp_path, "mystery")
    for row in (orca, mystery):
        central_registry.register(root, row)
    with serving(root, tmp_path) as server:
        assert exchange(server, "/api/message", {"coordinator_id": orca["id"], "text": "hi"})[0] == 200
        assert exchange(server, "/api/pause", {"coordinator_id": orca["id"]})[0] == 200
        for path, extra in (("/api/message", {"text": "hi"}), ("/api/pause", {}), ("/api/attach", {})):
            refused = exchange(server, path, {"coordinator_id": mystery["id"], **extra})
            assert refused == (400, {"error": "action refused"})
    assert [m["body"] for m in central_messages.pending(root, orca["id"], "in")] == ["hi"]
    assert central_messages.pending(root, mystery["id"], "in") == []
    assert len(list((root / "coordinators" / orca["id"]).rglob("control/*.json"))) == 1
    assert not list((root / "coordinators" / mystery["id"]).rglob("control/*.json"))


def test_attach_fallback_keeps_an_explicit_central_dir_without_the_home_path(tmp_path, monkeypatch):
    from crew_chief import central_config

    home = Path.home()
    with running(tmp_path, monkeypatch) as server:
        assert exchange(server, "/api/attach")[1]["command"] == "acc attach " + CID
        server.central_dir = str(home / "my central")
        data = exchange(server, "/api/attach")[1]
    assert data["command"] == f'acc attach {CID} --central-dir "~/my central"'
    assert str(home) not in json.dumps(data)
    # The copied command reaches the same root in any shell: acc expands the ~ itself.
    assert central_config.resolve_root("~/my central", {}) == home / "my central"
