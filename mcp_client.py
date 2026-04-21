"""MCP (Model Context Protocol) client — connect to external MCP servers.

Supports stdio (subprocess) and HTTP transports.
Discovers tools via JSON-RPC 2.0 and exposes them to the agent as OpenAI-format tools.

No dependency on the `mcp` SDK — uses subprocess + JSON-RPC directly for simplicity.
"""

import json
import os
import subprocess
import threading
import time
import requests as _req

import db
import logger

_log = logger.get("mcp")

# ── Active server connections ──

_servers: dict[str, "MCPServerConnection"] = {}  # name → connection
_servers_lock = threading.Lock()


# ── Config persistence (SQLite KV) ──

def load_config() -> dict:
    """Load MCP server configs from DB."""
    raw = db.kv_get("mcp:servers")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def save_config(config: dict):
    """Save MCP server configs to DB."""
    db.kv_set("mcp:servers", json.dumps(config, ensure_ascii=False))


# ── Built-in server presets — one-click install of common MCP servers ──
PRESETS: dict[str, dict] = {
    "filesystem": {
        "transport": "stdio", "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "{path}"],
        "description": "Read/write files in a given directory. Placeholder {path} → your chosen folder.",
        "placeholders": {"path": "~/projects"},
    },
    "github": {
        "transport": "stdio", "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-github"],
        "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "{token}"},
        "description": "GitHub API — read repos, issues, PRs, search code.",
        "placeholders": {"token": "ghp_... (Personal Access Token)"},
    },
    "brave-search": {
        "transport": "stdio", "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-brave-search"],
        "env": {"BRAVE_API_KEY": "{api_key}"},
        "description": "Brave web search (free tier available at brave.com/search/api).",
        "placeholders": {"api_key": "BSA... (Brave Search API key)"},
    },
    "memory": {
        "transport": "stdio", "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-memory"],
        "description": "Persistent key-value scratchpad — the agent can remember things across turns.",
    },
    "puppeteer": {
        "transport": "stdio", "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-puppeteer"],
        "description": "Headless browser automation via Puppeteer.",
    },
    "sqlite": {
        "transport": "stdio", "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-sqlite", "--db", "{path}"],
        "description": "Query a local SQLite database.",
        "placeholders": {"path": "~/my.db"},
    },
    "fetch": {
        "transport": "stdio", "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-fetch"],
        "description": "Fetch and convert web pages to markdown.",
    },
}


def get_presets() -> dict:
    """Return the built-in preset catalogue for the UI picker."""
    return PRESETS


def _validate_config(config: dict) -> str | None:
    """Return an error message if the config is invalid, else None."""
    transport = config.get("transport", "stdio")
    if transport == "stdio":
        if not config.get("command", "").strip():
            return "stdio transport requires a 'command'"
    elif transport == "http":
        url = (config.get("url") or "").strip()
        if not url:
            return "http transport requires a 'url'"
        if not url.startswith(("http://", "https://")):
            return "url must start with http:// or https://"
    else:
        return f"unknown transport: {transport!r}"
    return None


def add_server(name: str, **kwargs) -> str:
    """Add or update an MCP server config."""
    config = load_config()
    new_config = {
        "command": kwargs.get("command", ""),
        "args": kwargs.get("args", []),
        "env": kwargs.get("env", {}),
        "url": kwargs.get("url", ""),
        "transport": kwargs.get("transport", "stdio"),
        "enabled": kwargs.get("enabled", True),
    }
    err = _validate_config(new_config)
    if err:
        return f"Invalid config for '{name}': {err}"
    config[name] = new_config
    save_config(config)
    return f"MCP server '{name}' configured"


def remove_server(name: str) -> str:
    """Remove an MCP server config and stop it."""
    stop_server(name)
    config = load_config()
    if name in config:
        del config[name]
        save_config(config)
        return f"MCP server '{name}' removed"
    return f"MCP server '{name}' not found"


# ── Server connection classes ──

class MCPServerConnection:
    """Base class for MCP server connections."""

    def __init__(self, name: str, config: dict):
        self.name = name
        self.config = config
        self._id = 0
        self._lock = threading.Lock()
        self.tools: list[dict] = []  # cached MCP tool definitions
        self.connected = False
        self.error: str | None = None
        self.error_streak = 0          # consecutive RPC failures → circuit-breaker
        self.last_used: float = 0.0    # timestamp of last successful RPC
        self.stderr_tail: list[str] = []  # last N stderr lines (stdio only)

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def _rpc(self, method: str, params: dict | None = None) -> dict:
        raise NotImplementedError

    def initialize(self) -> bool:
        """Send initialize handshake."""
        try:
            import config as _config
            resp = self._rpc("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "qwe-qwe", "version": getattr(_config, "VERSION", "0.17.1")},
            })
            if resp.get("result"):
                # Send initialized notification
                self._notify("notifications/initialized")
                self.connected = True
                self.error = None
                self.error_streak = 0
                _log.info(f"MCP '{self.name}' initialized")
                return True
            self.error = resp.get("error", {}).get("message", "Init failed")
            return False
        except Exception as e:
            self.error = str(e)
            _log.error(f"MCP '{self.name}' init failed: {e}")
            return False

    def _notify(self, method: str, params: dict | None = None):
        """Send a notification (no response expected)."""
        pass  # override in subclasses

    def discover_tools(self) -> list[dict]:
        """Fetch tools from server and cache them."""
        try:
            resp = self._rpc("tools/list")
            raw_tools = resp.get("result", {}).get("tools", [])
            self.tools = raw_tools
            _log.info(f"MCP '{self.name}': discovered {len(raw_tools)} tools")
            return raw_tools
        except Exception as e:
            _log.error(f"MCP '{self.name}' tools/list failed: {e}")
            return []

    _CIRCUIT_BREAKER_THRESHOLD = 3  # consecutive failures → mark disconnected

    def call_tool(self, tool_name: str, arguments: dict) -> str:
        """Execute a tool and return text result."""
        if not self.connected:
            return f"Error: MCP '{self.name}' is disconnected (restart via MCP settings)"
        if self.error_streak >= self._CIRCUIT_BREAKER_THRESHOLD:
            return (
                f"Error: MCP '{self.name}' disabled after {self.error_streak} consecutive failures. "
                f"Last error: {self.error or 'unknown'}. Restart the server from the MCP settings."
            )
        try:
            resp = self._rpc("tools/call", {
                "name": tool_name,
                "arguments": arguments,
            })
            result = resp.get("result", {})
            if result.get("isError"):
                content = result.get("content", [])
                err_text = "\n".join(c.get("text", "") for c in content if c.get("type") == "text")
                self.error_streak += 1
                self.error = err_text or "tool execution failed"
                return f"Error: {err_text}" if err_text else "Error: tool execution failed"
            # Success — reset breaker
            self.error_streak = 0
            self.error = None
            # Extract text content
            content = result.get("content", [])
            parts = []
            for c in content:
                if c.get("type") == "text":
                    parts.append(c["text"])
                elif c.get("type") == "image":
                    parts.append(f"[image: {c.get('mimeType', 'image')}]")
                elif c.get("type") == "resource":
                    parts.append(f"[resource: {c.get('uri', '')}]")
            return "\n".join(parts) if parts else str(result)
        except Exception as e:
            self.error_streak += 1
            self.error = str(e)
            # Mark disconnected when breaker trips so subsequent calls fail fast
            if self.error_streak >= self._CIRCUIT_BREAKER_THRESHOLD:
                self.connected = False
                _log.warning(
                    f"MCP '{self.name}' circuit-breaker tripped after "
                    f"{self.error_streak} failures — marking disconnected"
                )
            return f"Error calling MCP tool '{tool_name}': {e}"

    def stop(self):
        """Disconnect from server."""
        self.connected = False

    def status(self) -> dict:
        return {
            "name": self.name,
            "connected": self.connected,
            "transport": self.config.get("transport", "stdio"),
            "tools_count": len(self.tools),
            "error": self.error,
        }


class StdioMCPServer(MCPServerConnection):
    """MCP server connected via subprocess stdin/stdout."""

    _RPC_TIMEOUT = 30.0      # max seconds to wait for a response line
    _STDERR_TAIL_MAX = 50    # keep last N stderr lines for debugging

    def __init__(self, name: str, config: dict):
        super().__init__(name, config)
        self.proc: subprocess.Popen | None = None
        self._stderr_thread: threading.Thread | None = None
        self._stopping = False

    def _drain_stderr(self):
        """Background reader: drain the subprocess stderr so its buffer
        doesn't fill up and block the server. Keeps a tail for diagnostics."""
        try:
            while self.proc and self.proc.stderr and not self._stopping:
                line = self.proc.stderr.readline()
                if not line:
                    break
                try:
                    text = line.decode("utf-8", errors="replace").rstrip()
                except Exception:
                    continue
                if text:
                    self.stderr_tail.append(text)
                    if len(self.stderr_tail) > self._STDERR_TAIL_MAX:
                        self.stderr_tail = self.stderr_tail[-self._STDERR_TAIL_MAX:]
                    # MCP servers often log verbose info to stderr — only surface warnings+
                    lower = text.lower()
                    if any(w in lower for w in ("error", "fatal", "exception", "traceback")):
                        _log.warning(f"MCP '{self.name}' stderr: {text[:200]}")
        except Exception as e:
            _log.debug(f"MCP '{self.name}' stderr reader exited: {e}")

    def start(self) -> bool:
        """Spawn the server process."""
        command = self.config.get("command", "")
        args = self.config.get("args", [])
        env_overrides = self.config.get("env", {})
        if not command:
            self.error = "No command specified"
            return False
        env = {**os.environ, **(env_overrides or {})}
        try:
            # Don't inherit our stdin; ensure line-buffered where possible
            self.proc = subprocess.Popen(
                [command] + args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                bufsize=0,  # unbuffered — critical for line-based JSON-RPC
            )
            self._stopping = False
            self._stderr_thread = threading.Thread(
                target=self._drain_stderr, name=f"mcp-{self.name}-stderr", daemon=True
            )
            self._stderr_thread.start()
            _log.info(f"MCP '{self.name}' process started (PID {self.proc.pid})")
            return True
        except FileNotFoundError:
            self.error = f"Command not found on PATH: {command!r}"
            _log.error(f"MCP '{self.name}' start failed: {self.error}")
            return False
        except Exception as e:
            self.error = f"Failed to start: {e}"
            _log.error(f"MCP '{self.name}' start failed: {e}")
            return False

    def _readline_with_timeout(self, timeout: float) -> bytes:
        """Read a line from stdout with a timeout — avoids hanging forever
        when the server dies mid-request. Uses a helper thread so we work
        uniformly on Windows (no select() on pipes)."""
        result: list[bytes] = []
        err: list[Exception] = []
        done = threading.Event()

        def _reader():
            try:
                line = self.proc.stdout.readline()
                result.append(line)
            except Exception as e:
                err.append(e)
            finally:
                done.set()

        threading.Thread(target=_reader, daemon=True).start()
        if not done.wait(timeout):
            raise TimeoutError(f"no response within {timeout:.0f}s")
        if err:
            raise err[0]
        return result[0] if result else b""

    def _rpc(self, method: str, params: dict | None = None) -> dict:
        if not self.proc or self.proc.poll() is not None:
            raise ConnectionError(f"MCP '{self.name}' process not running")
        msg_id = self._next_id()
        msg: dict = {"jsonrpc": "2.0", "id": msg_id, "method": method}
        if params is not None:
            msg["params"] = params
        raw = json.dumps(msg) + "\n"
        with self._lock:
            try:
                self.proc.stdin.write(raw.encode("utf-8"))
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError) as e:
                raise ConnectionError(f"MCP '{self.name}' stdin broken: {e}")
            try:
                line = self._readline_with_timeout(self._RPC_TIMEOUT)
            except TimeoutError:
                raise
            if not line:
                # Stdout closed → process exited. Surface stderr tail to help debug.
                tail = "\n".join(self.stderr_tail[-10:]) or "(no stderr)"
                raise ConnectionError(
                    f"MCP '{self.name}' returned empty response (process likely exited). "
                    f"Recent stderr:\n{tail}"
                )
        self.last_used = time.time()
        return json.loads(line.decode("utf-8"))

    def _notify(self, method: str, params: dict | None = None):
        if not self.proc or self.proc.poll() is not None:
            return
        msg: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        raw = json.dumps(msg) + "\n"
        with self._lock:
            self.proc.stdin.write(raw.encode("utf-8"))
            self.proc.stdin.flush()

    def stop(self):
        super().stop()
        self._stopping = True
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
            _log.info(f"MCP '{self.name}' process stopped")
        self.proc = None
        # Let stderr reader exit naturally
        if self._stderr_thread and self._stderr_thread.is_alive():
            self._stderr_thread.join(timeout=1)
        self._stderr_thread = None


class HttpMCPServer(MCPServerConnection):
    """MCP server connected via HTTP (Streamable HTTP transport)."""

    def __init__(self, name: str, config: dict):
        super().__init__(name, config)
        self.url = config.get("url", "").rstrip("/")
        # Build auth headers from env config (e.g. {"Authorization": "Bearer ..."})
        self.headers = {"Content-Type": "application/json"}
        env = config.get("env", {})
        if env.get("AUTHORIZATION"):
            self.headers["Authorization"] = env["AUTHORIZATION"]
        elif env.get("API_KEY"):
            self.headers["Authorization"] = f"Bearer {env['API_KEY']}"
        elif env.get("SUPABASE_SERVICE_ROLE_KEY"):
            self.headers["Authorization"] = f"Bearer {env['SUPABASE_SERVICE_ROLE_KEY']}"

    def start(self) -> bool:
        if not self.url:
            self.error = "No URL specified"
            return False
        return True

    def _rpc(self, method: str, params: dict | None = None) -> dict:
        msg_id = self._next_id()
        msg: dict = {"jsonrpc": "2.0", "id": msg_id, "method": method}
        if params is not None:
            msg["params"] = params
        resp = _req.post(self.url, json=msg, headers=self.headers, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def _notify(self, method: str, params: dict | None = None):
        msg: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        try:
            _req.post(self.url, json=msg, timeout=5)
        except Exception:
            pass

    def stop(self):
        super().stop()


# ── Server lifecycle ──

def start_server(name: str) -> str:
    """Start (or restart) an MCP server by name."""
    config = load_config()
    if name not in config:
        return f"MCP server '{name}' not configured"
    srv_config = config[name]
    if not srv_config.get("enabled", True):
        return f"MCP server '{name}' is disabled"

    # Stop if already running
    stop_server(name)

    transport = srv_config.get("transport", "stdio")
    if transport == "stdio":
        conn = StdioMCPServer(name, srv_config)
        if not conn.start():
            return f"Failed to start '{name}': {conn.error}"
    elif transport == "http":
        conn = HttpMCPServer(name, srv_config)
        if not conn.start():
            return f"Failed to connect '{name}': {conn.error}"
    else:
        return f"Unknown transport: {transport}"

    # Initialize handshake
    if not conn.initialize():
        conn.stop()
        return f"MCP '{name}' handshake failed: {conn.error}"

    # Discover tools
    conn.discover_tools()

    with _servers_lock:
        _servers[name] = conn

    return f"MCP '{name}' connected ({len(conn.tools)} tools)"


def stop_server(name: str) -> str:
    """Stop an MCP server."""
    with _servers_lock:
        conn = _servers.pop(name, None)
    if conn:
        conn.stop()
        return f"MCP '{name}' stopped"
    return f"MCP '{name}' not running"


def restart_server(name: str) -> str:
    """Stop then start an MCP server."""
    stop_server(name)
    return start_server(name)


def start_all():
    """Start all enabled MCP servers. Called on app startup."""
    config = load_config()
    for name, srv_config in config.items():
        if srv_config.get("enabled", True):
            try:
                result = start_server(name)
                _log.info(result)
            except Exception as e:
                _log.error(f"MCP '{name}' startup failed: {e}")


def stop_all():
    """Stop all running MCP servers. Called on app shutdown."""
    with _servers_lock:
        names = list(_servers.keys())
    for name in names:
        stop_server(name)


# ── Tool interface (used by tools.py) ──

def get_all_mcp_tools() -> list[dict]:
    """Get all tools from all connected MCP servers in OpenAI function-calling format."""
    all_tools = []
    with _servers_lock:
        servers = list(_servers.values())
    for conn in servers:
        if not conn.connected:
            continue
        for tool in conn.tools:
            # Convert MCP tool schema → OpenAI function format
            openai_tool = {
                "type": "function",
                "function": {
                    "name": f"mcp__{conn.name}__{tool['name']}",
                    "description": f"[MCP:{conn.name}] {tool.get('description', '')}",
                    "parameters": tool.get("inputSchema", {"type": "object", "properties": {}}),
                },
            }
            all_tools.append(openai_tool)
    return all_tools


def _fix_paths_in_args(args: dict) -> dict:
    """Convert Git Bash paths (/c/Users/...) to Windows (C:/Users/...) in tool args."""
    import sys
    if sys.platform != "win32":
        return args
    fixed = {}
    for k, v in args.items():
        if isinstance(v, str) and len(v) >= 3 and v[0] == "/" and v[2] == "/":
            drive = v[1].upper()
            if drive.isalpha():
                v = f"{drive}:{v[2:]}"
        fixed[k] = v
    return fixed


def execute_mcp_tool(full_name: str, args: dict) -> str:
    """Execute an MCP tool. full_name format: mcp__servername__toolname"""
    parts = full_name.split("__", 2)
    if len(parts) != 3 or parts[0] != "mcp":
        return f"Invalid MCP tool name: {full_name}"
    server_name = parts[1]
    tool_name = parts[2]

    with _servers_lock:
        conn = _servers.get(server_name)
    if not conn:
        return f"MCP server '{server_name}' not connected"
    if not conn.connected:
        return f"MCP server '{server_name}' disconnected"

    # Fix Git Bash paths for Windows MCP servers
    args = _fix_paths_in_args(args)

    return conn.call_tool(tool_name, args)


# ── Status ──

def list_servers() -> list[dict]:
    """List all configured servers with connection status + diagnostics."""
    config = load_config()
    result = []
    with _servers_lock:
        active = dict(_servers)
    for name, srv_config in config.items():
        conn = active.get(name)
        entry = {
            "name": name,
            "transport": srv_config.get("transport", "stdio"),
            "command": srv_config.get("command", ""),
            "args": srv_config.get("args", []),
            "url": srv_config.get("url", ""),
            "env_keys": list((srv_config.get("env") or {}).keys()),  # names only — no secret values
            "enabled": srv_config.get("enabled", True),
            "connected": bool(conn and conn.connected),
            "tools_count": len(conn.tools) if conn else 0,
            "error": conn.error if conn else None,
            "error_streak": conn.error_streak if conn else 0,
            "stderr_tail": (getattr(conn, "stderr_tail", None) or [])[-10:] if conn else [],
            "last_used": conn.last_used if conn else 0.0,
            "tools": [],
        }
        if conn and conn.tools:
            entry["tools"] = [
                {"name": t["name"], "description": t.get("description", "")[:160]}
                for t in conn.tools
            ]
        result.append(entry)
    return result


def health_check() -> dict:
    """Fast health probe — returns counts without hitting the wire.

    Useful for UI polling + startup summary.
    """
    with _servers_lock:
        servers = list(_servers.values())
    running = sum(1 for s in servers if s.connected)
    tripped = sum(1 for s in servers if s.error_streak >= 3)
    tools_total = sum(len(s.tools) for s in servers if s.connected)
    return {
        "configured": len(load_config()),
        "running": running,
        "tripped": tripped,
        "tools_total": tools_total,
    }
