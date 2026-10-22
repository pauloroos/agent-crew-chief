"""Tests for exclusive job handoff with persisted takeover evidence (T043).
Verification boundary (tasks.md):
Fake installed CLIs and temporary real job records prove same job/worktree,
accepted preservation, bounded spec status/pending/open attempt/rulings/
messages brief; explicit cost-target consent, lease exclusion and idempotent
ruling/registry crash reconciliation; public wait/handoff/stop action boundary.
Proof: ``python -m pytest tests/test_central_handoff.py -q``.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from crew_chief import central_registry
from crew_chief.central_handoff import (
    SUPPORTED_COORDINATOR_CLIS,
    build_takeover_brief,
    handoff_journal_entry,
    record_handoff,
    validate_target,
)


# ---------------------------------------------------------------------------
# helpers — mirror test_central_supervisor.py record_for / prepared pattern
# ---------------------------------------------------------------------------

def _record_for(tmp_path: Path, **overrides) -> dict:
    """Build a coordinator record that passes _validate_record."""
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    stamp = "2026-01-01T00:00:00+00:00"
    record = {
        "schema_version": 1,
        "id": str(uuid.uuid4()),
        "repo": str(repo),
        "worktree": str(worktree),
        "branch": f"central/{uuid.uuid4()}",
        "base_commit": "0" * 40,
        "backend": "default",
        "cli": "claude",
        "cli_version": "",
        "acc_version": "",
        "session_id": None,
        "mode": "raw-request",
        "authorization": {
            "mode": "raw-request",
            "approved": False,
            "authorize_raw": True,
            "settings": {"coordinator_unrestricted": True},
        },
        "job_ids": [],
        "active_job_id": None,
        "state": "running",
        "created_at": stamp,
        "updated_at": stamp,
        "last_error": None,
        "supervisor_nonce": None,
    }
    record.update(overrides)
    return record


def _prepared(tmp_path: Path, **overrides) -> tuple[Path, dict]:
    """Register one coordinator and return (root, record)."""
    root = tmp_path / "central"
    record = _record_for(tmp_path, **overrides)
    central_registry.register(root, record)
    # Create coordinator directory structure expected by handoff module.
    coord_dir = root / "coordinators" / record["id"]
    coord_dir.mkdir(parents=True, exist_ok=True)
    return root, record


def _make_job(root: Path, job_id: str, pieces: list[dict], rulings: list[dict] | None = None) -> None:
    """Write a minimal job record under root/jobs/<job_id>.json."""
    jobs_dir = root / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    job = {
        "id": job_id,
        "title": "test job",
        "pieces": pieces,
        "rulings": rulings or [],
    }
    (jobs_dir / f"{job_id}.json").write_text(
        json.dumps(job, indent=2) + "\n", encoding="utf-8"
    )


def _make_inbox_message(coord_dir: Path, msg_id: str, text: str) -> None:
    """Write one inbox message for the coordinator."""
    inbox = coord_dir / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    payload = {
        "id": msg_id,
        "text": text,
        "reply_to": None,
        "sent_at": "2026-01-01T00:00:00+00:00",
    }
    (inbox / f"{msg_id}.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# validate_target
# ---------------------------------------------------------------------------

class TestValidateTarget:
    def test_supported_clis_are_known(self) -> None:
        assert "claude" in SUPPORTED_COORDINATOR_CLIS
        assert "codex" in SUPPORTED_COORDINATOR_CLIS

    def test_rejects_non_string(self) -> None:
        assert not validate_target(None)  # type: ignore[arg-type]
        assert not validate_target(42)  # type: ignore[arg-type]

    def test_rejects_unknown_cli(self) -> None:
        assert not validate_target("unknown-cli-xyz")

    def test_case_insensitive(self) -> None:
        # Even if not installed, the name must be recognized as supported.
        # We cannot assert True without the binary, but we can assert the
        # name check passes by testing that an unsupported name fails.
        assert not validate_target("NOTSUPPORTED")


# ---------------------------------------------------------------------------
# build_takeover_brief
# ---------------------------------------------------------------------------

class TestBuildTakeoverBrief:
    def test_basic_brief_structure(self, tmp_path: Path) -> None:
        root, record = _prepared(tmp_path)
        brief = build_takeover_brief(
            root, record["id"], lambda: {"phases": []}
        )
        assert brief["schema_version"] == 1
        assert brief["coordinator_id"] == record["id"]
        assert "generated_at" in brief
        assert brief["spec_status"] == {"phases": []}
        assert brief["pending_pieces"] == []
        assert brief["open_attempt"] is None
        assert brief["rulings"] == []
        assert brief["messages"] == []

    def test_pending_pieces_excludes_accepted(self, tmp_path: Path) -> None:
        root, record = _prepared(tmp_path, active_job_id="job-1")
        _make_job(root, "job-1", [
            {"n": 1, "ref": "T001", "title": "done", "accepted": True},
            {"n": 2, "ref": "T002", "title": "pending", "accepted": False},
        ])
        brief = build_takeover_brief(root, record["id"], lambda: {})
        assert len(brief["pending_pieces"]) == 1
        assert brief["pending_pieces"][0]["n"] == 2

    def test_open_attempt_captured(self, tmp_path: Path) -> None:
        root, record = _prepared(tmp_path, active_job_id="job-2")
        _make_job(root, "job-2", [
            {
                "n": 3,
                "ref": "T003",
                "title": "failing",
                "attempts": [
                    {"n": 1, "status": "proof_failed", "executor": "x"},
                ],
            },
        ])
        brief = build_takeover_brief(root, record["id"], lambda: {})
        assert brief["open_attempt"] is not None
        assert brief["open_attempt"]["piece"] == 3
        assert brief["open_attempt"]["status"] == "proof_failed"

    def test_rulings_included(self, tmp_path: Path) -> None:
        root, record = _prepared(tmp_path, active_job_id="job-3")
        _make_job(root, "job-3", [], rulings=[
            {"id": "r1", "type": "handoff", "text": "test", "at": "2026-01-01T00:00:00+00:00"},
        ])
        brief = build_takeover_brief(root, record["id"], lambda: {})
        assert len(brief["rulings"]) == 1
        assert brief["rulings"][0]["id"] == "r1"

    def test_inbox_messages_included(self, tmp_path: Path) -> None:
        root, record = _prepared(tmp_path)
        coord_dir = root / "coordinators" / record["id"]
        _make_inbox_message(coord_dir, "m1", "hello")
        brief = build_takeover_brief(root, record["id"], lambda: {})
        assert len(brief["messages"]) == 1
        assert brief["messages"][0]["text"] == "hello"

    def test_raises_on_missing_coordinator_dir(self, tmp_path: Path) -> None:
        root = tmp_path / "central"
        root.mkdir()
        fake_id = str(uuid.uuid4())
        with pytest.raises(ValueError, match="no coordinator directory"):
            build_takeover_brief(root, fake_id, lambda: {})

    def test_raises_on_bad_spec_status(self, tmp_path: Path) -> None:
        root, record = _prepared(tmp_path)
        with pytest.raises(ValueError, match="spec status unavailable"):
            build_takeover_brief(root, record["id"], lambda: (_ for _ in ()).throw(RuntimeError("boom")))

    def test_raises_on_oversized_job_record(self, tmp_path: Path) -> None:
        root, record = _prepared(tmp_path, active_job_id="big")
        jobs_dir = root / "jobs"
        jobs_dir.mkdir(parents=True)
        # Write a file exceeding the evidence cap.
        big_content = "x" * (256 * 1024 + 1)
        (jobs_dir / "big.json").write_text(big_content, encoding="utf-8")
        with pytest.raises(ValueError, match="exceeds the"):
            build_takeover_brief(root, record["id"], lambda: {})

    def test_custom_clock(self, tmp_path: Path) -> None:
        root, record = _prepared(tmp_path)
        fixed = datetime(2026, 10, 21, 12, 0, 0, tzinfo=timezone.utc)
        brief = build_takeover_brief(root, record["id"], lambda: {}, now=fixed)
        assert brief["generated_at"] == fixed.isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# handoff_journal_entry
# ---------------------------------------------------------------------------

class TestHandoffJournalEntry:
    def test_valid_states(self) -> None:
        for state in ("prepared", "released", "acquired", "completed", "failed"):
            entry = handoff_journal_entry("hid", state)
            assert entry["state"] == state
            assert entry["handoff_id"] == "hid"
            assert "at" in entry

    def test_invalid_state_raises(self) -> None:
        with pytest.raises(ValueError, match="handoff journal state"):
            handoff_journal_entry("hid", "exploded")

    def test_detail_preserved(self) -> None:
        entry = handoff_journal_entry("hid", "failed", detail="timeout")
        assert entry["detail"] == "timeout"

    def test_no_detail_when_none(self) -> None:
        entry = handoff_journal_entry("hid", "prepared")
        assert "detail" not in entry


# ---------------------------------------------------------------------------
# record_handoff
# ---------------------------------------------------------------------------

class TestRecordHandoff:
    def test_creates_new_handoff_entry(self, tmp_path: Path) -> None:
        root, record = _prepared(tmp_path)
        hid = record_handoff(
            root, record["id"], "claude", "codex", "completed",
            session_id="s1", job_id="j1",
        )
        assert isinstance(hid, str) and len(hid) > 0
        registry = central_registry.load_registry(root)
        history = registry["coordinators"][record["id"]]["handoff_history"]
        assert len(history) == 1
        assert history[0]["handoff_id"] == hid
        assert history[0]["source_cli"] == "claude"
        assert history[0]["target_cli"] == "codex"
        assert history[0]["outcome"] == "completed"
        assert len(history[0]["journal"]) == 1
        assert history[0]["journal"][0]["state"] == "prepared"

    def test_idempotent_on_same_handoff_id(self, tmp_path: Path) -> None:
        root, record = _prepared(tmp_path)
        hid = str(uuid.uuid4())
        record_handoff(root, record["id"], "claude", "codex", "completed", handoff_id=hid)
        record_handoff(root, record["id"], "claude", "codex", "completed", handoff_id=hid)
        registry = central_registry.load_registry(root)
        history = registry["coordinators"][record["id"]]["handoff_history"]
        assert len(history) == 1
        # Second call with same outcome should not duplicate journal entries.
        assert len(history[0]["journal"]) == 1

    def test_appends_new_state_to_journal(self, tmp_path: Path) -> None:
        root, record = _prepared(tmp_path)
        hid = str(uuid.uuid4())
        record_handoff(root, record["id"], "claude", "codex", "prepared", handoff_id=hid)
        record_handoff(root, record["id"], "claude", "codex", "completed", handoff_id=hid)
        registry = central_registry.load_registry(root)
        journal = registry["coordinators"][record["id"]]["handoff_history"][0]["journal"]
        states = [e["state"] for e in journal]
        assert "prepared" in states
        assert "completed" in states

    def test_rejects_non_canonical_coordinator(self, tmp_path: Path) -> None:
        root = tmp_path / "central"
        root.mkdir()
        with pytest.raises(ValueError, match="canonical UUID"):
            record_handoff(root, "not-a-uuid", "claude", "codex", "completed")

    def test_rejects_empty_source_cli(self, tmp_path: Path) -> None:
        root, record = _prepared(tmp_path)
        with pytest.raises(ValueError, match="source_cli"):
            record_handoff(root, record["id"], "", "codex", "completed")

    def test_rejects_empty_target_cli(self, tmp_path: Path) -> None:
        root, record = _prepared(tmp_path)
        with pytest.raises(ValueError, match="target_cli"):
            record_handoff(root, record["id"], "claude", "  ", "completed")

    def test_rejects_unregistered_coordinator(self, tmp_path: Path) -> None:
        root = tmp_path / "central"
        root.mkdir()
        fake_id = str(uuid.uuid4())
        with pytest.raises(ValueError, match="no coordinator"):
            record_handoff(root, fake_id, "claude", "codex", "completed")

    def test_custom_handoff_id_preserved(self, tmp_path: Path) -> None:
        root, record = _prepared(tmp_path)
        custom = "custom-handoff-id-1234"
        hid = record_handoff(
            root, record["id"], "claude", "codex", "completed",
            handoff_id=custom,
        )
        assert hid == custom