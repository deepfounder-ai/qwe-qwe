"""Spicy Duck — Lovense smart device integration for qwe-qwe.

Connects to Lovense toys via LAN API (local network) or Cloud API.
Requires Lovense Remote app running on phone with toys connected.

Activation: this skill is hidden by default. Enable via:
  self_config(action="set", key="spicy_duck", value="quack")
"""

import json
import time

DESCRIPTION = "Control Lovense smart devices (vibrate, patterns, presets)"

INSTRUCTION = """Use Spicy Duck tools to control Lovense devices:
- lovense_connect: discover toys on local network
- lovense_vibrate: set vibration level (0-20)
- lovense_pattern: send a pattern sequence
- lovense_preset: activate built-in pattern (pulse, wave, fireworks, earthquake)
- lovense_stop: stop all toys
- lovense_status: get battery and connection info

The user must have Lovense Remote app open with toys connected.
Default: LAN mode (same WiFi network). Cloud mode needs developer token.

Be tasteful and respectful. Follow the user's lead.
"""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "lovense_connect",
            "description": "Discover Lovense toys on local network. Returns connected devices.",
            "parameters": {
                "type": "object",
                "properties": {
                    "host": {"type": "string", "description": "LAN host (default: 127-0-0-1.lovense.club)"},
                    "port": {"type": "integer", "description": "HTTPS port (default: 30010)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lovense_vibrate",
            "description": "Set vibration intensity. Level 0 = off, 1-20 = intensity.",
            "parameters": {
                "type": "object",
                "properties": {
                    "level": {"type": "integer", "description": "Vibration level 0-20"},
                    "duration": {"type": "number", "description": "Duration in seconds (0 = until stopped)"},
                    "toy_id": {"type": "string", "description": "Specific toy ID (default: all)"},
                },
                "required": ["level"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lovense_pattern",
            "description": "Send a vibration pattern. Pattern is semicolon-separated levels with interval.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Pattern: levels separated by ; (e.g. '5;10;15;20;15;10;5')"},
                    "interval": {"type": "number", "description": "Interval between levels in seconds (default: 0.5)"},
                    "toy_id": {"type": "string", "description": "Specific toy ID (default: all)"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lovense_preset",
            "description": "Activate a built-in pattern: pulse, wave, fireworks, earthquake.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "enum": ["pulse", "wave", "fireworks", "earthquake"], "description": "Preset pattern name"},
                    "duration": {"type": "number", "description": "Duration in seconds (0 = until stopped)"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lovense_stop",
            "description": "Stop all toys immediately.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lovense_status",
            "description": "Get connected toy info: name, battery, connection status.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

# ── State ──
_host = "127-0-0-1.lovense.club"
_port = 30010
_toys = {}  # id -> {name, status, battery}
_base_url = None


def _get_url():
    global _base_url
    if not _base_url:
        _base_url = f"https://{_host}:{_port}"
    return _base_url


def _api(command: str, params: dict = None) -> dict:
    """Send command to Lovense local API."""
    import requests
    url = f"{_get_url()}/command"
    body = {"command": command}
    if params:
        body.update(params)
    try:
        resp = requests.post(url, json=body, timeout=5, verify=False)
        return resp.json()
    except requests.exceptions.ConnectionError:
        return {"code": -1, "message": "Cannot connect to Lovense Remote. Make sure the app is running on the same network."}
    except Exception as e:
        return {"code": -1, "message": str(e)}


def execute(name: str, args: dict) -> str:
    global _host, _port, _toys, _base_url

    if name == "lovense_connect":
        _host = args.get("host", "127-0-0-1.lovense.club")
        _port = args.get("port", 30010)
        _base_url = None  # reset

        result = _api("GetToys")
        if result.get("code") == 200:
            toys_data = result.get("data", {}).get("toys", {})
            if isinstance(toys_data, str):
                try:
                    toys_data = json.loads(toys_data)
                except Exception:
                    toys_data = {}
            _toys = {}
            lines = ["Connected toys:"]
            for tid, info in toys_data.items():
                _toys[tid] = {
                    "name": info.get("name", "unknown"),
                    "nickName": info.get("nickName", ""),
                    "status": info.get("status", 0),
                    "battery": info.get("battery", -1),
                }
                status = "online" if info.get("status") == 1 else "offline"
                lines.append(f"  {info.get('nickName') or info.get('name')} (ID: {tid}) — {status}, battery: {info.get('battery', '?')}%")
            if not _toys:
                return "Connected to Lovense Remote but no toys found. Make sure a toy is paired in the app."
            return "\n".join(lines)
        return f"Connection failed: {result.get('message', 'unknown error')}"

    elif name == "lovense_vibrate":
        level = max(0, min(20, args.get("level", 0)))
        duration = args.get("duration", 0)
        params = {"action": "Vibrate", "intensity": str(level)}
        if duration > 0:
            params["timeSec"] = str(int(duration))
        tid = args.get("toy_id")
        if tid:
            params["toy"] = tid
        result = _api("Function", params)
        if result.get("code") == 200:
            return f"Vibration set to {level}/20" + (f" for {duration}s" if duration else "")
        return f"Error: {result.get('message', 'unknown')}"

    elif name == "lovense_pattern":
        pattern = args.get("pattern", "5;10;15;10;5")
        interval = args.get("interval", 0.5)
        # Convert pattern to API format: "V:1;F:v;S:100#" where V=vibration level, F=feature, S=interval ms
        levels = pattern.split(";")
        rule = ";".join(f"V:{l.strip()};F:v;S:{int(interval*1000)}#" for l in levels if l.strip())
        params = {"action": "Pattern", "rule": rule, "strength": pattern}
        tid = args.get("toy_id")
        if tid:
            params["toy"] = tid
        result = _api("Pattern", params)
        if result.get("code") == 200:
            return f"Pattern playing: {pattern} (interval: {interval}s)"
        return f"Error: {result.get('message', 'unknown')}"

    elif name == "lovense_preset":
        preset = args.get("name", "pulse")
        duration = args.get("duration", 0)
        params = {"action": "Preset", "name": preset}
        if duration > 0:
            params["timeSec"] = str(int(duration))
        result = _api("Preset", params)
        if result.get("code") == 200:
            return f"Preset '{preset}' activated" + (f" for {duration}s" if duration else "")
        return f"Error: {result.get('message', 'unknown')}"

    elif name == "lovense_stop":
        result = _api("Function", {"action": "Stop"})
        if result.get("code") == 200:
            return "All toys stopped"
        return f"Error: {result.get('message', 'unknown')}"

    elif name == "lovense_status":
        if not _toys:
            # Try to refresh
            connect_result = execute("lovense_connect", {})
            if not _toys:
                return connect_result
        lines = ["Toy status:"]
        for tid, info in _toys.items():
            lines.append(f"  {info['nickName'] or info['name']} — battery: {info['battery']}%, status: {'online' if info['status'] == 1 else 'offline'}")
        return "\n".join(lines)

    return f"Unknown spicy_duck tool: {name}"
