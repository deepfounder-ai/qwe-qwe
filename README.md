# ⚡ qwe-qwe

Lightweight offline AI agent for local models. No cloud, no API keys, no subscriptions — just your GPU.

## Architecture

```
       CLI (terminal)  ←──┐
                          ├── Agent Loop → LLM (LM Studio)
Web UI (browser)   ←──┘        ├── Qdrant (semantic memory)
                               ├── SQLite (history, state)
                               ├── Tools (shell, files, memory)
                               ├── Skills (pluggable)
                               ├── Scheduler (cron)
                               └── Logger (structured logs)
```

## Quick Start

```bash
# 1. Clone
git clone <repo> && cd qwe-qwe

# 2. Install
./setup.sh

# 3. Activate venv
source .venv/bin/activate

# 4. Run
qwe-qwe              # terminal chat
qwe-qwe --web        # web UI at http://localhost:7860
```

### Prerequisites

- Python 3.11+
- [LM Studio](https://lmstudio.ai) with a loaded model (Qwen 3.5 9B recommended)
- Embedding model in LM Studio (nomic-embed-text-v1.5)

## Interfaces

### Terminal (CLI)
```bash
qwe-qwe
```
Full-featured terminal interface with Rich rendering, `/soul` editor, `/skills` selector, `/logs` viewer.

### Web UI
```bash
qwe-qwe --web                  # default: 0.0.0.0:7860
qwe-qwe --web --port 8080      # custom port
```
Dark-themed chat interface with WebSocket streaming, soul sliders, log viewer.

**API endpoints:**
| Endpoint | Description |
|----------|-------------|
| `GET /` | Chat UI |
| `GET /api/status` | Agent stats |
| `GET /api/history` | Chat history |
| `GET /api/logs` | Tail log files |
| `GET /api/soul` | Soul config |
| `POST /api/soul` | Update traits |
| `WS /ws` | Chat WebSocket |

## CLI Commands

| Command | Description |
|---------|-------------|
| `/soul` | Interactive personality editor |
| `/skills` | Enable/disable skill plugins |
| `/memory` | Search semantic memory |
| `/cron` | View scheduled tasks |
| `/tasks` | Background task status |
| `/stats` | Session statistics |
| `/logs` | View system logs |
| `/clear` | Clear conversation |
| `/quit` | Exit |

## Tools

| Tool | Description |
|------|-------------|
| `memory_search` | Semantic search over long-term memory |
| `memory_save` | Save facts, preferences, decisions |
| `memory_delete` | Remove a memory |
| `read_file` | Read file contents |
| `write_file` | Create/overwrite files |
| `shell` | Run shell commands (with safety blocks) |
| `schedule_task` | Cron-like task scheduling |
| `spawn_task` | Background parallel tasks |

## Skills

Pluggable skill system — drop a `.py` file in `skills/` and toggle with `/skills`:
- `weather` — weather reports
- `finance` — expense tracking
- `notes` — note management
- `timer` — timers and alarms
- `soul_editor` — AI-assisted personality tuning
- `skill_creator` — create new skills from chat

## Logging

Structured system logs in `logs/`:
- `qwe-qwe.log` — all events (rotated at 5MB)
- `errors.log` — warnings and errors only

View from CLI: `/logs`, `/logs errors`, `/logs 50`
View from web: Settings → Logs

## Config

Edit `config.py`:
```python
LLM_BASE_URL = "http://192.168.0.49:1234/v1"  # LM Studio
LLM_MODEL = "qwen/qwen3.5-9b"
EMBED_MODEL = "text-embedding-nomic-embed-text-v1.5"
```

## Files

```
├── cli.py          # Terminal interface + entry point
├── server.py       # FastAPI web server
├── agent.py        # Core agent loop
├── config.py       # All settings
├── db.py           # SQLite storage
├── memory.py       # Qdrant semantic memory
├── soul.py         # Personality system
├── tools.py        # Tool definitions + execution
├── tasks.py        # Background task runner
├── scheduler.py    # Cron-like scheduler
├── logger.py       # Structured logging
├── skills/         # Pluggable skills
├── static/         # Web UI
├── logs/           # System logs
├── memory/         # Qdrant disk storage
├── setup.sh        # Installer
└── pyproject.toml  # Package config
```

## License

MIT
