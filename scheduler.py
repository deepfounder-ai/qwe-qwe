"""Scheduler — cron-like task runner with SQLite storage."""

import threading
import time
import json
import re
from datetime import datetime, timezone, timedelta
import db
import config
import providers
import logger

_log = logger.get("scheduler")


def _tz():
    """Resolve the scheduler timezone.

    Preference order:
      1. ``config.get("tz_name")`` — an IANA zone like ``"Europe/Moscow"`` or
         ``"America/New_York"`` — resolved via ``zoneinfo``. Honours DST so a
         ``daily HH:MM`` task fires at the wall clock the user expects across
         transitions.
      2. Fallback: a fixed offset from ``config.TZ_OFFSET`` (the legacy
         behaviour). Fixed offsets do not track DST and will drift ±1h
         across transitions — acceptable if the user's locale doesn't have
         DST or no ``tz_name`` is configured.
    """
    try:
        tz_name = (config.get("tz_name") or "").strip()
    except Exception:
        tz_name = ""
    if tz_name:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(tz_name)
        except Exception as e:
            _log.warning(f"invalid tz_name={tz_name!r} ({e}); falling back to fixed offset")
    return timezone(timedelta(hours=config.TZ_OFFSET))

_thread_started = False
_callbacks = []  # [(fn, args)] — called when task completes
HEARTBEAT_TASK_NAME = "__heartbeat__"
SYNTHESIS_TASK_NAME = "__synthesis__"


def _ensure_table():
    db.execute("""
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


def add(name: str, task: str, schedule: str, skip_dry_run: bool = False) -> dict:
    """Add a scheduled task with automatic dry-run validation.

    Schedule formats:
      "in 5m"       — run once in 5 minutes
      "in 2h"       — run once in 2 hours
      "every 30m"   — repeat every 30 minutes
      "every 2h"    — repeat every 2 hours
      "daily 09:00" — every day at 09:00
      "HH:MM"       — once today/tomorrow at that time

    Pre-validation: executes the task once (with real side effects!) before
    saving. If execution fails, the task is NOT saved and an error with
    hints is returned so the model can retry with a corrected task description.
    Note: for send/notify tasks, the pre-validation WILL send a real message.
    """
    _ensure_table()

    next_run, repeat = _parse_schedule(schedule)
    if next_run is None:
        return {"error": f"Can't parse schedule: '{schedule}'"}

    # Dry-run validation (unless explicitly skipped)
    if not skip_dry_run:
        _log.info(f"dry-run for '{name}': {task[:100]}")
        dry_result = _execute_task(task, max_rounds=5)
        validation = _validate_dry_run(dry_result, task)
        if not validation["ok"]:
            _log.warning(f"dry-run failed for '{name}': {validation['reason']}")
            return {
                "error": f"Dry-run failed: {validation['reason']}",
                "output": dry_result[:500],
                "hint": validation.get("hint", ""),
                "saved": False,
            }
        _log.info(f"dry-run passed for '{name}': {dry_result[:100]}")

    db.execute(
        "INSERT INTO scheduled_tasks (name, task, schedule, next_run, repeat, enabled) VALUES (?,?,?,?,?,1)",
        (name, task, schedule, next_run, 1 if repeat else 0)
    )

    dt = datetime.fromtimestamp(next_run, _tz()).strftime("%H:%M:%S")
    result = {"ok": True, "name": name, "next_run": dt, "repeat": bool(repeat)}
    if not skip_dry_run:
        result["dry_run"] = "passed"
        result["preview"] = dry_result[:200]
    return result


# ── Dry-run validation ──

_DRY_RUN_ERROR_MARKERS = [
    "command not found", "no such file or directory", "permission denied",
    "blocked:", "not allowed", "traceback (most recent call last)",
    "modulenotfounderror", "task completed (max rounds)",
    "connection refused", "name or service not known",
    "\nerrno ", "importerror:",
]

# Patterns that look like errors only at line start or after newline
_DRY_RUN_ERROR_PATTERNS = re.compile(
    r"(?:^|\n)\s*(?:error:|http error:|fatal:|exception:)", re.IGNORECASE
)


def _validate_dry_run(result: str, task_description: str) -> dict:
    """Check whether a dry-run (pre-validation execution) succeeded.

    Note: this is a real execution with side effects, not a sandboxed
    dry-run. The task runs through the full LLM tool-call loop.
    """
    if not result or not result.strip():
        return {"ok": False, "reason": "Empty output",
                "hint": "Task produced no output — check if commands exist and paths are correct"}

    lower = result.lower()
    for marker in _DRY_RUN_ERROR_MARKERS:
        if marker in lower:
            return {"ok": False, "reason": f"Output contains error: '{marker}'",
                    "hint": "Try using built-in tools (http_request, read_file, shell) instead of external scripts"}
    if _DRY_RUN_ERROR_PATTERNS.search(result):
        match = _DRY_RUN_ERROR_PATTERNS.search(result).group().strip()
        return {"ok": False, "reason": f"Output contains error pattern: '{match}'",
                "hint": "Try using built-in tools (http_request, read_file, shell) instead of external scripts"}

    # For send/notify tasks — verify delivery confirmation
    task_lower = task_description.lower()
    if any(w in task_lower for w in ("telegram", "send", "notify", "webhook")):
        if "ok" not in lower and "sent" not in lower and "200" not in result:
            return {"ok": False, "reason": "Send task didn't confirm delivery",
                    "hint": "Use http_request tool to POST to the API directly. Check secret_get for tokens."}

    return {"ok": True}


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
    rows = db.fetchall(
        "SELECT id, name, task, schedule, next_run, repeat, enabled FROM scheduled_tasks ORDER BY next_run"
    )
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
    row = db.fetchone("SELECT name FROM scheduled_tasks WHERE id=?", (task_id,))
    if not row:
        return f"✗ Task #{task_id} not found"
    if row[0] == HEARTBEAT_TASK_NAME:
        return f"✗ Heartbeat task cannot be removed. Use settings to disable it."
    db.execute("DELETE FROM scheduled_tasks WHERE id=?", (task_id,))
    return f"✓ Task #{task_id} removed"


def on_complete(fn):
    """Register callback for task completion: fn(name, task, result)."""
    _callbacks.append(fn)


def _register_heartbeat():
    """Auto-register heartbeat cron task if enabled and not already registered."""
    val = db.kv_get("heartbeat:enabled")
    if val == "0":  # enabled by default (None or "1" → enabled)
        return
    _ensure_table()
    row = db.fetchone(
        "SELECT id FROM scheduled_tasks WHERE name=?", (HEARTBEAT_TASK_NAME,)
    )
    if row:
        return  # already registered
    interval = config.get("heartbeat_interval_min")
    schedule = f"every {interval}m"
    next_run = time.time() + interval * 60
    db.execute(
        "INSERT INTO scheduled_tasks (name, task, schedule, next_run, repeat, enabled) VALUES (?,?,?,?,1,1)",
        (HEARTBEAT_TASK_NAME, HEARTBEAT_TASK_NAME, schedule, next_run)
    )
    _log.info(f"heartbeat registered: every {interval}m")


def _unregister_heartbeat():
    """Remove heartbeat task from scheduler."""
    _ensure_table()
    db.execute("DELETE FROM scheduled_tasks WHERE name=?", (HEARTBEAT_TASK_NAME,))
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


def _register_synthesis():
    """Auto-register night synthesis cron if enabled."""
    if not config.get("synthesis_enabled"):
        return
    _ensure_table()
    row = db.fetchone(
        "SELECT id FROM scheduled_tasks WHERE name=?", (SYNTHESIS_TASK_NAME,)
    )
    if row:
        return  # already registered
    synthesis_time = config.get("synthesis_time")  # e.g. "03:00"
    schedule = f"daily {synthesis_time}"
    # Parse HH:MM to next run timestamp (timezone-aware)
    h, m = map(int, synthesis_time.split(":"))
    now = datetime.now(_tz())
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    next_run = target.timestamp()
    db.execute(
        "INSERT INTO scheduled_tasks (name, task, schedule, next_run, repeat, enabled) VALUES (?,?,?,?,1,1)",
        (SYNTHESIS_TASK_NAME, SYNTHESIS_TASK_NAME, schedule, next_run)
    )
    _log.info(f"synthesis registered: {schedule}")


def start():
    """Start the scheduler background thread."""
    global _thread_started
    if _thread_started:
        return
    _thread_started = True
    _register_heartbeat()
    _register_synthesis()
    t = threading.Thread(target=_loop, daemon=True)
    t.start()


def _loop():
    """Main scheduler loop — checks every 30 seconds."""
    # Initial delay: let telegram bot and other services connect first.
    # Without this, tasks with past next_run fire immediately on restart
    # but telegram isn't ready yet → notifications silently dropped.
    time.sleep(15)
    while True:
        try:
            _check_and_run()
        except Exception:
            _log.error("scheduler loop error", exc_info=True)
        time.sleep(30)


def _check_and_run():
    """Check for due tasks and execute them."""
    _ensure_table()
    now = time.time()

    rows = db.fetchall(
        "SELECT id, name, task, schedule, repeat FROM scheduled_tasks WHERE enabled=1 AND next_run<=?",
        (now,)
    )

    for id_, name, task, schedule, repeat in rows:
        # Pre-reschedule to prevent duplicate execution if task takes >30s
        if repeat:
            _, interval = _parse_schedule(schedule)
            next_run = now + (interval if interval else 3600)
            db.execute(
                "UPDATE scheduled_tasks SET next_run=? WHERE id=?",
                (next_run, id_)
            )
        else:
            # Disable one-time task before execution
            db.execute("UPDATE scheduled_tasks SET enabled=0 WHERE id=?", (id_,))

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
            db.execute("UPDATE scheduled_tasks SET last_run=? WHERE id=?", (now, id_))
        else:
            db.execute("DELETE FROM scheduled_tasks WHERE id=?", (id_,))


def _execute_task(task_desc: str, max_rounds: int = 10) -> str:
    """Run a task through the LLM.

    Args:
        task_desc: task description or special name (e.g. __heartbeat__).
        max_rounds: maximum tool call rounds (default 10, dry-run uses 5).
    """
    import config
    import tools

    # Heartbeat tasks — special handling
    if task_desc == HEARTBEAT_TASK_NAME:
        return _execute_heartbeat()

    # Synthesis tasks — direct Python, no LLM
    if task_desc == SYNTHESIS_TASK_NAME:
        import synthesis
        return synthesis.run_synthesis()

    # Simple reminders don't need LLM — just return a clean notification
    reminder_markers = ["remind", "напомни", "напоминание", "напомнить", "выпить", "drink", "stretch", "break"]
    lower_task = task_desc.lower()
    if any(m in lower_task for m in reminder_markers) and len(task_desc) < 200:
        return f"🔔 Reminder: {task_desc}"

    data_dir = str(config.DATA_DIR)
    client = providers.get_client()
    messages = [
        {"role": "system", "content": (
            "You are a scheduled task worker. Execute the task and return the result.\n"
            "If the task is a reminder, just return the reminder text — do NOT create new reminders.\n"
            f"Your files: logs={data_dir}/logs/, workspace={data_dir}/workspace/\n"
            "Use secret_get() for API keys/tokens — secrets are in encrypted vault.\n"
            "Use memory_search() to find saved info (tokens, configs, previous results).\n"
            "Use http_request() for HTTP/API calls instead of curl.\n"
            "Use tools step by step. Be concise."
        )},
        {"role": "user", "content": task_desc},
    ]

    all_tools = tools.get_all_tools()
    rounds = 0

    while rounds < max_rounds:
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
