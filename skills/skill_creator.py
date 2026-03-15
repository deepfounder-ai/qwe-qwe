"""Skill Creator — generates new skills with scaffold template + validation."""

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
            "description": "Create a new skill. Provide name, description, tools JSON, and execute body logic.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Skill name (lowercase, no spaces, e.g. 'todo_list')",
                    },
                    "description": {
                        "type": "string",
                        "description": "Detailed description of what the skill should do, what tools it needs",
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

SUBAGENT_PROMPT = '''You are a skill code generator for the qwe-qwe agent framework.

Generate ONLY the parts needed to fill a skill template. Reply as JSON with these keys:
{
    "docstring": "One-line module description",
    "short_description": "Short description for skill list (max 80 chars)",
    "instruction": "Instructions for the agent on how to use this skill (when to call which tool, what to avoid)",
    "tools": [array of OpenAI-format tool definitions],
    "table_ddl": "SQL CREATE TABLE IF NOT EXISTS statements (one per table, separated by newlines)",
    "execute_body": "Python code for the if/elif chain inside execute(). Use 4-space indent. Start with 'if name == ...'."
}

RULES:
1. Output ONLY valid JSON. No markdown, no explanation, no code fences.
2. Tool names must be snake_case, unique and descriptive.
3. In execute_body:
   - Access args with args.get("key", default) or args["key"]
   - Use `conn` variable (already available from template)
   - Always return strings
   - Handle errors with try/except
   - Use json.loads/dumps for JSON data
   - Use datetime for dates (already imported)
4. For table_ddl: plain SQL, each CREATE TABLE on its own line, indented with 4 spaces.
5. In tools array: follow OpenAI function calling format exactly.
6. DO NOT use: db.cursor(), db.connect(), db.close(), print()
7. The template already imports json, datetime, db and calls db._get_conn().'''


def execute(name: str, args: dict) -> str:
    if name == "create_skill":
        return _create_skill(args["name"], args["description"])
    elif name == "list_skill_files":
        return _list_skills()
    return f"Unknown tool: {name}"


def _create_skill(skill_name: str, description: str) -> str:
    """Generate skill via sub-agent, fill template, validate."""
    import json, re, ast
    from pathlib import Path
    from openai import OpenAI
    import config, providers

    # Validate name
    skill_name = skill_name.lower().replace(" ", "_").replace("-", "_")
    if not skill_name.isidentifier():
        return f"Error: '{skill_name}' is not a valid Python identifier"

    skills_dir = Path(__file__).parent
    target = skills_dir / f"{skill_name}.py"
    if target.exists():
        return f"Error: skill '{skill_name}' already exists at {target}"

    # Sub-agent: generate template parts as JSON
    client = providers.get_client()
    model = providers.get_model()

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SUBAGENT_PROMPT},
                {"role": "user", "content": f"Create a skill called '{skill_name}'.\n\nDescription: {description}"},
            ],
            temperature=0.3,
            max_tokens=4096,
        )

        raw = resp.choices[0].message.content or ""

        # Strip thinking tags
        raw = re.sub(r"<think>.*?</think>\s*", "", raw, flags=re.DOTALL).strip()

        # Strip markdown code fences if present
        if raw.startswith("```"):
            lines = raw.split("\n")
            lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            raw = "\n".join(lines)

        # Extract JSON from response
        json_match = re.search(r'\{.*\}', raw, re.DOTALL)
        if not json_match:
            return f"Sub-agent didn't return valid JSON.\n\nRaw output:\n{raw[:500]}"

        try:
            parts = json.loads(json_match.group())
        except json.JSONDecodeError as e:
            # Try repair
            from agent import _repair_json
            parts = _repair_json(json_match.group())
            if not parts:
                return f"Sub-agent returned broken JSON: {e}\n\nRaw:\n{raw[:500]}"

        # Validate required keys
        required_keys = ["docstring", "short_description", "tools", "execute_body"]
        missing = [k for k in required_keys if k not in parts]
        if missing:
            return f"Sub-agent output missing keys: {missing}\n\nGot: {list(parts.keys())}"

        # Format table DDL with proper indentation
        table_ddl = parts.get("table_ddl", "").strip()
        if table_ddl:
            # Ensure each statement is properly indented and wrapped in conn.execute()
            ddl_lines = []
            for stmt in re.split(r';\s*', table_ddl):
                stmt = stmt.strip()
                if not stmt:
                    continue
                # Wrap in conn.execute() if not already
                if not stmt.startswith("conn.execute"):
                    ddl_lines.append(f'    conn.execute("""{stmt}""")')
                else:
                    ddl_lines.append(f"    {stmt}")
            table_ddl = "\n".join(ddl_lines)
        else:
            table_ddl = "    pass  # No tables needed"

        # Format execute body with proper indentation
        execute_body = parts.get("execute_body", "").strip()
        # Ensure 4-space indent
        body_lines = execute_body.split("\n")
        formatted_body = []
        for line in body_lines:
            stripped = line.lstrip()
            if not stripped:
                formatted_body.append("")
                continue
            # Calculate current indent level
            current_indent = len(line) - len(stripped)
            # Ensure minimum 4-space indent (top-level inside execute)
            if current_indent < 4:
                formatted_body.append("    " + stripped)
            else:
                formatted_body.append(line)
        execute_body = "\n".join(formatted_body)

        # Format tools JSON
        tools_list = parts.get("tools", [])
        tools_json = json.dumps(tools_list, indent=4, ensure_ascii=False)

        # Fill template
        code = SKILL_TEMPLATE.format(
            docstring=parts.get("docstring", f"{skill_name} skill"),
            short_description=parts.get("short_description", description[:80]),
            instruction=parts.get("instruction", f"Use {skill_name} tools as needed."),
            tools_json=tools_json,
            table_ddl=table_ddl,
            execute_body=execute_body,
        )

        # Validate Python syntax before saving
        try:
            ast.parse(code)
        except SyntaxError as e:
            return f"Generated code has syntax error: {e}\n\nCode:\n{code[:800]}"

        # Save file
        target.write_text(code, encoding="utf-8")

        # Part 3: Auto-validate
        from skills import validate_skill, enable
        valid, validation_errors = validate_skill(str(target))

        if not valid:
            error_list = "\n".join(f"  - {e}" for e in validation_errors)
            return (
                f"⚠️ Skill '{skill_name}' created but has validation errors:\n{error_list}\n"
                f"File: {target}\n"
                f"Fix with write_file tool."
            )

        # Auto-enable
        enable(skill_name)

        # Count tools
        tool_count = len(tools_list)
        return (
            f"✓ Skill '{skill_name}' created and enabled!\n"
            f"  File: {target}\n"
            f"  Tools: {tool_count}\n"
            f"  Description: {parts.get('short_description', '')}"
        )

    except Exception as e:
        return f"Sub-agent error: {e}"


def _list_skills() -> str:
    from pathlib import Path
    skills_dir = Path(__file__).parent
    files = sorted(f.name for f in skills_dir.glob("*.py") if not f.name.startswith("_"))
    if not files:
        return "No skills found."
    return "Existing skills:\n" + "\n".join(f"  - {f}" for f in files)
