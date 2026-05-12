"""Skill Creator — generates new skills via multi-step background pipeline."""

import ast
import json
import re
import threading
import time
from pathlib import Path

import db
import pricing
import providers as _providers

DESCRIPTION = "Create new skills by describing what they should do"

INSTRUCTION = """When creating skills, you have access to the full agent runtime. Generated skills can call any other tool, save to memory, use the camera, read secrets — anything the agent itself can do.

DATABASE (lazy import inside execute()):
- db._get_conn() → sqlite3.Connection (thread-local, DO NOT close)
- db.kv_get(key) → str | None
- db.kv_set(key, value) → None
- db.kv_get_prefix(prefix) → dict[str, str]
- db.kv_inc(key, delta=1) → int
DON'T use: db.cursor(), db.connect(), db.close(), db.execute(), db.datetime

MEMORY (semantic recall + atomic facts):
- import memory
- memory.save(text, tag="user", thread_id=None, synth=True) → point_id
- memory.search(query, limit=8) → list of dicts with text/tag/score/ts

ANY OTHER AGENT TOOL by name (this is how skills compose):
- import tools
- tools.execute("camera_capture", {"prompt": "describe what you see"}) → str
- tools.execute("http_request", {"url": "...", "method": "GET"}) → str
- tools.execute("read_file", {"path": "..."}) → str
- tools.execute("write_file", {"path": "...", "content": "..."}) → str
- tools.execute("send_file", {"path": "..."}) → str (attaches file to chat)
- tools.execute("open_url", {"url": "..."}) → str (opens in user's browser)
- tools.execute("secret_save", {"key": "...", "value": "..."}) → str
- tools.execute("secret_get", {"key": "..."}) → str

LLM (direct call when needed):
- import providers
- client = providers.get_client()
- resp = client.chat.completions.create(model=providers.get_model(), messages=[...])

CONFIG / SCHEDULER / TASKS (available but rarely needed):
- import config; config.LLM_MODEL, config.DATA_DIR
- import scheduler; scheduler.add(name, schedule, prompt) for cron-style
- import tasks; tasks.register(name, description) for background work

TABLE NAMING (CRITICAL — qwe-qwe uses ONE shared SQLite for everything):
- ALWAYS prefix your skill's tables with "skill_<skill_name>_". Example:
  skill_meal_logger_meals, skill_workout_tracker_sets, skill_slack_notify_webhooks
- This prevents silent data collisions with other skills (two skills both
  creating a generic `notes` table would share rows accidentally) and with
  core agent tables (messages, kv, threads, scheduled_tasks, routine_runs).
- Do NOT use generic names: notes, logs, tasks, users, items, records.
- Same rule for kv_set keys when persisting config — prefix with
  "skill:<skill_name>:" e.g. "skill:meal_logger:daily_target".

ALWAYS:
- Lazy-import inside execute(): json, datetime, memory, tools, etc. Cheap module load.
- Create tables with CREATE TABLE IF NOT EXISTS
- Return strings from execute()
- Handle errors with try/except → return friendly error string

NEVER:
- Hardcode API keys / tokens — use secret_save/secret_get
- Block forever waiting on user input from outside the chat
- Modify global agent state — keep skill state in own SQLite tables or kv_* keys
- Use ungrouped table names like 'notes' or 'logs' — collide with other skills"""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "create_skill",
            "description": "Generate a new skill module that adds tools to qwe-qwe. Use this WHENEVER the user asks to connect/integrate/use a service or build a capability that doesn't already exist as a tool — Gmail, Slack, Notion, GitHub, weather APIs, fitness trackers, custom workflows. Do NOT shell-install CLI tools or write loose scripts for these requests; use this tool instead. Runs in background, returns immediately, notifies when ready.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Skill name (lowercase, no spaces, e.g. 'workout_tracker', 'gmail', 'slack_notify')",
                    },
                    "description": {
                        "type": "string",
                        "description": "Detailed description of what the skill should do — include the service/API name, what tools it should expose, what auth it needs, and example use cases",
                    },
                },
                "required": ["name", "description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_skill",
            "description": "Delete a user-created skill by name. Cannot delete built-in skills.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Skill name to delete (e.g. 'health_check')",
                    },
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_skill_files",
            "description": "List existing skill files to see examples.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

# ── Template ──

SKILL_TEMPLATE = '''"""{docstring}"""

DESCRIPTION = "{short_description}"

INSTRUCTION = """{instruction}"""

TOOLS = {tools_json}


def execute(name: str, args: dict) -> str:
    """Handle tool calls for this skill."""
    import json
    from datetime import datetime
    import db

    conn = db._get_conn()

    # Ensure tables exist
{table_ddl}
    conn.commit()

{execute_body}

    return f"Unknown tool: {{name}}"
'''

# ── Step prompts (each step is a focused, small task for 9B model) ──

STEP1_PLAN = """You are a skill architect. Given a skill description, output a JSON plan.

Skills you create run inside the qwe-qwe agent and can use ALL of its
capabilities — they are not sandboxed mini-apps. You may compose tools
that combine:

  - memory.save / memory.search    → persistent semantic recall
  - tools.execute("camera_capture", ...)   → vision / live frame analysis
  - tools.execute("http_request", ...)     → call any HTTP API
  - tools.execute("secret_save"/"secret_get") → persisted API keys / tokens
  - tools.execute("read_file"/"write_file"/"send_file") → workspace I/O
  - tools.execute("open_url", ...)         → push a URL to the user's browser
  - providers.get_client()                  → direct LLM calls when needed

Plan for these when relevant. Examples:
  "fitness coach" → http_request (wearable API) + memory (goals + history) + LLM (advice)
  "meal logger"   → camera_capture (food photo) + memory + own SQLite table
  "slack notify"  → secret_get (webhook) + http_request

Output ONLY valid JSON, no markdown, no explanation:
{{
    "docstring": "One-line module description",
    "short_description": "Short desc (max 80 chars)",
    "instruction": "When and how to use this skill's tools",
    "tables": ["skill_<skill_name>_<purpose>: column1 TYPE, column2 TYPE, ..."],
    "tools": ["tool_name: brief description of what it does"]
}}

CRITICAL: table names MUST be prefixed with skill_<skill_name>_ to
avoid collisions with other skills and the core agent tables. Example
for skill 'meal_logger': "skill_meal_logger_meals" and
"skill_meal_logger_targets" — never just "meals" or "targets".

Keep it simple. IMPORTANT: Max 3 tools only! Max 2 tables. Fewer = better."""

STEP2_TOOLS = """You are a tool definition generator. Given a plan, output OpenAI function tool definitions as a JSON array.

Output ONLY a valid JSON array, no markdown:
[
    {{
        "type": "function",
        "function": {{
            "name": "tool_name",
            "description": "What it does",
            "parameters": {{
                "type": "object",
                "properties": {{
                    "param1": {{"type": "string", "description": "..."}},
                    "param2": {{"type": "integer", "description": "..."}}
                }},
                "required": ["param1"]
            }}
        }}
    }}
]

Rules: snake_case names, clear descriptions, correct JSON types."""

STEP3_CODE = """Generate Python code for a skill's execute() function body.

Variables already available: name (str), args (dict), conn (sqlite3 connection), json, datetime, db.

You may also lazy-import inside any branch:
    import memory                  # memory.save(text, tag, synth=True), memory.search(query, limit)
    import tools                   # tools.execute("camera_capture"|"http_request"|"secret_get"|...)
    import providers               # providers.get_client() for direct LLM calls

Output ONLY the if/elif code block. No markdown. No explanation. No thinking.

Example A — local CRUD (notes):

    if name == "add_note":
        text = args.get("text", "")
        conn.execute("INSERT INTO notes (text, created) VALUES (?, ?)", (text, datetime.now().isoformat()))
        conn.commit()
        return f"Note saved: {text[:50]}"

    elif name == "list_notes":
        rows = conn.execute("SELECT id, text, created FROM notes ORDER BY id DESC LIMIT 10").fetchall()
        if not rows:
            return "No notes yet."
        lines = [f"#{r[0]}: {r[1]} ({r[2]})" for r in rows]
        return "\\n".join(lines)

Example B — camera + memory:

    if name == "describe_scene":
        import memory, tools
        result = tools.execute("camera_capture", {"prompt": args.get("prompt", "describe what you see")})
        memory.save(result, tag="scene_log", synth=True)
        return result

Example C — secret + http_request:

    elif name == "post_to_slack":
        import tools
        webhook = tools.execute("secret_get", {"key": "slack_webhook"})
        if webhook.startswith("Error") or webhook.startswith("Secret"):
            return "Slack webhook not configured. Save it via secret_save with key='slack_webhook'."
        return tools.execute("http_request", {
            "url": webhook,
            "method": "POST",
            "json_body": {"text": args.get("message", "")}
        })

Example D — search persistent memory:

    elif name == "recall_about_user":
        import memory
        topic = args.get("topic", "")
        hits = memory.search(topic, limit=5)
        if not hits:
            return f"No memories matching '{topic}'."
        return "\\n".join(f"- {h['text'][:120]} ({h.get('tag', '?')})" for h in hits)

Now generate code following these patterns. Use 4-space indent. Each branch returns a string."""

# ── Template assembly (replaces LLM code generation for CRUD skills) ──

def _sanitize_id(name: str) -> str:
    """Ensure a SQL identifier is safe."""
    return re.sub(r'[^a-zA-Z0-9_]', '', name) or "col"


def _infer_op(tool_name: str, description: str = "") -> str:
    """Infer operation type from tool name (and optionally description)."""
    n = tool_name.lower()
    d = description.lower()
    # Order matters: more specific patterns first, then general CRUD
    if any(w in n for w in ("schedule", "cron", "every", "remind", "timer")):
        return "schedule"
    if any(w in n for w in ("telegram",)) or ("telegram" in d and any(w in n for w in ("send", "notify"))):
        return "telegram"
    if any(w in n for w in ("send", "post", "notify", "webhook", "ping", "alert")):
        return "http_request"
    if any(w in n for w in ("read_file", "load_file", "open_file")):
        return "read_file"
    # read_*/fetch_* only → read_file if description mentions file/path/disk
    if any(w in n for w in ("read", "fetch", "load")) and any(w in d for w in ("file", "path", "disk", "local")):
        return "read_file"
    if any(w in n for w in ("delete", "remove", "drop")):
        return "delete"
    if any(w in n for w in ("update", "edit", "modify", "change")):
        return "update"
    if any(w in n for w in ("stats", "count", "summary", "total", "status")):
        return "stats"
    if any(w in n for w in ("get", "read", "view", "fetch", "detail")):
        return "get"
    if any(w in n for w in ("list", "search", "find", "all", "show", "browse")):
        return "list"
    if any(w in n for w in ("add", "create", "new", "insert", "log", "record")):
        return "add"
    return "custom"


def _t_add(name: str, spec: dict, first: bool) -> str:
    kw = "if" if first else "elif"
    table = _sanitize_id(spec.get("table", "items"))
    cols = spec.get("cols", {})
    preview = spec.get("preview", next(iter(cols), "item"))
    lines = [f'    {kw} name == "{name}":']
    safe_names = {}
    for c, ctype in cols.items():
        sc = _sanitize_id(c)
        # Prefix with v_ to avoid shadowing function params like 'name'
        var = f"v_{sc}" if sc in ("name", "args", "conn") else sc
        safe_names[c] = var
        default = '""' if ctype == "string" else "0" if ctype == "integer" else "0.0"
        lines.append(f'        {var} = args.get("{sc}", {default})')
    col_list = ", ".join(_sanitize_id(c) for c in cols)
    placeholders = ", ".join("?" for _ in cols)
    vals = ", ".join(safe_names[c] for c in cols)
    lines.append(f'        conn.execute("INSERT INTO {table} ({col_list}) VALUES ({placeholders})", ({vals},))')
    lines.append(f'        conn.commit()')
    preview_var = safe_names.get(preview, _sanitize_id(preview))
    lines.append(f'        return f"Added: {{{preview_var}[:50]}}"')
    return "\n".join(lines)


def _t_list(name: str, spec: dict, first: bool) -> str:
    kw = "if" if first else "elif"
    table = _sanitize_id(spec.get("table", "items"))
    cols = [_sanitize_id(c) for c in spec.get("cols", ["id"])]
    fmt = spec.get("format", "#{r[0]}")
    filt = spec.get("filter_col")
    col_str = ", ".join(cols)
    lines = [f'    {kw} name == "{name}":']
    lines.append(f'        limit = args.get("limit", 10)')
    if filt:
        filt = _sanitize_id(filt)
        lines.append(f'        fv = args.get("{filt}")')
        lines.append(f'        if fv:')
        lines.append(f'            rows = conn.execute("SELECT {col_str} FROM {table} WHERE {filt} = ? ORDER BY id DESC LIMIT ?", (fv, limit)).fetchall()')
        lines.append(f'        else:')
        lines.append(f'            rows = conn.execute("SELECT {col_str} FROM {table} ORDER BY id DESC LIMIT ?", (limit,)).fetchall()')
    else:
        lines.append(f'        rows = conn.execute("SELECT {col_str} FROM {table} ORDER BY id DESC LIMIT ?", (limit,)).fetchall()')
    lines.append(f'        if not rows:')
    lines.append(f'            return "No items found."')
    lines.append(f'        out = [f"{fmt}" for r in rows]')
    lines.append(f'        return "\\n".join(out)')
    return "\n".join(lines)


def _t_delete(name: str, spec: dict, first: bool) -> str:
    kw = "if" if first else "elif"
    table = _sanitize_id(spec.get("table", "items"))
    lines = [f'    {kw} name == "{name}":']
    lines.append(f'        item_id = args.get("id")')
    lines.append(f'        if not item_id:')
    lines.append(f'            return "Error: id is required"')
    lines.append(f'        conn.execute("DELETE FROM {table} WHERE id = ?", (item_id,))')
    lines.append(f'        conn.commit()')
    lines.append(f'        return f"Deleted #{{item_id}}"')
    return "\n".join(lines)


def _t_update(name: str, spec: dict, first: bool) -> str:
    kw = "if" if first else "elif"
    table = _sanitize_id(spec.get("table", "items"))
    ucols = spec.get("update_cols", spec.get("cols", []))
    if isinstance(ucols, dict):
        ucols = list(ucols.keys())
    ucols = [_sanitize_id(c) for c in ucols]
    lines = [f'    {kw} name == "{name}":']
    lines.append(f'        item_id = args.get("id")')
    lines.append(f'        if not item_id:')
    lines.append(f'            return "Error: id is required"')
    lines.append(f'        sets, vals = [], []')
    lines.append(f'        for col in {ucols!r}:')
    lines.append(f'            v = args.get(col)')
    lines.append(f'            if v is not None:')
    lines.append(f'                sets.append(f"{{col}}=?")')
    lines.append(f'                vals.append(v)')
    lines.append(f'        if not sets:')
    lines.append(f'            return "Nothing to update"')
    lines.append(f'        vals.append(item_id)')
    lines.append(f'        conn.execute(f"UPDATE {table} SET {{\\",\\".join(sets)}} WHERE id=?", vals)')
    lines.append(f'        conn.commit()')
    lines.append(f'        return f"Updated #{{item_id}}"')
    return "\n".join(lines)


def _t_get(name: str, spec: dict, first: bool) -> str:
    kw = "if" if first else "elif"
    table = _sanitize_id(spec.get("table", "items"))
    cols = [_sanitize_id(c) for c in spec.get("cols", ["id"])]
    fmt = spec.get("format", "#{r[0]}")
    col_str = ", ".join(cols)
    lines = [f'    {kw} name == "{name}":']
    lines.append(f'        item_id = args.get("id")')
    lines.append(f'        if not item_id:')
    lines.append(f'            return "Error: id is required"')
    lines.append(f'        r = conn.execute("SELECT {col_str} FROM {table} WHERE id = ?", (item_id,)).fetchone()')
    lines.append(f'        if not r:')
    lines.append(f'            return "Not found"')
    lines.append(f'        return f"{fmt}"')
    return "\n".join(lines)


def _t_stats(name: str, spec: dict, first: bool) -> str:
    kw = "if" if first else "elif"
    table = _sanitize_id(spec.get("table", "items"))
    label = spec.get("label", "items")
    lines = [f'    {kw} name == "{name}":']
    lines.append(f'        count = conn.execute("SELECT COUNT(*) FROM {table}").fetchone()[0]')
    lines.append(f'        return f"{{count}} {label} total"')
    return "\n".join(lines)


def _t_http_request(name: str, spec: dict, first: bool) -> str:
    """Template for HTTP request tools — uses actual param names from tool definition."""
    kw = "if" if first else "elif"
    url_param = spec.get("url_param", "url")
    body_params = spec.get("body_params", ["body", "message", "text"])
    method = spec.get("method", "GET").upper()
    lines = [f'    {kw} name == "{name}":']
    lines.append(f'        import urllib.request, urllib.error, json as _json')
    lines.append(f'        target_url = args.get("{url_param}", "")')
    lines.append(f'        if not target_url:')
    lines.append(f'            return "Error: {url_param} is required"')
    # Build body from all non-url params
    if len(body_params) == 1:
        lines.append(f'        body = args.get("{body_params[0]}", "")')
    elif body_params:
        # Multiple body params → build JSON payload from all of them
        gets = ", ".join(f'"{p}": args.get("{p}", "")' for p in body_params)
        lines.append(f'        body = _json.dumps({{{gets}}})')
    else:
        lines.append(f'        body = ""')
    lines.append(f'        try:')
    if method == "POST":
        lines.append(f'            data = body.encode("utf-8") if body else None')
        lines.append(f'            req = urllib.request.Request(target_url, data=data, method="POST")')
        lines.append(f'            req.add_header("Content-Type", "application/json")')
    else:
        lines.append(f'            req = urllib.request.Request(target_url)')
    lines.append(f'            req.add_header("User-Agent", "qwe-qwe/skill")')
    lines.append(f'            with urllib.request.urlopen(req, timeout=15) as resp:')
    lines.append(f'                result = resp.read().decode("utf-8")[:2000]')
    lines.append(f'            return f"OK ({{len(result)}} chars): {{result[:200]}}"')
    lines.append(f'        except urllib.error.URLError as e:')
    lines.append(f'            return f"Request failed: {{e}}"')
    return "\n".join(lines)


def _t_read_file(name: str, spec: dict, first: bool) -> str:
    """Template for file reading tools — uses actual param names from tool definition."""
    kw = "if" if first else "elif"
    path_param = spec.get("path_param", "path")
    extra_params = spec.get("extra_params", [])
    lines = [f'    {kw} name == "{name}":']
    lines.append(f'        from pathlib import Path')
    # If multiple file params (e.g. cpu_file, mem_file), read all and concatenate
    all_paths = [path_param] + extra_params
    if len(all_paths) > 1:
        lines.append(f'        results = []')
        for p in all_paths:
            lines.append(f'        _{_sanitize_id(p)} = args.get("{p}", "")')
            lines.append(f'        if _{_sanitize_id(p)}:')
            lines.append(f'            _p = Path(_{_sanitize_id(p)}).expanduser()')
            lines.append(f'            if _p.is_file() and _p.stat().st_size <= 1_000_000:')
            lines.append(f'                try:')
            lines.append(f'                    results.append(f"[{p}] " + _p.read_text(encoding="utf-8", errors="replace")[:2000])')
            lines.append(f'                except Exception as e:')
            lines.append(f'                    results.append(f"[{p}] Error: {{e}}")')
            lines.append(f'            elif _p.exists():')
            lines.append(f'                results.append(f"[{p}] Too large or not a file: {{_p}}")')
            lines.append(f'            else:')
            lines.append(f'                results.append(f"[{p}] Not found: {{_p}}")')
        lines.append(f'        if not results:')
        lines.append(f'            return "Error: no file paths provided"')
        lines.append(f'        return "\\n".join(results)')
    else:
        lines.append(f'        file_path = args.get("{path_param}", "")')
        lines.append(f'        if not file_path:')
        lines.append(f'            return "Error: {path_param} is required"')
        lines.append(f'        p = Path(file_path).expanduser()')
        lines.append(f'        if not p.exists():')
        lines.append(f'            return f"File not found: {{p}}"')
        lines.append(f'        if not p.is_file():')
        lines.append(f'            return f"Not a file: {{p}}"')
        lines.append(f'        if p.stat().st_size > 1_000_000:')
        lines.append(f'            return f"File too large: {{p.stat().st_size}} bytes (max 1MB)"')
        lines.append(f'        try:')
        lines.append(f'            text = p.read_text(encoding="utf-8", errors="replace")')
        lines.append(f'            if len(text) > 4000:')
        lines.append(f'                text = text[:4000] + f"\\n... ({{len(text)}} chars total, truncated)"')
        lines.append(f'            return text')
        lines.append(f'        except Exception as e:')
        lines.append(f'            return f"Read error: {{e}}"')
    return "\n".join(lines)


def _t_telegram(name: str, spec: dict, first: bool) -> str:
    """Template for Telegram-sending tools — builds API URL from bot_token + chat_id."""
    kw = "if" if first else "elif"
    pm = spec.get("param_map", {})
    token_p = pm.get("bot_token", "bot_token")
    chat_p = pm.get("chat_id", "chat_id")
    text_p = pm.get("text", "message_text")
    lines = [f'    {kw} name == "{name}":']
    lines.append(f'        import urllib.request, urllib.error, json as _json')
    lines.append(f'        bot_token = args.get("{token_p}", "")')
    lines.append(f'        chat_id = args.get("{chat_p}", "")')
    lines.append(f'        text = args.get("{text_p}", "")')
    lines.append(f'        if not bot_token:')
    lines.append(f'            return "Error: {token_p} is required"')
    lines.append(f'        if not chat_id:')
    lines.append(f'            return "Error: {chat_p} is required"')
    lines.append(f'        if not text:')
    lines.append(f'            return "Error: {text_p} is required"')
    lines.append(f'        url = f"https://api.telegram.org/bot{{bot_token}}/sendMessage"')
    lines.append(f'        payload = _json.dumps({{"chat_id": chat_id, "text": text, "parse_mode": "HTML"}}).encode("utf-8")')
    lines.append(f'        try:')
    lines.append(f'            req = urllib.request.Request(url, data=payload, method="POST")')
    lines.append(f'            req.add_header("Content-Type", "application/json")')
    lines.append(f'            req.add_header("User-Agent", "qwe-qwe/skill")')
    lines.append(f'            with urllib.request.urlopen(req, timeout=15) as resp:')
    lines.append(f'                result = _json.loads(resp.read().decode("utf-8"))')
    lines.append(f'            if result.get("ok"):')
    lines.append(f'                return f"Telegram message sent to chat {{chat_id}}"')
    lines.append(f'            return f"Telegram API error: {{result}}"')
    lines.append(f'        except urllib.error.URLError as e:')
    lines.append(f'            return f"Telegram request failed: {{e}}"')
    return "\n".join(lines)


def _t_schedule(name: str, spec: dict, first: bool) -> str:
    """Template for scheduling tools — calls scheduler.add() with actual param names."""
    kw = "if" if first else "elif"
    pm = spec.get("param_map", {})
    # Use actual param names from tool definition, fall back to generic
    name_p = pm.get("name") or "name"
    task_p = pm.get("task") or "task"
    sched_p = pm.get("schedule") or "schedule"
    lines = [f'    {kw} name == "{name}":']
    lines.append(f'        import scheduler')
    lines.append(f'        task_name = args.get("{name_p}", "scheduled_task")')
    lines.append(f'        task_desc = args.get("{task_p}", "")')
    lines.append(f'        sched = args.get("{sched_p}", "in 1h")')
    lines.append(f'        if not task_desc:')
    lines.append(f'            return "Error: {task_p} is required"')
    lines.append(f'        result = scheduler.add(task_name, task_desc, sched)')
    lines.append(f'        if result.get("error"):')
    lines.append(f'            return f"Schedule failed: {{result[\'error\']}}"')
    lines.append(f'        return f"Scheduled \'{{task_name}}\': {{sched}} (next: {{result.get(\'next_run\', \'?\')}})"')
    return "\n".join(lines)


_TEMPLATE_BUILDERS = {
    "add": _t_add, "list": _t_list, "delete": _t_delete,
    "update": _t_update, "get": _t_get, "stats": _t_stats,
    "http_request": _t_http_request, "telegram": _t_telegram,
    "read_file": _t_read_file, "schedule": _t_schedule,
}


def _assemble_from_mapping(mapping: dict) -> tuple:
    """Assemble execute() body from operation mapping.

    Returns (code_body, has_custom, custom_tool_names).
    """
    blocks = []
    custom_tools = []

    for tool_name, spec in mapping.items():
        op = spec.get("op", "custom")
        if op not in _TEMPLATE_BUILDERS and op != "custom":
            op = _infer_op(tool_name)

        if op == "custom" or op not in _TEMPLATE_BUILDERS:
            custom_tools.append(tool_name)
            continue

        block = _TEMPLATE_BUILDERS[op](tool_name, spec, first=len(blocks) == 0)
        blocks.append(block)

    return "\n\n".join(blocks), bool(custom_tools), custom_tools


def _auto_format(cols: list) -> str:
    """Generate f-string format from column list for display."""
    if not cols:
        return "#{r[0]}"
    parts = []
    for i, c in enumerate(cols):
        if i == 0:
            parts.append(f"#{{r[{i}]}}")
        elif i == 1:
            parts.append(f"{{r[{i}]}}")
        else:
            parts.append(f"({{r[{i}]}})")
    return ": ".join(parts[:2]) + (" " + " ".join(parts[2:]) if len(parts) > 2 else "")


def _extract_table_cols(plan: dict, table_name: str) -> list:
    """Parse column names from plan['tables'] entry matching table_name."""
    for t in plan.get("tables", []):
        parts = t.split(":")
        if len(parts) < 2:
            continue
        tname = parts[0].strip().split("(")[0].strip()
        if _sanitize_id(tname) == _sanitize_id(table_name):
            cols_str = parts[1].strip()
            cols = []
            for c in cols_str.split(","):
                col_name = c.strip().split()[0]  # take name, skip TYPE
                col_name = _sanitize_id(col_name)
                if col_name and col_name.lower() not in ("id", "created_at"):
                    cols.append(col_name)
            return cols
    return []


def _has_telegram_params(props: dict) -> bool:
    """Check if tool parameters look like Telegram API params (bot_token + chat_id)."""
    names = {p.lower() for p in props}
    has_token = any(w in n for n in names for w in ("bot_token", "token"))
    has_chat = any(w in n for n in names for w in ("chat_id", "chat"))
    return has_token and has_chat


def _map_telegram_params(props: dict) -> dict:
    """Map actual parameter names to telegram template roles."""
    token_p = next((p for p in props if any(w in p.lower() for w in ("bot_token", "token"))), "bot_token")
    chat_p = next((p for p in props if any(w in p.lower() for w in ("chat_id", "chat"))), "chat_id")
    text_p = next((p for p in props if any(w in p.lower() for w in ("message", "text", "body", "content")) and "token" not in p.lower() and "chat" not in p.lower()), "message_text")
    return {"bot_token": token_p, "chat_id": chat_p, "text": text_p}


def _build_mapping_from_tools(tools_list: list, plan: dict) -> dict:
    """Build operation mapping deterministically from tool definitions and plan.

    No LLM call — uses tool names, parameter definitions, and table info from plan.
    """
    # Parse table names from plan
    tables = []
    for t in plan.get("tables", []):
        tname = t.split(":")[0].strip().split("(")[0].strip()
        tname = _sanitize_id(tname)
        if tname:
            tables.append(tname)
    default_table = tables[0] if tables else "items"

    mapping = {}
    for tool in tools_list:
        func = tool.get("function", {})
        tool_name = func.get("name", "")
        if not tool_name:
            continue
        props = func.get("parameters", {}).get("properties", {})

        tool_desc = func.get("description", "")
        op = _infer_op(tool_name, tool_desc)

        # Non-DB ops don't need table matching — pass actual param names
        # http_request with bot_token+chat_id → upgrade to telegram
        if op == "http_request" and _has_telegram_params(props):
            op = "telegram"
        if op in ("http_request", "telegram", "read_file", "schedule"):
            spec = {"op": op, "params": list(props.keys())}
            if op == "telegram":
                spec["param_map"] = _map_telegram_params(props)
            elif op == "http_request":
                # Find the URL-like param (by name or by being the first string param)
                url_param = next((p for p in props if any(w in p.lower() for w in ("url", "endpoint", "webhook", "target", "link"))), None)
                if not url_param:
                    url_param = next((p for p, s in props.items() if s.get("type") == "string"), "url")
                spec["url_param"] = url_param
                # Remaining string params are body candidates
                body_params = [p for p in props if p != url_param]
                spec["body_params"] = body_params
                if any(w in tool_name for w in ("post", "send", "notify", "webhook")):
                    spec["method"] = "POST"
            elif op == "read_file":
                # Find the path-like param
                path_param = next((p for p in props if any(w in p.lower() for w in ("path", "file", "filename", "location"))), None)
                if not path_param:
                    path_param = next(iter(props), "path")
                spec["path_param"] = path_param
                # Any other params are extra (e.g. second file path)
                spec["extra_params"] = [p for p in props if p != path_param]
            elif op == "schedule":
                # Map params by semantic role
                spec["param_map"] = {
                    "name": next((p for p in props if any(w in p.lower() for w in ("name", "label", "title", "job"))), None),
                    "task": next((p for p in props if any(w in p.lower() for w in ("task", "desc", "command", "message", "action"))), None),
                    "schedule": next((p for p in props if any(w in p.lower() for w in ("schedule", "time", "interval", "cron", "every", "when"))), None),
                }
            mapping[tool_name] = spec
            continue

        # Match table by name overlap (e.g. add_habit -> habits)
        table = default_table
        for t in tables:
            # Check if table stem appears in tool name
            stem = t.rstrip("s")
            if stem and stem in tool_name:
                table = t
                break

        # Extract cols from tool parameters (skip "id" and "limit")
        cols = {}
        for pname, pspec in props.items():
            if pname in ("id", "limit"):
                continue
            ptype = pspec.get("type", "string")
            cols[pname] = ptype

        spec = {"op": op, "table": table}

        if op == "add":
            spec["cols"] = cols
            first_str = next((c for c, t in cols.items() if t == "string"), next(iter(cols), None))
            if first_str:
                spec["preview"] = first_str

        elif op == "list":
            plan_cols = _extract_table_cols(plan, table)
            list_cols = ["id"] + (plan_cols if plan_cols else [_sanitize_id(c) for c in cols])
            spec["cols"] = list_cols
            spec["format"] = _auto_format(list_cols)
            filter_cols = [c for c in cols if cols[c] == "string"]
            if filter_cols:
                spec["filter_col"] = filter_cols[0]

        elif op == "update":
            spec["update_cols"] = [_sanitize_id(c) for c in cols]

        elif op == "stats":
            spec["label"] = table

        elif op == "get":
            plan_cols = _extract_table_cols(plan, table)
            get_cols = ["id"] + (plan_cols[:4] if plan_cols else [_sanitize_id(c) for c in cols])
            spec["cols"] = get_cols
            spec["format"] = _auto_format(get_cols)

        mapping[tool_name] = spec

    return mapping


def _build_table_ddl(plan: dict) -> str:
    """Build CREATE TABLE DDL from plan, fixing duplicate id/created_at columns."""
    ddl_lines = []
    for table_spec in plan.get("tables", []):
        spec = table_spec.strip()
        if spec.upper().startswith("CREATE TABLE"):
            ddl_lines.append(f'    conn.execute("""{spec}""")')
        elif ":" in spec:
            tname, cols = spec.split(":", 1)
            tname = _sanitize_id(tname.strip())
            parts = [c.strip() for c in cols.strip().split(",")]
            # Strip id and created_at — we auto-add them
            parts = [c for c in parts if c
                     and not re.match(r'^id\b', c, re.IGNORECASE)
                     and not re.match(r'^created_at\b', c, re.IGNORECASE)]
            # Quote column names to avoid SQLite reserved words (e.g. "income")
            quoted = []
            for p in parts:
                tokens = p.strip().split(None, 1)
                if len(tokens) == 2:
                    quoted.append(f'"{tokens[0]}" {tokens[1]}')
                elif tokens:
                    quoted.append(f'"{tokens[0]}" TEXT')
            cols_clean = ", ".join(quoted)
            if cols_clean:
                ddl_lines.append(
                    f'    conn.execute("""CREATE TABLE IF NOT EXISTS {tname} (\n'
                    f'        id INTEGER PRIMARY KEY AUTOINCREMENT,\n'
                    f'        {cols_clean},\n'
                    f'        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP\n'
                    f'    )""")')
            else:
                ddl_lines.append(
                    f'    conn.execute("""CREATE TABLE IF NOT EXISTS {tname} (\n'
                    f'        id INTEGER PRIMARY KEY AUTOINCREMENT,\n'
                    f'        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP\n'
                    f'    )""")')
        elif "(" in spec:
            match = re.match(r'(\w+)\s*\((.+)\)', spec)
            if match:
                tname, cols = match.group(1), match.group(2).strip()
                ddl_lines.append(
                    f'    conn.execute("""CREATE TABLE IF NOT EXISTS {tname} ({cols})""")')
    return "\n".join(ddl_lines) if ddl_lines else "    pass  # No tables needed"


# ── Duplicate creation guard ──
_active_skills: set = set()
_active_lock = threading.Lock()


def execute(name: str, args: dict) -> str:
    if name == "create_skill":
        return _create_skill_async(args["name"], args["description"])
    elif name == "delete_skill":
        return _delete_skill(args["name"])
    elif name == "list_skill_files":
        return _list_skills()
    return f"Unknown tool: {name}"


def _delete_skill(skill_name: str) -> str:
    """Delete a user-created skill. Refuses to delete built-in skills."""
    import config
    from skills import disable

    skill_name = skill_name.lower().replace(" ", "_").replace("-", "_")
    if not skill_name.isidentifier():
        return f"Error: '{skill_name}' is not a valid skill name"

    # Only allow deleting from user skills directory
    user_dir = config.USER_SKILLS_DIR
    target = user_dir / f"{skill_name}.py"

    # Check if it's a built-in skill
    builtin_dir = Path(__file__).parent
    if (builtin_dir / f"{skill_name}.py").exists():
        return f"Error: '{skill_name}' is a built-in skill and cannot be deleted"

    if not target.exists():
        return f"Error: skill '{skill_name}' not found in {user_dir}"

    dropped_tables = _drop_skill_owned_tables(skill_name, target)

    # Disable first
    disable(skill_name)

    # Delete file
    target.unlink()

    # Clean up __pycache__
    pycache = user_dir / "__pycache__"
    if pycache.exists():
        for cached in pycache.glob(f"skill_{skill_name}*"):
            cached.unlink(missing_ok=True)
        for cached in pycache.glob(f"{skill_name}*"):
            cached.unlink(missing_ok=True)

    if dropped_tables:
        return f"Deleted skill '{skill_name}' ({dropped_tables} skill table(s) dropped)"
    return f"Deleted skill '{skill_name}'"


def _drop_skill_owned_tables(skill_name: str, skill_path: Path) -> int:
    """Drop SQLite tables declared by this skill's own namespaced DDL."""
    try:
        source = skill_path.read_text(encoding="utf-8")
        tables = _extract_skill_owned_tables(source, skill_name)
        if not tables:
            return 0

        import db

        conn = db._get_conn()
        dropped = 0
        for table in tables:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if not exists:
                continue
            conn.execute(f'DROP TABLE IF EXISTS "{table}"')
            dropped += 1
        if dropped:
            conn.commit()
        return dropped
    except Exception as e:
        import logger

        logger.get("skill_creator").warning(
            f"failed to clean tables for {skill_name}: {e}"
        )
        return 0


def _extract_skill_owned_tables(source: str, skill_name: str) -> list[str]:
    """Return safe table names with the skill_<skill_name>_ prefix."""
    prefix = f"skill_{skill_name}_"
    pattern = re.compile(
        r"CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?\s+[`\"'\[]?"
        r"([A-Za-z_][A-Za-z0-9_]*)"
        r"[`\"'\]]?",
        re.IGNORECASE,
    )
    tables = {
        table
        for table in pattern.findall(source)
        if table.startswith(prefix) and table.isidentifier()
    }
    return sorted(tables)


def _create_skill_async(skill_name: str, description: str) -> str:
    """Kick off background skill generation."""
    import logger
    _log = logger.get("skill_creator")
    # Telemetry: first skill_create attempt this session. Fires before any
    # validation so we capture both successful and failed attempts.
    # No-op when telemetry is off.
    try:
        import telemetry as _tel
        _tel.track_feature_first_use("skill_create")
    except Exception:
        pass

    skill_name = skill_name.lower().replace(" ", "_").replace("-", "_")
    if not skill_name.isidentifier():
        return f"Error: '{skill_name}' is not a valid Python identifier"

    # Prevent duplicate concurrent generation
    with _active_lock:
        if skill_name in _active_skills:
            return f"Skill '{skill_name}' is already being generated. Please wait."
        _active_skills.add(skill_name)

    import config
    skills_dir = config.USER_SKILLS_DIR  # user skills go to ~/.qwe-qwe/skills/
    target = skills_dir / f"{skill_name}.py"
    if target.exists():
        with _active_lock:
            _active_skills.discard(skill_name)
        return f"Error: skill '{skill_name}' already exists at {target}"

    # Register in background tasks registry
    import tasks
    task_id = tasks.register(f"skill:{skill_name}", f"Creating skill '{skill_name}': {description[:100]}")

    # Launch background thread
    t = threading.Thread(
        target=_generate_skill_pipeline,
        args=(skill_name, description, target, task_id),
        daemon=True,
    )
    t.start()
    _log.info(f"skill generation started in background: {skill_name} (task #{task_id})")

    return (
        f"⏳ Skill '{skill_name}' generation started in background.\n"
        f"I'll work through: plan → tools → code → validate.\n"
        f"This takes 2-5 minutes. I'll notify when done."
    )


def _llm_call(system: str, user: str, max_tokens: int = 2048,
              _tok_accum: list | None = None) -> str:
    """Make a single LLM call with generous context.

    If *_tok_accum* is a list, appends a (in_tok, out_tok) tuple so callers
    can aggregate token counts across multiple calls without changing the
    return type.
    """
    import providers
    client = providers.get_client()
    model = providers.get_model()

    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.2,
        max_tokens=max_tokens,
    )
    if _tok_accum is not None and getattr(resp, "usage", None):
        _tok_accum.append((
            int(getattr(resp.usage, "prompt_tokens", 0) or 0),
            int(getattr(resp.usage, "completion_tokens", 0) or 0),
        ))
    raw = resp.choices[0].message.content or ""
    # Strip thinking tags
    raw = re.sub(r"<think>.*?</think>\s*", "", raw, flags=re.DOTALL).strip()
    return raw


def _extract_json(raw: str):
    """Extract JSON from LLM output, handling markdown fences and thinking text."""
    # Strip thinking tags (tagged and untagged)
    raw = re.sub(r"<think>.*?</think>\s*", "", raw, flags=re.DOTALL).strip()

    # Strip markdown code fences
    if "```" in raw:
        lines = raw.split("\n")
        clean = []
        in_fence = False
        for line in lines:
            if line.strip().startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence or not clean:
                clean.append(line)
        raw = "\n".join(clean).strip()

    # Try full text as JSON first
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        pass

    # Scan from end to find last valid JSON block (skips thinking text before JSON)
    for i in range(len(raw) - 1, -1, -1):
        if raw[i] in ('}', ']'):
            opener = '{' if raw[i] == '}' else '['
            depth = 0
            for j in range(i, -1, -1):
                if raw[j] == raw[i]:
                    depth += 1
                elif raw[j] == opener:
                    depth -= 1
                if depth == 0:
                    try:
                        return json.loads(raw[j:i + 1])
                    except (json.JSONDecodeError, ValueError):
                        break
            # Only try the outermost match
            break

    # Try json repair as last resort
    try:
        from agent import _repair_json
        return _repair_json(raw)
    except Exception:
        pass

    return None


def _extract_code(raw: str) -> str:
    """Extract Python code from LLM output."""
    # Strip thinking tags and thinking blocks
    raw = re.sub(r"<think>.*?</think>\s*", "", raw, flags=re.DOTALL).strip()

    # Strip everything before first 'if name' or 'if ' line
    lines = raw.split("\n")
    code_start = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("if name ==") or stripped.startswith("if name=="):
            code_start = i
            break
        # Also catch markdown-fenced code
        if stripped.startswith("```"):
            continue

    if code_start is not None:
        # Take everything from first 'if name' line
        code_lines = []
        in_fence = False
        for line in lines[code_start:]:
            if line.strip().startswith("```"):
                in_fence = not in_fence
                continue
            code_lines.append(line)
        return "\n".join(code_lines)

    # Fallback: strip markdown fences
    if "```" in raw:
        clean = []
        in_fence = False
        for line in lines:
            if line.strip().startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence:
                clean.append(line)
        if clean:
            return "\n".join(clean)

    return raw.strip()


def _fix_indentation(code: str) -> str:
    """Fix indentation by detecting the offset and normalizing to 4-space base.

    Strategy: find the first `if name ==` line, measure its indent,
    then shift ALL lines so that line sits at exactly 4 spaces.
    Preserves relative indentation within blocks.
    """
    lines = code.split("\n")

    # Find anchor: first `if name ==` line
    anchor_indent = None
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("if name ==") or stripped.startswith("if name=="):
            anchor_indent = len(line) - len(stripped)
            break

    if anchor_indent is None:
        # No anchor found — just ensure minimum 4-space indent
        fixed = []
        for line in lines:
            if not line.strip():
                fixed.append("")
                continue
            current = len(line) - len(line.lstrip())
            if current < 4:
                fixed.append("    " + line.lstrip())
            else:
                fixed.append(line)
        return "\n".join(fixed)

    # Shift everything so anchor is at indent=4
    shift = 4 - anchor_indent
    fixed = []
    for line in lines:
        if not line.strip():
            fixed.append("")
            continue
        current = len(line) - len(line.lstrip())
        new_indent = max(4, current + shift)
        fixed.append(" " * new_indent + line.lstrip())

    return "\n".join(fixed)


def _fix_empty_blocks(code: str) -> str:
    """Add 'pass' after empty if/elif/else/try/except blocks."""
    lines = code.split("\n")
    fixed = []
    for i, line in enumerate(lines):
        fixed.append(line)
        stripped = line.rstrip()
        if stripped.endswith(":"):
            # Check if next non-empty line is at same or lesser indent
            current_indent = len(line) - len(line.lstrip())
            next_indent = None
            for j in range(i + 1, min(i + 3, len(lines))):
                next_stripped = lines[j].strip()
                if next_stripped:
                    next_indent = len(lines[j]) - len(lines[j].lstrip())
                    break
            if next_indent is not None and next_indent <= current_indent:
                fixed.append(" " * (current_indent + 4) + "pass")
    return "\n".join(fixed)


def _save_skill_result(skill_name: str, description: str, tool_names: list, success: bool):
    """Save skill creation result to memory + chat history so the model knows about it."""
    try:
        import memory
        if success:
            tools_str = ", ".join(tool_names) if tool_names else "none"
            text = (
                f"Skill '{skill_name}' created successfully. "
                f"Description: {description}. "
                f"Available tools: {tools_str}. "
                f"User can use /{skill_name} to interact with it."
            )
        else:
            text = f"Skill '{skill_name}' creation failed. Description was: {description}. User may want to retry."
        memory.save(text, tag="task")
    except Exception:
        pass

    # Save to chat history so the model sees it in context next turn
    try:
        import db
        status = "✅" if success else "❌"
        if success:
            tools_str = ", ".join(tool_names) if tool_names else ""
            chat_msg = f"{status} Skill '{skill_name}' ready! Tools: {tools_str}. Use /{skill_name}."
        else:
            chat_msg = f"{status} Skill '{skill_name}' creation failed. Try with a simpler description."
        db.save_message("assistant", chat_msg, meta={"source": "skill_creator"})
    except Exception:
        pass


def _notify(skill_name: str, message: str):
    """Send notification about skill generation progress."""
    import logger
    _log = logger.get("skill_creator")
    _log.info(f"[{skill_name}] {message}")

    # Try to notify via WebSocket (for web UI auto-refresh)
    try:
        import sys
        if "server" in sys.modules:
            import asyncio
            server = sys.modules["server"]
            ws_loop = getattr(server, "_ws_loop", None)
            ws_clients = getattr(server, "_ws_clients", None)
            broadcast = getattr(server, "_broadcast", None)
            if ws_loop and ws_clients and broadcast:
                asyncio.run_coroutine_threadsafe(
                    broadcast({"type": "task_update", "name": skill_name, "text": message}),
                    ws_loop
                )
    except Exception as e:
        _log.debug(f"[{skill_name}] WS notify failed: {e}")

    # Try to notify via telegram
    try:
        import telegram_bot
        if telegram_bot.is_verified() and telegram_bot._running:
            owner = telegram_bot.get_owner_id()
            if owner:
                telegram_bot.send_message(owner, f"🔧 Skill '{skill_name}': {message}")
    except Exception:
        pass


def _cleanup_debug_logs(logs_dir: Path, keep: int = 5):
    """Remove old skill_debug_* files, keeping only the most recent."""
    debug_files = sorted(logs_dir.glob("skill_debug_*.py"), key=lambda f: f.stat().st_mtime)
    for f in debug_files[:-keep]:
        try:
            f.unlink()
        except OSError:
            pass


def _smoke_test(skill_path: Path, tools_list: list[dict]) -> list[str]:
    """Try importing the skill and calling execute() with empty args for each tool.

    Also verifies that required parameters from tool definitions are actually
    referenced in the generated execute() code (catches definition/implementation mismatch).
    Returns list of error strings (empty = OK).
    """
    errors = []
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(f"_smoke_{skill_path.stem}", skill_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as e:
        return [f"Import failed: {e}"]

    # Read source code for param-usage check
    try:
        source = skill_path.read_text(encoding="utf-8")
    except Exception:
        source = ""

    # Extract the execute() function body via AST so we only check
    # param usage inside execute(), not in the TOOLS dict or other
    # module-level declarations (which would always contain the param
    # names and defeat the check).
    execute_body_source = _extract_execute_body(source)

    for t in tools_list:
        func = t.get("function", {})
        tool_name = func.get("name", "")
        if not tool_name:
            continue

        # 1. Basic call test
        try:
            result = mod.execute(tool_name, {})
            if not isinstance(result, str):
                errors.append(f"{tool_name}: execute() returned {type(result).__name__}, expected str")
        except Exception as e:
            err_str = str(e)
            # Some errors are expected with empty args (e.g. missing required param)
            # Only flag actual crashes, not "missing argument" type errors
            if "NOT NULL" in err_str or "required" in err_str.lower() or "missing" in err_str.lower():
                pass  # Expected with empty args — continue to param check
            else:
                errors.append(f"{tool_name}: {e}")

        # 2. Param-usage check: every required param must appear in execute() source
        if execute_body_source:
            required = func.get("parameters", {}).get("required", [])
            for param in required:
                # Look for args.get("param") or args["param"] or param as variable
                if f'"{param}"' not in execute_body_source and f"'{param}'" not in execute_body_source:
                    errors.append(
                        f'{tool_name}: required param "{param}" not found in execute() code — '
                        f'definition/implementation mismatch'
                    )

    return errors


def _extract_execute_body(source: str) -> str:
    """Return the source text of the execute() function body only.

    Uses AST to find the FunctionDef for 'execute' and extracts the
    raw source spanning its body. Returns empty string on any failure
    (unparseable source, no execute function, etc.) so callers degrade
    gracefully to no check rather than crashing.
    """
    if not source:
        return ""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ""
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "execute":
            # Get line/col positions of the first statement in the body
            first = node.body[0]
            last = node.body[-1]
            lines = source.splitlines(keepends=True)
            # lineno is 1-based; slice from first body line to last body line
            start_line = first.lineno - 1
            end_line = last.end_lineno  # end_lineno is also 1-based
            return "".join(lines[start_line:end_line])
    return ""


def _generate_skill_pipeline(skill_name: str, description: str, target: Path, task_id: int = 0):
    """Multi-step skill generation pipeline running in background."""
    try:
        _run_pipeline(skill_name, description, target, task_id)
    finally:
        with _active_lock:
            _active_skills.discard(skill_name)


def _emit_pipeline_telemetry(outcome: str, attempts: int, start_time: float,
                              tools_count: int) -> None:
    """Emit a `skill_creator_pipeline` telemetry event. No-op when disabled.

    Privacy: outcome / attempts / duration / tools-count only. Never the
    skill name (could be a corp identifier) or any tool names.
    """
    try:
        import telemetry
        if not telemetry.enabled():
            return
        kind = outcome if outcome in telemetry.PIPELINE_OUTCOMES else "max_attempts_exhausted"
        telemetry.track_event("skill_creator_pipeline", {
            "outcome": kind,
            "attempts": int(attempts),
            "duration_ms": int((time.time() - start_time) * 1000),
            "tools_count": int(tools_count),
        })
    except Exception:
        # Telemetry must never crash the skill pipeline
        pass


def _run_pipeline(skill_name: str, description: str, target: Path, task_id: int = 0):
    """Actual pipeline logic, wrapped by _generate_skill_pipeline for cleanup."""
    import logger
    import tasks
    _log = logger.get("skill_creator")
    start = time.time()
    max_attempts = 3
    # Track per-attempt failure mode so the FINAL outcome reflects what
    # actually killed the last try (validate vs smoke vs syntax).
    last_failure: str = "max_attempts_exhausted"

    # ── Cost-tracking bracket ──────────────────────────────────────────
    _sc_thread_id = f"skill:{skill_name}"
    _rid = db.insert_agent_run(
        thread_id=_sc_thread_id, source="skill_creator",
        started_at=start, status="running",
        model=_providers.get_model(), provider=_providers.get_active_name(),
    )
    _tok_accum: list = []   # each _llm_call appends (in_tok, out_tok) here
    # mutable dict so inner closures can update without nonlocal
    _run_state: dict = {"status": "ok", "error": None, "preview": None}

    def _finalize_run():
        """Write final metrics to agent_runs. Called at every exit point."""
        _finished = time.time()
        agg_in = sum(t[0] for t in _tok_accum)
        agg_out = sum(t[1] for t in _tok_accum)
        _cost = None
        try:
            _cost = pricing.compute_cost(_providers.get_model(), agg_in, agg_out)
        except Exception:
            pass
        db.finalize_agent_run(
            _rid, finished_at=_finished,
            duration_ms=int((_finished - start) * 1000),
            status=_run_state["status"], error=_run_state["error"],
            result_preview=_run_state["preview"],
            input_tokens=agg_in, output_tokens=agg_out,
            cost_usd=_cost,
        )
    # ──────────────────────────────────────────────────────────────────

    def _progress(step: str):
        if task_id:
            tasks.update(task_id, "running", step)
        _notify(skill_name, step)

    for attempt in range(1, max_attempts + 1):
        _log.info(f"[{skill_name}] attempt {attempt}/{max_attempts}")

        try:
            # ── Step 1: Plan ──
            _progress(f"Step 1/5: planning (attempt {attempt})")
            _log.info(f"[{skill_name}] step 1: planning")
            plan_raw = _llm_call(
                STEP1_PLAN,
                f"Create a skill called '{skill_name}'.\nDescription: {description}",
                max_tokens=1024,
                _tok_accum=_tok_accum,
            )
            plan = _extract_json(plan_raw)
            if not plan or not isinstance(plan, dict):
                _log.warning(f"[{skill_name}] step 1 failed: bad JSON")
                continue

            _log.info(f"[{skill_name}] step 1 done: {len(plan.get('tools', []))} tools planned")

            # ── Step 2: Tool definitions ──
            _progress(f"Step 2/5: generating tools (attempt {attempt})")
            _log.info(f"[{skill_name}] step 2: generating tool definitions")
            tools_raw = _llm_call(
                STEP2_TOOLS,
                f"Skill: {skill_name}\nPlan:\n{json.dumps(plan, indent=2, ensure_ascii=False)}",
                max_tokens=2048,
                _tok_accum=_tok_accum,
            )
            tools_list = _extract_json(tools_raw)
            if not tools_list or not isinstance(tools_list, list):
                _log.warning(f"[{skill_name}] step 2 failed: bad tools JSON")
                continue

            # Validate tool structure
            valid_tools = []
            for t in tools_list:
                if isinstance(t, dict) and t.get("function", {}).get("name"):
                    if "type" not in t:
                        t["type"] = "function"
                    valid_tools.append(t)
            if not valid_tools:
                _log.warning(f"[{skill_name}] step 2: no valid tools")
                continue
            tools_list = valid_tools

            _log.info(f"[{skill_name}] step 2 done: {len(tools_list)} tools")

            # ── Step 3: Deterministic mapping + template assembly (no LLM) ──
            _progress(f"Step 3/5: assembling code (attempt {attempt})")
            _log.info(f"[{skill_name}] step 3: building mapping from tool definitions")
            tool_names = [t["function"]["name"] for t in tools_list]
            tables_info = "\n".join(plan.get("tables", []))

            mapping = _build_mapping_from_tools(tools_list, plan)
            execute_body, has_custom, custom_tools = _assemble_from_mapping(mapping)
            _log.info(f"[{skill_name}] step 3: assembled {len(mapping) - len(custom_tools)} tools from templates")

            if has_custom and custom_tools:
                # Fall back to LLM only for truly custom operations
                _log.info(f"[{skill_name}] step 3: {len(custom_tools)} custom tools need LLM: {custom_tools}")
                tool_descriptions = "\n".join(
                    f"- {t['function']['name']}: {t['function'].get('description', '')}"
                    for t in tools_list
                )
                # When execute_body is empty (no deterministic mapping
                # produced anything), the custom block must start with
                # `if name == "..."`, not `elif` — otherwise the file
                # has `elif` without preceding `if` and SyntaxErrors.
                # Tell the LLM the right keyword for the FIRST tool.
                first_keyword = "if" if not execute_body else "elif"
                custom_prompt = (
                    f"Skill: {skill_name}\n"
                    f"Tables (already created):\n{tables_info}\n\n"
                    f"Generate Python branches for these tools:\n"
                    + "\n".join(f"- {tn}" for tn in custom_tools)
                    + f"\n\nDescriptions:\n{tool_descriptions}\n"
                    f"Start the FIRST tool with '{first_keyword} name == \"...\":' "
                    f"and the rest with 'elif name == \"...\":'. Each branch "
                    f"contains the FULL implementation (no stub `pass`) and "
                    f"returns a string. All code for a tool MUST be indented "
                    f"under its branch — no top-level statements between branches."
                )
                custom_raw = _llm_call(STEP3_CODE, custom_prompt, max_tokens=2048,
                                       _tok_accum=_tok_accum)
                custom_code = _extract_code(custom_raw)
                custom_code = _fix_indentation(custom_code)
                custom_code = _fix_empty_blocks(custom_code)
                # Defensive post-process: even with the prompt fix,
                # small models sometimes still emit `elif` first when
                # they're supposed to start with `if`. If our body is
                # empty and the LLM output starts with `elif` (after
                # any leading whitespace), rewrite the first `elif` to
                # `if` so the file parses.
                if not execute_body:
                    custom_code = re.sub(
                        r'^(\s*)elif(\s+name\s*==)',
                        r'\1if\2',
                        custom_code, count=1, flags=re.MULTILINE,
                    )
                execute_body = execute_body + "\n\n" + custom_code if execute_body else custom_code

            # ── Step 4: Generate table DDL ──
            _progress(f"Step 4/5: building tables (attempt {attempt})")
            table_ddl = _build_table_ddl(plan)

            # ── Step 5: Assemble & validate ──
            _progress(f"Step 5/5: validating (attempt {attempt})")
            _log.info(f"[{skill_name}] step 5: assembling and validating")
            tools_json = json.dumps(tools_list, indent=4, ensure_ascii=False)

            code = SKILL_TEMPLATE.format(
                docstring=plan.get("docstring", f"{skill_name} skill"),
                short_description=plan.get("short_description", description[:80]),
                instruction=plan.get("instruction", f"Use {skill_name} tools as needed."),
                tools_json=tools_json,
                table_ddl=table_ddl,
                execute_body=execute_body,
            )

            # Save for debugging (keep only last 5 debug files)
            logs_dir = Path(__file__).parent.parent / "logs"
            logs_dir.mkdir(exist_ok=True)
            debug_path = logs_dir / f"skill_debug_{skill_name}_{attempt}.py"
            debug_path.write_text(code, encoding="utf-8")
            _cleanup_debug_logs(logs_dir, keep=5)

            # Validate syntax
            try:
                ast.parse(code)
            except SyntaxError as e:
                _log.warning(f"[{skill_name}] syntax error on attempt {attempt}: {e}")
                # Try one more fix: ensure all blocks have content
                execute_body = _fix_empty_blocks(execute_body)
                code = SKILL_TEMPLATE.format(
                    docstring=plan.get("docstring", f"{skill_name} skill"),
                    short_description=plan.get("short_description", description[:80]),
                    instruction=plan.get("instruction", f"Use {skill_name} tools as needed."),
                    tools_json=tools_json,
                    table_ddl=table_ddl,
                    execute_body=execute_body,
                )
                try:
                    ast.parse(code)
                except SyntaxError as e2:
                    _log.warning(f"[{skill_name}] still syntax error after fix: {e2}")
                    last_failure = "syntax_error"
                    continue

            # Save
            target.write_text(code, encoding="utf-8")

            # Validate with skill loader
            from skills import validate_skill, enable
            valid, errors = validate_skill(str(target))

            if not valid:
                _log.warning(f"[{skill_name}] validation errors: {errors}")
                # Keep file for manual fix, but don't enable
                if attempt < max_attempts:
                    target.unlink(missing_ok=True)  # retry will overwrite anyway
                    last_failure = "validate_fail"
                    continue
                # Last attempt — keep file, notify with errors
                msg = f"⚠️ Created with errors: {'; '.join(errors)}. Fix with write_file."
                if task_id:
                    tasks.update(task_id, "error", msg)
                _notify(skill_name, msg)
                _emit_pipeline_telemetry("validate_fail", attempt, start, 0)
                _run_state["status"] = "err"; _run_state["error"] = "validate_fail"
                _finalize_run()
                return

            # Smoke test: try calling execute() with each tool
            smoke_errors = _smoke_test(target, tools_list)
            if smoke_errors:
                _log.warning(f"[{skill_name}] smoke test errors: {smoke_errors}")
                if attempt < max_attempts:
                    target.unlink(missing_ok=True)
                    last_failure = "smoke_fail"
                    continue
                msg = f"⚠️ Created but smoke test failed: {'; '.join(smoke_errors)}"
                if task_id:
                    tasks.update(task_id, "error", msg)
                _notify(skill_name, msg)
                _emit_pipeline_telemetry("smoke_fail", attempt, start, 0)
                _run_state["status"] = "err"; _run_state["error"] = "smoke_fail"
                _finalize_run()
                return

            # Enable
            enable(skill_name)

            elapsed = int(time.time() - start)
            tool_names = [t["function"]["name"] for t in tools_list]
            msg = f"✅ Created and enabled! ({len(tools_list)} tools, {elapsed}s)"
            if task_id:
                tasks.update(task_id, "done", msg)
            _notify(skill_name, msg)
            _save_skill_result(skill_name, description, tool_names, success=True)
            _log.info(f"[{skill_name}] SUCCESS in {elapsed}s, attempt {attempt}")
            _emit_pipeline_telemetry("success", attempt, start, len(tools_list))
            _run_state["status"] = "ok"
            _run_state["preview"] = f"created skill: {skill_name} ({len(tools_list)} tools)"
            _finalize_run()
            return

        except Exception as e:
            _log.error(f"[{skill_name}] attempt {attempt} error: {e}", exc_info=True)
            last_failure = "max_attempts_exhausted"
            _run_state["status"] = "err"; _run_state["error"] = str(e)[:500]
            continue

    # All attempts failed
    elapsed = int(time.time() - start)
    msg = f"❌ Failed after {max_attempts} attempts ({elapsed}s). Try simpler description."
    if task_id:
        tasks.update(task_id, "error", msg)
    _notify(skill_name, msg)
    _save_skill_result(skill_name, description, [], success=False)
    _log.error(f"[{skill_name}] FAILED after {max_attempts} attempts")
    _emit_pipeline_telemetry(last_failure, max_attempts, start, 0)
    _finalize_run()


def _list_skills() -> str:
    from pathlib import Path
    import config
    # List from both built-in and user directories
    files = set()
    for d in (Path(__file__).parent, config.USER_SKILLS_DIR):
        if d.exists():
            files.update(f.name for f in d.glob("*.py") if not f.name.startswith("_"))
    if not files:
        return "No skills found."
    return "Existing skills:\n" + "\n".join(f"  - {f}" for f in sorted(files))
