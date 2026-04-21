# v0.17.2 — MCP hardened + systemic

MCP (Model Context Protocol) module audited, hardened, and promoted to a first-class subsystem. Live-tested end-to-end with the official `@modelcontextprotocol/server-memory` — 9 tools discovered, handshake clean, `tools/call` roundtrip verified.

## 🔧 `mcp_client.py` hardening

- **stderr drain thread** — stdio servers' stderr used to fill buffers silently and hang the subprocess. Now a daemon reader drains stderr into a rolling tail (last 50 lines) and surfaces warnings to the main log.
- **RPC read timeout** — `readline()` no longer blocks forever when a server dies mid-request. 30-s timeout via helper thread (cross-platform — no `select()` on Windows pipes).
- **Dead-process detection** — empty stdout → explicit `ConnectionError` with stderr tail attached for diagnostics.
- **Broken-pipe handling** on stdin write → clean `ConnectionError` instead of silent hang.
- **Circuit breaker** — 3 consecutive RPC failures → server marked disconnected, subsequent `call_tool` fail-fast with a clear message instead of retrying forever.
- **Unbuffered stdio** (`bufsize=0`) — critical for line-delimited JSON-RPC.
- **Handshake version** — uses `config.VERSION` dynamically (was hardcoded `0.6.0`).
- **`FileNotFoundError` surfaced** — if `command` isn't on PATH, error message says so immediately.

## 🎛️ Systemic integration

- **Config validation** — `add_server()` rejects stdio without command or http without valid URL.
- **`list_servers()` enriched** — returns `error_streak`, `stderr_tail`, `last_used`, env-key names (not values), full tool list per server.
- **New endpoint**: `GET /api/mcp/health` — fast summary (configured / running / tripped / tools_total) for UI polling.
- **New endpoint**: `GET /api/mcp/presets` — 7 built-in server presets for one-click install.

## 🎨 UI — MCP tab polished

- **Stats strip**: RUNNING (N/M) · TOOLS · TRIPPED
- **Rich server cards**: status badge (running / stopped / broken / disabled), command line, env-var names (no values!), tool chips (first 8 + overflow count)
- **Error surface**: when `s.error` is set, red box with message + last 5 stderr lines inline
- **Add-server modal with presets**: filesystem · github · brave-search · memory · puppeteer · sqlite · fetch. Click a preset → prefills the form. Manual override always available.
- **Refresh button** in the header

## 🐛 Chat fix (bonus)

- **Stop button stayed after turn ended** — on `reply` event the surgical post-turn refresh rebuilt Inspector and Topbar but not the Composer, so the Send↔Stop swap never fired. Composer right-side is now re-rendered in place after every turn, with textarea contents + caret position preserved if the user started typing during the turn.

## 🧪 Live test

```python
mcp_client.add_server('mcp-test-memory', transport='stdio', command='npx.cmd',
    args=['-y', '@modelcontextprotocol/server-memory'])
mcp_client.start_server('mcp-test-memory')
# → "MCP 'mcp-test-memory' connected (9 tools)"

mcp_client.execute_mcp_tool('mcp__mcp-test-memory__create_entities', {})
# → roundtrip confirmed (server returned validation error for empty args)
```

## 📦 Upgrade

```bash
git pull && pip install -e . --upgrade
```

🤖 Generated with [Claude Code](https://claude.com/claude-code)
