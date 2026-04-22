"""Tool definitions and execution — optimized for small models."""

import json, subprocess, os, re, shutil, sys, threading, time, unicodedata
from pathlib import Path
import config
import memory
import logger

# Thread-local abort event for the currently-running tool call.
# The agent loop sets `_current_abort_event` before each tool_executor call;
# blocking tools (shell, http_request) poll it and abort early if set.
# Using threading.local so concurrent turns (web + telegram) don't share state.
_tl = threading.local()


def _set_abort_event(evt: threading.Event | None) -> None:
    """Register an abort event for tool calls made from the current thread.

    Called by agent_loop.run_loop() before dispatching each tool call so the
    blocking tools (shell, http_request) can observe aborts without having to
    change the tool_executor signature.
    """
    _tl.abort_event = evt


def _get_abort_event() -> threading.Event | None:
    return getattr(_tl, "abort_event", None)

_log = logger.get("tools")

# Agent workspace — all relative paths resolve here
WORKSPACE = config.WORKSPACE_DIR

# ── Shell detection ──
# Priority: Git Bash > MSYS2 > cmd.exe (never WSL — causes stack overflow)
_SHELL_EXE: str | None = None

def _detect_shell() -> str | None:
    """Find the best shell on this platform. Called once at import."""
    if sys.platform != "win32":
        return None  # Linux/Mac: shell=True uses /bin/sh, good enough

    # Search order for Windows bash
    candidates = [
        Path("C:/Program Files/Git/usr/bin/bash.exe"),
        Path("C:/Program Files (x86)/Git/usr/bin/bash.exe"),
        Path("C:/msys64/usr/bin/bash.exe"),
    ]
    for p in candidates:
        if p.exists():
            return str(p)

    # PATH search — but skip WSL bash (system32\bash.exe)
    found = shutil.which("bash") or shutil.which("bash.exe")
    if found and "system32" not in found.lower():
        return found

    return None  # fallback to cmd.exe via shell=True

_SHELL_EXE = _detect_shell()
if _SHELL_EXE:
    _log.info(f"shell: using bash at {_SHELL_EXE}")
else:
    _log.info(f"shell: {'native /bin/sh' if sys.platform != 'win32' else 'cmd.exe (no bash found)'}")

# Directories the agent is allowed to write to (whitelist — safer than blacklist)
_WRITE_WHITELIST: list[str] | None = None


def _get_write_whitelist() -> list[str]:
    """Lazily compute write-allowed directories."""
    global _WRITE_WHITELIST
    if _WRITE_WHITELIST is None:
        _WRITE_WHITELIST = [
            str(config.WORKSPACE_DIR.resolve()),   # ~/.qwe-qwe/workspace/
            str(config.DATA_DIR.resolve()),         # ~/.qwe-qwe/
            str(Path.cwd().resolve()),              # project working directory
        ]
    return _WRITE_WHITELIST


def _get_path_arg(args: dict) -> str | None:
    """Extract path from tool args — models use various field names."""
    return args.get("path") or args.get("file_path") or args.get("filepath") or args.get("file")


def _resolve_path(raw: str, for_write: bool = False) -> Path:
    """Resolve a file path for agent operations.

    - Git Bash paths (/c/Users/...) -> C:/Users/... on Windows
    - Relative paths -> workspace (~/.qwe-qwe/workspace/)
    - ~ expands to home
    - For writes: only allow workspace, data dir, and cwd (whitelist)
    """
    # Convert Git Bash / MSYS2 paths to Windows: /c/Users/... → C:/Users/...
    if sys.platform == "win32" and len(raw) >= 3 and raw[0] == "/" and raw[2] == "/":
        drive = raw[1].upper()
        if drive.isalpha():
            raw = f"{drive}:{raw[2:]}"
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = WORKSPACE / p
    p = p.resolve()
    if for_write:
        s = str(p)
        allowed = any(s.startswith(w) for w in _get_write_whitelist())
        if not allowed:
            raise PermissionError(
                f"Cannot write outside allowed directories. Path: {p}\n"
                f"Allowed: workspace, data dir (~/.qwe-qwe/), project dir"
            )
    return p


# ── Shell safety ──
#
# IMPORTANT: `_check_shell_safety` is a *best-effort speed bump*, NOT a trust
# boundary. The qwe-qwe agent runs the shell with the full privileges of the
# user who launched it, so a determined attacker (or a sufficiently creative
# language model) can always find a way around pattern-based filtering — shell
# is a full programming language with indirection through `eval`, `$(...)`,
# process substitution, `printf \x..`, base64 decode, `python -c`, etc.
#
# The goal is simply: "model accidentally pastes a suggested curl|sh from some
# random website" > "silently blocked with a clear message in the tool result".
# If you need a real sandbox, run the agent inside a container with a
# read-only rootfs and no network — not a regex.

_SHELL_BLOCKED_PATTERNS = re.compile(
    r"(?:^|[\s;|&])\s*(?:"
    r"sudo\b|su\s+\w|"                           # privilege escalation
    r"rm\s+-[rf]*\s+/|rm\s+-[rf]*\s+~/|rm\s+-[rf]*\s+\$HOME|"  # recursive delete root/home
    r">\s*/dev/|dd\s+if=|"                        # raw device writes
    r"mkfs|fdisk|parted|"                         # disk formatting
    r"chmod\s+[0-7]{3,4}\s+/|chown\s+\S+\s+/|"   # system permission changes
    r"shutdown|reboot|halt|poweroff|"             # system control
    r"pkill\s+-9|killall\s|kill\s+-9\s+1\b"       # process killing
    r")",
    re.IGNORECASE
)

_SHELL_BLOCKED_EXACT = [
    "rm -rf /", "rm -rf /*", "rm -rf ~", "rm -rf $HOME",
    ":(){:|:&};:",   # fork bomb
    ":(){ :|:& };:", # fork bomb variant
]

# Additional hardening patterns — applied to the NORMALIZED command (after
# NFKC folding, empty-quote stripping, and bounded hex unescaping) so the
# obvious obfuscations below are caught alongside the plain forms.
_SHELL_HARDENED_PATTERNS = re.compile(
    r"(?:"
    r"\$\(\s*(?:curl|wget)\b"                  # $(curl ...) — command substitution to fetch
    r"|<\(\s*(?:curl|wget)\b"                  # <(curl ...) — bash process substitution
    r"|\beval\b[^\n]*\$\("                     # eval ... $(...) — double indirection
    r"|\beval\b[^\n]*`"                         # eval ... `...` — backtick variant
    r"|\bpython[23]?\s+-c\b[^\n]*(?:os\.system|subprocess\.|__import__|exec\s*\()"
    r"|\bperl\s+-e\b[^\n]*(?:system|exec|`)"   # perl one-liner shelling out
    r"|\bruby\s+-e\b[^\n]*(?:system|exec|`)"   # ruby one-liner shelling out
    r"|\bnode\s+-e\b[^\n]*(?:child_process|execSync|spawnSync)"
    r"|\bbase64\s+(?:-d|--decode)\b[^\n]*\|\s*(?:sh|bash|zsh|python)"
    # Dynamic command word producing rm-flags: ``$(echo rm) -rf /`` — the
    # regex keyword check can't see "rm", but the ``-rf /`` (or ``-rf ~``)
    # argument is already a strong signal by itself.
    r"|\$\([^)]{1,40}\)\s+-[rRf]+\s+[/~]"
    r"|`[^`]{1,40}`\s+-[rRf]+\s+[/~]"           # backtick variant
    r")",
    re.IGNORECASE,
)


def _normalize_for_safety_check(cmd: str) -> str:
    """Fold obfuscation so regex patterns catch common bypasses.

    Applies, in order:
    1. NFKC unicode normalisation — folds compat forms AND strips some
       lookalikes (but NOT all; Cyrillic letters with distinct codepoints
       survive, which we handle in step 4).
    2. Remove empty-string quoting: ``rm""`` / ``rm''`` / ``rm""`` /
       ``rm""`` all become ``rm``. Bash treats these as a no-op
       concatenation, so an attacker uses them to split a keyword across
       what the regex thinks are token boundaries.
    3. Bounded hex/octal unescape of ``\\xNN`` / ``\\NNN`` sequences — decode
       up to 256 occurrences (plenty for ``\\x72\\x6d -rf /``-style tricks,
       but won't loop forever on adversarial input).
    4. Transliterate known Cyrillic/Greek lookalikes (ѕ→s, а→a, е→e, о→o,
       р→p, с→c, и→n, ԁ→d) so ``ѕudo`` matches the ``sudo`` pattern.

    Returned string is used ONLY for the safety check — the original
    (untransformed) command is still what's passed to the shell.
    """
    if not cmd:
        return ""
    # 1. NFKC
    out = unicodedata.normalize("NFKC", cmd)
    # 2. Empty-string quote pairs — ASCII and smart-quote variants. Do this
    # in a bounded loop because removing one pair may abut another.
    _empty_quotes = ('""', "''", "\u201c\u201d", "\u2018\u2019", "\u00ab\u00bb")
    for _ in range(8):
        before = out
        for q in _empty_quotes:
            out = out.replace(q, "")
        if out == before:
            break
    # 3. Hex unescape (\xNN). Bounded count guards against pathological input.
    def _hex_sub(m):
        try:
            return chr(int(m.group(1), 16))
        except Exception:
            return m.group(0)
    out = re.sub(r"\\x([0-9a-fA-F]{2})", _hex_sub, out, count=256)
    # Octal (\NNN) — common in printf. Bounded.
    def _oct_sub(m):
        try:
            return chr(int(m.group(1), 8))
        except Exception:
            return m.group(0)
    out = re.sub(r"\\([0-3][0-7]{2})", _oct_sub, out, count=256)
    # 4. Known Cyrillic/Greek lookalikes — transliterate to ASCII so the
    # downstream patterns (which are ASCII) catch ``ѕudo``, ``rе``, etc.
    _lookalikes = str.maketrans({
        "\u0455": "s",  # Cyrillic dze ѕ → s
        "\u0430": "a",  # Cyrillic а → a
        "\u0435": "e",  # Cyrillic е → e
        "\u043e": "o",  # Cyrillic о → o
        "\u0440": "p",  # Cyrillic р → p
        "\u0441": "c",  # Cyrillic с → c
        "\u0445": "x",  # Cyrillic х → x
        "\u0443": "y",  # Cyrillic у → y (visually; used in ``уm``)
        "\u03bf": "o",  # Greek omicron ο → o
        "\u03b1": "a",  # Greek alpha α → a
        "\u03c1": "p",  # Greek rho ρ → p
        "\u03c5": "u",  # Greek upsilon υ → u (visually close)
        "\u0456": "i",  # Cyrillic і → i
    })
    out = out.translate(_lookalikes)
    return out


def _check_shell_safety(cmd: str) -> str | None:
    """Returns a block reason string if the command looks dangerous, else None.

    Speed bump, not a sandbox — see the module-level comment above.

    Checks, in order:
    1. Exact-substring match of known-bad commands (``rm -rf /`` etc.).
    2. Regex against the raw command (catches plain forms).
    3. Regex against the *normalized* command (NFKC-folded, empty-quote-
       stripped, hex-unescaped, Cyrillic-transliterated).
    4. Piped-download-to-shell match (``curl ... | sh``) against normalized.
    5. Hardened patterns (``$(curl ...)``, ``<(curl ...)``, ``eval $(...)``,
       ``python -c ... os.system``, etc.) against normalized.
    """
    if not isinstance(cmd, str):
        return None
    # Exact substring matches (raw — NFKC might mangle them)
    for b in _SHELL_BLOCKED_EXACT:
        if b in cmd:
            return "Blocked: dangerous command pattern."

    # Normalize for the remaining checks so obfuscation doesn't slip past.
    norm = _normalize_for_safety_check(cmd)

    # Original patterns — check both raw and normalized so (a) nothing that
    # used to be blocked is silently unblocked by normalization, and (b)
    # obfuscated forms are now caught.
    if _SHELL_BLOCKED_PATTERNS.search(cmd) or _SHELL_BLOCKED_PATTERNS.search(norm):
        return "Blocked: potentially dangerous command."
    # Block curl/wget piped to shell (remote code execution) — normalized so
    # ``curl ""foo"" | sh`` (with empty strings inserted) is caught.
    if re.search(r"(?:curl|wget)\s.*\|\s*(?:sh|bash|zsh|python)", norm, re.IGNORECASE):
        return "Blocked: piping downloads to shell not allowed."
    # Hardened checks — command substitution, process substitution, eval,
    # python/perl/ruby/node indirection, base64-decode-pipe.
    if _SHELL_HARDENED_PATTERNS.search(norm):
        return "Blocked: obfuscated or indirect dangerous command."
    return None


# ── Persistent Camera (OpenCV) ──

import threading as _cam_threading

_camera_lock = _cam_threading.Lock()
_camera_cap = None      # cv2.VideoCapture instance (stays open)
_camera_last_frame = None  # latest base64 JPEG
_camera_last_ts = 0.0   # timestamp of last capture


def _camera_grab_frame() -> str | None:
    """Grab a camera frame. Tries: 1) WebSocket (browser), 2) OpenCV (direct).
    OpenCV camera stays open for fast subsequent captures.
    Returns base64 JPEG or None.
    """
    global _camera_cap, _camera_last_frame, _camera_last_ts
    import time as _time

    # Try 1: WebSocket (browser camera)
    try:
        from server import request_camera_frame_sync
        frame = request_camera_frame_sync(timeout=3.0)
        if frame:
            _log.info(f"camera: frame via WebSocket ({len(frame)} chars)")
            return frame
    except (ImportError, Exception):
        pass

    # Try 2: OpenCV persistent camera
    with _camera_lock:
        try:
            import cv2
        except ImportError:
            _log.warning("camera: opencv-python not installed")
            return None

        # Open camera if not yet open
        if _camera_cap is None or not _camera_cap.isOpened():
            cam_setting = config.get("camera_index")
            if cam_setting >= 0:
                # Specific camera requested
                _log.info(f"camera: opening index {cam_setting} (from settings)")
                _camera_cap = cv2.VideoCapture(cam_setting)
                if _camera_cap.isOpened():
                    for _ in range(5): _camera_cap.read()
                    _time.sleep(0.3)
                else:
                    _log.warning(f"camera: index {cam_setting} not available")
                    _camera_cap = None
                    return None
            else:
                # Auto-detect: try indexes 0-3, pick first with real frames
                _log.info("camera: auto-detecting...")
                for cam_idx in range(4):
                    cap = cv2.VideoCapture(cam_idx)
                    if not cap.isOpened():
                        continue
                    for _ in range(5): cap.read()
                    _time.sleep(0.3)
                    ret, test_frame = cap.read()
                    if ret and test_frame.mean() > 3:
                        _camera_cap = cap
                        _log.info(f"camera: auto-selected index {cam_idx} ({test_frame.shape[1]}x{test_frame.shape[0]})")
                        break
                    cap.release()
            if _camera_cap is None or not _camera_cap.isOpened():
                _log.warning("camera: no working camera found")
                _camera_cap = None
                return None

        ret, img = _camera_cap.read()
        if not ret:
            _log.warning("camera: read() failed, reopening...")
            _camera_cap.release()
            _camera_cap = None
            return None

        # Resize to ~49K pixels
        import math
        h, w = img.shape[:2]
        max_area = 49152
        if w * h > max_area:
            scale = math.sqrt(max_area / (w * h))
            img = cv2.resize(img, (int(w * scale), int(h * scale)))

        import base64 as _b64
        _, buf = cv2.imencode('.jpg', img, [cv2.IMWRITE_JPEG_QUALITY, 70])
        frame = _b64.b64encode(buf).decode()
        _camera_last_frame = frame
        _camera_last_ts = _time.time()
        _log.info(f"camera: frame via OpenCV ({len(frame)} chars)")
        return frame


def camera_release():
    """Release the persistent camera (called on shutdown)."""
    global _camera_cap
    with _camera_lock:
        if _camera_cap:
            _camera_cap.release()
            _camera_cap = None


# ── Core tools (always loaded) vs Extended (loaded via tool_search) ──

_CORE_TOOL_NAMES = {
    "memory_search", "memory_save",
    "read_file", "write_file", "shell",
    "self_config",  # manage own settings
    "http_request", "spawn_task",
    "tool_search",  # meta-tool to discover more tools
    "browser_open", "browser_snapshot", "browser_set_visible",
    "browser_click", "browser_fill", "browser_eval",  # web interaction
    "send_file",  # attach file to chat message
    "camera_capture",  # capture camera frame for vision analysis
    "open_url",  # open URL in user's real desktop browser
}

# Pending files to attach to the response (populated by send_file tool)
_pending_files: list[dict] = []  # [{path, name, url, size}]

# Session-level: tools activated by tool_search (persists within agent turn)
_active_extra_tools: set[str] = set()


_spicy_duck_on: bool | None = None  # cached per turn


def _reset_active_tools():
    """Reset extra tools between turns."""
    global _spicy_duck_on
    _active_extra_tools.clear()
    _pending_files.clear()
    _spicy_duck_on = None  # re-check on next get_all_tools()


def get_pending_files() -> list[dict]:
    """Get files queued by send_file tool for attachment to response."""
    return list(_pending_files)


# ── Tool definitions — SHORT descriptions, small models need clarity ──

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "memory_search",
            "description": "Search saved memories by query.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_save",
            "description": (
                "Save a DURABLE fact to long-term memory. "
                "Call this ONLY when: (1) user explicitly says remember/запомни/save, OR "
                "(2) you learned a stable fact about the user (name, role, location, stack, preferences, deadlines, project constants) "
                "that will matter in future conversations. "
                "DO NOT save: conversational intents ('user wants X'), current session plans, task lists, "
                "acknowledgments ('user said hi'), transient requests, your own reasoning, or what you're about to do. "
                "Rule of thumb: if it won't be useful a week from now, don't save it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "The durable fact (NOT a task description or intent summary). Long texts auto-chunked."},
                    "tag": {"type": "string", "description": "Category: user (about the user) / project (stable project info) / fact (general) / decision (committed choice) / knowledge (domain info). AVOID 'task'."},
                    "source": {"type": "string", "description": "Source name (article title, URL, filename)"},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_delete",
            "description": "Delete a memory by search query. Finds closest match and removes it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search to find memory to delete"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "shell",
            "description": "Run a bash shell command in workspace directory. Use UNIX commands (ls, find, grep, cat), NOT Windows (dir, findstr). Returns stdout+stderr.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to run"},
                    "timeout": {"type": "integer", "description": "Seconds to wait (default 120)"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file's contents.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write content to a file. Relative paths go to workspace. Creates directories if needed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path"},
                    "content": {"type": "string", "description": "File content"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_file",
            "description": "Attach a file to the chat message so user can download it. Use after write_file to share the result. Do NOT use for directories or large numbers of files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file to send"},
                    "caption": {"type": "string", "description": "Short description of the file (optional)"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "camera_capture",
            "description": "Capture a photo from the user's camera RIGHT NOW and analyze it. Use when user says 'look', 'what do you see', 'check camera', or when you need to see something. Camera must be enabled by user.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "What to look for or analyze (e.g. 'describe what you see', 'read the text on the paper', 'identify the schematic')"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_url",
            "description": "Open URL in the user's REAL desktop browser (visible to user). Use when user says 'open', 'launch', 'show me in browser'. NOT for reading pages — use browser_open for that.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to open"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "schedule_task",
            "description": "Schedule a task to run later or repeatedly. Auto-validates via dry-run before saving. Formats: 'in 5m', 'in 2h', 'every 30m', 'daily 09:00', '14:30'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Short name for the task"},
                    "task": {"type": "string", "description": "What to do when the time comes"},
                    "schedule": {"type": "string", "description": "When: 'in 5m', 'every 1h', 'daily 09:00', '14:30'"},
                    "skip_dry_run": {"type": "boolean", "description": "Skip validation dry-run (default false)"},
                },
                "required": ["name", "task", "schedule"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_cron",
            "description": "List all scheduled/cron tasks.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_cron",
            "description": "Remove a scheduled task by its ID number.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer", "description": "Task ID to remove"},
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "switch_model",
            "description": "Switch to a different LLM model or provider. Use when user asks to change model.",
            "parameters": {
                "type": "object",
                "properties": {
                    "model": {"type": "string", "description": "Model name to switch to"},
                    "provider": {"type": "string", "description": "Provider name (lmstudio/openai/groq/etc). Optional."},
                },
                "required": ["model"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "spawn_task",
            "description": "Run a task in background. Use when user gives 2+ tasks at once.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "Task description — what the background worker should do"},
                },
                "required": ["task"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "secret_save",
            "description": "Securely store a secret (password, API key, token). Encrypted in vault.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Secret name (e.g. 'github_token')"},
                    "value": {"type": "string", "description": "Secret value"},
                },
                "required": ["key", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "secret_get",
            "description": "Retrieve a stored secret by name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Secret name"},
                },
                "required": ["key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "secret_list",
            "description": "List all stored secret names (not values).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "secret_delete",
            "description": "Delete a stored secret.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Secret name to delete"},
                },
                "required": ["key"],
            },
        },
    },
    # User profile tools
    {
        "type": "function",
        "function": {
            "name": "user_profile_update",
            "description": "Save a NEW fact about the user (name, timezone, preferences). Only call when you learn something new.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Fact key (e.g. 'name', 'timezone', 'language', 'tech_stack')"},
                    "value": {"type": "string", "description": "Fact value"},
                },
                "required": ["key", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "user_profile_get",
            "description": "Show the user's saved profile.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    # HTTP request tool
    {
        "type": "function",
        "function": {
            "name": "http_request",
            "description": "Make HTTP request to REST APIs / webhooks / JSON endpoints (Telegram bot API, weather API, GitHub API, etc.). DO NOT use for web pages — always use browser_open for websites, HTML pages, search results, news, articles.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Full URL including https://"},
                    "method": {"type": "string", "enum": ["GET", "POST", "PUT", "DELETE"], "description": "HTTP method (default GET)"},
                    "body": {"type": "string", "description": "Request body (JSON string for POST/PUT)"},
                    "headers": {"type": "object", "description": "Extra headers as key-value pairs"},
                },
                "required": ["url"],
            },
        },
    },
    # RAG tools
    {
        "type": "function",
        "function": {
            "name": "rag_index",
            "description": "Index a file or directory for search. Supports: txt, md, py, js, json, pdf, etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File or directory path to index"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rag_search",
            "description": "Search indexed files by query. Returns relevant text chunks with file paths.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "limit": {"type": "integer", "description": "Max results (default 5)"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rag_status",
            "description": "Show RAG index status: files and chunks count.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    # Self-config: read/write own settings
    {
        "type": "function",
        "function": {
            "name": "self_config",
            "description": "Read or change qwe-qwe's own settings. action='list' shows all, action='get' reads one, action='set' changes one. Keys: telegram:bot_token, telegram:chat_id, telegram:group_id, streaming:telegram, or any setting name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "get", "set"], "description": "list=show all settings, get=read one, set=change one"},
                    "key": {"type": "string", "description": "Setting key (e.g. 'telegram:bot_token', 'max_tool_rounds', 'context_budget')"},
                    "value": {"type": "string", "description": "New value (for action=set)"},
                },
                "required": ["action"],
            },
        },
    },
    # Meta-tool: discover additional tools
    {
        "type": "function",
        "function": {
            "name": "tool_search",
            "description": "Find and activate additional tools by keyword. Use when you need a capability not in your current tools. IMPORTANT: for ANY web/internet task (search, news, open URL, browse site) use keyword 'browser'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Keyword: browser (web/internet/news/search), notes, schedule, secret, mcp, profile, rag, skill, timer, cron, model"},
                },
                "required": ["query"],
            },
        },
    },
]


# ── Tool search index ──
# Maps keywords to tool names for discovery

_BROWSER_ALL = [
    "browser_open", "browser_snapshot", "browser_screenshot",
    "browser_click", "browser_fill", "browser_eval", "browser_close",
    "browser_back", "browser_forward", "browser_reload",
    "browser_accessibility", "browser_console",
    "browser_hover", "browser_select", "browser_press_key", "browser_wait_for", "browser_upload", "browser_drag",
    "browser_tabs", "browser_tab_new", "browser_tab_switch", "browser_tab_close",
    "browser_network",
]

_TOOL_SEARCH_INDEX = {
    "browser": _BROWSER_ALL,
    "web": _BROWSER_ALL + ["http_request"],
    "search": ["browser_open", "memory_search", "rag_search"],
    "google": ["browser_open"],
    "notes": ["create_note", "list_notes", "read_note", "delete_note", "edit_note"],
    "note": ["create_note", "list_notes", "read_note", "delete_note", "edit_note"],
    "schedule": ["schedule_task", "list_cron", "remove_cron"],
    "cron": ["schedule_task", "list_cron", "remove_cron"],
    "timer": ["set_timer", "schedule_task"],
    "secret": ["secret_save", "secret_get", "secret_list", "secret_delete"],
    "vault": ["secret_save", "secret_get", "secret_list", "secret_delete"],
    "password": ["secret_save", "secret_get"],
    "key": ["secret_save", "secret_get"],
    "mcp": ["mcp_list_servers", "mcp_add_server", "mcp_remove_server", "mcp_restart_server", "mcp_toggle_server"],
    "profile": ["user_profile_update", "user_profile_get"],
    "user": ["user_profile_update", "user_profile_get", "memory_search"],
    "rag": ["rag_index", "rag_search", "rag_status"],
    "index": ["rag_index", "rag_status"],
    "knowledge": ["rag_index", "rag_search"],
    "model": ["switch_model"],
    "switch": ["switch_model"],
    "skill": ["create_skill", "delete_skill", "list_skill_files"],
    "soul": ["add_trait", "remove_trait", "list_traits"],
    "trait": ["add_trait", "remove_trait", "list_traits"],
    "personality": ["add_trait", "remove_trait", "list_traits"],
    "memory": ["memory_search", "memory_save", "memory_delete"],
    "delete": ["memory_delete", "secret_delete", "delete_note"],
    "file": ["read_file", "write_file"],
    "screenshot": ["browser_screenshot"],
    "navigate": ["browser_open"],
    "click": ["browser_click", "browser_fill"],
    "news": ["browser_open", "browser_snapshot"],
    "internet": ["browser_open", "browser_snapshot"],
    "browse": ["browser_open", "browser_snapshot", "browser_screenshot"],
    "url": ["browser_open"],
    "site": ["browser_open", "browser_snapshot"],
    "page": ["browser_open", "browser_snapshot"],
    "lovense": ["lovense_connect", "lovense_vibrate", "lovense_pattern", "lovense_preset", "lovense_stop", "lovense_status"],
    "spicy": ["lovense_connect", "lovense_vibrate", "lovense_pattern", "lovense_preset", "lovense_stop", "lovense_status"],
    "duck": ["lovense_connect", "lovense_vibrate", "lovense_pattern", "lovense_preset", "lovense_stop", "lovense_status"],
    "vibrate": ["lovense_vibrate", "lovense_pattern", "lovense_preset", "lovense_stop"],
    "toy": ["lovense_connect", "lovense_vibrate", "lovense_stop", "lovense_status"],
}


def _do_self_config(args: dict) -> str:
    """Read or change qwe-qwe's own settings."""
    import db
    action = args.get("action", "list")
    key = args.get("key", "")
    value = args.get("value", "")

    if action == "list":
        lines = ["=== Editable Settings ==="]
        for k, (kv_key, type_, default, desc, *_) in config.EDITABLE_SETTINGS.items():
            current = db.kv_get(kv_key)
            if current is None:
                current = str(default)
            lines.append(f"  {k} = {current}  ({desc})")
        # Also show key system KV values
        lines.append("\n=== System Keys ===")
        for sys_key in ["telegram:bot_token", "telegram:chat_id", "telegram:group_id",
                        "telegram:streaming", "user_name", "active_skills",
                        "soul:name", "soul:language"]:
            val = db.kv_get(sys_key)
            if val and "token" in sys_key:
                val = val[:15] + "..." if len(val) > 15 else val  # mask tokens
            lines.append(f"  {sys_key} = {val or '(not set)'}")
        return "\n".join(lines)

    elif action == "get":
        if not key:
            return "Error: 'key' is required for action='get'"
        # Try editable settings first
        if key in config.EDITABLE_SETTINGS:
            return f"{key} = {config.get(key)}"
        # Try raw KV
        val = db.kv_get(key)
        return f"{key} = {val}" if val is not None else f"{key} is not set"

    elif action == "set":
        if not key:
            return "Error: 'key' is required for action='set'"
        if not value and value != "0":
            return "Error: 'value' is required for action='set'"
        # Try editable settings first (has validation)
        if key in config.EDITABLE_SETTINGS:
            return config.set(key, value)
        # Raw KV set for system keys (telegram, soul, etc.)
        db.kv_set(key, value)
        # Auto-restart telegram bot when token changes
        if key == "telegram:bot_token" and value:
            try:
                import telegram_bot
                telegram_bot.stop()
                telegram_bot.set_token(value)
                telegram_bot.start()
                return f"✓ {key} set + telegram bot restarted"
            except Exception as e:
                return f"✓ {key} set (bot restart failed: {e})"
        return f"✓ {key} = {value}"

    return f"Unknown action: {action}. Use 'list', 'get', or 'set'."


def _do_tool_search(query: str) -> str:
    """Search for tools by keyword. Returns matching tool names and activates them."""
    import re as _re
    # Sanitize: strip hallucinated tool_call syntax, XML tags, etc.
    query = _re.sub(r'[<>{}|"\']', ' ', query)
    query = _re.sub(r'tool_call|call:|function', '', query, flags=_re.IGNORECASE)
    query_lower = query.lower().strip().split()[0] if query.strip() else ""  # take first word only
    found = set()

    # Direct keyword match
    for kw, tool_names in _TOOL_SEARCH_INDEX.items():
        if kw in query_lower or query_lower in kw:
            found.update(tool_names)

    # Also search tool descriptions from all available tools
    if not found:
        all_t = _get_all_tools_full()
        for t in all_t:
            fn = t["function"]
            if query_lower in fn["name"] or query_lower in fn.get("description", "").lower():
                found.add(fn["name"])

    if not found:
        return f"No tools found for '{query}'. Available keywords: browser, notes, schedule, secret, mcp, profile, rag, skill, soul, timer, model"

    # Check if tools already activated — short-circuit to prevent repeated tool_search
    if found and found.issubset(_active_extra_tools):
        tool_list = ", ".join(sorted(found))
        return (
            f"ALREADY ACTIVE: {tool_list}. "
            f"Do NOT call tool_search again. Call the tool directly (e.g., browser_open)."
        )

    # Activate found tools for this turn
    _active_extra_tools.update(found)

    # Return descriptions of activated tools
    all_t = _get_all_tools_full()
    lines = [f"Activated {len(found)} tools:"]
    for t in all_t:
        fn = t["function"]
        if fn["name"] in found:
            params = list(fn.get("parameters", {}).get("properties", {}).keys())
            lines.append(f"  - {fn['name']}({', '.join(params)}): {fn.get('description', '')}")
    lines.append("\nCall these tools directly NOW. Do NOT call tool_search again.")
    return "\n".join(lines)


def _get_all_tools_full() -> list[dict]:
    """Get ALL tools (core + extended + skills + MCP) without filtering."""
    import skills
    all_tools = list(TOOLS)
    all_tools += skills.get_tools(compact=True)
    try:
        import mcp_client
        all_tools += mcp_client.get_all_mcp_tools()
    except Exception:
        pass
    return all_tools


# ── Tool execution ──

def execute(name: str, args: dict) -> str:
    """Execute a tool and return result as string."""
    try:
        # MCP tools: mcp__servername__toolname
        if name.startswith("mcp__"):
            import mcp_client
            return mcp_client.execute_mcp_tool(name, args)

        if name == "tool_search":
            return _do_tool_search(args.get("query", ""))

        elif name == "self_config":
            return _do_self_config(args)

        elif name == "memory_search":
            results = memory.search(args["query"], tag=args.get("tag"))
            if not results:
                return "No memories found."
            return "\n".join(
                f"[{r['tag']}] (score:{r['score']}) {r['text']}" for r in results
            )

        elif name == "memory_delete":
            results = memory.search(args["query"], limit=1)
            if not results:
                return "No matching memory found."
            point_id = results[0]["id"]
            text_preview = results[0]["text"][:60]
            memory.delete(point_id)
            return f"✓ Deleted memory: {text_preview}..."

        elif name == "memory_save":
            text = args["text"]
            tag = args.get("tag", "general")
            meta = {}
            if args.get("source"):
                meta["source"] = args["source"]
            pid = memory.save(text, tag=tag, meta=meta if meta else None)
            chunked = len(text) > 1000
            if chunked:
                chunks = memory._chunk_text(text)
                return f"Saved ({len(chunks)} chunks, group id: {pid[:8]}, queued for synthesis)"
            return f"Saved (id: {pid[:8]})"

        elif name == "read_file":
            _raw = _get_path_arg(args)
            if not _raw:
                return "Error: missing required argument 'path'"
            p = _resolve_path(_raw)
            if not p.exists():
                return f"Error: file not found: {_raw}"
            text = p.read_text(encoding="utf-8", errors="replace")
            total_len = len(text)
            if total_len > 8000:
                text = text[:8000] + f"\n... (truncated, {total_len} chars total)"
            if total_len > 4000:
                text += (
                    f"\n⚠️ Large file ({total_len} chars). "
                    f"To modify: edit ONLY the specific part, don't rewrite the whole file. "
                    f"Use shell('sed ...') or write only the changed section."
                )
            return text

        elif name == "write_file":
            _raw = _get_path_arg(args)
            if not _raw:
                return "Error: missing required argument 'path'"
            p = _resolve_path(_raw, for_write=True)
            p.parent.mkdir(parents=True, exist_ok=True)
            content = args.get("content", "")
            p.write_text(content, encoding="utf-8")
            return f"Written {len(content)} chars to {p}"

        elif name == "send_file":
            _raw = _get_path_arg(args)
            if not _raw:
                return "Error: missing required argument 'path'"
            p = _resolve_path(_raw)
            if not p.exists():
                return f"Error: file not found: {_raw}"
            if p.is_dir():
                return "Error: send_file works with single files, not directories"
            size = p.stat().st_size
            if size > 50 * 1024 * 1024:
                return f"Error: file too large ({size // 1024 // 1024} MB). Max 50 MB."
            # Copy to uploads for serving via HTTP
            import uuid as _uuid
            file_id = _uuid.uuid4().hex[:8]
            dest = Path(config.UPLOADS_DIR) / f"{file_id}_{p.name}"
            shutil.copy2(str(p), str(dest))
            url = f"/uploads/{dest.name}"
            caption = args.get("caption", p.name)
            _pending_files.append({
                "path": str(p),
                "name": p.name,
                "url": url,
                "size": size,
                "caption": caption,
            })
            return f"File attached: {p.name} ({size / 1024:.1f} KB). User will see download link."

        elif name == "open_url":
            url = args.get("url", "")
            if not url:
                return "Error: URL required"
            try:
                if sys.platform == "win32":
                    subprocess.Popen(["cmd.exe", "/c", "start", "", url], shell=False)
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", url])
                else:
                    subprocess.Popen(["xdg-open", url])
                return f"Opened {url} in desktop browser."
            except Exception as e:
                return f"Error opening URL: {e}"

        elif name == "camera_capture":
            frame = _camera_grab_frame()
            if not frame:
                return "Error: no camera available. Connect a webcam or enable camera in web UI."
            # Save frame to uploads so user can see it in chat
            import uuid as _uuid, base64 as _b64
            img_id = _uuid.uuid4().hex[:8]
            img_path = Path(config.UPLOADS_DIR) / f"cam_{img_id}.jpg"
            img_path.write_bytes(_b64.b64decode(frame))
            img_url = f"/uploads/cam_{img_id}.jpg"
            _pending_files.append({
                "path": str(img_path), "name": f"cam_{img_id}.jpg",
                "url": img_url, "size": img_path.stat().st_size,
                "caption": "Camera capture", "is_image": True,
            })

            # Send frame to LLM for vision analysis — uses current active model
            prompt = args.get("prompt") or "Describe what you see in detail."
            import providers
            try:
                resp = providers.get_client().chat.completions.create(
                    model=providers.get_model(),
                    messages=[{"role": "user", "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{frame}"}},
                        {"type": "text", "text": prompt},
                    ]}],
                    temperature=0.3, max_tokens=1024, stream=False,
                )
                return f"Camera capture:\n{resp.choices[0].message.content or '(no description)'}"
            except Exception as e:
                return f"Error analyzing camera frame: {e}"

        elif name == "shell":
            cmd = args["command"]
            _log.info(f"shell: {cmd[:200]}")
            # Safety check — block dangerous command patterns
            block_reason = _check_shell_safety(cmd)
            if block_reason:
                _log.warning(f"shell blocked: {cmd}")
                return block_reason
            t = min(args.get("timeout", 120), 300)
            cwd = str(WORKSPACE)

            env = os.environ.copy()
            venv = os.environ.get("VIRTUAL_ENV")
            if venv:
                env["PATH"] = f"{venv}/bin:" + env.get("PATH", "")
            env["PYTHONIOENCODING"] = "utf-8"

            # Popen + polling so we can react to the user pressing Stop.
            # subprocess.run() would block the whole thread for up to 300s.
            # We poll every 200ms and check the per-thread abort event; on
            # abort we kill the whole process tree and return a concise message.
            #
            # Killing the tree matters: ``bash -c "sleep 10"`` spawns a child
            # ``sleep`` that inherits the pipe fds. Killing only bash leaves
            # ``sleep`` holding stdout/stderr open, and ``communicate()`` then
            # hangs. So we use a new process group (POSIX) / CREATE_NEW_PROCESS_GROUP
            # (Windows), and ``taskkill /T /F`` on Windows for tree kill.
            abort_evt = _get_abort_event()
            popen_kwargs = dict(
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL, env=env, cwd=cwd,
            )
            if sys.platform == "win32":
                popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            else:
                popen_kwargs["start_new_session"] = True
            try:
                if _SHELL_EXE:
                    proc = subprocess.Popen([_SHELL_EXE, "-c", cmd], **popen_kwargs)
                else:
                    proc = subprocess.Popen(cmd, shell=True, **popen_kwargs)
            except OSError as e:
                _log.error(f"shell OSError: {e}")
                return f"Error: shell failed ({e}). Try a simpler command."

            def _kill_tree():
                """Kill proc and all descendants. Best-effort, never raises."""
                if sys.platform == "win32":
                    try:
                        subprocess.run(
                            ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            timeout=3,
                        )
                    except Exception as e:
                        _log.warning(f"taskkill failed: {e}; falling back to proc.kill()")
                        try:
                            proc.kill()
                        except Exception:
                            pass
                else:
                    try:
                        os.killpg(os.getpgid(proc.pid), 9)  # SIGKILL
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass

            deadline = time.monotonic() + t
            aborted = False
            timed_out = False
            poll_interval = 0.2
            try:
                while True:
                    if proc.poll() is not None:
                        break
                    if abort_evt is not None and abort_evt.is_set():
                        aborted = True
                        break
                    if time.monotonic() >= deadline:
                        timed_out = True
                        break
                    time.sleep(poll_interval)
            finally:
                if proc.poll() is None:
                    _kill_tree()
                try:
                    stdout_b, stderr_b = proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    stdout_b, stderr_b = b"", b""
                except Exception:
                    stdout_b, stderr_b = b"", b""

            stdout = (stdout_b or b"").decode("utf-8", errors="replace")
            stderr = (stderr_b or b"").decode("utf-8", errors="replace")

            if aborted:
                return "⏹ Shell aborted (user pressed Stop)."
            if timed_out:
                output = stdout
                if stderr:
                    output += f"\nSTDERR: {stderr}"
                output += f"\n(timed out after {t}s)"
                return output.strip() or f"(no output; timed out after {t}s)"

            output = stdout
            if stderr:
                output += f"\nSTDERR: {stderr}"
            if proc.returncode != 0:
                output += f"\n(exit code: {proc.returncode})"
            # Truncate long outputs for small context models
            if len(output) > 2000:
                output = output[:1000] + "\n...(truncated)...\n" + output[-500:]
            return output.strip() or "(no output)"

        elif name == "schedule_task":
            import scheduler
            result = scheduler.add(
                args["name"], args["task"], args["schedule"],
                skip_dry_run=args.get("skip_dry_run", False),
            )
            if result.get("error"):
                parts = [f"Error: {result['error']}"]
                if result.get("output"):
                    parts.append(f"Output: {result['output']}")
                if result.get("hint"):
                    parts.append(f"Hint: {result['hint']}")
                return "\n".join(parts)
            repeat_str = " (repeating)" if result["repeat"] else " (one-time)"
            msg = f"✓ Scheduled '{result['name']}' → next run: {result['next_run']}{repeat_str}"
            if result.get("preview"):
                msg += f"\nDry-run preview: {result['preview']}"
            return msg

        elif name == "list_cron":
            import scheduler
            tasks_list = scheduler.list_tasks()
            if not tasks_list:
                return "No scheduled tasks."
            lines = []
            for t in tasks_list:
                repeat = "🔄" if t["repeat"] else "⏱"
                lines.append(f"#{t['id']} {repeat} {t['name']} → {t['next_run']} ({t['schedule']}) | {t['task'][:60]}")
            return "\n".join(lines)

        elif name == "remove_cron":
            import scheduler
            return scheduler.remove(args["task_id"])

        elif name == "switch_model":
            import providers as prov
            result_parts = []
            if args.get("provider"):
                r = prov.switch(args["provider"])
                result_parts.append(r)
            r = prov.set_model(args["model"])
            result_parts.append(r)
            return " | ".join(result_parts)

        elif name == "spawn_task":
            import tasks
            task_id = tasks.spawn(args["task"])
            return f"Task #{task_id} queued: {args['task'][:60]}"

        elif name == "secret_save":
            import vault
            return vault.save(args["key"], args["value"])

        elif name == "secret_get":
            import vault
            val = vault.get(args["key"])
            return val if val else f"Secret '{args['key']}' not found"

        elif name == "secret_list":
            import vault
            keys = vault.list_keys()
            return ", ".join(keys) if keys else "No secrets stored"

        elif name == "secret_delete":
            import vault
            return vault.delete(args["key"])

        elif name == "user_profile_update":
            import db
            key = args["key"].strip().lower().replace(" ", "_")
            db.kv_set(f"user:{key}", args["value"])
            return f"Profile updated: {key} = {args['value']}"

        elif name == "user_profile_get":
            import db
            profile = db.kv_get_prefix("user:")
            if not profile:
                return "No profile data yet."
            lines = [f"- {k.replace('user:', '')}: {v}" for k, v in sorted(profile.items())]
            return "\n".join(lines)

        elif name == "http_request":
            import urllib.request
            import urllib.error
            import socket
            from urllib.parse import urlparse, quote
            url = args["url"]
            # Encode non-ASCII characters in URL (e.g. Cyrillic)
            url = quote(url, safe=':/?#[]@!$&\'()*+,;=-._~%')
            # Basic URL validation (no SSRF blocking — qwe-qwe is a local agent)
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https"):
                return f"Error: only http/https URLs allowed, got '{parsed.scheme}'"
            method = args.get("method", "GET").upper()
            body = args.get("body")
            hdrs = {"User-Agent": "qwe-qwe/0.5"}
            if body:
                hdrs["Content-Type"] = "application/json"
            if args.get("headers"):
                hdrs.update(args["headers"])
            data = body.encode("utf-8") if body else None
            req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
            # For localhost HTTPS, skip SSL verification (self-signed certs are normal)
            ssl_ctx = None
            if parsed.scheme == "https" and parsed.hostname in ("localhost", "127.0.0.1", "::1"):
                import ssl as _ssl
                ssl_ctx = _ssl.create_default_context()
                ssl_ctx.check_hostname = False
                ssl_ctx.verify_mode = _ssl.CERT_NONE
            # Short timeout so aborts aren't blocked for 15s.
            # urllib.request.urlopen has no built-in abort hook — we rely on the
            # 5s socket timeout plus abort-event checks before and after.
            abort_evt = _get_abort_event()
            if abort_evt is not None and abort_evt.is_set():
                return "⏹ HTTP aborted (user pressed Stop)."
            http_timeout = float(args.get("timeout", 5))
            if http_timeout > 30:
                http_timeout = 30
            try:
                with urllib.request.urlopen(req, timeout=http_timeout, context=ssl_ctx) as resp:
                    text = resp.read().decode("utf-8", errors="replace")
                    if len(text) > 10000:
                        text = text[:10000] + "\n...(truncated)"
                    # Abort check after the blocking read — if the user stopped
                    # mid-request, surface that instead of the (now-stale) body.
                    if abort_evt is not None and abort_evt.is_set():
                        return "⏹ HTTP aborted (user pressed Stop)."
                    return f"HTTP {resp.status}: {text}"
            except urllib.error.HTTPError as he:
                body_text = he.read().decode("utf-8", errors="replace")[:5000]
                return f"HTTP {he.code}: {body_text}"
            except urllib.error.URLError as ue:
                return f"HTTP error: {ue.reason}"
            except (socket.timeout, TimeoutError):
                return f"HTTP error: request timed out ({http_timeout:g}s)"
            except Exception as e:
                return f"HTTP error: {e}"

        elif name == "rag_index":
            import rag
            raw_path = _get_path_arg(args)
            if not raw_path:
                return "Error: missing required argument 'path'"
            path = Path(raw_path).expanduser()
            if path.is_dir():
                results = rag.index_directory(str(path))
                indexed = sum(1 for r in results if r["status"] == "indexed")
                total_chunks = sum(r["chunks"] for r in results)
                return f"Indexed {indexed} files, {total_chunks} chunks total"
            else:
                result = rag.index_file(str(path))
                return f"{result['path']}: {result['status']} ({result['chunks']} chunks)"

        elif name == "rag_search":
            import rag
            results = rag.search(args["query"], limit=args.get("limit", 5))
            if not results:
                return "No results found. Try indexing files first with rag_index."
            lines = []
            for r in results:
                lines.append(f"[{r['file_path']}] (score: {r['score']})")
                lines.append(r["text"][:500])
                lines.append("")
            return "\n".join(lines)

        elif name == "rag_status":
            import rag
            s = rag.get_status()
            return f"RAG index: {s['files']} files, {s['chunks']} chunks"

        else:
            # Try skills
            import skills
            result = skills.execute(name, args)
            if result is not None:
                return result
            return f"Unknown tool: {name}"

    except subprocess.TimeoutExpired:
        cmd = args.get('command', '?')
        _log.error(f"shell timeout: {cmd[:100]}")
        # Help the model understand what happened
        hint = ""
        if any(srv in cmd for srv in ['uvicorn', 'flask', 'gunicorn', 'npm start', 'node ', 'python -m http']):
            hint = " This looks like a server/daemon — it blocks forever. Use spawn_task instead of shell for long-running processes."
        return f"Error: command timed out after {args.get('timeout', 120)}s.{hint} Do NOT retry the same command."
    except Exception as e:
        _log.error(f"tool {name} exception: {e}", exc_info=True)
        # Sanitize error message — don't leak full paths or internals
        err_msg = str(e).replace(str(Path.home()), "~")
        return f"Error: {type(e).__name__}: {err_msg}"


def get_all_tools(compact: bool = False) -> list[dict]:
    """Get core tools + activated extra tools (from tool_search).

    Only core tools are always sent. Extended tools appear after tool_search activates them.
    Fewer tools = better tool-calling accuracy on local models.
    """
    all_available = _get_all_tools_full()

    # Check if hidden skills are active — their tools bypass tool_search
    global _spicy_duck_on
    if _spicy_duck_on is None:
        import db as _db
        _spicy_duck_on = _db.kv_get("spicy_duck") == "quack"
    _always_on = set()
    if _spicy_duck_on:
        _always_on.update({"lovense_connect", "lovense_vibrate", "lovense_pattern",
                           "lovense_preset", "lovense_stop", "lovense_status"})

    # Filter: core tools + always-on (hidden skills) + any activated by tool_search
    result = []
    for t in all_available:
        name = t["function"]["name"]
        if name in _CORE_TOOL_NAMES or name in _always_on or name in _active_extra_tools:
            result.append(t)

    return result
