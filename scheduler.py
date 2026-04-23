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
            enabled INTEGER DEFAULT 1,
            run_count INTEGER DEFAULT 0,
            last_status TEXT,
            last_error TEXT,
            last_duration_ms INTEGER,
            last_result TEXT,
            thread_id TEXT
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

    # Dry-run validation (unless explicitly skipped).
    # max_rounds bumped 5 → 8: real tasks (logs → filter → send to telegram)
    # often need 6-7 rounds just to explore the filesystem before executing
    # the actual action. 5 was too tight.
    if not skip_dry_run:
        _log.info(f"dry-run for '{name}': {task[:100]}")
        dry_result = _execute_task(task, max_rounds=8)
        validation = _validate_dry_run(dry_result, task)
        if not validation["ok"]:
            _log.warning(f"dry-run failed for '{name}': {validation['reason']}")
            return {
                "error": f"Dry-run failed: {validation['reason']}",
                "output": dry_result[:500],
                "hint": validation.get("hint", ""),
                # UI signal: offer the user a "save anyway" retry with
                # skip_dry_run=true instead of making them re-type the form.
                "offer_skip": True,
                "saved": False,
            }
        _log.info(f"dry-run passed for '{name}': {dry_result[:100]}")

    # One thread per routine, created at save time and reused on every
    # firing. System tasks (heartbeat, synthesis) and quick reminders
    # don't get a thread — they're stateless and noise to surface as
    # chat logs.
    routine_thread_id: str | None = None
    if _is_routine(task):
        try:
            import threads
            t = threads.create(f"Routine · {name}", meta={
                "kind": "routine",
                "routine_name": name,
                "schedule": schedule,
                "created_at": time.time(),
            })
            routine_thread_id = t["id"]
        except Exception as e:
            _log.warning(f"failed to create routine thread for '{name}': {e}")

    db.execute(
        "INSERT INTO scheduled_tasks "
        "(name, task, schedule, next_run, repeat, enabled, thread_id) "
        "VALUES (?,?,?,?,?,1,?)",
        (name, task, schedule, next_run, 1 if repeat else 0, routine_thread_id)
    )

    dt = datetime.fromtimestamp(next_run, _tz()).strftime("%H:%M:%S")
    result = {"ok": True, "name": name, "next_run": dt, "repeat": bool(repeat)}
    if routine_thread_id:
        result["thread_id"] = routine_thread_id
    if not skip_dry_run:
        result["dry_run"] = "passed"
        result["preview"] = dry_result[:200]
    return result


# ── Runtime error classification (shared with dry-run) ──


def _looks_like_error(result: str) -> bool:
    """Heuristic: does a task result look like a failure?

    Used to classify live runs as ok/err for UI stats. A stricter version
    (``_validate_dry_run``) runs on pre-save validation; this one just
    flags obvious error text so users don't see every run as "ok" when
    the task actually crashed.
    """
    if not result:
        return False
    low = result.lower()
    for marker in _DRY_RUN_ERROR_MARKERS:
        if marker in low:
            return True
    if _DRY_RUN_ERROR_PATTERNS.search(result):
        return True
    return False


# ── Dry-run validation ──

# Strict failure markers — output containing these means the task is
# genuinely broken (missing binary, permission denied, traceback, etc).
# NOTE: "task completed (max rounds)" used to live here but was removed —
# hitting max rounds means the task is *complex* (agent explored filesystem
# for a while before composing the reply), not that it's broken. Rejecting
# complex-but-valid tasks on dry-run was the top cron-creation pain point
# reported in v0.17.28.
_DRY_RUN_ERROR_MARKERS = [
    "command not found", "no such file or directory", "permission denied",
    "blocked:", "not allowed", "traceback (most recent call last)",
    "modulenotfounderror",
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

    # For send/notify tasks — verify delivery confirmation.
    # This check is a pragmatic heuristic, not a correctness proof: we scan
    # the agent's final reply for any of several confirmation phrases across
    # English + Russian. Historically this was English-only (ok/sent/200)
    # which rejected every task where the agent confirmed in Russian
    # ("Отправил сводку в Telegram"). If a task genuinely failed to send,
    # it'll have an error marker that's already been caught above — we
    # don't need to double-check here, so the bar stays low.
    task_lower = task_description.lower()
    if any(w in task_lower for w in ("telegram", "send", "notify", "webhook",
                                      "отправ", "уведом", "пришли", "пиши", "напиши")):
        confirmations_en = ("ok", "sent", "200", "delivered", "posted", "message_id",
                            "ok=true", "success", "done")
        confirmations_ru = ("отправил", "отправлено", "отправлена", "послал", "послано",
                            "доставлено", "успешно", "готово", "сделано")
        ok_markers = confirmations_en + confirmations_ru
        if not any(m in lower for m in ok_markers):
            return {"ok": False, "reason": "Send task didn't confirm delivery",
                    "hint": "Use http_request or the telegram tool directly, and make sure "
                            "the final reply mentions the send succeeded."}

    return {"ok": True}


# Day-of-week aliases used by the weekly schedule parser.
# Python's datetime.weekday(): Monday=0 ... Sunday=6.
_DOW = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
    # Russian short names — users write schedules in their language.
    "пн": 0, "вт": 1, "ср": 2, "чт": 3, "пт": 4, "сб": 5, "вс": 6,
}
_DOW_ALIASES = {
    "weekdays": "mon,tue,wed,thu,fri",
    "weekends": "sat,sun",
    "будни": "mon,tue,wed,thu,fri",
    "выходные": "sat,sun",
}


def _next_weekly_run(days: list[int], h: int, mi: int, now: float) -> float:
    """Given a set of weekdays (0=Mon..6=Sun) and HH:MM, return the next
    fire timestamp >= now. Repeat interval for weekly is always 1 week
    (604800s) — the loop finds the next matching day each time."""
    nowdt = datetime.fromtimestamp(now, _tz())
    today_wd = nowdt.weekday()
    for offset in range(8):  # 7 days ahead + today
        candidate_wd = (today_wd + offset) % 7
        if candidate_wd not in days:
            continue
        candidate = nowdt.replace(hour=h, minute=mi, second=0, microsecond=0) + \
                    timedelta(days=offset)
        if candidate.timestamp() > now:
            return candidate.timestamp()
    # Shouldn't reach here — fallback to one week from now
    return now + 7 * 86400


def _parse_schedule(schedule: str) -> tuple:
    """Parse schedule string → (next_run_timestamp, repeat_seconds_or_0).

    Supported grammar:
      in 5m / in 2h / in 30s            — one-off, relative
      every 30m / every 2h              — repeat every N units
      every 2 days 09:00                — every N days at that time
      daily HH:MM                       — every day at HH:MM
      weekdays HH:MM / weekends HH:MM   — Mon-Fri / Sat-Sun at HH:MM
      mon HH:MM                         — every Monday at HH:MM (short
                                          names: mon tue wed thu fri sat sun)
      mon,wed,fri HH:MM                 — any subset, comma-separated
      HH:MM                             — one-off today/tomorrow at that time
    """
    now = time.time()
    s = schedule.strip().lower()

    # "in 5m", "in 2h", "in 30s"
    m = re.match(r"in\s+(\d+)\s*(s|m|h)", s)
    if m:
        val, unit = int(m.group(1)), m.group(2)
        secs = val * {"s": 1, "m": 60, "h": 3600}[unit]
        return (now + secs, 0)

    # "every N days HH:MM" — check BEFORE "every Nm/h" so days/hours don't
    # both match "every 2 d..." (they can't, but explicit ordering is safer).
    m = re.match(r"every\s+(\d+)\s+days?\s+(\d{1,2}):(\d{2})$", s)
    if m:
        n, h, mi = int(m.group(1)), int(m.group(2)), int(m.group(3))
        today = datetime.now(_tz()).replace(hour=h, minute=mi, second=0, microsecond=0)
        ts = today.timestamp()
        if ts <= now:
            ts += n * 86400
        return (ts, n * 86400)

    # "every 30m", "every 2h", "every 30s"
    m = re.match(r"every\s+(\d+)\s*(s|m|h)$", s)
    if m:
        val, unit = int(m.group(1)), m.group(2)
        secs = val * {"s": 1, "m": 60, "h": 3600}[unit]
        return (now + secs, secs)

    # "daily HH:MM"
    m = re.match(r"daily\s+(\d{1,2}):(\d{2})$", s)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        today = datetime.now(_tz()).replace(hour=h, minute=mi, second=0, microsecond=0)
        ts = today.timestamp()
        if ts <= now:
            ts += 86400
        return (ts, 86400)

    # Weekly: "weekdays HH:MM", "weekends HH:MM", "mon HH:MM",
    # "mon,wed,fri HH:MM". Aliases expand before day parsing.
    m = re.match(r"([a-z,а-яё]+)\s+(\d{1,2}):(\d{2})$", s)
    if m:
        days_spec = _DOW_ALIASES.get(m.group(1), m.group(1))
        h, mi = int(m.group(2)), int(m.group(3))
        day_tokens = [d.strip() for d in days_spec.split(",") if d.strip()]
        if day_tokens and all(tok in _DOW for tok in day_tokens):
            days = sorted({_DOW[tok] for tok in day_tokens})
            ts = _next_weekly_run(days, h, mi, now)
            # Weekly repeat: scheduler re-computes next fire each time via
            # _parse_schedule, so the "interval" we return just tells the
            # loop not to delete this as a one-off. 7 days is the natural
            # cadence for a weekly schedule.
            return (ts, 7 * 86400)

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
        "SELECT id, name, task, schedule, next_run, last_run, repeat, enabled, "
        "       run_count, last_status, last_error, last_duration_ms, last_result, "
        "       thread_id "
        "FROM scheduled_tasks ORDER BY next_run"
    )
    tasks = []
    for (id_, name, task, schedule, next_run, last_run, repeat, enabled,
         run_count, last_status, last_error, last_duration_ms, last_result,
         thread_id) in rows:
        next_dt = datetime.fromtimestamp(next_run, _tz()).strftime("%Y-%m-%d %H:%M")
        last_dt = (datetime.fromtimestamp(last_run, _tz()).strftime("%Y-%m-%d %H:%M")
                    if last_run else "")
        # UI's "last" field — compact human string ("ok · 42ms" / "err · Timeout")
        if last_status == "ok":
            last = f"ok · {last_duration_ms}ms" if last_duration_ms else "ok"
        elif last_status == "err":
            err_short = (last_error or "failed").split("\n", 1)[0][:60]
            last = f"err · {err_short}"
        else:
            last = ""
        tasks.append({
            "id": id_, "name": name, "task": task, "schedule": schedule,
            "next_run": next_dt, "last_run": last_dt,
            "repeat": bool(repeat), "enabled": bool(enabled),
            "run_count": int(run_count or 0),
            "last_status": last_status or "",
            "last": last,
            "last_error": last_error or "",
            "last_duration_ms": int(last_duration_ms or 0),
            "last_result": (last_result or "")[:200],
            # thread_id: the routine's permanent chat thread. UI links the
            # routine card to this so users can scroll through past runs.
            "thread_id": thread_id or "",
        })
    return tasks


def remove(task_id: int) -> str:
    _ensure_table()
    row = db.fetchone("SELECT name, thread_id FROM scheduled_tasks WHERE id=?",
                       (task_id,))
    if not row:
        return f"✗ Task #{task_id} not found"
    name, thread_id = row
    if name == HEARTBEAT_TASK_NAME:
        return f"✗ Heartbeat task cannot be removed. Use settings to disable it."
    db.execute("DELETE FROM scheduled_tasks WHERE id=?", (task_id,))
    # Archive the routine's chat thread so the UI drops it from the active
    # list but history is still accessible for "recent runs" digests.
    if thread_id:
        try:
            import threads as _threads
            _threads.archive(thread_id)
        except Exception as e:
            _log.debug(f"thread archive failed for routine {task_id}: {e}")
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
        "SELECT id, name, task, schedule, repeat, thread_id FROM scheduled_tasks "
        "WHERE enabled=1 AND next_run<=?",
        (now,)
    )

    for id_, name, task, schedule, repeat, thread_id in rows:
        # Pre-reschedule to prevent duplicate execution if task takes >30s
        if repeat:
            # Use _parse_schedule to compute the NEXT actual fire time —
            # important for calendar schedules (weekly Mon/Wed/Fri, every
            # N days at HH:MM) where `now + interval` would skip over the
            # correct next weekday.
            next_run, interval = _parse_schedule(schedule)
            if next_run is None:
                # Parser rejected the schedule; fall back to 1h so the
                # row doesn't fire in a tight loop on each scheduler tick.
                next_run = now + 3600
            db.execute(
                "UPDATE scheduled_tasks SET next_run=? WHERE id=?",
                (next_run, id_)
            )
        else:
            # Disable one-time task before execution
            db.execute("UPDATE scheduled_tasks SET enabled=0 WHERE id=?", (id_,))

        # Execute task (time it, catch exceptions so one bad job doesn't
        # freeze the whole scheduler loop).
        _log.info(f"cron firing: #{id_} '{name}' → {task[:80]}")
        t0 = time.time()
        result = ""
        error_msg = None
        try:
            # Routines (user-created repeating jobs) run through agent.run
            # in their permanent thread — every firing appends a new turn
            # there, so the thread grows into a chat log of all runs.
            # System jobs (heartbeat/synthesis) and reminders keep the fast
            # stateless path.
            if _is_routine(task):
                # Legacy rows (created before migration 004) have NULL
                # thread_id — lazy-create one now and stamp back.
                if not thread_id:
                    thread_id = _ensure_routine_thread(id_, name, schedule)
                result = _execute_routine(task, name, id_, thread_id)
            else:
                result = _execute_task(task)
        except Exception as e:
            error_msg = f"{type(e).__name__}: {e}"
            _log.warning(f"cron crashed: #{id_} '{name}' → {error_msg}", exc_info=True)
        duration_ms = int((time.time() - t0) * 1000)
        _log.info(f"cron done: #{id_} '{name}' ({duration_ms}ms) → {result[:200]}")

        # Classify run outcome. Errors thrown by _execute_task, or result
        # text starting with a known error marker, count as failures.
        status = "err" if (error_msg or _looks_like_error(result)) else "ok"
        last_result_preview = (error_msg or result or "")[:500]

        # Notify callbacks
        for fn in _callbacks:
            try:
                fn(name, task, result)
            except Exception:
                _log.warning(f"cron callback error for #{id_}", exc_info=True)

        # Finalize: metrics update for repeating tasks, delete one-offs.
        if repeat:
            db.execute(
                "UPDATE scheduled_tasks "
                "SET last_run=?, run_count=COALESCE(run_count,0)+1, "
                "    last_status=?, last_error=?, last_duration_ms=?, last_result=? "
                "WHERE id=?",
                (now, status, error_msg, duration_ms, last_result_preview, id_),
            )
        else:
            db.execute("DELETE FROM scheduled_tasks WHERE id=?", (id_,))


def _is_routine(task_desc: str) -> bool:
    """True if this is a user-created routine (runs through agent.run in a
    dedicated thread), False for system tasks (heartbeat, synthesis) and
    trivial reminders which keep the fast stateless path."""
    if task_desc == HEARTBEAT_TASK_NAME:
        return False
    if task_desc == SYNTHESIS_TASK_NAME:
        return False
    low = task_desc.lower()
    reminder_markers = ("remind", "напомни", "напоминание", "напомнить",
                        "выпить", "drink", "stretch", "break")
    if any(m in low for m in reminder_markers) and len(task_desc) < 200:
        return False
    return True


def _ensure_routine_thread(cron_id: int, routine_name: str, schedule: str) -> str:
    """Back-fill a thread for a routine that predates migration 004.

    Routines created before v0.17.30 were saved without a thread_id
    column. On first post-migration firing we create one, stamp it
    back, and return it. Empty string on failure → caller falls back
    to agent.run's default active thread (ugly but non-fatal).
    """
    try:
        import threads
        t = threads.create(f"Routine · {routine_name}", meta={
            "kind": "routine",
            "routine_name": routine_name,
            "schedule": schedule,
            "backfilled": True,
        })
        tid = t["id"]
        db.execute("UPDATE scheduled_tasks SET thread_id=? WHERE id=?",
                   (tid, cron_id))
        _log.info(f"back-filled thread_id for routine #{cron_id} '{routine_name}' → {tid}")
        return tid
    except Exception as e:
        _log.warning(f"routine #{cron_id}: thread back-fill failed: {e}")
        return ""


def _execute_routine(task_desc: str, routine_name: str, cron_id: int,
                      thread_id: str) -> str:
    """Run a routine firing through agent.run inside its permanent thread.

    Each firing appends a fresh user → assistant turn to ``thread_id``,
    so the thread grows into a chat log of all past runs. Users see this
    as a normal conversation when they open the routine card.

    Returns the reply text. Exceptions propagate to `_check_and_run`
    which logs and marks the run as failed.
    """
    import agent
    from turn_context import TurnContext

    # Headless ctx — no WS client to stream to; messages persist via
    # agent.run's own db.save_message calls.
    ctx = TurnContext(source="routine")
    result = agent.run(task_desc, thread_id=thread_id, source="routine", ctx=ctx)
    return getattr(result, "reply", "") or ""


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
    # System prompt is deliberately specific — scheduled tasks run with no
    # user follow-up, so the model has to get things right on the first
    # try. The bullet points below short-circuit the most common wasted
    # rounds (shell-find-then-ls-then-dir-then-Get-ChildItem looking for
    # files whose paths are already known, or searching for Telegram
    # send tools that don't exist as standalone functions).
    messages = [
        {"role": "system", "content": (
            "You are a scheduled task worker. Execute the task and return the result in ONE pass.\n"
            "You will NOT be able to ask follow-up questions — make decisions and act.\n"
            "\n"
            "Known paths (use read_file DIRECTLY, don't shell-find first):\n"
            f"  {data_dir}/logs/qwe-qwe.log   — full INFO+ log\n"
            f"  {data_dir}/logs/errors.log    — WARNING+ only (usually what you want for summaries)\n"
            f"  {data_dir}/workspace/         — agent workspace (for writes)\n"
            "\n"
            "Tool selection cheat-sheet:\n"
            "- Read a known file: use read_file(path). Do NOT use shell for this.\n"
            "- Run shell commands ONLY when you need process output (ps, curl, git, etc).\n"
            "- Send a Telegram message to the user: use telegram_notify_owner(text=<msg>).\n"
            "  ONE call, no token or chat_id needed — the bot is already configured. This is\n"
            "  the correct tool for 'send to telegram', 'notify me', 'пришли в телегу', etc.\n"
            "  Do NOT use http_request for this — you'll waste rounds hunting for chat IDs.\n"
            "- Need an extended tool (mcp, schedule, notes, etc)? Call tool_search('<keyword>')\n"
            "  FIRST to unlock it, THEN call the tool. Don't guess tool names.\n"
            "- Save a result for later runs: memory_save({text, tag:'cron-result'}).\n"
            "\n"
            "Final reply rules:\n"
            "- If the task is a reminder ('remind me', 'напомни'): just return the reminder text.\n"
            "- If the task sends/posts/notifies: the final reply MUST confirm delivery\n"
            "  (e.g. 'Sent.', 'Отправлено.', 'message_id=42'). Dry-run validation checks for this.\n"
            "- Keep the final reply under 200 words. Summarise, don't paste full logs back."
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
                # 2048: 1024 was clipping mid-reply on summary tasks where the
                # final turn included paraphrased log lines plus a confirmation.
                max_tokens=2048,
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
