"""Scheduler — cron-like task runner with SQLite storage."""

import threading, time, json, re
from datetime import datetime, timezone, timedelta
import db, config, providers
import logger

_log = logger.get("scheduler")


def _tz():
    return timezone(timedelta(hours=config.TZ_OFFSET))

_thread_started = False
_callbacks = []  # [(fn, args)] — called when task completes


def _ensure_table():
    conn = db._get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scheduled_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            task TEXT NOT NULL,
            schedule TEXT NOT NULL,
            next_run REAL NOT NULL,
            last_run REAL,
            repeat INTEGER DEFAULT 0,
            enabled INTEGER DEFAULT 1
        )
    """)
    conn.commit()


def add(name: str, task: str, schedule: str) -> dict:
    """Add a scheduled task.
    
    Schedule formats:
      "in 5m"       — run once in 5 minutes
      "in 2h"       — run once in 2 hours
      "every 30m"   — repeat every 30 minutes
      "every 2h"    — repeat every 2 hours
      "daily 09:00" — every day at 09:00
      "HH:MM"       — once today/tomorrow at that time
    """
    _ensure_table()
    conn = db._get_conn()

    next_run, repeat = _parse_schedule(schedule)
    if next_run is None:
        return {"error": f"Can't parse schedule: '{schedule}'"}

    conn.execute(
        "INSERT INTO scheduled_tasks (name, task, schedule, next_run, repeat, enabled) VALUES (?,?,?,?,?,1)",
        (name, task, schedule, next_run, 1 if repeat else 0)
    )
    conn.commit()

    dt = datetime.fromtimestamp(next_run, _tz()).strftime("%H:%M:%S")
    return {"ok": True, "name": name, "next_run": dt, "repeat": bool(repeat)}


def _parse_schedule(schedule: str) -> tuple:
    """Parse schedule string → (next_run_timestamp, repeat_seconds_or_0)."""
    now = time.time()
    s = schedule.strip().lower()

    # "in 5m", "in 2h", "in 30s"
    m = re.match(r"in\s+(\d+)\s*(s|m|h)", s)
    if m:
        val, unit = int(m.group(1)), m.group(2)
        secs = val * {"s": 1, "m": 60, "h": 3600}[unit]
        return (now + secs, 0)

    # "every 30m", "every 2h"
    m = re.match(r"every\s+(\d+)\s*(s|m|h)", s)
    if m:
        val, unit = int(m.group(1)), m.group(2)
        secs = val * {"s": 1, "m": 60, "h": 3600}[unit]
        return (now + secs, secs)

    # "daily HH:MM"
    m = re.match(r"daily\s+(\d{1,2}):(\d{2})", s)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        today = datetime.now(_tz()).replace(hour=h, minute=mi, second=0, microsecond=0)
        ts = today.timestamp()
        if ts <= now:
            ts += 86400
        return (ts, 86400)

    # "HH:MM" — one-time
    m = re.match(r"(\d{1,2}):(\d{2})$", s)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        today = datetime.now(_tz()).replace(hour=h, minute=mi, second=0, microsecond=0)
        ts = today.timestamp()
        if ts <= now:
            ts += 86400
        return (ts, 0)

    return (None, 0)


def list_tasks() -> list[dict]:
    _ensure_table()
    conn = db._get_conn()
    rows = conn.execute(
        "SELECT id, name, task, schedule, next_run, repeat, enabled FROM scheduled_tasks ORDER BY next_run"
    ).fetchall()
    tasks = []
    for id_, name, task, schedule, next_run, repeat, enabled in rows:
        dt = datetime.fromtimestamp(next_run, _tz()).strftime("%Y-%m-%d %H:%M")
        tasks.append({
            "id": id_, "name": name, "task": task, "schedule": schedule,
            "next_run": dt, "repeat": bool(repeat), "enabled": bool(enabled),
        })
    return tasks


def remove(task_id: int) -> str:
    _ensure_table()
    conn = db._get_conn()
    conn.execute("DELETE FROM scheduled_tasks WHERE id=?", (task_id,))
    conn.commit()
    return f"✓ Task #{task_id} removed"


def on_complete(fn):
    """Register callback for task completion: fn(name, task, result)."""
    _callbacks.append(fn)


HEARTBEAT_TASK_NAME = "__heartbeat__"


def _register_heartbeat():
    """Auto-register heartbeat cron task if enabled and not already registered."""
    val = db.kv_get("heartbeat:enabled")
    if val == "0":  # enabled by default (None or "1" → enabled)
        return
    _ensure_table()
    conn = db._get_conn()
    row = conn.execute(
        "SELECT id FROM scheduled_tasks WHERE name=?", (HEARTBEAT_TASK_NAME,)
    ).fetchone()
    if row:
        return  # already registered
    interval = config.get("heartbeat_interval_min")
    schedule = f"every {interval}m"
    next_run = time.time() + interval * 60
    conn.execute(
        "INSERT INTO scheduled_tasks (name, task, schedule, next_run, repeat, enabled) VALUES (?,?,?,?,1,1)",
        (HEARTBEAT_TASK_NAME, HEARTBEAT_TASK_NAME, schedule, next_run)
    )
    conn.commit()
    _log.info(f"heartbeat registered: every {interval}m")


def _unregister_heartbeat():
    """Remove heartbeat task from scheduler."""
    _ensure_table()
    conn = db._get_conn()
    conn.execute("DELETE FROM scheduled_tasks WHERE name=?", (HEARTBEAT_TASK_NAME,))
    conn.commit()
    _log.info("heartbeat unregistered")


def _execute_heartbeat() -> str:
    """Execute heartbeat: run checklist items through agent."""
    raw = db.kv_get("heartbeat:items")
    if not raw:
        return "HEARTBEAT_OK"
    try:
        items = json.loads(raw)
    except json.JSONDecodeError:
        return "HEARTBEAT_OK"
    if not items:
        return "HEARTBEAT_OK"

    import agent
    prompt = (
        "Here are your periodic tasks:\n"
        + "\n".join(f"- {item}" for item in items)
        + "\n\nCheck what's relevant now. If nothing needs attention, reply HEARTBEAT_OK."
    )
    try:
        result = agent.run(prompt, thread_id=None, source="heartbeat")
        return result.reply
    except Exception as e:
        _log.error(f"heartbeat agent error: {e}")
        return f"Error: {e}"


def start():
    """Start the scheduler background thread."""
    global _thread_started
    if _thread_started:
        return
    _thread_started = True
    _register_heartbeat()
    t = threading.Thread(target=_loop, daemon=True)
    t.start()


def _loop():
    """Main scheduler loop — checks every 30 seconds."""
    while True:
        try:
            _check_and_run()
        except Exception:
            _log.error("scheduler loop error", exc_info=True)
        time.sleep(30)


def _check_and_run():
    """Check for due tasks and execute them."""
    _ensure_table()
    conn = db._get_conn()
    now = time.time()

    rows = conn.execute(
        "SELECT id, name, task, schedule, repeat FROM scheduled_tasks WHERE enabled=1 AND next_run<=?",
        (now,)
    ).fetchall()

    for id_, name, task, schedule, repeat in rows:
        # Pre-reschedule to prevent duplicate execution if task takes >30s
        if repeat:
            _, interval = _parse_schedule(schedule)
            next_run = now + (interval if interval else 3600)
            conn.execute(
                "UPDATE scheduled_tasks SET next_run=? WHERE id=?",
                (next_run, id_)
            )
        else:
            # Disable one-time task before execution
            conn.execute("UPDATE scheduled_tasks SET enabled=0 WHERE id=?", (id_,))
        conn.commit()

        # Execute task
        _log.info(f"cron firing: #{id_} '{name}' → {task[:80]}")
        result = _execute_task(task)
        _log.info(f"cron done: #{id_} '{name}' → {result[:200]}")

        # Notify callbacks
        for fn in _callbacks:
            try:
                fn(name, task, result)
            except Exception:
                _log.warning(f"cron callback error for #{id_}", exc_info=True)

        # Finalize
        if repeat:
            conn.execute("UPDATE scheduled_tasks SET last_run=? WHERE id=?", (now, id_))
        else:
            conn.execute("DELETE FROM scheduled_tasks WHERE id=?", (id_,))
        conn.commit()


def _execute_task(task_desc: str) -> str:
    """Run a task through the LLM."""
    import config, tools

    # Heartbeat tasks — special handling
    if task_desc == HEARTBEAT_TASK_NAME:
        return _execute_heartbeat()

    # Simple reminders don't need LLM — just return a clean notification
    reminder_markers = ["remind", "напомни", "напоминание", "напомнить", "выпить", "drink", "stretch", "break"]
    lower_task = task_desc.lower()
    if any(m in lower_task for m in reminder_markers) and len(task_desc) < 200:
        return f"🔔 Reminder: {task_desc}"

    client = providers.get_client()
    messages = [
        {"role": "system", "content": (
            "You are a background task worker. Execute the task and return the result. "
            "If the task is a reminder, just return the reminder text — do NOT create new reminders or timers. "
            "Use tools only when needed. Be concise."
        )},
        {"role": "user", "content": task_desc},
    ]

    all_tools = tools.get_all_tools()
    rounds = 0

    while rounds < 5:
        try:
            resp = client.chat.completions.create(
                model=providers.get_model(),
                messages=messages,
                tools=all_tools,
                tool_choice="auto",
                temperature=0.5,
                max_tokens=1024,
            )
        except Exception as e:
            _log.error(f"cron LLM call failed: {e}", exc_info=True)
            return f"Error: {e}"

        msg = resp.choices[0].message

        if msg.tool_calls:
            assistant_msg = {"role": "assistant", "content": msg.content or ""}
            assistant_msg["tool_calls"] = [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in msg.tool_calls
            ]
            messages.append(assistant_msg)

            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    args = {}
                result = tools.execute(tc.function.name, args)
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
            rounds += 1
            continue

        reply = re.sub(r"<think>.*?</think>\s*", "", msg.content or "", flags=re.DOTALL).strip()
        return reply

    return "Task completed (max rounds)"
