"""Tool definitions and execution — optimized for small models."""

import json, subprocess, os
from pathlib import Path
import memory

# ── Tool definitions — SHORT descriptions, small models need clarity ──

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "memory_search",
            "description": "Search memories about user, past conversations, or saved facts.",
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
            "description": "Save important info: user preferences, facts, decisions.",
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
            "description": "Run any shell command. Use for: installs, file operations, git, system tasks. Returns stdout+stderr.",
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
            "name": "spawn_task",
            "description": "Run a task in background while you handle other tasks. MUST use when user gives 2+ separate tasks in one message. Each task gets its own worker.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "Task description — what the background worker should do"},
                },
                "required": ["task"],
            },
        },
    },
]

# NOTE: web_fetch removed from core — small models handle 5 tools better than 6.
# Available as a skill if needed.


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
            if len(text) > 8000:
                text = text[:8000] + f"\n... (truncated, {len(text)} chars total)"
            return text

        elif name == "write_file":
            p = Path(args["path"]).expanduser()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(args["content"], encoding="utf-8")
            return f"Written {len(args['content'])} chars to {p}"

        elif name == "shell":
            cmd = args["command"]
            # Block dangerous commands
            blocked = ["sudo ", "rm -rf /", "mkfs", "> /dev/"]
            for b in blocked:
                if b in cmd:
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

        elif name == "spawn_task":
            import tasks
            task_id = tasks.spawn(args["task"])
            return f"Task #{task_id} queued: {args['task'][:60]}"

        else:
            # Try skills
            import skills
            result = skills.execute(name, args)
            if result is not None:
                return result
            return f"Unknown tool: {name}"

    except subprocess.TimeoutExpired:
        return f"Error: command timed out after {args.get('timeout', 120)}s"
    except Exception as e:
        return f"Error: {type(e).__name__}: {e}"


def get_all_tools() -> list[dict]:
    """Get base tools + active skill tools."""
    import skills
    return TOOLS + skills.get_tools()
