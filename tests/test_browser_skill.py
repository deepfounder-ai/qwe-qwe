"""Browser skill (skills/browser.py) — tool dispatch tests.

`skills/browser.py` wraps Playwright and exposes 23 tools. Before this
file, coverage was 0% because every test path would either try to
``import playwright`` or launch Chromium. We fix that by monkeypatching
``_ensure_browser`` to a no-op and injecting a fake ``_page`` /
``_pages`` into the module — each test then asserts the right Playwright
method is called with the right args for a given tool name.

No real browser launches. No Playwright install required (even if the
package is present, ``_ensure_browser`` is stubbed out before it'd be
called).

Issue: https://github.com/deepfounder-ai/castor/issues/5
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


@pytest.fixture
def browser(monkeypatch):
    """Fresh browser module with a fake page injected into the active session.

    Post-Phase-3 the browser module no longer has module-level _page /
    _pages — state lives on a per-goal BrowserSession. The fixture
    therefore mocks the DEFAULT session's page directly. ensure_running
    is stubbed so we never launch real Chromium.

    Yields ``(module, page_mock, pages_list)`` with the same shape as
    before so existing assertions still work.
    """
    import importlib
    import sys
    if "skills.browser" in sys.modules:
        mod = importlib.reload(sys.modules["skills.browser"])
    else:
        mod = importlib.import_module("skills.browser")

    page = MagicMock(name="page")
    page.title.return_value = "Example Domain"
    page.url = "https://example.com/"
    page.inner_text.return_value = "Example Domain\nThis domain is for use in examples."
    page.content.return_value = "<html><body>Example Domain</body></html>"
    page.evaluate.return_value = []
    page.accessibility.snapshot.return_value = {"role": "WebArea", "name": "Example"}
    page.is_closed.return_value = False

    # Reset the session registry and inject the fake into the default session.
    # _get_active_session falls back to "__default__" when no ctx.goal_id.
    mod._sessions.clear()
    sess = mod._get_session(mod._DEFAULT_SESSION_ID)
    sess.page = page
    sess.pages = [page]
    sess.network_log = []
    sess.console_log = []

    # Stub session lifecycle so no Chromium launches
    monkeypatch.setattr(sess, "ensure_running", lambda: None)
    monkeypatch.setattr(sess, "close", lambda: None)
    monkeypatch.setattr(sess, "is_alive", lambda: True)

    return mod, page, sess.pages


# ── Navigation ────────────────────────────────────────────────────────


def test_browser_open_calls_goto_and_returns_title_url_text(browser):
    mod, page, _ = browser
    page.title.return_value = "Example Domain"
    page.url = "https://example.com/"
    page.inner_text.return_value = "Example Domain"

    out = mod.execute("browser_open", {"url": "example.com"})

    # URL auto-prefixed with https://
    page.goto.assert_called_once()
    called_url = page.goto.call_args[0][0]
    assert called_url == "https://example.com"
    # Response shape the agent relies on
    assert "Title: Example Domain" in out
    assert "URL: https://example.com/" in out
    assert "Example Domain" in out


def test_browser_open_accepts_absolute_url(browser):
    mod, page, _ = browser
    mod.execute("browser_open", {"url": "https://foo.test/x"})
    assert page.goto.call_args[0][0] == "https://foo.test/x"


def test_browser_back_forward_reload(browser):
    mod, page, _ = browser
    assert "back" in mod.execute("browser_back", {}).lower() or \
           page.go_back.called
    assert "forward" in mod.execute("browser_forward", {}).lower() or \
           page.go_forward.called
    assert "reload" in mod.execute("browser_reload", {}).lower() or \
           page.reload.called


# ── Reading ───────────────────────────────────────────────────────────


def test_browser_snapshot_returns_title_url_text(browser):
    mod, page, _ = browser
    page.title.return_value = "Hi"
    page.url = "https://x.test/"
    page.inner_text.return_value = "Body text here"

    out = mod.execute("browser_snapshot", {})
    assert "Title: Hi" in out
    assert "URL: https://x.test/" in out
    assert "Body text here" in out


def test_browser_snapshot_respects_selector(browser):
    mod, page, _ = browser
    mod.execute("browser_snapshot", {"selector": "main"})
    page.inner_text.assert_called_with("main")


def test_browser_snapshot_truncates_long_bodies(browser):
    mod, page, _ = browser
    page.inner_text.return_value = "x" * 10_000
    out = mod.execute("browser_snapshot", {})
    assert "truncated" in out.lower()
    assert len(out) < 6_000  # header + 4000 chars of body + marker


# ── Interaction ──────────────────────────────────────────────────────


def test_browser_click_calls_playwright_click(browser):
    mod, page, _ = browser
    mod.execute("browser_click", {"selector": "button.submit"})
    page.click.assert_called()
    assert page.click.call_args[0][0] == "button.submit"


def test_browser_fill_calls_playwright_fill(browser):
    mod, page, _ = browser
    mod.execute("browser_fill", {"selector": "#q", "value": "hello"})
    # fill() is called with a timeout kwarg — assert the positional
    # args match without being strict about the timeout value.
    page.fill.assert_called_once()
    assert page.fill.call_args.args == ("#q", "hello")


def test_browser_eval_returns_playwright_evaluate_result(browser):
    mod, page, _ = browser
    page.evaluate.return_value = {"answer": 42}
    out = mod.execute("browser_eval", {"expression": "({answer: 42})"})
    page.evaluate.assert_called_with("({answer: 42})")
    # Serialised in the response text somehow
    assert "42" in out


# ── Browser lifecycle ────────────────────────────────────────────────


def test_browser_set_visible_restarts_browser_on_mode_change(browser, monkeypatch):
    """Flipping headless/visible should close the active session so the
    next browser_open launches with the new mode.

    Post-Phase-3 the close happens per-session (`session.close()`), not via
    a module-level `_close_browser` shim. We patch the active session's
    close directly.
    """
    mod, _page, _ = browser
    sess = mod._get_active_session()
    close_called = {"n": 0}
    monkeypatch.setattr(sess, "close", lambda: close_called.__setitem__("n", close_called["n"] + 1))
    # is_alive must return True so the "already in mode + alive" short-circuit
    # is not taken on the no-op repeat call below.
    monkeypatch.setattr(sess, "is_alive", lambda: True)

    # Default is headless=True; set_visible(True) means not-headless → mode flip
    out = mod.execute("browser_set_visible", {"visible": True})
    assert "visible" in out.lower()
    assert close_called["n"] == 1, "mode change must restart browser"

    # Calling again with same value — no restart
    out = mod.execute("browser_set_visible", {"visible": True})
    assert close_called["n"] == 1


def test_browser_close_invokes_close(browser, monkeypatch):
    """browser_close should call the active session's close()."""
    mod, _page, _ = browser
    sess = mod._get_active_session()
    close_called = {"n": 0}
    monkeypatch.setattr(sess, "close", lambda: close_called.__setitem__("n", close_called["n"] + 1))
    out = mod.execute("browser_close", {})
    assert "closed" in out.lower()
    assert close_called["n"] == 1


# ── Hallucinated tool names redirect ─────────────────────────────────


@pytest.mark.parametrize("alias,canonical_url_substring", [
    ("open_url", "https://example.com"),
    ("navigate", "https://example.com"),
    ("browse", "https://example.com"),
])
def test_open_url_aliases_redirect_to_browser_open(browser, alias, canonical_url_substring):
    mod, page, _ = browser
    mod.execute(alias, {"url": "example.com"})
    page.goto.assert_called()
    assert canonical_url_substring in page.goto.call_args[0][0]


def test_google_search_alias_redirects_to_brave(browser):
    mod, page, _ = browser
    mod.execute("google_search", {"query": "python async"})
    page.goto.assert_called()
    url = page.goto.call_args[0][0]
    # The skill rewrites google_search → brave search URL so no real
    # Google hits (Google blocks headless). Spec says rewrites to brave.
    from urllib.parse import urlparse as _urlparse
    assert _urlparse(url).hostname == "search.brave.com"  # exact hostname, not substring
    assert "python" in url


# ── Unknown tool returns clean error ─────────────────────────────────


def test_unknown_tool_returns_clear_error(browser):
    mod, _page, _ = browser
    out = mod.execute("browser_nonsense_xyz", {})
    # Any recognisable error string — "unknown", "not found", "error:", etc.
    low = out.lower()
    assert ("unknown" in low) or ("not" in low) or ("error" in low)
