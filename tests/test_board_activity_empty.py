"""Regression test for board Activity card empty-state layout (defect B).

Verifies that `.feed li.empty` renders as a single-line centered message
instead of one-word-per-line caused by inheriting the feed grid columns.
"""
from contextlib import contextmanager
import json
from pathlib import Path
import threading

import pytest

# Playwright is an optional test dependency; skip gracefully if absent.
try:
    from playwright.sync_api import sync_playwright
    _HAS_PLAYWRIGHT = True
except ImportError:
    sync_playwright = None  # type: ignore[assignment]
    _HAS_PLAYWRIGHT = False


def _playwright_browser_available() -> bool:
    """Attempt to actually launch a browser; cache the result."""
    if not _HAS_PLAYWRIGHT:
        return False
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, timeout=5000)
            browser.close()
        return True
    except Exception:
        return False


_PLAYWRIGHT_BROWSER_AVAILABLE = _playwright_browser_available()


EMPTY_SNAPSHOT = {
    "schema_version": 1,
    "generated_at": "2026-01-01T00:00:00Z",
    "coordinators": [],
    "questions": [],
    "totals": {"tokens_total": 0},
    "diagnostics": [],
}


@contextmanager
def serve_board(tmp_path):
    """Start the central web server with real assets and an empty snapshot."""
    from crew_chief import central_snapshot
    from crew_chief.central_web import create_server

    assets = Path(__file__).resolve().parents[1] / "skill" / "crew-chief" / "web"
    if not (assets / "central.js").exists():
        pytest.skip("web assets not found")

    original = central_snapshot.snapshot
    central_snapshot.snapshot = lambda *a, **kw: EMPTY_SNAPSHOT
    server = create_server(tmp_path / "central", assets)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        yield server
    finally:
        central_snapshot.snapshot = original
        server.shutdown()
        server.server_close()
        thread.join(3)


@pytest.fixture(scope="module")
def browser():
    if not _PLAYWRIGHT_BROWSER_AVAILABLE:
        pytest.skip("Playwright browser not available")
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        yield b
        b.close()


def _assert_single_line_height(page, selector, max_lines):
    """Assert element height is consistent with at most `max_lines` lines of text."""
    box = page.evaluate(
        """(sel) => {
            const el = document.querySelector(sel);
            if (!el) return null;
            const style = getComputedStyle(el);
            const lh = parseFloat(style.lineHeight) || parseFloat(style.fontSize) * 1.4;
            return { height: el.getBoundingClientRect().height, lineHeight: lh };
        }""",
        selector,
    )
    assert box is not None, f"{selector} not found in DOM"
    # Allow generous padding but never 3x line-height (the bug symptom).
    max_allowed = box["lineHeight"] * max_lines + 40  # padding tolerance
    assert box["height"] <= max_allowed, (
        f"{selector} height {box['height']:.1f}px exceeds {max_lines}-line budget "
        f"({max_allowed:.1f}px); likely word-per-line wrapping"
    )


def test_activity_empty_state_single_line_1366(tmp_path, browser):
    """At 1366px the empty state must fit on one line."""
    with serve_board(tmp_path) as server:
        page = browser.new_page(viewport={"width": 1366, "height": 900})
        host, port = server.server_address
        page.goto(f"http://{host}:{port}/")
        page.wait_for_selector(".feed li.empty", timeout=5000)
        _assert_single_line_height(page, ".feed li.empty", max_lines=1)
        text = page.text_content(".feed li.empty")
        assert "No activity yet." in text
        page.close()


def test_activity_empty_state_no_word_wrap_390(tmp_path, browser):
    """At 390px the empty state must not break into more than 2 lines."""
    with serve_board(tmp_path) as server:
        page = browser.new_page(viewport={"width": 390, "height": 800})
        host, port = server.server_address
        page.goto(f"http://{host}:{port}/")
        page.wait_for_selector(".feed li.empty", timeout=5000)
        _assert_single_line_height(page, ".feed li.empty", max_lines=2)
        page.close()