"""MCP Manager skill — manage MCP servers via chat commands.

Allows the agent to add, remove, list, and control MCP servers
through natural language without needing the Settings UI.
"""

DESCRIPTION = "Manage MCP tool servers (add, remove, list, restart)"

INSTRUCTION = """MCP Manager: You can manage external tool servers via Model Context Protocol.
- mcp_list_servers: see all configured MCP servers and their tools
- mcp_add_server: add a new MCP server (stdio or http transport)
- mcp_remove_server: remove an MCP server by name
- mcp_restart_server: restart a server connection
- mcp_toggle_server: enable or disable a server

IMPORTANT RULES:
1. On Windows, use npx.cmd (not npx) for stdio servers.
2. For HTTP MCP servers that require auth (like Supabase), pass the API key in env:
   env={"SUPABASE_SERVICE_ROLE_KEY": "eyJ..."} or env={"API_KEY": "..."} or env={"AUTHORIZATION": "Bearer eyJ..."}
   The system auto-detects these env vars and adds them as Authorization header.
3. Supabase MCP uses HTTP transport with url like: https://mcp.supabase.com/mcp?project_ref=XXX
   ALWAYS pass the service_role_key in env when adding Supabase MCP.
4. If user pastes a JSON config like {"mcpServers": {"name": {"url": "...", ...}}}, parse it and use mcp_add_server with the correct params.
5. If user gives you an API key or token separately, use it in the env parameter of mcp_add_server."""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "mcp_list_servers",
            "description": "List all configured MCP servers with status, tools, and connection info.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mcp_add_server",
            "description": "Add and connect a new MCP server. For stdio: provide command+args. For http: provide url.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Unique server name (e.g. supabase, github, filesystem)"},
                    "transport": {"type": "string", "enum": ["stdio", "http"], "description": "Transport type"},
                    "command": {"type": "string", "description": "Command to run (stdio only, e.g. npx.cmd, node, python)"},
                    "args": {"type": "array", "items": {"type": "string"}, "description": "Command arguments (stdio only)"},
                    "url": {"type": "string", "description": "Server URL (http only)"},
                    "env": {"type": "object", "description": "Environment variables (e.g. {\"API_KEY\": \"...\"})", "additionalProperties": {"type": "string"}},
                },
                "required": ["name", "transport"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mcp_remove_server",
            "description": "Remove an MCP server and disconnect it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Server name to remove"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mcp_restart_server",
            "description": "Restart an MCP server connection (re-initialize and rediscover tools).",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Server name to restart"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mcp_toggle_server",
            "description": "Enable or disable an MCP server.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Server name"},
                    "enabled": {"type": "boolean", "description": "True to enable, false to disable"},
                },
                "required": ["name", "enabled"],
            },
        },
    },
]


def execute(name: str, args: dict) -> str:
    import mcp_client

    if name == "mcp_list_servers":
        servers = mcp_client.list_servers()
        if not servers:
            return "No MCP servers configured. Use mcp_add_server to add one."
        lines = []
        for s in servers:
            status = "🟢 connected" if s["connected"] else ("🔴 error" if s["enabled"] else "⚫ disabled")
            info = s["command"] or s["url"]
            lines.append(f"**{s['name']}** ({s['transport']}) — {status}")
            lines.append(f"  {info}")
            if s["tools_count"]:
                tool_names = ", ".join(t["name"] for t in s["tools"])
                lines.append(f"  Tools ({s['tools_count']}): {tool_names}")
            if s.get("error"):
                lines.append(f"  Error: {s['error']}")
        return "\n".join(lines)

    elif name == "mcp_add_server":
        srv_name = args["name"]
        transport = args.get("transport", "stdio")
        result = mcp_client.add_server(
            srv_name,
            command=args.get("command", ""),
            args=args.get("args", []),
            url=args.get("url", ""),
            env=args.get("env", {}),
            transport=transport,
            enabled=True,
        )
        # Auto-start
        start_result = mcp_client.start_server(srv_name)
        return f"{result}\n{start_result}"

    elif name == "mcp_remove_server":
        return mcp_client.remove_server(args["name"])

    elif name == "mcp_restart_server":
        return mcp_client.start_server(args["name"])

    elif name == "mcp_toggle_server":
        srv_name = args["name"]
        enabled = args.get("enabled", True)
        config = mcp_client.load_config()
        if srv_name not in config:
            return f"MCP server '{srv_name}' not found"
        config[srv_name]["enabled"] = enabled
        mcp_client.save_config(config)
        if enabled:
            return mcp_client.start_server(srv_name)
        else:
            return mcp_client.stop_server(srv_name)

    return f"Unknown MCP tool: {name}"
