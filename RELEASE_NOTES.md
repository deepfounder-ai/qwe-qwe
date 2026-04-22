# v0.17.17 — Provider picker: key input modal + visual cues

User clicked the `openrouter` provider card in **Settings → Model** and nothing happened — no way to enter an API key. The picker was silently switching the active provider, model discovery was failing (no key), and the UI gave no surface to recover.

## 🔧 Fixes

### 1. "NEEDS KEY" badge on cloud providers without a saved key

Provider cards now show a small amber chip in the top-right corner when `!has_key && !local`. You can see at a glance which providers are ready to use vs. which need credentials:

```
┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│ 🟢 lmstudio   │    │ ollama        │    │ openrouter   │
│ localhost:1234│    │ localhost:11434│    │ openrouter.ai│
│              │    │              │    │    NEEDS KEY │
└──────────────┘    └──────────────┘    └──────────────┘
   (online)           (no ping)         (amber badge)
```

Local providers also show a green dot when the health ping succeeds.

### 2. Clicking a "NEEDS KEY" card opens a key-input modal

Instead of silently switching (and failing), the card click checks `data-needs-key` and routes to a new `openProviderKeyModal(name)` that:

- Pre-fills the base URL from the provider preset (e.g. `https://openrouter.ai/api/v1` for openrouter) — read-only-looking but editable if you use a proxy.
- Password-masked key input with `autofocus`.
- **Built-in hints** pointing to the right key-management page for common providers:
  - `openai` → platform.openai.com/api-keys
  - `openrouter` → openrouter.ai/keys
  - `groq` → console.groq.com/keys
  - `anthropic` → console.anthropic.com/settings/keys
  - `together` → api.together.xyz/settings/api-keys
  - `deepseek` → platform.deepseek.com/api_keys
  - `mistral` → console.mistral.ai/api-keys

On **Save + switch**:

1. `POST /api/provider` — persists the config (same endpoint used by "add custom provider")
2. `POST /api/model {provider: name}` — switches active provider
3. Refreshes `/api/status` + `/api/providers` so the card loses the amber badge and becomes active.

If either step fails, the modal stays open so you can fix and retry.

### 3. Once a key is saved, next click switches directly

No modal the second time — the server reports `has_key: true` on that provider, so the click goes straight through the normal switch path.

## 📦 Upgrade

```bash
git pull && pip install -e . --upgrade
# Restart the server
```

Open **Settings → Model** and the cloud providers you haven't configured will show an amber **NEEDS KEY** chip. Click one and the modal appears.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
