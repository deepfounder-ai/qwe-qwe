"""Agent Loop v2 — clean execution loop inspired by claw-code-agent.

Replaces the inner loop of agent._run_inner() with:
- Clean turn-based execution
- Continuation handling for max_tokens truncation
- Multi-dimensional budget tracking
- Event-based streaming (no global callbacks)
- Tool execution separated from loop logic
"""

import json
import re
import time
import logger
from agent_events import EventEmitter, AgentEvent, EVT_BUDGET_WARNING
from agent_budget import BudgetLimits, BudgetStats, check_budget, warning_check

_log = logger.get("loop")


# ── Layer 1: Intent Router via small LLM ──

_ROUTER_SYSTEM = '''You are a tool routing function. Output EXACTLY one JSON object.

RULES:
- Output ONLY valid JSON. NEVER add text, explanations, or extra fields.
- ANY request about current/recent info (news, weather, prices, events, sports, stocks) → browser_open
- You DO have access to tools. NEVER say you cannot do something. Just route to the right tool.
- If user asks to remember/save → memory_save
- If user asks to find saved info → memory_search
- If user wants to run a command or find files → shell
- If user sends a URL → browser_open with that URL
- General knowledge (math, facts, greetings) → none

For browser_open, translate query to English, use DuckDuckGo:
{"tool": "browser_open", "args": {"url": "https://duckduckgo.com/?q=QUERY"}}

/no_think'''

_ROUTER_FEW_SHOT = [
    {"role": "user", "content": "weather in London"},
    {"role": "assistant", "content": '{"tool":"browser_open","args":{"url":"https://duckduckgo.com/?q=weather+London"}}'},
    {"role": "user", "content": "find all .py files"},
    {"role": "assistant", "content": '{"tool":"shell","args":{"command":"find . -name \\"*.py\\""}}'},
    {"role": "user", "content": "remember my birthday is May 5"},
    {"role": "assistant", "content": '{"tool":"memory_save","args":{"text":"User birthday is May 5"}}'},
    {"role": "user", "content": "news about Hungary"},
    {"role": "assistant", "content": '{"tool":"browser_open","args":{"url":"https://duckduckgo.com/?q=Hungary+latest+news"}}'},
    {"role": "user", "content": "dollar exchange rate today"},
    {"role": "assistant", "content": '{"tool":"browser_open","args":{"url":"https://duckduckgo.com/?q=USD+exchange+rate+today"}}'},
    # Ambiguous / short queries should always route to none to avoid hallucinations
    {"role": "user", "content": "hello"},
    {"role": "assistant", "content": '{"tool":"none"}'},
    {"role": "user", "content": "try again"},
    {"role": "assistant", "content": '{"tool":"none"}'},
    {"role": "user", "content": "ok"},
    {"role": "assistant", "content": '{"tool":"none"}'},
    {"role": "user", "content": "thanks"},
    {"role": "assistant", "content": '{"tool":"none"}'},
]

# Router model cache
_ROUTER_MODEL_CACHE: str | None = None


def _get_router_config() -> tuple[str, str] | None:
    """Get router URL and model from config. Returns (url, model) or None if disabled."""
    global _ROUTER_MODEL_CACHE
    import config
    url = config.get("router_url")
    if not url:
        return None

    model = config.get("router_model")
    if model:
        return (url, model)

    # Auto-detect model from endpoint
    if _ROUTER_MODEL_CACHE:
        return (url, _ROUTER_MODEL_CACHE)
    try:
        import urllib.request
        req = urllib.request.Request(f"{url}/models")
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read())
            models = data.get("data") or data.get("models") or []
            if models:
                _ROUTER_MODEL_CACHE = models[0].get("id") or models[0].get("name")
                return (url, _ROUTER_MODEL_CACHE)
    except Exception:
        pass
    return None


_AMBIGUOUS_PHRASES = (
    "еще раз", "ещё раз", "try again", "again", "повтори",
    "ok", "okay", "fine", "ок", "хорошо", "ладно",
    "thanks", "спасибо", "thank you",
    "да", "нет", "yes", "no",
    "продолжи", "continue", "дальше", "next",
)


def _is_ambiguous_input(text: str) -> bool:
    """True if input is too context-dependent to route without history."""
    t = text.strip().lower().rstrip("!?.,")
    if len(t) < 4:
        return True
    # Exact match short phrases
    if t in _AMBIGUOUS_PHRASES:
        return True
    # Starts with ambiguous phrase AND short overall
    if len(t) < 30:
        for phrase in _AMBIGUOUS_PHRASES:
            if t.startswith(phrase):
                return True
    return False


def _detect_intent(user_input: str, tool_names: set[str]) -> tuple[str, dict] | None:
    """Layer 1: Route user intent to a tool using a small LLM.
    Falls back to URL regex if router model is unavailable.
    Returns (tool_name, args_dict) or None.
    """
    text = user_input.strip()
    if not text or len(text) < 3:
        return None

    # Fast path: URL in message → browser_open (no model needed)
    url_match = re.search(r"https?://\S+", text)
    if url_match and "browser_open" in tool_names:
        return ("browser_open", {"url": url_match.group()})

    # Ambiguous inputs need conversation context — let main LLM handle those
    if _is_ambiguous_input(text):
        _log.debug(f"router: skipping ambiguous input {text[:50]!r}")
        return None

    # Try small LLM router
    router_cfg = _get_router_config()
    if not router_cfg:
        return None  # router not configured, skip

    router_url, router_model = router_cfg
    try:
        import urllib.request
        msgs = [{"role": "system", "content": _ROUTER_SYSTEM}] + _ROUTER_FEW_SHOT + [{"role": "user", "content": text}]
        payload = json.dumps({
            "model": router_model,
            "messages": msgs,
            "temperature": 0.0,
            "max_tokens": 200,
            "response_format": {"type": "json_object"},
        }).encode("utf-8")
        req = urllib.request.Request(f"{router_url}/chat/completions", data=payload,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())

        content = result["choices"][0]["message"]["content"].strip()
        if not content:
            return None

        # Parse JSON — handle truncated responses
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            # Try to repair truncated JSON
            if '{"tool"' in content:
                # Find last complete key-value and close
                content = content.rstrip()
                if not content.endswith("}"):
                    # Truncated URL — close it
                    if '"url":' in content or '"command":' in content or '"text":' in content:
                        content = content.rstrip('"') + '"}}'
                    else:
                        content += "}"
                try:
                    parsed = json.loads(content)
                except json.JSONDecodeError:
                    return None
            else:
                return None

        tool = parsed.get("tool", "none")
        if tool == "none" or tool not in tool_names:
            return None

        args = parsed.get("args", {})

        # Sanity check: for browser searches, the URL must reference the user's query.
        # Prevents router from hallucinating results copied from few-shot examples.
        if tool == "browser_open" and isinstance(args, dict):
            url = str(args.get("url", "")).lower()
            # Extract query term from duckduckgo URL
            q_match = re.search(r'[?&]q=([^&]+)', url)
            if q_match:
                query = q_match.group(1).replace("+", " ").replace("%20", " ")
                # Check if any meaningful word from query appears in user input
                user_words = set(re.findall(r'\w+', text.lower()))
                query_words = set(re.findall(r'\w+', query))
                # Ignore common short/stop words
                stop = {"the", "a", "an", "today", "latest", "news", "about", "what", "is", "in"}
                query_words -= stop
                if query_words and not (query_words & user_words):
                    _log.warning(f"router hallucinated: input={text[:40]!r} query={query!r} — rejecting")
                    return None

        _log.info(f"router: {tool}({json.dumps(args, ensure_ascii=False)[:80]})")
        return (tool, args)

    except Exception as e:
        _log.debug(f"router failed: {e}")
        return None


# ── Layer 2: Text-to-Tool Extraction ──

def _extract_tool_from_text(text: str, tool_names: set[str]) -> tuple[str, dict] | None:
    """Layer 2: Extract tool call from model text output when it fails to use native function calling.
    Returns (tool_name, args_dict) or None.
    """
    if not text or not tool_names:
        return None

    # Pattern 1: Qwen leaked <tool_call> syntax
    m = re.search(r'<tool_call>\s*\{[^}]*?"name"\s*:\s*"(\w+)"[^}]*?"arguments"\s*:\s*(\{[^}]*\})', text, re.DOTALL)
    if m and m.group(1) in tool_names:
        try:
            args = json.loads(m.group(2))
            return (m.group(1), args)
        except json.JSONDecodeError:
            pass

    # Pattern 2: function_name({"key": "value"}) in text
    for name in tool_names:
        pat = re.search(rf'{re.escape(name)}\s*\(\s*(\{{[^)]*\}})\s*\)', text)
        if pat:
            try:
                args = json.loads(pat.group(1))
                return (name, args)
            except json.JSONDecodeError:
                pass

    # Pattern 3: function_name(key="value") in text
    for name in tool_names:
        pat = re.search(rf'{re.escape(name)}\s*\(\s*(\w+)\s*=\s*["\']([^"\']+)["\']', text)
        if pat:
            return (name, {pat.group(1): pat.group(2)})

    # Pattern 4: "use tool_name" + URL nearby
    for name in tool_names:
        if name in text:
            url = re.search(r'https?://\S+', text)
            if url and "browser" in name:
                return (name, {"url": url.group()})

    return None


def _get_tool_names(tools: list[dict]) -> set[str]:
    """Extract tool names from tools list."""
    return {t["function"]["name"] for t in tools if "function" in t}


# ── Context Management: tool result clearing + size caps ──

_TOOL_RESULT_MAX_CHARS = 4000  # cap individual tool results
_KEEP_RECENT_TOOL_RESULTS = 3  # keep last N tool results intact


def _clear_old_tool_results(messages: list[dict]):
    """Replace old tool results with one-line summaries (keep last N intact).
    This prevents context overflow during long multi-step tasks.
    Claude Code calls this Tier 1 clearing — runs before every API call.
    """
    tool_indices = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    if len(tool_indices) <= _KEEP_RECENT_TOOL_RESULTS:
        return  # nothing to clear

    # Clear all but the last N
    to_clear = tool_indices[:-_KEEP_RECENT_TOOL_RESULTS]
    for idx in to_clear:
        m = messages[idx]
        content = m.get("content", "")
        if content.startswith("[cleared"):
            continue  # already cleared
        # Create one-line summary
        first_line = content.split("\n")[0][:120].strip()
        m["content"] = f"[cleared — {first_line}]"


def _cap_tool_result(result: str) -> str:
    """Cap tool result to prevent one large output from eating the entire context."""
    if len(result) <= _TOOL_RESULT_MAX_CHARS:
        return result
    return result[:_TOOL_RESULT_MAX_CHARS] + f"\n... [truncated at {_TOOL_RESULT_MAX_CHARS} chars, {len(result)} total]"


def _should_plan(user_input: str) -> bool:
    """Detect if user request is complex enough to warrant a planning step."""
    if len(user_input) < 80:
        return False
    _plan_triggers = (
        "implement", "create", "build", "refactor", "write", "develop", "make",
        "реализуй", "создай", "напиши", "разработай", "сделай", "построй",
        "step by step", "пошагово", "план", "plan",
    )
    text_lower = user_input.lower()
    return any(t in text_lower for t in _plan_triggers)


def _synthesize_tool_call(tool_name: str, args: dict, tool_executor, messages: list, emitter, stats) -> str:
    """Execute a tool call that was detected from text/intent (not native function calling).
    Injects proper messages into the conversation and returns the tool result.
    """
    import uuid
    call_id = f"synth_{uuid.uuid4().hex[:8]}"

    # Inject assistant message with synthetic tool call
    messages.append({
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": call_id,
            "type": "function",
            "function": {"name": tool_name, "arguments": json.dumps(args)},
        }],
    })

    # Execute
    emitter.tool_start(tool_name, str(args)[:80])
    stats.add_tool_call()

    tool_start = time.time()
    try:
        result = tool_executor(tool_name, args)
    except Exception as e:
        result = f"Error: {e}"
        stats.add_error()
    tool_ms = int((time.time() - tool_start) * 1000)

    result_short = result.replace("\n", " ")[:150]
    emitter.tool_end(tool_name, result_short, tool_ms)

    # Inject tool result
    messages.append({"role": "tool", "tool_call_id": call_id, "content": result})
    return result


def run_loop(
    client,
    model: str,
    messages: list[dict],
    tools: list[dict],
    emitter: EventEmitter,
    budget: BudgetLimits | None = None,
    temperature: float = 0.6,
    presence_penalty: float = 1.5,
    max_tokens: int = 2048,
    tool_executor=None,
    json_repair_fn=None,
    self_check_fn=None,
    extra_kwargs: dict | None = None,
) -> dict:
    """Run the agent loop.

    Args:
        client: OpenAI-compatible client
        model: model name
        messages: initial messages (system + history + user)
        tools: list of tool schemas (OpenAI format)
        emitter: EventEmitter for streaming
        budget: execution limits
        temperature: LLM temperature
        presence_penalty: LLM presence penalty
        max_tokens: max tokens per LLM call
        tool_executor: callable(name, args) -> str
        json_repair_fn: callable(raw_json) -> str|None (optional JSON repair)
        self_check_fn: callable(name, args) -> (ok, fixed_args)|None (optional)
        extra_kwargs: extra kwargs for LLM API call

    Returns:
        dict with: reply, thinking, tool_calls, finish_reason, stats
    """
    if budget is None:
        budget = BudgetLimits.from_config()
    if tool_executor is None:
        import tools as _tools
        tool_executor = _tools.execute

    stats = BudgetStats()
    all_tool_calls: list[str] = []
    final_content = ""
    thinking_content = ""
    extra = extra_kwargs or {}
    _budget_warned = False
    _recent_tool_sigs: list[str] = []  # loop detection: last N tool call signatures
    _tool_name_counts: dict[str, int] = {}  # Layer 4: per-tool frequency
    _tool_search_count = 0  # Layer 3: tool_search call counter
    _nudge_count = 0  # Layer 5: anti-hedge nudge counter
    _nudge_cleanup = False  # Layer 5: cleanup nudge messages on next iteration
    _force_finish = False  # Layer 4: force finish after loop detection
    _tool_names = _get_tool_names(tools) if tools else set()

    # Layer 1: Intent router — detect obvious intents before first LLM call
    if tools and tool_executor:
        _user_input = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                _user_input = m.get("content", "") if isinstance(m.get("content"), str) else ""
                break
        if _user_input:
            _log.info(f"router: checking intent for: {_user_input[:60]}")
            intent = _detect_intent(_user_input, _tool_names)
            _log.info(f"router: result = {intent}")
            if intent:
                _log.info(f"intent detected: {intent[0]}({str(intent[1])[:60]})")
                # Auto-activate browser tools if needed
                import tools as _tools_mod
                _tools_mod._active_extra_tools.update({"browser_open", "browser_snapshot", "browser_screenshot",
                                                        "browser_click", "browser_fill", "browser_eval", "browser_close"})
                _synthesize_tool_call(intent[0], intent[1], tool_executor, messages, emitter, stats)
                all_tool_calls.append(intent[0])

    # Aggregated generation time across all turns — time from FIRST content
    # token of each turn's stream to the LAST chunk. Excludes time-to-first-token
    # (prompt processing) so tok/s reflects actual generation speed.
    total_gen_ms = 0
    total_output_tokens = 0

    # Fix 2: Planning prompt — inject for complex tasks on first round
    if tools and tool_executor:
        _ui = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                _ui = m.get("content", "") if isinstance(m.get("content"), str) else ""
                break
        if _ui and _should_plan(_ui):
            _log.info("complex task detected, injecting planning prompt")
            messages.append({"role": "user", "content":
                "[system] This is a complex task. Before executing, outline a numbered plan (3-7 steps). "
                "Then immediately start executing step 1. Check off steps as you complete them."})

    while True:
        # ── Budget check ──
        decision = check_budget(budget, stats)
        if decision.exceeded:
            _log.warning(f"budget exceeded: {decision.reason}")
            emitter.status(f"Budget: {decision.reason}")
            break

        # Layer 4: force finish — after loop detection, let model produce one more reply then stop
        if _force_finish and stats.turns > 1 and final_content:
            _log.info("force finish: breaking after loop detection")
            break

        # Fix 1: Clear old tool results to prevent context overflow (Tier 1 clearing)
        if stats.turns > 0:
            _clear_old_tool_results(messages)

        # Budget warning to model (inject once)
        if not _budget_warned:
            warning = warning_check(budget, stats)
            if warning:
                messages.append({"role": "user", "content": f"[system] {warning}. Give final answer."})
                emitter.emit(AgentEvent(EVT_BUDGET_WARNING, {"message": warning}))
                _budget_warned = True

        stats.add_turn()
        emitter.emit(AgentEvent("turn_start", {"turn": stats.turns}))

        # ── Call LLM ──
        stream_start = time.time()
        first_token_ts: float | None = None  # moment the first content/reasoning/tool chunk arrives
        full_content = ""
        reasoning_content = ""
        tool_calls_data: dict[int, dict] = {}
        finish_reason = None
        usage = None

        try:
            stream = client.chat.completions.create(
                model=model,
                messages=messages,
                tools=tools if tools else None,
                tool_choice="auto" if tools else None,
                temperature=temperature,
                presence_penalty=presence_penalty,
                max_tokens=max_tokens,
                stream=True,
                stream_options={"include_usage": True},
                **extra,
            )
        except Exception:
            # Fallback without stream_options
            try:
                stream = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    tools=tools if tools else None,
                    tool_choice="auto" if tools else None,
                    temperature=temperature,
                    presence_penalty=presence_penalty,
                    max_tokens=max_tokens,
                    stream=True,
                    **extra,
                )
            except Exception as e:
                # Vision fallback — strip images if not supported
                if "image" in str(e).lower() or "mmproj" in str(e).lower():
                    _log.warning(f"vision not supported, stripping images")
                    emitter.status("Model doesn't support images, retrying...")
                    for m in messages:
                        if isinstance(m.get("content"), list):
                            text_parts = [p["text"] for p in m["content"] if p.get("type") == "text"]
                            m["content"] = " ".join(text_parts) + "\n(Image attached but model doesn't support vision.)"
                    stream = client.chat.completions.create(
                        model=model, messages=messages,
                        tools=tools if tools else None,
                        tool_choice="auto" if tools else None,
                        temperature=temperature, max_tokens=max_tokens,
                        stream=True, **extra,
                    )
                else:
                    raise

        # ── Process stream ──
        in_think = False
        _think_detected = False
        for chunk in stream:
            # Usage from final chunk
            if hasattr(chunk, 'usage') and chunk.usage:
                usage = chunk.usage

            delta = chunk.choices[0].delta if chunk.choices else None
            if not delta:
                continue

            finish_reason = chunk.choices[0].finish_reason

            # Mark time-to-first-token on the first chunk carrying any real
            # output (content / reasoning / tool_calls). `first_token_ts`
            # then anchors generation-speed measurement, stripping out the
            # prompt-processing latency that precedes the first token.
            if first_token_ts is None and (
                delta.content
                or getattr(delta, "reasoning_content", None)
                or getattr(delta, "reasoning", None)
                or delta.tool_calls
            ):
                first_token_ts = time.time()

            # Reasoning content (Qwen/DeepSeek native thinking)
            rc = getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)
            if rc:
                reasoning_content += rc
                if not in_think:
                    emitter.status("thinking...")
                    in_think = True
                emitter.thinking(rc)

            # Text content
            if delta.content:
                full_content += delta.content
                text = delta.content

                # Detect <think> tags in content (check only new text, not full_content)
                if not _think_detected and "<think>" in text:
                    in_think = True
                    _think_detected = True
                    emitter.status("thinking...")
                elif in_think and "</think>" in text:
                    in_think = False
                    emitter.status("writing reply...")
                elif "<|channel>" in text and not _think_detected:
                    in_think = True
                    emitter.status("thinking...")
                elif in_think:
                    emitter.thinking(text)
                elif "<|" in text:
                    pass  # skip special tokens
                else:
                    emitter.content(text)

            # Tool calls
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

        stream_end = time.time()
        stream_ms = int((stream_end - stream_start) * 1000)
        if first_token_ts is not None:
            gen_ms = int((stream_end - first_token_ts) * 1000)
            ttft_ms = int((first_token_ts - stream_start) * 1000)
        else:
            gen_ms = 0
            ttft_ms = stream_ms

        # Track tokens
        turn_output_tokens = 0
        if usage:
            turn_output_tokens = getattr(usage, 'completion_tokens', 0)
            stats.add_tokens(
                input_tok=getattr(usage, 'prompt_tokens', 0),
                output_tok=turn_output_tokens,
            )
        else:
            # No usage from provider — estimate from content length
            turn_output_tokens = max(1, len(full_content) // 4)

        # Aggregate across turns so tool-call flows get a sensible average.
        if gen_ms > 0 and turn_output_tokens > 0:
            total_gen_ms += gen_ms
            total_output_tokens += turn_output_tokens

        _log.info(
            f"turn {stats.turns}: finish={finish_reason}, content={len(full_content)}, "
            f"tools={len(tool_calls_data)}, ttft_ms={ttft_ms}, gen_ms={gen_ms}, "
            f"stream_ms={stream_ms}, out_tok={turn_output_tokens}"
        )

        # ── Process tool calls ──
        if tool_calls_data:
            # Add assistant message with tool calls
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
                # Layer 3: tool_search short-circuit
                if tc["name"] == "tool_search":
                    _tool_search_count += 1
                    if _tool_search_count > 1:
                        tool_result = (
                            "STOP: tools already activated. Do NOT call tool_search again. "
                            "Call the actual tool directly (e.g., browser_open, browser_snapshot)."
                        )
                        messages.append({"role": "tool", "tool_call_id": tc["id"], "content": tool_result})
                        continue

                # Parse arguments
                args = _parse_tool_args(tc["arguments"], json_repair_fn)
                if args is None:
                    tool_result = f"Error: invalid JSON arguments: {tc['arguments'][:100]}"
                    stats.add_error()
                else:
                    # Self-check for dangerous tools
                    if self_check_fn and args:
                        ok, fixed = self_check_fn(tc["name"], args)
                        if not ok and fixed:
                            args = fixed
                        elif not ok:
                            tool_result = "Error: self-check rejected this action."
                            stats.add_error()
                            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": tool_result})
                            continue

                    # Layer 4: per-tool frequency limit
                    _tool_name_counts[tc["name"]] = _tool_name_counts.get(tc["name"], 0) + 1
                    if _tool_name_counts[tc["name"]] > 5:
                        tool_result = f"LIMIT: {tc['name']} called too many times. Use the results you already have."
                        _force_finish = True
                        _log.warning(f"tool frequency limit: {tc['name']} called {_tool_name_counts[tc['name']]} times")
                        messages.append({"role": "tool", "tool_call_id": tc["id"], "content": tool_result})
                        continue

                    # Execute tool
                    emitter.tool_start(tc["name"], str(args)[:80])
                    stats.add_tool_call()
                    all_tool_calls.append(tc["name"])

                    tool_start = time.time()
                    try:
                        tool_result = tool_executor(tc["name"], args)
                    except Exception as e:
                        tool_result = f"Error: {e}"
                        stats.add_error()
                    tool_ms = int((time.time() - tool_start) * 1000)

                    # Fix 4: Cap tool results to prevent context overflow
                    tool_result = _cap_tool_result(tool_result)

                    result_short = tool_result.replace("\n", " ")[:150]
                    emitter.tool_end(tc["name"], result_short, tool_ms)

                # Append tool result to messages
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": tool_result,
                })

            # Layer 4: Loop detection — 2 identical calls = force finish
            _turn_sig = "|".join(f"{tc['name']}:{tc['arguments'][:60]}" for tc in tool_calls_data.values())
            _recent_tool_sigs.append(_turn_sig)
            if len(_recent_tool_sigs) > 5:
                _recent_tool_sigs.pop(0)
            if len(_recent_tool_sigs) >= 2 and len(set(_recent_tool_sigs[-2:])) == 1:
                _log.warning(f"loop detected: {_turn_sig[:80]}")
                _force_finish = True
                messages.append({"role": "user", "content":
                    "[system] STOP. You are in a loop. Give your final answer NOW based on what you already have. Do NOT call any more tools."})

            # Fix 3: Progress checkpoints — every 5 tool calls, force re-orientation
            if stats.tool_calls > 0 and stats.tool_calls % 5 == 0 and not _force_finish:
                _log.info(f"progress checkpoint at {stats.tool_calls} tool calls")
                messages.append({"role": "user", "content":
                    f"[system] Progress checkpoint ({stats.tool_calls} tool calls). "
                    f"Briefly: what have you accomplished so far? What remains? Continue with the next step."})

            continue  # Next turn

        # ── No tool calls — check finish reason ──
        # Clean up nudge messages from previous round (Layer 5)
        if _nudge_cleanup:
            if len(messages) >= 2 and messages[-1].get("role") == "user" and "[system]" in str(messages[-1].get("content", "")):
                messages.pop()  # remove nudge
                if messages and messages[-1].get("role") == "assistant":
                    messages.pop()  # remove hedge
            _nudge_cleanup = False

        # Strip thinking tags from content
        raw_reply = _strip_thinking(full_content)
        thinking_content = reasoning_content or _extract_thinking(full_content)

        # Layer 2: Try to extract tool call from text (model described it but didn't call it)
        if not _force_finish and tools and raw_reply:
            extracted = _extract_tool_from_text(full_content, _tool_names)
            if extracted:
                _log.info(f"extracted tool from text: {extracted[0]}({str(extracted[1])[:60]})")
                # Don't add the hedge text as final reply — execute the tool instead
                _synthesize_tool_call(extracted[0], extracted[1], tool_executor, messages, emitter, stats)
                all_tool_calls.append(extracted[0])
                continue  # Let LLM summarize the result

        # Layer 5: Anti-hedge — detect empty reply or hedge phrases
        _reply_is_empty = len(raw_reply.strip()) == 0 and (len(full_content) > 0 or len(reasoning_content) > 0)
        _hedge_phrases = ("let me", "i'll try", "i will", "давай", "поищу", "посмотрю",
                          "попробу", "let me search", "let me check", "i can help",
                          "based on my knowledge", "i would")
        _is_hedge = any(p in raw_reply.lower() for p in _hedge_phrases) if raw_reply else False

        if not _force_finish and _nudge_count < 2 and (_reply_is_empty or _is_hedge):
            _log.info(f"anti-hedge triggered: empty={_reply_is_empty}, hedge={_is_hedge}, nudge#{_nudge_count+1}")
            nudge_text = raw_reply if raw_reply.strip() else "Let me think about this..."
            messages.append({"role": "assistant", "content": nudge_text})
            messages.append({"role": "user", "content":
                "[system] STOP describing what you'll do. Actually CALL the tool right now. "
                "If you need web info, call browser_open(url). Reply with actual content, not plans."})
            _nudge_count += 1
            _nudge_cleanup = True
            emitter.status("nudging to act...")
            continue

        # Continuation: model was truncated
        if finish_reason in ("length", "max_tokens"):
            _log.info("response truncated, requesting continuation")
            messages.append({"role": "assistant", "content": full_content})
            messages.append({"role": "user", "content": "[system] Your response was truncated. Continue exactly where you left off."})
            emitter.status("continuing...")
            final_content += raw_reply
            continue

        # Normal finish — done
        final_content += raw_reply
        break

    # ── Build result ──
    # tok/s is measured from first-token-to-last-chunk across ALL turns,
    # which is the number users compare against llama.cpp / Ollama output.
    # Including TTFT (time-to-first-token) made this value 2-5× too low
    # on local models with large prompts.
    tok_per_sec = 0.0
    if total_gen_ms > 0 and total_output_tokens > 0:
        tok_per_sec = round(total_output_tokens / (total_gen_ms / 1000), 1)

    return {
        "reply": final_content.strip(),
        "thinking": thinking_content.strip(),
        "tool_calls": all_tool_calls,
        "stats": stats,
        "tok_per_sec": tok_per_sec,
        "prompt_tokens": getattr(usage, 'prompt_tokens', 0) if usage else 0,
        "completion_tokens": getattr(usage, 'completion_tokens', 0) if usage else 0,
    }


def _parse_tool_args(raw: str, repair_fn=None) -> dict | None:
    """Parse tool call arguments from JSON string."""
    if not raw or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
        return {}
    except json.JSONDecodeError:
        if repair_fn:
            repaired = repair_fn(raw)
            if repaired is not None:
                try:
                    parsed = json.loads(repaired)
                    if isinstance(parsed, dict):
                        return parsed
                except Exception:
                    pass
        _log.warning(f"failed to parse tool args: {raw[:100]}")
        return None


def _strip_thinking(text: str) -> str:
    """Remove thinking blocks from model output."""
    text = re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL)
    # Gemma <|channel>thought — extract reply after thought block
    if "<|channel>" in text:
        # Split by channel markers, take the last non-thought segment
        segments = re.split(r"<\|channel\>\w*\s*", text)
        # Filter out empty segments and take the last substantial one as reply
        reply_parts = [s.strip() for s in segments if s.strip() and len(s.strip()) > 5]
        text = reply_parts[-1] if reply_parts else ""
    text = re.sub(r"<\|[^>]+\>", "", text)
    return text.strip()


def _extract_thinking(text: str) -> str:
    """Extract thinking content from <think> tags."""
    match = re.search(r"<think>(.*?)</think>", text, re.DOTALL)
    return match.group(1).strip() if match else ""
