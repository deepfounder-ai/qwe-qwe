"""Tool definitions and execution — optimized for small models."""

import json, subprocess, os, re, shutil, sys
from pathlib import Path
import config
import memory
import logger

_log = logger.get("tools")

# Agent workspace — all relative paths resolve here
WORKSPACE = config.WORKSPACE_DIR

# Detect shell: prefer Git Bash on Windows (not WSL bash), fallback to cmd
_SHELL_EXE: str | None = None
if sys.platform == "win32":
    # Prefer Git Bash over WSL bash (WSL bash causes stack overflow on simple commands)
    _git_bash = Path("C:/Program Files/Git/usr/bin/bash.exe")
    if _git_bash.exists():
        _SHELL_EXE = str(_git_bash)
    else:
        _found = shutil.which("bash") or shutil.which("bash.exe")
        # Skip WSL bash (system32\bash.exe) — it causes issues
        if _found and "system32" not in _found.lower():
            _SHELL_EXE = _found
    if _SHELL_EXE:
        _log.info(f"shell: using bash at {_SHELL_EXE}")
    else:
        _log.info("shell: bash not found, using cmd.exe")

# Directories the agent is allowed to write to (whitelist — safer than blacklist)
_WRITE_WHITELIST: list[str] | None = None


def _get_write_whitelist() -> list[str]:
    """Lazily compute write-allowed directories."""
    global _WRITE_WHITELIST
    if _WRITE_WHITELIST is None:
        _WRITE_WHITELIST = [
            str(config.WORKSPACE_DIR.resolve()),   # ~/.qwe-qwe/workspace/
            str(config.DATA_DIR.resolve()),         # ~/.qwe-qwe/
            str(Path.cwd().resolve()),              # project working directory
        ]
    return _WRITE_WHITELIST


def _resolve_path(raw: str, for_write: bool = False) -> Path:
    """Resolve a file path for agent operations.

    - Git Bash paths (/c/Users/...) -> C:/Users/... on Windows
    - Relative paths -> workspace (~/.qwe-qwe/workspace/)
    - ~ expands to home
    - For writes: only allow workspace, data dir, and cwd (whitelist)
    """
    # Convert Git Bash / MSYS2 paths to Windows: /c/Users/... → C:/Users/...
    if sys.platform == "win32" and len(raw) >= 3 and raw[0] == "/" and raw[2] == "/":
        drive = raw[1].upper()
        if drive.isalpha():
            raw = f"{drive}:{raw[2:]}"
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = WORKSPACE / p
    p = p.resolve()
    if for_write:
        s = str(p)
        allowed = any(s.startswith(w) for w in _get_write_whitelist())
        if not allowed:
            raise PermissionError(
                f"Cannot write outside allowed directories. Path: {p}\n"
                f"Allowed: workspace, data dir (~/.qwe-qwe/), project dir"
            )
    return p


# ── Shell safety ──

_SHELL_BLOCKED_PATTERNS = re.compile(
    r"(?:^|[\s;|&])\s*(?:"
    r"sudo\b|su\s+\w|"                           # privilege escalation
    r"rm\s+-[rf]*\s+/|rm\s+-[rf]*\s+~/|rm\s+-[rf]*\s+\$HOME|"  # recursive delete root/home
    r">\s*/dev/|dd\s+if=|"                        # raw device writes
    r"mkfs|fdisk|parted|"                         # disk formatting
    r"chmod\s+[0-7]{3,4}\s+/|chown\s+\S+\s+/|"   # system permission changes
    r"shutdown|reboot|halt|poweroff|"             # system control
    r"pkill\s+-9|killall\s|kill\s+-9\s+1\b"       # process killing
    r")",
    re.IGNORECASE
)

_SHELL_BLOCKED_EXACT = [
    "rm -rf /", "rm -rf /*", "rm -rf ~", "rm -rf $HOME",
    ":(){:|:&};:",   # fork bomb
    ":(){ :|:& };:", # fork bomb variant
]


def _check_shell_safety(cmd: str) -> str | None:
    """Returns error message if command is blocked, None if safe."""
    # Exact substring matches
    for b in _SHELL_BLOCKED_EXACT:
        if b in cmd:
            return f"Blocked: dangerous command pattern."
    # Regex pattern matches
    if _SHELL_BLOCKED_PATTERNS.search(cmd):
        return "Blocked: potentially dangerous command."
    # Block command substitution — prevents hiding commands inside $() or backticks
    if "$(" in cmd or "`" in cmd:
        return "Blocked: command substitution ($() and backticks) not allowed for safety."
    # Block curl/wget piped to shell
    if re.search(r"(?:curl|wget)\s.*\|\s*(?:sh|bash|zsh|python)", cmd, re.IGNORECASE):
        return "Blocked: piping downloads to shell not allowed."
    return None


# ── Core tools (always loaded) vs Extended (loaded via tool_search) ──

_CORE_TOOL_NAMES = {
    "memory_search", "memory_save",
    "read_file", "write_file", "shell",
    "self_config",  # manage own settings
    "http_request", "spawn_task",
    "tool_search",  # meta-tool to discover more tools
}

# Session-level: tools activated by tool_search (persists within agent turn)
_active_extra_tools: set[str] = set()


_spicy_duck_on: bool | None = None  # cached per turn


def _reset_active_tools():
    """Reset extra tools between turns."""
    global _spicy_duck_on
    _active_extra_tools.clear()
    _spicy_duck_on = None  # re-check on next get_all_tools()


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
            "description": "Save info to long-term memory. Long texts auto-chunked for knowledge graph.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "What to remember (long texts auto-chunked)"},
                    "tag": {"type": "string", "description": "Category: user/project/fact/task/knowledge"},
                    "source": {"type": "string", "description": "Source name (article title, URL, filename)"},
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
            "description": "Run a bash shell command in workspace directory. Use UNIX commands (ls, find, grep, cat), NOT Windows (dir, findstr). Returns stdout+stderr.",
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
            "description": "Write content to a file. Relative paths go to workspace. Creates directories if needed.",
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
            "description": "Schedule a task to run later or repeatedly. Auto-validates via dry-run before saving. Formats: 'in 5m', 'in 2h', 'every 30m', 'daily 09:00', '14:30'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Short name for the task"},
                    "task": {"type": "string", "description": "What to do when the time comes"},
                    "schedule": {"type": "string", "description": "When: 'in 5m', 'every 1h', 'daily 09:00', '14:30'"},
                    "skip_dry_run": {"type": "boolean", "description": "Skip validation dry-run (default false)"},
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
    # HTTP request tool
    {
        "type": "function",
        "function": {
            "name": "http_request",
            "description": "Make HTTP request to any URL. Use for APIs, webhooks, Telegram bot, etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Full URL including https://"},
                    "method": {"type": "string", "enum": ["GET", "POST", "PUT", "DELETE"], "description": "HTTP method (default GET)"},
                    "body": {"type": "string", "description": "Request body (JSON string for POST/PUT)"},
                    "headers": {"type": "object", "description": "Extra headers as key-value pairs"},
                },
                "required": ["url"],
            },
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
    # Self-config: read/write own settings
    {
        "type": "function",
        "function": {
            "name": "self_config",
            "description": "Read or change qwe-qwe's own settings. action='list' shows all, action='get' reads one, action='set' changes one. Keys: telegram:bot_token, telegram:chat_id, telegram:group_id, streaming:telegram, or any setting name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "get", "set"], "description": "list=show all settings, get=read one, set=change one"},
                    "key": {"type": "string", "description": "Setting key (e.g. 'telegram:bot_token', 'max_tool_rounds', 'context_budget')"},
                    "value": {"type": "string", "description": "New value (for action=set)"},
                },
                "required": ["action"],
            },
        },
    },
    # Meta-tool: discover additional tools
    {
        "type": "function",
        "function": {
            "name": "tool_search",
            "description": "Find and activate additional tools by keyword. Use when you need a capability not in your current tools (e.g. 'browser', 'notes', 'schedule', 'secret', 'mcp', 'profile', 'rag', 'model', 'skill', 'timer').",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Keyword: browser, notes, schedule, secret, mcp, profile, rag, skill, timer, cron, model"},
                },
                "required": ["query"],
            },
        },
    },
]


# ── Tool search index ──
# Maps keywords to tool names for discovery

_TOOL_SEARCH_INDEX = {
    "browser": ["browser_open", "browser_snapshot", "browser_screenshot", "browser_click", "browser_fill", "browser_eval", "browser_close"],
    "web": ["browser_open", "browser_snapshot", "browser_screenshot", "http_request"],
    "search": ["browser_open", "memory_search", "rag_search"],
    "google": ["browser_open"],
    "notes": ["create_note", "list_notes", "read_note", "delete_note", "edit_note"],
    "note": ["create_note", "list_notes", "read_note", "delete_note", "edit_note"],
    "schedule": ["schedule_task", "list_cron", "remove_cron"],
    "cron": ["schedule_task", "list_cron", "remove_cron"],
    "timer": ["set_timer", "schedule_task"],
    "secret": ["secret_save", "secret_get", "secret_list", "secret_delete"],
    "vault": ["secret_save", "secret_get", "secret_list", "secret_delete"],
    "password": ["secret_save", "secret_get"],
    "key": ["secret_save", "secret_get"],
    "mcp": ["mcp_list_servers", "mcp_add_server", "mcp_remove_server", "mcp_restart_server", "mcp_toggle_server"],
    "profile": ["user_profile_update", "user_profile_get"],
    "user": ["user_profile_update", "user_profile_get", "memory_search"],
    "rag": ["rag_index", "rag_search", "rag_status"],
    "index": ["rag_index", "rag_status"],
    "knowledge": ["rag_index", "rag_search"],
    "model": ["switch_model"],
    "switch": ["switch_model"],
    "skill": ["create_skill", "delete_skill", "list_skill_files"],
    "soul": ["add_trait", "remove_trait", "list_traits"],
    "trait": ["add_trait", "remove_trait", "list_traits"],
    "personality": ["add_trait", "remove_trait", "list_traits"],
    "memory": ["memory_search", "memory_save", "memory_delete"],
    "delete": ["memory_delete", "secret_delete", "delete_note"],
    "file": ["read_file", "write_file"],
    "screenshot": ["browser_screenshot"],
    "navigate": ["browser_open"],
    "click": ["browser_click", "browser_fill"],
    "lovense": ["lovense_connect", "lovense_vibrate", "lovense_pattern", "lovense_preset", "lovense_stop", "lovense_status"],
    "spicy": ["lovense_connect", "lovense_vibrate", "lovense_pattern", "lovense_preset", "lovense_stop", "lovense_status"],
    "duck": ["lovense_connect", "lovense_vibrate", "lovense_pattern", "lovense_preset", "lovense_stop", "lovense_status"],
    "vibrate": ["lovense_vibrate", "lovense_pattern", "lovense_preset", "lovense_stop"],
    "toy": ["lovense_connect", "lovense_vibrate", "lovense_stop", "lovense_status"],
}


def _do_self_config(args: dict) -> str:
    """Read or change qwe-qwe's own settings."""
    import db
    action = args.get("action", "list")
    key = args.get("key", "")
    value = args.get("value", "")

    if action == "list":
        lines = ["=== Editable Settings ==="]
        for k, (kv_key, type_, default, desc, *_) in config.EDITABLE_SETTINGS.items():
            current = db.kv_get(kv_key)
            if current is None:
                current = str(default)
            lines.append(f"  {k} = {current}  ({desc})")
        # Also show key system KV values
        lines.append("\n=== System Keys ===")
        for sys_key in ["telegram:bot_token", "telegram:chat_id", "telegram:group_id",
                        "telegram:streaming", "user_name", "active_skills",
                        "soul:name", "soul:language"]:
            val = db.kv_get(sys_key)
            if val and "token" in sys_key:
                val = val[:15] + "..." if len(val) > 15 else val  # mask tokens
            lines.append(f"  {sys_key} = {val or '(not set)'}")
        return "\n".join(lines)

    elif action == "get":
        if not key:
            return "Error: 'key' is required for action='get'"
        # Try editable settings first
        if key in config.EDITABLE_SETTINGS:
            return f"{key} = {config.get(key)}"
        # Try raw KV
        val = db.kv_get(key)
        return f"{key} = {val}" if val is not None else f"{key} is not set"

    elif action == "set":
        if not key:
            return "Error: 'key' is required for action='set'"
        if not value and value != "0":
            return "Error: 'value' is required for action='set'"
        # Try editable settings first (has validation)
        if key in config.EDITABLE_SETTINGS:
            return config.set(key, value)
        # Raw KV set for system keys (telegram, soul, etc.)
        db.kv_set(key, value)
        # Auto-restart telegram bot when token changes
        if key == "telegram:bot_token" and value:
            try:
                import telegram_bot
                telegram_bot.stop()
                telegram_bot.set_token(value)
                telegram_bot.start()
                return f"✓ {key} set + telegram bot restarted"
            except Exception as e:
                return f"✓ {key} set (bot restart failed: {e})"
        return f"✓ {key} = {value}"

    return f"Unknown action: {action}. Use 'list', 'get', or 'set'."


def _do_tool_search(query: str) -> str:
    """Search for tools by keyword. Returns matching tool names and activates them."""
    query_lower = query.lower().strip()
    found = set()

    # Direct keyword match
    for kw, tool_names in _TOOL_SEARCH_INDEX.items():
        if kw in query_lower or query_lower in kw:
            found.update(tool_names)

    # Also search tool descriptions from all available tools
    if not found:
        all_t = _get_all_tools_full()
        for t in all_t:
            fn = t["function"]
            if query_lower in fn["name"] or query_lower in fn.get("description", "").lower():
                found.add(fn["name"])

    if not found:
        return f"No tools found for '{query}'. Available keywords: browser, notes, schedule, secret, mcp, profile, rag, skill, soul, timer, model"

    # Activate found tools for this turn
    _active_extra_tools.update(found)

    # Return descriptions of activated tools
    all_t = _get_all_tools_full()
    lines = [f"Activated {len(found)} tools:"]
    for t in all_t:
        fn = t["function"]
        if fn["name"] in found:
            params = list(fn.get("parameters", {}).get("properties", {}).keys())
            lines.append(f"  - {fn['name']}({', '.join(params)}): {fn.get('description', '')}")
    lines.append("\nYou can now call these tools directly.")
    return "\n".join(lines)


def _get_all_tools_full() -> list[dict]:
    """Get ALL tools (core + extended + skills + MCP) without filtering."""
    import skills
    all_tools = list(TOOLS)
    all_tools += skills.get_tools(compact=True)
    try:
        import mcp_client
        all_tools += mcp_client.get_all_mcp_tools()
    except Exception:
        pass
    return all_tools


# ── Tool execution ──

def execute(name: str, args: dict) -> str:
    """Execute a tool and return result as string."""
    try:
        # MCP tools: mcp__servername__toolname
        if name.startswith("mcp__"):
            import mcp_client
            return mcp_client.execute_mcp_tool(name, args)

        if name == "tool_search":
            return _do_tool_search(args.get("query", ""))

        elif name == "self_config":
            return _do_self_config(args)

        elif name == "memory_search":
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
            text = args["text"]
            tag = args.get("tag", "general")
            meta = {}
            if args.get("source"):
                meta["source"] = args["source"]
            pid = memory.save(text, tag=tag, meta=meta if meta else None)
            chunked = len(text) > 1000
            if chunked:
                chunks = memory._chunk_text(text)
                return f"Saved ({len(chunks)} chunks, group id: {pid[:8]}, queued for synthesis)"
            return f"Saved (id: {pid[:8]})"

        elif name == "read_file":
            p = _resolve_path(args["path"])
            if not p.exists():
                return f"Error: file not found: {args['path']}"
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
            p = _resolve_path(args["path"], for_write=True)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(args["content"], encoding="utf-8")
            return f"Written {len(args['content'])} chars to {p}"

        elif name == "shell":
            cmd = args["command"]
            _log.info(f"shell: {cmd[:200]}")
            # Safety check — block dangerous command patterns
            block_reason = _check_shell_safety(cmd)
            if block_reason:
                _log.warning(f"shell blocked: {cmd}")
                return block_reason
            t = min(args.get("timeout", 120), 300)
            env = os.environ.copy()
            venv = os.environ.get("VIRTUAL_ENV")
            if venv:
                env["PATH"] = f"{venv}/bin:" + env.get("PATH", "")
            # Force UTF-8 for subprocess to handle emoji and non-ASCII
            env["PYTHONIOENCODING"] = "utf-8"
            # Use bash on Windows if available (Git Bash), otherwise cmd
            if _SHELL_EXE:
                result = subprocess.run(
                    [_SHELL_EXE, "-c", args["command"]],
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                    timeout=t, env=env, cwd=str(WORKSPACE),
                    stdin=subprocess.DEVNULL,
                )
            else:
                result = subprocess.run(
                    args["command"], shell=True, capture_output=True, text=True,
                    encoding="utf-8", errors="replace",
                    timeout=t, env=env, cwd=str(WORKSPACE),
                    stdin=subprocess.DEVNULL,
                )
            output = result.stdout or ""
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
            result = scheduler.add(
                args["name"], args["task"], args["schedule"],
                skip_dry_run=args.get("skip_dry_run", False),
            )
            if result.get("error"):
                parts = [f"Error: {result['error']}"]
                if result.get("output"):
                    parts.append(f"Output: {result['output']}")
                if result.get("hint"):
                    parts.append(f"Hint: {result['hint']}")
                return "\n".join(parts)
            repeat_str = " (repeating)" if result["repeat"] else " (one-time)"
            msg = f"✓ Scheduled '{result['name']}' → next run: {result['next_run']}{repeat_str}"
            if result.get("preview"):
                msg += f"\nDry-run preview: {result['preview']}"
            return msg

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

        elif name == "http_request":
            import urllib.request
            import urllib.error
            import socket
            from urllib.parse import urlparse
            url = args["url"]
            # Basic URL validation (no SSRF blocking — qwe-qwe is a local agent)
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https"):
                return f"Error: only http/https URLs allowed, got '{parsed.scheme}'"
            method = args.get("method", "GET").upper()
            body = args.get("body")
            hdrs = {"User-Agent": "qwe-qwe/0.5"}
            if body:
                hdrs["Content-Type"] = "application/json"
            if args.get("headers"):
                hdrs.update(args["headers"])
            data = body.encode("utf-8") if body else None
            req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    text = resp.read().decode("utf-8", errors="replace")
                    if len(text) > 10000:
                        text = text[:10000] + "\n...(truncated)"
                    return f"HTTP {resp.status}: {text}"
            except urllib.error.HTTPError as he:
                body_text = he.read().decode("utf-8", errors="replace")[:5000]
                return f"HTTP {he.code}: {body_text}"
            except urllib.error.URLError as ue:
                return f"HTTP error: {ue.reason}"
            except (socket.timeout, TimeoutError):
                return "HTTP error: request timed out (15s)"
            except Exception as e:
                return f"HTTP error: {e}"

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
        cmd = args.get('command', '?')
        _log.error(f"shell timeout: {cmd[:100]}")
        # Help the model understand what happened
        hint = ""
        if any(srv in cmd for srv in ['uvicorn', 'flask', 'gunicorn', 'npm start', 'node ', 'python -m http']):
            hint = " This looks like a server/daemon — it blocks forever. Use spawn_task instead of shell for long-running processes."
        return f"Error: command timed out after {args.get('timeout', 120)}s.{hint} Do NOT retry the same command."
    except Exception as e:
        _log.error(f"tool {name} exception: {e}", exc_info=True)
        # Sanitize error message — don't leak full paths or internals
        err_msg = str(e).replace(str(Path.home()), "~")
        return f"Error: {type(e).__name__}: {err_msg}"


def get_all_tools(compact: bool = False) -> list[dict]:
    """Get core tools + activated extra tools (from tool_search).

    Only core tools are always sent. Extended tools appear after tool_search activates them.
    This saves ~2000 tokens per request for small models.
    """
    all_available = _get_all_tools_full()

    # Check if hidden skills are active — their tools bypass tool_search
    # Cached per-turn (reset in _reset_active_tools)
    global _spicy_duck_on
    if _spicy_duck_on is None:
        import db as _db
        _spicy_duck_on = _db.kv_get("spicy_duck") == "quack"
    _always_on = set()
    if _spicy_duck_on:
        _always_on.update({"lovense_connect", "lovense_vibrate", "lovense_pattern",
                           "lovense_preset", "lovense_stop", "lovense_status"})

    # Filter: core tools + always-on (hidden skills) + any activated by tool_search
    result = []
    for t in all_available:
        name = t["function"]["name"]
        if name in _CORE_TOOL_NAMES or name in _always_on or name in _active_extra_tools:
            result.append(t)

    return result
