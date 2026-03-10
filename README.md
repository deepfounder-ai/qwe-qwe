# ⚡ qwe-qwe

Lightweight offline AI agent for local models. Runs on any gaming laptop with a GPU.

## Architecture

```
You (CLI) → Agent Loop → Qwen 3.5 9B (LM Studio)
                ├── Qdrant (semantic memory, in-memory)
                ├── SQLite (history, settings)
                └── Tools (files, shell, web, memory)
```

**Key idea:** Memory lives in databases, not in context. The model gets only what it needs right now (~2-4k tokens), not everything ever.

## Quick Start

```bash
# 1. Prerequisites: LM Studio running with Qwen 3.5 + nomic-embed
# 2. Install
pip install -r requirements.txt

# 3. Run
python cli.py
```

## Tools

| Tool | Description |
|------|-------------|
| `memory_search` | Semantic search over long-term memory |
| `memory_save` | Save facts, preferences, decisions |
| `read_file` | Read file contents |
| `write_file` | Create/overwrite files |
| `shell` | Run shell commands |
| `web_fetch` | Fetch URL content |

## Config

Edit `config.py` — LM Studio URL, model names, context limits.

## Files

- `config.py` — all settings
- `agent.py` — core agent loop
- `tools.py` — tool definitions + execution
- `memory.py` — Qdrant semantic memory
- `db.py` — SQLite storage
- `cli.py` — terminal interface
- `qwe_qwe.db` — auto-created SQLite database
