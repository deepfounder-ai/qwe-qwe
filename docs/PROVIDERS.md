# LLM providers

castor talks to any **OpenAI-compatible** chat-completions endpoint. Pick the one that fits — local for data-on-prem, cloud for speed / capability — and switch between them per-thread without restarting.

## Supported out of the box

| Provider | Type | Where to set up | Free tier? |
|---|---|---|---|
| **LM Studio** | Local | [lmstudio.ai](https://lmstudio.ai) — auto-detected at `http://localhost:1234/v1` | All local |
| **Ollama** | Local | [ollama.ai](https://ollama.ai) — `ollama serve` then `ollama pull qwen2.5:7b` | All local |
| **OpenAI** | Cloud | [platform.openai.com/api-keys](https://platform.openai.com/api-keys) | Trial credit |
| **OpenRouter** | Cloud | [openrouter.ai/keys](https://openrouter.ai/keys) — multi-model gateway | Free tier with rate limits |
| **Groq** | Cloud | [console.groq.com/keys](https://console.groq.com/keys) | Generous free tier — fast |
| **Together** | Cloud | [api.together.xyz/settings/api-keys](https://api.together.xyz/settings/api-keys) | Trial credit |
| **DeepSeek** | Cloud | [platform.deepseek.com/api_keys](https://platform.deepseek.com/api_keys) | Pay-as-you-go, cheap |
| **Perplexity** | Cloud | [perplexity.ai/settings/api](https://perplexity.ai/settings/api) | Pay-as-you-go |
| **Cerebras** | Cloud | [cloud.cerebras.ai](https://cloud.cerebras.ai) — extremely fast | Free tier available |
| **Mistral** | Cloud | [console.mistral.ai/api-keys](https://console.mistral.ai/api-keys) | Trial credit |
| **Anthropic** | Cloud | [console.anthropic.com/settings/keys](https://console.anthropic.com/settings/keys) | Pay-as-you-go |

**Anthropic uses a native adapter** — not the OpenAI compatibility shim. This enables Anthropic-specific features: prompt caching (up to 90% cost reduction on cached input tokens), extended thinking budgets (for Claude Sonnet 4.6+ / Opus 4), and structured tool-use error handling. Set `CASTOR_LLM_KEY` to your Anthropic API key and select `anthropic` as provider — routing is automatic.

Any other OpenAI-compatible provider (Azure OpenAI, AWS Bedrock with the OpenAI shim, vLLM, llama.cpp's server, etc.) works the same way — set `CASTOR_LLM_URL` to its base URL.

## Picking a model

Tool-calling is the hard requirement — castor relies on `tool_calls` in the response, so **the model must support OpenAI-format function calling**.

| Use case | Suggested model | Provider |
|---|---|---|
| **First install / try it out** | Qwen 2.5 7B Instruct | LM Studio / Ollama (local, 8GB VRAM) |
| **Local on a laptop** | Gemma 2 4B / Qwen 2.5 3B | Ollama |
| **Local on a workstation** | Qwen 2.5 14B / Llama 3.1 8B | LM Studio |
| **Fast cloud (free)** | Llama 3.1 70B / Mixtral 8x7B | Groq |
| **Best quality cloud** | GPT-4o / Claude Sonnet 4.6 | OpenAI / Anthropic |
| **Cheapest cloud** | DeepSeek V3 | DeepSeek |
| **Hardest reasoning** | DeepSeek R1 / o1 | DeepSeek / OpenAI |

For local models, **4-bit quantization** (Q4_K_M or Q4_0) is the sweet spot — fits on a single consumer GPU with negligible quality loss for tool calling.

## Configuration

### Quick: environment variables

```bash
# Local — LM Studio defaults
export CASTOR_LLM_URL=http://localhost:1234/v1
export CASTOR_LLM_MODEL=qwen/qwen2.5-7b-instruct
export CASTOR_LLM_KEY=lm-studio

# Cloud — example: Groq free tier
export CASTOR_LLM_URL=https://api.groq.com/openai/v1
export CASTOR_LLM_MODEL=llama-3.3-70b-versatile
export CASTOR_LLM_KEY=gsk_...
```

Put these in your shell rc or a `.env` file at the repo root. Restart the agent for changes to take effect.

### Web UI: Settings → Model

Settings → **Model** has a provider picker that lists every provider castor knows about. Providers marked with a yellow **NEEDS KEY** badge open a modal with:

- A link to where to get the key for that provider
- The endpoint URL (pre-filled)
- A model dropdown (auto-fetched from the provider's `/models` once the key is valid)

The picker has a per-provider memory — switch providers, and the next time you switch back you don't have to re-enter the key.

### CLI / Telegram: `/model`

```
/model                          # show current
/model openai                   # switch provider (uses default model)
/model openai gpt-4o            # switch provider + model
/model groq llama-3.3-70b-versatile
```

`/model` lists everything available; if a provider needs a key, you'll get a hint about which env var or settings page to use.

## Per-thread provider switching

Each thread remembers the provider + model it last used. Open thread A, set Groq + Llama-3.3-70b → it sticks. Switch to thread B, set local LM Studio → that sticks too. Switching threads doesn't change provider on the active thread.

Why this matters: keep a **fast cheap model** for casual chat threads and a **stronger expensive model** for code-heavy threads. Or run on-prem for client-data threads and cloud for everything else.

## Context window

castor **detects** the model's real context window automatically:

- **LM Studio**: hits `/api/v0/models` to read the loaded model's `loaded_context_length`
- **Ollama**: hits `/api/show` to read `model_info.context_length` then `num_ctx`
- **Cloud providers**: hard-coded table of known limits

The Web UI Inspector shows two numbers in the Context Window gauge:

- **`prompt_tokens`** — what the agent is actually about to send
- **`context_budget`** — the agent-side cap (default 24 000 tokens, configurable in Settings → Advanced)

The agent compacts (summarises older messages) when prompt_tokens approaches context_budget — NOT when it approaches the model's actual context. This is intentional: leaves headroom for tool outputs, prevents the LLM from re-summarising your conversation every turn.

If your model has a 128k window but you want to cap how much castor sends per turn, lower `context_budget` in Settings. Want the agent to use everything? Raise it.

## Provider health

`castor --doctor` includes a provider check — verifies the endpoint responds, the model is loaded (local providers), and a token-counting request works.

In the Web UI, **Settings → Model → Test connection** runs the same check with a click. If the local provider is down, you'll see a clear `connection refused` instead of a mysterious failure mid-turn.

## Common gotchas

**LM Studio: "model not loaded"** — open LM Studio, go to the chat tab, click any model to load it. The model needs to be ready BEFORE the agent's first turn.

**Ollama: "model not found"** — `ollama pull <name>` first. castor won't auto-pull; we don't want a 9GB download starting silently mid-conversation.

**Groq rate limits** — free tier is generous but does throttle. Heavy tool-using sessions on the 70B model can hit 30 RPM. Drop to the 8B model or move to a different provider for tool-heavy threads.

**Cloud provider blocks tool_calls** — some smaller free-tier models don't support function calling. castor will detect malformed responses and try to recover, but switch to a tool-capable model if you see consistent failures.

**Model is too small for tools** — anything under ~3B parameters tends to forget tool definitions mid-turn. 7B is a hard floor for reliable agent loops; bigger is better.

## Adding a new provider preset

If you want a provider that isn't pre-listed:

1. **Simple route — just use it.** Set `CASTOR_LLM_URL` / `CASTOR_LLM_KEY` / `CASTOR_LLM_MODEL` to whatever you want. The agent doesn't care; it talks OpenAI-format.

2. **Add a preset** so it shows up in the picker. Edit `providers.py::PRESETS` — typically a one-line addition:

```python
"my-provider": ProviderPreset(
    name="My Provider",
    url="https://api.my-provider.com/v1",
    needs_key=True,
    key_help_url="https://my-provider.com/keys",
    default_model="some-model",
),
```

Open a PR — providers are small additions and we ship them.

## Cost

For **local providers**, cost is just your electricity bill.

For **cloud providers**, the agent does NOT meter anything at the castor level — that's between you and the provider. Some practical observations:

- **Tool search** keeps the system prompt small (~750 tokens vs ~3000 if every tool were loaded). Real savings on per-message cost.
- **Tool result clearing** (after 3 cleared tools' worth of history) prevents long tool-heavy threads from re-shipping the same outputs every turn.
- **Compaction** kicks in around 24k tokens, summarising older turns. Caps the per-turn cost.

In a normal tool-using day, expect 5-50 LLM calls per concrete task. A `tool_search → 2-3 tool calls → reply` pattern is typical; bigger workflows (build a thing, debug a thing) can be 50+ tool calls.

## Cross-links

- [VOICE.md](VOICE.md) — Live Voice Mode uses STT/TTS providers in addition to the chat LLM
- [PRIVACY.md](PRIVACY.md) — what data goes to which provider, telemetry contract
