# Browser — visible Chrome + headless automation

The agent has two ways to use a browser:

1. **Headless** (`browser_open`) — Chromium in the background, fast, invisible. For scraping, fetching content, automated logins. The agent is the only one who sees the page.
2. **Visible** (`browser_set_visible(true)` then any browser tool) — a real Chrome window pops open on your desktop. You watch the agent navigate; you can intervene; you handle the parts the agent shouldn't (passwords, 2FA codes).
3. **Open in user's browser** (`open_url`) — castor doesn't drive the page at all. Hands the URL to your OS default browser. For "show me this thing" — the agent's job ends at the URL.

The right tool depends on whether the goal is **the page's content** (headless) or **a logged-in workflow** (visible).

## Quick start

The browser skill is built-in but **not active by default** (loading 23 tools every turn would blow the token budget). Activate when needed:

```
You:    Open Wikipedia for "FastAPI" and summarize it.
Agent:  [tool_search("browser")] → 7 quickstart tools activated
        [browser_open url="https://en.wikipedia.org/wiki/FastAPI"]
        [browser_snapshot] → reads main content
        FastAPI is a modern Python web framework ...
```

`tool_search` keywords that activate browser tools: `browser`, `web`, `scrape`, `page`, `chrome`, `playwright`.

## Quickstart vs full tool surface

After `tool_search("browser")` the agent gets 7 **quickstart** tools — the high-value 80%:

| Tool | What it does |
|---|---|
| `browser_open(url)` | Navigate to URL. Headless by default. |
| `browser_snapshot()` | Return the page as readable markdown (heuristic: main content, headings, links). The default "read the page" call. |
| `browser_screenshot()` | PNG of the viewport. Returned as image attachment. |
| `browser_click(selector_or_text)` | Click an element. Accepts CSS selector OR visible text. |
| `browser_fill(selector, value)` | Fill an input. |
| `browser_eval(js)` | Run JS in the page context, return stringified result. |
| `browser_close()` | Close the browser. |

For more advanced workflows (multiple tabs, frames, file uploads, downloads, network interception), there are **16 additional tools**:

```
tool_search("browser advanced")
```

→ adds `browser_tabs_*`, `browser_frame_*`, `browser_upload`, `browser_download`, `browser_wait_for`, `browser_press_key`, etc.

## Visible browser — for logged-in flows

Hosted cloud agents can't sign in to your bank, your CRM, or your invoice system — your credentials don't live in their cloud, and you wouldn't paste them there even if they could. Castor sidesteps this: **the agent drives YOUR browser, on YOUR machine, with YOUR existing session.**

```
You:    Show me recent invoices from the billing.example.com admin panel.
        Log in with my credentials and take a screenshot.
Agent:  [browser_set_visible(true)] — Chrome window pops up
        [browser_open url="https://billing.example.com/admin"]
        → page shows the login form
You:    *types login + 2FA in the visible window yourself*
Agent:  [browser_snapshot] — reads the invoice list now that you're in
        [browser_screenshot] — captures the page
        You have 3 open invoices: ...
```

Key idea: **YOU log in, the agent takes it from there.** Passwords and 2FA never touch the agent, the LLM, or any log file.

The visible browser uses Playwright with `headless=False`. All 23 browser tools work against the visible window — switching to visible doesn't change the tool surface, just the visibility.

Toggle visibility anytime:

```
[browser_set_visible(true)]    # next browser_open shows the window
[browser_set_visible(false)]   # next browser_open goes back to headless
```

Visibility state is per-server, not per-thread. If you want a separate persistent browser session per thread, use [presets](PRESET_GUIDE.md) — each preset has its own browser profile.

**Goals get isolated sessions automatically.** When running inside a goal (via the `browser` subagent type), each goal gets its own Playwright `BrowserContext`. A login from subtask 1 carries over to subtask 2..N within the same goal, but parallel goals don't leak cookies or state between each other. See [GOALS.md](GOALS.md).

## `open_url` — handoff to the user's browser

When the agent has nothing to do with the page itself ("show me the docs", "open this OAuth flow"):

```
You:    Open the Castor documentation.
Agent:  [open_url url="https://github.com/deepfounder-ai/castor"]
        → opens in your default browser
```

`open_url` is a **core tool** — no `tool_search` needed. The agent uses it for:

- Browser-based OAuth flows (Google login, GitHub auth, Stripe Connect)
- "Look at this thing for me" (a page, a video, a tweet)
- Hardware-key / WebAuthn flows that can't be automated
- Any time the user needs to act on the page interactively

Rule 16 in the agent's system prompt (`soul.py`) reinforces this for external-wait flows like OAuth: emit the URL via `open_url`, end the turn, resume when the user replies "done".

## When the agent picks which

The agent's `soul.py` has explicit rules:

- **Rule 6** — `browser_open` = headless, `open_url` = show user, `browser_set_visible(true) + browser_*` = interact visibly. The agent knows these are three different things.
- **Rule 11** — for general web search, use **Brave Search** (Google and DuckDuckGo block headless Chromium). The agent has Brave Search wired in by default.
- **Rule 16** — external-wait flows: don't run blocking shell commands waiting for a user-side action. Use `open_url` + end turn + resume on user reply.

You can override by being explicit ("open the page visibly", "scrape this in the background"). The agent reads explicit instructions before falling back to rule defaults.

## Patterns

### Scrape a public site (no login)

```
You:    S&P 500 prices for the last month from investing.com.
Agent:  [tool_search("browser")] [browser_open] [browser_snapshot]
        → S&P closed at 5,847 on 2026-05-10, up 1.2% MoM ...
```

Pure headless — no window appears, no interruption.

### Workflow inside your CRM

```
You:    In the CRM admin add a lead: Anna Ivanova, +7 ..., source "VK Ads".
Agent:  [browser_set_visible(true)]
        [browser_open url="https://crm.example.com/leads/new"]
        → window pops up
        [browser_fill selector="#name"  value="Anna Ivanova"]
        [browser_fill selector="#phone" value="+7 ..."]
        [browser_click text="VK Ads"]
        [browser_click text="Save"]
        → "Lead created" toast visible
        Done.
```

You watch the agent fill the form. You can interrupt if it goes wrong.

### OAuth handoff

```
You:    Connect my Google Calendar.
Agent:  [open_url url="https://accounts.google.com/o/oauth2/auth?..."]
        → opens in your default browser
        Sign in there, grant calendar:read scope, and tell me when you're back.
You:    *signs in*  done
Agent:  [http_request POST /oauth/callback ...]
        Connected. I can now read events from your Google Calendar.
```

The agent doesn't see your password or 2FA — only the OAuth callback URL with the code.

## Configuration

| Setting | Default | What it does |
|---|---|---|
| `browser_headless_default` | `1` | `0` = visible by default. Most users want `1`. |
| `browser_user_data_dir` | `~/.castor/playwright-data` | Where browser session state (cookies, localStorage) persists |
| `browser_viewport_width` | `1280` | Headless viewport size |
| `browser_viewport_height` | `800` | |
| `browser_timeout_ms` | `30000` | Per-operation timeout (page load, click wait) |

Settings → Tools & skills → Browser sub-section exposes these.

## Setup

Playwright + Chromium needs a one-time install:

```bash
# Runs automatically the first time you use a browser tool, but you can pre-install:
playwright install chromium
```

On Linux you may need extra system libs — `playwright install-deps chromium` handles it. Doctor checks for both.

For visible browser on Linux, you need a display server (X / Wayland). On a headless server, the agent will refuse to set `visible=True` and emit a clear "no display available" message.

## Troubleshooting

**`browser_open` hangs** — Playwright is downloading Chromium on first use (~150 MB). Wait 30 seconds. Subsequent calls are instant.

**Google / DuckDuckGo return CAPTCHA** — they block headless Chromium. Use Brave Search (rule 11) or `tool_search("brave")` to activate the Brave search tools.

**Site detects Playwright** — `browser_eval("navigator.webdriver")` returns `true` for default Playwright. Set `browser_stealth=1` in Settings to enable webdriver-flag patching (not bulletproof but defeats most simple checks).

**Visible Chrome doesn't show on Mac** — first time, macOS asks for screen recording permission for the Python interpreter. Approve in System Settings → Privacy → Screen Recording, then restart castor.

**Lost session after restart** — Playwright user data lives in `~/.castor/playwright-data/`. If you `rm -rf ~/.castor`, that's gone. To preserve sessions across re-installs, back up that directory.

## Security notes

- Sessions persist across runs — if you've logged into your bank via the visible browser, that cookie stays in `playwright-data/`. Don't share that directory.
- The headless browser executes arbitrary JS via `browser_eval` — never run on a site where you wouldn't trust agent-emitted JS to execute in your auth context. Especially with `browser_set_visible(true)` since the session is logged in.
- For "the agent visits the public internet and reads stuff", headless is safer than visible — no logged-in session can be accidentally interacted with.

## Cross-links

- [SKILLS.md](SKILLS.md) — the `browser` skill is one of the built-ins
- [MCP.md](MCP.md) — alternative: Playwright MCP server gives finer-grained tools, though castor's built-in works for most cases
- [PRESET_GUIDE.md](PRESET_GUIDE.md) — presets can have their own browser profile, isolated from your default session
