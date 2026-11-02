"""UTF-8 output on Windows: under ``python -I`` the console code page is used.

``python -I`` ignores PYTHONIOENCODING/PYTHONUTF8, so on Windows stdout uses
the console code page and accented text comes out broken. These tests run the
CLI the way it is shipped (``python -I crew-chief.py``) and require real
UTF-8 bytes on stdout.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from crew_chief import ledger

REPO = Path(__file__).resolve().parents[1]
ENTRY = REPO / "skill" / "crew-chief" / "scripts" / "crew-chief.py"
MODEL = "provider/modelo-ção"


def _env(home: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["CREW_CHIEF_HOME"] = str(home)
    env.pop("PYTHONUTF8", None)
    env.pop("PYTHONIOENCODING", None)
    return env


def _run(home: Path, work: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, "-I", str(ENTRY), *args],
        cwd=work, env=_env(home),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        check=False,
    )


def _setup(tmp_path, monkeypatch) -> tuple[Path, Path]:
    home = tmp_path / "cchome"
    home.mkdir()
    (home / "models.toml").write_text(
        f"""\
[[model]]
agent = "opencode"
model = "{MODEL}"
cost = "free"
note = "padrão"
""",
        encoding="utf-8",
    )
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setenv("CREW_CHIEF_HOME", str(home))
    return home, work


def test_models_prints_accents_as_utf8(tmp_path, monkeypatch):
    home, work = _setup(tmp_path, monkeypatch)
    proc = _run(home, work, "models")
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    out = proc.stdout.decode("utf-8")  # fails on Windows before the fix
    assert "padrão" in out
    assert MODEL in out


def test_score_and_pick_print_accents_as_utf8(tmp_path, monkeypatch):
    home, work = _setup(tmp_path, monkeypatch)
    proc = _run(home, work, "record", "--kind", "code", "--agent", "opencode",
                "--model", MODEL, "--outcome", "pass", "--score", "80",
                "--note", "revisão", "--job", "ç", "--project", "proj-ção")
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    for command in (["score"], ["pick", "--kind", "code"]):
        proc = _run(home, work, *command)
        assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
        out = proc.stdout.decode("utf-8")  # fails on Windows before the fix
        assert MODEL in out


def test_ledger_round_trip_keeps_accents(tmp_path, monkeypatch):
    home, work = _setup(tmp_path, monkeypatch)
    proc = _run(home, work, "record", "--kind", "code", "--agent", "opencode",
                "--model", MODEL, "--outcome", "pass", "--score", "80",
                "--note", "revisão", "--job", "ç", "--project", "proj-ção")
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    (entry,) = ledger.read()
    assert (entry["note"], entry["job"], entry["project"]) == ("revisão", "ç", "proj-ção")
    proc = _run(home, work, "score", "--json")
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    rows = json.loads(proc.stdout.decode("utf-8"))
    assert any(r["model"] == MODEL for r in rows)
