"""T048: message attachments stored in the coordinator inbox (FR-035, SC-015)."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from crew_chief import central_messages, coordinator_protocol
from crew_chief.central_messages import (
    ATTACHMENT_MAX_FILE_BYTES,
    ATTACHMENT_MAX_TOTAL_BYTES,
    MessageError,
    owner_message,
    pending,
    send,
)


def _cid() -> str:
    return str(uuid.uuid4())


def _root(tmp_path: Path) -> Path:
    return tmp_path / "central"


def _png() -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"\x00\x01\x02" * 100


def _jpg() -> bytes:
    return b"\xff\xd8\xff\xe0" + b"\x00\x01\x02" * 100


def _gif87() -> bytes:
    return b"GIF87a" + b"\x00\x01\x02" * 100


def _gif89() -> bytes:
    return b"GIF89a" + b"\x00\x01\x02" * 100


def _webp() -> bytes:
    return b"RIFF" + b"\x12\x34\x56\x78" + b"WEBP" + b"\x00\x01\x02" * 100


def _pdf() -> bytes:
    return b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"


def _txt(text: str = "hello world\n") -> bytes:
    return text.encode("utf-8")


def _send(root: Path, cid: str, body: str = "hi", attachments=None) -> str:
    return send(root, cid, owner_message(cid, body, attachments=attachments))


def test_each_allowed_type_accepted_by_magic_and_extension(tmp_path):
    root, cid = _root(tmp_path), _cid()
    cases = [
        ("photo.png", _png(), "png"),
        ("photo.jpg", _jpg(), "jpg"),
        ("photo.jpeg", _jpg(), "jpg"),
        ("anim.gif", _gif89(), "gif"),
        ("anim87.gif", _gif87(), "gif"),
        ("shot.webp", _webp(), "webp"),
        ("doc.pdf", _pdf(), "pdf"),
        ("note.txt", _txt(), "text"),
        ("note.md", _txt("# title\n"), "text"),
        ("code.py", _txt("print('hi')\n"), "text"),
        ("data.json", _txt('{"a": 1}\n'), "text"),
        ("style.css", _txt("a { color: red; }\n"), "text"),
    ]
    for name, data, kind in cases:
        mid = _send(root, cid, body=f"msg {name}", attachments=[(name, data)])
        rows = pending(root, cid, "in")
        row = next(r for r in rows if r["id"] == mid)
        assert len(row["attachments"]) == 1
        att = row["attachments"][0]
        assert att["size"] == len(data)
        assert att["type"] == kind
        assert "path" not in att
        assert "data" not in att
        stored = list((root / "coordinators" / cid / "attachments" / mid).iterdir())
        assert len(stored) == 1
        assert stored[0].read_bytes() == data


def test_disguised_unsupported_and_nul_refused_with_nothing_stored(tmp_path):
    root, cid = _root(tmp_path), _cid()
    bad = [
        ("evil.jpg", _png()),  # png bytes, jpg extension
        ("evil.png", _txt("plain text")),  # text bytes, png extension
        ("photo.heic", b"\x00\x00\x00\x18ftypheic" + b"\x00" * 100),
        ("vector.svg", _txt('<svg xmlns="http://www.w3.org/2000/svg"></svg>')),
        ("run.exe", b"MZ" + b"\x00" * 100),
        ("nul.txt", b"hello\x00world"),
    ]
    for name, data in bad:
        with pytest.raises(MessageError) as excinfo:
            _send(root, cid, attachments=[(name, data)])
        assert name in str(excinfo.value)
        assert pending(root, cid, "in") == []
        base = root / "coordinators" / cid / "attachments"
        if base.exists():
            assert list(base.iterdir()) == []


def test_file_too_large_total_too_large_and_too_many_refused(tmp_path):
    root, cid = _root(tmp_path), _cid()
    big = b"a" * (ATTACHMENT_MAX_FILE_BYTES + 1)
    with pytest.raises(MessageError) as excinfo:
        _send(root, cid, attachments=[("big.txt", big)])
    assert "big.txt" in str(excinfo.value)
    assert "file too large" in str(excinfo.value)
    assert pending(root, cid, "in") == []

    ten = b"b" * (10 * 1024 * 1024)
    five_plus = b"c" * (5 * 1024 * 1024 + 1)
    with pytest.raises(MessageError) as excinfo:
        _send(root, cid, attachments=[
            ("a1.txt", ten), ("a2.txt", ten), ("a3.txt", five_plus),
        ])
    assert "a3.txt" in str(excinfo.value)
    assert "message too large" in str(excinfo.value)
    assert pending(root, cid, "in") == []
    base = root / "coordinators" / cid / "attachments"
    if base.exists():
        assert list(base.iterdir()) == []

    many = [(f"f{i}.txt", _txt(f"file {i}\n")) for i in range(21)]
    with pytest.raises(MessageError) as excinfo:
        _send(root, cid, attachments=many)
    assert "too many files" in str(excinfo.value)
    assert pending(root, cid, "in") == []


def test_names_sanitized_and_unique(tmp_path):
    root, cid = _root(tmp_path), _cid()
    mid = _send(root, cid, attachments=[
        ("../evil.png", _png()),
        ("/abs/path.jpg", _jpg()),
        ("a\x01\x02b.txt", _txt("controls\n")),
        ("CON.txt", _txt("reserved\n")),
        (".hidden.txt", _txt("hidden\n")),
        ("photo.png", _png()),
        ("photo.png", _png()),
    ])
    row = next(r for r in pending(root, cid, "in") if r["id"] == mid)
    names = [a["name"] for a in row["attachments"]]
    assert len(set(names)) == len(names)
    for name in names:
        assert "/" not in name and "\\" not in name
        assert ".." not in name
        assert not name.startswith(".")
        assert "\x01" not in name and "\x00" not in name
    assert not any(n.split("-", 1)[1].startswith(".") for n in names)
    assert any("evil.png" in n for n in names)
    assert any("path.jpg" in n for n in names)
    assert any(n.endswith("-_CON.txt") or "_CON" in n for n in names)
    stored = sorted(p.name for p in (root / "coordinators" / cid / "attachments" / mid).iterdir())
    assert stored == sorted(names)


def test_windows_reserved_aux_and_trailing_dot_space(tmp_path):
    root, cid = _root(tmp_path), _cid()
    mid = _send(root, cid, attachments=[
        ("aux.txt", _txt("aux\n")),
        ("note.txt ", _txt("trailing\n")),
    ])
    row = next(r for r in pending(root, cid, "in") if r["id"] == mid)
    names = [a["name"] for a in row["attachments"]]
    assert any("_aux" in n for n in names)
    for name in names:
        base = name.split("-", 1)[1]
        assert not base.endswith(".") and not base.endswith(" ")


def test_atomic_store_or_nothing_on_mixed_batch(tmp_path):
    root, cid = _root(tmp_path), _cid()
    fixed = str(uuid.uuid4())
    env = owner_message(cid, "mixed", attachments=[("good.png", _png()), ("bad.exe", b"MZ")])
    env["id"] = fixed
    with pytest.raises(MessageError):
        send(root, cid, env)
    assert pending(root, cid, "in") == []
    assert not (root / "coordinators" / cid / "attachments" / fixed).exists()


def test_symlinked_attachments_dir_refused(tmp_path):
    root, cid = _root(tmp_path), _cid()
    _send(root, cid, body="first")
    base = root / "coordinators" / cid / "attachments"
    base.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "marker.txt"
    marker.write_bytes(b"untouched")
    # Replace the attachments dir with a symlink.
    for child in list(base.iterdir()):
        if child.is_dir():
            import shutil
            shutil.rmtree(child)
        else:
            child.unlink()
    base.rmdir()
    try:
        base.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("native symlink capability unavailable")
    with pytest.raises(MessageError, match="link|escape"):
        _send(root, cid, attachments=[("photo.png", _png())])
    assert pending(root, cid, "in")[0]["body"] == "first"
    assert marker.read_bytes() == b"untouched"


def test_symlinked_attachments_dir_refused_mock(tmp_path, monkeypatch):
    from pathlib import Path as _Path
    root, cid = _root(tmp_path), _cid()
    _send(root, cid, body="first")
    real_is_symlink = _Path.is_symlink

    def _fake_is_symlink(self):
        try:
            text = str(self).replace("\\", "/")
            if text.endswith(f"{cid}/attachments"):
                return True
        except Exception:
            pass
        return real_is_symlink(self)

    monkeypatch.setattr(_Path, "is_symlink", _fake_is_symlink)
    with pytest.raises(MessageError, match="link|escape"):
        _send(root, cid, attachments=[("photo.png", _png())])


def test_envelopes_without_attachments_unchanged(tmp_path):
    root, cid = _root(tmp_path), _cid()
    mid = _send(root, cid, body="plain")
    row = next(r for r in pending(root, cid, "in") if r["id"] == mid)
    assert "attachments" not in row
    stored = json.loads((root / "coordinators" / cid / "inbox" / f"{mid}.json").read_text(encoding="utf-8"))
    assert "attachments" not in stored
    assert "path" not in json.dumps(stored)


def test_next_round_prompt_lists_attachment_paths(tmp_path):
    root, cid = _root(tmp_path), _cid()
    mid = _send(root, cid, body="see attached", attachments=[("shot.png", _png())])
    rows = pending(root, cid, "in")
    row = next(r for r in rows if r["id"] == mid)
    enriched = central_messages.attachment_paths(root, cid, row)
    expected_path = enriched["attachments"][0]["path"]
    assert "1-shot.png" in expected_path
    assert str(mid) in expected_path
    prompt = coordinator_protocol.render_round(
        {"mode": "direct-spec"}, "request", [enriched], {},
    )
    assert "Messages may list attachments with a local path" in prompt
    context_json = prompt.split("CONTEXT_JSON\n", 1)[1]
    context = json.loads(context_json)
    assert context["inbox"][0]["attachments"][0]["path"] == expected_path
    assert "1-shot.png" in prompt


def test_render_round_rejects_bad_attachment_shapes():
    good = {
        "id": str(uuid.uuid4()), "body": "hi", "reply_to": None,
        "attachments": [{"name": "1-photo.png", "size": 100, "type": "png", "path": "/tmp/x/1-photo.png"}],
    }
    coordinator_protocol.render_round({"mode": "direct-spec"}, "request", [good], {})
    bad_missing_path = {
        "id": str(uuid.uuid4()), "body": "hi", "reply_to": None,
        "attachments": [{"name": "1-photo.png", "size": 100, "type": "png"}],
    }
    with pytest.raises(Exception):
        coordinator_protocol.render_round({"mode": "direct-spec"}, "request", [bad_missing_path], {})
