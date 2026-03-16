"""A comprehensive tool for logging workouts, setting fitness goals, and visualizing progress across strength, bodyweight, and rowing activities."""

DESCRIPTION = "Track strength, bodyweight, and rowing workouts with goals and progress visualization."

INSTRUCTION = """Use to log sets/reps/weights or time/distance for various exercises. Set fitness goals like strength gains or distance targets. View progress charts and receive goal reminders via notifications."""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "log_session",
            "description": "Create, edit, or delete workout sessions with exercise details and metrics for strength, bodyweight, and rowing activities.",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "integer",
                        "description": "Unique identifier for the workout session (optional, omit to create new)"
                    },
                    "date": {
                        "type": "string",
                        "description": "Date of the workout session in YYYY-MM-DD format"
                    },
                    "type": {
                        "type": "string",
                        "description": "Workout type: strength, bodyweight, or rowing"
                    },
                    "sets": {
                        "type": "array",
                        "description": "Array of exercise sets with details like weight, reps, duration, distance"
                    },
                    "notes": {
                        "type": "string",
                        "description": "Optional notes about the session"
                    }
                },
                "required": [
                    "date",
                    "type"
                ]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "set_goal",
            "description": "Define fitness targets for strength gains, reps, distance, and adjust based on performance.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {
                        "type": "integer",
                        "description": "Unique identifier for the user"
                    },
                    "metric": {
                        "type": "string",
                        "description": "Fitness metric: strength, reps, distance, weight_lifted, or rowing_distance"
                    },
                    "target_value": {
                        "type": "number",
                        "description": "Target value for the fitness goal"
                    },
                    "deadline": {
                        "type": "string",
                        "description": "Goal deadline in YYYY-MM-DD format (optional)"
                    }
                },
                "required": [
                    "user_id",
                    "metric",
                    "target_value"
                ]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "view_progress",
            "description": "Visualize progress over time and manage push notifications for workout reminders.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {
                        "type": "integer",
                        "description": "Unique identifier for the user"
                    },
                    "time_range": {
                        "type": "string",
                        "description": "Time range for progress view: week, month, quarter, or year"
                    },
                    "metric": {
                        "type": "string",
                        "description": "Metric to visualize: strength, reps, distance, weight_lifted, or rowing_distance"
                    },
                    "enable_notifications": {
                        "type": "boolean",
                        "description": "Whether to enable push notifications for reminders"
                    },
                    "notification_frequency": {
                        "type": "string",
                        "description": "Frequency of notifications: daily, weekly, or monthly"
                    }
                },
                "required": [
                    "user_id"
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
    conn.execute("""CREATE TABLE IF NOT EXISTS workout_sessions (id INTEGER, date DATE, type TEXT, sets JSON, notes TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS fitness_goals (id INTEGER, user_id INTEGER, metric TEXT, target_value FLOAT, deadline DATE)""")
    conn.commit()

    if name == "log_session":
        action = args.get("action", "create")
        date = args.get("date", datetime.now().strftime("%Y-%m-%d"))
        type_ = args.get("type", "")
        sets = args.get("sets", "{}")
        notes = args.get("notes", "")

        if action == "create":
            conn.execute(
                "INSERT INTO workout_sessions (date, type, sets, notes) VALUES (?, ?, ?, ?)",
                (date, type_, sets, notes)
            )
            conn.commit()
            return f"Workout session logged for {date}: {type_}"
        elif action == "edit":
            session_id = args.get("id")
            if session_id:
                conn.execute(
                    "UPDATE workout_sessions SET date=?, type=?, sets=?, notes=? WHERE id=?",
                    (date, type_, sets, notes, session_id)
                )
                conn.commit()
                return f"Workout session updated for ID {session_id}"
            return "Session ID required for edit."
        elif action == "delete":
            session_id = args.get("id")
            if session_id:
                conn.execute("DELETE FROM workout_sessions WHERE id=?", (session_id,))
                conn.commit()
                return f"Workout session deleted for ID {session_id}"
            return "Session ID required for delete."
        else:
            return "Invalid action. Use create, edit, or delete."

    elif name == "set_goal":
        metric = args.get("metric", "")
        target_value = args.get("target_value")
        deadline = args.get("deadline")
        user_id = args.get("user_id")
        action = args.get("action", "create")

        if action == "create":
            conn.execute(
                "INSERT INTO fitness_goals (user_id, metric, target_value, deadline) VALUES (?, ?, ?, ?)",
                (user_id, metric, target_value, deadline)
            )
            conn.commit()
            return f"Goal set: {metric} = {target_value} by {deadline}"
        elif action == "update":
            goal_id = args.get("id")
            if goal_id:
                conn.execute(
                    "UPDATE fitness_goals SET target_value=?, deadline=? WHERE id=?",
                    (target_value, deadline, goal_id)
                )
                conn.commit()
                return f"Goal updated for ID {goal_id}"
            return "Goal ID required for update."
        elif action == "delete":
            goal_id = args.get("id")
            if goal_id:
                conn.execute("DELETE FROM fitness_goals WHERE id=?", (goal_id,))
                conn.commit()
                return f"Goal deleted for ID {goal_id}"
            return "Goal ID required for delete."
        else:
            return "Invalid action. Use create, update, or delete."

    elif name == "view_progress":
        metric = args.get("metric", "")
        user_id = args.get("user_id")

        if metric and user_id:
            rows = conn.execute(
                "SELECT id, date, type, sets FROM workout_sessions WHERE user_id=? ORDER BY date DESC LIMIT 10",
                (user_id,)
            ).fetchall()

            if not rows:
                return f"No workout sessions found for user {user_id}."

            lines = [f"#{r[0]}: {r[2]} on {r[1]} - Sets: {r[3]}" for r in rows]
            return "\n".join(lines)
        else:
            return "Provide metric and user_id to view progress."

    return f"Unknown tool: {name}"
