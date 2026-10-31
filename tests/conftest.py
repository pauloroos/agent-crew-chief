"""Shared pytest fixtures and import path setup for agent-crew-chief tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "skill" / "crew-chief" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


@pytest.fixture(autouse=True)
def ledger_home(tmp_path_factory, monkeypatch) -> Path:
    """Point the ledger at a throwaway folder, never the developer's real scores."""
    home = tmp_path_factory.mktemp("crew-chief-home")
    monkeypatch.setenv("CREW_CHIEF_HOME", str(home))
    return home


@pytest.fixture(autouse=True)
def home_dir(tmp_path_factory, monkeypatch) -> Path:
    """Redirect ``Path.home()`` (and HOME/USERPROFILE) to an empty temp dir."""
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


@pytest.fixture(autouse=True)
def deny_external_connections(monkeypatch):
    """Deny non-loopback resolution in every test (isolated, no-network)."""
    import socket

    original = socket.getaddrinfo

    def resolve(host, *args, **kwargs):
        assert host in ("127.0.0.1", "localhost", "::1"), host
        return original(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)

