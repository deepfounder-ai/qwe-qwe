"""Tool definitions and execution — optimized for small models."""

import json, subprocess, os
from pathlib import Path
import memory
import logger

_log = logger.get("tools")

# ── Tool definitions — SHORT descriptions, small models need clarity ──

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "memory_search",
            "description": "Search saved memories by query.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_save",
            "description": "Save important info to long-term memory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "What to remember"},
                    "tag": {"type": "string", "description": "Category: user/project/fact/task"},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_delete",
            "description": "Delete a memory by search query. Finds closest match and removes it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search to find memory to delete"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "shell",
            "description": "Run a shell command. Returns stdout+stderr.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to run"},
                    "timeout": {"type": "integer", "description": "Seconds to wait (default 120)"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file's contents.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write content to a file. Creates directories if needed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path"},
                    "content": {"type": "string", "description": "File content"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "schedule_task",
            "description": "Schedule a task to run later or repeatedly. Formats: 'in 5m', 'in 2h', 'every 30m', 'daily 09:00', '14:30'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Short name for the task"},
                    "task": {"type": "string", "description": "What to do when the time comes"},
                    "schedule": {"type": "string", "description": "When: 'in 5m', 'every 1h', 'daily 09:00', '14:30'"},
                },
                "required": ["name", "task", "schedule"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_cron",
            "description": "List all scheduled/cron tasks.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_cron",
            "description": "Remove a scheduled task by its ID number.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer", "description": "Task ID to remove"},
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "switch_model",
            "description": "Switch to a different LLM model or provider. Use when user asks to change model.",
            "parameters": {
                "type": "object",
                "properties": {
                    "model": {"type": "string", "description": "Model name to switch to"},
                    "provider": {"type": "string", "description": "Provider name (lmstudio/openai/groq/etc). Optional."},
                },
                "required": ["model"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "spawn_task",
            "description": "Run a task in background. Use when user gives 2+ tasks at once.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "Task description — what the background worker should do"},
                },
                "required": ["task"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "secret_save",
            "description": "Securely store a secret (password, API key, token). Encrypted in vault.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Secret name (e.g. 'github_token')"},
                    "value": {"type": "string", "description": "Secret value"},
                },
                "required": ["key", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "secret_get",
            "description": "Retrieve a stored secret by name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Secret name"},
                },
                "required": ["key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "secret_list",
            "description": "List all stored secret names (not values).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "secret_delete",
            "description": "Delete a stored secret.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Secret name to delete"},
                },
                "required": ["key"],
            },
        },
    },
    # User profile tools
    {
        "type": "function",
        "function": {
            "name": "user_profile_update",
            "description": "Save a NEW fact about the user (name, timezone, preferences). Only call when you learn something new.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Fact key (e.g. 'name', 'timezone', 'language', 'tech_stack')"},
                    "value": {"type": "string", "description": "Fact value"},
                },
                "required": ["key", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "user_profile_get",
            "description": "Show the user's saved profile.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    # RAG tools
    {
        "type": "function",
        "function": {
            "name": "rag_index",
            "description": "Index a file or directory for search. Supports: txt, md, py, js, json, pdf, etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File or directory path to index"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rag_search",
            "description": "Search indexed files by query. Returns relevant text chunks with file paths.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "limit": {"type": "integer", "description": "Max results (default 5)"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rag_status",
            "description": "Show RAG index status: files and chunks count.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


# ── Tool execution ──

def execute(name: str, args: dict) -> str:
    """Execute a tool and return result as string."""
    try:
        if name == "memory_search":
            results = memory.search(args["query"], tag=args.get("tag"))
            if not results:
                return "No memories found."
            return "\n".join(
                f"[{r['tag']}] (score:{r['score']}) {r['text']}" for r in results
            )

        elif name == "memory_delete":
            results = memory.search(args["query"], limit=1)
            if not results:
                return "No matching memory found."
            point_id = results[0]["id"]
            text_preview = results[0]["text"][:60]
            memory.delete(point_id)
            return f"✓ Deleted memory: {text_preview}..."

        elif name == "memory_save":
            pid = memory.save(args["text"], tag=args.get("tag", "general"))
            return f"Saved (id: {pid[:8]})"

        elif name == "read_file":
            p = Path(args["path"]).expanduser()
            if not p.exists():
                return f"Error: not found: {p}"
            text = p.read_text(encoding="utf-8", errors="replace")
            total_len = len(text)
            if total_len > 8000:
                text = text[:8000] + f"\n... (truncated, {total_len} chars total)"
            if total_len > 4000:
                text += (
                    f"\n⚠️ Large file ({total_len} chars). "
                    f"To modify: edit ONLY the specific part, don't rewrite the whole file. "
                    f"Use shell('sed ...') or write only the changed section."
                )
            return text

        elif name == "write_file":
            p = Path(args["path"]).expanduser()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(args["content"], encoding="utf-8")
            return f"Written {len(args['content'])} chars to {p}"

        elif name == "shell":
            cmd = args["command"]
            _log.info(f"shell: {cmd[:200]}")
            # Block dangerous commands
            blocked = ["sudo ", "rm -rf /", "mkfs", "> /dev/"]
            for b in blocked:
                if b in cmd:
                    _log.warning(f"shell blocked: {cmd}")
                    return f"Blocked: '{b}' not allowed. Use pip (venv) instead of sudo apt."
            t = min(args.get("timeout", 120), 300)
            env = os.environ.copy()
            venv = os.environ.get("VIRTUAL_ENV")
            if venv:
                env["PATH"] = f"{venv}/bin:" + env.get("PATH", "")
            result = subprocess.run(
                args["command"], shell=True, capture_output=True, text=True,
                timeout=t, env=env, stdin=subprocess.DEVNULL  # prevent interactive prompts
            )
            output = result.stdout
            if result.stderr:
                output += f"\nSTDERR: {result.stderr}"
            if result.returncode != 0:
                output += f"\n(exit code: {result.returncode})"
            # Truncate long outputs aggressively for small context models
            if len(output) > 2000:
                output = output[:1000] + "\n...(truncated)...\n" + output[-500:]
            return output.strip() or "(no output)"

        elif name == "schedule_task":
            import scheduler
            result = scheduler.add(args["name"], args["task"], args["schedule"])
            if "error" in result:
                return result["error"]
            repeat_str = " (repeating)" if result["repeat"] else " (one-time)"
            return f"✓ Scheduled '{result['name']}' → next run: {result['next_run']}{repeat_str}"

        elif name == "list_cron":
            import scheduler
            tasks_list = scheduler.list_tasks()
            if not tasks_list:
                return "No scheduled tasks."
            lines = []
            for t in tasks_list:
                repeat = "🔄" if t["repeat"] else "⏱"
                lines.append(f"#{t['id']} {repeat} {t['name']} → {t['next_run']} ({t['schedule']}) | {t['task'][:60]}")
            return "\n".join(lines)

        elif name == "remove_cron":
            import scheduler
            return scheduler.remove(args["task_id"])

        elif name == "switch_model":
            import providers as prov
            result_parts = []
            if args.get("provider"):
                r = prov.switch(args["provider"])
                result_parts.append(r)
            r = prov.set_model(args["model"])
            result_parts.append(r)
            return " | ".join(result_parts)

        elif name == "spawn_task":
            import tasks
            task_id = tasks.spawn(args["task"])
            return f"Task #{task_id} queued: {args['task'][:60]}"

        elif name == "secret_save":
            import vault
            return vault.save(args["key"], args["value"])

        elif name == "secret_get":
            import vault
            val = vault.get(args["key"])
            return val if val else f"Secret '{args['key']}' not found"

        elif name == "secret_list":
            import vault
            keys = vault.list_keys()
            return ", ".join(keys) if keys else "No secrets stored"

        elif name == "secret_delete":
            import vault
            return vault.delete(args["key"])

        elif name == "user_profile_update":
            import db
            key = args["key"].strip().lower().replace(" ", "_")
            db.kv_set(f"user:{key}", args["value"])
            return f"Profile updated: {key} = {args['value']}"

        elif name == "user_profile_get":
            import db
            profile = db.kv_get_prefix("user:")
            if not profile:
                return "No profile data yet."
            lines = [f"- {k.replace('user:', '')}: {v}" for k, v in sorted(profile.items())]
            return "\n".join(lines)

        elif name == "rag_index":
            import rag
            path = Path(args["path"]).expanduser()
            if path.is_dir():
                results = rag.index_directory(str(path))
                indexed = sum(1 for r in results if r["status"] == "indexed")
                total_chunks = sum(r["chunks"] for r in results)
                return f"Indexed {indexed} files, {total_chunks} chunks total"
            else:
                result = rag.index_file(str(path))
                return f"{result['path']}: {result['status']} ({result['chunks']} chunks)"

        elif name == "rag_search":
            import rag
            results = rag.search(args["query"], limit=args.get("limit", 5))
            if not results:
                return "No results found. Try indexing files first with rag_index."
            lines = []
            for r in results:
                lines.append(f"[{r['file_path']}] (score: {r['score']})")
                lines.append(r["text"][:500])
                lines.append("")
            return "\n".join(lines)

        elif name == "rag_status":
            import rag
            s = rag.get_status()
            return f"RAG index: {s['files']} files, {s['chunks']} chunks"

        else:
            # Try skills
            import skills
            result = skills.execute(name, args)
            if result is not None:
                return result
            return f"Unknown tool: {name}"

    except subprocess.TimeoutExpired:
        _log.error(f"shell timeout: {args.get('command', '?')[:100]}")
        return f"Error: command timed out after {args.get('timeout', 120)}s"
    except Exception as e:
        _log.error(f"tool {name} exception: {e}", exc_info=True)
        return f"Error: {type(e).__name__}: {e}"


def get_all_tools(compact: bool = False) -> list[dict]:
    """Get base tools + active skill tools."""
    import skills
    return TOOLS + skills.get_tools(compact=compact)
