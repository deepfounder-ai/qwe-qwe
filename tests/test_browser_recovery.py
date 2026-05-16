"""Browser auto-recovery from dead-session errors.

Live test exposed a real autonomy bug: the agent gave up on the WHOLE goal
after seeing `TargetClosedError` on a subagent's browser_open. That's
infrastructure noise — Chrome process died externally or session went
stale — not a reason to abandon the user's task.

The fix in skills/browser.py wraps every tool call: if the result string
contains a known dead-session marker, we auto-close the broken browser,
relaunch a fresh one, and retry the operation ONCE. Only after a second
dead-session result do we surface an error — and the surfaced text tells
the LLM to fall back to non-browser tools (http_request) or alternative
data sources.

These tests don't launch real Chromium — they monkey-patch _execute_impl
to control what the first/second attempts return.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_browser():
    spec = importlib.util.spec_from_file_location(
        "browser_under_test",
        str(Path(__file__).resolve().parent.parent / "skills" / "browser.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_dead_session_markers_recognized():
    """The error-string heuristic must match the strings Playwright actually emits."""
    browser = _load_browser()
    assert browser._looks_like_dead_session("Target page, context or browser has been closed")
    assert browser._looks_like_dead_session("TargetClosedError: navigation failed")
    assert browser._looks_like_dead_session("Connection closed by peer")
    assert browser._looks_like_dead_session("BrowserClosedError")
    # NOT recoverable — these are real errors the agent must handle itself
    assert not browser._looks_like_dead_session("Timeout 30000ms exceeded")
    assert not browser._looks_like_dead_session("net::ERR_NAME_NOT_RESOLVED")
    assert not browser._looks_like_dead_session("Element not found: #foo")
    assert not browser._looks_like_dead_session("")


def _mock_session_methods(browser_mod, monkeypatch, close_calls, ensure_calls,
                         ensure_raises=None):
    """Patch the active session's close + ensure_running methods.

    Recovery now operates per-session (Phase 3) — we capture the active
    session in the same call and patch its methods, so the test sees the
    SAME session the recovery code targets.
    """
    sess = browser_mod._get_active_session()
    monkeypatch.setattr(sess, "close", lambda: close_calls.append(True))
    if ensure_raises:
        def _broken():
            raise ensure_raises
        monkeypatch.setattr(sess, "ensure_running", _broken)
    else:
        monkeypatch.setattr(sess, "ensure_running", lambda: ensure_calls.append(True))
    return sess


def test_recovery_retries_on_dead_session(monkeypatch):
    """First call returns TargetClosedError → close+relaunch → second call succeeds.

    The LLM sees the [recovered ...] prefix and the successful result, NOT the
    raw error. Infrastructure heals itself, agent keeps going.
    """
    browser = _load_browser()

    calls = []
    def _fake_impl(name, args):
        calls.append((name, args))
        if len(calls) == 1:
            return "Browser error (TargetClosedError): page has been closed"
        return "Title: Example\nURL: https://example.com\n\nbody text"

    closes = []
    ensures = []
    monkeypatch.setattr(browser, "_execute_impl", _fake_impl)
    _mock_session_methods(browser, monkeypatch, closes, ensures)

    result = browser._execute_with_recovery("browser_open", {"url": "https://example.com"})

    assert len(calls) == 2
    assert closes == [True]
    assert ensures == [True]
    assert "[recovered from dead session" in result
    assert "Example" in result


def test_recovery_skips_for_browser_close(monkeypatch):
    """browser_close on a dead session is a no-op — no point reopening just to close."""
    browser = _load_browser()
    monkeypatch.setattr(
        browser, "_execute_impl",
        lambda n, a: "Browser error (TargetClosedError): closed",
    )
    closes = []
    ensures = []
    _mock_session_methods(browser, monkeypatch, closes, ensures)

    result = browser._execute_with_recovery("browser_close", {})
    assert closes == []
    assert result.startswith("Browser error")


def test_recovery_surfaces_error_after_second_failure(monkeypatch):
    """If the SECOND attempt also dies, escalate with a clear fall-back hint."""
    browser = _load_browser()

    monkeypatch.setattr(
        browser, "_execute_impl",
        lambda n, a: "Browser error (TargetClosedError): again",
    )
    closes = []
    ensures = []
    _mock_session_methods(browser, monkeypatch, closes, ensures)

    result = browser._execute_with_recovery("browser_open", {"url": "x"})
    assert "twice in a row" in result
    assert "http_request" in result or "alternative" in result.lower()


def test_recovery_passes_through_non_session_errors(monkeypatch):
    """A real timeout or 404 should reach the LLM as-is — these are signals the
    agent SHOULD reason about (different URL, wait + retry, etc.), not infra noise."""
    browser = _load_browser()
    monkeypatch.setattr(
        browser, "_execute_impl",
        lambda n, a: "Browser error (TimeoutError): 30000ms exceeded",
    )
    closes = []
    ensures = []
    _mock_session_methods(browser, monkeypatch, closes, ensures)

    result = browser._execute_with_recovery("browser_open", {"url": "x"})
    assert "30000ms exceeded" in result
    assert closes == []


def test_recovery_handles_ensure_browser_failure(monkeypatch):
    """If recovery itself fails (Playwright won't even relaunch), return a clear
    escalation message instead of crashing."""
    browser = _load_browser()
    monkeypatch.setattr(
        browser, "_execute_impl",
        lambda n, a: "Browser error (TargetClosedError): boom",
    )
    closes = []
    ensures = []
    _mock_session_methods(browser, monkeypatch, closes, ensures,
                          ensure_raises=RuntimeError("playwright install broken"))

    result = browser._execute_with_recovery("browser_open", {"url": "x"})
    assert "recovery failed" in result.lower()
    assert "escalate" in result.lower() or "stuck" in result.lower()
