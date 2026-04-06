<p align="center">
  <img src="static/logo.png" alt="qwe-qwe" width="280">
</p>

<h3 align="center">AI agent optimized for small local models</h3>

<p align="center">
  Built for Qwen 9B & Gemma 4B on a gaming laptop. No cloud required.
</p>

<p align="center">
  <a href="#quick-start">Quick Start</a> •
  <a href="#why-small-models">Why Small Models</a> •
  <a href="#interfaces">Interfaces</a> •
  <a href="#tool-search">Tool Search</a> •
  <a href="#tools">Tools</a> •
  <a href="#skills">Skills</a> •
  <a href="#mcp">MCP</a> •
  <a href="#telegram-bot">Telegram</a> •
  <a href="#diagnostics">Doctor</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/version-0.9.0-blue" alt="version">
  <img src="https://img.shields.io/badge/python-3.11+-green" alt="python">
  <img src="https://img.shields.io/badge/license-MIT-orange" alt="license">
  <img src="https://img.shields.io/badge/runs-100%25_offline-purple" alt="offline">
  <a href="https://t.me/qwe_qwe_ai"><img src="https://img.shields.io/badge/community-Telegram-blue?logo=telegram" alt="Telegram"></a>
</p>

---

## What is qwe-qwe?

A personal AI agent designed to squeeze maximum capability out of **small local models** (4-9B parameters). Chat via terminal, browser, or Telegram — with tools, semantic memory, browser control, MCP integration, scheduled tasks, and a customizable personality.

Optimized for **Qwen 3.5 9B** and **Gemma 4 E4B** running on a single consumer GPU (4-8GB VRAM). Cloud providers supported as fallback, but the architecture, prompts, and tool system are built for the constraints of small models.

> **Philosophy**: every token is expensive. Don't make the model smarter — make the system around it smarter. Tool search, compact prompts, retry loops, JSON repair, and self-checks compensate for what the model lacks.

## Why Small Models

| | Cloud (GPT, Claude) | Local (Qwen 9B) |
|---|---|---|
| **Latency** | 2-10s network + inference | 1-5s local inference |
| **Privacy** | Data leaves your machine | Everything stays local |
| **Cost** | $20-200/month | Free after GPU purchase |
| **Offline** | No | Works without internet |
| **Customization** | System prompt only | Full control over everything |
| **Reliability** | API outages, rate limits | Always available |

qwe-qwe makes the trade-off worth it by working *with* the model's limitations instead of fighting them.

## Quick Start

### Prerequisites

- Python 3.11+
- [LM Studio](https://lmstudio.ai) or [Ollama](https://ollama.ai) with a loaded model
- **Recommended models:**
  - Qwen 3.5 9B Q4_K_M (~5.5GB) — best for tool calling and agents
  - Gemma 4 E4B-IT (~4GB) — fast, good for simple tasks
- **Embeddings:** FastEmbed (ONNX, local) — multilingual-MiniLM (384d, 50+ languages) + SPLADE++

### Install

**One-line install:**
```bash
curl -fsSL https://raw.githubusercontent.com/deepfounder-ai/qwe-qwe/main/install.sh | bash
```

**Windows:**
```cmd
git clone https://github.com/deepfounder-ai/qwe-qwe.git && cd qwe-qwe
setup.bat
```

**Manual:**
```bash
git clone https://github.com/deepfounder-ai/qwe-qwe.git && cd qwe-qwe
./setup.sh
source .venv/bin/activate
```

### Run

```bash
qwe-qwe              # terminal chat
qwe-qwe --web        # web UI at http://localhost:7860
qwe-qwe --doctor     # check everything works
```

LM Studio / Ollama are auto-detected on localhost during setup. If your server is on another machine:
```bash
export QWE_LLM_URL=http://<your-ip>:1234/v1
```

### Recommended hardware

| Component | Minimum | Recommended |
|-----------|---------|-------------|
| GPU | 4GB VRAM (4B Q4) | 8GB VRAM (9B Q4_K_M) |
| RAM | 8GB | 16GB |
| Storage | 10GB | 20GB (models + memory) |

Works on: gaming laptops, desktop GPUs (RTX 3060+), Mac M1+ (via Ollama).

## Architecture

```
                               +-- Qdrant (semantic memory, hybrid search)
CLI (terminal)  <--+           +-- RAG (file indexing & search)
Web UI (browser) <--+-- Agent -+-- SQLite (history, threads, state)
Telegram bot    <--/    Loop   +-- Tools (8 core + tool_search)
                        |      +-- Skills (7 built-in, user-creatable)
                        |      +-- Browser (Playwright/Chromium)
                        |      +-- MCP (external tool servers)
                        |      +-- Scheduler (cron tasks)
                        |      +-- Vault (encrypted secrets)
                        v
                   LLM (local or cloud)
                   7 providers supported
```

### Small-model optimizations

- **Tool Search** — only 8 core tools loaded by default (~750 tokens); model calls `tool_search("keyword")` to activate more. Saves **75% tokens** vs loading all 46 tools
- **Compact system prompt** (~1200 tokens) — no redundant tool descriptions
- **JSON repair engine** — fixes malformed tool calls (trailing commas, unclosed brackets, single quotes)
- **Anti-hedge nudge** — if model talks instead of acting, it gets pushed to use tools
- **Self-check validation** — validates tool args before execution, with required-field checks
- **Smart compaction** — summarizes old messages when context fills up, saves to memory
- **Stuck detection** — warns model after 5+ tool errors per turn
- **Experience learning** — agent remembers past task outcomes and adapts strategies
- **Shell via Git Bash** — UNIX commands work on Windows, auto-detected

## Interfaces

### Web UI

```bash
qwe-qwe --web
```

ChatGPT-style dark UI with:
- **Left sidebar** with thread list (collapsible on desktop, slide-in on mobile)
- **Real-time streaming** via WebSocket
- **Tool call activity log** — collapsible groups showing what the agent did (Claude Code style)
- Thread management (create, switch, rename, branch)
- Image upload & vision
- Soul personality editor
- Model picker with provider switching
- MCP server management
- Settings page (all agent parameters)
- Mobile responsive (iOS/Android friendly)
- LAN access from phone/tablet/PC

### Terminal (CLI)

```bash
qwe-qwe
```

Rich-formatted terminal chat with 20+ slash commands: `/soul`, `/skills`, `/memory`, `/model`, `/thread`, `/cron`, `/logs`, `/stats`, `/doctor` and more.

### Telegram Bot

Full mobile access — streaming responses, slash commands, topic-to-thread mapping, image support, formatted messages. [Setup guide below](#telegram-bot-setup).

## Tool Search

qwe-qwe uses a **meta-tool architecture** to minimize token usage. Only 8 core tools are loaded by default:

| Core Tool | Purpose |
|-----------|---------|
| `memory_search` | Search saved memories |
| `memory_save` | Save to long-term memory |
| `read_file` | Read file contents |
| `write_file` | Write/create files |
| `shell` | Run bash commands |
| `http_request` | HTTP requests to any API |
| `spawn_task` | Run tasks in background |
| `tool_search` | Discover & activate more tools |

When the model needs more capabilities, it calls `tool_search("browser")` or `tool_search("notes")` — which activates the relevant tools for that turn.

**Keywords:** `browser`, `notes`, `schedule`, `secret`, `mcp`, `profile`, `rag`, `skill`, `soul`, `timer`, `model`, `cron`

This saves **~3000 tokens per request** compared to loading all 46 tools.

## Tools

46 tools total across core + extensions + skills:

| Category | Tools | Loaded |
|----------|-------|--------|
| **Memory** | `memory_search`, `memory_save`, `memory_delete` | Core |
| **Files & Shell** | `read_file`, `write_file`, `shell` | Core |
| **HTTP** | `http_request` | Core |
| **Tasks** | `spawn_task`, `schedule_task`, `list_cron`, `remove_cron` | Core + Search |
| **Vault** | `secret_save`, `secret_get`, `secret_list`, `secret_delete` | Search |
| **RAG** | `rag_index`, `rag_search`, `rag_status` | Search |
| **Browser** | `browser_open`, `browser_snapshot`, `browser_screenshot`, `browser_click`, `browser_fill`, `browser_eval`, `browser_close` | Search |
| **Notes** | `create_note`, `list_notes`, `read_note`, `edit_note`, `delete_note` | Search |
| **Model** | `switch_model` | Search |
| **Profile** | `user_profile_update`, `user_profile_get` | Search |

## Skills

Pluggable skill system — built-in skills + create your own from chat:

| Skill | Description |
|-------|-------------|
| `browser` | Web browsing via Playwright (open, read, click, screenshot) |
| `mcp_manager` | Manage MCP tool servers (add, remove, restart) |
| `skill_creator` | Create new skills from chat (multi-step LLM pipeline) |
| `soul_editor` | AI-assisted personality tuning |
| `notes` | Note management |
| `timer` | Countdown timers |
| `weather` | Weather reports via wttr.in |

### Creating skills from chat

```
You: create a skill for tracking my daily habits
Agent: Skill 'habit_tracker' generation started...
       plan -> tools -> code -> validate -> Created and enabled! (3 tools, 45s)
```

## Browser

Built-in browser control via Playwright + headless Chromium:

```
You: open google.com and search for "qwen 3.5 benchmarks"
Agent: [tool_search("browser")] -> [browser_open] -> [browser_snapshot]
       Found results: ...
```

Tools: `browser_open`, `browser_snapshot`, `browser_screenshot`, `browser_click`, `browser_fill`, `browser_eval`, `browser_close`

Activated via `tool_search("browser")`. The agent can navigate pages, read content, fill forms, click buttons, and take screenshots.

## MCP

**Model Context Protocol** — connect external tool servers to extend the agent's capabilities:

```
You: add MCP server for filesystem access
Agent: [tool_search("mcp")] -> [mcp_add_server] Added 'filesystem' (14 tools)
```

Supports **stdio** (subprocess) and **HTTP** transports. Configured via Settings > System > MCP Servers or through chat using the `mcp_manager` skill.

MCP tools appear as `mcp__servername__toolname` and are automatically available through tool_search.

## Providers

Primary target is **local models via LM Studio or Ollama**. Cloud providers supported as fallback:

| Provider | Type | Notes |
|----------|------|-------|
| **LM Studio** | Local | Primary target. Auto-loads models |
| **Ollama** | Local | Standard Ollama API |
| **OpenAI** | Cloud | GPT-4o, GPT-4.1, etc. |
| **OpenRouter** | Cloud | Multi-model gateway |
| **Groq** | Cloud | Fast inference |
| **Together** | Cloud | Open-source models |
| **DeepSeek** | Cloud | DeepSeek models |

Switch on the fly via `/model` (CLI/Telegram) or Settings (Web UI). Auto-discovers available models.

## Memory & Knowledge Graph

Three-layer knowledge system in a single Qdrant collection:

```
Layer 1: RAW           Layer 2: ENTITIES        Layer 3: WIKI
(saved immediately)    (night synthesis)        (night synthesis)

"FastAPI uses       -> [FastAPI] --uses-->      "FastAPI is a modern
 Pydantic for          [Pydantic]               Python framework that
 validation..."        [Python]                  uses Pydantic for
                       [Starlette]               automatic validation..."
```

### How it works

**During the day** (fast, no LLM cost):
- Agent saves facts and knowledge via `memory_save`
- Long texts (>1000 chars) auto-chunked into ~800 char pieces
- Each chunk tagged `synthesis_status=pending`

**At night** (configurable cron, default 03:00):
- Synthesis worker processes pending queue
- LLM extracts entities + relations from chunks
- Creates entity nodes with typed relations (uses, built_on, part_of, etc.)
- Generates wiki summaries stored as searchable chunks
- Writes markdown to `~/.qwe-qwe/wiki/` as human-readable backup

**During search** (enriched context):
- Wiki chunks found first (synthesized = higher quality embeddings)
- Entity relations expanded (follow links to related knowledge)
- Raw chunks provide specifics
- Result: synthesized + structured + raw knowledge in one query

### Features

- **Hybrid search**: FastEmbed dense (384d, 50+ languages) + SPLADE++ sparse, fused via RRF
- **Auto-chunking**: long texts split on sentence boundaries with overlap
- **Knowledge graph**: entities with typed relations, built automatically
- **Wiki pages**: synthesized markdown, searchable and human-readable
- **Graph visualization**: interactive force-directed graph in Web UI (Knowledge > Graph tab)
- **Thread isolation**: each conversation has its own memory context
- **Smart compaction**: old messages summarized and saved to memory when context fills
- **Auto-context**: wiki + entities + memories injected into each turn
- **Experience learning**: past task outcomes inform future strategies
- **Modes**: in-memory (testing), disk (default), or remote Qdrant server

## Scheduler

Cron-like task scheduling with natural syntax:

```
"in 5m"        -> run once in 5 minutes
"every 2h"     -> repeat every 2 hours
"daily 09:00"  -> every day at 09:00
"14:30"        -> once today/tomorrow at 14:30
```

Results delivered to Telegram and Web UI. Simple reminders bypass LLM for instant delivery.

## Telegram Bot Setup

1. Create a bot via [@BotFather](https://t.me/BotFather) -> copy the token
2. Set the token: `/telegram token <TOKEN>` (CLI) or Settings -> Telegram (Web)
3. Start the bot: `/telegram start`
4. Generate activation code: `/telegram activate`
5. Send the 6-digit code to your bot in Telegram

### Security

- One-time 6-digit codes, expire in 10 minutes
- 3 wrong attempts -> permanent ban (by Telegram user ID)
- Only verified owner can chat with the bot

### Telegram Features

- **Streaming responses** via editMessageText
- **Topic isolation**: supergroup topics -> separate threads
- **Formatted messages**: MarkdownV2 with HTML fallback
- **Image support**: send images for vision analysis
- **Cron results**: scheduled task output delivered to chat
- 12 slash commands: `/status`, `/model`, `/soul`, `/skills`, `/memory`, `/threads`, `/stats`, `/cron`, `/thinking`, `/doctor`, `/clear`, `/help`

## Personality (Soul)

8 adjustable traits (low / moderate / high):

| Trait | Low | High |
|-------|-----|------|
| humor | serious | jokes around |
| honesty | diplomatic | brutally honest |
| curiosity | answers questions | asks follow-ups |
| brevity | verbose | concise |
| formality | casual | formal |
| proactivity | waits for requests | suggests ideas |
| empathy | rational | empathetic |
| creativity | practical | unconventional |

Plus custom traits, agent name, and language selection. Edit via `/soul` (CLI), Settings (Web), or `/soul` (Telegram).

## Diagnostics

```bash
qwe-qwe --doctor
```

Checks 14 system components: Python, dependencies, SQLite, Qdrant, provider, LLM API, model status, embeddings, inference latency, Telegram, threads, skills, tools, disk space.

## Config

Environment variables:

```bash
QWE_LLM_URL=http://localhost:1234/v1    # LLM server URL
QWE_LLM_MODEL=qwen/qwen3.5-9b          # Model name
QWE_LLM_KEY=lm-studio                  # API key
QWE_DB_PATH=~/.qwe-qwe/qwe_qwe.db     # Database path
QWE_QDRANT_MODE=disk                    # memory | disk | server
QWE_PASSWORD=                           # Web UI auth (optional)
```

## Docker

```bash
docker compose up
```

LM Studio / Ollama should be running on the host. Persistent data in `./data/`.

## Project Structure

```
cli.py            Terminal interface + entry point
server.py         FastAPI web server + WebSocket
agent.py          Core agent loop + JSON repair + self-check
config.py         Settings (env-configurable)
db.py             SQLite storage (WAL mode)
memory.py         Qdrant semantic memory (hybrid search)
rag.py            RAG file indexing & search
tools.py          Tool definitions + tool_search + execution
mcp_client.py     Model Context Protocol client
providers.py      Multi-provider LLM management
soul.py           Personality system + prompt generation
tasks.py          Background task runner
scheduler.py      Cron-like scheduler
threads.py        Thread management
telegram_bot.py   Telegram bot integration
vault.py          Encrypted secrets (Fernet)
logger.py         Structured logging
skills/           Pluggable skill modules
  browser.py      Web browsing (Playwright)
  mcp_manager.py  MCP server management
  skill_creator.py Skill generation pipeline
  soul_editor.py  Personality editing
  notes.py        Note management
  timer.py        Countdown timers
  weather.py      Weather reports
static/           Web UI (single-file HTML/CSS/JS)
```

## Community

Join our Telegram community: **[@qwe_qwe_ai](https://t.me/qwe_qwe_ai)**

## License

MIT

---

<p align="center">
  Built with care by <a href="https://deepfounder.ai">DeepFounder</a>
</p>
