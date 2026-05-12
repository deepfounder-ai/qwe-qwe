<p align="center">
  <img src="static/logo.png" alt="qwe-qwe" width="280">
</p>

<h3 align="center">Business-oriented AI agent</h3>

<p align="center">
  Self-hosted AI agent ready to drop into business workflows. Bring any OpenAI-compatible LLM — Azure OpenAI, AWS Bedrock, OpenAI, Groq, OpenRouter, or a local model on your own hardware. Your data, your provider, your rules.
</p>

<p align="center">
  <a href="#quick-start">Quick Start</a> •
  <a href="#interfaces">Interfaces</a> •
  <a href="#tool-search">Tool Search</a> •
  <a href="#tools">Tools</a> •
  <a href="#skills">Skills</a> •
  <a href="#mcp">MCP</a> •
  <a href="#telegram-bot">Telegram</a> •
  <a href="#diagnostics">Doctor</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/version-0.20.0-blue" alt="version">
  <img src="https://img.shields.io/badge/python-3.11+-green" alt="python">
  <img src="https://img.shields.io/badge/platform-linux%20%7C%20macos%20%7C%20windows-lightgrey" alt="platform">
  <img src="https://img.shields.io/badge/license-MIT-orange" alt="license">
  <a href="https://t.me/qwe_qwe_ai"><img src="https://img.shields.io/badge/community-Telegram-blue?logo=telegram" alt="Telegram"></a>
</p>

---

## What is qwe-qwe?

A **business-oriented AI agent** built to drop into real workflows: customer ops, internal automation, knowledge retrieval, scheduled reporting, custom integrations, **hardware on the floor**, and **rich UI in chat** (forms, dashboards, mockups). Deploys on your infrastructure — a workstation, your own server, or the cloud account you already have. Chat via web UI, terminal, or Telegram, with tools, semantic memory, browser control, MCP integrations, a cron-like scheduler, direct USB/serial access to scales, scanners, GPS, label printers, and PLCs — and a sandboxed canvas panel where the agent can render arbitrary HTML for visual artifacts.

**Bring your own LLM**: works with any OpenAI-compatible provider — Azure OpenAI, AWS Bedrock, OpenAI, Groq, OpenRouter, DeepSeek, Together — or a local model via LM Studio / Ollama if you need everything on-prem. Your provider, your context window, your budget. Switch providers per-thread without restarting the agent.

> **Philosophy**: the system around the LLM should do the heavy lifting. Tool search keeps the prompt lean, recall keeps state out of the conversation, scheduler runs work without you, skills extend capability without redeploys. The result is an agent that's reliable on whatever model you pick — small enough to run on a laptop or large enough to handle complex multi-step tasks.

## Why qwe-qwe

| | qwe-qwe | Hosted SaaS agents |
|---|---|---|
| **Data** | Stays on your infrastructure | Sent to the vendor |
| **LLM choice** | Any OpenAI-compatible provider | Locked to vendor's model |
| **Customization** | Full code + soul + skills | System prompt + few hooks |
| **Cost model** | Your existing LLM bill, no per-seat | Per-seat / per-action SaaS pricing |
| **Compliance** | Self-hosted = your audit trail | Vendor's compliance posture |
| **Extensibility** | Skills, MCP, custom tools | Vendor's marketplace |
| **Hardware access** | Native USB / serial — scales, scanners, GPS, PLCs | None (cloud agents can't see your floor) |
| **Reliability** | No vendor outages or rate limits | Vendor SLA |

## Quick Start

### Prerequisites

- **Python 3.11+**
- **An LLM endpoint** — pick one:
  - **Hosted** (any OpenAI-compatible API): Azure OpenAI, AWS Bedrock, OpenAI, Groq, OpenRouter, DeepSeek, Together. Set `QWE_LLM_URL` + `QWE_LLM_KEY` and you're done.
  - **Local** (data stays on-prem): [LM Studio](https://lmstudio.ai) or [Ollama](https://ollama.ai) with any tool-capable model. Qwen 9B / Gemma 4B work well on a single consumer GPU; bigger models if you have the hardware.
- **Embeddings**: FastEmbed (ONNX, local, CPU) — multilingual-MiniLM (384d, 50+ languages) + SPLADE++. Runs comfortably on a laptop without a GPU.

### Install

Runs natively on **Linux**, **macOS** (Intel & Apple Silicon) and **Windows 10/11** — single `pip install -e .` pulls every runtime dep (including MarkItDown, python-docx/pptx, openpyxl, pdfminer.six, pypdf, fastembed, qdrant-client, uvicorn).

#### 🐧 Linux / 🍎 macOS — one-line

```bash
curl -fsSL https://raw.githubusercontent.com/deepfounder-ai/qwe-qwe/main/install.sh | bash
```

This clones the repo, creates a venv, installs everything, verifies critical deps, pre-downloads the embedding model, and drops `qwe-qwe` on your `$PATH`.

#### 🪟 Windows

```cmd
git clone https://github.com/deepfounder-ai/qwe-qwe.git
cd qwe-qwe
setup.bat
```

On Windows shell commands are routed through **Git Bash** (auto-detected at install time — install [Git for Windows](https://git-scm.com/download/win) if missing). Falls back to `cmd.exe` if not found.

#### Manual (any platform)

```bash
git clone https://github.com/deepfounder-ai/qwe-qwe.git
cd qwe-qwe

# Create venv
python3 -m venv .venv            # or `python -m venv .venv` on Windows
source .venv/bin/activate        # macOS/Linux
# .venv\Scripts\activate         # Windows PowerShell / cmd

# Install package + all runtime deps
pip install -e .

# Verify everything is wired
qwe-qwe --doctor
```

#### Update an existing install

```bash
# Linux / macOS
curl -fsSL https://raw.githubusercontent.com/deepfounder-ai/qwe-qwe/main/install.sh | bash

# Any platform, inside the checkout:
git pull && pip install -e . --upgrade
```

The update script is idempotent — re-running it detects an existing checkout and refreshes deps.

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

### System requirements

For **hosted-LLM** deployments, qwe-qwe itself is light — any modern laptop or small VM works (the agent process is ~300MB resident, plus Qdrant on disk for memory).

For **local-LLM** deployments where the model runs on the same machine:

| Component | Minimum | Recommended |
|-----------|---------|-------------|
| GPU | 4GB VRAM (4B Q4) | 8GB VRAM (9B Q4_K_M) or larger |
| RAM | 8GB | 16GB |
| Storage | 10GB | 20GB (models + memory) |

Works on: gaming laptops, desktop GPUs (RTX 3060+), Mac M1+ (via Ollama), Linux servers.

## Architecture

```
                               +-- Qdrant (semantic memory, hybrid search)
CLI (terminal)  <--+           +-- RAG (file indexing & search)
Web UI (browser) <--+-- Agent -+-- SQLite (history, threads, state)
Telegram bot    <--/    Loop   +-- Tools (8 core + tool_search)
                        |      +-- Skills (9 built-in, user-creatable)
                        |      +-- Browser (Playwright/Chromium)
                        |      +-- MCP (external tool servers)
                        |      +-- Scheduler (cron tasks)
                        |      +-- Vault (encrypted secrets)
                        |      +-- Hardware (USB/serial via pyserial — scales,
                        |                    scanners, GPS, PLCs, sensors)
                        |      +-- Canvas (sandboxed HTML side panel — forms,
                        |                  dashboards, mockups)
                        v
                   LLM (local or cloud)
                   10 providers supported
```

### Engineering around the LLM

These are the techniques the agent uses to stay reliable across model sizes — they make small models capable enough for production work *and* keep large models cheap by burning fewer tokens per turn:

- **Tool Search** — only 8 core tools loaded by default (~750 tokens); model calls `tool_search("keyword")` to activate more. Saves **75% tokens** vs loading all 49 tools
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
qwe-qwe --web                    # http://localhost:7860
qwe-qwe --web --ssl --port 7861  # HTTPS (needed for mic/camera)
```

Premium single-file SPA — **zero runtime JS dependencies** (no React, no CDN build). Linear / Vercel / Anthropic-Console aesthetic with Geist + Instrument Serif + Geist Mono type stack.

**Shell**
- 56-px icon rail (left) → chat / memory / scheduler / presets / settings
- 264-px thread list with rename + delete inline actions
- Editorial chat canvas (centered, 780 px)
- Right-side **Inspector**: context-window gauge, INPUT / OUTPUT token cards, sparkbars (tokens-per-turn), recalled memories (`/api/knowledge/search` on last user prompt), active tools, latency bars
- **⌘K command palette** + Gmail-style **Alt+letter** nav shortcuts
- Keyboard cheatsheet modal (`Shift+?`)

**Chat fidelity**
- Streaming without flicker — in-place DOM patches, targeted updates, never full re-render during a turn
- **Tool calls grouped by 11 categories** (memory / knowledge / files / shell / browser / web / vision / voice / automation / skills / orchestration), each expandable for full JSON input + output
- **Markdown** rendering (H1–H6, bold / italic / strike, inline code, blockquote, lists, links)
- **Code blocks** with line-number gutter, filename + language label, copy button
- **Thinking** block as collapsible `<details>` after the turn ends
- **Regenerate** = clean restart — server deletes the last user→assistant turn so the model has no idea it's a regeneration
- Persistent attachments — images + files saved to message meta, survive server restart

**Memory / Knowledge**
- Drag-drop upload supporting **50+ formats** (see [Knowledge ingest](#knowledge-ingest))
- URL scraping via MarkItDown
- Folder scan — preview + batch index
- Interactive knowledge graph (force-directed SVG) with hover edge highlights + search filter

**Mobile**
- iPhone safe-area insets on all 4 sides
- Bottom tab bar replaces rail
- Slide-in drawer for thread list
- Composer textarea at 16 px (no iOS auto-zoom)
- `100dvh` viewport, honors URL bar + home indicator

**Settings** — 17 tabs grouped into Agent / I/O / Automation / System (Model, Soul, Tools, Memory, Voice, Camera, Telegram, MCP, Heartbeat, Inference, Network, Privacy, Appearance, Advanced, Account). Advanced sub-tabs expose all 30+ `EDITABLE_SETTINGS` as forms. **Abort** button stops runaway turns; **login modal** handles password-protected installs.

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

49 tools total across core + extensions + skills:

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
| **Hardware (serial)** | `serial_list_ports`, `serial_read_once`, `serial_write` | Search |

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
| `serial_port` | Talk to USB-serial / RS-232 / RS-485 hardware (scales, scanners, GPS, label printers, PLCs over Modbus RTU) — Windows / macOS / Linux |
| `canvas` | Render HTML in a sandboxed right-side panel — forms (blocking, returns user input), dashboards (saveable), mockups / prototypes |

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

## Hardware

qwe-qwe runs on a real machine — and your machine is plugged into real hardware. Cloud agents can't see your floor; qwe-qwe can. The built-in **`serial_port`** skill talks USB-serial / RS-232 / RS-485 to the universe of business devices that show up on every loading dock, lab bench, retail counter, and factory line.

Cross-platform via `pyserial`: same API on **Windows** (`COM3`), **macOS** (`/dev/tty.usbserial-…`), and **Linux** (`/dev/ttyUSB0`).

**Tools** (activated via `tool_search("serial")` / `"scale"` / `"modbus"` / `"rfid"` / `"barcode"` / `"gps"` / `"plc"` / `"hardware"`):

| Tool | Purpose |
|---|---|
| `serial_list_ports` | Enumerate plugged-in devices with description, manufacturer, VID:PID |
| `serial_read_once` | Open → read one frame (line / N bytes / timeout) → close. Text or hex. |
| `serial_write` | Open → write → close. **Gated by `confirm=true`** — writes to PLCs / actuators can damage equipment. |

### What you can plug in

| Category | Examples | Use case |
|---|---|---|
| **Weighing scales** | Mettler Toledo, CAS, Ohaus | Warehouse: "weigh pallet → ID via barcode → log to inventory + print label" |
| **Barcode / RFID readers** | Datalogic, Honeywell (RS-232), UHF inventory readers | Receiving, stocktake, asset tracking, real-time inventory |
| **GPS modules** | u-blox NEO-6M / 9M | Fleet tracking, geofence-into-warehouse triggers |
| **Label printers** | Zebra (ZPL), TSC, Godex (ESC/P) | "Print this shipping label for the order I just packed" |
| **Receipt printers** | Epson / Star / Bixolon (ESC/POS) | POS, kitchen display tickets |
| **Industrial PLCs** | Anything Modbus RTU over RS-485 | Factory automation — read sensors, command motors, monitor production lines |
| **VFDs / inverters** | Modbus-RTU drives | Pump and motor control |
| **Environmental sensors** | pH, conductivity, gas, humidity, temperature | Greenhouses, labs, climate-controlled storage |
| **Energy meters** | RS-485 with DLMS/COSEM | Plant-floor power monitoring, billing |
| **Cash drawers / fingerprint scanners / SCUD turnstiles** | Standard serial-trigger devices | POS, time tracking, access control |

### Concrete scenarios that just work

**Weighing flow with Telegram alerts:**

```
You: I have Mettler Toledo scales on COM3 baud 9600. When I weigh
     something, ask me what it is via Telegram and log it.
Agent: [tool_search("scale")] → activates 3 serial tools
       [serial_list_ports] → confirms "USB-Serial CH340 (COM3)"
       [serial_read_once port=COM3 baud=9600 until="newline"]
         → "ST,GS,+00012.345,kg"
       [telegram_notify_owner "I just weighed 12.345 kg — what was it?"]
       [memory_save "Weighed 12.345 kg of [user reply] on 2026-05-09 14:32"]
       Done.
```

**Modbus RTU on a VFD with safety:**

```
You: Read the current frequency setpoint from the VFD on COM5
     (Modbus address 1, register 0x2000).
Agent: [serial_read_once] → constructs Modbus frame in hex,
       decodes the response → "Setpoint is 50.0 Hz (raw 0x01F4)"

You: Now set it to 30 Hz.
Agent: [serial_write data="01 06 20 00 01 2C ..." confirm=false]
       (DRY RUN — pass confirm=true to actually send.
        port: COM5, bytes: 8, hex: 0106200001 2C...)
       This will write 30.0 Hz (0x012C) to register 0x2000.
       Confirm to proceed.
You: yes go ahead
Agent: [serial_write ... confirm=true] → "Wrote 8 bytes to COM5."
```

The `confirm=true` gate is mandatory on every actuator write — set incorrectly, a PLC write can physically damage equipment, so the agent always shows a hex preview first and requires explicit confirmation.

**GPS-triggered routine:**

```
You: every 5 min read the GPS on COM4 and if I'm within 200m of
     the warehouse coordinates (52.0123, 4.5678) ping me on Telegram.
Agent: [tool_search("schedule")] [tool_search("gps")]
       [schedule_task every="5m"] running:
         serial_read_once port=COM4 until="newline"
         parse $GPRMC, compute haversine, telegram_notify_owner if <200m
       Done. Routine #4 scheduled.
```

### Setup notes

- **Linux**: serial reads need `dialout` group membership. `qwe-qwe --doctor` flags this and prints the fix: `sudo usermod -aG dialout $USER` then re-login.
- **macOS**: FTDI cables work out of the box. CH340 / CH341 cables on Apple Silicon need the [WCH driver](https://www.wch.cn/downloads/CH34XSER_MAC_ZIP.html).
- **Windows**: drivers usually auto-install via Windows Update. CH340 may need the [WCH driver](https://www.wch.cn/download/CH341SER_EXE.html); FTDI cables are plug-and-play.

For non-serial hardware (Zigbee, Z-Wave, Wi-Fi, MQTT), the practical bridge is **Home Assistant** — adding an HA MCP server through `mcp_manager` exposes its 2000+ integrations. Pattern docs in [`docs/HARDWARE.md`](docs/HARDWARE.md).

## Canvas

Rich UI in chat. The built-in **`canvas`** skill renders model-supplied HTML in a sandboxed right-side panel — 480px wide, mutually exclusive with the inspector. Three use cases:

| Use case | Tool | Behavior |
|---|---|---|
| **Interactive forms** | `canvas_prompt(html, title?)` | BLOCKS the agent's turn until the user submits the form. Returns the submitted data as JSON. Mirrors `camera_capture`'s blocking pattern. |
| **Dashboards** | `canvas_render(html, title?)` then `canvas_save(slug, title, html)` | Fire-and-forget render, then save under a slug. Saved dashboards appear in the **Canvases** left-nav view and survive reloads. |
| **Mockups / prototypes** | `canvas_render(html, title?)` | Visual communication. Agent renders a layout; user gives feedback in chat; agent iterates. |

### Concrete scenario

```
You:    Make a form to add a new client. Fields: full name, phone, source
        (Google / Referral / Other).
Agent:  [tool_search("form")] → activates canvas_prompt
        [canvas_prompt html="<form>...</form>" title="New client"]
        → panel opens on the right (480px), inspector auto-closes
        → user fills + clicks Save
Agent:  Receives {full_name: "Anna", phone: "+7 ...", source: "Google"}
        [memory_save "Client Anna · +7 ... · via Google · 2026-05-11"]
        Saved.
```

### Security

The iframe is **rigorously sandboxed**: `<iframe sandbox="allow-scripts allow-forms">` — **no `allow-same-origin`**. Iframe origin is `"null"`, so model-generated HTML cannot read parent cookies, localStorage, or DOM. The only channel back to the agent is `parent.postMessage({type:'canvas_submit', data: {...}}, '*')`, filtered by iframe identity. External CDN resources (Chart.js, fonts) work, but without the user's auth.

HTML cap: 256 KB per artifact, enforced at both skill entry and REST POST. Pattern docs + reference HTML template + postMessage protocol: [`docs/CANVAS.md`](docs/CANVAS.md).

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

## Knowledge ingest

The knowledge base ingests **50+ formats** via Microsoft **MarkItDown** (primary) with stdlib fallbacks (pinned as hard deps — no silent degradation on fresh installs):

| Category | Formats |
|---|---|
| **Documents** | PDF · DOCX · PPTX · XLSX · EPUB · ODT · RTF · Jupyter notebooks (`.ipynb`) |
| **Web** | HTML · any `https://…` URL (MarkItDown handles fetch + markdown conversion) |
| **Data** | JSON · CSV · TSV · YAML · TOML · XML · INI · ENV |
| **Code** | Python, JS/TS, Go, Rust, Java/Kotlin/Scala, C/C++, Ruby, PHP, SQL, GraphQL, 40+ extensions total |
| **Markup** | Markdown · reStructuredText · AsciiDoc · TeX |
| **Images** | PNG · JPG · WEBP — via vision pipeline |

### Three ways to ingest

1. **Drop or pick files** — Memory tab upload-zone → batch upload + index
2. **Paste URL** — `POST /api/knowledge/url` fetches, converts to markdown, indexes under `source:url` tag
3. **Scan folder** — preview first (lists indexable files with size/method), then index all in one pass

Each source is stored under `~/.qwe-qwe/uploads/kb/<slug>_<name>`, chunked into ~800-char pieces, embedded + dense-vector-indexed in Qdrant, and queued for the nightly **synthesis** job that extracts entities + wiki pages from the content.

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

## Routines (scheduled tasks)

A routine is a chat thread with a schedule attached. Each firing appends a new user + assistant turn to the same thread — tool calls, thinking steps, and replies all visible as a normal conversation. Corrections you add between firings become context the agent sees on the next run, so you can debug a misbehaving routine through dialogue.

**Schedule syntax** (natural, no 5-field cron):

```
"in 5m"             -> run once in 5 minutes
"every 2h"          -> repeat every 2 hours
"every 30m"         -> repeat every 30 minutes
"every 2 days 09:00" -> repeat every 2 days at 09:00
"daily 09:00"       -> every day at 09:00
"weekdays 09:00"    -> Mon-Fri at 09:00  (alias: "будни")
"weekends 10:00"    -> Sat-Sun at 10:00  (alias: "выходные")
"mon,wed,fri 14:30" -> those days at 14:30  (alias: "пн,ср,пт")
"14:30"             -> once today/tomorrow at 14:30
```

**UI controls**:
- Routines tab → New routine modal (frequency picker with day chips + time)
- ▶ Run now button fires out-of-schedule; ⏸ Pause / Resume skips firings without deleting
- Four-state status badge: running / active / last run failed / paused
- Routine's thread opens with one click from the card; deleting either side cascades

**Tools available inside routines**: everything in the core tool set, plus `telegram_notify_owner(text)` for one-call sends to the verified owner (no token/chat-id wrangling).

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

Checks 20+ system components: Python, dependencies, SQLite, Qdrant, provider, LLM API, model loaded, embeddings, inference latency, agent loop v2, MCP servers, browser skill, Telegram, threads, skills, tools, cron/heartbeat, STT/TTS, files indexed, knowledge graph (entities/wiki), synthesis cron, BM25 index, disk space, logs.

## Config

Environment variables:

```bash
QWE_LLM_URL=http://localhost:1234/v1   # LLM server URL
QWE_LLM_MODEL=qwen/qwen3.5-9b          # Model name
QWE_LLM_KEY=lm-studio                  # API key
QWE_DB_PATH=~/.qwe-qwe/qwe_qwe.db      # Database path
QWE_DATA_DIR=~/.qwe-qwe                # Where threads / memory / uploads live
QWE_QDRANT_MODE=disk                   # memory | disk | server
QWE_PASSWORD=                          # Web UI password (shows login modal if set)
QWE_STT_DEVICE=cpu                     # STT inference device (cpu | cuda)
```

Everything else (30+ knobs — `context_budget`, `rag_chunk_size`, `synthesis_time`, `tts_api_url`, etc.) lives in **Settings → Advanced → Settings** and persists in SQLite.

### Data layout

All user data in `~/.qwe-qwe/` (configurable via `QWE_DATA_DIR`):

```
qwe_qwe.db        SQLite — messages, threads, KV, settings
memory/           Qdrant vectors (disk mode)
wiki/             Synthesized markdown pages
skills/           User-created skills
uploads/          Images, documents, camera captures
  kb/             Knowledge-base files awaiting / done indexing
workspace/        Default CWD for relative paths (switches per-preset)
presets/<id>/     Installed presets (each with own workspace/, knowledge/, skills/)
logs/             qwe-qwe.log (INFO+), errors.log (WARNING+)
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

## Contributing

**Contributions welcome.** qwe-qwe is a small open project — your PR won't get lost in a queue.

- 📘 Read [CONTRIBUTING.md](CONTRIBUTING.md) for setup + workflow
- 🏗️ See [ARCHITECTURE.md](ARCHITECTURE.md) for the big picture
- 🐛 [Open an issue](../../issues/new/choose) if you found a bug or want a feature
- 💬 [Start a Discussion](../../discussions) for questions and workflow sharing
- 🔒 [Security vulnerabilities](SECURITY.md) — private report via GitHub Security Advisory
- 🤝 Everyone is expected to follow the [Code of Conduct](CODE_OF_CONDUCT.md)

### Good first issues

If you want to help but don't know where to start, we label easy tasks as [`good first issue`](../../issues?q=is%3Aopen+is%3Aissue+label%3A%22good+first+issue%22). Typical starting points:

- Add a new [skill](skills/) (weather, notes, timers — each is 50-100 lines of Python)
- Add a new [provider](providers.py) preset (`PRESETS` dict — ~5 lines)
- Improve [doctor checks](cli.py) — add detection for a new subsystem edge case
- Write [integration tests](tests/test_integration.py) for a 0%-covered module (check `pytest --cov`)

### What I'm NOT looking for

Be upfront so we don't waste each other's time:

- Cloud-first features that don't work offline
- Rewrites of the single-file web UI to React/Vue/Svelte
- Splitting `server.py` for the sake of splitting (until it's actually causing pain)
- Generic LLM wrapper features that exist in 20 other projects

### Housekeeping

Dependencies are tracked by [Dependabot](.github/dependabot.yml) — weekly grouped PRs for pip (minor + patch bundled) and monthly PRs for GitHub Actions land in the inbox. Security updates bypass the grouping and open their own PR immediately.

## Community

- 💬 [Telegram — @qwe_qwe_ai](https://t.me/qwe_qwe_ai) — quick chat, show-and-tell, release announcements
- 💭 [GitHub Discussions](../../discussions) — long-form questions, workflow sharing
- ⭐ If qwe-qwe is useful — **star the repo**. It's the clearest signal we're on the right track.

## License

MIT

---

<p align="center">
  Built with care by <a href="https://deepfounder.ai">DeepFounder</a>
</p>
