"""Custom cowsay skill that invokes the cowsay CLI with custom text."""

DESCRIPTION = "Calls cowsay with custom text input"

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "custom_cowsay",
            "description": "Execute cowsay command with provided text",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Text to display in cowsay"},
                },
                "required": ["text"],
            },
        },
    },
]

def execute(name: str, args: dict) -> str:
    if name == "custom_cowsay":
        try:
            text = args.get("text", "")
            cowsay_path = "/home/kirco/qwe-qwe/.venv/bin/cowsay"
            
            import subprocess
            result = subprocess.run(
                [cowsay_path, text],
                capture_output=True,
                text=True,
                check=False
            )
            
            if result.returncode == 0:
                return result.stdout.strip()
            else:
                return f"Error running cowsay: {result.stderr.strip()}"
        except Exception as e:
            return f"Exception occurred: {str(e)}"
    return f"Unknown tool: {name}"