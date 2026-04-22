# Architecture

## Overview

qwe-qwe is a **local-first, single-process Python agent** tuned for small
(3B–14B) OpenAI-compatible models on LM Studio / Ollama / llama.cpp. One
process hosts the CLI, the FastAPI web server, the Telegram bot, the agent
loop, the tool dispatcher, and the vector store. SQLite and Qdrant
(embedded disk mode) hold all state — no external services, no GPU
required.

## System diagram

```
 ┌────────┐   ┌──────────┐   ┌──────────┐
 │  CLI   │   │ Web UI   │   │ Telegram │
 └───┬────┘   └────┬─────┘   └────┬─────┘
     │             │ WS/HTTP      │ long-poll
     ▼             ▼              ▼
   ┌─────────────────────────────────────┐
   │   server.py  (FastAPI + WebSocket)  │  auth, rate-limit, file uploads
   └───────────────────┬─────────────────┘
                       │ builds TurnContext
                       ▼
   ┌─────────────────────────────────────┐
   │ agent.run → _build_messages         │  history + auto-context recall
   │          → agent_loop.run_loop      │  stream, tool dispatch, abort
   └────┬───────────────────────┬────────┘
        │ tool calls            │ LLM HTTP (stream)
        ▼                       ▼
   ┌─────────┐          ┌───────────────┐
   │tools.py │          │ providers.py  │ → LM Studio / Ollama / OpenAI /
   │execute()│          │ (7 providers) │   Groq / Together / custom
   └────┬────┘          └───────────────┘
        │
        ├──► memory.py  ──► Qdrant (~/.qwe-qwe/memory/)  + SQLite FTS5
        ├──► rag.py     ──► markitdown / yt-dlp → memory (tag=knowledge)
        ├──► db.py      ──► SQLite (~/.qwe-qwe/qwe_qwe.db)
        ├──► skills/    ──► plugin tools (browser, notes, timer, …)
        └──► mcp_client ──► MCP servers (subprocess, stdio)
```

State lives in SQLite (chat history, threads, scheduled tasks, FTS5
indexes), Qdrant (dense + sparse vectors) and a few loose directories
under `~/.qwe-qwe/`.

## Core modules

- `agent.py` — entry points (`run`, `_run_inner`), message builder, compaction, auto-context recall
- `agent_loop.py` — v2 execution loop: streaming, tool dispatch, loop detection, abort (see [CLAUDE.md](CLAUDE.md) for details)
- `server.py` — FastAPI app, REST endpoints, WebSocket chat, optional password auth, per-IP rate limit
- `tools.py` — `TOOLS` list + `execute(name, args)` dispatcher for ~28 core tools
- `memory.py` — Qdrant abstraction + 3-way hybrid search (dense + SPLADE++ sparse + SQLite FTS5 BM25) fused via RRF
- `rag.py` — file / URL indexing via `markitdown` (PDF, DOCX, HTML, images…) with `yt-dlp` fallback for YouTube
- `providers.py` — 7 OpenAI-compatible provider configs, parallel ping, capability flags
- `soul.py` — personality traits → system prompt builder (`to_prompt()`)
- `turn_context.py` — per-request state (abort event, callbacks, image/file meta) kept in a `ContextVar` so concurrent turns don't stomp each other (added v0.17.25)
- `scheduler.py` — cron-style scheduled task runner
- `presets.py` — preset activation (swaps active thread + workspace + knowledge dir)
- `mcp_client.py` — Model Context Protocol client over stdio
- `skills/` — plugin system (see `skills/__init__.py`); each skill exports `DESCRIPTION`, `TOOLS`, `execute()`

## Request lifecycle (Web UI message)

1. Browser sends JSON frame over WS → `server.py` WebSocket handler.
2. Handler builds a `TurnContext` (abort event, streaming callbacks, image/file metadata).
3. `agent.run(user_input, thread_id, ctx=ctx)` is invoked (`agent.py:1275`).
4. `_build_messages` loads recent thread messages and calls `_auto_context` (`agent.py:739`) to retrieve relevant memories.
5. `agent_loop.run_loop` streams from the LLM, dispatches `delta.tool_calls` to `tools.execute()`, and emits deltas via the `TurnContext` callbacks.
6. On `finish_reason=stop` the assistant message is persisted; files queued by `send_file` are attached to the WS reply.

## Memory architecture

One Qdrant collection (`qwe_qwe`), three layers distinguished by `tag`:

- **Raw** (`knowledge/fact/user/...`) — direct saves and RAG chunks (~1000 chars).
- **Entity** (`entity`) — graph nodes with typed relations, produced by `synthesis.py`.
- **Wiki** (`wiki`) — synthesized markdown summaries (best recall quality).

Search is **3-way hybrid** — SQLite FTS5 BM25 + dense (FastEmbed 384d
multilingual) + sparse (SPLADE++) — merged with RRF (`memory.py:484`).
Auto-context is thread-scoped for raw messages and cross-thread for
synthesized tags.

## Tool search meta-pattern

The model sees ~28 core tools by default. `tool_search("keyword")` unlocks
extended families (notes, schedule, mcp, rag, soul, timer, ~18 more browser
tools) on demand — ~75% fewer schema tokens without losing capability.

## State locations

| Data | Path |
| --- | --- |
| User data root | `~/.qwe-qwe/` (override via `QWE_DATA_DIR`) |
| Messages, threads, kv, FTS5 | `qwe_qwe.db` (SQLite) |
| Memory vectors | `memory/` (Qdrant disk mode) |
| Indexed files | `uploads/kb/` |
| Wiki summaries | `wiki/` |
| User skills | `skills/` |
| Presets | `presets/<id>/` (each with its own `workspace/`, `knowledge/`, `skills/`) |
| Logs | `logs/qwe-qwe.log`, `logs/errors.log` |

## Extension points

- **Add a tool** — append an entry to `TOOLS` in `tools.py:466` and add a branch to `execute()` at `tools.py:1016`.
- **Add a skill** — drop a `.py` in `skills/` (package) or `~/.qwe-qwe/skills/` (user); export `DESCRIPTION`, `TOOLS`, `execute()`. See `skills/notes.py` for a minimal example.
- **Add a provider** — extend the registry in `providers.py` (look near `_LOCAL_PROVIDERS` at line 304); an OpenAI-compatible `/v1` endpoint is all that's required.

## See also

- [CLAUDE.md](CLAUDE.md) — LLM-agent workflow details (loop mechanics, prompt rules, release checklist).
- [CONTRIBUTING.md](CONTRIBUTING.md) — how to set up a dev environment and open a PR.
