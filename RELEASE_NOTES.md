# v0.17.10 — Context gauge denominator is `context_budget`, not model's raw context

v0.17.8 made the Context Window gauge show `prompt_tokens / model_context`. That was wrong — I'd forgotten that `context_budget` (the agent-side setting, default 24k) is the **actual** ceiling. Compaction and tool-result truncation both trigger on `context_budget`, not on the model's native context length. So if your model supports 128k but `context_budget` is 24k, the agent will compact at ~24k regardless — the 128k was never the limit.

## 🔧 What changed

### The gauge now measures what actually matters

**Numerator**: `lt.prompt_tokens` (last API call's `usage.prompt_tokens`) — unchanged.

**Denominator**: `context_budget` (the setting that triggers compaction in `agent._maybe_compact()` and tool-result summarization/truncation in `agent_loop.run_loop`). This is what determines when the agent starts shedding load.

**Secondary reference**: `model_context` (auto-detected from LM Studio `/api/v0/models` or Ollama `/api/show`, override via setting). Shown as `model 32k` next to the gauge so you can see whether your budget is within what the model can actually accept.

### Misconfiguration warning

If `context_budget > model_context` (e.g. budget=32k but the model only accepts 16k), the panel now shows a red warning bar:

> ⚠ context_budget (32k) exceeds the model's actual context (16k). Lower context_budget in Settings → Model to avoid provider errors.

This used to silently fail at the provider — now you see it before sending.

### `/api/status`

Now returns both:
- `context_budget` — agent-side effective ceiling
- `model_context` — model's native capacity (or 0 if unknown)
- `model_context_source` — `override` / `detected` / `unknown`

## Why the two numbers exist

| Setting | What it does | Default |
|---|---|---|
| `context_budget` | Agent-side. When the total conversation hits this, compaction summarizes old messages into memory. Tool outputs get summarized/truncated when they would push the prompt past this. | 24 000 |
| `model_context` | Model-side hard cap. Going past this = provider HTTP error. | auto-detect, or set manually (0 = auto) |

The agent keeps total prompt ≤ `context_budget`, so `model_context` is mostly informational — useful only for catching misconfigurations where someone set `context_budget` higher than the model supports.

## 📦 Upgrade

```bash
git pull && pip install -e . --upgrade
# Restart the server
```

If you want to raise the practical ceiling: **Settings → Model → `context_budget`** (keep it ≤ `model_context`). If `model_context` shows `?`, either your provider doesn't report it or set `model_context` manually.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
