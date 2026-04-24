# v0.17.33 — Issue cleanup: more providers + browser-skill tests

Housekeeping release — two good-first-issues from the backlog shipped.

## 🔌 More LLM providers (closes #6)

Three hosted OpenAI-compatible providers the core team doesn't self-host but users ask for often:

- **Perplexity** (`https://api.perplexity.ai`) — sonar / sonar-pro / sonar-reasoning. Built-in web search returns grounded answers with citations; unique value vs pure-LLM providers. `response_format` not officially supported — ask-for-JSON is the workaround.
- **Cerebras** (`https://api.cerebras.ai/v1`) — llama-3.3-70b / qwen-3-32b. Fastest hosted inference on the market (~2000 tok/s on Llama-3). Good catalogue is smaller than OpenRouter but what's there is blisteringly fast.
- **Mistral** (`https://api.mistral.ai/v1`) — mistral-large / codestral / ministral-8b. European host (EU data residency), codestral is a code specialist.

Each picks up a `CAPABILITIES` row so the structured-output cache in `agent.py` doesn't silently disable what works. Provider-key modal in Settings → Model grows URL hints for the two that were missing them (mistral already had one).

## 🧪 Browser-skill tests (closes #5)

`skills/browser.py` was 300 statements at 0% coverage because every test path either tried to `import playwright` or launch Chromium. New `tests/test_browser_skill.py` sidesteps both: `_ensure_browser` / `_close_browser` stubbed to no-op, a `MagicMock` `_page` injected, and each test asserts the right Playwright method was invoked with the right args for a given tool name.

16 tests, no real browser, no Playwright install required. Covers:

- `browser_open` — URL auto-prefix, response shape (`Title: ... URL: ... <text>`)
- `browser_back` / `forward` / `reload` — history nav
- `browser_snapshot` — `inner_text`, selector honoured, 4000-char cap on body
- `browser_click` / `browser_fill` — dispatch to Playwright with selector + value
- `browser_eval` — expression passthrough + result serialisation
- `browser_set_visible` — mode flip restarts the browser
- `browser_close`
- Hallucinated-tool aliases redirect: `open_url` / `navigate` / `browse` → `browser_open`
- `google_search` → Brave search rewrite (Google blocks headless)
- Unknown tool name → clean error string, no exception

## Test count

Suite: 315 → **333 passing**. +16 browser-skill, +2 provider-preset.

## Upgrade

```bash
pip install --upgrade qwe-qwe   # or re-run ./setup.sh
```

No migrations, no config changes. The new provider presets appear in Settings → Model → Provider on next restart.
