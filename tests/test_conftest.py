"""Tests for the shared fixtures defined in ``conftest.py``."""

from __future__ import annotations

import sys
from pathlib import Path


def test_home_dir_points_path_home_at_the_throwaway_home(home_dir):
    """The autouse fixture redirects ``Path.home()`` for every test.

    A background program writing ``~/.grokbot`` during the suite must land in
    the throwaway home, not the developer's, so containment never sees it.
    """
    assert Path.home() == home_dir
    (home_dir / ".grokbot").mkdir()
    assert (Path.home() / ".grokbot").is_dir()


def test_fake_bin_prefixes_an_absolute_interpreter_shebang(fake_bin):
    """A stub without a shebang must start with the absolute interpreter path.

    A relative shebang such as ``#!python3`` is not valid on Linux, so every
    fake CLI would fail to start there.
    """
    bin_dir = fake_bin("stub", "print('x')\n")

    first_line = (bin_dir / "stub").read_text(encoding="utf-8").splitlines()[0]

    assert first_line == f"#!{sys.executable}"


def test_fake_bin_keeps_an_explicit_shebang(fake_bin):
    """A stub that brings its own shebang is written unchanged."""
    bin_dir = fake_bin("stub", "#!/bin/sh\necho hi\n")

    first_line = (bin_dir / "stub").read_text(encoding="utf-8").splitlines()[0]

    assert first_line == "#!/bin/sh"
