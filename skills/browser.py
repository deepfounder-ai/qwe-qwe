"""Browser control skill — navigate, read, click, fill, screenshot via Playwright."""

import os
import time

DESCRIPTION = "Control a web browser — navigate, click, fill forms, take screenshots"

INSTRUCTION = """Use browser tools to interact with web pages:
- browser_open: navigate to a URL, returns page title and text preview
- browser_snapshot: get full page text (best for reading page content)
- browser_screenshot: capture visual screenshot (for layout/visual checks)
- browser_click / browser_fill: interact with page elements
- browser_eval: run JavaScript for advanced queries
- browser_close: close browser when done

Workflow for web search:
1. browser_open("https://www.google.com/search?q=your+query")
2. browser_snapshot() to read search results
3. browser_open(result_url) to visit a result
4. browser_snapshot() to read the page

Tips:
- Prefer browser_snapshot over browser_screenshot (text is cheaper and more useful)
- Use browser_snapshot before browser_click to discover selectors
- Always browser_close when done to free resources
- If a page has dynamic content, use browser_eval to query the DOM
"""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "browser_open",
            "description": "Open a URL in the browser. Launches browser if needed. Returns page title and text preview.",
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
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
]

# ── Module state ──
_playwright = None
_browser = None
_page = None
_network_log = []  # list of {url, method, status, resource_type}


def _ensure_browser():
    """Launch Playwright browser if not running."""
    global _playwright, _browser, _page, _network_log
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
    )
    _page = context.new_page()
    _network_log = []

    def _on_response(response):
        try:
            _network_log.append({
                "url": response.url[:120],
                "method": response.request.method,
                "status": response.status,
                "type": response.request.resource_type,
            })
            # Keep last 50 entries
            if len(_network_log) > 50:
                _network_log.pop(0)
        except Exception:
            pass

    _page.on("response", _on_response)


def _close_browser():
    """Close browser and clean up."""
    global _playwright, _browser, _page, _network_log
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
    _playwright = None
    _browser = None
    _page = None
    _network_log = []


def execute(name: str, args: dict) -> str:
    """Execute a browser tool."""
    try:
        # Handle common hallucinated tool names — redirect to real tools
        if name == "google_search":
            query = args.get("query", args.get("q", ""))
            if query:
                args = {"url": f"https://www.google.com/search?q={query.replace(' ', '+')}"}
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

        return f"Unknown browser tool: {name}"

    except RuntimeError as e:
        return f"Browser error: {e}"
    except Exception as e:
        error_type = type(e).__name__
        return f"Browser error ({error_type}): {str(e)[:300]}"
