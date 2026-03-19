# Product Hunt Launch — qwe-qwe

## Tagline (60 chars max)
```
Your AI agent that runs 100% offline on your laptop
```
(52 chars)

## Short Description (260 chars max)
```
Open-source AI agent optimized for small local models (9B). Chat via terminal, browser, or Telegram. Semantic memory, 32+ tools, RAG, skill creation from chat. Runs on gaming laptops (8GB GPU). No cloud, no API keys, no subscriptions. MIT license.
```
(249 chars)

## Long Description

### The problem

Cloud AI assistants cost $20-200/month, send your data to remote servers, and go down when you need them most. Local models are getting powerful enough for real work — but running a raw model gives you a chatbot, not an assistant.

### The solution

qwe-qwe wraps a small local model (Qwen 9B) with everything it needs to be a real personal agent: semantic memory, file tools, shell access, RAG search, encrypted vault, scheduled tasks, and a skill creation pipeline.

The key insight: don't make the 9B model smarter — make the system around it smarter. JSON repair catches malformed outputs. Retry loops handle tool failures. Memory persists context across sessions. Experience learning remembers what worked and what didn't.

### What you get

- **Semantic Memory (Memento)** — Your agent remembers facts, preferences, and past task outcomes. Successful approaches get reinforced, failed ones get avoided.
- **32+ Built-in Tools** — File operations, shell commands, web search, note-taking, secrets vault, scheduling. One tool call away.
- **RAG File Search** — Index any file (code, docs, PDFs) and search by meaning, not keywords.
- **Skill Creation from Chat** — Say "create a habit tracker skill" and the agent generates, validates, and deploys working Python code.
- **4 Interfaces** — Terminal (rich formatting, 20+ commands), Web UI (dark theme, WebSocket streaming), Telegram bot (mobile access with security), LAN (access from any device).
- **Customizable Personality** — Adjust humor, formality, creativity, verbosity. Add custom traits. Name your agent.
- **7 LLM Providers** — Local-first (LM Studio, Ollama) with cloud fallback (OpenAI, OpenRouter, Groq, Together, DeepSeek).

### Hardware

Runs on what you already have:
- Gaming laptop with RTX 3060+ (8GB VRAM)
- Mac M1/M2/M3 (via Ollama)
- Any machine with 16GB RAM

### How it works

1. **Install**: `curl -fsSL https://raw.githubusercontent.com/deepfounder-ai/qwe-qwe/main/install.sh | bash`
2. **Set up inference**: `qwe-qwe --setup-inference` (auto-detects GPU, installs Ollama, pulls model)
3. **Chat**: `qwe-qwe` (terminal) or `qwe-qwe --web` (browser)

### Open source

MIT license. 8,800+ lines of Python. 242 commits. Built by DeepFounder.

GitHub: https://github.com/deepfounder-ai/qwe-qwe
Community: https://t.me/qwe_qwe_ai

---

## Topics
- Open Source
- Artificial Intelligence
- Developer Tools
- Privacy
- Productivity

## Maker's First Comment

```
Hey Product Hunt! I'm the maker of qwe-qwe.

I built this because I was spending $40/month on cloud AI subscriptions while my RTX 3060 sat idle during non-gaming hours. Modern 9B models like Qwen 3.5 are surprisingly capable — but a raw model is just a chatbot. I wanted a real assistant.

The core philosophy: don't try to make a 9B model as smart as GPT-4. Instead, build smart infrastructure around it. When the model generates broken JSON, repair it. When a tool fails, retry with a different approach. When the context fills up, summarize and save to memory. When a task succeeds, remember how for next time.

The result: an agent that runs 100% on my laptop, remembers my preferences across sessions, manages files, runs shell commands, indexes my documents, and creates its own skills when needed.

What's next: vision/image understanding, voice interface, and MCP protocol support.

Would love to hear your feedback! Join us at @qwe_qwe_ai on Telegram.
```

## Gallery Image Descriptions

1. **Hero** — Logo + "Your AI agent that runs 100% offline" + stats: 32+ tools, 4 interfaces, semantic memory, MIT license
2. **Web UI** — Chat interface showing agent using a tool, dark theme
3. **How It Works** — 3 steps: Install (terminal), Pick Model (wizard), Chat (multi-channel)
4. **Architecture** — Visual: User channels → Agent loop → Tools/Memory/RAG/Skills → Local LLM
5. **Memory & Experience** — Memento flow: tasks get saved with outcomes, past experiences inform future decisions

---

## Social Shoutout Posts

### Twitter / X

```
I built an AI agent that runs 100% offline on my gaming laptop.

No cloud. No API keys. No subscriptions.

Qwen 9B + semantic memory + 32 tools + RAG + skill creation from chat.

Terminal, Web UI, and Telegram. Works on RTX 3060 (8GB VRAM) or Mac M1+.

Open source (MIT): github.com/deepfounder-ai/qwe-qwe

#AI #OpenSource #LocalLLM #Privacy
```

### Reddit r/LocalLLaMA

**Title:** `qwe-qwe — open-source AI agent that makes Qwen 9B actually useful (tools, memory, RAG)`

```
Hey r/LocalLLaMA! I've been building a personal AI agent specifically optimized for small local models.

The philosophy: Qwen 9B is smart enough for real work, but it needs infrastructure around it. Raw chat isn't enough — you need tools, memory, and error recovery.

What qwe-qwe does:
- Wraps your local model with 32+ tools (shell, files, web, notes, vault, scheduler)
- Semantic memory via Qdrant (remembers across sessions, auto-injects context)
- Memento experience learning — saves task outcomes, reuses successful approaches
- JSON repair engine — fixes malformed tool calls (common with 9B models)
- RAG with chunk-optimized indexing (800 chars for small context windows)
- Create new skills from chat ("make a habit tracker" → generates + validates + deploys Python)
- 4 interfaces: CLI, Web UI, Telegram bot, LAN
- KV cache optimization — static system prompt blocks first, dynamic data last

Hardware: RTX 3060+ (8GB VRAM), Mac M1+, or anything with 16GB RAM.

Runs via Ollama or LM Studio. Setup wizard auto-detects GPU and pulls the right model.

MIT license: https://github.com/deepfounder-ai/qwe-qwe
Telegram: https://t.me/qwe_qwe_ai

Happy to answer questions about the architecture!
```

### Reddit r/selfhosted

**Title:** `Self-hosted AI agent with semantic memory, RAG, encrypted vault, and Telegram bot — runs 100% offline`

```
Built a self-hosted personal AI agent that runs entirely on your machine. No cloud dependencies, no API keys needed.

Key features:
- Chat via terminal, web browser, or Telegram
- Semantic memory that persists across sessions (Qdrant)
- Index and search your files by meaning (RAG — 30+ formats)
- Encrypted vault for secrets/API keys
- 32+ built-in tools (shell, files, scheduling, notes)
- Create custom skills from natural language
- Docker support: `docker compose up`

Runs on: RTX 3060+ (8GB GPU), Mac M1+, or 16GB RAM machine.
Model: Qwen 9B via Ollama (~5.5GB).
One-command install with setup wizard.

Open source, MIT license.

GitHub: https://github.com/deepfounder-ai/qwe-qwe
Community: https://t.me/qwe_qwe_ai
```

### Hacker News — Show HN

**Title:** `Show HN: qwe-qwe – Offline AI agent that makes 9B models useful`

```
I built an open-source AI agent designed for small local models (Qwen 9B on 8GB VRAM).

The core insight: don't try to make a 9B model as smart as GPT-4. Instead, build smart infrastructure around it:
- JSON repair for malformed tool calls
- Retry loops with self-validation
- Semantic memory (Qdrant) with auto-context injection
- Experience learning (Memento) — saves task outcomes, reinforces success patterns
- Tool budget management (small models degrade with >9 visible tools)
- Smart compaction when context fills up

Features: 32+ tools, RAG file search, skill creation from chat, encrypted vault, scheduler. Accessible via CLI, web UI, Telegram bot.

Stack: Python, FastAPI, SQLite, Qdrant, OpenAI-compatible API (Ollama/LM Studio).

MIT license. One-line install.

https://github.com/deepfounder-ai/qwe-qwe
```

### Telegram @qwe_qwe_ai

```
🚀 qwe-qwe is live on Product Hunt!

Your AI agent that runs 100% offline on your laptop.

What's inside:
🧠 Semantic memory (Memento)
🔧 32+ built-in tools
📚 RAG file search
⚡ Skill creation from chat
📱 CLI + Web UI + Telegram
🔒 100% offline, encrypted vault

Works on RTX 3060+ / Mac M1+ / 16GB RAM.
Open source, MIT license.

👉 Upvote on Product Hunt: [LINK]
⭐ GitHub: github.com/deepfounder-ai/qwe-qwe

Thank you for your support! 🙏
```
