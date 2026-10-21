"""Tests for versioned coordinator registry records (T003, FR-006/022/024).

The registry keeps one versioned JSON file per central root and never touches
real ``~/.config``: every test injects a temporary central root.
"""

from __future__ import annotations

import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from crew_chief import records
from crew_chief.central_registry import (
    RECORD_MAX_BYTES,
    REGISTRY_READ_MAX_BYTES,
    SCHEMA_VERSION,
    RegistryError,
    RegistryLockTimeout,
    load_registry,
    owner_lease,
    register,
    update_record,
)

BOM = b"\xef\xbb\xbf"


def _root(tmp_path: Path) -> Path:
    return tmp_path / "central"


def _record(tmp_path: Path, **overrides) -> dict:
    """A fully valid coordinator record."""
    coordinator_id = overrides.get("id", str(uuid.uuid4()))
    base = {
        "schema_version": SCHEMA_VERSION,
        "id": coordinator_id,
        "repo": str(tmp_path / "repo"),
        "worktree": str(tmp_path / "repo.crew" / "worktree-a"),
        "branch": "acc/coordinator-a",
        "base_commit": "0123456789abcdef0123456789abcdef01234567",
        "backend": "default",
        "cli": "claude",
        "cli_version": "1.0.0",
        "acc_version": "0.4.0",
        "session_id": None,
        "mode": "approved-spec",
        "authorization": {"raw": False, "request_digest": "digest", "at": "2026-01-01T00:00:00+00:00"},
        "job_ids": [],
        "active_job_id": None,
        "state": "created",
        "last_error": None,
    }
    base.update(overrides)
    return base


def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _write_registry(root: Path, data: dict, *, bom: bool = False, crlf: bool = False) -> None:
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    if crlf:
        text = text.replace("\n", "\r\n")
    blob = text.encode("utf-8")
    if bom:
        blob = BOM + blob
    _write_bytes(root / "registry.json", blob)


# --- load_registry ---------------------------------------------------------


def test_load_missing_registry_is_empty(tmp_path):
    result = load_registry(_root(tmp_path))
    assert result == {
        "schema_version": SCHEMA_VERSION,
        "coordinators": {},
        "errors": [],
    }


def test_register_persists_record_with_defaults(tmp_path):
    root = _root(tmp_path)
    record = _record(tmp_path)
    returned = register(root, record)
    assert returned == record["id"]

    data = load_registry(root)
    assert data["errors"] == []
    stored = data["coordinators"][record["id"]]
    assert stored["schema_version"] == SCHEMA_VERSION
    assert stored["session_id"] is None
    assert stored["job_ids"] == []
    assert stored["active_job_id"] is None
    assert stored["last_error"] is None
    assert stored["created_at"]
    assert stored["updated_at"] == stored["created_at"]


def test_register_generates_canonical_uuid_id_when_absent(tmp_path):
    root = _root(tmp_path)
    record = _record(tmp_path)
    del record["id"]
    returned = register(root, record)
    assert str(uuid.UUID(returned)) == returned  # canonical form
    data = load_registry(root)
    assert list(data["coordinators"]) == [returned]
    assert data["coordinators"][returned]["id"] == returned


def test_register_rejects_invalid_records(tmp_path):
    root = _root(tmp_path)
    cases = [
        ({"id": "not-a-uuid"}, "canonical UUID"),
        ({"id": ".."}, "canonical UUID"),
        ({"schema_version": 2}, "schema_version"),
        ({"schema_version": None}, "schema_version"),
        ({"repo": "relative/path"}, "absolute"),
        ({"worktree": ""}, "absolute"),
        ({"cli": ""}, "non-empty"),
        ({"mode": ""}, "non-empty"),
        ({"authorization": "yes"}, "authorization"),
        ({"job_ids": "job-1"}, "job_ids"),
        ({"unknown_extra": 1}, "unknown_extra"),
    ]
    for index, (overrides, fragment) in enumerate(cases):
        record = _record(tmp_path, id=str(uuid.uuid4()), **overrides)
        with pytest.raises(RegistryError, match=fragment):
            register(root, record)
        assert load_registry(root)["coordinators"] == {}, f"case {index} wrote anyway"


def test_register_rejects_duplicate_id(tmp_path):
    root = _root(tmp_path)
    record = _record(tmp_path, state="first")
    register(root, record)
    duplicate = _record(tmp_path, id=record["id"], state="second")
    with pytest.raises(RegistryError, match="already registered"):
        register(root, duplicate)
    assert load_registry(root)["coordinators"][record["id"]]["state"] == "first"


def test_register_never_mutates_or_aliases_the_callers_record(tmp_path):
    root = _root(tmp_path)
    record = _record(tmp_path)
    register(root, record)
    record["state"] = "caller-changed"
    record["job_ids"].append("mutated")
    stored = load_registry(root)["coordinators"][record["id"]]
    assert stored["state"] == "created"
    assert stored["job_ids"] == []


def test_update_record_merges_and_bumps_updated_at(tmp_path):
    root = _root(tmp_path)
    record = _record(tmp_path)
    register(root, record)

    updated = update_record(root, record["id"], {"session_id": "sess-1", "job_ids": ["j1"]})
    assert updated["session_id"] == "sess-1"
    assert updated["job_ids"] == ["j1"]
    assert updated["created_at"]

    stored = load_registry(root)["coordinators"][record["id"]]
    assert stored["session_id"] == "sess-1"
    assert stored["job_ids"] == ["j1"]
    assert stored["updated_at"] != stored["created_at"]  # bumped on update
    assert stored["created_at"] == updated["created_at"]


def test_update_record_returns_detached_copy(tmp_path):
    root = _root(tmp_path)
    record = _record(tmp_path)
    register(root, record)
    updated = update_record(root, record["id"], {"state": "running"})
    updated["state"] = "touched-by-caller"
    stored = load_registry(root)["coordinators"][record["id"]]
    assert stored["state"] == "running"


def test_update_record_missing_coordinator(tmp_path):
    root = _root(tmp_path)
    register(root, _record(tmp_path))
    with pytest.raises(RegistryError, match="no coordinator"):
        update_record(root, str(uuid.uuid4()), {"state": "running"})


def test_update_record_rejects_invalid_coordinator_ids(tmp_path):
    root = _root(tmp_path)
    for bad in ["..", "x/../y", "not-a-uuid", "", "C:\\evil", "\x00"]:
        with pytest.raises(RegistryError, match="UUID"):
            update_record(root, bad, {"state": "running"})


def test_update_record_rejects_immutable_fields(tmp_path):
    root = _root(tmp_path)
    record = _record(tmp_path)
    register(root, record)
    for key, value in [
        ("id", str(uuid.uuid4())),
        ("schema_version", 2),
        ("created_at", "2000-01-01T00:00:00+00:00"),
    ]:
        with pytest.raises(RegistryError, match=f"must not modify .*{key}"):
            update_record(root, record["id"], {key: value})


def test_update_record_rejects_invalid_merged_result(tmp_path):
    root = _root(tmp_path)
    record = _record(tmp_path)
    register(root, record)
    with pytest.raises(RegistryError, match="state"):
        update_record(root, record["id"], {"state": ""})
    # The stored record is untouched by a rejected update.
    assert load_registry(root)["coordinators"][record["id"]]["state"] == "created"


# --- per-row error isolation (FR-024) ---------------------------------------


def test_load_registry_isolates_corrupt_rows(tmp_path):
    root = _root(tmp_path)
    good1, bad_key, good2 = str(uuid.uuid4()), "x/../y", str(uuid.uuid4())
    _write_registry(
        root,
        {
            "schema_version": SCHEMA_VERSION,
            "coordinators": {
                good1: _record(tmp_path, id=good1),
                bad_key: {"id": bad_key},
                "not-an-object": ["surprise"],
                good2: dict(_record(tmp_path, id=good2), repo="relative/repo"),
            },
        },
    )
    data = load_registry(root)
    assert set(data["coordinators"]) == {good1, good2}
    reported = {entry["record"] for entry in data["errors"]}
    assert {"x/../y", "not-an-object", good2} <= reported


def test_unknown_schema_row_survives_rewrites(tmp_path):
    root = _root(tmp_path)
    foreign, current = str(uuid.uuid4()), str(uuid.uuid4())
    _write_registry(
        root,
        {
            "schema_version": SCHEMA_VERSION,
            "coordinators": {
                foreign: {"schema_version": 11, "id": foreign, "note": "keep me"},
                current: _record(tmp_path, id=current),
            },
        },
    )
    data = load_registry(root)
    assert set(data["coordinators"]) == {current}
    assert any(foreign in entry["record"] for entry in data["errors"])
    assert any("schema" in entry["message"] for entry in data["errors"])

    # A normal update keeps the foreign row exactly as it is.
    update_record(root, current, {"state": "running"})
    data = load_registry(root)
    assert set(data["coordinators"]) == {current}
    assert data["errors"], "foreign row must still be reported after the rewrite"
    raw = json.loads((root / "registry.json").read_text(encoding="utf-8-sig"))
    assert raw["coordinators"][foreign] == {"schema_version": 11, "id": foreign, "note": "keep me"}


def test_update_refuses_incompatible_row(tmp_path):
    root = _root(tmp_path)
    foreign = str(uuid.uuid4())
    _write_registry(
        root,
        {
            "schema_version": SCHEMA_VERSION,
            "coordinators": {foreign: {"schema_version": 11, "id": foreign}},
        },
    )
    with pytest.raises(RegistryError, match="schema"):
        update_record(root, foreign, {"state": "running"})


def test_load_registry_reports_structural_errors(tmp_path):
    root = _root(tmp_path)
    _write_registry(root, {"schema_version": SCHEMA_VERSION, "coordinators": ["not", "a", "map"]})
    data = load_registry(root)
    assert data["coordinators"] == {}
    assert data["errors"] and data["errors"][0]["record"] == "<registry>"

    _write_registry(root, ["not", "an", "object"])
    data = load_registry(root)
    assert data["coordinators"] == {}
    assert data["errors"][0]["record"] == "<registry>"

    _write_registry(root, {"coordinators": {}})  # schema_version missing
    data = load_registry(root)
    assert data["coordinators"] == {}
    assert data["errors"][0]["record"] == "<registry>"

    _write_registry(root, {"schema_version": 99, "coordinators": {}})
    data = load_registry(root)
    assert data["coordinators"] == {}
    assert data["errors"][0]["record"] == "<registry>"


def test_register_refuses_corrupt_registry_without_rewriting(tmp_path):
    root = _root(tmp_path)
    before = b"{corrupt"
    _write_bytes(root / "registry.json", before)
    with pytest.raises(RegistryError, match="invalid JSON|valid JSON"):
        register(root, _record(tmp_path))
    assert (root / "registry.json").read_bytes() == before


def test_registry_reads_are_bounded(tmp_path):
    root = _root(tmp_path)
    huge = "x" * (REGISTRY_READ_MAX_BYTES + 1)
    _write_registry(root, {"schema_version": SCHEMA_VERSION, "coordinators": {}, "pad": huge})
    with pytest.raises(RegistryError, match=f"cap|{REGISTRY_READ_MAX_BYTES}"):
        load_registry(root)
    with pytest.raises(RegistryError, match="cap|8"):
        register(root, _record(tmp_path))  # refused, file untouched


def test_record_rows_are_bounded_and_isolated(tmp_path):
    root = _root(tmp_path)
    oversized, good = str(uuid.uuid4()), str(uuid.uuid4())
    bad_row = dict(_record(tmp_path, id=oversized), last_error="x" * (RECORD_MAX_BYTES + 1))
    _write_registry(
        root,
        {
            "schema_version": SCHEMA_VERSION,
            "coordinators": {oversized: bad_row, good: _record(tmp_path, id=good)},
        },
    )
    data = load_registry(root)
    assert set(data["coordinators"]) == {good}
    assert any(oversized in entry["record"] for entry in data["errors"])

    # The isolated oversized row survives a rewrite of the good row.
    update_record(root, good, {"state": "running"})
    data = load_registry(root)
    assert set(data["coordinators"]) == {good}
    assert any(oversized in entry["record"] for entry in data["errors"])


# --- concurrency and atomicity (FR-006) -------------------------------------


def test_concurrent_register_and_updates_lose_no_entries(tmp_path):
    root = _root(tmp_path)
    seed = _record(tmp_path)
    register(root, seed)

    def _writer(i):
        record = _record(
            tmp_path,
            id=str(uuid.uuid4()),
            branch=f"acc/coordinator-{i}",
            state=f"from-{i}",
        )
        register(root, record)
        update_record(root, seed["id"], {"session_id": f"sess-{i}"})

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(_writer, range(6)))

    data = load_registry(root)
    assert data["errors"] == []
    assert len(data["coordinators"]) == 7  # the seed + 6 registrants


def test_concurrent_field_updates_do_not_lose_each_other(tmp_path):
    root = _root(tmp_path)
    record = _record(tmp_path)
    register(root, record)

    def _patch(key, value):
        update_record(root, record["id"], {key: value})

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(_patch, "session_id", "sess-A"),
            pool.submit(_patch, "state", "running"),
        ]
        for future in futures:
            future.result()

    stored = load_registry(root)["coordinators"][record["id"]]
    assert stored["session_id"] == "sess-A"
    assert stored["state"] == "running"


def test_atomic_write_interruption_keeps_old_records(tmp_path, monkeypatch):
    root = _root(tmp_path)
    record = _record(tmp_path)
    register(root, record)
    before = (root / "registry.json").read_bytes()

    def _broken_replace(*args, **kwargs):
        raise OSError("simulated crash between temp file and replace")

    monkeypatch.setattr(os, "replace", _broken_replace)
    with pytest.raises(OSError, match="simulated crash"):
        update_record(root, record["id"], {"state": "running"})
    monkeypatch.undo()

    assert (root / "registry.json").read_bytes() == before
    assert load_registry(root)["coordinators"][record["id"]]["state"] == "created"
    leftovers = [p.name for p in root.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []  # write_atomic cleaned its temp file up


def test_stray_temp_files_are_ignored(tmp_path):
    root = _root(tmp_path)
    record = _record(tmp_path)
    register(root, record)
    (root / "registry.json.ab12cd.tmp").write_bytes(b'{"half-a": "write"', encoding="utf-8")
    data = load_registry(root)
    assert record["id"] in data["coordinators"]
    register(root, _record(tmp_path, id=str(uuid.uuid4())))
    assert len(load_registry(root)["coordinators"]) == 2


# --- BOM / CRLF style (FR-022) ----------------------------------------------


def test_fresh_registry_writes_lf_without_bom(tmp_path):
    root = _root(tmp_path)
    register(root, _record(tmp_path))
    blob = (root / "registry.json").read_bytes()
    assert not blob.startswith(BOM)
    assert b"\r\n" not in blob


def test_existing_bom_and_crlf_style_survive_rewrites(tmp_path):
    root = _root(tmp_path)
    first = _record(tmp_path)
    register(root, first)
    blob = (root / "registry.json").read_bytes()
    data = json.loads(blob.decode("utf-8-sig"))
    body = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    _write_bytes(root / "registry.json", BOM + body.replace("\n", "\r\n").encode("utf-8"))

    second = _record(tmp_path, id=str(uuid.uuid4()))
    register(root, second)
    rewrite = (root / "registry.json").read_bytes()
    assert rewrite.startswith(BOM), "BOM must survive"
    assert b"\r\n" in rewrite, "CRLF style must survive"

    updated = update_record(root, first["id"], {"session_id": "sess-CRLF"})
    assert updated["session_id"] == "sess-CRLF"
    rewrite = (root / "registry.json").read_bytes()
    assert rewrite.startswith(BOM) and b"\r\n" in rewrite
    data = load_registry(root)
    assert len(data["coordinators"]) == 2
    assert data["coordinators"][first["id"]]["session_id"] == "sess-CRLF"


# --- registry lock (bounded, no unsafe takeover) -----------------------------


def test_registry_lock_is_released_after_writes(tmp_path):
    root = _root(tmp_path)
    register(root, _record(tmp_path))
    assert not (root / "registry.lock").exists()


def test_contested_registry_lock_times_out_and_stays_taken(tmp_path):
    root = _root(tmp_path)
    host = root / "registry.lock"
    stub = json.dumps({"nonce": "ghost-owner", "pid": 999999, "acquired_at": "2000-01-01T00:00:00+00:00"})
    _write_bytes(host, stub.encode("utf-8"))

    with pytest.raises(RegistryLockTimeout, match="explicit recovery"):
        update_record(root, str(uuid.uuid4()), {}, timeout_s=0.2)

    # The holder's lock survived: age and PID alone never authorize removal.
    assert host.read_text(encoding="utf-8") == stub

    # Explicit recovery (the owner releases) restores normal operation.
    host.unlink()
    register(root, _record(tmp_path))
    assert not (root / "registry.lock").exists()


# --- owner leases (plan: supervisor lease, owner nonce) ----------------------


def test_owner_lease_acquire_release_and_reacquire(tmp_path):
    root = _root(tmp_path)
    coordinator_id = str(uuid.uuid4())
    with owner_lease(root, coordinator_id, "nonce-a") as lease:
        assert lease["nonce"] == "nonce-a"
        assert lease["pid"] == os.getpid()
        host = root / "coordinators" / coordinator_id / "supervisor.lock"
        assert host.is_file()
        payload = json.loads(host.read_text(encoding="utf-8"))
        assert payload["nonce"] == "nonce-a"
        assert payload["acquired_at"]
    # released on exit
    assert not host.exists()
    with owner_lease(root, coordinator_id, "nonce-b"):
        payload = json.loads(host.read_text(encoding="utf-8"))
        assert payload["nonce"] == "nonce-b"


def test_owner_lease_same_nonce_is_reentrant(tmp_path):
    root = _root(tmp_path)
    coordinator_id = str(uuid.uuid4())
    with owner_lease(root, coordinator_id, "nonce-a", timeout_s=5.0):
        with owner_lease(root, coordinator_id, "nonce-a", timeout_s=5.0):
            pass  # same owner: renewal, never a timeout
        payload = json.loads(
            (root / "coordinators" / coordinator_id / "supervisor.lock").read_text(encoding="utf-8")
        )
        assert payload["nonce"] == "nonce-a"


def test_contested_owner_lease_times_out_without_takeover(tmp_path):
    root = _root(tmp_path)
    coordinator_id = str(uuid.uuid4())
    with owner_lease(root, coordinator_id, "nonce-a"):
        host = root / "coordinators" / coordinator_id / "supervisor.lock"
        with pytest.raises(RegistryLockTimeout, match="explicit recovery"):
            with owner_lease(root, coordinator_id, "nonce-b", timeout_s=0.2):
                pass
        # Still bound to the legitimate owner.
        payload = json.loads(host.read_text(encoding="utf-8"))
        assert payload["nonce"] == "nonce-a"
        assert payload["pid"]
    # The stale-looking lock (old mtime) is likewise never taken over.
    host.parent.mkdir(parents=True, exist_ok=True)
    ghost = json.dumps({"nonce": "ghost", "pid": 1, "acquired_at": "2000-01-01T00:00:00+00:00"})
    host.write_text(ghost, encoding="utf-8")
    os.utime(host, (0, 0))
    with pytest.raises(RegistryLockTimeout, match="explicit recovery"):
        with owner_lease(root, coordinator_id, "nonce-b", timeout_s=0.2):
            pass
    assert json.loads(host.read_text(encoding="utf-8"))["nonce"] == "ghost"


def test_owner_lease_released_when_the_body_raises(tmp_path):
    root = _root(tmp_path)
    coordinator_id = str(uuid.uuid4())
    with pytest.raises(RuntimeError, match="boom"):
        with owner_lease(root, coordinator_id, "nonce-a"):
            raise RuntimeError("boom")
    host = root / "coordinators" / coordinator_id / "supervisor.lock"
    assert not host.exists()
    with owner_lease(root, coordinator_id, "nonce-b", timeout_s=5.0):
        pass


def test_owner_lease_never_touches_the_registry_file(tmp_path):
    root = _root(tmp_path)
    coordinator_id = str(uuid.uuid4())
    with owner_lease(root, coordinator_id, "nonce-a"):
        assert not (root / "registry.json").exists()
        data = load_registry(root)
        assert data["coordinators"] == {} and data["errors"] == []


def test_owner_lease_rejects_traversal_and_symlink_escapes(tmp_path):
    root = _root(tmp_path)
    for bad in ["..", "x/../y", "not-a-uuid"]:
        with pytest.raises(RegistryError, match="UUID"):
            owner_lease(root, bad, "nonce-a")
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "coordinators" / str(uuid.uuid4())
    try:
        link.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(outside, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available in this environment")
    with pytest.raises(RegistryError, match="symbolic link|symlink"):
        owner_lease(root, link.name, "nonce-a")
    assert (outside).is_dir() and not any(outside.iterdir()), "nothing written through the escape"


def test_owner_lease_requires_a_nonce(tmp_path):
    with pytest.raises(RegistryError, match="nonce"):
        owner_lease(_root(tmp_path), str(uuid.uuid4()), "")
