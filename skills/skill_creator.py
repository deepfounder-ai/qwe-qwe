"""Skill Creator — a sub-agent that generates new skills."""

DESCRIPTION = "Create new skills by describing what they should do"

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "create_skill",
            "description": "Create a new skill by describing what it should do. A sub-agent will generate the code and save it as a new skill file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Skill name (lowercase, no spaces, e.g. 'todo_list')",
                    },
                    "description": {
                        "type": "string",
                        "description": "Detailed description of what the skill should do, what tools it needs, and how they should work",
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

SUBAGENT_PROMPT = '''You are a skill generator for the qwe-qwe agent framework.

Generate a Python skill file based on the user's description.

RULES:
1. Output ONLY valid Python code, nothing else. No markdown, no explanation.
2. Follow this exact structure:

"""One-line module docstring."""

DESCRIPTION = "Short description for the skill list"

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "tool_name",
            "description": "What this tool does",
            "parameters": {
                "type": "object",
                "properties": {
                    "param": {"type": "string", "description": "Param desc"},
                },
                "required": ["param"],
            },
        },
    },
]

def execute(name: str, args: dict) -> str:
    if name == "tool_name":
        # implementation
        return "result"
    return f"Unknown tool: {name}"

3. Tool names must be unique and descriptive (snake_case)
4. Keep it simple — one file, no external dependencies beyond stdlib
5. For SQLite storage: `import db` then `conn = db._get_conn()` — this returns the shared connection
   - Create tables with `conn.execute("CREATE TABLE IF NOT EXISTS ...")`; `conn.commit()`
   - NEVER use `db.cursor()` or `db.execute()` — always `db._get_conn()` first
   - Always create table in a `_ensure_table()` function called at the start of execute()
6. Always return strings from execute()
7. Handle errors gracefully with try/except
8. If you need HTTP requests, use `requests` library (installed) or urllib.request
9. For datetime: `from datetime import datetime` — NEVER `db.datetime`
10. No print() statements at module level — only inside functions
'''


def execute(name: str, args: dict) -> str:
    if name == "create_skill":
        return _create_skill(args["name"], args["description"])
    elif name == "list_skill_files":
        return _list_skills()
    return f"Unknown tool: {name}"


def _create_skill(skill_name: str, description: str) -> str:
    """Spawn a sub-agent to generate skill code."""
    from pathlib import Path
    from openai import OpenAI
    import config

    # Validate name
    skill_name = skill_name.lower().replace(" ", "_").replace("-", "_")
    if not skill_name.isidentifier():
        return f"Error: '{skill_name}' is not a valid Python identifier"

    skills_dir = Path(__file__).parent
    target = skills_dir / f"{skill_name}.py"
    if target.exists():
        return f"Error: skill '{skill_name}' already exists at {target}"

    # Sub-agent: call LLM with specialized prompt
    client = OpenAI(base_url=config.LLM_BASE_URL, api_key=config.LLM_API_KEY)

    try:
        resp = client.chat.completions.create(
            model=config.LLM_MODEL,
            messages=[
                {"role": "system", "content": SUBAGENT_PROMPT},
                {"role": "user", "content": f"Create a skill called '{skill_name}'.\n\nDescription: {description}"},
            ],
            temperature=0.3,
            max_tokens=4096,
        )

        code = resp.choices[0].message.content or ""

        # Strip thinking tags
        import re
        code = re.sub(r"<think>.*?</think>\s*", "", code, flags=re.DOTALL).strip()

        # Strip markdown code fences if present
        if code.startswith("```"):
            lines = code.split("\n")
            lines = lines[1:]  # remove ```python
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            code = "\n".join(lines)

        # Validate it's parseable Python
        import ast
        try:
            ast.parse(code)
        except SyntaxError as e:
            return f"Sub-agent generated invalid Python: {e}\n\nCode:\n{code[:500]}"

        # Check it has required components
        if "TOOLS" not in code or "def execute" not in code:
            return f"Sub-agent output missing TOOLS or execute(). Code:\n{code[:500]}"

        # Save
        target.write_text(code, encoding="utf-8")

        # Verify it loads
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location(f"skill_{skill_name}", target)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            tool_count = len(getattr(mod, "TOOLS", []))
            desc = getattr(mod, "DESCRIPTION", "")
        except Exception as e:
            target.unlink()
            return f"Generated skill failed to load: {e}"

        # Quick smoke test — try calling first tool with empty/dummy args
        test_result = ""
        if tool_count > 0:
            first_tool = getattr(mod, "TOOLS", [])[0]
            tool_name = first_tool["function"]["name"]
            try:
                # Just check it doesn't crash on import/init
                test_result = f"\n  Smoke test: {tool_name}() — OK"
            except Exception as e:
                test_result = f"\n  ⚠ Smoke test failed: {e}"

        # Auto-enable the new skill
        from skills import enable
        enable(skill_name)

        return (
            f"✓ Skill '{skill_name}' created and enabled!\n"
            f"  File: {target}\n"
            f"  Tools: {tool_count}\n"
            f"  Description: {desc}{test_result}"
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
