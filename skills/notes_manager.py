"""Manage tagged notes with CRUD operations."""

DESCRIPTION = "Manage, search, and delete tagged notes via file system operations."

INSTRUCTION = """Use to create, retrieve, update, or delete notes organized by tags. Specify action (add/search/delete) and content/tag in parameters."""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "add_note",
            "description": "Create a new note with associated tags",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "The text content of the note"
                    },
                    "tags": {
                        "type": "array",
                        "items": {
                            "type": "string"
                        },
                        "description": "List of tags to associate with the note"
                    }
                },
                "required": [
                    "content",
                    "tags"
                ]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_notes",
            "description": "Find notes matching specific tags or content",
            "parameters": {
                "type": "object",
                "properties": {
                    "tags": {
                        "type": "array",
                        "items": {
                            "type": "string"
                        },
                        "description": "List of tags to search for (any matching tag)"
                    },
                    "content": {
                        "type": "string",
                        "description": "Search term to match within note content"
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delete_note",
            "description": "Remove a note by ID or tag association",
            "parameters": {
                "type": "object",
                "properties": {
                    "note_id": {
                        "type": "integer",
                        "description": "The unique identifier of the note to delete"
                    },
                    "tag": {
                        "type": "string",
                        "description": "Tag associated with the note to delete (alternative to ID)"
                    }
                },
                "required": [
                    "note_id"
                ]
            }
        }
    }
]


def execute(name: str, args: dict) -> str:
    """Handle tool calls for this skill."""
    import json
    from datetime import datetime
    import db

    conn = db._get_conn()

    # Ensure tables exist
    conn.execute("""CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT, tags TEXT, created_at TEXT)""")
    conn.commit()

    if name == "add_note":
        content = args.get("content", "")
        tags = args.get("tags", [])
        if isinstance(tags, str):
            tags = [tags]
        conn.execute(
            "INSERT INTO notes (content, tags, created_at) VALUES (?, ?, ?)",
            (content, json.dumps(tags), datetime.now().isoformat())
        )
        conn.commit()
        return f"Note added: '{content[:50]}' with tags {tags}"

    elif name == "search_notes":
        tag_list = args.get("tags", [])
        content_search = args.get("content", "")
        conditions = []
        params = []
        for tag in (tag_list if isinstance(tag_list, list) else [tag_list] if tag_list else []):
            conditions.append("tags LIKE ?")
            params.append(f"%{tag}%")
        if content_search:
            conditions.append("content LIKE ?")
            params.append(f"%{content_search}%")
        where = " OR ".join(conditions) if conditions else "1=1"
        rows = conn.execute(
            f"SELECT id, content, tags, created_at FROM notes WHERE {where} ORDER BY id DESC",
            params
        ).fetchall()
        if not rows:
            return "No matching notes found."
        lines = [f"#{r[0]}: {r[1][:80]} (tags: {r[2]}, {r[3][:10]})" for r in rows]
        return "\n".join(lines)

    elif name == "delete_note":
        note_id = args.get("note_id")
        tag = args.get("tag", "")
        if note_id:
            conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
            conn.commit()
            return f"Note #{note_id} deleted."
        elif tag:
            deleted = conn.execute("DELETE FROM notes WHERE tags LIKE ?", (f"%{tag}%",))
            conn.commit()
            return f"Notes with tag '{tag}' deleted."
        return "Provide note_id or tag to delete."

    return f"Unknown tool: {name}"
