"""Workout tracking skill — log exercises, analyze progress, set goals."""

DESCRIPTION = "Track workouts: log exercises with weights/reps/duration, view history, analyze progress, set goals."

INSTRUCTION = """Workout tracker stores data in SQLite (same DB as main app).
When user says they trained — call add_workout with date, exercises JSON, and type.
When asked about history — call get_workout_history.
If result is empty — just tell the user "no workouts recorded yet", do NOT try to fix the database or read source code."""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "add_workout",
            "description": "Log a workout session with exercises, weights, reps, duration",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "Date (YYYY-MM-DD)"},
                    "exercises": {"type": "string", "description": "JSON array of exercises [{name, weight, reps, duration_minutes}]"},
                    "load_type": {"type": "string", "description": "Type: strength/cardio"},
                    "duration_minutes": {"type": "integer", "description": "Total duration in minutes"}
                },
                "required": ["date", "exercises"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_workout_history",
            "description": "Get workout history. Returns list of workouts or 'no workouts yet'",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "description": "Number of days to look back (default: 30)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_progress_analysis",
            "description": "Analyze progress for a specific exercise over time",
            "parameters": {
                "type": "object",
                "properties": {
                    "exercise_name": {"type": "string", "description": "Exercise name to analyze"},
                },
                "required": ["exercise_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_goal",
            "description": "Set a workout goal with target date",
            "parameters": {
                "type": "object",
                "properties": {
                    "goal_text": {"type": "string", "description": "Goal description"},
                    "target_date": {"type": "string", "description": "Target date (YYYY-MM-DD)"},
                },
                "required": ["goal_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_goals",
            "description": "List active workout goals",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
]


def execute(name: str, args: dict) -> str:
    import json
    from datetime import datetime, timedelta
    import db

    conn = db._get_conn()

    # Ensure tables exist
    conn.execute("""CREATE TABLE IF NOT EXISTS workouts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,
        exercises TEXT NOT NULL,
        load_type TEXT DEFAULT 'strength',
        duration_minutes INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS workout_goals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        goal_text TEXT NOT NULL,
        target_date TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.commit()

    if name == "add_workout":
        date = args.get("date", datetime.now().strftime("%Y-%m-%d"))
        exercises = args.get("exercises", "[]")
        load_type = args.get("load_type", "strength")
        duration = args.get("duration_minutes", 0)

        # Parse exercises to count them
        try:
            ex_list = json.loads(exercises) if isinstance(exercises, str) else exercises
            ex_count = len(ex_list) if isinstance(ex_list, list) else 1
        except:
            ex_list = exercises
            ex_count = 1

        exercises_str = json.dumps(ex_list, ensure_ascii=False) if not isinstance(exercises, str) else exercises

        conn.execute(
            "INSERT INTO workouts (date, exercises, load_type, duration_minutes) VALUES (?, ?, ?, ?)",
            (date, exercises_str, load_type, duration)
        )
        conn.commit()
        return f"✅ Workout logged: {date}, {ex_count} exercise(s), {duration}min ({load_type})"

    elif name == "get_workout_history":
        days = args.get("days", 30)
        since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

        rows = conn.execute(
            "SELECT date, exercises, load_type, duration_minutes FROM workouts WHERE date >= ? ORDER BY date DESC",
            (since,)
        ).fetchall()

        if not rows:
            return f"No workouts recorded in the last {days} days."

        lines = []
        for r in rows:
            try:
                ex = json.loads(r[1])
                if isinstance(ex, list):
                    ex_names = ", ".join(e.get("name", "?") for e in ex)
                else:
                    ex_names = str(ex)[:50]
            except:
                ex_names = str(r[1])[:50]
            lines.append(f"📅 {r[0]} | {ex_names} | {r[2]} | {r[3]}min")

        return f"Workouts (last {days} days):\n" + "\n".join(lines)

    elif name == "get_progress_analysis":
        exercise_name = args.get("exercise_name", "")
        if not exercise_name:
            return "Specify an exercise name to analyze."

        rows = conn.execute(
            "SELECT date, exercises FROM workouts ORDER BY date ASC"
        ).fetchall()

        data_points = []
        for r in rows:
            try:
                exercises = json.loads(r[1])
                if isinstance(exercises, list):
                    for ex in exercises:
                        if exercise_name.lower() in ex.get("name", "").lower():
                            data_points.append({
                                "date": r[0],
                                "weight": ex.get("weight", 0),
                                "reps": ex.get("reps", 0),
                                "duration": ex.get("duration_minutes", 0),
                            })
            except:
                pass

        if not data_points:
            return f"No data found for '{exercise_name}'."

        weights = [d["weight"] for d in data_points if d["weight"]]
        result = f"📊 {exercise_name}: {len(data_points)} sessions"
        if weights:
            result += f"\n  Max: {max(weights)}kg, Avg: {sum(weights)/len(weights):.1f}kg"
            if len(weights) > 1:
                trend = "📈" if weights[-1] > weights[0] else "📉" if weights[-1] < weights[0] else "➡️"
                result += f"\n  Trend: {trend} ({weights[0]}kg → {weights[-1]}kg)"
        return result

    elif name == "set_goal":
        goal_text = args.get("goal_text", "")
        target_date = args.get("target_date")

        conn.execute(
            "INSERT INTO workout_goals (goal_text, target_date) VALUES (?, ?)",
            (goal_text, target_date)
        )
        conn.commit()
        return f"🎯 Goal set: {goal_text}" + (f" (by {target_date})" if target_date else "")

    elif name == "get_goals":
        rows = conn.execute(
            "SELECT id, goal_text, target_date FROM workout_goals ORDER BY created_at DESC"
        ).fetchall()

        if not rows:
            return "No active goals."

        lines = []
        for r in rows:
            target = f" → {r[2]}" if r[2] else ""
            lines.append(f"🎯 #{r[0]}: {r[1]}{target}")
        return "Active goals:\n" + "\n".join(lines)

    return f"Unknown tool: {name}"
