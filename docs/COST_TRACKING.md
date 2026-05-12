# Cost tracking

qwe-qwe records token counts and estimated cost for every LLM call made during
a session. The data lives entirely on your machine — nothing is sent anywhere
except a one-time pricing JSON fetch from a public URL (no session data is
included in that request).

---

## What gets tracked

Every site that calls an LLM through the agent runtime writes one row:

| Call site | `source` value |
|---|---|
| Main agent loop (user turn) | `cli` / `web` / `telegram` / `scheduler` |
| Synthesis night worker | `synthesis` |
| Skill creator pipeline | `skill_creator` |
| Scheduled routine firings | `scheduler` |

Not yet tracked: STT, TTS, and embedding calls (different cost models — planned
for a future release).

---

## Where the data lives

SQLite database at `~/.qwe-qwe/qwe_qwe.db` (override via `QWE_DATA_DIR`),
table `agent_runs`. Added by migration `008_agent_runs.sql`.

Each row contains:

| Column | Type | Description |
|---|---|---|
| `id` | TEXT | UUID |
| `thread_id` | TEXT | Chat thread this run belongs to |
| `source` | TEXT | `web` / `cli` / `telegram` / `scheduler` / `synthesis` / `skill_creator` |
| `started_at` | REAL | Unix timestamp (seconds) |
| `finished_at` | REAL | Unix timestamp (seconds) |
| `model` | TEXT | Model id string |
| `provider` | TEXT | Provider name (lmstudio / ollama / openai / …) |
| `input_tokens` | INTEGER | Prompt tokens reported by the provider |
| `output_tokens` | INTEGER | Completion tokens reported by the provider |
| `cost_usd` | REAL | Estimated cost in USD (0 when pricing unavailable) |
| `status` | TEXT | `ok` / `err` / `aborted` |

---

## Where to view it

**Web UI:**

- **Sessions list** — each thread row shows a token chip and a cost chip.
- **Per-thread drilldown** — click any thread row to open a modal with a
  timeline of every run: model, source, status, duration, tokens, and
  estimated cost per call.
- **Topline widget** — 30-day aggregate totals (input + output tokens,
  total cost) shown above the sessions list.
- **Routines page** — Cost (30d) column so you can spot expensive scheduled
  jobs at a glance.

**Settings → Cost tracking** — pricing URL, auto-update toggle, and a manual
refresh button for the local pricing cache.

**Direct SQL:**

```sql
SELECT thread_id, model, input_tokens, output_tokens, cost_usd, status
FROM agent_runs
ORDER BY started_at DESC
LIMIT 50;
```

**REST API:**

```
GET /api/threads                      -- includes input_tokens, output_tokens,
                                         cost_usd, run_count per thread
GET /api/threads/{id}/runs            -- all runs for a thread (paginated)
GET /api/analytics/period?days=30     -- aggregate totals for a time window
GET /api/pricing/status               -- cache age, model count, source URL
POST /api/pricing/refresh             -- force a fresh pricing fetch
```

---

## How pricing is sourced

1. **LiteLLM community JSON** — fetched from
   `https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json`
   and cached locally at `~/.qwe-qwe/pricing_cache.json`. The URL is
   configurable via the `pricing_url` setting.
2. **Bundled fallback** — a hardcoded table covering the top-10 most common
   models ships with qwe-qwe and is used when the cache is absent (first run
   offline) or stale beyond the configured TTL.
3. **Per-model override** — you can pin exact prices for any model (useful for
   enterprise agreements or self-hosted models with a known cost).

No request body or session data is included in the pricing fetch — it is a
plain `GET` of a static JSON file.

---

## Air-gapped / self-hosted mirror setup

Point the agent at an internal mirror serving the same LiteLLM JSON schema:

1. Host a copy of the LiteLLM pricing JSON on your internal server.
2. In Settings → Cost tracking, set **Pricing URL** to your mirror URL, or
   set it programmatically:

```python
import config
config.set("pricing_url", "https://mirror.corp.example/litellm-pricing.json")
```

The agent will fetch from that URL on the next refresh cycle.

---

## Adding a per-model price override

Useful when your contract rate differs from the public LiteLLM prices, or for
local models where you want to track GPU cost:

**Via Python:**

```python
import db, json

db.kv_set(
    "pricing_override_my-custom-model",
    json.dumps({"input": 1e-6, "output": 2e-6})   # cost per token in USD
)
```

**Via Settings UI:** Settings → Cost tracking → Per-model overrides → Add.

The `input` and `output` values are cost **per token** in USD (not per
thousand tokens). For example, a model priced at $1 / million input tokens
and $2 / million output tokens is `{"input": 0.000001, "output": 0.000002}`.

---

## Privacy

No cost data or token counts leave your machine. The only outbound request is
the pricing JSON `GET` (a public static file — no authentication, no query
parameters, no body).

For the full data inventory see `docs/PRIVACY.md`.
