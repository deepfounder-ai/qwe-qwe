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
  <a href="#diagnostics">Doctor</a>
</p>

---

## What is qwe-qwe?

A personal AI agent that runs **entirely on your machine**. Chat via terminal, browser, or Telegram — with tools, semantic memory, scheduled tasks, and a customizable personality. Works with any OpenAI-compatible LLM (LM Studio, Ollama, or cloud providers).

## Architecture

```
                               ┌── Qdrant (semantic memory)
CLI (terminal)  ←──┐           ├── SQLite (history, threads, state)
Web UI (browser) ←──┼── Agent ─┤── Tools (32 built-in)
Telegram bot    ←──┘    Loop   ├── Skills (pluggable)
                        │      ├── Scheduler (cron tasks)
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

### Prerequisites

- Python 3.11+
- [LM Studio](https://lmstudio.ai) with a loaded model (Qwen 3.5 9B recommended)
- Embedding model (nomic-embed-text-v1.5 recommended)

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
Dark-themed chat with WebSocket streaming, soul sliders, model picker, thread management, and settings page.

### Telegram Bot
Full-featured Telegram integration with slash commands, topic-to-thread mapping, and formatted messages. [Setup guide →](#telegram-bot)

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

32 built-in tools the agent can use:

| Category | Tools |
|----------|-------|
| **Memory** | `memory_search`, `memory_save`, `memory_delete` |
| **Files** | `read_file`, `write_file`, `list_directory` |
| **Shell** | `shell` (with safety blocks for destructive commands) |
| **Tasks** | `schedule_task`, `spawn_task` |
| **Notes** | `create_note`, `list_notes`, `search_notes` |
| **Web** | `web_search` |
| **System** | `get_time`, `get_weather` |

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
6. ✅ Verified — you're the owner

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

  All 14 checks passed! ⚡
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

## Logging

Structured logs with rotation:

- `logs/qwe-qwe.log` — all events (rotated at 5MB)
- View via `/logs` (CLI), Settings → Logs (Web), or `qwe-qwe --doctor`

## Project Structure

```
├── cli.py           # Terminal interface + entry point
├── server.py        # FastAPI web server + WebSocket
├── agent.py         # Core agent loop + compaction
├── config.py        # Settings and defaults
├── db.py            # SQLite storage (WAL mode)
├── memory.py        # Qdrant semantic memory
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
├── logs/            # System logs
├── setup.sh         # Installer
├── install.sh       # One-line install script
└── pyproject.toml   # Package config
```

## License

MIT
