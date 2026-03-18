"""Core agent loop — the brain of qwe-qwe."""

import json, re, sys, time, threading, base64, io
from openai import OpenAI
from rich.console import Console
import config, db, tools, memory, soul, providers, threads
import logger

_log = logger.get("agent")
_console = Console()
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
                    _log.info(f"self-check corrected {tool_name}: {args} → {parsed['args']}")
                    return False, parsed["args"]
            except json.JSONDecodeError:
                pass  # fall through to text parsing

        if text.upper().startswith("OK"):
            return True, None

        m = re.search(r'\{.*\}', text, re.DOTALL)
        if m:
            corrected = json.loads(m.group())
            _log.info(f"self-check corrected {tool_name}: {args} → {corrected}")
            return False, corrected

        return True, None
    except Exception as e:
        _log.warning(f"self-check failed for {tool_name}: {e}")
        return True, None


def _strip_thinking(text: str) -> str:
    """Remove <think>...</think> blocks from model output."""
    return re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL).strip()


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


def _auto_context(user_input: str, thread_id: str | None = None) -> str:
    """Auto-retrieve relevant memories: thread-scoped first, then global.
    
    Strategy:
    1. Search within current thread (if thread_id provided) — up to 2 results
    2. Search globally — up to remaining slots
    3. Deduplicate by content
    """
    try:
        seen_texts = set()
        lines = ["[Relevant context from memory:]"]

        # Compute embedding once, reuse for all searches (saves 2 API calls)
        try:
            vector = memory.embed(user_input)
        except Exception:
            return ""  # embedding unavailable

        # Thread-scoped search first (prioritize local context)
        if thread_id:
            thread_results = memory.search_by_vector(
                vector, limit=2, thread_id=thread_id
            )
            for r in thread_results:
                if r["score"] > 0.3 and r["text"] not in seen_texts:
                    tag_info = f"{r['tag']}"
                    lines.append(f"- [{tag_info}] {r['text']}")
                    seen_texts.add(r["text"])

        # Global search (fill remaining slots)
        max_memory = config.get("max_memory_results")
        remaining = max_memory - (len(lines) - 1)
        if remaining > 0:
            global_results = memory.search_by_vector(vector, limit=remaining + 2)
            for r in global_results:
                if len(lines) - 1 >= max_memory:
                    break
                if r["score"] > 0.3 and r["text"] not in seen_texts:
                    lines.append(f"- [{r['tag']}] {r['text']}")
                    seen_texts.add(r["text"])

        # Experience cases (separate tag, additive, higher threshold)
        if config.get("experience_learning"):
            exp_hits = memory.search_by_vector(
                vector, limit=config.MAX_EXPERIENCE_RESULTS + 1, tag="experience"
            )
            exp_lines = []
            for r in exp_hits:
                if len(exp_lines) >= config.MAX_EXPERIENCE_RESULTS:
                    break
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

    # Auto-retrieve from Qdrant (thread-scoped + global)
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
                 "retry_successes", "self_check_fixes")

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
SYSTEM_RESERVE = 2000      # system prompt + tools
RECENT_RESERVE = 2         # always keep last N user+assistant pairs


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
                        "Summarize this conversation into key facts. Extract:\n"
                        "- User preferences and decisions\n"
                        "- Technical details, configs, paths\n"
                        "- Task results and outcomes\n"
                        "- Names, dates, important context\n"
                        "If nothing important — reply SKIP.\n"
                        "Be concise: bullet points, max 200 words."
                    )},
                    {"role": "user", "content": convo[:8000]},  # cap input
                ],
                temperature=0.3,
                max_tokens=512,
            )
            summary = _strip_thinking(resp.choices[0].message.content or "")

            if summary and summary.strip().upper() != "SKIP":
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
        stream = client.chat.completions.create(
            model=_model,
            messages=messages,
            tools=all_tools,
            tool_choice="auto",
            temperature=soul.get_temperature(),
            presence_penalty=presence_penalty,
            max_tokens=2048,
            stream=True,
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

            # Handle reasoning_content (Qwen3/DeepSeek with enable_thinking)
            rc = getattr(delta, "reasoning_content", None)
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
                elif in_think:
                    # Stream thinking chunk (skip partial tag fragments)
                    if text and not text.startswith("<"):
                        _console.print(f"  [dim]{text}[/]", end="")
                        _emit_thinking(text)

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
                try:
                    args = json.loads(tc["arguments"])
                except Exception:
                    _log.warning(f"json parse failed, attempting repair: {tc['arguments'][:200]}")
                    args = _repair_json(tc["arguments"])
                    if args:
                        result.json_repairs += 1
                        db.kv_inc("stats:json_repairs")
                        _log.info(f"json repair succeeded for {tc['name']}")
                    else:
                        # Retry: ask model to regenerate the JSON
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

                # Context guard: check if tool result would blow the budget
                budget = config.get("context_budget")
                current_tokens = _estimate_tokens(messages)
                result_tokens = len(tool_result) // 4
                headroom = budget - current_tokens - 500  # reserve 500 for response

                if result_tokens > headroom and headroom > 0:
                    # Truncate tool result to fit
                    max_chars = headroom * 4
                    original_len = len(tool_result)
                    tool_result = tool_result[:max_chars] + (
                        f"\n\n⚠️ OUTPUT TRUNCATED ({original_len} chars → {max_chars} chars). "
                        f"Context budget: {budget} tokens, used: {current_tokens}. "
                        f"Try a more specific query or work with smaller chunks."
                    )
                    _log.warning(f"tool result truncated: {original_len} → {max_chars} chars (budget: {budget})")
                elif headroom <= 0:
                    tool_result = (
                        f"⚠️ CONTEXT FULL — cannot fit tool output ({result_tokens} tokens needed, "
                        f"{budget} budget used up). Summarize what you have or start fresh."
                    )
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

                    if fail_count >= 2:
                        _log.error(f"tool {tc['name']} failed 2x, stopping retries")
                        tool_result += "\n\nSTOP: This tool failed twice. Do NOT retry. Answer with what you have or try a different approach."
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

        # Retry: if model hedges instead of acting
        action_phrases = ["i would", "i can", "i'll", "let me", "shall i", "want me to"]
        if (rounds == 0 and
            any(p in raw_reply.lower()[:100] for p in action_phrases) and
            len(raw_reply) < 300):
            messages.append({"role": "assistant", "content": raw_reply})
            messages.append({"role": "user", "content": "Don't ask, just do it. Use the tools."})
            _console.print(f"  [dim]🔄 nudging to use tools...[/]")
            rounds += 1
            continue

        result.reply = _clean_response(raw_reply)

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
                     self_checks=result.self_check_fixes, thread=tid or "active")

        if result.tool_calls_made:
            _save_experience(user_input, result, rounds, total_tool_errors)

        return result

    _log.warning(f"max tool rounds ({max_tool_rounds}) exhausted")
    result.reply = "I've used all my tool rounds for this turn."
    db.save_message("assistant", result.reply, thread_id=tid)
    return result
