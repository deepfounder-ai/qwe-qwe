"""Chrome browser skill."""

DESCRIPTION = "Skill to fetch a web page using portable Chrome headless dump-dom."

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "fetch_page",
            "description": "Fetch a web page using the portable Chrome binary in headless mode with --dump-dom.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The URL of the page to fetch"},
                },
                "required": ["url"],
            },
        },
    },
]

import subprocess

def execute(name: str, args: dict) -> str:
    if name == "fetch_page":
        try:
            url = args["url"]
            cmd = [
                "/home/kirco/chrome/bin/google-chrome-stable",
                "--headless",
                "--dump-dom",
                url,
            ]
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30
            )
            if result.returncode != 0:
                return f"Error: Chrome exited with code {result.returncode}: {result.stderr}"
            return result.stdout
        except Exception as e:
            return f"Exception: {str(e)}"
    return f"Unknown tool: {name}"