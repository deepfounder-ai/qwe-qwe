# v0.17.12 — Memory discipline: stop saving conversational noise

User reported the agent was saving useless stuff to long-term memory:

- Right after "hi, be my business assistant" → saved `user wants me to be a business assistant, need to ask about his business domain, tasks, goals, what functions he needs…`
- On a "clean up memory" task → saved `[EXP] Task: надо подчистить память | Tools: memory_search, self_config | Steps: 3 | Result: success | Learned: Вижу, что в памяти сейчас: 📦 Что хранится: 6 записе…`

Both are noise that pollutes recall quality. Fixed on two fronts:

## 🔧 What changed

### 1. `memory_save` tool description is now prescriptive

Before:
> Save info to long-term memory. Long texts auto-chunked for knowledge graph.

Now:
> Save a **DURABLE FACT** to long-term memory. Call this ONLY when: (1) user explicitly says remember/запомни/save, OR (2) you learned a stable fact about the user (name, role, location, stack, preferences, deadlines, project constants) that will matter in future conversations. **DO NOT save**: conversational intents ("user wants X"), current session plans, task lists, acknowledgments ("user said hi"), transient requests, your own reasoning, or what you're about to do. Rule of thumb: if it won't be useful a week from now, don't save it.

Also removed `task` from the list of suggested tags — tasks belong in the scheduler, not in memory.

### 2. Soul rule 8 rewritten

Before:
> Memory: search before saving (avoid duplicates). Tags: user, project, fact, task, decision, idea.

Now:
> **MEMORY DISCIPLINE — default is DO NOT SAVE.** Call memory_save ONLY for (a) user explicit "remember"/"запомни", (b) durable facts that matter weeks later (user name/role/stack/preferences, committed decisions, stable project info). **NEVER save**: intents ("user wants…"), session plans, current tasks, greetings, your own reasoning, "need to learn more about…", TODO lists. **Ask yourself: will this matter in a week? If not, skip.**

### 3. Experience auto-save filters noise

`_save_experience()` fired after every tool-using turn — including memory-cleanup and meta tasks. It now skips:

- **Meta-only tool sets** — when the whole turn used only memory/self-config/tool-search/list-* tools. Saving "I searched memory" as an experience is circular.
- **Memory-topic user inputs** — keywords like `память`, `memory`, `запомни`, `forget`, `clean memory`, `recall` in the user message skip save. Experiences about managing memory poison the recall pool.
- **Trivial single-round turns** — one tool round + reply under 80 chars = nothing worth remembering.

Verified with unit tests:

| Case | Tools | Input | Rounds | Saved? |
|---|---|---|---|---|
| Memory cleanup | `memory_search, self_config` | "надо подчистить память" | 3 | ❌ |
| Real task | `read_file, shell, write_file` | "write a CSV sorter" | 3 | ✅ |
| Meta-only | `memory_search, memory_save` | "do some unrelated thing" | 2 | ❌ |
| Trivial | `read_file` | "open the config" (reply: "OK.") | 1 | ❌ |

## 📦 Upgrade

```bash
git pull && pip install -e . --upgrade
# Restart the server
```

If you already have junk in memory from before this fix: **Memory → Knowledge graph → Clear graph** wipes the synthesized layer. For raw saves, search for `[EXP]` entries or conversational summaries in the Memory tab and delete individually (or `memory_delete` via the agent).

🤖 Generated with [Claude Code](https://claude.com/claude-code)
