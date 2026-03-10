"""Timer skill — set simple countdown timers."""

import threading, time

DESCRIPTION = "Set countdown timers with notifications"

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
]


def execute(name: str, args: dict) -> str:
    if name == "set_timer":
        secs = args["seconds"]
        label = args.get("label", "Timer")

        def _ring():
            time.sleep(secs)
            print(f"\n  ⏰ {label} — {secs}s done!")

        t = threading.Thread(target=_ring, daemon=True)
        t.start()
        return f"⏱ Timer set: {label} ({secs}s)"
    return f"Unknown tool: {name}"
