# v0.17.8 — Context Window panel shows real numbers

Before: the big gauge in the right Inspector said "108 / 24k, 0.4%" while the model was actually receiving **3 452** prompt tokens per turn. That's the number that matters — not the thread-history estimate. You couldn't tell from the UI how close you were to actually running out of context.

## 🔧 What changed

The **Context Window** panel is now a real gauge of model capacity.

### New numerator: `prompt_tokens` from the last API call

Pulled from `usage.prompt_tokens` on the OpenAI-compatible response. This is the **actual** size of the prompt that left your machine — system prompt + tool schemas + soul + memory recalls + conversation history + recent tool results, all combined. This is what determines whether the model has room to keep going.

### New denominator: the model's real context window

Priority chain in `/api/status`:

1. **Manual override** — `Settings → Inference → model_context` (new setting, 0 = auto).
2. **Auto-detected** — polled from the active provider:
   - **LM Studio** (`/api/v0/models`) — reads `loaded_context_length` per model.
   - **Ollama** (`/api/show`) — reads `*.context_length` from modelinfo.
3. **`ollama_num_ctx`** fallback when provider is Ollama.
4. **Unknown** — panel shows `?` with a tooltip pointing to the setting.

Cached for 60s per (provider, model) to avoid hitting the provider every render.

### Colour coding

| % of context | Colour |
|---|---|
| <50% | green |
| 50–75% | accent |
| 75–90% | warn |
| ≥90% | error |

### Mini-stats reshuffled

The bottom cards used to show `INPUT` + `OUTPUT`. `INPUT` was redundant with the new top gauge, so it's gone. Cards now show:

| OUTPUT | TOK/SEC | HISTORY |
|---|---|---|
| completion tokens last turn | decode speed | thread history estimate (for reference) |

Auto-sizing grid (1-3 cells via CSS `auto-fit`).

## 📦 Upgrade

```bash
git pull && pip install -e . --upgrade
# Restart the server
```

If you see `?` in the gauge: your provider didn't report context length. Go to **Settings → Inference → model_context** and punch in the value manually (e.g. 32768 for most Qwen/Llama finetunes, 131072 for Gemma 3 27B IT, etc.).

🤖 Generated with [Claude Code](https://claude.com/claude-code)
