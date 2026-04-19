# Creating qwe-qwe Presets

A preset transforms qwe-qwe into a domain specialist — customer support agent, architect assistant, code reviewer, etc. It packages personality, knowledge, tools, and system instructions into a single `.qwp` archive.

## Structure

```
my-preset/
  preset.yaml         # Manifest (required)
  system_prompt.md    # Role + instructions (required)
  knowledge/          # Markdown knowledge base (optional)
    faq.md
    policies.md
  skills/             # Custom Python tools (optional)
    __init__.py
    my_tool.py
  README.md           # Description for marketplace (optional)
```

## Quick Start

1. Create a directory with `preset.yaml` + `system_prompt.md`
2. Test: drag-drop the folder into qwe-qwe Market page
3. Package: `zip -r my-preset.qwp my-preset/`
4. Distribute the `.qwp` file

## preset.yaml — Full Reference

```yaml
schema_version: 1

# ── Identity ──
id: my-business-bot           # lowercase-kebab, 2-64 chars, globally unique
name: My Business Assistant
category: customer-service     # lowercase-kebab (ecommerce, legal, education, etc.)
version: 1.0.0                # SemVer

# ── Author ──
author:
  name: Your Name
  url: https://yoursite.com    # optional
  email: you@example.com       # optional

# ── License ──
license:
  type: free                   # free | commercial | trial
  # For commercial:
  # price:
  #   amount: 29
  #   currency: USD
  #   model: one-time          # one-time | subscription
  #   period: null             # monthly | yearly (for subscription)
  # terms_url: https://yoursite.com/terms

# ── Description ──
description:
  short: "One-line description for marketplace card (10-160 chars)"
  long: |
    Multi-line detailed description.
    Explain what the agent does, what it's good at,
    and who should use it.
  language: en                 # ISO 639-1 (en, ru, es, de, fr, zh, ja, etc.)

# Optional metadata
tags: [support, customer-service, retail]
target_audience:
  - Small business owners
  - Customer support teams
icon: icon.png                 # optional, relative path
screenshots: [screen1.png]    # optional

# ── Soul (personality) ──
soul:
  agent_name: Alex             # Agent's name (shown in chat)
  language: en                 # Response language (ISO code)
  traits:
    humor: low                 # low | moderate | high
    honesty: high
    curiosity: moderate
    brevity: high              # high = concise answers
    formality: moderate
    proactivity: high          # high = takes initiative
    empathy: high
    creativity: low
  # Optional custom traits
  custom_traits:
    - name: patience
      description: "Extremely patient with confused customers"
      level: high

# ── System Prompt ──
# The core instructions that define the agent's role and behavior.
# Use EITHER path (to a file) OR text (inline).
system_prompt:
  path: system_prompt.md       # relative to preset root
  # OR inline:
  # text: "You are a helpful assistant for..."

# ── Skills (custom tools) ──
skills:
  custom:                      # Custom Python skill modules
    - path: skills/my_tool.py
      name: my_tool            # must match Python module name
      description: "What this tool does"
  required_builtin:            # Built-in skills to auto-enable
    - notes                    # notes, browser, timer, etc.

# ── Knowledge Base ──
# Markdown files indexed into RAG on install.
# Agent searches these automatically when answering questions.
knowledge:
  - path: knowledge/faq.md
    title: Frequently Asked Questions
    tags: [faq, general]
  - path: knowledge/policies.md
    title: Company Policies
    tags: [policy, rules]

# ── Installation ──
install:
  steps: []                    # Post-install instructions (shown to user)
  env_vars:                    # Environment variables the preset needs
    - name: COMPANY_NAME
      description: Your company name (used in greetings)
      default: "our company"
      required: false

# ── Compatibility ──
compatibility:
  qwe_qwe_version: ">=0.12.0"
  models:
    recommended: [qwen2.5:7b, llama3.1:8b]
    minimum_params_b: 7        # minimum model size in billions
```

## system_prompt.md — Writing Effective Instructions

This is the most important file. It defines WHO the agent is and HOW it behaves.

```markdown
# Alex — Customer Support Assistant

## Role

You are Alex, a customer support assistant for [Company Name].
Your job is to help customers with orders, returns, and general questions.

## Rules

- Always greet the customer warmly
- Search knowledge base before answering
- If you don't know — say so, don't make up answers
- Escalate complex issues to a human operator
- Never share internal pricing or margins

## Knowledge

You have access to these knowledge bases (search automatically):
- FAQ — common customer questions
- Policies — return, shipping, payment policies

## Response Format

- Keep answers under 3 sentences when possible
- Use bullet points for lists
- Include order numbers when referencing orders

## Escalation

When to escalate to human operator:
- Customer is angry after 2 failed resolution attempts
- Legal or compliance questions
- Refund over $500
- Technical issues with the website
```

### Tips

- **Be specific** — "answer in 2-3 sentences" is better than "be concise"
- **Give examples** — show the agent what a good response looks like
- **Define boundaries** — what the agent should NOT do is as important as what it should
- **Don't disable tools** — the system ensures core tools (memory, browser, shell, files) remain available regardless of preset instructions

## Knowledge Files

Markdown files in `knowledge/` are indexed into RAG (vector search) when the preset is installed. The agent searches them automatically when answering questions.

### Format

```markdown
# Shipping Policy

## Domestic Shipping
- Standard: 5-7 business days, $5.99
- Express: 2-3 business days, $12.99
- Free shipping on orders over $50

## International Shipping
- Europe: 10-14 business days, $19.99
- Asia: 14-21 business days, $24.99

## Tracking
All orders include tracking. Tracking number is sent via email within 24 hours.
```

### Tips

- Use headers for sections (RAG chunks by headers)
- Keep each file focused on one topic
- Include concrete data (prices, timelines, contact info)
- Write in the same language as `description.language`

## Custom Skills (Tools)

Python modules in `skills/` that give the agent new capabilities.

### Skill Template

```python
"""My custom tool — does something useful."""

DESCRIPTION = "Short description for skill list"

INSTRUCTION = """When to use this tool:
- Call my_tool when user asks about X
- Parameters: query (required), limit (optional)
"""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "my_tool",
            "description": "What this tool does — shown to the model",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query"
                    },
                    "limit": {
                        "type": "number",
                        "description": "Max results (default 5)"
                    },
                },
                "required": ["query"],
            },
        },
    },
]


def execute(name: str, args: dict) -> str:
    """Execute the tool. Returns result string."""
    if name == "my_tool":
        query = args.get("query", "")
        limit = int(args.get("limit", 5))
        # Your logic here
        return f"Found {limit} results for '{query}'"
    return f"Unknown tool: {name}"
```

### Important

- Tool names must be `snake_case`
- `execute()` must return a string (never raise)
- Keep tool descriptions short — they consume model context tokens
- Don't import heavy libraries at module level (use lazy imports inside `execute`)

## Packaging

```bash
# From the preset directory
cd my-preset/
zip -r ../my-preset.qwp .

# The .qwp file can be:
# - Dragged into qwe-qwe Market page
# - Shared with others
# - Published to qwe-qwe marketplace
```

## Testing

1. **Local directory**: drag-drop the folder into Market page — installs without packaging
2. **Activate**: click "Activate" on the preset card
3. **Test**: chat with the agent — verify personality, knowledge search, custom tools
4. **Check system prompt**: agent should introduce itself with preset name/role
5. **Deactivate**: click "Deactivate" — agent should return to default personality

## Validation

qwe-qwe validates presets on install:
- YAML schema check (all required fields, correct types)
- File existence (all referenced paths must exist inside preset dir)
- Security: no path traversal (all paths confined to preset directory)
- Size limits: total archive < 50MB

## Lifecycle

```
Install (.qwp → ~/.qwe-qwe/presets/<id>/)
    ↓
Activate
    ├── Backup current soul
    ├── Apply soul traits (name, language, traits)
    ├── Index knowledge (RAG with tag preset:<id>)
    ├── Enable custom skills
    └── Set system_prompt suffix
    ↓
Active (agent uses preset personality + knowledge + tools)
    ↓
Deactivate
    ├── Restore soul from backup
    ├── Disable preset skills
    └── Clear system_prompt suffix
    ↓
Uninstall
    ├── Deactivate (if active)
    ├── Delete RAG chunks (by tag + file_path)
    └── Remove files + DB row
```

## Example Presets

See `~/.qwe-qwe/presets/ecom-support-ru/` for a complete working example with knowledge base and custom skills.
