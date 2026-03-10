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
            t = min(args.get("timeout", 120), 300)
            env = os.environ.copy()
            venv = os.environ.get("VIRTUAL_ENV")
            if venv:
                env["PATH"] = f"{venv}/bin:" + env.get("PATH", "")
            result = subprocess.run(
                args["command"], shell=True, capture_output=True, text=True,
                timeout=t, env=env
            )
            output = result.stdout
            if result.stderr:
                output += f"\nSTDERR: {result.stderr}"
            if result.returncode != 0:
                output += f"\n(exit code: {result.returncode})"
            # Truncate long outputs
            if len(output) > 4000:
                output = output[:2000] + "\n...(truncated)...\n" + output[-1000:]
            return output.strip() or "(no output)"

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
