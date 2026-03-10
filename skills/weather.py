"""Weather skill — get current weather via wttr.in (no API key needed)."""

DESCRIPTION = "Current weather for any city"

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get current weather for a city. Returns temperature, conditions, wind.",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "City name (e.g. 'Buenos Aires', 'Moscow')"},
                },
                "required": ["city"],
            },
        },
    },
]


def execute(name: str, args: dict) -> str:
    if name == "get_weather":
        import urllib.request
        city = args["city"].replace(" ", "+")
        url = f"https://wttr.in/{city}?format=%l:+%C+%t+%w+%h"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "qwe-qwe/0.1"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.read().decode("utf-8").strip()
        except Exception as e:
            return f"Weather error: {e}"
    return f"Unknown tool: {name}"
