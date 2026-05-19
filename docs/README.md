# Documentation

User guides for castor — one document per feature. If you're looking at castor for the first time, start with the [project README](../README.md) for a tour. The docs here go deeper.

## Getting started

| Document | About |
|---|---|
| [Project README](../README.md) | What castor is, quick-start install, architecture diagram |
| [CONTRIBUTING.md](../CONTRIBUTING.md) | Dev setup, test workflow, release flow |
| [CLAUDE.md](../CLAUDE.md) | What an LLM agent needs to know to navigate the codebase |

## Configuration

| Document | About |
|---|---|
| [PROVIDERS.md](PROVIDERS.md) | LLM provider setup — LM Studio, Ollama, OpenAI, Anthropic (native), Groq, OpenRouter, DeepSeek, Together, Perplexity, Cerebras, Mistral. Where to get keys, how to switch per-thread. |
| [SOUL.md](SOUL.md) | Personality tuning — 8 traits, name, language, custom traits |
| [PRIVACY.md](PRIVACY.md) | What data lives where, telemetry contract, opting in / out |

## Inputs (what you can give the agent)

| Document | About |
|---|---|
| [VOICE.md](VOICE.md) | Live Voice Mode — VAD → STT → LLM → TTS → auto-listen. STT/TTS providers, voice cloning. |
| [CAMERA.md](CAMERA.md) | `camera_capture` tool, PiP overlay, capture-on-send, resolution / quality settings |
| [KNOWLEDGE.md](KNOWLEDGE.md) | Knowledge ingest — 50+ formats via MarkItDown, URL scraping, folder scan, YouTube transcripts |
| [MEMORY.md](MEMORY.md) | How memory works for users — what to save, what gets remembered, recall behaviour |

## Outputs (what the agent can produce)

| Document | About |
|---|---|
| [CANVAS.md](CANVAS.md) | Sandboxed HTML side panel — forms (blocking), dashboards (saveable), mockups |
| Hardware ↓ | Serial / USB-COM output (label printers, PLCs) |

## Capabilities

| Document | About |
|---|---|
| [BROWSER.md](BROWSER.md) | Visible vs headless mode, the 23 browser tools, when the agent uses which |
| [HARDWARE.md](HARDWARE.md) | `serial_port` skill — scales, scanners, GPS, label printers, PLCs over Modbus RTU |
| [SKILLS.md](SKILLS.md) | Skills overview — built-ins, `skill_creator`, [skill import](SKILLS_IMPORT.md) |
| [SKILLS_IMPORT.md](SKILLS_IMPORT.md) | Install community skills from skills.sh / GitHub (Anthropic SKILL.md spec) |
| [MCP.md](MCP.md) | Model Context Protocol — connect external tool servers (filesystem, GitHub, Slack, etc.) |

## Workflows

| Document | About |
|---|---|
| [GOALS.md](GOALS.md) | Long-running autonomous tasks — orchestrator plans, subagents execute, acceptance gate validates. Survives disconnects & restarts. |
| [ROUTINES.md](ROUTINES.md) | Scheduled tasks — cron syntax, debug-via-dialogue, Telegram delivery |
| [PRESET_GUIDE.md](PRESET_GUIDE.md) | Presets — bundled skill + knowledge + workspace setups for specific roles |
| [PRESET_EXAMPLES.md](PRESET_EXAMPLES.md) | Ready-to-install preset gallery |

## Integrations

| Document | About |
|---|---|
| [TELEGRAM.md](TELEGRAM.md) | Bot setup, topic-to-thread mapping, security, slash commands |

## Architecture & design

| Document | About |
|---|---|
| [../ARCHITECTURE.md](../ARCHITECTURE.md) | High-level system map |
| [how-memory-works.md](how-memory-works.md) | Memory architecture — raw / entity / wiki layers, hybrid search, synthesis |
| [knowledge-graph-design.md](knowledge-graph-design.md) | Knowledge graph design — entities, relations, force-directed visualization |
| [agent-loop-v2-design.md](agent-loop-v2-design.md) | Agent loop — text-to-tool extraction, anti-hedge, tool result clearing |
| [adr/](adr/) | Architecture Decision Records |

---

## Want to add a doc?

Each user-facing doc follows this pattern:

1. **One-paragraph elevator pitch** — what the feature does, why you'd use it
2. **Quick start** — minimal example you can copy-paste
3. **Configuration** — env vars + Settings location
4. **Common scenarios** — 2-3 real-world patterns
5. **Limits, gotchas, security notes**
6. **Cross-links** to related docs

Keep design docs separate (see [agent-loop-v2-design.md](agent-loop-v2-design.md) etc. for the convention). User docs answer "how do I use it"; design docs answer "why was it built this way".
