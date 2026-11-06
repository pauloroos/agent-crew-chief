"""0.13.0: ACC summary writes one job line before the logs folder goes; the ledger lock."""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from crew_chief import jobs, ledger
from crew_chief.cli import main

TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2"
START = datetime(2026, 11, 3, 22, 57, 44, tzinfo=timezone.utc)


def _logs(tmp_path: Path) -> Path:
    logs = tmp_path / "shop-acc-logs"
    logs.mkdir()
    (logs / "acc-mfa-l1-fix-driver.md").write_text(f"job spec with {TOKEN}\n", encoding="utf-8")
    (logs / "acc-mfa-l1-fix-driver.log").write_text(
        "\x1b[0m> build\n"
        "Error: 429 Too Many Requests\n"
        "  expect(res.status).toBe(429)\n"
        "provider said: insufficient_quota\n"
        "    api.post('/x', { config: { rateLimit: { max: 5 } } })\n"
        f"push with {TOKEN}\n", encoding="utf-8")
    (logs / "acc-mfa-l1-fix-driver.log.err").write_text("request timed out\n", encoding="utf-8")
    (logs / "acc-mfa-l1-fix-scrutineer-1.md").write_text("ok\nVERDICT: fail\nVERDICT: pass\n", encoding="utf-8")
    (logs / "acc-mfa-l1-fix-scrutineer-1-prompt.md").write_text("VERDICT: fail\n", encoding="utf-8")
    (logs / "acc-mfa-l1-fix-scrutineer-2.md").write_text("still writing\n", encoding="utf-8")
    for path in logs.iterdir():
        os.utime(path, (START.timestamp(), START.timestamp()))
    return logs


def _lines(machine: str) -> list[dict]:
    return [json.loads(x) for f in jobs.folder(machine).glob("*.jsonl")
            for x in f.read_text(encoding="utf-8").splitlines()]


def test_summary_counts_errors_reads_verdicts_and_copies_no_log_text(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CREW_CHIEF_MACHINE", "pc")
    monkeypatch.delenv("CREW_CHIEF_JOB_ID", raising=False)
    monkeypatch.delenv("CREW_CHIEF_COORDINATOR", raising=False)
    logs = _logs(tmp_path)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    ledger.append({"at": now, "project": "shop", "kind": "code", "agent": "opencode", "model": "beam",
                   "outcome": "pass", "score": 90, "machine": "pc"})
    ledger.append({"at": now, "project": "shop", "kind": "code", "agent": "x", "model": "other-machine",
                   "outcome": "pass", "score": 90, "machine": "notebook"})
    ledger.append({"at": now, "project": "other", "kind": "code", "agent": "x", "model": "y",
                   "outcome": "pass", "score": 90})
    pr = "https://github.com/me/shop/pull/7"
    args = ["summary", "--job", "mfa", "--pr", pr, "--logs", str(logs), "--project", "shop"]
    assert main(args) == 0
    assert capsys.readouterr().out == "summary 20261103-225744-mfa written to jobs/pc/2026-11.jsonl\n"
    [line] = _lines("pc")
    text = json.dumps(line)
    assert TOKEN not in text and "Too Many" not in text and "spec" not in text
    assert set(line) == {"id", "maquina", "repo", "inicio", "fim", "minutos", "rc", "modelos_usados",
                         "vereditos_fiscal", "erros", "pr_url"}
    assert line["id"] == "20261103-225744-mfa" and line["maquina"] == "pc" and line["repo"] == "shop"
    assert line["inicio"] == "2026-11-03T22:57:44+00:00" and line["rc"] == 0 and line["minutos"] > 0
    assert line["erros"] == {"cota": 1, "429": 1, "timeout": 1, "escrita_fora_da_pasta": 0}
    assert line["vereditos_fiscal"] == {"acc-mfa-l1-fix-scrutineer-1": "pass",
                                        "acc-mfa-l1-fix-scrutineer-2": "sem veredito"}
    assert line["modelos_usados"] == ["opencode:beam"]
    assert line["pr_url"] == pr
    assert all(p.exists() for p in logs.iterdir())  # the crew chief removes the folder, not ACC summary
    # a second run of the same job adds nothing
    assert main(args) == 0
    assert capsys.readouterr().out == "summary 20261103-225744-mfa already in jobs/pc/2026-11.jsonl\n"
    assert len(_lines("pc")) == 1


def test_summary_takes_the_launcher_id_and_coordinator(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CREW_CHIEF_MACHINE", "orca")
    monkeypatch.setenv("CREW_CHIEF_JOB_ID", "20261103-225744-eeaafe")
    monkeypatch.setenv("CREW_CHIEF_COORDINATOR", "opencode:opencode-go/deepseek-v4.1-flash")
    assert main(["summary", "--job", "mfa", "--rc", "1", "--logs", str(tmp_path / "none"),
                 "--project", "shop"]) == 0
    [line] = _lines("orca")
    assert line["id"] == "20261103-225744-eeaafe" and line["inicio"] == "2026-11-03T22:57:44+00:00"
    assert line["rc"] == 1 and line["pr_url"] is None and line["vereditos_fiscal"] == {}
    assert line["modelos_usados"] == ["opencode:opencode-go/deepseek-v4.1-flash"]
    assert line["erros"] == {"cota": 0, "429": 0, "timeout": 0, "escrita_fora_da_pasta": 0}


def test_summary_skips_an_earlier_runs_files_and_matches_owner_repo(tmp_path, monkeypatch):
    """0.13.1: a logs folder left by an earlier run lent its verdicts; owner/repo missed every grade."""
    monkeypatch.setenv("CREW_CHIEF_MACHINE", "orca")
    monkeypatch.setenv("CREW_CHIEF_JOB_ID", "20261104-193148-4e153c")
    monkeypatch.delenv("CREW_CHIEF_COORDINATOR", raising=False)
    logs = _logs(tmp_path)  # every file dated START, the run before
    (logs / "acc-cc-l2-scrutineer-1.md").write_text("VERDICT: pass\n", encoding="utf-8")  # this run's
    ledger.append({"at": "2026-11-04T20:10:00+00:00", "project": "shop", "kind": "code",
                   "agent": "opencode", "model": "beam", "outcome": "pass", "score": 90, "machine": "orca"})
    line = jobs.summarize("me/shop", logs, "cc", 0, None, now=datetime.now(timezone.utc))
    assert line["vereditos_fiscal"] == {"acc-cc-l2-scrutineer-1": "pass"}
    assert line["erros"] == {"cota": 0, "429": 0, "timeout": 0, "escrita_fora_da_pasta": 0}
    assert line["modelos_usados"] == ["opencode:beam"]


def test_a_grade_written_during_a_sync_waits_for_the_lock():
    held = threading.Event()

    def hold():
        with ledger.locked():
            held.set()
            time.sleep(0.4)

    thread = threading.Thread(target=hold)
    thread.start()
    held.wait()
    began = time.monotonic()
    ledger.append({"kind": "code", "agent": "a", "model": "m", "outcome": "pass", "score": 80})
    waited = time.monotonic() - began
    thread.join()
    assert waited >= 0.3
    assert len(ledger.read()) == 1
    assert not (ledger.ledger_path().parent / ".lock").exists()


def test_a_stale_lock_is_broken():
    lock = ledger.ledger_path().parent / ".lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.touch()
    old = time.time() - ledger.LOCK_STALE_S - 5
    os.utime(lock, (old, old))
    ledger.append({"kind": "code", "agent": "a", "model": "m", "outcome": "pass", "score": 80})
    assert len(ledger.read()) == 1 and not lock.exists()


def test_sync_runs_under_the_lock(tmp_path, monkeypatch):
    from crew_chief import sync

    seen = []
    monkeypatch.setattr(sync, "_run_sync",
                        lambda home, url: seen.append((ledger.ledger_path().parent / ".lock").exists()) or 0)
    assert sync.run_sync(tmp_path, "url") == 0 and seen == [True]
