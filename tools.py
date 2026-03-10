"""Tool definitions and execution for the agent."""

import json, subprocess, os
from pathlib import Path
import memory

# ── Tool definitions (OpenAI function calling format) ──

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "memory_search",
            "description": "Search long-term memory for relevant information. Use before answering questions about past conversations, user preferences, or stored knowledge.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What to search for"},
                    "tag": {"type": "string", "description": "Optional tag filter (e.g. 'user', 'project', 'fact')"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_save",
            "description": "Save important information to long-term memory. Use for user preferences, important facts, decisions, or anything worth remembering across sessions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "The information to remember"},
                    "tag": {"type": "string", "description": "Category tag: 'user', 'project', 'fact', 'task', 'general'"},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read contents of a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to read"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write content to a file. Creates parent directories if needed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to write"},
                    "content": {"type": "string", "description": "Content to write"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "shell",
            "description": "Run a shell command and return output. Use for system tasks, listing files, installing packages, git operations, etc. For long commands (installs, builds) set timeout higher.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to execute"},
                    "timeout": {"type": "integer", "description": "Timeout in seconds (default 30, use 120+ for installs)"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "Fetch content from a URL. Returns text content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to fetch"},
                },
                "required": ["url"],
            },
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

        elif name == "memory_save":
            pid = memory.save(args["text"], tag=args.get("tag", "general"))
            return f"Saved to memory (id: {pid[:8]})"

        elif name == "read_file":
            p = Path(args["path"]).expanduser()
            if not p.exists():
                return f"Error: file not found: {p}"
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
            t = min(args.get("timeout", 120), 300)  # default 2min, max 5min
            result = subprocess.run(
                args["command"], shell=True, capture_output=True, text=True, timeout=t
            )
            output = result.stdout
            if result.stderr:
                output += f"\nSTDERR: {result.stderr}"
            if result.returncode != 0:
                output += f"\n(exit code: {result.returncode})"
            return output.strip() or "(no output)"

        elif name == "web_fetch":
            import urllib.request
            req = urllib.request.Request(args["url"], headers={"User-Agent": "qwe-qwe/0.1"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                text = resp.read().decode("utf-8", errors="replace")
            if len(text) > 8000:
                text = text[:8000] + "\n... (truncated)"
            return text

        else:
            # Try skills
            import skills
            result = skills.execute(name, args)
            if result is not None:
                return result
            return f"Unknown tool: {name}"

    except Exception as e:
        return f"Error: {type(e).__name__}: {e}"


def get_all_tools() -> list[dict]:
    """Get base tools + active skill tools."""
    import skills
    return TOOLS + skills.get_tools()
