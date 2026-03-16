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

    skills_dir = Path(__file__).parent
    target = skills_dir / f"{skill_name}.py"
    if target.exists():
        return f"Error: skill '{skill_name}' already exists at {target}"

    # Launch background thread
    t = threading.Thread(
        target=_generate_skill_pipeline,
        args=(skill_name, description, target),
        daemon=True,
    )
    t.start()
    _log.info(f"skill generation started in background: {skill_name}")

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
    """Fix common indentation issues from small models."""
    lines = code.split("\n")
    fixed = []
    for line in lines:
        if not line.strip():
            fixed.append("")
            continue
        # Ensure minimum 4-space indent for top-level
        stripped = line.lstrip()
        current_indent = len(line) - len(stripped)
        if current_indent < 4 and (
            stripped.startswith("if ") or
            stripped.startswith("elif ") or
            stripped.startswith("else:") or
            stripped.startswith("return ") or
            stripped.startswith("try:") or
            stripped.startswith("except ") or
            stripped.startswith("finally:")
        ):
            fixed.append("    " + stripped)
        elif current_indent < 8 and not (
            stripped.startswith("if ") or
            stripped.startswith("elif ") or
            stripped.startswith("else:") or
            stripped.startswith("return ") or
            stripped.startswith("try:") or
            stripped.startswith("except ") or
            stripped.startswith("finally:") or
            stripped.startswith("for ") or
            stripped.startswith("while ") or
            stripped.startswith("#")
        ) and current_indent >= 4:
            fixed.append(line)
        else:
            if current_indent < 4:
                fixed.append("    " + stripped)
            else:
                fixed.append(line)

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

    # Try to notify via telegram
    try:
        import telegram_bot
        if telegram_bot.is_verified() and telegram_bot._running:
            owner = telegram_bot.get_owner_id()
            if owner:
                telegram_bot.send_message(owner, f"🔧 Skill '{skill_name}': {message}")
    except Exception:
        pass


def _generate_skill_pipeline(skill_name: str, description: str, target: Path):
    """Multi-step skill generation pipeline running in background."""
    import logger
    _log = logger.get("skill_creator")
    start = time.time()
    max_attempts = 3

    for attempt in range(1, max_attempts + 1):
        _log.info(f"[{skill_name}] attempt {attempt}/{max_attempts}")

        try:
            # ── Step 1: Plan ──
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

            # ── Step 3: Generate execute body ──
            _log.info(f"[{skill_name}] step 3: generating code")
            tool_names = [t["function"]["name"] for t in tools_list]
            tool_descriptions = "\n".join(
                f"- {t['function']['name']}: {t['function'].get('description', '')}"
                for t in tools_list
            )
            tables_info = "\n".join(plan.get("tables", []))

            code_prompt = (
                f"Skill: {skill_name}\n"
                f"Tables (already created via DDL):\n{tables_info}\n\n"
                f"Tools to implement:\n{tool_descriptions}\n\n"
                f"Generate the if/elif chain for execute(). "
                f"Tool names: {tool_names}"
            )

            code_raw = _llm_call(STEP3_CODE, code_prompt, max_tokens=3072)
            execute_body = _extract_code(code_raw)

            # Fix common issues
            execute_body = _fix_indentation(execute_body)
            execute_body = _fix_empty_blocks(execute_body)

            # ── Step 4: Generate table DDL ──
            table_ddl_lines = []
            for table_spec in plan.get("tables", []):
                # Parse "table_name: col1 TYPE, col2 TYPE"
                if ":" in table_spec:
                    tname, cols = table_spec.split(":", 1)
                    tname = tname.strip()
                    cols = cols.strip()
                    table_ddl_lines.append(
                        f'    conn.execute("""CREATE TABLE IF NOT EXISTS {tname} ({cols})""")'
                    )
            table_ddl = "\n".join(table_ddl_lines) if table_ddl_lines else "    pass  # No tables needed"

            # ── Step 5: Assemble & validate ──
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

            # Save for debugging
            debug_path = Path(__file__).parent.parent / "logs" / f"skill_debug_{skill_name}_{attempt}.py"
            debug_path.write_text(code, encoding="utf-8")

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
                target.unlink(missing_ok=True)
                continue

            # Enable
            enable(skill_name)

            elapsed = int(time.time() - start)
            _notify(skill_name, f"✅ Created and enabled! ({len(tools_list)} tools, {elapsed}s)")
            _log.info(f"[{skill_name}] SUCCESS in {elapsed}s, attempt {attempt}")
            return

        except Exception as e:
            _log.error(f"[{skill_name}] attempt {attempt} error: {e}", exc_info=True)
            continue

    # All attempts failed
    elapsed = int(time.time() - start)
    _notify(skill_name, f"❌ Failed after {max_attempts} attempts ({elapsed}s). Try simpler description.")
    _log.error(f"[{skill_name}] FAILED after {max_attempts} attempts")


def _list_skills() -> str:
    from pathlib import Path
    skills_dir = Path(__file__).parent
    files = sorted(f.name for f in skills_dir.glob("*.py") if not f.name.startswith("_"))
    if not files:
        return "No skills found."
    return "Existing skills:\n" + "\n".join(f"  - {f}" for f in files)
