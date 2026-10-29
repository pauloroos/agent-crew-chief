"""Isolated core verification across Windows/Linux/macOS (T028).

Covers FR-019/020/021 and SC-007/008 at the core gate:

* the CI matrix runs the suite on ubuntu/windows/macos with Python 3.11/3.13;
* subprocesses inherit only temporary HOME/USERPROFILE, global-prefs and
  central roots and find no live ``claude``/``codex`` on PATH;
* a config sentinel is unchanged and the native ``acc`` wrapper executes;
* wrapper contents cover all three OS families with spaces/Unicode paths.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from crew_chief import central_config, command_install

REPO_ROOT = Path(__file__).resolve().parents[1]
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"

FAKE_ENTRY = """import os
import sys
print("fake-entry:" + "|".join(sys.argv[1:]))
sys.exit(int(os.environ.get("FAKE_ENTRY_EXIT", "0")))
"""


def _release(tmp_path: Path, name: str = "release") -> Path:
    root = tmp_path / name
    script = root / command_install.BUNDLE_ENTRY
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(FAKE_ENTRY, encoding="utf-8")
    return root


def _isolated_env(tmp_path: Path) -> tuple[dict, Path, Path, Path, Path]:
    """Temporary home/global-prefs/central roots plus a PATH with no live CLIs."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    prefs = tmp_path / "global-prefs.toml"
    prefs.write_text('review_default = "x:y"\n', encoding="utf-8")
    central = tmp_path / "central"
    central.mkdir(exist_ok=True)
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir(exist_ok=True)
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env["AGENT_CREW_CHIEF_GLOBAL_PREFS"] = str(prefs)
    env["AGENT_CREW_CHIEF_CENTRAL_DIR"] = str(central)
    env["PATH"] = str(empty_bin)
    return env, home, prefs, central, empty_bin


def _run_child(env: dict, code: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-I", "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )


def test_ci_matrix_covers_three_os_and_two_pythons():
    text = CI_YML.read_text(encoding="utf-8-sig")
    for token in ("ubuntu-latest", "windows-latest", "macos-latest"):
        assert token in text, f"CI matrix is missing {token}"
    for token in ('"3.11"', '"3.13"', "3.11", "3.13"):
        assert token in text
    assert "python -m pytest" in text or "python -m pytest -q" in text


def test_subprocess_inherits_only_temporary_roots_and_no_live_cli(tmp_path):
    env, home, prefs, central, _ = _isolated_env(tmp_path)
    code = (
        "import json, os, shutil;"
        "print(json.dumps({"
        "'HOME': os.environ.get('HOME'),"
        "'USERPROFILE': os.environ.get('USERPROFILE'),"
        "'GLOBAL': os.environ.get('AGENT_CREW_CHIEF_GLOBAL_PREFS'),"
        "'CENTRAL': os.environ.get('AGENT_CREW_CHIEF_CENTRAL_DIR'),"
        "'claude': shutil.which('claude'),"
        "'codex': shutil.which('codex')}))"
    )
    result = _run_child(env, code)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["HOME"] == str(home)
    assert payload["USERPROFILE"] == str(home)
    assert payload["GLOBAL"] == str(prefs)
    assert payload["CENTRAL"] == str(central)
    assert payload["claude"] is None
    assert payload["codex"] is None
    # The real developer home is nowhere in the child view.
    assert str(Path.home()) not in json.dumps(payload)


def test_central_root_resolves_to_temporary_dir_in_subprocess(tmp_path):
    env, _, _, central, _ = _isolated_env(tmp_path)
    code = (
        "import json, os, sys;"
        f"sys.path.insert(0, {str(REPO_ROOT / 'skill' / 'crew-chief' / 'scripts')!r});"
        "from crew_chief import central_config;"
        "from pathlib import Path;"
        "root = central_config.resolve_root(None, dict(os.environ), Path(os.environ['HOME']));"
        "print(json.dumps(str(root)))"
    )
    result = _run_child(env, code)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == str(central)


def test_wrapper_contents_cover_three_families_and_leave_prefs_alone(tmp_path):
    prefs = tmp_path / "preferences.toml"
    prefs.write_text('review_default = "x:y"\n', encoding="utf-8")
    before = prefs.read_bytes()
    home = tmp_path / "home with spaces ação"
    home.mkdir()

    release = _release(tmp_path, "pacote ação")
    for platform, name in (("windows", "acc.cmd"), ("linux", "acc"), ("macos", "acc")):
        target = command_install.install_command(
            release, tmp_path / "bin" / platform, Path(sys.executable), platform
        )
        assert target.name == name
        text = target.read_text(encoding="utf-8")
        assert " -I " in text
        assert "ação" in text
        assert command_install.OWNER_MARKER in text

    assert prefs.read_bytes() == before
    assert list(home.iterdir()) == []


def test_native_wrapper_executes_fake_entry_with_isolated_env(tmp_path, monkeypatch):
    env, home, prefs, _, _ = _isolated_env(tmp_path)
    before_prefs = prefs.read_bytes()
    # ASCII release path: the Windows batch wrapper needs no `chcp` line,
    # so it runs with a PATH that contains no live model CLI.
    release = _release(tmp_path, "fake release")
    target = command_install.install_command(
        release, tmp_path / "bin", Path(sys.executable),
        command_install.current_platform(),
    )
    monkeypatch.setenv("FAKE_ENTRY_EXIT", "0")
    env["FAKE_ENTRY_EXIT"] = "0"
    result = subprocess.run(
        [str(target), "one", "two words"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert "fake-entry:one|two words" in result.stdout
    assert prefs.read_bytes() == before_prefs
    assert list(home.iterdir()) == []


def test_runtime_needs_no_pty_curses_tmux_or_conpty(tmp_path):
    import ast

    source = (REPO_ROOT / "skill" / "crew-chief" / "scripts" / "crew_chief" / "command_install.py")
    tree = ast.parse(source.read_text(encoding="utf-8-sig"))
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".")[0])
    assert not ({"pty", "curses", "_curses", "tmux"} & imports)
    # The rendered wrappers start the interpreter on the current terminal.
    import re

    release = _release(tmp_path, "release")
    entry = release / command_install.BUNDLE_ENTRY
    for platform in ("windows", "linux", "macos"):
        text = command_install.render_wrapper(entry, Path(sys.executable), platform).lower()
        for token in ("pty", "curses", "tmux", "conpty"):
            assert re.search(rf"(?<![a-z_]){token}(?![a-z_])", text) is None, token
