"""Browser control skill — navigate, read, click, fill, screenshot via Playwright."""

import asyncio
import concurrent.futures
import logging
import os
import threading
import time

_log = logging.getLogger("castor.browser")

DESCRIPTION = "Control a web browser — navigate, click, fill forms, take screenshots"

INSTRUCTION = """Use browser tools to interact with web pages:

Navigation:
- browser_open(url): navigate to URL, returns title + text preview
- browser_back() / browser_forward(): history navigation
- browser_reload(): refresh current page

Reading page content:
- browser_snapshot(selector="body"): full page text (cheap, best for reading)
- browser_accessibility(): structured a11y tree (best for LLMs to find elements)
- browser_screenshot(): visual capture (only when you need to SEE layout)
- browser_console(): read browser console logs (for debugging)

Interacting:
- browser_click(selector): click element
- browser_fill(selector, value): fill input/textarea
- browser_select(selector, value): pick from <select>
- browser_hover(selector): hover (reveals tooltips/menus)
- browser_press_key(key): press key (Enter/Escape/Tab/etc)
- browser_wait_for(selector, timeout): wait until element appears
- browser_drag(source, target): drag-and-drop element
- browser_upload(selector, filepath): upload file via <input type=file>

Tabs:
- browser_tabs(): list open tabs
- browser_tab_new(url): open new tab
- browser_tab_switch(index): switch to tab N
- browser_tab_close(index): close tab

Advanced:
- browser_eval(expression): run JavaScript, returns result
- browser_network(filter): list recent network requests
- browser_close(): close browser, free resources

Workflow for web search:
1. browser_open("https://search.brave.com/search?q=your+query") — NEVER Google (blocks bots)
2. browser_snapshot() to read results
3. browser_open(result_url) then browser_snapshot() to read article

Tips:
- Prefer browser_snapshot (text) over browser_screenshot (image) — cheaper
- Use browser_accessibility to find clickable elements with proper selectors
- Use browser_wait_for before browser_click on dynamic pages
- Always browser_close when done to free resources
"""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "browser_open",
            "description": "Fetch and read a web page in background (headless, invisible to user). Returns page text. Use for searching, reading articles, scraping. NOT for opening browser for user — use open_url for that.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to navigate to"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_screenshot",
            "description": "Take a screenshot of the current page. Returns the file path.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_snapshot",
            "description": "Get the text content of the current page (no images). Best for reading.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "CSS selector to read (default: body)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_click",
            "description": "Click an element on the page by CSS selector or visible text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "CSS selector (e.g. 'button.submit', '#login', 'a:text(\"Sign in\")')"},
                },
                "required": ["selector"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_fill",
            "description": "Fill an input, textarea, or select element with a value.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "CSS selector for the input"},
                    "value": {"type": "string", "description": "Value to fill"},
                },
                "required": ["selector", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_eval",
            "description": "Execute JavaScript in the page and return the result.",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {"type": "string", "description": "JavaScript expression to evaluate"},
                },
                "required": ["expression"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_network",
            "description": "List recent network requests. Use filter='failed' for errors only.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filter": {"type": "string", "description": "'all' or 'failed' (default: all)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_close",
            "description": "Close the browser and free resources.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_set_visible",
            "description": "Switch browser to VISIBLE mode (user can see the window) or back to headless. Call this BEFORE browser_open when user wants to watch you work in the browser.",
            "parameters": {
                "type": "object",
                "properties": {
                    "visible": {"type": "boolean", "description": "true = show browser window, false = headless (default)"},
                },
                "required": ["visible"],
            },
        },
    },
    # ──Navigation ──
    {
        "type": "function",
        "function": {
            "name": "browser_back",
            "description": "Go back in browser history.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_forward",
            "description": "Go forward in browser history.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_reload",
            "description": "Reload the current page.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    # ──Accessibility tree ──
    {
        "type": "function",
        "function": {
            "name": "browser_accessibility",
            "description": "Get structured accessibility tree of the page (roles, names, clickable elements). Best tool to discover what's interactable and find exact selectors for LLM-driven actions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "interesting_only": {"type": "boolean", "description": "Only show elements that matter (default true)"},
                },
            },
        },
    },
    # ──Console logs ──
    {
        "type": "function",
        "function": {
            "name": "browser_console",
            "description": "Read browser console messages (errors, warnings, logs). Use for debugging JS issues.",
            "parameters": {
                "type": "object",
                "properties": {
                    "level": {"type": "string", "description": "Filter: 'all', 'error', 'warning' (default all)"},
                    "limit": {"type": "number", "description": "Max messages (default 50)"},
                },
            },
        },
    },
    # ──Element interactions ──
    {
        "type": "function",
        "function": {
            "name": "browser_hover",
            "description": "Hover over an element (reveals tooltips, dropdown menus, hover states).",
            "parameters": {
                "type": "object",
                "properties": {"selector": {"type": "string", "description": "CSS selector"}},
                "required": ["selector"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_select",
            "description": "Select option in a <select> dropdown.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "CSS selector of the <select>"},
                    "value": {"type": "string", "description": "Option value or visible label"},
                },
                "required": ["selector", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_press_key",
            "description": "Press a keyboard key (e.g. Enter, Escape, Tab, ArrowDown, PageDown, Control+A).",
            "parameters": {
                "type": "object",
                "properties": {"key": {"type": "string", "description": "Key name: Enter, Escape, Tab, ArrowUp/Down/Left/Right, PageDown, etc."}},
                "required": ["key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_wait_for",
            "description": "Wait until an element appears (or disappears) on the page. Use before interacting with dynamic content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "CSS selector"},
                    "state": {"type": "string", "description": "'visible' (default), 'hidden', 'attached', 'detached'"},
                    "timeout_ms": {"type": "number", "description": "Max wait in ms (default 5000)"},
                },
                "required": ["selector"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_drag",
            "description": "Drag one element and drop it onto another (drag-and-drop).",
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {"type": "string", "description": "CSS selector of the element to drag"},
                    "target": {"type": "string", "description": "CSS selector of the drop target"},
                },
                "required": ["source", "target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_upload",
            "description": "Upload a file via an <input type='file'> element.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "CSS selector of the file input"},
                    "filepath": {"type": "string", "description": "Absolute local path to the file"},
                },
                "required": ["selector", "filepath"],
            },
        },
    },
    # ──Tabs ──
    {
        "type": "function",
        "function": {
            "name": "browser_tabs",
            "description": "List all open tabs with index, title, URL.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_tab_new",
            "description": "Open a new tab and switch to it.",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string", "description": "URL to open (optional)"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_tab_switch",
            "description": "Switch to a tab by index (from browser_tabs).",
            "parameters": {
                "type": "object",
                "properties": {"index": {"type": "number", "description": "Tab index (0-based)"}},
                "required": ["index"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_tab_close",
            "description": "Close a tab by index.",
            "parameters": {
                "type": "object",
                "properties": {"index": {"type": "number", "description": "Tab index (0-based)"}},
                "required": ["index"],
            },
        },
    },
]

# ── Per-goal browser sessions (Phase 3 of long-running agent runtime) ──
#
# Each Goal gets its own BrowserSession keyed by goal_id, with a persistent
# user_data_dir under ~/.castor/browser_sessions/<goal_id>/. This means:
#
#   - Parallel goals don't fight over cookies/login/page state.
#   - A goal that logged into LinkedIn in subtask 1 keeps that session for
#     subtasks 2..N — even across worker restarts (user_data_dir on disk).
#   - Chat / cli / telegram (no goal context) share a single "__default__"
#     session — same UX as before this refactor.
#
# Concurrency model: each session has its OWN Playwright instance + lock.
# The global _browser_executor still serialises Python calls into Playwright
# (Playwright sync API isn't thread-safe within one instance), but different
# sessions can have their browsers open simultaneously — different Chrome
# processes, fully isolated profiles.
import config
from pathlib import Path


_DEFAULT_SESSION_ID = "__default__"
_headless_mode = True  # default for fresh sessions; can be flipped per-session


def _attach_page_listeners(page, session):
    """Attach network + console listeners that write into *session*'s logs."""
    def _on_response(response):
        try:
            session.network_log.append({
                "url": response.url[:120],
                "method": response.request.method,
                "status": response.status,
                "type": response.request.resource_type,
            })
            if len(session.network_log) > 50:
                session.network_log.pop(0)
        except Exception:
            pass

    def _on_console(msg):
        try:
            session.console_log.append({
                "level": msg.type,
                "text": msg.text[:400],
                "location": str(msg.location.get("url", ""))[:120] if hasattr(msg, 'location') and msg.location else "",
            })
            if len(session.console_log) > 100:
                session.console_log.pop(0)
        except Exception:
            pass

    page.on("response", _on_response)
    page.on("console", _on_console)


class BrowserSession:
    """One isolated browser state — Playwright instance, BrowserContext,
    open pages, per-page logs. Use ``ensure_running()`` to lazily launch
    Chrome with the session's ``user_data_dir`` and ``close()`` to release.

    Thread-safe: ``self.lock`` is held during launch + close so concurrent
    tool calls on the same session don't double-launch or close mid-op.
    The global _browser_executor (max_workers=1) further serialises
    Playwright sync calls.
    """

    def __init__(self, session_id: str, headless: bool = True):
        self.session_id = session_id
        self.headless = headless
        self.playwright = None
        # NOTE: this is a BrowserContext (from launch_persistent_context),
        # not a Browser. The .close() method closes both context AND the
        # underlying browser process, so we never store the browser handle
        # separately.
        self.browser = None
        self.page = None
        self.pages: list = []
        self.network_log: list = []
        self.console_log: list = []
        self.lock = threading.Lock()
        # The persistent profile directory. Created lazily in ensure_running.
        self.user_data_dir = (
            Path(config.DATA_DIR) / "browser_sessions" / session_id
        )

    def is_alive(self) -> bool:
        if self.browser is None:
            return False
        try:
            # BrowserContext doesn't expose .is_connected(); .pages access
            # raises if the underlying context is closed.
            _ = self.browser.pages
            return True
        except Exception:
            return False

    def ensure_running(self) -> None:
        if self.is_alive():
            return
        with self.lock:
            if self.is_alive():
                return
            try:
                from playwright.sync_api import sync_playwright
            except ImportError:
                raise RuntimeError(
                    "Playwright is not installed. Run: pip install playwright "
                    "&& python -m playwright install chromium"
                )
            self.user_data_dir.mkdir(parents=True, exist_ok=True)
            self.playwright = sync_playwright().start()
            self.browser = self.playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.user_data_dir),
                headless=self.headless,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
                viewport={"width": 1280, "height": 800},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                ignore_https_errors=True,
            )
            existing = list(self.browser.pages)
            self.pages = existing or [self.browser.new_page()]
            self.page = self.pages[0]
            self.network_log = []
            self.console_log = []
            _attach_page_listeners(self.page, self)

    def close(self) -> None:
        with self.lock:
            try:
                if self.browser:
                    self.browser.close()
            except Exception:
                pass
            try:
                if self.playwright:
                    self.playwright.stop()
            except Exception:
                pass
            self.browser = None
            self.playwright = None
            self.page = None
            self.pages = []
            self.network_log = []
            self.console_log = []


# Session registry. _get_session is idempotent / get-or-create.
_sessions: dict[str, BrowserSession] = {}
_sessions_registry_lock = threading.Lock()


def _get_session(session_id: str) -> BrowserSession:
    with _sessions_registry_lock:
        sess = _sessions.get(session_id)
        if sess is None:
            sess = BrowserSession(session_id, headless=_headless_mode)
            _sessions[session_id] = sess
        return sess


def _get_active_session() -> BrowserSession:
    """Pick session based on the active TurnContext.

    Resolution order:
      1. _executor_thread_session.session_id — set when we cross the
         ThreadPoolExecutor boundary so the inner thread knows which goal
         it's serving (ctx is per-thread, doesn't auto-propagate).
      2. ctx.goal_id — set by tools._set_turn_ctx before each tool dispatch.
      3. fall back to "__default__" (chat / cli / telegram).
    """
    try:
        override = getattr(_executor_thread_session, "session_id", None)
        if override:
            return _get_session(override)
    except Exception:
        pass
    try:
        # Local import to avoid the tools↔skills circular dependency at
        # module import time.
        import tools as _tools
        ctx = _tools._get_turn_ctx()
        if ctx is not None:
            gid = getattr(ctx, "goal_id", None)
            if gid:
                return _get_session(gid)
    except Exception:
        pass
    return _get_session(_DEFAULT_SESSION_ID)


def _close_session(session_id: str) -> None:
    """Close + drop a session from the registry. Idempotent."""
    with _sessions_registry_lock:
        sess = _sessions.pop(session_id, None)
    if sess is not None:
        sess.close()


# Serialize browser launch/close to prevent multiple Chrome windows when
# concurrent spawn_task workers all try to open the browser at the same time.
# (Kept module-level for backward-compat with any external caller that
# imports it; new code uses session.lock.)
_browser_lock = threading.Lock()


# Backward-compat shims so any external caller that imported _ensure_browser /
# _close_browser still works. They operate on the active session.

def _ensure_browser():
    """Backward-compat shim: ensure the active session's browser is running."""
    _get_active_session().ensure_running()


def _close_browser():
    """Backward-compat shim: close the active session's browser."""
    _get_active_session().close()


# Legacy access for old code that read module-level _page / _pages directly
# (none of our codebase does after this refactor, but keep these as None to
# avoid AttributeError on accidental imports).
_playwright = None
_browser = None
_page = None
_pages: list = []
_network_log: list = []
_console_log: list = []


_browser_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)


# Error class names that mean "browser session is dead — recoverable by restart".
# Playwright raises these when the underlying Chrome process crashed, was killed
# (eg the user externally `pkill`-ed Chrome), or the CDP socket dropped. We do
# NOT want to surface raw "TargetClosedError" up to the LLM — autonomous agents
# treat that as a permanent failure and give up. Instead, infrastructure
# auto-recovers: close stale state, relaunch, retry the operation once.
_RECOVERABLE_BROWSER_ERRORS = (
    "TargetClosedError",
    "BrowserClosedError",
    "Connection closed",
    "Target page, context or browser has been closed",
    "browserContext.newPage",
)


def _looks_like_dead_session(err_str: str) -> bool:
    """Heuristic: does this error mean 'browser process is dead, can be relaunched'?"""
    if not err_str:
        return False
    return any(marker in err_str for marker in _RECOVERABLE_BROWSER_ERRORS)


def _execute_with_recovery(name: str, args: dict) -> str:
    """Run a browser op. On dead-session errors, auto-close + retry once.

    The autonomy contract: we don't want the LLM to see TargetClosedError and
    decide the goal is unreachable. The infrastructure should heal itself.
    Only after a SECOND failure do we surface the error string — that's when
    something genuinely odd is happening that the LLM should reason about.
    """
    # Capture which session WAS active when the call began, so recovery
    # operates on the SAME session (otherwise a context switch between
    # calls could heal the wrong session).
    session = _get_active_session()
    result = _execute_impl(name, args)
    if not _looks_like_dead_session(result):
        return result
    if name in ("browser_close", "browser_set_visible"):
        return result
    _log.warning(f"browser tool {name} hit dead-session error on session "
                 f"{session.session_id!r}; auto-recovering: {result[:140]}")
    try:
        session.close()
    except Exception:
        pass
    try:
        session.ensure_running()
    except Exception as e:
        return f"Browser recovery failed: {e}. Goal-runtime is now stuck — escalate to user."
    retry_result = _execute_impl(name, args)
    if _looks_like_dead_session(retry_result):
        return (
            f"Browser session died twice in a row — likely Playwright/Chromium is "
            f"broken on this host. Original error: {result[:200]}. After auto-recovery "
            f"retry: {retry_result[:200]}. Consider falling back to http_request or "
            f"alternative data sources (no browser required)."
        )
    return (
        f"[recovered from dead session — auto-closed stale browser, retried successfully]\n"
        + retry_result
    )


def execute(name: str, args: dict) -> str:
    """Execute a browser tool - runs in a dedicated thread to avoid asyncio conflicts.

    Session resolution happens HERE (in the caller's thread) so the
    ctx.goal_id stashed in threading.local doesn't get lost when we hop
    to the _browser_executor thread. We resolve the session_id up-front
    and stash it on threading.local of the inner thread before delegating
    to _execute_with_recovery.
    """
    # Resolve the target session in THIS thread — _get_active_session
    # reads tools._get_turn_ctx() which is threading.local.
    target_session_id = _resolve_session_id_from_ctx()

    # Check if we're inside an asyncio event loop
    try:
        loop = asyncio.get_running_loop()
        in_async = loop.is_running()
    except RuntimeError:
        in_async = False

    if not in_async:
        # Same thread — ctx is already set, just dispatch.
        return _execute_with_recovery(name, args)

    # Async caller: hop to the executor thread, and propagate the
    # resolved session_id explicitly so the executor doesn't fall back
    # to "__default__".
    def _run_with_session():
        _executor_thread_session.session_id = target_session_id
        try:
            return _execute_with_recovery(name, args)
        finally:
            _executor_thread_session.session_id = None

    future = _browser_executor.submit(_run_with_session)
    try:
        return future.result(timeout=30)
    except concurrent.futures.TimeoutError:
        return "Error: browser operation timed out after 30s"


# Thread-local override used by the _browser_executor thread.
# _get_active_session reads this BEFORE falling back to ctx-lookup.
_executor_thread_session = threading.local()


def _resolve_session_id_from_ctx() -> str:
    """Same logic as _get_active_session but returns the id only, not the
    object. Caller passes this through threading boundaries."""
    try:
        import tools as _tools
        ctx = _tools._get_turn_ctx()
        if ctx is not None:
            gid = getattr(ctx, "goal_id", None)
            if gid:
                return gid
    except Exception:
        pass
    return _DEFAULT_SESSION_ID


def _execute_impl(name: str, args: dict) -> str:
    """Browser tool execution against the active per-goal session.

    Picks the session at the top via _get_active_session() — a Goal-bound
    ctx routes to its own per-goal session (isolated cookies, page state,
    network log), anything else goes to the singleton ``__default__``
    session. Two parallel goals never share state.
    """
    session = _get_active_session()
    try:
        # Handle common hallucinated tool names — redirect to real tools
        if name == "google_search":
            query = args.get("query", args.get("q", ""))
            if query:
                args = {"url": f"https://search.brave.com/search?q={query.replace(' ', '+')}"}
                name = "browser_open"
        elif name in ("open_url", "navigate", "browse"):
            name = "browser_open"
        elif name in ("extract_content", "get_page_content", "read_page"):
            name = "browser_snapshot"
        elif name in ("take_screenshot", "capture_screenshot"):
            name = "browser_screenshot"

        if name == "browser_set_visible":
            visible = args.get("visible", True)
            new_mode = not visible  # headless is the opposite of visible
            with session.lock:
                if new_mode == session.headless and session.is_alive():
                    return f"Browser already in {'visible' if visible else 'headless'} mode (reusing existing window)."
                if new_mode != session.headless:
                    session.headless = new_mode
                    # Trigger relaunch with new mode on the next call.
                    # Inside the lock would deadlock (close() acquires it),
                    # so flag it for after-release.
                    _need_close = True
                else:
                    _need_close = False
            if _need_close:
                session.close()
            if visible:
                try:
                    import telemetry as _tel
                    _tel.track_feature_first_use("browser_visible")
                except Exception:
                    pass
            return f"Browser set to {'visible' if visible else 'headless'} mode. Next browser_open will {'show' if visible else 'hide'} the window."

        if name == "browser_close":
            session.close()
            return "Browser closed."

        # All other tools need browser running
        session.ensure_running()

        if name == "browser_open":
            url = args.get("url", "")
            if not url.startswith(("http://", "https://")):
                url = "https://" + url
            session.page.goto(url, wait_until="domcontentloaded", timeout=15000)
            session.page.wait_for_timeout(1000)
            title = session.page.title() or "(no title)"
            try:
                text = session.page.inner_text("body")[:2000]
            except Exception:
                text = "(could not extract text)"

            links_section = ""
            if any(se in session.page.url for se in ("duckduckgo.com", "bing.com", "google.com", "search.brave.com")):
                try:
                    links = session.page.evaluate('''() => {
                        const results = [];
                        document.querySelectorAll('a[href]').forEach(a => {
                            const href = a.href;
                            const text = (a.innerText || '').trim().substring(0, 80);
                            if (text.length > 10 && href.startsWith('http') &&
                                !href.includes('duckduckgo.com') && !href.includes('bing.com') &&
                                !href.includes('google.com') && !href.includes('brave.com') &&
                                !href.includes('javascript:'))
                                results.push({title: text, url: href});
                        });
                        return results.slice(0, 10);
                    }''')
                    if links:
                        links_section = "\n\n--- Search Results (clickable URLs) ---\n"
                        for i, lnk in enumerate(links, 1):
                            links_section += f"{i}. [{lnk['title']}]({lnk['url']})\n"
                        links_section += "\nTo open a result: browser_open(url)"
                except Exception:
                    pass

            return f"Title: {title}\nURL: {session.page.url}\n\n{text}{links_section}"

        elif name == "browser_screenshot":
            workspace = str(config.WORKSPACE_DIR)
            filename = f"screenshot_{int(time.time())}.png"
            filepath = os.path.join(workspace, filename)
            session.page.screenshot(path=filepath, full_page=False)
            return f"Screenshot saved: {filepath} ({os.path.getsize(filepath)} bytes)"

        elif name == "browser_snapshot":
            selector = args.get("selector", "body")
            try:
                text = session.page.inner_text(selector)
            except Exception:
                text = session.page.content()
            title = session.page.title() or ""
            url = session.page.url
            if len(text) > 4000:
                text = text[:4000] + "\n... (truncated)"
            return f"Title: {title}\nURL: {url}\n\n{text}"

        elif name == "browser_click":
            selector = args["selector"]
            try:
                session.page.click(selector, timeout=5000)
            except Exception:
                session.page.get_by_text(selector).first.click(timeout=5000)
            session.page.wait_for_timeout(1000)
            title = session.page.title() or ""
            return f"Clicked '{selector}'. Page: {title} ({session.page.url})"

        elif name == "browser_fill":
            selector = args["selector"]
            value = args["value"]
            session.page.fill(selector, value, timeout=5000)
            return f"Filled '{selector}' with '{value[:50]}'"

        elif name == "browser_eval":
            expression = args["expression"]
            result = session.page.evaluate(expression)
            import json
            try:
                return json.dumps(result, ensure_ascii=False, default=str)[:2000]
            except Exception:
                return str(result)[:2000]

        elif name == "browser_network":
            filt = args.get("filter", "all")
            entries = session.network_log[-30:]
            if filt == "failed":
                entries = [e for e in entries if e["status"] >= 400]
            if not entries:
                return "No network requests recorded." if filt == "all" else "No failed requests."
            lines = [f"{e['method']} {e['status']} {e['type']}: {e['url']}" for e in entries]
            return "\n".join(lines)

        elif name == "browser_back":
            session.page.go_back(timeout=10000)
            session.page.wait_for_timeout(500)
            return f"Back. Page: {session.page.title() or ''} ({session.page.url})"

        elif name == "browser_forward":
            session.page.go_forward(timeout=10000)
            session.page.wait_for_timeout(500)
            return f"Forward. Page: {session.page.title() or ''} ({session.page.url})"

        elif name == "browser_reload":
            session.page.reload(timeout=15000)
            session.page.wait_for_timeout(500)
            return f"Reloaded. Page: {session.page.title() or ''} ({session.page.url})"

        elif name == "browser_accessibility":
            try:
                cdp = session.page.context.new_cdp_session(session.page)
                tree = cdp.send("Accessibility.getFullAXTree", {})
                cdp.detach()
                nodes = tree.get("nodes", [])
                interesting = args.get("interesting_only", True)
                simple = []
                for n in nodes:
                    role = (n.get("role") or {}).get("value", "")
                    name_v = (n.get("name") or {}).get("value", "")
                    if interesting and not (role and (name_v or role in ("button", "link", "textbox", "checkbox"))):
                        continue
                    simple.append({"role": role, "name": name_v[:80] if name_v else "", "id": n.get("nodeId")})
                import json as _json
                text = _json.dumps(simple[:100], ensure_ascii=False, indent=2)
                if len(text) > 8000:
                    text = text[:8000] + "\n... (truncated)"
                return text
            except Exception:
                expr = '''(function(){
                  const els = document.querySelectorAll('button, a, input, select, textarea, [role]');
                  return Array.from(els).slice(0, 100).map(e => ({
                    tag: e.tagName.toLowerCase(),
                    role: e.getAttribute('role') || '',
                    text: (e.innerText || e.value || e.placeholder || e.title || '').substring(0, 60),
                    id: e.id || '',
                    class: (e.className || '').substring(0, 60)
                  })).filter(x => x.text || x.id || x.role);
                })()'''
                result = session.page.evaluate(expr)
                import json as _json
                text = _json.dumps(result, ensure_ascii=False, indent=2)
                if len(text) > 8000:
                    text = text[:8000] + "\n... (truncated)"
                return text

        elif name == "browser_console":
            level = (args.get("level") or "all").lower()
            limit = int(args.get("limit") or 50)
            entries = session.console_log[-limit:]
            if level != "all":
                entries = [e for e in entries if e["level"] == level]
            if not entries:
                return f"No {level} console messages."
            lines = [f"[{e['level']}] {e['text']}" + (f"  @ {e['location']}" if e['location'] else "") for e in entries]
            return "\n".join(lines)

        elif name == "browser_hover":
            selector = args["selector"]
            try:
                session.page.hover(selector, timeout=5000)
            except Exception:
                session.page.get_by_text(selector).first.hover(timeout=5000)
            session.page.wait_for_timeout(300)
            return f"Hovered '{selector}'"

        elif name == "browser_select":
            selector = args["selector"]
            value = args["value"]
            try:
                session.page.select_option(selector, value=value, timeout=5000)
            except Exception:
                session.page.select_option(selector, label=value, timeout=5000)
            return f"Selected '{value}' in {selector}"

        elif name == "browser_press_key":
            key = args["key"]
            session.page.keyboard.press(key)
            session.page.wait_for_timeout(200)
            return f"Pressed '{key}'"

        elif name == "browser_wait_for":
            selector = args["selector"]
            state = args.get("state", "visible")
            timeout = int(args.get("timeout_ms", 5000))
            try:
                session.page.wait_for_selector(selector, state=state, timeout=timeout)
                return f"'{selector}' is now {state}"
            except Exception as e:
                return f"Timeout waiting for '{selector}' ({state}): {str(e)[:150]}"

        elif name == "browser_drag":
            source = args["source"]
            target = args["target"]
            session.page.drag_and_drop(source, target, timeout=5000)
            session.page.wait_for_timeout(300)
            return f"Dragged '{source}' → '{target}'"

        elif name == "browser_upload":
            selector = args["selector"]
            filepath = args["filepath"]
            session.page.set_input_files(selector, filepath)
            return f"Uploaded {filepath} to {selector}"

        elif name == "browser_tabs":
            session.pages = [p for p in session.pages if not p.is_closed()]
            if not session.pages:
                return "No open tabs."
            active_idx = session.pages.index(session.page) if session.page in session.pages else -1
            lines = []
            for i, p in enumerate(session.pages):
                marker = "→ " if i == active_idx else "  "
                try:
                    title = p.title() or "(untitled)"
                except Exception:
                    title = "?"
                lines.append(f"{marker}[{i}] {title} — {p.url}")
            return "\n".join(lines)

        elif name == "browser_tab_new":
            url = args.get("url")
            ctx = session.page.context
            new_page = ctx.new_page()
            session.pages.append(new_page)
            session.page = new_page
            _attach_page_listeners(new_page, session)
            if url:
                if not url.startswith(("http://", "https://")):
                    url = "https://" + url
                new_page.goto(url, wait_until="domcontentloaded", timeout=15000)
                new_page.wait_for_timeout(500)
            return f"Opened tab [{len(session.pages)-1}]: {new_page.title() or ''} ({new_page.url})"

        elif name == "browser_tab_switch":
            idx = int(args["index"])
            session.pages = [p for p in session.pages if not p.is_closed()]
            if idx < 0 or idx >= len(session.pages):
                return f"Invalid index {idx}. Valid: 0..{len(session.pages)-1}"
            session.page = session.pages[idx]
            session.page.bring_to_front()
            return f"Switched to tab [{idx}]: {session.page.title() or ''} ({session.page.url})"

        elif name == "browser_tab_close":
            idx = int(args["index"])
            session.pages = [p for p in session.pages if not p.is_closed()]
            if idx < 0 or idx >= len(session.pages):
                return f"Invalid index {idx}. Valid: 0..{len(session.pages)-1}"
            closing = session.pages.pop(idx)
            closing.close()
            if session.page == closing and session.pages:
                session.page = session.pages[0]
            return f"Closed tab [{idx}]. Now {len(session.pages)} tab(s) open."

        return f"Unknown browser tool: {name}"

    except RuntimeError as e:
        return f"Browser error: {e}"
    except Exception as e:
        error_type = type(e).__name__
        return f"Browser error ({error_type}): {str(e)[:300]}"
