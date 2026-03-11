"""Notes skill — simple markdown notes stored in SQLite."""

DESCRIPTION = "Create and manage quick notes"

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "create_note",
            "description": "Create a quick note with a title and content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Note title"},
                    "content": {"type": "string", "description": "Note content"},
                },
                "required": ["title", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_notes",
            "description": "List all saved notes. Returns titles and creation dates.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_note",
            "description": "Read a note by its title.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Note title to read"},
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_note",
            "description": "Delete a note by title.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Note title to delete"},
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_note",
            "description": "Update an existing note's content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Note title to edit"},
                    "content": {"type": "string", "description": "New content"},
                },
                "required": ["title", "content"],
            },
        },
    },
]


def _ensure_table():
    import db
    conn = db._get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            ts REAL NOT NULL
        )
    """)
    conn.commit()


def execute(name: str, args: dict) -> str:
    import db, time
    _ensure_table()
    conn = db._get_conn()

    if name == "create_note":
        conn.execute("INSERT INTO notes (title, content, ts) VALUES (?,?,?)",
                     (args["title"], args["content"], time.time()))
        conn.commit()
        return f"✓ Note '{args['title']}' saved"

    elif name == "list_notes":
        rows = conn.execute("SELECT title, ts FROM notes ORDER BY ts DESC LIMIT 20").fetchall()
        if not rows:
            return "No notes yet."
        lines = []
        for title, ts in rows:
            from datetime import datetime
            dt = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
            lines.append(f"- {title} ({dt})")
        return "\n".join(lines)

    elif name == "read_note":
        row = conn.execute("SELECT content, ts FROM notes WHERE title=?", (args["title"],)).fetchone()
        if not row:
            return f"Note '{args['title']}' not found."
        return row[0]

    elif name == "delete_note":
        r = conn.execute("DELETE FROM notes WHERE title=?", (args["title"],))
        conn.commit()
        if r.rowcount:
            return f"✓ Note '{args['title']}' deleted"
        return f"Note '{args['title']}' not found."

    elif name == "edit_note":
        r = conn.execute("UPDATE notes SET content=?, ts=? WHERE title=?",
                         (args["content"], time.time(), args["title"]))
        conn.commit()
        if r.rowcount:
            return f"✓ Note '{args['title']}' updated"
        return f"Note '{args['title']}' not found."

    return f"Unknown tool: {name}"
