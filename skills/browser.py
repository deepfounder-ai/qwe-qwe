"""Browser control skill — navigate, read, click, fill, screenshot via Playwright."""

import os
import time

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
1. browser_open("https://duckduckgo.com/?q=your+query") — NEVER Google (blocks bots)
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
            "description": "Open a URL in a real headless browser (Playwright/Chromium). Use this for ALL web pages: websites, search results, news, articles, HTML. Do NOT use http_request for web pages. Returns page title and text preview.",
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
    # ── NEW: Navigation ──
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
    # ── NEW: Accessibility tree ──
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
    # ── NEW: Console logs ──
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
    # ── NEW: Element interactions ──
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
    # ── NEW: Tabs ──
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

# ── Module state ──
_playwright = None
_browser = None
_page = None               # currently active page
_pages: list = []          # all open pages (tabs)
_network_log = []          # list of {url, method, status, resource_type}
_console_log: list = []    # list of {level, text, location}


def _attach_page_listeners(page):
    """Attach network + console listeners to a page."""
    def _on_response(response):
        try:
            _network_log.append({
                "url": response.url[:120],
                "method": response.request.method,
                "status": response.status,
                "type": response.request.resource_type,
            })
            if len(_network_log) > 50:
                _network_log.pop(0)
        except Exception:
            pass

    def _on_console(msg):
        try:
            _console_log.append({
                "level": msg.type,
                "text": msg.text[:400],
                "location": str(msg.location.get("url", ""))[:120] if hasattr(msg, 'location') and msg.location else "",
            })
            if len(_console_log) > 100:
                _console_log.pop(0)
        except Exception:
            pass

    page.on("response", _on_response)
    page.on("console", _on_console)


def _ensure_browser():
    """Launch Playwright browser if not running."""
    global _playwright, _browser, _page, _pages, _network_log, _console_log
    if _browser and _browser.is_connected():
        return
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError(
            "Playwright is not installed. Run: pip install playwright && python -m playwright install chromium"
        )
    _playwright = sync_playwright().start()
    _browser = _playwright.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage"],
    )
    context = _browser.new_context(
        viewport={"width": 1280, "height": 800},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        ignore_https_errors=True,  # accept self-signed certs (e.g. localhost HTTPS dev servers)
    )
    _page = context.new_page()
    _pages = [_page]
    _network_log = []
    _console_log = []
    _attach_page_listeners(_page)


def _close_browser():
    """Close browser and clean up."""
    global _playwright, _browser, _page, _pages, _network_log, _console_log
    try:
        if _page:
            _page.close()
    except Exception:
        pass
    try:
        if _browser:
            _browser.close()
    except Exception:
        pass
    try:
        if _playwright:
            _playwright.stop()
    except Exception:
        pass
    _pages = []
    _console_log = []
    _playwright = None
    _browser = None
    _page = None
    _network_log = []


def execute(name: str, args: dict) -> str:
    """Execute a browser tool."""
    global _page, _pages
    try:
        # Handle common hallucinated tool names — redirect to real tools
        if name == "google_search":
            query = args.get("query", args.get("q", ""))
            if query:
                args = {"url": f"https://duckduckgo.com/?q={query.replace(' ', '+')}"}
                name = "browser_open"
        elif name in ("open_url", "navigate", "browse"):
            name = "browser_open"
        elif name in ("extract_content", "get_page_content", "read_page"):
            name = "browser_snapshot"
        elif name in ("take_screenshot", "capture_screenshot"):
            name = "browser_screenshot"

        if name == "browser_close":
            _close_browser()
            return "Browser closed."

        # All other tools need browser running
        _ensure_browser()

        if name == "browser_open":
            url = args.get("url", "")
            if not url.startswith(("http://", "https://")):
                url = "https://" + url
            # Auto-redirect Google searches to DuckDuckGo (Google blocks headless browsers)
            import re as _re
            _google_search = _re.match(r"https?://(?:www\.)?google\.com/search\?.*q=([^&]+)", url)
            _google_news = _re.match(r"https?://news\.google\.com/search\?.*q=([^&]+)", url)
            if _google_search:
                url = f"https://duckduckgo.com/?q={_google_search.group(1)}"
            elif _google_news:
                url = f"https://duckduckgo.com/?q={_google_news.group(1)}+news"
            elif "google.com/search" in url or "news.google.com" in url:
                url = "https://duckduckgo.com/"
            _page.goto(url, wait_until="domcontentloaded", timeout=15000)
            # Wait a bit for dynamic content
            _page.wait_for_timeout(1000)
            title = _page.title() or "(no title)"
            # Get text preview
            try:
                text = _page.inner_text("body")[:2000]
            except Exception:
                text = "(could not extract text)"
            return f"Title: {title}\nURL: {_page.url}\n\n{text}"

        elif name == "browser_screenshot":
            import config
            workspace = str(config.WORKSPACE_DIR)
            filename = f"screenshot_{int(time.time())}.png"
            filepath = os.path.join(workspace, filename)
            _page.screenshot(path=filepath, full_page=False)
            return f"Screenshot saved: {filepath} ({os.path.getsize(filepath)} bytes)"

        elif name == "browser_snapshot":
            selector = args.get("selector", "body")
            try:
                text = _page.inner_text(selector)
            except Exception:
                text = _page.content()
            title = _page.title() or ""
            url = _page.url
            # Truncate for LLM context
            if len(text) > 4000:
                text = text[:4000] + "\n... (truncated)"
            return f"Title: {title}\nURL: {url}\n\n{text}"

        elif name == "browser_click":
            selector = args["selector"]
            # Try direct CSS selector first, then text-based
            try:
                _page.click(selector, timeout=5000)
            except Exception:
                # Try as text selector
                _page.get_by_text(selector).first.click(timeout=5000)
            _page.wait_for_timeout(1000)
            title = _page.title() or ""
            return f"Clicked '{selector}'. Page: {title} ({_page.url})"

        elif name == "browser_fill":
            selector = args["selector"]
            value = args["value"]
            _page.fill(selector, value, timeout=5000)
            return f"Filled '{selector}' with '{value[:50]}'"

        elif name == "browser_eval":
            expression = args["expression"]
            result = _page.evaluate(expression)
            import json
            try:
                return json.dumps(result, ensure_ascii=False, default=str)[:2000]
            except Exception:
                return str(result)[:2000]

        elif name == "browser_network":
            filt = args.get("filter", "all")
            entries = _network_log[-30:]  # last 30
            if filt == "failed":
                entries = [e for e in entries if e["status"] >= 400]
            if not entries:
                return "No network requests recorded." if filt == "all" else "No failed requests."
            lines = [f"{e['method']} {e['status']} {e['type']}: {e['url']}" for e in entries]
            return "\n".join(lines)

        # ── Navigation: back/forward/reload ──
        elif name == "browser_back":
            _page.go_back(timeout=10000)
            _page.wait_for_timeout(500)
            return f"Back. Page: {_page.title() or ''} ({_page.url})"

        elif name == "browser_forward":
            _page.go_forward(timeout=10000)
            _page.wait_for_timeout(500)
            return f"Forward. Page: {_page.title() or ''} ({_page.url})"

        elif name == "browser_reload":
            _page.reload(timeout=15000)
            _page.wait_for_timeout(500)
            return f"Reloaded. Page: {_page.title() or ''} ({_page.url})"

        # ── Accessibility tree (structured DOM) ──
        elif name == "browser_accessibility":
            # Playwright removed page.accessibility in 1.56+. Use CDP directly.
            try:
                cdp = _page.context.new_cdp_session(_page)
                tree = cdp.send("Accessibility.getFullAXTree", {})
                cdp.detach()
                nodes = tree.get("nodes", [])
                # Keep only meaningful nodes (with role+name)
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
            except Exception as e:
                # Fallback: JS-based extraction of interactive elements
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
                result = _page.evaluate(expr)
                import json as _json
                text = _json.dumps(result, ensure_ascii=False, indent=2)
                if len(text) > 8000:
                    text = text[:8000] + "\n... (truncated)"
                return text

        # ── Console logs ──
        elif name == "browser_console":
            level = (args.get("level") or "all").lower()
            limit = int(args.get("limit") or 50)
            entries = _console_log[-limit:]
            if level != "all":
                entries = [e for e in entries if e["level"] == level]
            if not entries:
                return f"No {level} console messages."
            lines = [f"[{e['level']}] {e['text']}" + (f"  @ {e['location']}" if e['location'] else "") for e in entries]
            return "\n".join(lines)

        # ── Element interactions ──
        elif name == "browser_hover":
            selector = args["selector"]
            try:
                _page.hover(selector, timeout=5000)
            except Exception:
                _page.get_by_text(selector).first.hover(timeout=5000)
            _page.wait_for_timeout(300)
            return f"Hovered '{selector}'"

        elif name == "browser_select":
            selector = args["selector"]
            value = args["value"]
            # Try by value first, then by label
            try:
                _page.select_option(selector, value=value, timeout=5000)
            except Exception:
                _page.select_option(selector, label=value, timeout=5000)
            return f"Selected '{value}' in {selector}"

        elif name == "browser_press_key":
            key = args["key"]
            _page.keyboard.press(key)
            _page.wait_for_timeout(200)
            return f"Pressed '{key}'"

        elif name == "browser_wait_for":
            selector = args["selector"]
            state = args.get("state", "visible")
            timeout = int(args.get("timeout_ms", 5000))
            try:
                _page.wait_for_selector(selector, state=state, timeout=timeout)
                return f"'{selector}' is now {state}"
            except Exception as e:
                return f"Timeout waiting for '{selector}' ({state}): {str(e)[:150]}"

        elif name == "browser_drag":
            source = args["source"]
            target = args["target"]
            _page.drag_and_drop(source, target, timeout=5000)
            _page.wait_for_timeout(300)
            return f"Dragged '{source}' → '{target}'"

        elif name == "browser_upload":
            selector = args["selector"]
            filepath = args["filepath"]
            _page.set_input_files(selector, filepath)
            return f"Uploaded {filepath} to {selector}"

        # ── Tabs ──
        elif name == "browser_tabs":
            # Clean closed pages
            _pages = [p for p in _pages if not p.is_closed()]
            if not _pages:
                return "No open tabs."
            active_idx = _pages.index(_page) if _page in _pages else -1
            lines = []
            for i, p in enumerate(_pages):
                marker = "→ " if i == active_idx else "  "
                try:
                    title = p.title() or "(untitled)"
                except Exception:
                    title = "?"
                lines.append(f"{marker}[{i}] {title} — {p.url}")
            return "\n".join(lines)

        elif name == "browser_tab_new":
            url = args.get("url")
            ctx = _page.context
            new_page = ctx.new_page()
            _pages.append(new_page)
            _page = new_page
            _attach_page_listeners(new_page)
            if url:
                if not url.startswith(("http://", "https://")):
                    url = "https://" + url
                new_page.goto(url, wait_until="domcontentloaded", timeout=15000)
                new_page.wait_for_timeout(500)
            return f"Opened tab [{len(_pages)-1}]: {new_page.title() or ''} ({new_page.url})"

        elif name == "browser_tab_switch":
            idx = int(args["index"])
            _pages = [p for p in _pages if not p.is_closed()]
            if idx < 0 or idx >= len(_pages):
                return f"Invalid index {idx}. Valid: 0..{len(_pages)-1}"
            _page = _pages[idx]
            _page.bring_to_front()
            return f"Switched to tab [{idx}]: {_page.title() or ''} ({_page.url})"

        elif name == "browser_tab_close":
            idx = int(args["index"])
            _pages = [p for p in _pages if not p.is_closed()]
            if idx < 0 or idx >= len(_pages):
                return f"Invalid index {idx}. Valid: 0..{len(_pages)-1}"
            closing = _pages.pop(idx)
            closing.close()
            if _page == closing and _pages:
                _page = _pages[0]
            return f"Closed tab [{idx}]. Now {len(_pages)} tab(s) open."

        return f"Unknown browser tool: {name}"

    except RuntimeError as e:
        return f"Browser error: {e}"
    except Exception as e:
        error_type = type(e).__name__
        return f"Browser error ({error_type}): {str(e)[:300]}"
