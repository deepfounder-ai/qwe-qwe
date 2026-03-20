# Changelog

## v0.5.0 — 2026-03-20

### 🧠 Hybrid Search & Embeddings
- **FastEmbed** replaces OpenAI/LM Studio for embeddings — fully local ONNX inference, no server needed
- **Multilingual embeddings** — switched to `paraphrase-multilingual-MiniLM-L12-v2` (50+ languages including Russian)
- **Hybrid search** — dense (FastEmbed) + sparse (SPLADE++) vectors fused via Reciprocal Rank Fusion (RRF)
- **IDF modifier** on sparse index — rare/specific terms score higher than common words
- **Qdrant-side score filtering** — low-relevance results never leave the database (thresholds: 0.45 memory, 0.5 experience)
- **Recommend API** — positive/negative example-based search for experience learning
- **Grouping** — deduplicate search results by thread
- **Float16 vectors** — 2x less storage, same quality
- **Auto-migration v1→v2** — existing collections upgrade automatically with crash recovery

### 🤖 Small Model Optimizations
- **Smart tool output summarization** — JSON structure extraction, head+tail for logs (instead of dumb truncation)
- **Progressive context injection** — skip memory retrieval for trivial queries ("hello", "ok")
- **Improved compaction** — more focused summaries, 150 words max
- **Chain-of-workers** — when a background worker exhausts its rounds, it generates a structured handoff and spawns a continuation worker (max depth 3, total 45 rounds)
- **Self-knowledge** — agent knows its own file paths (logs, db, workspace) from the system prompt
- **spawn_task delegation rule** — model told to delegate complex 3+ step tasks to background workers

### 🔧 Skill Creator
- **Telegram template** — dedicated `_t_telegram` for Telegram Bot API integration (builds URL from bot_token, POSTs sendMessage)
- **Auto-detection** — `_infer_op` recognizes telegram skills before falling back to http_request
- **Param validation in smoke test** — verifies all required params from tool definition are actually used in generated code
- **delete_skill tool** — remove user-created skills by name
- **Template-based generation** — http_request, read_file, schedule templates use actual param names from definitions

### 🏗 Infrastructure
- **Vault module** — encrypted secrets (Fernet) now properly included in package (`py-modules`)
- **Graceful vault degradation** — clear error message if `cryptography` not installed
- **Background worker prompts** — workers know file paths, vault, memory, scheduling tools
- **Scheduler worker** — increased rounds (5→10), knows about `secret_get()` and `memory_search()`
- **System prompt architecture** — structured sections: identity → self-knowledge → tools → memory protocol → rules → examples
- **Inference wizard** — removed embedding model setup (FastEmbed handles it automatically)
- **Web UI** — removed embedding model configuration, shows FastEmbed status

### 🐛 Fixes
- Hash collisions in sparse embeddings — switched from `hash()` to `zlib.crc32` (deterministic)
- Migration crash safety — temp collection + resume logic for v1→v2 upgrade
- WS broadcast for task progress in background threads
- SQLite reserved word errors in DDL (quoted column names)
- Ollama `reasoning` field handling for thinking mode
- Input unlock when switching threads during generation

---

## v0.4.0

- Setup-inference wizard with Ollama auto-install
- LLM fallback hybrid mode — auto-escalate or ask user
- Configurable RAG chunk size
- Interactive model selection in wizard
