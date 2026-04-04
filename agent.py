"""Core agent loop — the brain of qwe-qwe."""

import json, re, sys, time, threading, base64, io
from openai import OpenAI
from rich.console import Console
import config, db, tools, memory, soul, providers, threads
import logger

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

# Status callback: set by server.py to push live updates to WebSocket
_status_callback: callable = None  # (text: str) -> None


def _emit_status(text: str):
    """Emit a status update to connected clients (if callback set)."""
    if _status_callback:
        try:
            _status_callback(text)
        except Exception:
            pass


_thinking_callback = None  # set by server.py for live thinking streaming


def _emit_thinking(text: str):
    """Emit a thinking chunk to connected clients."""
    if _thinking_callback:
        try:
            _thinking_callback(text)
        except Exception:
            pass


_content_callback = None  # set by server.py / cli.py for live content streaming


def _emit_content(text: str):
    """Emit a content (reply) chunk to connected clients."""
    if _content_callback:
        try:
            _content_callback(text)
        except Exception:
            pass


_tool_call_callback = None  # set by server.py for tool call events


def _emit_tool_call(name: str, args_preview: str, result_preview: str = ""):
    """Emit a tool call event to connected clients (Claude Code style)."""
    if _tool_call_callback:
        try:
            _tool_call_callback(name, args_preview, result_preview)
        except Exception:
            pass


def _resize_image_b64(b64: str, max_side: int = 512, quality: int = 80) -> str:
    """Resize image to fit within max_side px and re-encode as JPEG."""
    try:
        from PIL import Image
        raw = base64.b64decode(b64)
        img = Image.open(io.BytesIO(raw))
        w, h = img.size
        if max(w, h) > max_side:
            ratio = max_side / max(w, h)
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
_abort_event = threading.Event()  # can be replaced by server
_structured_output_failed = False  # runtime cache: disable after first 400
_pending_image_path: str | None = None  # set by server before run() for image persistence


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

    # Try to close unclosed brackets/braces
    opens = s.count("{") - s.count("}")
    s += "}" * max(0, opens)
    brackets = s.count("[") - s.count("]")
    s += "]" * max(0, brackets)

    # Close unclosed string (odd number of unescaped quotes)
    quote_count = len(re.findall(r'(?<!\\)"', s))
    if quote_count % 2 == 1:
        s += '"'
        # Re-close brackets that might now be inside the string
        opens = s.count("{") - s.count("}")
        s += "}" * max(0, opens)

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
    if _structured_output_failed:
        return {}
    if providers.supports("supports_response_format"):
        return {"response_format": {"type": "json_object"}}
    return {}


def _mark_structured_failed(error: Exception):
    """Cache structured output failure to avoid repeated 400s."""
    global _structured_output_failed
    err_str = str(error)
    if "400" in err_str or "response_format" in err_str.lower():
        _structured_output_failed = True
        _log.info("structured output disabled for this session (provider returned 400)")


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


def _strip_thinking(text: str) -> str:
    """Remove thinking blocks from model output (Qwen <think>, Gemma <|channel>thought, etc.)."""
    # Qwen / DeepSeek style
    text = re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL)

    # Gemma style: <|channel>thought ... entire content may be inside
    # If the WHOLE text is a thinking block, extract any user-facing reply from it
    if text.strip().startswith("<|channel>thought"):
        # Try to find actual reply after thinking reasoning
        # Look for patterns that indicate the model switched to answering
        lines = text.split("\n")
        reply_lines = []
        in_reply = False
        for line in lines:
            stripped = line.strip()
            # Skip the opening tag
            if stripped.startswith("<|channel>"):
                continue
            # Heuristic: reply starts after thinking when model addresses user directly
            # (Russian/English text without "Step", "Thinking", numbered analysis)
            if not in_reply:
                # Detect transition to reply
                if (stripped and not stripped.startswith(("Step ", "Thinking", "Analysis", "1.", "2.", "3.", "4.", "5."))
                        and not stripped.startswith(("-", "*"))
                        and len(stripped) > 30
                        and any(c in stripped for c in "абвгдежзийклмнопрстуфхцчшщьыъэюяАБВ")):
                    in_reply = True
                    reply_lines.append(line)
            else:
                reply_lines.append(line)
        if reply_lines:
            text = "\n".join(reply_lines)
        else:
            # Fallback: just strip the tag and return everything
            text = re.sub(r"<\|channel\>thought\b\s*", "", text, flags=re.DOTALL)
    else:
        # Non-whole-block: strip thinking segment
        text = re.sub(r"<\|channel\>thought\b.*?(?=<\|channel\>|$)", "", text, flags=re.DOTALL)

    # Generic: strip any remaining <|...|> special tokens
    text = re.sub(r"<\|[^>]+\>", "", text)
    return text.strip()


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


def _extract_thinking(text: str) -> str | None:
    """Extract thinking content."""
    m = re.search(r"<think>(.*?)</think>", text, re.DOTALL)
    return m.group(1).strip() if m else None


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
    2. Thread-scoped hybrid search (score >= 0.45) — up to 2 results
    3. Global hybrid search (score >= 0.45) — fill remaining slots
    4. Experience search (score >= 0.5) — only proven patterns
    5. Deduplicate by content across all results
    """
    # Score thresholds — higher = fewer but more precise results.
    # For RRF fusion scores, 0.45 is a good cutoff (tested empirically).
    MEMORY_SCORE_MIN = 0.45
    EXPERIENCE_SCORE_MIN = 0.5

    try:
        seen_texts = set()
        lines = ["[Relevant context from memory:]"]

        # Compute embedding once (FastEmbed, no network)
        try:
            vector = memory.embed(user_input)
        except Exception:
            return ""  # embedding unavailable

        # Thread-scoped search first (prioritize local context)
        if thread_id:
            thread_results = memory.search_by_vector(
                vector, limit=2, thread_id=thread_id,
                query_text=user_input,
                score_threshold=MEMORY_SCORE_MIN,
            )
            for r in thread_results:
                if r["text"] not in seen_texts:
                    lines.append(f"- [{r['tag']}] {r['text']}")
                    seen_texts.add(r["text"])

        # Global search (fill remaining slots)
        max_memory = config.get("max_memory_results")
        remaining = max_memory - (len(lines) - 1)
        if remaining > 0:
            global_results = memory.search_by_vector(
                vector, limit=remaining + 2,
                query_text=user_input,
                score_threshold=MEMORY_SCORE_MIN,
            )
            for r in global_results:
                if len(lines) - 1 >= max_memory:
                    break
                if r["text"] not in seen_texts:
                    lines.append(f"- [{r['tag']}] {r['text']}")
                    seen_texts.add(r["text"])

        # Experience cases (higher threshold — only proven patterns)
        if config.get("experience_learning"):
            exp_hits = memory.search_by_vector(
                vector, limit=config.MAX_EXPERIENCE_RESULTS + 1, tag="experience",
                query_text=user_input,
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
            if exp_lines:
                lines.append("")
                lines.append("[Relevant past experiences:]")
                lines.extend(exp_lines)

        hits = sum(1 for l in lines if l.startswith("- "))
        if hits > 0:
            _log.info(f"auto_context: {hits} items injected (thread={thread_id or 'global'})")
        if len(lines) == 1:
            return ""
        return "\n".join(lines)
    except BaseException as e:
        _log.warning(f"auto_context failed ({type(e).__name__}): {e}", exc_info=True)
        return ""


_OUTCOME_WEIGHTS = {"success": 1.0, "partial": 0.6, "failed": 0.2}


def _save_experience(user_input: str, result: "TurnResult", rounds: int,
                     fail_count: int, _sync: bool = False):
    """Save a compact experience case after a tool-using turn (async, non-blocking)."""
    if not config.get("experience_learning"):
        return
    if not result.tool_calls_made:
        return

    outcome = "failed" if fail_count >= 2 else "partial" if fail_count > 0 else "success"
    task = user_input.strip().replace("\n", " ")[:80]
    tools_str = ", ".join(dict.fromkeys(result.tool_calls_made))
    reply_summary = result.reply.strip().replace("\n", " ")[:60]

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

    # Thinking mode — inject prompt instruction for models that don't natively support it
    thinking_on = db.kv_get("thinking_enabled")
    if thinking_on == "true":
        system_text += (
            "\n\nIMPORTANT: Before answering, think through the problem step by step. "
            "Write your reasoning inside <think>...</think> tags. "
            "After thinking, write your final answer outside the tags. "
            "Example:\n<think>\nLet me analyze this...\n</think>\nHere is my answer."
        )

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

    msgs.extend(history)

    # New user message — multimodal if image provided
    if image_b64:
        image_b64 = _resize_image_b64(image_b64, max_side=512)
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
                 "retry_successes", "self_check_fixes", "self_check_rejections")

    def __init__(self):
        self.reply = ""
        self.thinking = ""
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.total_tokens = 0
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
_compaction_callbacks: list = []  # [(callback_fn, context)]


def on_compaction(callback):
    """Register a callback for compaction events: callback(event, data).
    
    Events: 'start', 'summary', 'done', 'skip', 'error'
    """
    _compaction_callbacks.append(callback)


def _notify_compaction(event: str, data: dict):
    """Notify all registered callbacks about compaction events."""
    for cb in _compaction_callbacks:
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
                        "Compress this conversation into key facts for future reference. "
                        "Extract ONLY:\n"
                        "- Decisions made and why\n"
                        "- Technical facts: names, paths, configs, versions\n"
                        "- Task outcomes: what worked, what failed\n"
                        "- User preferences discovered\n"
                        "Skip greetings, small talk, tool output details.\n"
                        "Format: bullet points, max 150 words.\n"
                        "If nothing worth saving — reply SKIP."
                    )},
                    {"role": "user", "content": convo[:8000]},  # cap input
                ],
                temperature=0.3,
                max_tokens=512,
            )
            summary = _strip_thinking(resp.choices[0].message.content or "")

            if summary and not summary.strip().upper().startswith("SKIP"):
                memory.save(summary, tag="compaction", thread_id=thread_id)
                _log.info(f"compaction: saved summary ({len(summary)} chars)")
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
        source: str = "cli", image_b64: str | None = None) -> TurnResult:
    """Run one agent turn: user input → (tool loops) → final response.

    Args:
        source: "cli", "web", or "telegram" — tells the agent where it's running
        image_b64: optional base64-encoded image for vision
    """
    # Thread-specific model override (local variable, not global state mutation)
    model_override = _get_thread_model(thread_id)
    return _run_inner(user_input, thread_id, source, image_b64, model_override)


def _run_inner(user_input: str, thread_id: str | None,
               source: str, image_b64: str | None,
               model_override: str | None = None) -> TurnResult:
    """Inner agent loop."""
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

    # Save user message (with image path if present)
    user_meta = None
    if image_b64 and _pending_image_path:
        user_meta = {"image_path": _pending_image_path}
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

    rounds = 0
    last_failed_tool = None
    fail_count = 0
    total_tool_errors = 0  # cumulative (never resets) — for experience scoring
    _injected_instructions: set[str] = set()  # track which skill instructions were injected

    max_tool_rounds = config.get("max_tool_rounds")
    while rounds < max_tool_rounds:
        # Check abort
        if hasattr(sys.modules[__name__], '_abort_event') and _abort_event.is_set():
            result.reply = "⏹ Stopped."
            break

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

        stream = client.chat.completions.create(
            model=_model,
            messages=messages,
            tools=all_tools,
            tool_choice="auto",
            temperature=soul.get_temperature(),
            presence_penalty=presence_penalty,
            max_tokens=2048,
            stream=True,
            **extra,
        )

        # Collect streamed response
        full_content = ""
        reasoning_content = ""  # for models that use separate reasoning_content field
        tool_calls_data: dict[int, dict] = {}  # index -> {id, name, arguments}
        in_think = False
        think_shown = False
        finish_reason = None

        for chunk in stream:
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
                tool_result = tools.execute(tc["name"], args)
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

        # Retry: if model hedges instead of acting (no tool calls on round 0)
        if (rounds == 0 and not tool_calls_data and len(raw_reply) < 3000
                and len(raw_reply) > 20):
            messages.append({"role": "assistant", "content": raw_reply})
            messages.append({"role": "user", "content": "Don't ask, just do it. Use the tools NOW. Не спрашивай — ДЕЛАЙ. Используй инструменты СЕЙЧАС."})
            _console.print(f"  [dim]🔄 nudging to use tools...[/]")
            _emit_status("🔄 nudging to act...")
            rounds += 1
            continue

        result.reply = _clean_response(raw_reply)

        # Offer fallback for short/empty responses on non-trivial questions
        fb_config = providers.get_fallback_config()
        if (fb_config and rounds == 0 and not result.tool_calls_made
                and len(result.reply.strip()) < 50 and len(user_input) > 30):
            _, fb_model = fb_config
            result.reply += f"\n\n---\n_Ответ короткий. Отправить на {fb_model}?_"

        # Track session tokens (estimate from content length since streaming doesn't give usage)
        est_tokens = len(full_content) // 4
        turn_ms = int((time.time() - turn_start) * 1000)

        # Save with metadata for history restore
        msg_meta = {
            "tools": result.tool_calls_made,
            "duration_ms": turn_ms,
            "context_hits": result.auto_context_hits,
            "thinking": result.thinking or "",
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
