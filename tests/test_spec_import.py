"""0.15.0: ACC spec-import copies an approved spec byte for byte; ACC summary records its hash."""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from crew_chief import jobs
from crew_chief.cli import main

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]


def _repo(tmp_path: Path, monkeypatch) -> Path:
    repo = tmp_path / "shop"
    repo.mkdir()
    subprocess.run([*GIT, "init", "-q"], cwd=repo, check=True)
    subprocess.run([*GIT, "commit", "-q", "--allow-empty", "-m", "start"], cwd=repo, check=True)
    monkeypatch.chdir(repo)
    return repo


def _commit(repo: Path, message: str) -> None:
    subprocess.run([*GIT, "add", "specs"], cwd=repo, check=True)
    subprocess.run([*GIT, "commit", "-q", "-m", message], cwd=repo, check=True)


def _approved(tmp_path: Path) -> Path:
    source = tmp_path / "bia" / "spec-aprovada.md"
    source.parent.mkdir()
    # CRLF, accents and no final newline must arrive as they are
    source.write_bytes("# Spec\r\nAcentuação e premissas\r\nsem fim de linha".encode("utf-8"))
    return source


def _lines(machine: str) -> list[dict]:
    return [json.loads(x) for f in jobs.folder(machine).glob("*.jsonl")
            for x in f.read_text(encoding="utf-8").splitlines()]


def test_spec_import_copia_exata_e_hash(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path, monkeypatch)
    (repo / "specs" / "001-old").mkdir(parents=True)
    source = _approved(tmp_path)
    attachment = tmp_path / "bia" / "tela.png"
    attachment.write_bytes(bytes(range(256)))
    args = ["spec-import", str(source), "--anexo", str(attachment), "--slug", "login"]
    assert main(args) == 0
    folder = repo / "specs" / "002-login"
    assert capsys.readouterr().out.strip() == str(folder)
    assert (folder / "spec.md").read_bytes() == source.read_bytes()
    assert (folder / "anexos" / "tela.png").read_bytes() == attachment.read_bytes()
    approved = json.loads((folder / "approved.json").read_text(encoding="utf-8"))
    assert approved["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert approved["fonte"] == str(source)
    assert datetime.fromisoformat(approved["importado_em"]).tzinfo is not None
    # the same approved spec again: same folder, nothing new
    assert main(args) == 0
    assert capsys.readouterr().out.strip() == str(folder)
    assert sorted(p.name for p in (repo / "specs").iterdir()) == ["001-old", "002-login"]
    assert json.loads((folder / "approved.json").read_text(encoding="utf-8")) == approved


def test_spec_import_nao_sobrescreve_diferente(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path, monkeypatch)
    source = _approved(tmp_path)
    original = source.read_bytes()
    assert main(["spec-import", str(source), "--slug", "login"]) == 0
    folder = repo / "specs" / "001-login"
    capsys.readouterr()
    source.write_text("outra spec", encoding="utf-8")
    assert main(["spec-import", str(source), "--slug", "login"]) == 2
    assert "spec.md" in capsys.readouterr().err
    assert (folder / "spec.md").read_bytes() == original
    # a different attachment under the same name is refused too, before anything is written
    source.write_bytes(original)
    attachment = tmp_path / "bia" / "tela.png"
    attachment.write_bytes(b"one")
    assert main(["spec-import", str(source), "--anexo", str(attachment), "--slug", "login"]) == 0
    attachment.write_bytes(b"two")
    assert main(["spec-import", str(source), "--anexo", str(attachment), "--slug", "login"]) == 2
    assert (folder / "anexos" / "tela.png").read_bytes() == b"one"
    # a missing file or a bad slug writes nothing
    assert main(["spec-import", str(tmp_path / "none.md"), "--slug", "x"]) == 2
    assert main(["spec-import", str(source), "--slug", "../x"]) == 2
    assert sorted(p.name for p in (repo / "specs").iterdir()) == ["001-login"]


def test_summary_card_e_spec_sha(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path, monkeypatch)
    monkeypatch.setenv("CREW_CHIEF_MACHINE", "orca")
    monkeypatch.delenv("CREW_CHIEF_JOB_ID", raising=False)
    monkeypatch.delenv("CREW_CHIEF_COORDINATOR", raising=False)
    source = _approved(tmp_path)
    assert main(["spec-import", str(source), "--slug", "login"]) == 0
    _commit(repo, "spec")
    monkeypatch.setenv("CREW_CHIEF_CARD", "CMP-12")
    # no --spec: the import of this run (in the default logs folder) names the spec
    assert main(["summary", "--job", "login", "--project", "shop"]) == 0
    [line] = _lines("orca")
    assert line["card"] == "CMP-12"
    assert line["spec_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert line["spec_divergente"] is False
    # a run with no card and no spec
    monkeypatch.setenv("CREW_CHIEF_CARD", "")
    assert main(["summary", "--job", "other", "--project", "shop", "--logs", str(tmp_path / "none")]) == 0
    other = next(x for x in _lines("orca") if x["id"].endswith("-other"))
    assert other["card"] is None and other["spec_sha256"] is None and other["spec_divergente"] is False


def test_summary_spec_divergente(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path, monkeypatch)
    monkeypatch.setenv("CREW_CHIEF_MACHINE", "orca")
    monkeypatch.delenv("CREW_CHIEF_JOB_ID", raising=False)
    monkeypatch.delenv("CREW_CHIEF_CARD", raising=False)
    source = _approved(tmp_path)
    assert main(["spec-import", str(source), "--slug", "login"]) == 0
    folder = repo / "specs" / "001-login"
    _commit(repo, "spec")
    (folder / "spec.md").write_text("# Spec reescrita pelo coordenador\n", encoding="utf-8")
    _commit(repo, "rewrite")
    capsys.readouterr()
    args = ["summary", "--job", "login", "--project", "shop", "--spec", str(folder),
            "--logs", str(tmp_path / "none")]
    assert main(args) == 1
    assert "spec_divergente" in capsys.readouterr().err
    [line] = _lines("orca")
    assert line["spec_divergente"] is True
    assert line["spec_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()


def test_summary_ancora_fora_da_arvore(tmp_path, monkeypatch, capsys):
    """0.15.1: spec.md and approved.json edited together still diverge from the import note in the logs."""
    repo = _repo(tmp_path, monkeypatch)
    monkeypatch.setenv("CREW_CHIEF_MACHINE", "orca")
    monkeypatch.delenv("CREW_CHIEF_JOB_ID", raising=False)
    monkeypatch.delenv("CREW_CHIEF_SPEC_SHA256", raising=False)
    source = _approved(tmp_path)
    assert main(["spec-import", str(source), "--slug", "login"]) == 0
    folder = repo / "specs" / "001-login"
    note = json.loads((tmp_path / "shop-acc-logs" / "spec-import.json").read_text(encoding="utf-8"))
    assert note["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest() and note["fonte"] == str(source)
    rewritten = b"# outra spec\n"
    (folder / "spec.md").write_bytes(rewritten)
    approved = json.loads((folder / "approved.json").read_text(encoding="utf-8"))
    (folder / "approved.json").write_text(json.dumps({**approved, "sha256": hashlib.sha256(rewritten).hexdigest()}),
                                          encoding="utf-8")
    _commit(repo, "rewrite both")
    assert main(["summary", "--job", "login", "--project", "shop"]) == 1
    assert "spec_divergente" in capsys.readouterr().err
    [line] = _lines("orca")
    assert line["spec_divergente"] is True


def test_summary_hash_esperado_da_ponte(tmp_path, monkeypatch, capsys):
    """0.15.1: CREW_CHIEF_SPEC_SHA256 from the launcher must match; set with no spec imported = spec lost."""
    repo = _repo(tmp_path, monkeypatch)
    monkeypatch.setenv("CREW_CHIEF_MACHINE", "orca")
    monkeypatch.delenv("CREW_CHIEF_JOB_ID", raising=False)
    source = _approved(tmp_path)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    assert main(["spec-import", str(source), "--slug", "login"]) == 0
    _commit(repo, "spec")
    monkeypatch.setenv("CREW_CHIEF_SPEC_SHA256", digest.upper())
    assert main(["summary", "--job", "a", "--project", "shop"]) == 0
    monkeypatch.setenv("CREW_CHIEF_SPEC_SHA256", hashlib.sha256(b"other").hexdigest())
    assert main(["summary", "--job", "b", "--project", "shop"]) == 1
    monkeypatch.setenv("CREW_CHIEF_SPEC_SHA256", digest)
    assert main(["summary", "--job", "c", "--project", "shop", "--logs", str(tmp_path / "none")]) == 1
    lines = {x["id"].rsplit("-", 1)[-1]: x for x in _lines("orca")}
    assert lines["a"]["spec_divergente"] is False and lines["b"]["spec_divergente"] is True
    assert lines["c"]["spec_divergente"] is True and lines["c"]["spec_sha256"] is None


def test_spec_import_recusa_link_simbolico(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path, monkeypatch)
    source = _approved(tmp_path)
    link = tmp_path / "bia" / "link.md"
    try:
        link.symlink_to(source)
        (repo / "specs").mkdir()
        (repo / "specs" / "001-login").symlink_to(tmp_path / "bia", target_is_directory=True)
    except OSError:
        pytest.skip("no symbolic links here (Windows without the privilege)")
    assert main(["spec-import", str(link), "--slug", "other"]) == 2
    assert "symbolic link" in capsys.readouterr().err
    assert main(["spec-import", str(source), "--slug", "login"]) == 2  # the folder is a link
    assert "symbolic link" in capsys.readouterr().err
    assert sorted(p.name for p in (repo / "specs").iterdir()) == ["001-login"]
    assert sorted(p.name for p in (tmp_path / "bia").iterdir()) == ["link.md", "spec-aprovada.md"]
