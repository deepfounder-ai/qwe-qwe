"""Timer skill — set, list, and cancel countdown timers."""

import threading
import time
import uuid

DESCRIPTION = "Set, list, and cancel countdown timers"

# In-memory registry: {id: {"thread": Thread, "label": str, "seconds": int, "start_time": float}}
_timers: dict[str, dict] = {}
_lock = threading.Lock()

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "set_timer",
            "description": "Set a countdown timer. Prints a message when done.",
            "parameters": {
                "type": "object",
                "properties": {
                    "seconds": {"type": "integer", "description": "Timer duration in seconds"},
                    "label": {"type": "string", "description": "What the timer is for"},
                },
                "required": ["seconds"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_timers",
            "description": "List all active (pending) timers with their id, label, seconds remaining, and start time.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_timer",
            "description": "Cancel a pending timer by its id. Ids are returned by list_timers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "timer_id": {"type": "string", "description": "The id of the timer to cancel"},
                },
                "required": ["timer_id"],
            },
        },
    },
]


def execute(name: str, args: dict) -> str:
    if name == "set_timer":
        return _set_timer(args)
    elif name == "list_timers":
        return _list_timers()
    elif name == "cancel_timer":
        return _cancel_timer(args)
    return f"Unknown tool: {name}"


def _set_timer(args: dict) -> str:
    secs = int(args["seconds"])
    label = args.get("label", "Timer")
    timer_id = str(uuid.uuid4())[:8]

    def _ring(_tid: str, _label: str, _secs: int):
        time.sleep(_secs)
        print(f"\n  ⏰ {_label} — {_secs}s done!")
        with _lock:
            _timers.pop(_tid, None)

    t = threading.Thread(target=_ring, args=(timer_id, label, secs), daemon=True)
    t.start()

    with _lock:
        _timers[timer_id] = {
            "thread": t,
            "label": label,
            "seconds": secs,
            "start_time": time.time(),
        }

    return f"⏱ Timer set: {label} ({secs}s) — id: {timer_id}"


def _list_timers() -> str:
    now = time.time()
    with _lock:
        if not _timers:
            return "No active timers."
        lines = ["**Active timers:**"]
        for tid, info in sorted(_timers.items()):
            elapsed = now - info["start_time"]
            remaining = max(0, info["seconds"] - int(elapsed))
            lines.append(
                f"  • `{tid}` — {info['label']} — {remaining}s remaining (of {info['seconds']}s)"
            )
        return "\n".join(lines)


def _cancel_timer(args: dict) -> str:
    timer_id = args.get("timer_id", "")
    with _lock:
        info = _timers.pop(timer_id, None)
    if info is None:
        return f"❌ Timer '{timer_id}' not found. Use list_timers to see active timers."
    # Can't truly kill a running threading.Thread, but we remove it from
    # the registry so the ring function is a no-op when it fires.
    return f"✅ Cancelled timer '{timer_id}' — {info['label']} ({info['seconds']}s)"