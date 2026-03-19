"""Skill Creator — generates new skills via multi-step background pipeline."""

import json, re, ast, time, threading
from pathlib import Path

DESCRIPTION = "Create new skills by describing what they should do"

INSTRUCTION = """When creating skills, use ONLY these db functions:
- db._get_conn() → returns sqlite3.Connection (thread-local, DO NOT close it)
- db.kv_get(key) → str or None
- db.kv_set(key, value) → None
- db.kv_get_prefix(prefix) → dict[str, str]
- db.kv_inc(key, delta=1) → int
DO NOT use: db.cursor(), db.connect(), db.close(), db.execute(), db.datetime
Always import inside execute(): json, datetime
Always create tables with CREATE TABLE IF NOT EXISTS inside execute().
Return strings from execute(). Handle errors with try/except."""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "create_skill",
            "description": "Create a new skill in background. Returns immediately, notifies when done.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Skill name (lowercase, no spaces, e.g. 'workout_tracker')",
                    },
                    "description": {
                        "type": "string",
                        "description": "Detailed description of what the skill should do",
                    },
                },
                "required": ["name", "description"],
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

Output ONLY valid JSON, no markdown, no explanation:
{{
    "docstring": "One-line module description",
    "short_description": "Short desc (max 80 chars)",
    "instruction": "When and how to use this skill's tools",
    "tables": ["table_name: column1 TYPE, column2 TYPE, ..."],
    "tools": ["tool_name: brief description of what it does"]
}}

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

Output ONLY the if/elif code block. No markdown. No explanation. No thinking.

Example for a "notes" skill with tools add_note and list_notes:

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

Now generate code following this exact pattern. Use 4-space indent. Each branch returns a string."""

STEP3_MAPPING = """You are a tool-to-operation mapper. Given a skill plan with tables and tools, output a JSON object mapping each tool to a database operation.

Output ONLY valid JSON, no markdown, no explanation.

Operation types:
- "add": Insert a new row
- "list": Select multiple rows with optional filter
- "delete": Delete a row by id
- "update": Modify a row by id
- "get": Get a single row by id
- "stats": Count rows
- "custom": Complex logic that doesn't fit above

For each tool specify:
{
  "tool_name": {
    "op": "add|list|delete|update|get|stats|custom",
    "table": "table_name",
    "cols": {"col1": "string", "col2": "integer"},
    "preview": "col_to_show_in_confirmation",
    "format": "#{r[0]}: {r[1]} ({r[2]})",
    "filter_col": "optional_filter_column",
    "label": "human_noun_for_stats",
    "update_cols": ["col1", "col2"]
  }
}

Only include fields relevant to the operation type."""


# ── Template assembly (replaces LLM code generation for CRUD skills) ──

def _sanitize_id(name: str) -> str:
    """Ensure a SQL identifier is safe."""
    return re.sub(r'[^a-zA-Z0-9_]', '', name) or "col"


def _infer_op(tool_name: str) -> str:
    """Infer operation type from tool name as heuristic fallback."""
    n = tool_name.lower()
    # Order matters: more specific patterns first
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
    for c, ctype in cols.items():
        c = _sanitize_id(c)
        default = '""' if ctype == "string" else "0" if ctype == "integer" else "0.0"
        lines.append(f'        {c} = args.get("{c}", {default})')
    col_list = ", ".join(_sanitize_id(c) for c in cols)
    placeholders = ", ".join("?" for _ in cols)
    vals = ", ".join(_sanitize_id(c) for c in cols)
    lines.append(f'        conn.execute("INSERT INTO {table} ({col_list}) VALUES ({placeholders})", ({vals},))')
    lines.append(f'        conn.commit()')
    preview = _sanitize_id(preview)
    lines.append(f'        return f"Added: {{{preview}[:50]}}"')
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


_TEMPLATE_BUILDERS = {
    "add": _t_add, "list": _t_list, "delete": _t_delete,
    "update": _t_update, "get": _t_get, "stats": _t_stats,
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
            cols_clean = ", ".join(parts)
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


def execute(name: str, args: dict) -> str:
    if name == "create_skill":
        return _create_skill_async(args["name"], args["description"])
    elif name == "list_skill_files":
        return _list_skills()
    return f"Unknown tool: {name}"


def _create_skill_async(skill_name: str, description: str) -> str:
    """Kick off background skill generation."""
    import logger
    _log = logger.get("skill_creator")

    skill_name = skill_name.lower().replace(" ", "_").replace("-", "_")
    if not skill_name.isidentifier():
        return f"Error: '{skill_name}' is not a valid Python identifier"

    import config
    skills_dir = config.USER_SKILLS_DIR  # user skills go to ~/.qwe-qwe/skills/
    target = skills_dir / f"{skill_name}.py"
    if target.exists():
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


def _llm_call(system: str, user: str, max_tokens: int = 2048) -> str:
    """Make a single LLM call with generous context."""
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
    raw = resp.choices[0].message.content or ""
    # Strip thinking tags
    raw = re.sub(r"<think>.*?</think>\s*", "", raw, flags=re.DOTALL).strip()
    return raw


def _extract_json(raw: str):
    """Extract JSON from LLM output, handling markdown fences."""
    # Strip markdown code fences
    if "```" in raw:
        lines = raw.split("\n")
        clean = []
        in_fence = False
        for line in lines:
            if line.strip().startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence or not clean:  # keep content
                clean.append(line)
        raw = "\n".join(clean).strip()

    # Try to find JSON
    match = re.search(r'[\[{].*[\]}]', raw, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    # Try json repair
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


def _notify(skill_name: str, message: str):
    """Send notification about skill generation progress."""
    import logger
    _log = logger.get("skill_creator")
    _log.info(f"[{skill_name}] {message}")

    # Try to notify via WebSocket (for web UI auto-refresh)
    try:
        import asyncio
        # Import _broadcast and _ws_loop from server if available
        from server import _broadcast, _ws_loop, _ws_clients
        if _ws_loop and _ws_clients:
            asyncio.run_coroutine_threadsafe(
                _broadcast({"type": "task_update", "name": skill_name, "text": message}),
                _ws_loop
            )
    except Exception:
        pass

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

    Catches crashes on import or basic call — NOT functional correctness.
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

    for t in tools_list:
        tool_name = t.get("function", {}).get("name", "")
        if not tool_name:
            continue
        try:
            result = mod.execute(tool_name, {})
            if not isinstance(result, str):
                errors.append(f"{tool_name}: execute() returned {type(result).__name__}, expected str")
        except Exception as e:
            err_str = str(e)
            # Some errors are expected with empty args (e.g. missing required param)
            # Only flag actual crashes, not "missing argument" type errors
            if "NOT NULL" in err_str or "required" in err_str.lower() or "missing" in err_str.lower():
                continue  # Expected with empty args
            errors.append(f"{tool_name}: {e}")

    return errors


def _generate_skill_pipeline(skill_name: str, description: str, target: Path, task_id: int = 0):
    """Multi-step skill generation pipeline running in background."""
    import logger
    import tasks
    _log = logger.get("skill_creator")
    start = time.time()
    max_attempts = 3

    def _progress(step: str):
        if task_id:
            tasks.update(task_id, "running", step)

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

            # ── Step 3: Operation mapping + template assembly ──
            _progress(f"Step 3/5: mapping operations (attempt {attempt})")
            _log.info(f"[{skill_name}] step 3: generating operation mapping")
            tool_names = [t["function"]["name"] for t in tools_list]
            tool_descriptions = "\n".join(
                f"- {t['function']['name']}: {t['function'].get('description', '')}"
                for t in tools_list
            )
            tables_info = "\n".join(plan.get("tables", []))

            mapping_prompt = (
                f"Skill: {skill_name}\n"
                f"Tables:\n{tables_info}\n\n"
                f"Tools to map:\n{tool_descriptions}\n\n"
                f"Map each tool to an operation. Tool names: {tool_names}"
            )

            mapping_raw = _llm_call(STEP3_MAPPING, mapping_prompt, max_tokens=1024)
            mapping = _extract_json(mapping_raw)

            if mapping and isinstance(mapping, dict):
                # Fill missing tools with heuristic inference
                for tn in tool_names:
                    if tn not in mapping:
                        _log.info(f"[{skill_name}] tool '{tn}' missing from mapping, inferring op")
                        mapping[tn] = {"op": _infer_op(tn), "table": plan.get("tables", ["items"])[0].split(":")[0].strip()}

                execute_body, has_custom, custom_tools = _assemble_from_mapping(mapping)
                _log.info(f"[{skill_name}] step 3: assembled {len(mapping) - len(custom_tools)} tools from templates")

                if has_custom and custom_tools:
                    # Fall back to LLM for custom tools only
                    _log.info(f"[{skill_name}] step 3: {len(custom_tools)} custom tools need LLM: {custom_tools}")
                    custom_prompt = (
                        f"Skill: {skill_name}\n"
                        f"Tables (already created):\n{tables_info}\n\n"
                        f"Generate ONLY elif blocks for these tools:\n"
                        + "\n".join(f"- {tn}" for tn in custom_tools)
                        + f"\n\nDescriptions:\n{tool_descriptions}\n"
                        f"Start each with 'elif name == \"tool_name\":'. Each returns a string."
                    )
                    custom_raw = _llm_call(STEP3_CODE, custom_prompt, max_tokens=2048)
                    custom_code = _extract_code(custom_raw)
                    custom_code = _fix_indentation(custom_code)
                    custom_code = _fix_empty_blocks(custom_code)
                    execute_body = execute_body + "\n\n" + custom_code if execute_body else custom_code
            else:
                # Mapping failed — full fallback to LLM code generation
                _log.warning(f"[{skill_name}] step 3: mapping failed, falling back to LLM codegen")
                code_prompt = (
                    f"Skill: {skill_name}\n"
                    f"Tables (already created via DDL):\n{tables_info}\n\n"
                    f"Tools to implement:\n{tool_descriptions}\n\n"
                    f"Generate the if/elif chain for execute(). "
                    f"Tool names: {tool_names}"
                )
                code_raw = _llm_call(STEP3_CODE, code_prompt, max_tokens=3072)
                execute_body = _extract_code(code_raw)
                execute_body = _fix_indentation(execute_body)
                execute_body = _fix_empty_blocks(execute_body)

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
                    continue
                # Last attempt — keep file, notify with errors
                msg = f"⚠️ Created with errors: {'; '.join(errors)}. Fix with write_file."
                if task_id:
                    tasks.update(task_id, "error", msg)
                _notify(skill_name, msg)
                return

            # Smoke test: try calling execute() with each tool
            smoke_errors = _smoke_test(target, tools_list)
            if smoke_errors:
                _log.warning(f"[{skill_name}] smoke test errors: {smoke_errors}")
                if attempt < max_attempts:
                    target.unlink(missing_ok=True)
                    continue
                msg = f"⚠️ Created but smoke test failed: {'; '.join(smoke_errors)}"
                if task_id:
                    tasks.update(task_id, "error", msg)
                _notify(skill_name, msg)
                return

            # Enable
            enable(skill_name)

            elapsed = int(time.time() - start)
            msg = f"✅ Created and enabled! ({len(tools_list)} tools, {elapsed}s)"
            if task_id:
                tasks.update(task_id, "done", msg)
            _notify(skill_name, msg)
            _log.info(f"[{skill_name}] SUCCESS in {elapsed}s, attempt {attempt}")
            return

        except Exception as e:
            _log.error(f"[{skill_name}] attempt {attempt} error: {e}", exc_info=True)
            continue

    # All attempts failed
    elapsed = int(time.time() - start)
    msg = f"❌ Failed after {max_attempts} attempts ({elapsed}s). Try simpler description."
    if task_id:
        tasks.update(task_id, "error", msg)
    _notify(skill_name, msg)
    _log.error(f"[{skill_name}] FAILED after {max_attempts} attempts")


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
