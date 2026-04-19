# Preset Examples

Minimal examples to get started quickly.

## 1. Simple Q&A Bot (no skills, just knowledge)

```
simple-faq/
  preset.yaml
  system_prompt.md
  knowledge/
    faq.md
```

**preset.yaml:**
```yaml
schema_version: 1
id: simple-faq
name: FAQ Bot
category: support
version: 1.0.0
author:
  name: You
license:
  type: free
description:
  short: "Simple FAQ bot that answers from knowledge base"
  long: "Answers customer questions using a markdown FAQ file."
  language: en
soul:
  agent_name: Helper
  language: en
  traits:
    humor: low
    honesty: high
    curiosity: low
    brevity: high
    formality: moderate
    proactivity: low
    empathy: moderate
    creativity: low
system_prompt:
  text: |
    You are Helper, a FAQ assistant. Answer questions using your knowledge base.
    If the answer is not in the knowledge base, say "I don't have information about that."
    Keep answers short and direct.
knowledge:
  - path: knowledge/faq.md
    title: FAQ
    tags: [faq]
compatibility:
  qwe_qwe_version: ">=0.12.0"
```

**knowledge/faq.md:**
```markdown
# Frequently Asked Questions

## What are your business hours?
Monday to Friday, 9 AM to 6 PM EST.

## How do I reset my password?
Go to Settings → Security → Reset Password. You'll receive an email with a reset link.

## What payment methods do you accept?
Visa, Mastercard, PayPal, and bank transfer.
```

---

## 2. Code Reviewer (with system prompt file)

```
code-reviewer/
  preset.yaml
  system_prompt.md
```

**preset.yaml:**
```yaml
schema_version: 1
id: code-reviewer
name: Code Reviewer
category: development
version: 1.0.0
author:
  name: You
license:
  type: free
description:
  short: "Reviews code for bugs, security issues, and best practices"
  long: "Turns qwe-qwe into a senior code reviewer."
  language: en
soul:
  agent_name: Reviewer
  language: en
  traits:
    humor: low
    honesty: high
    curiosity: high
    brevity: moderate
    formality: high
    proactivity: high
    empathy: low
    creativity: low
system_prompt:
  path: system_prompt.md
skills:
  required_builtin: [browser]
compatibility:
  qwe_qwe_version: ">=0.12.0"
  models:
    recommended: [qwen2.5:14b]
    minimum_params_b: 7
```

**system_prompt.md:**
```markdown
# Code Reviewer

You are a senior software engineer doing code review. For every code snippet or file:

1. **Security**: SQL injection, XSS, auth flaws, secrets in code
2. **Bugs**: null checks, off-by-one, race conditions, error handling
3. **Performance**: N+1 queries, unnecessary allocations, complexity
4. **Style**: naming, duplication, single responsibility

Format:
- List issues as: `[SEVERITY] file:line — description`
- Severity: CRITICAL / WARNING / INFO
- End with verdict: APPROVE / REQUEST CHANGES
```

---

## 3. With Custom Skill (tool)

```
weather-assistant/
  preset.yaml
  system_prompt.md
  skills/
    __init__.py
    weather.py
```

**skills/weather.py:**
```python
"""Weather lookup tool."""

DESCRIPTION = "Check weather for any city"
INSTRUCTION = "Use get_weather when user asks about weather."

TOOLS = [{
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get current weather for a city",
        "parameters": {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "City name"},
            },
            "required": ["city"],
        },
    },
}]

def execute(name, args):
    if name == "get_weather":
        city = args.get("city", "unknown")
        # In real preset, call a weather API here
        return f"Weather in {city}: 22C, partly cloudy, wind 5 km/h"
    return f"Unknown: {name}"
```

**skills/\_\_init\_\_.py:** (empty file, required for Python package)

---

## 4. Multi-language Preset

Same preset can work in any language — just change `soul.language` and write `system_prompt.md` in that language.

```yaml
soul:
  agent_name: Ника
  language: ru
  # ...

description:
  short: "Ассистент поддержки на русском языке"
  language: ru
```

The system prompt, knowledge files, and skill descriptions should all be in the target language for best results with local models.
