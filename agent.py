"""Core agent loop — the brain of qwe-qwe."""

import json
import re
import time
import threading
import warnings
import base64
import io
from rich.console import Console
import config
import db
import tools
import memory
import soul
import providers
import threads
import logger
from turn_context import TurnContext, get_current as _get_ctx, set_current as _set_ctx, reset as _reset_ctx

_log = logger.get("agent")
_raw_console = Console(highlight=False, force_terminal=False)

class _SafeConsole:
    """Console wrapper that never crashes on encoding errors (cp1251 on Windows)."""
    def print(self, *args, **kwargs):
        try:
            _raw_console.print(*args, **kwargs)
        except (UnicodeEncodeError, UnicodeDecodeError):
            # Fallback: strip emoji and retry
            try:
                text = " ".join(str(a) for a in args)
                text = text.encode("ascii", "replace").decode("ascii")
                _raw_console.print(text, **kwargs)
            except Exception:
                pass
        except Exception:
            pass

_console = _SafeConsole()
_compaction_lock = threading.Lock()  # protects message read/delete during compaction


# ── Per-turn callbacks ──
#
# Historical note: these used to be module-level globals (``_status_callback``
# etc.), which meant two concurrent turns (web + telegram) stomped each other's
# callbacks — messages from turn A got routed into client B's queue. Per-turn
# state now lives on :class:`turn_context.TurnContext` and is read via a
# ``contextvars.ContextVar`` so emit helpers don't need the ctx threaded
# through every call.
#
# The old module-level names are kept as a **deprecation shim** (see
# ``__getattr__`` / ``__setattr__`` at the bottom of this module) so callers
# that haven't migrated (CLI stub, old snippets) still work — but a
# ``DeprecationWarning`` fires the first time each name is written.


def _emit_status(text: str):
    """Emit a status update to the active turn's client (if any)."""
    _get_ctx().emit_status(text)


def _emit_thinking(text: str):
    """Emit a thinking chunk to the active turn's client (if any)."""
    _get_ctx().emit_thinking(text)


def _emit_content(text: str):
    """Emit a content (reply) chunk to the active turn's client (if any)."""
    _get_ctx().emit_content(text)


def _emit_tool_call(name: str, args_preview: str, result_preview: str = ""):
    """Emit a tool call event to the active turn's client (if any)."""
    _get_ctx().emit_tool_call(name, args_preview, result_preview)


def _emit_recall(memories: list[dict]):
    """Emit recalled memories to the active turn's client (if any).

    UI uses this to render the real 'Recalled memories' panel — the items
    the agent actually saw, not a speculative knowledge-base search.
    """
    _get_ctx().emit_recall(memories)


def _resize_image_b64(b64: str, max_area: int = 49152, quality: int = 80) -> str:
    """Resize image to fit within *max_area* pixels and re-encode as JPEG."""
    try:
        from PIL import Image
        import math
        raw = base64.b64decode(b64)
        img = Image.open(io.BytesIO(raw))
        w, h = img.size
        if w * h > max_area:
            ratio = math.sqrt(max_area / (w * h))
            img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        result = base64.b64encode(buf.getvalue()).decode()
        _log.info(f"image resized: {w}x{h} → {img.size[0]}x{img.size[1]}, "
                  f"{len(raw)//1024}KB → {buf.tell()//1024}KB")
        return result
    except ImportError:
        _log.warning("Pillow not installed — sending image as-is")
        return b64
    except Exception as e:
        _log.warning(f"image resize failed: {e} — sending as-is")
        return b64
# Provider-level capability cache. Deliberately module-scoped: a 400 on
# ``response_format`` is a fact about the provider, not about a turn.
_structured_output_failed: set[str] = set()

# ── Deprecation shim for the retired module globals ──
#
# ``_abort_event``, ``_pending_image_path``, ``_pending_file`` and the
# ``_*_callback`` slots all moved onto :class:`turn_context.TurnContext`. We
# keep the names importable so old code (``agent._pending_image_path = ...``)
# still works, but the first write to each slot logs a ``DeprecationWarning``
# and is mirrored into a module-owned fallback :class:`TurnContext` that's
# used when ``agent.run()`` is called without an explicit ``ctx=``.
_legacy_ctx = TurnContext(source="legacy-shim")
_abort_event = _legacy_ctx.abort_event  # retained for /api/abort REST endpoint

_DEPRECATED_SLOTS = {
    "_status_callback": "on_status",
    "_thinking_callback": "on_thinking",
    "_content_callback": "on_content",
    "_tool_call_callback": "on_tool_call",
    "_recall_callback": "on_recall",
    "_pending_image_path": "image_path",
    "_pending_file": "file_meta",
}

# Remember which slots we've already warned about so migrating-but-noisy
# callers (server.py's legacy path) don't spam the log on every request.
_deprecation_warned: set[str] = set()


def _warn_deprecated_slot(name: str) -> None:
    if name in _deprecation_warned:
        return
    _deprecation_warned.add(name)
    field = _DEPRECATED_SLOTS.get(name, "")
    msg = (
        f"agent.{name} is deprecated — use TurnContext.{field} (pass ctx=... to agent.run). "
        "This shim will keep working, but new code should not rely on it."
    )
    warnings.warn(msg, DeprecationWarning, stacklevel=3)
    _log.warning(msg)


def __getattr__(name: str):
    """Module-level getattr — forwards deprecated slots to ``_legacy_ctx``.

    Only called when the attribute is otherwise unset on the module (PEP 562).
    Callers that *write* to the slot (``agent._content_callback = fn``) bypass
    this — the write sets a real module attribute. :func:`_harvest_legacy_slots`
    picks those back up at ``run()`` time and copies them onto the active ctx.
    """
    if name in _DEPRECATED_SLOTS:
        field = _DEPRECATED_SLOTS[name]
        return getattr(_legacy_ctx, field)
    raise AttributeError(f"module 'agent' has no attribute {name!r}")


def _harvest_legacy_slots(ctx: TurnContext) -> None:
    """Move values set via the legacy module-global API onto *ctx*.

    Old callers wrote ``agent._content_callback = fn`` before calling
    ``agent.run()``. After the refactor, we read callbacks off the active
    ``TurnContext`` — so at the top of every turn we check whether any of
    the old slots have been assigned and, if so, copy them onto the ctx
    (emitting a one-shot ``DeprecationWarning`` the first time each name
    is observed).
    """
    import sys
    mod = sys.modules[__name__]
    mod_dict = mod.__dict__
    for legacy_name, ctx_field in _DEPRECATED_SLOTS.items():
        if legacy_name in mod_dict:
            value = mod_dict[legacy_name]
            _warn_deprecated_slot(legacy_name)
            # Only overwrite the ctx field if the caller didn't set one
            # explicitly — explicit ctx wins over the global shim.
            if getattr(ctx, ctx_field) is None:
                setattr(ctx, ctx_field, value)


def _repair_json(raw: str) -> dict:
    """Attempt to repair malformed JSON from small models (Qwen, etc.).

    Common issues: trailing commas, single quotes, unclosed brackets,
    comments, raw newlines in strings, BOM characters.
    Returns parsed dict or {} if repair fails.
    """
    if not raw or not raw.strip():
        return {}

    s = raw.strip()

    # Remove BOM and zero-width chars
    s = s.lstrip("\ufeff\u200b\u200c\u200d")

    # Remove JS-style comments: // ... and /* ... */
    s = re.sub(r"//[^\n]*", "", s)
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.DOTALL)

    # Replace single quotes with double quotes (outside of double-quoted strings)
    # Simple heuristic: if no double quotes at all, swap single→double
    if '"' not in s and "'" in s:
        s = s.replace("'", '"')

    # Fix raw newlines/tabs inside string values → escape them
    # Match content between quotes and escape control chars
    def _escape_controls(m: re.Match) -> str:
        inner = m.group(1)
        inner = inner.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
        return f'"{inner}"'
    s = re.sub(r'"((?:[^"\\]|\\.)*?(?:\n|\r|\t)(?:[^"\\]|\\.)*?)"', _escape_controls, s)

    # Remove trailing commas before } or ]
    s = re.sub(r",\s*([}\]])", r"\1", s)

    # Try parsing after basic fixes
    try:
        return json.loads(s)
    except Exception:
        pass

    # Close unclosed string FIRST — if we close braces/brackets before the
    # string, the added } or ] land INSIDE the incomplete string instead of
    # after it, corrupting the structure. Example: `{"command": "ls -la`
    # must become `{"command": "ls -la"}`, not `{"command": "ls -la}"`.
    quote_count = len(re.findall(r'(?<!\\)"', s))
    if quote_count % 2 == 1:
        s += '"'

    # Now close any remaining unclosed brackets/braces. Smarter than a plain
    # count: we do a positional scan-and-insert so a premature `}` before a
    # pending `[` (e.g. `{"items": [1, 2, 3}`) gets a `]` inserted BEFORE the
    # `}` rather than appended at the end, which still wouldn't parse.
    out: list[str] = []
    stack: list[str] = []
    in_string = False
    escape = False
    for ch in s:
        if in_string:
            out.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            continue
        if ch in "{[":
            stack.append("}" if ch == "{" else "]")
            out.append(ch)
            continue
        if ch in "}]":
            # Close any mismatched openers underneath first.
            expected = stack[-1] if stack else None
            if expected and expected != ch:
                # Premature close — emit the pending closer first.
                out.append(stack.pop())
            if stack and stack[-1] == ch:
                stack.pop()
            out.append(ch)
            continue
        out.append(ch)
    # Append any openers still unmatched at end of input.
    out.extend(reversed(stack))
    s = "".join(out)

    try:
        return json.loads(s)
    except Exception:
        _log.warning(f"json repair failed: {raw[:200]}")
        return {}


def _repair_tool_json(raw: str) -> str | None:
    """Aggressive JSON repair for small model tool call outputs.

    Unlike _repair_json (which returns a dict), this returns the repaired
    JSON *string* so the caller can json.loads() it explicitly.
    Handles markdown fences, leading text, trailing commas, single quotes.
    """
    if not raw or not raw.strip():
        return None
    s = raw.strip()
    # Strip markdown fences
    if s.startswith("```"):
        s = re.sub(r'^```\w*\n?', '', s)
        s = re.sub(r'\n?```$', '', s)
        s = s.strip()
    # Strip leading text before first {
    idx = s.find('{')
    if idx < 0:
        return None
    if idx > 0:
        s = s[idx:]
    # Find matching closing brace
    depth = 0
    end = -1
    for i, c in enumerate(s):
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                end = i
                break
    if end > 0:
        s = s[:end + 1]
    # Fix trailing commas
    s = re.sub(r',\s*([}\]])', r'\1', s)
    # Fix single quotes to double quotes (only if no double quotes in values)
    if "'" in s and '"' not in s:
        s = s.replace("'", '"')
    try:
        json.loads(s)
        return s
    except json.JSONDecodeError:
        return None


def _json_format_extra() -> dict:
    """Return response_format kwarg if provider supports structured output."""
    provider = providers.get_active_name()
    if provider in _structured_output_failed:
        return {}
    if providers.supports("supports_response_format"):
        return {"response_format": {"type": "json_object"}}
    return {}


def _mark_structured_failed(error: Exception):
    """Cache structured output failure per-provider to avoid repeated 400s."""
    err_str = str(error)
    if "400" in err_str or "response_format" in err_str.lower():
        provider = providers.get_active_name()
        _structured_output_failed.add(provider)
        _log.info(f"structured output disabled for provider '{provider}' (returned 400)")


def _get_tool_schema(tool_name: str) -> dict | None:
    """Get the JSON schema for a tool by name."""
    for t in tools.TOOLS:
        if t["function"]["name"] == tool_name:
            return t["function"].get("parameters", {})
    return None


def _retry_tool_call(client, model: str, tool_name: str,
                     raw_args: str, max_retries: int = 3) -> dict | None:
    """Retry broken tool call JSON with progressively clearer prompts.

    Attempt 1: _repair_json() — already done by caller.
    Attempt 2: Ask model to reformat with schema hint.
    Attempt 3: Minimal prompt — "just give me the JSON".
    Returns parsed args dict or None if all retries fail.
    """
    schema = _get_tool_schema(tool_name)
    required = schema.get("required", []) if schema else []
    props = schema.get("properties", {}) if schema else {}
    schema_hint = ", ".join(f'{k}: {v.get("type", "string")}' for k, v in props.items())

    # Attempt 2: ask model to reformat (with structured output if available)
    retry_msgs = [
        {"role": "system", "content": "You fix broken JSON. Reply with ONLY valid JSON, nothing else."},
        {"role": "user", "content": (
            f"This JSON for tool '{tool_name}' is broken:\n{raw_args[:500]}\n\n"
            f"Required params: {schema_hint}\n"
            f"Reply with corrected JSON object only."
        )},
    ]
    for attempt_extra in [_json_format_extra(), {}]:  # try with structured output, fallback without
        try:
            resp = client.chat.completions.create(
                model=model, messages=retry_msgs,
                temperature=0.1, max_tokens=256, stream=False,
                **attempt_extra,
            )
            text = (resp.choices[0].message.content or "").strip()
            m = re.search(r'\{.*\}', text, re.DOTALL)
            if m:
                result = json.loads(m.group())
                _log.info(f"retry attempt 2 succeeded for {tool_name}")
                return result
            break  # parsed but no JSON found, move to attempt 3
        except Exception as e:
            if attempt_extra:  # structured output failed, try without
                _mark_structured_failed(e)
                _log.warning(f"retry attempt 2 (structured) failed: {e}, falling back")
                continue
            _log.warning(f"retry attempt 2 failed: {e}")

    # Attempt 3: minimal prompt
    params_desc = ", ".join(f'"{k}"' for k in required)
    minimal_msgs = [
        {"role": "user", "content": (
            f'Generate JSON for {tool_name}. Keys: {params_desc}. '
            f'Original (broken): {raw_args[:300]}'
        )},
    ]
    for attempt_extra in [_json_format_extra(), {}]:
        try:
            resp = client.chat.completions.create(
                model=model, messages=minimal_msgs,
                temperature=0.0, max_tokens=256, stream=False,
                **attempt_extra,
            )
            text = (resp.choices[0].message.content or "").strip()
            m = re.search(r'\{.*\}', text, re.DOTALL)
            if m:
                result = json.loads(m.group())
                _log.info(f"retry attempt 3 succeeded for {tool_name}")
                return result
            break
        except Exception as e:
            if attempt_extra:
                _mark_structured_failed(e)
                continue
            _log.warning(f"retry attempt 3 failed: {e}")

    _log.error(f"all retry attempts failed for {tool_name}")
    return None


# Tools where self-check is applied before execution
_SELF_CHECK_TOOLS = {"shell", "write_file"}

# Critical tool patterns for consensus self-verification (safety check)
_CRITICAL_TOOL_PATTERNS = {
    "shell": re.compile(
        r'\b(rm\s|mv\s|chmod\s|kill\s|pip\s+uninstall|git\s+reset|git\s+push'
        r'|DROP\s|DELETE\s+FROM|TRUNCATE)\b', re.IGNORECASE),
    "write_file": re.compile(
        r'(system32|/etc/|/usr/|\.env|\.ssh|\.git/|credentials|passwd)',
        re.IGNORECASE),  # only verify writes to sensitive paths
    "secret_delete": None, # always critical
}


def _needs_self_check(tool_name: str, args: dict) -> bool:
    """Check if a tool call should be self-verified for safety."""
    if tool_name not in _CRITICAL_TOOL_PATTERNS:
        return False
    pattern = _CRITICAL_TOOL_PATTERNS[tool_name]
    if pattern is None:
        return True  # always check
    # Pick the relevant text to check against pattern
    if tool_name == "write_file":
        text = args.get("path", "")
    else:
        text = args.get("command", "")
    return bool(pattern.search(text))


def _self_verify(client, model: str, tool_name: str, args: dict,
                 user_request: str) -> tuple[bool, str]:
    """Quick safety verification: is this tool call correct for the user's request?

    Returns (approved: bool, reason: str). Fails open on errors.
    """
    args_str = json.dumps(args, ensure_ascii=False)[:300]
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": (
                    "You are a safety checker. The user asked something and an AI wants to run a tool.\n"
                    "Is this tool call correct and safe for the user's request?\n"
                    "Reply ONLY: APPROVE or REJECT: <reason>"
                )},
                {"role": "user", "content": (
                    f"User request: {user_request[:200]}\n"
                    f"Tool: {tool_name}({args_str})"
                )},
            ],
            temperature=0.0,
            max_tokens=50,
            stream=False,
        )
        answer = _strip_thinking(resp.choices[0].message.content or "").strip()
        if answer.upper().startswith("APPROVE"):
            return True, "approved"
        elif answer.upper().startswith("REJECT"):
            return False, answer
        else:
            return True, "unclear response, allowing"  # fail open
    except Exception as e:
        _log.warning(f"self-verify failed: {e}")
        return True, f"check failed: {e}"  # fail open on errors


def _self_check_tool_call(client, model: str, tool_name: str,
                          args: dict) -> tuple[bool, dict | None]:
    """Ask model to validate tool arguments before execution.

    Returns (is_ok, corrected_args). If is_ok=True, args are fine.
    If is_ok=False and corrected_args is not None, use corrected version.
    """
    try:
        args_json = json.dumps(args, ensure_ascii=False)
        schema = _get_tool_schema(tool_name)
        required = schema.get("required", []) if schema else []

        use_structured = bool(_json_format_extra())
        if use_structured:
            system_msg = (
                'Check this tool call. Reply as JSON: {"status": "ok"} if correct, '
                'or {"status": "fix", "args": {corrected args}} if wrong.'
            )
        else:
            system_msg = (
                "Check this tool call. If arguments are correct, reply ONLY 'OK'. "
                "If wrong, reply with corrected JSON only."
            )

        check_msgs = [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": (
                f"Tool: {tool_name}\n"
                f"Required: {required}\n"
                f"Arguments: {args_json}"
            )},
        ]

        for attempt_extra in [_json_format_extra(), {}]:
            try:
                resp = client.chat.completions.create(
                    model=model, messages=check_msgs,
                    temperature=0.1, max_tokens=256, stream=False,
                    **attempt_extra,
                )
                text = _strip_thinking(resp.choices[0].message.content or "").strip()
                break
            except Exception as e:
                if attempt_extra:
                    _mark_structured_failed(e)
                    _log.warning(f"self-check (structured) failed: {e}, falling back")
                    continue
                raise

        # Parse response
        if use_structured:
            try:
                parsed = json.loads(text)
                if parsed.get("status") == "ok":
                    return True, None
                if parsed.get("status") == "fix" and parsed.get("args"):
                    fixed = parsed["args"]
                    # Validate: corrected args must have required fields
                    if required and not all(k in fixed for k in required):
                        _log.warning(f"self-check correction missing required fields, ignoring")
                        return True, None
                    _log.info(f"self-check corrected {tool_name}: {args} → {fixed}")
                    return False, fixed
            except json.JSONDecodeError:
                pass  # fall through to text parsing

        if text.upper().startswith("OK"):
            return True, None

        m = re.search(r'\{.*\}', text, re.DOTALL)
        if m:
            corrected = json.loads(m.group())
            # Validate: corrected args must have required fields
            if required and not all(k in corrected for k in required):
                _log.warning(f"self-check correction missing required fields, ignoring")
                return True, None
            _log.info(f"self-check corrected {tool_name}: {args} → {corrected}")
            return False, corrected

        return True, None
    except Exception as e:
        _log.warning(f"self-check failed for {tool_name}: {e}")
        return True, None


from utils import strip_thinking as _strip_thinking


def _clean_response(text: str) -> str:
    """Post-process LLM response: remove ChatGPT-isms, excess formatting."""
    # Strip markdown headers (## / ### / ####) — not appropriate for chat
    text = re.sub(r'^#{1,4}\s+.*$', lambda m: m.group(0).lstrip('#').strip(), text, flags=re.MULTILINE)

    # Strip markdown tables (lines with |---|)
    lines = text.split('\n')
    cleaned = []
    skip_table = False
    for line in lines:
        stripped = line.strip()
        # Detect table separator
        if re.match(r'^\|[-\s|:]+\|$', stripped):
            skip_table = True
            continue
        # Table rows
        if skip_table and stripped.startswith('|') and stripped.endswith('|'):
            # Convert table row to bullet point
            cells = [c.strip() for c in stripped.strip('|').split('|') if c.strip()]
            if cells:
                cleaned.append('- ' + ' | '.join(cells))
            continue
        # First table header row (before separator)
        if stripped.startswith('|') and stripped.endswith('|') and not skip_table:
            continue  # skip header, separator will trigger conversion
        skip_table = False
        cleaned.append(line)
    text = '\n'.join(cleaned)

    # Remove trailing "Want more?" / "Need anything else?" patterns (RU/EN)
    text = re.sub(
        r'\n+(?:Хочешь|Нужно|Скажи|Если нужно|Что именно|Могу ещё|Давай|Подсказать)[\s\S]{0,100}[?!😊😄🤔]\s*$',
        '', text
    )

    # Remove "Option N:" / "Variant N:" sections if more than 1
    variant_count = len(re.findall(r'(?:Вариант|Variant|Option)\s*\d', text))
    if variant_count > 1:
        # Keep only first variant
        parts = re.split(r'\n+(?:Вариант|Variant|Option)\s*\d[:\.]?\s*', text)
        if len(parts) >= 2:
            text = parts[0] + parts[1]

    # Collapse multiple blank lines
    text = re.sub(r'\n{3,}', '\n\n', text)

    return text.strip()


from utils import extract_thinking as _extract_thinking


def _summarize_tool_output(tool_name: str, output: str, max_chars: int) -> str:
    """Summarize large tool output to fit context budget.

    For structured data (JSON, tables), extract key info.
    For text, keep first and last parts with a summary marker.
    """
    # JSON output — extract structure, drop bulk data
    if output.lstrip()[:1] in ("{", "["):
        try:
            data = json.loads(output)
            if isinstance(data, list) and len(data) > 5:
                preview = json.dumps(data[:3], ensure_ascii=False, indent=1)
                result = f"{preview}\n\n[... {len(data)} total items, showing first 3]"
                if len(result) > max_chars:
                    result = result[:max_chars] + "\n[... capped]"
                return result
            elif isinstance(data, dict) and len(output) > max_chars:
                keys = list(data.keys())[:20]
                return f"Keys: {keys}\nFirst values preview:\n{output[:max_chars // 2]}..."
        except Exception:
            pass

    # Line-based output (ls, grep, logs) — keep head + tail, cap to max_chars
    lines = output.split("\n")
    if len(lines) > 30:
        head = "\n".join(lines[:15])
        tail = "\n".join(lines[-10:])
        result = f"{head}\n\n[... {len(lines)} lines total, {len(lines) - 25} omitted ...]\n\n{tail}"
        if len(result) > max_chars:
            result = result[:max_chars] + "\n[... capped]"
        return result

    # Default: head truncation with marker
    if len(output) > max_chars:
        return output[:max_chars] + f"\n[... truncated, {len(output)} chars total]"
    return output


# ── Task decomposition for complex requests ──
# Small 9B models choke on multi-step tasks; detect and break them down.

_COMPLEX_MARKERS = [
    (r'\b(?:and|и|а также|потом|затем|после)\b.*\b(?:and|и|а также|потом|затем|после)\b', 2),  # multiple conjunctions
    (r'(?:(?:1\.|2\.|3\.|\*|-)\s+\S+.*\n?){3,}', 3),  # numbered/bulleted list with 3+ items
    (r'\b(?:set up|настрой|создай|сделай)\b.*\b(?:and|и)\b.*\b(?:then|потом|затем)\b', 2),  # setup + chain
]


def _estimate_complexity(user_input: str) -> int:
    """Estimate task complexity. Returns 1 (simple), 2 (moderate), 3 (complex)."""
    score = 1
    for pattern, weight in _COMPLEX_MARKERS:
        if re.search(pattern, user_input, re.IGNORECASE | re.MULTILINE):
            score = max(score, weight)
    # Length heuristic — very long requests are usually complex
    if len(user_input) > 500:
        score = max(score, 2)
    if len(user_input) > 1000:
        score = max(score, 3)
    return score


def _decompose_task(client, model: str, user_input: str) -> list[str] | None:
    """Ask LLM to break a complex task into atomic steps. Returns list of steps or None."""
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": (
                    "Break this task into 2-5 small independent steps. "
                    "Each step should be one clear action. "
                    "Return ONLY a JSON array of strings. Example: [\"step 1\", \"step 2\"]\n"
                    "If the task is already simple, return [\"<original task>\"]"
                )},
                {"role": "user", "content": user_input},
            ],
            temperature=0.3,
            max_tokens=256,
        )
        raw = resp.choices[0].message.content or ""
        # Strip thinking tags
        raw = re.sub(r"<think>.*?</think>\s*", "", raw, flags=re.DOTALL).strip()
        # Extract JSON array
        match = re.search(r'\[.*\]', raw, re.DOTALL)
        if match:
            steps = json.loads(match.group())
            if isinstance(steps, list) and len(steps) >= 2:
                return [str(s) for s in steps]
    except Exception:
        pass
    return None


def _auto_context(user_input: str, thread_id: str | None = None) -> str:
    """Auto-retrieve relevant memories with Qdrant-side score filtering.

    Philosophy: small model, limited context — every injected memory must
    be high-quality. Qdrant filters by score_threshold BEFORE returning,
    so we never waste context budget on low-relevance results.

    Strategy:
    1. Compute embedding once (FastEmbed, local)
    2. Thread-scoped hybrid search (score >= 0.6) — up to 2 results
    3. Global hybrid search (score >= 0.6) — fill remaining slots
    4. Experience search (score >= 0.65) — only proven patterns
    5. Deduplicate by content across all results
    """
    # IMPORTANT — auto-recall must use DENSE-ONLY search, not hybrid RRF.
    # Qdrant's RRF-fused scores are normalized (top result = 1.0) and don't
    # correspond to absolute semantic similarity. A threshold of 0.6 on
    # RRF scores means "top 40% by rank", not "≥0.6 cosine similar" — so
    # unrelated memories (Russian travel notes for a Russian programming
    # question) sail through. We bypass hybrid by calling search_by_vector
    # WITHOUT query_text, which drops to pure dense cosine similarity
    # filtering on the 0..1 range below. Hybrid search stays available for
    # the explicit memory_search tool where the agent WANTS keyword+semantic.
    MEMORY_SCORE_MIN = 0.6
    EXPERIENCE_SCORE_MIN = 0.65

    try:
        seen_texts = set()
        lines = ["[Relevant context from memory:]"]
        # Structured recall list for UI streaming (mirrors `lines` 1:1 but with
        # metadata). Each item: {tag, text, score, source}. Emitted via
        # _emit_recall() right before we return so the Inspector panel shows
        # the exact memories the agent is about to see.
        recalled: list[dict] = []

        def _add(r: dict, source: str, tag: str | None = None, extra: str = ""):
            """Append to both the text prompt and the structured recall list."""
            t = tag or r.get("tag", "memory")
            text = r["text"]
            if text in seen_texts:
                return False
            seen_texts.add(text)
            display = f"{text}{extra}" if extra else text
            lines.append(f"- [{t}] {display}")
            recalled.append({
                "tag": t,
                "text": text[:400],  # cap for WS payload
                "score": round(float(r.get("score") or 0), 3),
                "source": source,
            })
            return True

        # Compute embedding once (FastEmbed, no network)
        try:
            vector = memory.embed(user_input)
        except Exception:
            return ""  # embedding unavailable

        # NOTE: every search_by_vector call below deliberately OMITS
        # `query_text=` so we stay on the dense-only path. With query_text
        # set, memory.py routes to hybrid RRF whose fused scores are
        # rank-normalized (top=1.0) and can't be thresholded meaningfully.
        # Dense-only returns raw cosine similarity where 0.6 actually means
        # "semantically close", not "top 40% by rank".

        # Thread-scoped search first (prioritize local context)
        if thread_id:
            thread_results = memory.search_by_vector(
                vector, limit=2, thread_id=thread_id,
                score_threshold=MEMORY_SCORE_MIN,
            )
            for r in thread_results:
                _add(r, source="thread")

        # Wiki/entity search first (synthesized knowledge = higher quality)
        wiki_results = memory.search_by_vector(
            vector, limit=2, tag="wiki",
            score_threshold=MEMORY_SCORE_MIN,
        )
        for r in wiki_results:
            _add(r, source="wiki", tag="wiki")

        # Relation expansion: if entity found, follow links to related wiki
        entity_results = memory.search_by_vector(
            vector, limit=1, tag="entity",
            score_threshold=MEMORY_SCORE_MIN,
        )
        for e in entity_results:
            relations = e.get("relations", [])
            if relations:
                rel_names = ", ".join(f"{r['rel']}→{r['to']}" for r in relations[:5])
                _add(e, source="entity", tag="entity", extra=f" ({rel_names})")

        # Global search (fill remaining slots).
        # Session isolation: only cross-thread SYNTHESIZED knowledge (fact/knowledge/user/project/decision/idea) —
        # NOT raw messages from other threads.
        max_memory = config.get("max_memory_results")
        if (max_memory - (len(lines) - 1)) > 0:
            _CROSS_THREAD_TAGS = ("fact", "knowledge", "user", "project", "decision", "idea")
            for _tag in _CROSS_THREAD_TAGS:
                if len(lines) - 1 >= max_memory:
                    break
                tag_results = memory.search_by_vector(
                    vector, limit=2, tag=_tag,
                    score_threshold=MEMORY_SCORE_MIN,
                )
                for r in tag_results:
                    if len(lines) - 1 >= max_memory:
                        break
                    _add(r, source="cross_thread")

        # Experience cases (higher threshold — only proven patterns)
        if config.get("experience_learning"):
            exp_hits = memory.search_by_vector(
                vector, limit=config.MAX_EXPERIENCE_RESULTS + 1, tag="experience",
                score_threshold=EXPERIENCE_SCORE_MIN,
            )
            exp_lines = []
            for r in exp_hits:
                if len(exp_lines) >= config.MAX_EXPERIENCE_RESULTS:
                    break
                # Composite score: similarity * outcome_weight
                # Failed experiences (outcome_score=0.2) are deprioritized
                effective = r["score"] * r.get("outcome_score", 1.0)
                if effective > 0.4 and r["text"] not in seen_texts:
                    exp_lines.append(f"- {r['text']}")
                    seen_texts.add(r["text"])
                    recalled.append({
                        "tag": "experience",
                        "text": r["text"][:400],
                        "score": round(float(effective), 3),
                        "source": "experience",
                    })
            if exp_lines:
                lines.append("")
                lines.append("[Relevant past experiences:]")
                lines.extend(exp_lines)

        hits = sum(1 for l in lines if l.startswith("- "))
        if hits > 0:
            _log.info(f"auto_context: {hits} items injected (thread={thread_id or 'global'})")
        # Stream the structured list to the UI (no-op if no callback wired)
        _emit_recall(recalled)
        if len(lines) == 1:
            return ""
        return "\n".join(lines)
    except BaseException as e:
        _log.warning(f"auto_context failed ({type(e).__name__}): {e}", exc_info=True)
        return ""


_OUTCOME_WEIGHTS = {"success": 1.0, "partial": 0.6, "failed": 0.2}


def _save_experience(user_input: str, result: "TurnResult", rounds: int,
                     fail_count: int, _sync: bool = False):
    """Save a compact experience case after a tool-using turn (async, non-blocking).

    Skips low-signal turns to keep the experience pool useful:
      - Trivial tasks (single tool round, no real learning).
      - Memory-meta tasks (the whole turn was about the memory system itself —
        saving "I searched memory" as experience is circular noise).
      - Self-config / introspection tasks.
      - Empty replies (nothing to learn from).
    """
    if not config.get("experience_learning"):
        return
    if not result.tool_calls_made:
        return

    # Unique tools used this turn
    tools_used = list(dict.fromkeys(result.tool_calls_made))

    # Skip when the whole turn was meta (memory / self-config / tool_search only)
    _META_TOOLS = {
        "memory_search", "memory_save", "memory_delete",
        "self_config", "tool_search",
        "list_notes", "list_skills", "list_cron", "list_secrets",
        "get_stats", "list_experience",
        "soul_editor", "skill_creator",
        "add_trait", "remove_trait", "list_traits",
        "rag_index", "user_profile_get",
    }
    if all(t in _META_TOOLS for t in tools_used):
        _log.debug(f"experience skipped: meta-only tools ({tools_used})")
        return

    # Skip when user input is about memory itself (poisons the recall pool)
    _low = (user_input or "").strip().lower()
    _MEMORY_KEYWORDS = (
        "память", "memory", "запомни", "remember", "forget", "забудь",
        "очисти", "clean", "clear memory", "удали", "delete memory",
        "что ты помнишь", "what do you remember", "recall",
        "забыл", "забыла", "забудьте", "забываешь",
        "запомнил", "запомните", "вспомни", "вспомнил",
    )
    if any(kw in _low for kw in _MEMORY_KEYWORDS):
        _log.debug(f"experience skipped: memory-meta input ({_low[:40]})")
        return

    # Skip single-round tasks unless they produced a substantive reply
    reply_stripped = result.reply.strip()
    if rounds <= 1 and len(reply_stripped) < 80:
        _log.debug(f"experience skipped: trivial single-round turn (reply={len(reply_stripped)}ch)")
        return

    outcome = "failed" if fail_count >= 2 else "partial" if fail_count > 0 else "success"
    task = user_input.strip().replace("\n", " ")[:80]
    tools_str = ", ".join(tools_used)
    reply_summary = reply_stripped.replace("\n", " ")[:60]

    case_text = (
        f"[EXP] Task: {task} | Tools: {tools_str} | "
        f"Steps: {rounds} | Result: {outcome} | "
        f"Learned: {reply_summary}"
    )

    def _do_save():
        try:
            memory.save(case_text, tag="experience", dedup=True, thread_id=None,
                        meta={"outcome_score": _OUTCOME_WEIGHTS.get(outcome, 0.5)})
            _log.info(f"experience saved: {outcome} | tools={tools_str}")
        except Exception as e:
            _log.warning(f"experience save failed: {e}")

    if _sync:
        _do_save()
    else:
        threading.Thread(target=_do_save, daemon=True).start()


def _build_messages(user_input: str, thread_id: str | None = None,
                    source: str = "cli", image_b64: str | None = None) -> list[dict]:
    """Build minimal context: soul + auto-context + recent history + user message."""
    # Soul → compact system prompt
    agent_soul = soul.load()
    system_text = soul.to_prompt(agent_soul)

    # Inject user profile from DB (~50 tokens)
    profile = db.kv_get_prefix("user:")
    if profile:
        profile_str = ", ".join(f"{k.replace('user:', '')}={v}" for k, v in sorted(profile.items()))
        if len(profile_str) > 200:
            profile_str = profile_str[:200] + "..."
        system_text += f"\nUser: {profile_str}"

    # Add source context
    if source == "telegram":
        system_text += "\nYou are chatting via Telegram. Your replies are sent directly as Telegram messages. You CAN send messages — just reply normally."
    elif source == "web":
        system_text += "\nYou are chatting via the web UI."

    # Thinking mode — inject prompt instruction for all models (default enabled)
    _thinking_raw = db.kv_get("thinking_enabled")
    thinking_on = _thinking_raw != "false"  # default True when unset
    _model_lower = providers.get_model().lower()
    _is_qwen = "qwen" in _model_lower or "qw" in _model_lower
    if thinking_on:
        system_text += (
            "\n\nIMPORTANT: Before answering, think through the problem step by step. "
            "Write your reasoning inside <think>...</think> tags. "
            "After thinking, write your final answer outside the tags. "
            "Example:\n<think>\nLet me analyze this...\n</think>\nHere is my answer."
        )
    elif _is_qwen:
        # Qwen3: disable thinking mode to prevent empty responses and improve tool calling
        system_text += "\n\n/no_think"

    # Progressive context injection: skip memory for trivial queries
    # (saves ~200ms embedding + Qdrant latency + context tokens)
    query_lower = (user_input or "").strip().lower().rstrip("!?.,")
    if query_lower not in TRIVIAL_QUERIES:
        context = _auto_context(user_input, thread_id=thread_id)
        if context:
            system_text += "\n\n" + context

    msgs = [{"role": "system", "content": system_text}]

    # Heartbeat: skip chat history, only system + user profile + memories
    if source == "heartbeat":
        msgs.append({"role": "user", "content": user_input})
        return msgs

    # Recent history from SQLite (lock prevents race with background compaction)
    with _compaction_lock:
        history = db.get_recent_messages(thread_id=thread_id)

    # Ensure history starts with user (not assistant) after system
    while history and history[0]["role"] != "user":
        history.pop(0)

    # Remove trailing user messages (we'll add the new one)
    while history and history[-1]["role"] == "user":
        history.pop()

    # Strip meta / extra fields — LLM APIs reject unknown properties
    _ALLOWED_MSG_KEYS = {"role", "content", "tool_calls", "tool_call_id", "name"}
    for m in history:
        msgs.append({k: v for k, v in m.items() if k in _ALLOWED_MSG_KEYS})

    # New user message — multimodal if image provided
    if image_b64:
        image_b64 = _resize_image_b64(image_b64)
        user_content = [
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
            {"type": "text", "text": user_input or "What's in this image?"},
        ]
        msgs.append({"role": "user", "content": user_content})
    else:
        msgs.append({"role": "user", "content": user_input})
    return msgs


class TurnResult:
    """Result of one agent turn with debug info."""
    __slots__ = ("reply", "thinking", "prompt_tokens", "completion_tokens", "total_tokens",
                 "tool_calls_made", "model", "auto_context_hits", "json_repairs",
                 "retry_successes", "self_check_fixes", "self_check_rejections",
                 "tok_per_sec")

    def __init__(self):
        self.reply = ""
        self.thinking = ""
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.total_tokens = 0
        self.tok_per_sec = 0.0
        self.tool_calls_made: list[str] = []
        self.model = providers.get_model()
        self.auto_context_hits = 0
        self.json_repairs = 0
        self.retry_successes = 0
        self.self_check_fixes = 0
        self.self_check_rejections = 0


def _estimate_tokens(messages: list[dict]) -> int:
    """Estimate token count for a list of messages (rough: 1 token ≈ 4 chars)."""
    total = 0
    for m in messages:
        content = m.get("content") or ""
        # Handle multimodal content (list of {type, text/image_url})
        if isinstance(content, list):
            content_len = sum(len(p.get("text", "")) for p in content if isinstance(p, dict))
            content_len += sum(250 for p in content if isinstance(p, dict) and p.get("type") == "image_url")
            total += content_len // 4 + 4
        else:
            total += len(content) // 4 + 4  # +4 for role/metadata overhead
        # Tool calls add extra tokens
        if m.get("tool_calls"):
            tc = m["tool_calls"]
            if isinstance(tc, str):
                total += len(tc) // 4
            elif isinstance(tc, list):
                for t in tc:
                    total += 50  # overhead per tool call
                    if isinstance(t, dict):
                        args = t.get("function", {}).get("arguments", "")
                        total += len(str(args)) // 4
    return total


# ── Compaction state (for notifications) ──
# Single-slot callback — matches how other callbacks in this module are wired
# (_status_callback, _content_callback, etc). Previously this was an
# append-only list; callers that re-registered (e.g. hot reload) leaked.
_compaction_callback = None


def on_compaction(callback):
    """Register a callback for compaction events: callback(event, data).

    Events: 'start', 'summary', 'done', 'skip', 'error'.

    Only one callback is kept — later calls replace the previous one. Pass
    ``None`` to unregister.
    """
    global _compaction_callback
    _compaction_callback = callback


def _notify_compaction(event: str, data: dict):
    """Notify the registered callback (if any) about compaction events."""
    cb = _compaction_callback
    if cb is None:
        return
    try:
        cb(event, data)
    except Exception as e:
        _log.warning(f"compaction callback error: {e}")


# Token budget settings
SYSTEM_RESERVE = 3500      # system prompt (~1500 tokens) + tool schemas + auto-context
RECENT_RESERVE = 2         # always keep last N user+assistant pairs
TOOL_OUTPUT_SUMMARIZE_THRESHOLD = 2000  # chars — above this, auto-summarize tool output
TRIVIAL_QUERIES = {"привет", "hello", "hi", "хай", "здравствуй", "ку", "hey", "yo",
                   "ok", "ок", "ага", "угу", "да", "нет", "пока", "спасибо", "thanks", "thx",
                   "okay", "sure", "nope", "yep", "yup", "bye", "пасиб", "ладно"}


def _maybe_compact(thread_id: str | None = None):
    """Smart compaction: token-aware, summarizes to memory, notifies."""
    all_msgs = db.get_recent_messages(limit=200, thread_id=thread_id)
    total_tokens = _estimate_tokens(all_msgs)

    # Check if we need compaction (token-based OR message count)
    msg_count = len(all_msgs)
    needs_compact = (
        total_tokens > config.get("context_budget") - SYSTEM_RESERVE or
        msg_count > config.get("compaction_threshold")
    )

    if not needs_compact:
        return

    _log.info(f"compaction triggered: {msg_count} msgs, ~{total_tokens} tokens (budget: {config.get('context_budget')})")

    # Keep recent messages (last N pairs)
    keep_count = RECENT_RESERVE * 2  # user + assistant pairs
    if len(all_msgs) <= keep_count + 2:
        return  # not enough to compact

    # Split: old messages to compact, recent to keep
    # all_msgs is already in chronological order
    to_compact_msgs = all_msgs[:len(all_msgs) - keep_count]

    if len(to_compact_msgs) < 3:
        return

    # Get DB IDs for the old messages
    oldest = db.get_oldest_messages(len(to_compact_msgs), thread_id=thread_id)
    if not oldest:
        return

    compact_tokens = _estimate_tokens(to_compact_msgs)
    _log.info(f"compacting {len(to_compact_msgs)} messages (~{compact_tokens} tokens)")

    # Notify: compaction starting
    _notify_compaction("start", {
        "thread_id": thread_id,
        "messages": len(to_compact_msgs),
        "tokens": compact_tokens,
    })

    # Build conversation for summarization (truncate very long messages)
    convo_lines = []
    for m in to_compact_msgs:
        content = m.get("content") or ""
        if not content:
            continue
        role = m["role"]
        # Truncate long tool outputs
        if role == "tool":
            content = content[:500] + ("..." if len(content) > 500 else "")
        elif len(content) > 1000:
            content = content[:1000] + "..."
        convo_lines.append(f"{role}: {content}")

    convo = "\n".join(convo_lines)

    # Summarize via LLM (use background thread via tasks module)
    import threading

    def _do_compact():
        try:
            providers.ensure_model_loaded()
            client = providers.get_client()
            resp = client.chat.completions.create(
                model=providers.get_model(),
                messages=[
                    {"role": "system", "content": (
                        "Compress this conversation into a structured summary for context recovery. "
                        "Use EXACTLY these sections (skip empty ones):\n\n"
                        "## Current State\nWhat is the current state of the task/project?\n\n"
                        "## Goals & Intent\nWhat is the user trying to accomplish?\n\n"
                        "## Recent Changes\nWhat was modified, created, or deleted?\n\n"
                        "## Key Decisions\nImportant choices made and why.\n\n"
                        "## Active Work\nWhat was in progress when the conversation was cut?\n\n"
                        "## Key Files\nFile paths, configs, URLs that matter.\n\n"
                        "## Learnings\nWhat worked, what failed, errors encountered.\n\n"
                        "## User Preferences\nDiscovered preferences, names, settings.\n\n"
                        "## Next Steps\nWhat should happen next?\n\n"
                        "Be concise — max 300 words total. "
                        "If nothing worth saving — reply SKIP."
                    )},
                    {"role": "user", "content": convo[:8000]},  # cap input
                ],
                temperature=0.3,
                max_tokens=1024,
            )
            summary = _strip_thinking(resp.choices[0].message.content or "")

            if summary and not summary.strip().upper().startswith("SKIP"):
                memory.save(summary, tag="compaction", thread_id=thread_id)
                _log.info(f"compaction: saved summary ({len(summary)} chars)")
                # Also inject summary as first user message so model sees it in-context
                db.save_message("user",
                    f"[Context from earlier conversation — auto-compacted]\n{summary}",
                    thread_id=thread_id)
                _notify_compaction("summary", {
                    "thread_id": thread_id,
                    "summary": summary[:300],
                    "saved_tokens": compact_tokens,
                })
            else:
                _log.info("compaction: nothing important, skipped")
                _notify_compaction("skip", {"thread_id": thread_id})

            # Delete compacted messages (lock prevents race with _build_messages)
            ids = [m["id"] for m in oldest]
            with _compaction_lock:
                db.delete_messages_by_ids(ids)

            remaining = db.count_messages(thread_id=thread_id)
            _log.info(f"compaction done: deleted {len(ids)} msgs, {remaining} remaining")
            _notify_compaction("done", {
                "thread_id": thread_id,
                "deleted": len(ids),
                "remaining": remaining,
            })

            # Cleanup old compaction summaries (>14 days)
            memory.cleanup(max_age_days=14, tag="compaction")
            # Cleanup old experience cases (>30 days)
            memory.cleanup(max_age_days=30, tag="experience")

        except Exception as e:
            _log.error(f"compaction failed: {e}", exc_info=True)
            _notify_compaction("error", {"thread_id": thread_id, "error": str(e)})

    # Run in background thread so it doesn't block the response
    t = threading.Thread(target=_do_compact, daemon=True)
    t.start()


def _get_thread_model(tid: str | None) -> str | None:
    """Get thread-specific model override, if any."""
    actual_tid = tid or threads.get_active_id()
    t = threads.get(actual_tid)
    if t and t.get("meta", {}).get("model"):
        return t["meta"]["model"]
    return None


def run(user_input: str, thread_id: str | None = None,
        source: str = "cli", image_b64: str | None = None,
        abort_event: "threading.Event | None" = None,
        ctx: TurnContext | None = None,
        save_user_msg: bool = True) -> TurnResult:
    """Run one agent turn: user input → (tool loops) → final response.

    Args:
        source: "cli", "web", or "telegram" — tells the agent where it's running.
            If *ctx* is provided, its ``source`` takes precedence.
        image_b64: optional base64-encoded image for vision
        abort_event: legacy per-request abort event. When *ctx* is provided,
            prefer ``ctx.abort_event`` instead — this parameter exists only
            for back-compat with pre-TurnContext callers.
        ctx: optional :class:`TurnContext`. When None, a default one is built
            (back-compat with CLI / single-turn callers). Concurrent callers
            (web server, telegram bot) should always supply their own.
        save_user_msg: if False, skip persisting ``user_input`` as a user
            message in the thread. The LLM still sees it as the final user
            turn in the messages array, but the database keeps only the
            assistant reply. Used by routine fires so each scheduled run
            doesn't insert a fake user-typed-this row that bloats the chat.
    """
    # Build or reuse the ctx.
    if ctx is None:
        ctx = TurnContext(source=source)
        if abort_event is not None:
            ctx.abort_event = abort_event
        # Pull any callbacks / pending state set via the legacy module globals.
        _harvest_legacy_slots(ctx)
    else:
        # Caller-supplied ctx wins, but honour the legacy abort_event= kwarg
        # if it was passed *alongside* an incomplete ctx.
        if abort_event is not None and ctx.abort_event is _legacy_ctx.abort_event:
            ctx.abort_event = abort_event
    # Thread-specific model override (local variable, not global state mutation)
    model_override = _get_thread_model(thread_id)
    return _run_inner(user_input, thread_id, ctx.source or source, image_b64,
                      model_override, ctx=ctx, save_user_msg=save_user_msg)


def _run_inner(user_input: str, thread_id: str | None,
               source: str, image_b64: str | None,
               model_override: str | None = None,
               abort_event: "threading.Event | None" = None,
               ctx: TurnContext | None = None,
               save_user_msg: bool = True) -> TurnResult:
    """Inner agent loop."""
    # Normalise ctx + abort_event. Callers going through ``run()`` always
    # provide *ctx*; the legacy ``abort_event=`` path is kept so existing
    # tests that call ``_run_inner`` directly keep working.
    if ctx is None:
        ctx = TurnContext(source=source)
        if abort_event is not None:
            ctx.abort_event = abort_event
        _harvest_legacy_slots(ctx)
    abort_event = ctx.abort_event

    _ctx_token = _set_ctx(ctx)
    try:
        return _run_inner_body(user_input, thread_id, source, image_b64,
                               model_override, abort_event, ctx,
                               save_user_msg=save_user_msg)
    finally:
        _reset_ctx(_ctx_token)


def _run_inner_body(user_input: str, thread_id: str | None,
                    source: str, image_b64: str | None,
                    model_override: str | None,
                    abort_event: threading.Event,
                    ctx: TurnContext,
                    save_user_msg: bool = True) -> TurnResult:
    """Body of the inner agent loop. Split out so _run_inner can install the
    :class:`TurnContext` on the ContextVar before and tear it down after."""
    client = providers.get_client()
    _model = model_override or providers.get_model()  # thread-safe local
    result = TurnResult()
    turn_start = time.time()
    tid = thread_id  # None = uses active thread via db._tid()

    _log.info(f"turn started | thread={tid or 'active'} | input: {user_input[:100]}")

    # Reset tool_search activations for new turn
    tools._reset_active_tools()

    # Check if this is a fallback confirmation ("да", "yes")
    if user_input.lower().strip() in ("да", "yes", "y", "давай", "go"):
        recent = db.get_recent_messages(limit=2, thread_id=tid)
        if recent and "Отправить на" in (recent[-1].get("content") or ""):
            fb_client = providers.get_fallback_client()
            fb_model = providers.get_fallback_model()
            if fb_client and fb_model and len(recent) >= 2:
                original_q = recent[-2].get("content", "")
                if original_q:
                    _console.print(f"  [yellow]⚡ Sending to {fb_model}...[/]")
                    _emit_status(f"⚡ {fb_model}...")
                    result = TurnResult()
                    db.save_message("user", user_input, thread_id=tid)
                    fb_msgs = _build_messages(original_q, thread_id=tid, source=source)
                    try:
                        fb_resp = fb_client.chat.completions.create(
                            model=fb_model, messages=fb_msgs,
                            temperature=0.3, max_tokens=2048, stream=False,
                        )
                        result.reply = _clean_response(
                            _strip_thinking(fb_resp.choices[0].message.content or "")
                        )
                        result.model = fb_model
                        db.kv_inc("stats:fallback_used")
                        db.save_message("assistant", result.reply, thread_id=tid,
                                        meta={"fallback_model": fb_model})
                        _console.print(f"  [green]⚡ Answered via {fb_model}[/]")
                        return result
                    except Exception as e:
                        _log.warning(f"fallback failed: {e}")

    # Sanitize surrogates (WSL terminal issue)
    user_input = user_input.encode("utf-8", errors="replace").decode("utf-8")

    # Auto-compact if history is too long
    _maybe_compact(thread_id=tid)

    # Save user message (with image path / file attachment if present),
    # unless the caller asked to skip persistence. Routine fires use this
    # so each scheduled run doesn't add a fake user-typed-this row to
    # the routine thread — the user_input still goes into the LLM
    # messages array via _build_messages, just not into the database.
    user_meta = None
    if image_b64 and ctx.image_path:
        user_meta = {"image_path": ctx.image_path}
    if ctx.file_meta:
        user_meta = {**(user_meta or {}), "file": ctx.file_meta}
    if save_user_msg:
        db.save_message("user", user_input, thread_id=tid, meta=user_meta)

    messages = _build_messages(user_input, thread_id=tid, source=source, image_b64=image_b64)

    # Touch thread timestamp
    threads.touch(tid)

    # Task decomposition: detect complex requests and inject a step-by-step plan
    complexity = _estimate_complexity(user_input)
    if complexity >= 3:
        steps = _decompose_task(client, _model, user_input)
        if steps and len(steps) > 1:
            plan = "\n".join(f"{i+1}. {s}" for i, s in enumerate(steps))
            # Enhance the existing user message with the plan (avoid injecting a system message)
            for m in messages:
                if m["role"] == "user" and m["content"] == user_input:
                    m["content"] = f"{user_input}\n\n[Recommended approach]\n{plan}\nStart with step 1."
                    break
            _log.info(f"task decomposed into {len(steps)} steps (complexity={complexity})")

    # Count auto-context hits (memories injected into system prompt)
    system_content = messages[0]["content"]
    if "[Relevant context from memory:]" in system_content:
        result.auto_context_hits = system_content.count("\n- [")

    # ── Agent Loop v2 (feature flag) ──
    if config.get("agent_loop_v2"):
        from agent_loop import run_loop
        from agent_events import EventEmitter
        from agent_budget import BudgetLimits

        emitter = EventEmitter()
        # Wire emitter to the ctx's per-turn callbacks. Each lambda captures
        # ``ctx`` by closure — NOT a module global — so concurrent turns can
        # install different callback sets without stomping each other.
        if ctx.on_content:
            emitter.on("content_delta", lambda e: ctx.emit_content(e.data["text"]))
        if ctx.on_thinking:
            emitter.on("thinking_delta", lambda e: ctx.emit_thinking(e.data["text"]))
        if ctx.on_status:
            emitter.on("status", lambda e: ctx.emit_status(e.data["text"]))
        if ctx.on_tool_call:
            # Track args from tool_start so tool_end can pair them
            _pending_args: dict[str, str] = {}
            def _on_start(e):
                _pending_args[e.data["name"]] = e.data.get("args", "")
            def _on_end(e):
                name = e.data["name"]
                args = _pending_args.pop(name, "")
                ctx.emit_tool_call(name, args, e.data.get("result", ""))
            emitter.on("tool_start", _on_start)
            emitter.on("tool_end", _on_end)

        _tools = tools.get_all_tools(compact=True)
        # Lower temperature when tools are present — improves tool-calling reliability
        _temp = min(soul.get_temperature(), 0.3) if _tools else soul.get_temperature()
        loop_result = run_loop(
            client=client,
            model=_model,
            messages=messages,
            tools=_tools,
            emitter=emitter,
            budget=BudgetLimits.from_config(),
            temperature=_temp,
            presence_penalty=config.get("presence_penalty"),
            max_tokens=2048,
            tool_executor=tools.execute,
            json_repair_fn=_repair_tool_json,
            extra_kwargs={"extra_body": {"options": {"num_ctx": config.get("ollama_num_ctx")}}} if providers.get_active_name() == "ollama" else {},
            abort_event=abort_event,
            ctx=ctx,
        )

        result.reply = _clean_response(loop_result["reply"])
        result.thinking = loop_result["thinking"]
        result.tool_calls_made = loop_result["tool_calls"]
        result.completion_tokens = loop_result["completion_tokens"]
        result.prompt_tokens = loop_result["prompt_tokens"]
        result.tok_per_sec = loop_result["tok_per_sec"]

        turn_ms = int((time.time() - turn_start) * 1000)
        msg_meta = {
            "tools": result.tool_calls_made,
            "tool_details": loop_result.get("tool_details", []),
            "duration_ms": turn_ms,
            "context_hits": result.auto_context_hits,
            "thinking": result.thinking or "",
            "tokens": result.completion_tokens,
            "prompt_tokens": result.prompt_tokens,
            "tok_per_sec": result.tok_per_sec,
        }
        db.save_message("assistant", result.reply, thread_id=tid, meta=msg_meta)

        stats = loop_result["stats"]
        logger.event("turn_complete", duration_ms=turn_ms, rounds=stats.turns,
                     tools_used=result.tool_calls_made, reply_len=len(result.reply),
                     est_tokens=result.completion_tokens, context_hits=result.auto_context_hits,
                     thread=tid or "active")

        if result.tool_calls_made:
            _save_experience(user_input, result, stats.turns, stats.total_errors)

        return result

    # ── Legacy agent loop (v1) ──
    rounds = 0
    last_failed_tool = None
    fail_count = 0
    total_tool_errors = 0
    _injected_instructions: set[str] = set()

    max_tool_rounds = config.get("max_tool_rounds") or 999
    _log.info(f"entering tool loop: rounds={rounds}, max={max_tool_rounds}, msgs={len(messages)}")
    _nudge_cleanup = False
    while rounds < max_tool_rounds:
        # Clean up nudge messages from previous round (don't let them persist in history)
        if _nudge_cleanup:
            # Remove last 2 messages (assistant hedge + user nudge)
            if len(messages) >= 2 and messages[-1].get("content", "").startswith("[system]"):
                messages.pop()  # remove nudge
                messages.pop()  # remove hedge
            _nudge_cleanup = False

        # Check abort
        if abort_event is not None and abort_event.is_set():
            result.reply = "⏹ Stopped."
            break

        # Warn model when approaching round limit
        if rounds == max_tool_rounds - 2:
            messages.append({"role": "user", "content": "[system] You have 2 tool rounds left. Wrap up and give your final answer NOW."})
        elif rounds == max_tool_rounds - 1:
            messages.append({"role": "user", "content": "[system] LAST round. Answer with what you have."})

        all_tools = tools.get_all_tools(compact=True)

        # Ensure model is loaded (auto-load for local providers)
        providers.ensure_model_loaded()

        # Stream the response
        # Note: enable_thinking via extra_body is not reliably supported across providers.
        # Thinking is triggered via system prompt injection in _build_messages() instead.
        # The reasoning_content handler below still catches native thinking if a provider sends it.
        presence_penalty = config.get("presence_penalty")
        extra = {}
        if providers.get_active_name() == "ollama":
            extra["extra_body"] = {"options": {"num_ctx": config.get("ollama_num_ctx")}}

        # Log prompt size for debugging
        _prompt_chars = sum(len(str(m.get("content", ""))) for m in messages)
        _tools_count = len(all_tools)
        _log.info(f"API call: {len(messages)} msgs, ~{_prompt_chars} chars, {_tools_count} tools, model={_model}")

        def _create_stream(msgs, **kw):
            try:
                return client.chat.completions.create(
                    model=_model, messages=msgs, tools=all_tools,
                    tool_choice="auto", temperature=soul.get_temperature(),
                    presence_penalty=presence_penalty, max_tokens=2048,
                    stream=True, stream_options={"include_usage": True},
                    **extra, **kw,
                )
            except Exception:
                return client.chat.completions.create(
                    model=_model, messages=msgs, tools=all_tools,
                    tool_choice="auto", temperature=soul.get_temperature(),
                    presence_penalty=presence_penalty, max_tokens=2048,
                    stream=True, **extra, **kw,
                )

        try:
            stream = _create_stream(messages)
        except Exception as e:
            # If image not supported, strip images and retry
            if "image" in str(e).lower() or "mmproj" in str(e).lower():
                _log.warning(f"vision not supported, retrying without image: {e}")
                _emit_status("Model doesn't support images, retrying as text...")
                for m in messages:
                    if isinstance(m.get("content"), list):
                        # Convert multimodal content to text-only
                        text_parts = [p["text"] for p in m["content"] if p.get("type") == "text"]
                        m["content"] = " ".join(text_parts) + "\n(An image was attached but this model doesn't support vision.)"
                stream = _create_stream(messages)
            else:
                raise

        # Collect streamed response
        full_content = ""
        reasoning_content = ""  # for models that use separate reasoning_content field
        tool_calls_data: dict[int, dict] = {}  # index -> {id, name, arguments}
        in_think = False
        think_shown = False
        finish_reason = None
        _stream_usage = None  # usage from last stream chunk
        _stream_start = time.time()

        for chunk in stream:
            # Capture usage from final chunk (empty choices)
            if hasattr(chunk, 'usage') and chunk.usage:
                _stream_usage = chunk.usage
            delta = chunk.choices[0].delta if chunk.choices else None
            if not delta:
                continue

            finish_reason = chunk.choices[0].finish_reason

            # Handle reasoning/reasoning_content (Ollama uses "reasoning", others use "reasoning_content")
            rc = getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)
            if rc:
                reasoning_content += rc
                if not think_shown:
                    _console.print("  [dim]💭 thinking...[/]")
                    _emit_status("💭 thinking...")
                    think_shown = True
                    in_think = True
                _console.print(f"  [dim]{rc}[/]", end="")
                _emit_thinking(rc)

            # Stream content (text)
            if delta.content:
                # If we were in reasoning_content mode, transition out
                if in_think and reasoning_content:
                    _console.print()  # newline after thinking block
                    in_think = False
                    _emit_status("✍️ writing reply...")

                full_content += delta.content
                text = delta.content

                # Track thinking state (for models that put <think> in content)
                # Check full_content to handle tags split across chunks
                if not in_think and "<think>" in full_content and not think_shown:
                    in_think = True
                    think_shown = True
                    _console.print("  [dim]💭 thinking...[/]")
                    _emit_status("💭 thinking...")
                    # Emit any content after <think> tag
                    after_tag = full_content.split("<think>", 1)[1]
                    if after_tag:
                        _console.print(f"  [dim]{after_tag}[/]", end="")
                        _emit_thinking(after_tag)
                elif in_think and "</think>" in full_content:
                    # Emit remaining thinking before </think>
                    before_tag = text.split("</think>", 1)[0] if "</think>" in text else ""
                    if before_tag:
                        _console.print(f"  [dim]{before_tag}[/]", end="")
                        _emit_thinking(before_tag)
                    _console.print()  # newline after thinking block
                    in_think = False
                    _emit_status("✍️ writing reply...")
                    # Emit any reply content after </think> in same chunk
                    after_close = text.split("</think>", 1)[1] if "</think>" in text else ""
                    if after_close:
                        _emit_content(after_close)
                elif in_think:
                    # Stream thinking chunk (skip partial tag fragments)
                    if text and not text.startswith("<"):
                        _console.print(f"  [dim]{text}[/]", end="")
                        _emit_thinking(text)
                else:
                    # Gemma thinking: <|channel>thought ... detect and redirect
                    if "<|channel>" in full_content and not in_think:
                        in_think = True
                        think_shown = True
                        _console.print("  [dim]💭 thinking...[/]")
                        _emit_status("💭 thinking...")
                        # Don't emit any of this as content
                    elif in_think:
                        _emit_thinking(text)  # stream as thinking, not content
                    elif "<|" in text:
                        pass  # skip special tokens from content stream
                    else:
                        # Normal reply content — stream to clients
                        _emit_content(text)

            # Stream tool calls
            if delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    if idx not in tool_calls_data:
                        tool_calls_data[idx] = {"id": "", "name": "", "arguments": ""}
                    if tc_delta.id:
                        tool_calls_data[idx]["id"] = tc_delta.id
                    if tc_delta.function:
                        if tc_delta.function.name:
                            tool_calls_data[idx]["name"] = tc_delta.function.name
                        if tc_delta.function.arguments:
                            tool_calls_data[idx]["arguments"] += tc_delta.function.arguments

        # Log finish state for debugging
        _log.info(f"LLM response: finish={finish_reason}, content_len={len(full_content)}, tool_calls={len(tool_calls_data)}, content_preview={full_content[:200]!r}")

        # Process tool calls
        if tool_calls_data:
            assistant_msg = {"role": "assistant", "content": full_content}
            assistant_msg["tool_calls"] = [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {"name": tc["name"], "arguments": tc["arguments"]},
                }
                for tc in tool_calls_data.values()
            ]
            messages.append(assistant_msg)

            for tc in tool_calls_data.values():
                result.tool_calls_made.append(tc["name"])
                db.kv_inc("stats:tool_calls_total")

                # Parse tool call arguments (with repair + retry for small models)
                args = None
                try:
                    args = json.loads(tc["arguments"])
                except Exception:
                    # Stage 1: aggressive string-level repair (fences, leading text, etc.)
                    repaired_str = _repair_tool_json(tc["arguments"])
                    if repaired_str is not None:
                        args = json.loads(repaired_str)
                        result.json_repairs += 1
                        db.kv_inc("stats:json_repairs")
                        _log.info(f"_repair_tool_json succeeded for {tc['name']}")

                if args is None:
                    # Stage 2: structural JSON repair (unclosed brackets, comments, etc.)
                    _log.warning(f"json parse failed, attempting repair: {tc['arguments'][:200]}")
                    args = _repair_json(tc["arguments"])
                    if args:
                        result.json_repairs += 1
                        db.kv_inc("stats:json_repairs")
                        _log.info(f"json repair succeeded for {tc['name']}")
                    else:
                        # Stage 3: retry — ask model to regenerate the JSON
                        retry_max = config.get("tool_retry_max")
                        if retry_max > 0:
                            _console.print(f"  [yellow]🔄 retrying {tc['name']} (broken JSON)...[/]")
                            _emit_status(f"🔄 retrying {tc['name']}...")
                            retried = _retry_tool_call(
                                client, _model,
                                tc["name"], tc["arguments"], max_retries=retry_max
                            )
                            if retried:
                                args = retried
                                result.retry_successes += 1
                                db.kv_inc("stats:retry_successes")
                                _console.print(f"  [green]✓ retry succeeded[/]")
                            else:
                                args = {}
                                _console.print(f"  [red]✗ retry failed, using empty args[/]")
                        else:
                            args = {}

                # Self-check for critical tools (shell, write_file)
                if args and tc["name"] in _SELF_CHECK_TOOLS and config.get("self_check_enabled"):
                    ok, fixed = _self_check_tool_call(
                        client, _model, tc["name"], args
                    )
                    if not ok and fixed:
                        args = fixed
                        result.self_check_fixes += 1
                        db.kv_inc("stats:self_check_fixes")
                        _console.print(f"  [yellow]🔍 self-check corrected args[/]")

                # Consensus self-verification for dangerous operations
                _verify_rejected = False
                if args and config.get("self_check_enabled") and _needs_self_check(tc["name"], args):
                    _v_ok, _v_reason = _self_verify(
                        client, _model, tc["name"], args, user_input
                    )
                    if not _v_ok:
                        _log.warning(f"self-verify REJECTED {tc['name']}: {_v_reason}")
                        _console.print(f"  [red]🛑 self-verify rejected: {_v_reason}[/]")
                        result.self_check_rejections += 1
                        db.kv_inc("stats:self_check_rejections")
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": f"Self-check rejected this action: {_v_reason}",
                        })
                        _verify_rejected = True
                    else:
                        _log.info(f"self-verify approved {tc['name']}: {_v_reason}")

                if _verify_rejected:
                    continue

                try:
                    args_short = json.dumps(args, ensure_ascii=False)
                    if len(args_short) > 80:
                        args_short = args_short[:80] + "..."
                except Exception:
                    args_short = str(args)[:80]

                _console.print(f"  [cyan]🔧 {tc['name']}[/]([dim]{args_short}[/])")
                _emit_status(f"🔧 {tc['name']}")

                # Lazy skill instruction injection (append to system msg, not insert new one)
                import skills
                instruction = skills.get_instruction(tc["name"])
                if instruction and tc["name"] not in _injected_instructions:
                    _injected_instructions.add(tc["name"])
                    messages[0]["content"] += f"\n\n[Skill: {tc['name']}]\n{instruction}"
                    _log.info(f"lazy-injected instruction for skill tool: {tc['name']}")

                tool_start = time.time()
                # Propagate per-request abort event + ctx into the tool via
                # threading.local — mirrors agent_loop._run_tool.
                try:
                    tools._set_abort_event(abort_event)
                    tools._set_turn_ctx(ctx)
                except Exception:
                    pass
                try:
                    tool_result = tools.execute(tc["name"], args)
                finally:
                    try:
                        tools._set_abort_event(None)
                        tools._set_turn_ctx(None)
                    except Exception:
                        pass
                tool_ms = int((time.time() - tool_start) * 1000)

                # Emit tool call with result to UI (Claude Code style)
                result_short = tool_result.replace("\n", " ")[:150] if tool_result else ""
                _emit_tool_call(tc['name'], args_short, result_short)

                # Smart output management: summarize large outputs, truncate if needed
                budget = config.get("context_budget")
                current_tokens = _estimate_tokens(messages)
                result_tokens = len(tool_result) // 4
                headroom = budget - current_tokens - 500  # reserve 500 for response

                # Large output? Summarize instead of dumb truncation
                if len(tool_result) > TOOL_OUTPUT_SUMMARIZE_THRESHOLD and headroom > 200:
                    tool_result = _summarize_tool_output(tc["name"], tool_result, headroom * 4)

                result_tokens = len(tool_result) // 4
                if result_tokens > headroom and headroom > 0:
                    max_chars = headroom * 4
                    original_len = len(tool_result)
                    tool_result = tool_result[:max_chars] + f"\n\n[truncated {original_len} → {max_chars} chars]"
                    _log.warning(f"tool result truncated: {original_len} → {max_chars} chars (budget: {budget})")
                elif headroom <= 0:
                    tool_result = f"[context full — output dropped ({result_tokens} tokens)]"
                    _log.warning(f"context full, tool result dropped: {tc['name']}")

                logger.event("tool_call", tool=tc["name"], args_preview=args_short,
                             result_len=len(tool_result), duration_ms=tool_ms)

                # Detect repeated failures
                if tool_result.startswith("Error"):
                    db.kv_inc("stats:tool_errors")
                    total_tool_errors += 1
                    _log.warning(f"tool error: {tc['name']} → {tool_result[:200]}")
                    if tc["name"] == last_failed_tool:
                        fail_count += 1
                    else:
                        last_failed_tool = tc["name"]
                        fail_count = 1

                    # Broader stuck detection: too many total errors = model is lost
                    if total_tool_errors >= 5:
                        tool_result += f"\n\nWARNING: You have made {total_tool_errors} tool errors this turn. Stop retrying and answer with what you have, or try a completely different approach."

                    if fail_count >= 2:
                        _log.error(f"tool {tc['name']} failed 2x, stopping retries")
                        tool_result += "\n\nSTOP: This tool failed twice. Do NOT retry. Answer with what you have or try a different approach."

                        # Auto-escalate to fallback model if configured
                        fb_client = providers.get_fallback_client()
                        fb_model = providers.get_fallback_model()
                        if fb_client and fb_model:
                            _log.info(f"auto-escalating to fallback: {fb_model}")
                            _console.print(f"  [yellow]⚡ Escalating to {fb_model}...[/]")
                            _emit_status(f"⚡ escalating to {fb_model}...")
                            try:
                                fb_resp = fb_client.chat.completions.create(
                                    model=fb_model, messages=messages,
                                    tools=all_tools, tool_choice="auto",
                                    temperature=0.3, max_tokens=2048, stream=False,
                                )
                                fb_msg = fb_resp.choices[0].message
                                if fb_msg.content:
                                    result.reply = _clean_response(_strip_thinking(fb_msg.content))
                                    result.model = fb_model
                                    db.kv_inc("stats:fallback_used")
                                    db.save_message("assistant", result.reply, thread_id=tid,
                                                    meta={"tools": result.tool_calls_made,
                                                          "fallback_model": fb_model})
                                    _console.print(f"  [green]⚡ Answered via {fb_model}[/]")
                                    return result
                            except Exception as e:
                                _log.warning(f"fallback escalation failed: {e}")
                else:
                    last_failed_tool = None
                    fail_count = 0

                # Show tool result preview
                preview = tool_result.replace("\n", " ")[:100]
                _console.print(f"  [dim]   → {preview}[/]")

                tool_msg = {
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": tool_result,
                }
                messages.append(tool_msg)

            rounds += 1

            # Some model templates (Qwen) require messages to end with user role
            # after tool results, otherwise jinja template fails with "No user query found"
            if messages[-1]["role"] == "tool":
                messages.append({"role": "user", "content": "Continue based on the tool results above."})

            # Trim context if too large (4 chars ≈ 1 token, keep under ~6k tokens)
            total_chars = sum(len(str(m.get("content", ""))) for m in messages)
            if total_chars > 24000:
                system = messages[0]
                tail = messages[-6:]
                # Ensure we don't start with orphaned tool results
                while tail and tail[0].get("role") == "tool":
                    tail.pop(0)
                # Ensure we don't start with assistant tool_calls without tool responses
                if tail and tail[0].get("role") == "assistant" and tail[0].get("tool_calls"):
                    tail.pop(0)
                messages = [system] + tail

            continue

        # No tool calls — final response
        _emit_status("✍️ writing reply...")
        # Use reasoning_content if available (Qwen3/DeepSeek native thinking),
        # otherwise extract from <think> tags in content
        result.thinking = reasoning_content.strip() or _extract_thinking(full_content) or ""
        raw_reply = _strip_thinking(full_content)

        # Retry: if model hedges instead of acting (no tool calls)
        # Nudge messages are TEMPORARY — removed after retry so they don't poison history
        _hedge_phrases = ("попробу", "let me", "i'll try", "i will", "давай", "поищу", "посмотрю", "let me search", "let me check")
        _reply_looks_like_hedge = any(p in raw_reply.lower() for p in _hedge_phrases)
        _reply_is_empty = len(raw_reply.strip()) == 0 and len(full_content) > 0  # thought but no reply
        if (not tool_calls_data
                and (_reply_is_empty
                     or (len(raw_reply) > 20 and len(raw_reply) < 3000
                         and (rounds == 0 and len(user_input.strip()) > 40 or _reply_looks_like_hedge)))):
            # Add temporary nudge (will be removed after this round)
            nudge_text = raw_reply if raw_reply.strip() else "I need to think about this..."
            messages.append({"role": "assistant", "content": nudge_text})
            messages.append({"role": "user", "content": "[system] Don't just say what you'll do — actually call the tool NOW. Reply with actual content."})
            _console.print(f"  [dim]🔄 nudging to use tools...[/]")
            _emit_status("🔄 nudging to act...")
            rounds += 1
            # After retry, remove the nudge messages so they don't stay in history
            _nudge_cleanup = True
            continue

        result.reply = _clean_response(raw_reply)

        # Offer fallback for short/empty responses on non-trivial questions
        fb_config = providers.get_fallback_config()
        if (fb_config and rounds == 0 and not result.tool_calls_made
                and len(result.reply.strip()) < 50 and len(user_input) > 30):
            _, fb_model = fb_config
            result.reply += f"\n\n---\n_Ответ короткий. Отправить на {fb_model}?_"

        # Track tokens — use real usage if available, else estimate
        if _stream_usage:
            est_tokens = _stream_usage.completion_tokens
            prompt_tokens = _stream_usage.prompt_tokens
        else:
            est_tokens = len(full_content) // 4
            prompt_tokens = 0
        turn_ms = int((time.time() - turn_start) * 1000)
        stream_ms = int((time.time() - _stream_start) * 1000)
        tok_per_sec = round(est_tokens / (stream_ms / 1000), 1) if stream_ms > 0 else 0

        # Store token stats in result for server to forward
        result.completion_tokens = est_tokens
        result.prompt_tokens = prompt_tokens
        result.tok_per_sec = tok_per_sec

        # Save with metadata for history restore
        msg_meta = {
            "tools": result.tool_calls_made,
            "duration_ms": turn_ms,
            "context_hits": result.auto_context_hits,
            "thinking": result.thinking or "",
            "tokens": est_tokens,
            "prompt_tokens": prompt_tokens,
            "tok_per_sec": tok_per_sec,
        }
        db.save_message("assistant", result.reply, thread_id=tid, meta=msg_meta)
        prev = int(db.kv_get("session_completion_tokens") or "0")
        db.kv_set("session_completion_tokens", str(prev + est_tokens))
        prev = int(db.kv_get("session_turns") or "0")
        db.kv_set("session_turns", str(prev + 1))

        logger.event("turn_complete", duration_ms=turn_ms, rounds=rounds,
                     tools_used=result.tool_calls_made, reply_len=len(result.reply),
                     est_tokens=est_tokens, context_hits=result.auto_context_hits,
                     json_repairs=result.json_repairs, retries=result.retry_successes,
                     self_checks=result.self_check_fixes,
                     self_check_rejections=result.self_check_rejections,
                     thread=tid or "active")

        if result.tool_calls_made:
            _save_experience(user_input, result, rounds, total_tool_errors)

        return result

    _log.warning(f"max tool rounds ({max_tool_rounds}) exhausted")
    result.reply = "I've used all my tool rounds for this turn."
    db.save_message("assistant", result.reply, thread_id=tid)
    return result
