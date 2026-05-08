# Privacy & Telemetry

qwe-qwe is **self-hosted**. Your chat history, memory, soul / personality, files, secrets, and LLM credentials all live on your machine and never leave it without your explicit action.

This document covers the **one** optional exception: anonymous usage telemetry, which is **off by default** and only activates after you explicitly opt in.

## What stays local, always

These are never sent off-machine, regardless of any setting:

- **Chat content** — every message you type, every assistant reply, every thinking block. Lives in `~/.qwe-qwe/qwe_qwe.db` only.
- **Memory** — semantic memory entries (`tag=user`, `fact`, `experience`, `wiki`, `entity`), atomic facts saved via the 📖 button, knowledge-base file content. Lives in `~/.qwe-qwe/memory/` (Qdrant) and `~/.qwe-qwe/memories/atoms/` (markdown).
- **Soul / personality** — name, language, traits (built-in + custom), low/high descriptions. Lives in the `kv` table.
- **Threads / folders / preset state** — names, organization, custom data.
- **Files** — anything you've drag-dropped, indexed, or attached.
- **Secrets** — API keys, tokens, anything in the encrypted vault. Lives in `~/.qwe-qwe/vault.enc`.
- **LLM credentials** — `QWE_LLM_URL`, `QWE_LLM_KEY`, provider URLs, exact model names.
- **Telegram bot token, conversation history, group lists.**
- **Identity** — IP address, hostname, username, machine id.

## Telemetry — opt-in only

If you opt in (Settings → Privacy → Telemetry, OR by answering "yes" to the first-run prompt), qwe-qwe will collect a small set of operational metrics that help the project understand how the agent is being used and what to fix.

### What gets collected

Every event is declared in [`telemetry.py::ALLOWED_EVENTS`](../telemetry.py) — that's the source of truth, and it's a closed whitelist. Code can't send anything outside this list. Right now there are **6 event types**:

#### `session_start`

Fires once per qwe-qwe process start.

| Field | Example | Why |
|---|---|---|
| `qwe_version` | `"0.18.4"` | Distribute fixes across the version skew. |
| `python_version` | `"3.12.10"` | Catch Python-version-specific bugs early. |
| `os` | `"linux"` / `"macos"` / `"windows"` | Same. |
| `provider_kind` | `"lmstudio"` / `"openai"` / `"azure"` / etc | Which providers are actually used. **Never the URL** — could be an internal corporate endpoint. |
| `model_size_bucket` | `"small"` / `"medium"` / `"large"` / `"unknown"` | Coarse model size (≤4B / 4-13B / >13B). **Never the model id** — could be a custom finetune that uniquely identifies you. |
| `has_web_ui`, `has_telegram`, `has_voice`, `has_camera`, `has_scheduler`, `has_mcp` | booleans | Which interfaces are configured at all. |
| `active_skills_count` | `5` | Count only — never the names. User-created skills could be `acme_corp_invoicing` which would deanonymize. |
| `scheduled_jobs_count` | `3` | Count only. |
| `indexed_sources_count` | `42` | How big the knowledge base is. |

#### `turn_complete`

Fires when an agent turn finishes (LLM stopped, tool calls done, reply rendered).

| Field | Example | Why |
|---|---|---|
| `duration_ms` | `4200` | Latency distribution per provider / model. |
| `rounds` | `3` | Tool-loop depth — proxy for task complexity. |
| `tool_categories_used` | `["memory", "files", "browser"]` | **Categories only**, from a fixed enum (memory, files, shell, http, browser, vision, voice, automation, skills, orchestration, vault, rag, other). **Never specific tool names** — wouldn't leak user-created skill names. |
| `tool_calls_count` | `5` | How tool-heavy turns are. |
| `tool_errors_count` | `0` | Reliability proxy. |
| `input_tokens` | `1850` | Cost / context-window pressure tracking. |
| `output_tokens` | `420` | Same. |
| `context_hits` | `3` | Memory recall effectiveness. |
| `source` | `"web"` / `"cli"` / `"telegram"` / `"scheduler"` | Which surface the turn came from. |

#### `thread_created`

Fires every time a new chat thread is created. Lets us see how often users start fresh conversations vs continue existing ones, and whether the trigger was a person or a system surface.

| Field | Example | Why |
|---|---|---|
| `source` | One of: `web` / `cli` / `telegram` / `scheduler` / `preset` / `other` | Which surface initiated the thread creation. Closed enum. **Never the thread name, id, or any meta** — those could carry user-typed content like `"acme-Q3-launch"` and deanonymize. |

#### `tool_error`

Fires when a tool call fails (exception, timeout, validation).

| Field | Example | Why |
|---|---|---|
| `tool_category` | `"shell"` | **Category only** (same enum as above). Never the tool name, args, or error message — those could contain anything you typed or any path on your system. |
| `error_kind` | `"timeout"` / `"exception"` / `"validation_failed"` / `"rate_limited"` / `"aborted"` / `"blocked"` | Categorical error class. |

#### `skill_creator_pipeline`

Fires when the skill-generator pipeline finishes (succeed or fail).

| Field | Example | Why |
|---|---|---|
| `outcome` | `"success"` / `"syntax_error"` / `"smoke_fail"` / `"validate_fail"` / `"max_attempts_exhausted"` / `"aborted"` | Whether code-generation worked. |
| `attempts` | `2` | How many retries before success. |
| `duration_ms` | `190000` | Pipeline runtime. |
| `tools_count` | `3` | How many tools the generated skill exposes. **Not their names.** |

#### `feature_first_use`

Fires the first time per session that a major feature is exercised. Lets us see what's actually used vs sitting unused.

| Field | Example | Why |
|---|---|---|
| `feature` | One of: `camera_capture` / `live_voice` / `telegram_send` / `scheduler_create` / `skill_create` / `browser_visible` / `mcp_add` / `preset_activate` / `knowledge_index_url` / `knowledge_index_file` | Which feature was first-touched. Closed enum. |

### Common metadata on every event

Every event is wrapped with:

- `anonymous_id` — random UUID generated once at opt-in, persisted in your local `kv` table. Not derived from any PII. You can rotate it any time without disabling telemetry (Settings → Privacy → Reset anonymous ID).
- `session_id` — random UUID regenerated each time qwe-qwe starts. Lets the receiver group events from one run without remembering anything across runs.
- `ts` — UNIX timestamp.

### What's deliberately NOT collected

- **No chat content.** No user input, no assistant replies, no thinking blocks, no thread titles.
- **No soul / personality.** Trait names, levels, custom traits — none of it.
- **No memory content.** Memory entries, knowledge-base text, RAG search queries, recall results.
- **No file paths or filenames.** Could leak project names.
- **No tool-call args or results.** Could contain anything you typed.
- **No exact model name or provider URL.** Could deanonymize via custom finetunes or internal endpoints.
- **No specific skill names** (custom or built-in). User-created skills could be `acme_corp_*` which deanonymizes.
- **No IP / hostname / username / machine id.**
- **No API keys or secrets.**
- **No Telegram bot token, conversation history, group ids.**

The whitelist in `telemetry.py::ALLOWED_EVENTS` is **type-strict** — every prop has a declared Python type, and string-valued props that could carry free text instead use a closed enum (e.g. `provider_kind`, `tool_category`, `error_kind`). A future refactor that accidentally added a string field couldn't sneak chat content past the validator without explicitly editing the whitelist.

### Where the data goes

Telemetry is **off by default** — you must explicitly opt in via the first-run prompt or Settings → Privacy → Telemetry.

If you opt in, events go to the project-operated Countly instance at **`https://qwelytics.deepfounder.ai/i`**, run by deepfounder.ai (the qwe-qwe project maintainer). It's a self-hosted Countly Community Edition — same code you could run yourself, same privacy guarantees on the wire, same data inventory documented in this file.

The end-user UI offers exactly **two choices**: enable or disable. The destination is fixed — the project decides where its own telemetry lands, you decide whether to participate. This is by design: a buffet of "alternative endpoints" surfaces in the UI would dilute the privacy signal and complicate the trust model.

When the project changes either the default destination or the schema of `ALLOWED_EVENTS`, the **`telemetry_consent_version`** bumps. Users who opted in under the old policy see a "policy updated, please re-confirm" banner in Settings → Privacy before any new event is sent — you always see the new policy before it takes effect.

**For operators / forks**: if you're packaging qwe-qwe for a different deployment and want telemetry to go elsewhere (your own Countly, a custom collector, or nowhere), edit the project defaults in `config.py`:

- `telemetry_endpoint` — POST destination URL
- `telemetry_format` — `"countly"` or `"raw"`
- `telemetry_countly_app_key` — Countly app key (when format=countly)

The `format=raw` path POSTs `{"events": [...]}` to whatever URL you set — useful for custom HTTP collectors. The `format=countly` path is the default Countly transformer with `device_id` = anonymous_id (cross-day per-user tracking works).

These are project-level decisions, not user-level — there's no UI surface to override them from inside qwe-qwe itself. Hidden in plain code, easy to fork.

#### What Countly receives on the wire

[Countly](https://count.ly) Community Edition is open-source, self-hostable product analytics. The qwe-qwe project runs an instance at `qwelytics.deepfounder.ai` for opt-in telemetry. End-users don't configure anything — the project's defaults are baked in.

Per opted-in user, Countly receives:
- App key (the project's public Countly app key — not a secret, equivalent to a Sentry/PostHog public DSN)
- `device_id` = our random `anonymous_id` (UUID, not derived from any PII; rotatable any time via Settings → Privacy → Reset; wipe-able via Forget Me)
- Timestamp
- Events array, each with:
  - `key` = event name (one of the 5 ALLOWED_EVENTS)
  - `count` = 1
  - `dur` (seconds) — for events with `duration_ms` prop, lets Countly compute event-duration averages natively
  - `segmentation` = props as flat string/number/bool (lists become CSV strings)
- User-Agent: `qwe-qwe/<version>`

The `device_id` is stable across days **by your explicit opt-in consent** — that's what makes retention / funnel metrics work. Reset rotates it, Forget Me wipes it.

### Controls

In Settings → Privacy → Telemetry you have:

- **Enable / disable** toggle. Default OFF. Disabling drops the queue.
- **Reset anonymous ID** — generates a fresh UUID without changing the enabled flag. Lets you "start over" without re-opting in.
- **Forget me** — disables telemetry, drops the queue, AND wipes the anonymous ID. Stronger than disable: any future re-opt-in gets a fresh id, so nothing ties periods of opt-in together.
- **View pending events** — see exactly what's queued before any send.
- **Send now** — manually flush the queue.

### Audit trail

All collection goes through `telemetry.track_event()`. To audit, run:

```bash
grep -rn "telemetry.track_event" .
```

Every call site is documented and bounded by the whitelist. There is no other path into the queue.

### Consent versioning

The `telemetry_consent_version` setting tracks which version of this policy you've agreed to. When `ALLOWED_EVENTS` changes shape (new event types added, schemas widened), the policy version bumps and the next session shows the consent prompt again — defaulted to your previous choice but giving you a chance to re-decide.

## Questions or concerns

Open an issue at [github.com/deepfounder-ai/qwe-qwe/issues](https://github.com/deepfounder-ai/qwe-qwe/issues) tagged `privacy`. Privacy regressions or surprises are the highest-priority bug class in this project.
