<p align="center">
  <img src="static/logo.png" alt="qwe-qwe" width="280">
</p>

<h3 align="center">Lightweight offline AI agent for local models</h3>

<p align="center">
  No cloud. No API keys. No subscriptions. Just your GPU.
</p>

<p align="center">
  <a href="#quick-start">Quick Start</a> •
  <a href="#interfaces">Interfaces</a> •
  <a href="#telegram-bot">Telegram</a> •
  <a href="#tools">Tools</a> •
  <a href="#skills">Skills</a> •
  <a href="#docker">Docker</a> •
  <a href="#diagnostics">Doctor</a>
</p>

---

## What is qwe-qwe?

A personal AI agent that runs **entirely on your machine**. Chat via terminal, browser, or Telegram — with tools, semantic memory, scheduled tasks, and a customizable personality. Works with any OpenAI-compatible LLM (LM Studio, Ollama, or cloud providers).

## Architecture

```
                               ┌── Qdrant (semantic memory)
CLI (terminal)  ←──┐           ├── RAG (file indexing & search)
Web UI (browser) ←──┼── Agent ─┤── SQLite (history, threads, state)
Telegram bot    ←──┘    Loop   ├── Tools (32 built-in)
                        │      ├── Skills (pluggable)
                        │      ├── Vision (image understanding)
                        │      ├── Scheduler (cron tasks)
                        │      ├── Vault (encrypted secrets)
                        │      └── Structured logging
                        ↓
                   LLM (local or cloud)
                   7 providers supported
```

## Quick Start

**One-line install:**
```bash
curl -fsSL https://raw.githubusercontent.com/deepfounder-ai/qwe-qwe/main/install.sh | bash
```

**Or manually:**
```bash
git clone https://github.com/deepfounder-ai/qwe-qwe.git && cd qwe-qwe
./setup.sh
source .venv/bin/activate
qwe-qwe              # terminal chat
qwe-qwe --web        # web UI at http://localhost:7860
qwe-qwe --doctor     # check everything works
```

**Docker:**
```bash
docker compose up
```

### Prerequisites

- Python 3.11+
- [LM Studio](https://lmstudio.ai) or [Ollama](https://ollama.ai) with a loaded model
- Recommended: Qwen 3.5 9B (works on 8GB GPU)
- Embedding model: nomic-embed-text-v1.5

LM Studio / Ollama are auto-detected on localhost during setup.

## Interfaces

### Terminal (CLI)
```bash
qwe-qwe
```
Rich-formatted terminal chat with `/soul` editor, `/skills` toggle, `/memory` search, `/logs` viewer, and more.

### Web UI
```bash
qwe-qwe --web                  # default: 0.0.0.0:7860
qwe-qwe --web --port 8080      # custom port
```
Dark-themed chat with WebSocket streaming, image upload, soul sliders, model picker, thread management, and settings page.

**LAN Access**: toggle LAN broadcasting from the Settings page — access your agent from any device on your local network (phone, tablet, another PC). When enabled, the web UI is available at `http://<your-ip>:7860`.

**API endpoints:**
| Endpoint | Description |
|----------|-------------|
| `GET /` | Chat UI |
| `GET /api/status` | Agent stats |
| `GET /api/discover` | Auto-discover LLM servers |
| `POST /api/upload` | Upload image for vision |
| `POST /api/login` | Authenticate (if password set) |
| `GET /api/history` | Chat history |
| `GET /api/soul` | Soul config |
| `POST /api/soul` | Update traits |
| `WS /ws` | Chat WebSocket |

### Telegram Bot
Full-featured Telegram integration with slash commands, topic-to-thread mapping, image support, and formatted messages. [Setup guide →](#telegram-bot)

## Providers

Switch between 7 LLM providers on the fly:

| Provider | Type | Notes |
|----------|------|-------|
| **LM Studio** | Local | Auto-loads models via v1 API |
| **Ollama** | Local | Standard Ollama API |
| **OpenAI** | Cloud | GPT-4, etc. |
| **OpenRouter** | Cloud | Multi-model gateway |
| **Groq** | Cloud | Fast inference |
| **Together** | Cloud | Open-source models |
| **DeepSeek** | Cloud | DeepSeek models |

Auto-switches model when changing providers to prevent invalid combos.

## Memory

Thread-scoped semantic memory powered by Qdrant:

- **Save**: agent auto-saves important facts, preferences, decisions
- **Search**: semantic similarity search (nomic-embed-text, 768 dim, COSINE)
- **Thread isolation**: each thread/topic has its own memory context
- **Smart compaction**: when context exceeds 24k tokens, old messages are summarized and saved to memory
- **Auto-context**: injects relevant memories into each conversation (thread-scoped first, then global)

## Tools

32+ built-in tools the agent can use:

| Category | Tools |
|----------|-------|
| **Memory** | `memory_search`, `memory_save`, `memory_delete` |
| **Files** | `read_file`, `write_file`, `list_directory` |
| **Shell** | `shell` (with safety blocks for destructive commands) |
| **Tasks** | `schedule_task`, `spawn_task` |
| **RAG** | `rag_index`, `rag_search`, `rag_status` |
| **Vault** | `secret_save`, `secret_get`, `secret_list`, `secret_delete` |
| **Notes** | `create_note`, `list_notes`, `search_notes` |
| **Web** | `web_search` |
| **System** | `get_time`, `get_weather`, `switch_model` |

## Skills

Pluggable skill system — drop a `.py` file in `skills/` and toggle with `/skills`:

- `weather` — weather reports via wttr.in
- `finance` — expense/income tracking
- `notes` — note management with search
- `timer` — timers and alarms
- `soul_editor` — AI-assisted personality tuning
- `skill_creator` — create new skills from chat

## Scheduler

Cron-like task scheduling with flexible syntax:

```
"in 5m"        → run once in 5 minutes
"every 2h"     → repeat every 2 hours
"daily 09:00"  → every day at 09:00
"14:30"        → once today/tomorrow at 14:30
```

- Results delivered to **Telegram** and **Web UI**
- Simple reminders bypass LLM for instant delivery
- Complex tasks run through the agent with full tool access
- Manage via `/cron` (CLI & Telegram) or Web UI

## Telegram Bot

Full mobile access to your agent via Telegram.

### Setup

1. Create a bot via [@BotFather](https://t.me/BotFather) → copy the token
2. Set the token: `/telegram token <TOKEN>` (CLI) or Settings → Telegram (Web)
3. Start the bot: `/telegram start`
4. Generate activation code: `/telegram activate` or Web UI "Generate Code"
5. Send the 6-digit code to your bot in Telegram
6. Verified — you're the owner

### Security

- **One-time 6-digit codes**, expire in 10 minutes
- **3 wrong attempts → permanent ban** (by Telegram user ID)
- Only verified owner can chat with the bot
- Group support: mention-only or all messages (BotFather privacy mode)

### Telegram Commands

| Command | Description |
|---------|-------------|
| `/status` | Agent status and stats |
| `/model` | Current model info |
| `/soul` | Personality traits |
| `/skills` | Active skills |
| `/memory` | Memory stats |
| `/threads` | Thread list |
| `/stats` | Session statistics |
| `/cron` | Scheduled tasks |
| `/thinking` | Toggle thinking mode |
| `/doctor` | Run diagnostics |
| `/clear` | Clear conversation |
| `/chatid` | Show chat/topic IDs |
| `/help` | Command list |

### Features

- **Topic isolation**: supergroup topics map to separate threads with isolated memory
- **Formatted messages**: MarkdownV2 with HTML fallback (bold, italic, code, links)
- **Continuous typing**: indicator stays active while model generates
- **Image support**: send images for vision analysis
- **Compaction notifications**: delivered to the same topic where they happened
- **Cron results**: scheduled task output delivered to your chat

## Diagnostics

```bash
qwe-qwe --doctor
```

Checks 14 system components:

```
  ✓ Python: 3.12.3
  ✓ Dependencies: ✓
  ✓ SQLite: 6 tables, 69 messages, 32 settings
  ✓ Qdrant: 4 memories (disk mode)
  ✓ Provider: qwen/qwen3.5-9b @ lmstudio
  ✓ LLM API: 2 models available
  ✓ Model loaded: qwen/qwen3.5-9b loaded in memory
  ✓ Embeddings: text-embedding-nomic-embed-text-v1.5
  ✓ Inference: replied 'ok' in 1.0s (10 tokens)
  ✓ Telegram: @yourbot (verified)
  ✓ Threads: 4 threads
  ✓ Skills: 6/7 active
  ✓ Tools: 32 tools registered
  ✓ Disk: 840.7GB free

  All 14 checks passed!
```

Also available via `/doctor` in Telegram.

## Personality (Soul)

Customize your agent's personality with adjustable traits:

- **Name** and **language**
- **Creativity** (0-10) — temperature control
- **Verbosity** (0-10) — response length
- **Formality** (0-10) — casual to formal
- **Custom traits** — add any personality dimension

Edit via `/soul` (CLI), Settings page (Web), or `/soul` (Telegram).

## Threads

Isolated conversation contexts:

- **Default thread** for general chat
- **Named threads** created manually or auto-created from Telegram topics
- Each thread has its own history, memory context, and optional model override
- Switch via `/thread` (CLI) or tabs (Web UI)

## Config

All settings can be overridden via environment variables:

```bash
QWE_LLM_URL=http://localhost:1234/v1    # LLM server URL
QWE_LLM_MODEL=qwen/qwen3.5-9b          # Model name
QWE_LLM_KEY=lm-studio                  # API key
QWE_EMBED_URL=                          # Embedding server (defaults to LLM URL)
QWE_EMBED_MODEL=text-embedding-nomic-embed-text-v1.5
QWE_DB_PATH=qwe_qwe.db                 # SQLite database path
QWE_QDRANT_MODE=disk                    # memory | disk | server
QWE_PASSWORD=                           # Set to enable web UI authentication
```

Or edit `config.py` directly.

## Docker

```bash
docker compose up
```

LM Studio / Ollama should be running on the host machine. The container connects via `host.docker.internal`.

Persistent data is stored in `./data/` (memory, logs, skills, database).

## Logging

Structured logs with rotation:

- `logs/qwe-qwe.log` — all events (rotated at 5MB)
- View via `/logs` (CLI), Settings → Logs (Web), or `qwe-qwe --doctor`

## Project Structure

```
├── cli.py           # Terminal interface + entry point
├── server.py        # FastAPI web server + auth + rate limiting
├── agent.py         # Core agent loop + JSON repair + compaction
├── config.py        # Settings (env-configurable)
├── db.py            # SQLite storage (WAL mode)
├── memory.py        # Qdrant semantic memory
├── rag.py           # RAG file indexing & search
├── discovery.py     # Auto-discover LLM servers
├── providers.py     # Multi-provider LLM management
├── soul.py          # Personality system
├── tools.py         # Tool definitions + execution
├── tasks.py         # Background task runner
├── scheduler.py     # Cron-like scheduler
├── threads.py       # Thread management
├── telegram_bot.py  # Telegram bot integration
├── vault.py         # Encrypted secrets storage
├── logger.py        # Structured logging
├── skills/          # Pluggable skill modules
├── static/          # Web UI (HTML/CSS/JS)
├── tests/           # Test suite
├── logs/            # System logs
├── setup.sh         # Installer
├── install.sh       # One-line install script
├── Dockerfile       # Container build
├── docker-compose.yml
└── pyproject.toml   # Package config
```

## License

MIT
