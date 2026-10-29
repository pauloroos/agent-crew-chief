"""Tests for FR-038: executor mode persistence, validation and legacy defaulting."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from crew_chief.cli import main
from crew_chief.records import (
    EXECUTOR_MODES,
    RecordError,
    job_executors,
    load_job,
    new_job,
    render_markdown,
    save_job,
    set_executors,
    update_job,
)

NOW = datetime(2026, 10, 25, 9, 0)


def _new_job(tmp_path: Path, **overrides) -> dict:
    kwargs = dict(
        title="Executor choice",
        request="Implement executor mode persistence.",
        distributed=True,
        reason="chosen: best score",
        now=NOW,
    )
    kwargs.update(overrides)
    return new_job(tmp_path / "project", **kwargs)


# --- new_job accepts --executors -------------------------------------------


def test_new_job_persists_valid_executor_mode(tmp_path):
    job = _new_job(tmp_path, executors="own")
    directory = tmp_path / "project" / "docs" / "crew-chief" / "jobs"
    data = json.loads((directory / f"{job['id']}.json").read_text(encoding="utf-8"))
    assert data["executors"] == "own"


def test_new_job_without_executors_defaults_to_free_first(tmp_path):
    """New jobs always persist 'executors'; legacy omission is only for old records."""
    job = _new_job(tmp_path)
    directory = tmp_path / "project" / "docs" / "crew-chief" / "jobs"
    data = json.loads((directory / f"{job['id']}.json").read_text(encoding="utf-8"))
    assert data["executors"] == "free_first"


def test_new_job_rejects_invalid_executor_mode(tmp_path):
    with pytest.raises(ValueError, match="invalid executor mode"):
        _new_job(tmp_path, executors="bogus")


# --- load_job defaults legacy records to free_first -----------------------


def test_load_job_legacy_defaults_to_free_first(tmp_path):
    job = _new_job(tmp_path)
    # Simulate a legacy record by removing the field from disk.
    directory = tmp_path / "project" / "docs" / "crew-chief" / "jobs"
    path = directory / f"{job['id']}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data.pop("executors", None)
    path.write_text(json.dumps(data), encoding="utf-8")

    loaded = load_job(tmp_path / "project", job["id"])
    assert loaded["executors"] == "free_first"

    # Disk must remain untouched.
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert "executors" not in on_disk


def test_load_job_preserves_explicit_mode(tmp_path):
    job = _new_job(tmp_path, executors="subagents")
    loaded = load_job(tmp_path / "project", job["id"])
    assert loaded["executors"] == "subagents"


# --- set_executors validates and records history --------------------------


def test_set_executors_updates_mode_and_history(tmp_path):
    job = _new_job(tmp_path, executors="free_first")
    project = tmp_path / "project"

    def apply(j: dict) -> None:
        set_executors(j, "own", source="cli")

    update_job(project, job["id"], apply)

    loaded = load_job(project, job["id"])
    assert loaded["executors"] == "own"
    assert len(loaded["executors_history"]) >= 1
    last = loaded["executors_history"][-1]
    assert last["mode"] == "own"
    assert last["source"] == "cli"
    assert "at" in last


def test_set_executors_rejects_invalid_mode(tmp_path):
    job = _new_job(tmp_path, executors="free_first")
    project = tmp_path / "project"

    def apply(j: dict) -> None:
        set_executors(j, "invalid", source="cli")

    with pytest.raises(ValueError, match="invalid executor mode"):
        update_job(project, job["id"], apply)

    # Mode must remain unchanged after a rejected update.
    loaded = load_job(project, job["id"])
    assert loaded["executors"] == "free_first"


def test_set_executors_does_not_touch_in_flight_attempts(tmp_path):
    """Mid-job switch affects only the next pick; existing attempts stay."""
    job = _new_job(tmp_path, executors="free_first")
    job.setdefault("pieces", []).append(
        {
            "n": 1,
            "title": "code piece",
            "work_type": "code",
            "risk": "low",
            "attempts": [
                {"executor": "opencode:m", "role": "exec", "status": "running"}
            ],
        }
    )
    save_job(tmp_path / "project", job)

    project = tmp_path / "project"

    def apply(j: dict) -> None:
        set_executors(j, "own", source="cli")

    update_job(project, job["id"], apply)

    loaded = load_job(project, job["id"])
    assert loaded["executors"] == "own"
    assert loaded["pieces"][0]["attempts"][0]["executor"] == "opencode:m"
    assert loaded["pieces"][0]["attempts"][0]["status"] == "running"


# --- validate_executor_mode covers every valid value ----------------------


@pytest.mark.parametrize("mode", EXECUTOR_MODES)
def test_validate_accepts_all_modes(tmp_path, mode):
    job = _new_job(tmp_path, executors=mode)
    loaded = load_job(tmp_path / "project", job["id"])
    assert loaded["executors"] == mode


# --- job_executors helper and BOM loading ---------------------------------


def test_job_executors_helper():
    assert job_executors({"executors": "own"}) == "own"
    assert job_executors({"executors": "subagents"}) == "subagents"
    assert job_executors({"executors": "free_first"}) == "free_first"
    assert job_executors({}) == "free_first"


def test_set_executors_project_signature(tmp_path):
    job = _new_job(tmp_path, executors="free_first")
    project = tmp_path / "project"

    loaded = set_executors(project, job["id"], "subagents", source="job set")
    assert loaded["executors"] == "subagents"
    history = loaded["executors_history"]
    assert len(history) == 1
    assert history[-1]["from"] == "free_first"
    assert history[-1]["to"] == "subagents"
    assert history[-1]["mode"] == "subagents"
    assert history[-1]["source"] == "job set"
    assert "at" in history[-1]


def test_load_job_handles_utf8_bom(tmp_path):
    job = _new_job(tmp_path, executors="own")
    path = tmp_path / "project" / "docs" / "crew-chief" / "jobs" / f"{job['id']}.json"
    raw = path.read_text(encoding="utf-8")
    # Write back with explicit UTF-8 BOM
    path.write_bytes(b"\xef\xbb\xbf" + raw.encode("utf-8"))

    loaded = load_job(tmp_path / "project", job["id"])
    assert loaded["id"] == job["id"]
    assert loaded["executors"] == "own"


def test_render_markdown_shows_executors(tmp_path):
    job = _new_job(tmp_path, executors="own")
    md = render_markdown(job)
    assert "- **Executors:** own" in md


# --- CLI: job new and job create alias ------------------------------------


def _req(tmp_path: Path) -> Path:
    p = tmp_path / "req.md"
    p.write_text("Test request\n", encoding="utf-8")
    return p


def test_cli_job_new_with_executors(tmp_path):
    project = tmp_path / "project"
    req = _req(tmp_path)
    code = main([
        "job", "new",
        "--project", str(project),
        "--title", "CLI new own",
        "--request-file", str(req),
        "--executors", "own",
    ])
    assert code == 0
    jobs = list((project / "docs" / "crew-chief" / "jobs").glob("*.json"))
    assert len(jobs) == 1
    data = json.loads(jobs[0].read_text(encoding="utf-8"))
    assert data["executors"] == "own"


def test_cli_job_create_alias(tmp_path):
    project = tmp_path / "project"
    req = _req(tmp_path)
    code = main([
        "job", "create",
        "--project", str(project),
        "--title", "CLI create subagents",
        "--request-file", str(req),
        "--executors", "subagents",
    ])
    assert code == 0
    jobs = list((project / "docs" / "crew-chief" / "jobs").glob("*.json"))
    assert len(jobs) == 1
    data = json.loads(jobs[0].read_text(encoding="utf-8"))
    assert data["executors"] == "subagents"


def test_cli_job_new_default_executors(tmp_path):
    project = tmp_path / "project"
    req = _req(tmp_path)
    code = main([
        "job", "new",
        "--project", str(project),
        "--title", "CLI default free_first",
        "--request-file", str(req),
    ])
    assert code == 0
    jobs = list((project / "docs" / "crew-chief" / "jobs").glob("*.json"))
    assert len(jobs) == 1
    data = json.loads(jobs[0].read_text(encoding="utf-8"))
    assert data["executors"] == "free_first"


def test_cli_job_new_rejects_invalid_executor(tmp_path, capsys):
    project = tmp_path / "project"
    req = _req(tmp_path)
    code = main([
        "job", "new",
        "--project", str(project),
        "--title", "CLI invalid",
        "--request-file", str(req),
        "--executors", "invalid",
    ])
    assert code == 2
    err = capsys.readouterr().err
    assert "invalid choice" in err or "invalid executor mode" in err


# --- CLI: job set ---------------------------------------------------------


def test_cli_job_set_updates_mode_atomically(tmp_path):
    job = _new_job(tmp_path, executors="free_first")
    project = tmp_path / "project"

    code = main([
        "job", "set",
        "--project", str(project),
        "--job", job["id"],
        "--executors", "own",
    ])
    assert code == 0

    loaded = load_job(project, job["id"])
    assert loaded["executors"] == "own"
    assert loaded["executors_history"][-1]["source"] == "job set"
    assert loaded["executors_history"][-1]["from"] == "free_first"
    assert loaded["executors_history"][-1]["to"] == "own"


def test_cli_job_set_rejects_invalid_mode(tmp_path, capsys):
    job = _new_job(tmp_path, executors="free_first")
    project = tmp_path / "project"

    code = main([
        "job", "set",
        "--project", str(project),
        "--job", job["id"],
        "--executors", "bogus",
    ])
    assert code == 2
    err = capsys.readouterr().err
    assert "invalid" in err

    loaded = load_job(project, job["id"])
    assert loaded["executors"] == "free_first"


def test_cli_job_set_missing_executors_flag(tmp_path, capsys):
    job = _new_job(tmp_path, executors="free_first")
    project = tmp_path / "project"

    code = main([
        "job", "set",
        "--project", str(project),
        "--job", job["id"],
    ])
    assert code == 2
    err = capsys.readouterr().err
    assert "--executors" in err


def test_cli_job_set_mid_job_pieces_in_flight(tmp_path):
    job = _new_job(tmp_path, executors="free_first")
    job.setdefault("pieces", []).append({
        "n": 1,
        "title": "Piece 1",
        "work_type": "code",
        "risk": "low",
        "attempts": [
            {"executor": "opencode:m", "role": "exec", "status": "running"}
        ],
    })
    save_job(tmp_path / "project", job)
    project = tmp_path / "project"

    code = main([
        "job", "set",
        "--project", str(project),
        "--job", job["id"],
        "--executors", "subagents",
    ])
    assert code == 0

    loaded = load_job(project, job["id"])
    assert loaded["executors"] == "subagents"
    assert len(loaded["pieces"]) == 1
    assert loaded["pieces"][0]["attempts"][0]["executor"] == "opencode:m"
    assert loaded["pieces"][0]["attempts"][0]["status"] == "running"


# --- CLI: job show --------------------------------------------------------


def test_cli_job_show_plain(tmp_path, capsys):
    job = _new_job(tmp_path, executors="free_first")
    project = tmp_path / "project"

    code = main([
        "job", "show",
        "--project", str(project),
        "--job", job["id"],
    ])
    assert code == 0
    out = capsys.readouterr().out
    assert f"job: {job['id']}" in out
    assert f"title: {job['title']}" in out
    assert "executors: free_first" in out


def test_cli_job_show_own_with_coordinator_flag(tmp_path, capsys):
    job = _new_job(tmp_path, executors="own")
    project = tmp_path / "project"

    code = main([
        "job", "show",
        "--project", str(project),
        "--job", job["id"],
        "--coordinator", "claude",
    ])
    assert code == 0
    out = capsys.readouterr().out
    assert "executors: own (claude)" in out


def test_cli_job_show_subagents_with_env_coordinator(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CREW_CHIEF_COORDINATOR", "opencode")
    job = _new_job(tmp_path, executors="subagents")
    project = tmp_path / "project"

    code = main([
        "job", "show",
        "--project", str(project),
        "--job", job["id"],
    ])
    assert code == 0
    out = capsys.readouterr().out
    assert "executors: subagents (opencode)" in out


def test_cli_job_show_json(tmp_path, capsys):
    job = _new_job(tmp_path, executors="own")
    project = tmp_path / "project"

    code = main([
        "job", "show",
        "--project", str(project),
        "--job", job["id"],
        "--json",
    ])
    assert code == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["id"] == job["id"]
    assert data["executors"] == "own"
