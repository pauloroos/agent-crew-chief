"""T037: real fake-executable processes, temporary Git/registry, no live Orca."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from crew_chief import central_launch, central_messages, central_registry


@pytest.fixture
def fixture(tmp_path, monkeypatch, git_repo):
    from crew_chief import central_orca
    script = tmp_path / "fake orca & unicode á.py"
    log = tmp_path / "calls.jsonl"
    worktree = tmp_path / "orca worktree & á"
    script.write_text('''
import json, os, pathlib, subprocess, sys
args = sys.argv[1:]
with open(os.environ['FAKE_ORCA_LOG'], 'a', encoding='utf-8') as f:
    f.write(json.dumps(args) + '\\n')
mode = os.environ.get('FAKE_ORCA_MODE', '')
if args == ['--version']:
    print('1.4.237' if mode != 'version' else 'unsupported')
elif args == ['--help']:
    print('orca agent-context worktree create --json')
elif args == ['agent-context', '--json']:
    flags = ['repo', 'name', 'setup', 'base-branch', 'no-parent', 'json']
    if mode == 'flags': flags.remove('setup')
    print(json.dumps({'schemaVersion': 99 if mode == 'schema' else 1,
                      'commands': [{'command':'worktree create', 'flags':flags}]}))
elif args[:2] == ['worktree', 'create']:
    repo = args[args.index('--repo')+1][5:]
    base = args[args.index('--base-branch')+1]
    name = args[args.index('--name')+1]
    wt = os.environ['FAKE_ORCA_WT']
    if mode == 'source':
        print(json.dumps({'ok':True,'result':{'id':'repo-fixture::'+repo}}))
        raise SystemExit(0)
    subprocess.run(['git','worktree','add','-b',name,wt,base], cwd=repo, check=True, capture_output=True)
    if mode == 'partial':
        print('creation reply lost; worktree preserved', file=sys.stderr)
        raise SystemExit(1)
    row = {'id':'repo-fixture::'+wt}
    print(json.dumps({'ok':mode != 'logical-error','result': row if mode == 'flat' else {'worktree':row}}))
else:
    raise SystemExit('unexpected command: '+repr(args))
''', encoding="utf-8")
    monkeypatch.setenv("FAKE_ORCA_LOG", str(log))
    monkeypatch.setenv("FAKE_ORCA_WT", str(worktree))
    monkeypatch.setenv("AGENT_CREW_CHIEF_CENTRAL_DIR", str(tmp_path / "central"))
    monkeypatch.setenv("CREW_CHIEF_COORDINATOR", "codex")
    original = central_orca._invoke

    def fake_invoke(executable, args):
        # Actual child process; Python replaces only the platform executable shim.
        assert executable == str(script)
        return original(sys.executable, [str(script), *args])

    monkeypatch.setattr(central_orca, "_invoke", fake_invoke)
    monkeypatch.setattr(central_launch.central_preflight, "preflight",
                        lambda *a: {"ok": True, "probe": {}, "acc": {},
                                    "authorization": {"request_digest": "fixture"}})
    request = tmp_path / "request.md"
    request.write_text("Implement the approved fixture.", encoding="utf-8")
    options = {"request_file": request, "mode": "raw-request", "authorize_raw": True,
               "release_root": tmp_path / "release", "backend": "orca"}
    backend = central_orca.orca_backend(tmp_path / "central", str(script))
    return backend, git_repo, options, log, worktree


def calls(log):
    return [json.loads(s) for s in log.read_text(encoding="utf-8").splitlines()]


def create(fixture):
    backend, repo, options, log, wt = fixture
    result = backend.create(repo, options)
    assert result["ok"], result
    return result["record"]


def test_probe_and_backend_contract(fixture):
    from crew_chief.central_backend import Backend
    backend, repo, options, log, wt = fixture
    assert isinstance(backend, Backend)
    assert backend.probe()["ok"]
    assert calls(log) == [["--version"], ["--help"], ["agent-context", "--json"]]


@pytest.mark.parametrize("mode", ["version", "schema", "flags"])
def test_incompatible_probe_prevents_any_creation(fixture, monkeypatch, mode):
    backend, repo, options, log, wt = fixture
    monkeypatch.setenv("FAKE_ORCA_MODE", mode)
    result = backend.create(repo, options)
    assert not result["ok"]
    assert "orca" in result["error"].lower()
    assert not wt.exists()
    assert not any(c[:2] == ["worktree", "create"] for c in calls(log))


def test_missing_binary_is_explicit(tmp_path):
    from crew_chief.central_orca import orca_backend
    result = orca_backend(tmp_path, str(tmp_path / "missing.exe")).probe()
    assert not result["ok"] and result["error"]


def test_create_uses_core_preflight_without_starting_any_agent(fixture):
    backend, repo, options, log, wt = fixture
    row = create(fixture)
    assert row["backend"] == "orca"
    assert row["session_id"] is None
    assert row["authorization"]["request_digest"] == "fixture"
    assert row["authorization"]["settings"]["central_backend"] == "orca"
    assert Path(row["worktree"]) == wt
    assert central_registry.load_registry(backend.root)["coordinators"][row["id"]] == row
    commands = calls(log)
    mutation = next(c for c in commands if c[:2] == ["worktree", "create"])
    assert mutation[mutation.index("--setup") + 1] == "skip"
    assert "--no-parent" in mutation
    assert all(c[:2] not in [["agent", "launch"], ["terminal", "send"]] for c in commands)
    assert not (repo.parent / (repo.name + ".crew")).exists()


def test_partial_creation_has_diagnostic_no_cleanup_and_no_fallback(fixture, monkeypatch):
    backend, repo, options, log, wt = fixture
    monkeypatch.setenv("FAKE_ORCA_MODE", "partial")
    result = backend.create(repo, options)
    assert not result["ok"] and "preserv" in result["error"]
    assert wt.exists()
    assert sum(c[:2] == ["worktree", "create"] for c in calls(log)) == 1
    assert not (repo.parent / (repo.name + ".crew")).exists()


def test_failed_preflight_keeps_registered_worktree_and_no_session(fixture, monkeypatch):
    backend, repo, options, log, wt = fixture
    monkeypatch.setattr(central_launch.central_preflight, "preflight",
                        lambda *a: {"ok": False, "error": "authorization denied"})
    result = backend.create(repo, options)
    assert not result["ok"] and result["error"] == "authorization denied"
    assert result["record"]["state"] == "failed"
    assert result["record"]["session_id"] is None
    assert wt.exists()


def test_start_send_attach_and_state_preserve_core_session_and_ids(fixture, monkeypatch):
    from crew_chief import central_attach, central_supervisor
    backend, repo, options, log, wt = fixture
    row = create(fixture)
    central_registry.update_record(backend.root, row["id"], {"session_id": "model-session-42"})
    state = backend.state(row)
    assert state["session_id"] == "model-session-42"
    mid = backend.send(row, central_messages.owner_message(row["id"], "text & $() á"))
    assert central_messages.pending(backend.root, row["id"], "in")[0]["id"] == mid
    monkeypatch.setattr(central_supervisor, "start_supervisor",
                        lambda r, root: {"ok": True, "coordinator_id": r["id"]})
    assert backend.start(row)["coordinator_id"] == row["id"]
    terminal = object()
    def attach(root, cid, term, timeout):
        assert term is terminal and timeout == 2
        assert central_registry.load_registry(root)["coordinators"][cid]["session_id"] == "model-session-42"
        return {"ok": True, "session_id": "model-session-42"}
    monkeypatch.setattr(central_attach, "attach", attach)
    assert backend.attach({**row, "terminal": terminal, "attach_timeout_s": 2})["session_id"] == "model-session-42"


def test_explicit_other_backend_is_not_silently_changed(fixture):
    backend, repo, options, log, wt = fixture
    result = backend.create(repo, {**options, "backend": "default"})
    assert not result["ok"] and "backend" in result["error"]
    assert not wt.exists()


def test_bad_request_starts_no_resource(fixture):
    backend, repo, options, log, wt = fixture
    result = backend.create(repo, {**options, "request_file": repo / "missing"})
    assert not result["ok"] and "request" in result["error"]
    assert not wt.exists()


def test_missing_request_option_is_a_diagnostic(fixture):
    backend, repo, options, log, wt = fixture
    options.pop("request_file")
    result = backend.create(repo, options)
    assert not result["ok"] and "request_file" in result["error"]
    assert not wt.exists()


def test_source_checkout_cannot_be_registered_as_new_worktree(fixture, monkeypatch):
    backend, repo, options, log, wt = fixture
    monkeypatch.setenv("FAKE_ORCA_MODE", "source")
    result = backend.create(repo, options)
    assert not result["ok"] and "source" in result["error"]
    assert not central_registry.load_registry(backend.root)["coordinators"]


def test_flat_documented_full_id_is_also_normalized(fixture, monkeypatch):
    monkeypatch.setenv("FAKE_ORCA_MODE", "flat")
    row = create(fixture)
    assert row["session_id"] is None and row["backend"] == "orca"


def test_json_failure_with_partial_worktree_is_not_success(fixture, monkeypatch):
    backend, repo, options, log, wt = fixture
    monkeypatch.setenv("FAKE_ORCA_MODE", "logical-error")
    result = backend.create(repo, options)
    assert not result["ok"] and "preserv" in result["error"]
    assert wt.exists()
    assert not central_registry.load_registry(backend.root)["coordinators"]
