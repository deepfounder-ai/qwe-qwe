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
import subprocess
import time
import db
import logger
import pricing
import providers
from agent_events import EventEmitter, AgentEvent, EVT_BUDGET_WARNING
from agent_budget import BudgetLimits, BudgetStats, check_budget, warning_check

_log = logger.get("loop")


def _classify_tool_error(exc: BaseException) -> str:
    """Map an exception class to one of telemetry.ERROR_KINDS.

    Privacy contract: classify the EXCEPTION TYPE only — never the message
    text. Messages can carry tool args, paths, or chat snippets, so we
    never look at `str(exc)`. Set: timeout / aborted / rate_limited /
    unauthorized / not_found / exception.
    """
    if isinstance(exc, subprocess.TimeoutExpired):
        return "timeout"
    if isinstance(exc, (KeyboardInterrupt, SystemExit)):
        return "aborted"
    name = type(exc).__name__
    if name in ("TimeoutError", "ReadTimeout", "ConnectTimeout"):
        return "timeout"
    if name in ("RateLimitError",):
        return "rate_limited"
    if name in ("FileNotFoundError",):
        return "not_found"
    if name in ("PermissionError",):
        return "unauthorized"
    return "exception"


def _emit_tool_error_telemetry(tool_name: str, error_kind: str) -> None:
    """Map tool name → category and emit a tool_error event. No-op when
    telemetry is disabled (default).

    Privacy: we send the bounded category (never the tool name) and the
    classified error_kind (never the exception message). The validator in
    `telemetry.track_event` rejects anything outside the enum.
    """
    try:
        import telemetry
        if not telemetry.enabled():
            return
        import tools as _tools
        cat = _tools.category_for_tool(tool_name)
        if cat not in telemetry.TOOL_CATEGORIES:
            cat = "other"
        kind = error_kind if error_kind in telemetry.ERROR_KINDS else "exception"
        telemetry.track_event("tool_error", {
            "tool_category": cat,
            "error_kind": kind,
        })
    except Exception as e:  # pragma: no cover — telemetry must never crash a turn
        _log.debug(f"telemetry tool_error: {e}")


def _run_tool(tool_executor, name: str, args: dict, abort_event, ctx=None) -> str:
    """Invoke a tool with the per-thread abort event set, then clear it.

    This lets blocking tools (shell, http_request) observe the abort signal
    without having to change the tool_executor signature. Using tools.py's
    threading.local-backed slot keeps concurrent turns (web + telegram)
    isolated from each other.

    ``ctx`` is stashed alongside the abort event so tools that want to emit
    status / tool_call events (none do today, but it's there if they grow
    that need) can reach the right client's queue.
    """
    try:
        import tools as _tools_mod
        _tools_mod._set_abort_event(abort_event)
        if ctx is not None and hasattr(_tools_mod, "_set_turn_ctx"):
            _tools_mod._set_turn_ctx(ctx)
    except Exception:
        pass
    try:
        return tool_executor(name, args)
    finally:
        try:
            import tools as _tools_mod
            _tools_mod._set_abort_event(None)
            if hasattr(_tools_mod, "_set_turn_ctx"):
                _tools_mod._set_turn_ctx(None)
        except Exception:
            pass


# ── Text-to-Tool Extraction ──

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

    # Pattern 5: !<function_call:{"call": "name", "arguments": {...}}>
    # Some LM Studio / Ollama-served models (notably certain Qwen variants)
    # emit tool calls in this wrapper format with "call" instead of "name".
    # Without this branch, the call is rendered as raw text and the model
    # never observes a tool result, often retrying forever — observed as
    # "infinite reply" symptom. Closes #10.
    m = re.search(r'!<function_call:(\{.*?\})>', text, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(1))
            fn_name = data.get("call") or data.get("name")
            args = data.get("arguments")
            if not isinstance(args, dict):
                args = {}
            if fn_name in tool_names:
                return (fn_name, args)
        except (json.JSONDecodeError, AttributeError):
            pass

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

    The summary carries *no* bytes of the original tool output — just a length
    and a tool name — so a tool that accidentally printed a secret can't leak
    it back to the model via the cleared stub. To recover the tool name we walk
    back to the preceding assistant message and map ``tool_call_id`` → function
    name. If a ``name`` field was attached to the tool message directly (some
    clients do), we prefer that.
    """
    tool_indices = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    if len(tool_indices) <= _KEEP_RECENT_TOOL_RESULTS:
        return  # nothing to clear

    # Build tool_call_id -> function name map from assistant messages
    id_to_name: dict[str, str] = {}
    for m in messages:
        if m.get("role") != "assistant":
            continue
        for tc in m.get("tool_calls") or []:
            tcid = tc.get("id")
            fname = ((tc.get("function") or {}).get("name")) or tc.get("name")
            if tcid and fname:
                id_to_name[tcid] = fname

    # Clear all but the last N
    to_clear = tool_indices[:-_KEEP_RECENT_TOOL_RESULTS]
    for idx in to_clear:
        m = messages[idx]
        content = m.get("content", "")
        if isinstance(content, str) and content.startswith("[cleared"):
            continue  # already cleared
        tool_name = (
            m.get("name")
            or id_to_name.get(m.get("tool_call_id", ""))
            or "tool"
        )
        n = len(content) if isinstance(content, str) else 0
        m["content"] = f"[cleared — {n} chars of {tool_name} output]"


def _cap_tool_result(result: str) -> str:
    """Cap tool result to prevent one large output from eating the entire context."""
    n = len(result)
    if n <= _TOOL_RESULT_MAX_CHARS:
        return result
    return result[:_TOOL_RESULT_MAX_CHARS] + f"\n... [truncated at {_TOOL_RESULT_MAX_CHARS} chars, {n} total]"


def _pre_dispatch_safety_check(tool_name: str, args: dict, self_check_fn=None) -> str | None:
    """Gate-check a tool call before dispatch — returns a rejection message
    if the call should NOT execute, or None to allow.

    Applied to both native (``delta.tool_calls``) and text-extracted tool
    calls so the extraction path can't bypass the shell/write_file safety
    checks that the native path goes through.

    Checks, in order:
    1. ``self_check_fn`` (if provided) — same function the native path uses.
       A returned ``(False, None)`` means hard-reject; ``(False, fixed)`` is
       returned for the caller to swap args.
    2. For ``shell``: route through ``tools._check_shell_safety`` on the
       ``command`` argument.
    3. For ``write_file``: route through ``tools._resolve_path(raw, for_write=True)``
       so the workspace whitelist catches writes outside ~/.castor/ / cwd.

    Returns None on allow, str on reject. Never raises.
    """
    if not isinstance(args, dict):
        return "Blocked: arguments must be a JSON object."
    # Shell pre-check — speed-bump against obviously-dangerous commands.
    if tool_name == "shell":
        cmd = args.get("command") or ""
        if not isinstance(cmd, str) or not cmd.strip():
            return "Blocked: missing shell command."
        try:
            import tools as _tools
            reason = _tools._check_shell_safety(cmd)
        except Exception as e:
            _log.warning(f"shell safety check crashed: {e}")
            reason = None
        if reason:
            return reason
    # write_file pre-check — enforce workspace whitelist.
    if tool_name == "write_file":
        try:
            import tools as _tools
            raw = _tools._get_path_arg(args)
            if not raw:
                return "Blocked: write_file missing path argument."
            # Raises PermissionError if outside whitelist.
            _tools._resolve_path(str(raw), for_write=True)
        except PermissionError as e:
            return f"Blocked: {e}"
        except Exception as e:
            # Don't fail-closed on unrelated path errors — let the tool
            # itself surface the real error message.
            _log.warning(f"write_file pre-check skipped: {e}")
    return None


def _synthesize_tool_call(tool_name: str, args: dict, tool_executor, messages: list, emitter, stats,
                          abort_event=None, self_check_fn=None, ctx=None) -> str:
    """Execute a tool call that was detected from text/intent (not native function calling).
    Injects proper messages into the conversation and returns the tool result.

    Text-extracted tool calls are routed through the SAME safety gate as
    native tool calls (``_pre_dispatch_safety_check``) before dispatch so
    the extraction path can't be used to bypass shell-safety / write
    whitelisting. A blocked call still produces a synthetic assistant+tool
    message pair so the conversation stays well-formed and the model sees
    a clear "rejected" status to react to.
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

    # Gate: run the shared pre-dispatch safety check first.
    block_reason = _pre_dispatch_safety_check(tool_name, args, self_check_fn=self_check_fn)
    # Self-check hook — mirrors the native-tool-call path in run_loop().
    if block_reason is None and self_check_fn:
        try:
            ok, fixed = self_check_fn(tool_name, args)
        except Exception as e:
            _log.warning(f"self-check hook raised: {e}")
            ok, fixed = True, None  # fail open on errors, as native path does
        if not ok:
            if fixed and isinstance(fixed, dict):
                args = fixed
                # Re-run safety checks on the corrected args — a self-check
                # fix must not relax the shell/write_file gate.
                block_reason = _pre_dispatch_safety_check(tool_name, args, self_check_fn=None)
            else:
                block_reason = "Self-check rejected this action."

    if block_reason:
        _log.warning(f"text-extracted {tool_name} rejected: {block_reason}")
        emitter.tool_start(tool_name, str(args)[:80])
        result = f"Rejected (extracted-tool safety gate): {block_reason}"
        emitter.tool_end(tool_name, result[:150], 0)
        stats.add_error()
        _emit_tool_error_telemetry(tool_name, "blocked")
        messages.append({"role": "tool", "tool_call_id": call_id, "content": result})
        return result

    # Execute
    emitter.tool_start(tool_name, str(args)[:80])
    stats.add_tool_call()

    tool_start = time.time()
    try:
        result = _run_tool(tool_executor, tool_name, args, abort_event, ctx=ctx)
    except Exception as e:
        result = f"Error: {e}"
        stats.add_error()
        _emit_tool_error_telemetry(tool_name, _classify_tool_error(e))
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
    abort_event=None,
    ctx=None,
    thread_id: str | None = None,
    system_note: str | None = None,
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
        abort_event: per-turn abort signal; takes precedence over ctx.abort_event
            when both are given (back-compat).
        ctx: optional :class:`turn_context.TurnContext`. When given, its
            ``abort_event`` is used if ``abort_event=`` wasn't passed, and the
            ctx is stashed on the tools module so tool executions can reach it.
        system_note: optional one-shot system message injected once at the start
            of this run, right after the soul system prompt (messages[0]). NOT
            persisted, NOT carried into subsequent turns. Used by
            resume_interrupted_run to nudge the model without injecting a
            [system] user-role message (CLAUDE.md OpenCode lesson).

    Returns:
        dict with: reply, thinking, tool_calls, finish_reason, stats
    """
    # Prefer explicit abort_event arg, but fall back to ctx.abort_event so new
    # callers only need to pass ctx.
    if abort_event is None and ctx is not None:
        abort_event = getattr(ctx, "abort_event", None)
    if budget is None:
        budget = BudgetLimits.from_config()
    if tool_executor is None:
        import tools as _tools
        tool_executor = _tools.execute

    # Inject system_note as a one-shot system message right after the soul
    # system prompt (messages[0]). This must happen before any LLM call so
    # the model sees it on the first (and only) turn it matters. NOT
    # persisted — lives only in this local messages list copy.
    if system_note:
        _insert_idx = 1 if messages and messages[0].get("role") == "system" else 0
        messages = list(messages)  # shallow copy — don't mutate caller's list
        messages.insert(_insert_idx, {"role": "system", "content": system_note})

    # ── Agent-run instrumentation ──────────────────────────────────────────
    _run_started = time.time()
    _provider = providers.get_active_name()
    _thread_id = db._tid(thread_id)  # resolve: explicit > active > default
    # Insert the row BEFORE entering the try/finally so that if the INSERT
    # itself raises, the finally block never calls finalize_agent_run(None, …).
    # Use a sentinel None so the finally block can guard on it.
    _run_id: int | None = None
    try:
        _run_id = db.insert_agent_run(
            thread_id=_thread_id,
            source=(ctx.source if ctx else "cli"),
            started_at=_run_started,
            status="running",
            cron_id=(ctx.cron_id if ctx else None),
            model=model,
            provider=_provider,
            resumed_from_run_id=(ctx.resumed_from_run_id if ctx else None),
        )
    except Exception as _ins_err:
        _log.warning(f"agent_runs INSERT failed (analytics disabled for this run): {_ins_err}")
    _final_status = "ok"
    _final_error: str | None = None

    stats = BudgetStats()
    all_tool_calls: list[str] = []
    all_tool_details: list[dict] = []  # {name, args, result} for history
    final_content = ""
    thinking_content = ""
    extra = extra_kwargs or {}
    _budget_warned = False
    from collections import deque
    _recent_tool_sigs: deque[str] = deque(maxlen=5)  # loop detection: O(1) append+evict
    _tool_name_counts: dict[str, int] = {}  # Layer 4: per-tool frequency
    _tool_search_count = 0  # Layer 3: tool_search call counter
    _nudge_count = 0  # Layer 5: anti-hedge nudge counter
    _nudge_cleanup = False  # Layer 5: cleanup nudge messages on next iteration
    _force_finish = False  # Layer 4: force finish after loop detection
    _tool_names = _get_tool_names(tools) if tools else set()


    # Aggregated generation time across all turns — time from FIRST content
    # token of each turn's stream to the LAST chunk. Excludes time-to-first-token
    # (prompt processing) so tok/s reflects actual generation speed.
    total_gen_ms = 0
    total_output_tokens = 0

    try:
        while True:
            # ── Budget check ──
            decision = check_budget(budget, stats)
            if decision.exceeded:
                _log.warning(f"budget exceeded: {decision.reason}")
                emitter.status(f"Budget: {decision.reason}")
                # Generate summary so model remembers what it did
                if not final_content and all_tool_calls:
                    tools_summary = ", ".join(dict.fromkeys(all_tool_calls))  # unique, ordered
                    final_content = f"[Task completed with {len(all_tool_calls)} tool calls: {tools_summary}. Budget limit reached.]"
                break

            # Abort check — user pressed Stop
            if abort_event and abort_event.is_set():
                _log.info("abort: user requested stop")
                final_content = "⏹ Stopped."
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
                # Abort mid-stream
                if abort_event and abort_event.is_set():
                    _log.info("abort: stopping stream mid-generation")
                    break
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
                # Add assistant message with tool calls. The `arguments`
                # field is normalized to valid JSON before persistence:
                # streaming can leave us with `""` (no args were streamed)
                # or fragmented payloads, and providers like Alibaba
                # DashScope strictly validate and 400 on anything that
                # isn't parseable JSON. See `normalize_args_for_api`.
                assistant_msg = {"role": "assistant", "content": full_content}
                assistant_msg["tool_calls"] = [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": normalize_args_for_api(tc["arguments"], json_repair_fn),
                        },
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
                        _emit_tool_error_telemetry(tc["name"], "validation_failed")
                    else:
                        # Self-check for dangerous tools
                        if self_check_fn and args:
                            ok, fixed = self_check_fn(tc["name"], args)
                            if not ok and fixed:
                                args = fixed
                            elif not ok:
                                tool_result = "Error: self-check rejected this action."
                                stats.add_error()
                                _emit_tool_error_telemetry(tc["name"], "blocked")
                                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": tool_result})
                                continue

                        # Pre-dispatch safety gate — same checks text-extracted
                        # calls go through. Catches dangerous shell commands and
                        # out-of-whitelist write_file paths that a faulty or
                        # prompt-injected ``self_check_fn`` might have let
                        # through (or that ran with no self_check_fn at all).
                        _block = _pre_dispatch_safety_check(tc["name"], args, self_check_fn=None)
                        if _block:
                            _log.warning(f"native {tc['name']} rejected by pre-dispatch: {_block}")
                            tool_result = f"Rejected: {_block}"
                            stats.add_error()
                            _emit_tool_error_telemetry(tc["name"], "blocked")
                            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": tool_result})
                            continue

                        # Track per-tool call count (for logging only — no hard limit)
                        _tool_name_counts[tc["name"]] = _tool_name_counts.get(tc["name"], 0) + 1

                        # Execute tool
                        args_preview = str(args)[:200]
                        emitter.tool_start(tc["name"], args_preview)
                        stats.add_tool_call()
                        all_tool_calls.append(tc["name"])

                        tool_start = time.time()
                        try:
                            tool_result = _run_tool(tool_executor, tc["name"], args, abort_event, ctx=ctx)
                        except Exception as e:
                            tool_result = f"Error: {e}"
                            stats.add_error()
                            _emit_tool_error_telemetry(tc["name"], _classify_tool_error(e))
                        tool_ms = int((time.time() - tool_start) * 1000)

                        # Fix 4: Cap tool results to prevent context overflow
                        tool_result = _cap_tool_result(tool_result)

                        result_short = tool_result.replace("\n", " ")[:200]
                        emitter.tool_end(tc["name"], result_short, tool_ms)
                        all_tool_details.append({"name": tc["name"], "args": args_preview, "result": result_short})

                    # Append tool result to messages
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": tool_result,
                    })

                # Layer 4: Loop detection — 2 identical calls = force finish
                _turn_sig = "|".join(f"{tc['name']}:{tc['arguments'][:200]}" for tc in tool_calls_data.values())
                _recent_tool_sigs.append(_turn_sig)  # deque auto-evicts oldest
                if len(_recent_tool_sigs) >= 2 and _recent_tool_sigs[-1] == _recent_tool_sigs[-2]:
                    _log.warning(f"loop detected: {_turn_sig[:80]}")
                    _force_finish = True
                    messages.append({"role": "user", "content":
                        "[system] STOP. You are in a loop. Give your final answer NOW based on what you already have. Do NOT call any more tools."})

                # Reset nudge counter after a productive tool-call turn so a later
                # empty-stop can be nudged again (without this, nudge fires at most
                # once per entire loop even if the agent does 20 more turns of work
                # between two separate silent-stop events).
                _nudge_count = 0

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
                    # Don't add the hedge text as final reply — execute the tool
                    # instead. Route through the pre-dispatch safety gate so a
                    # ``<tool_call>`` regex hit can't bypass shell-safety /
                    # write-whitelisting, AND pass the per-request abort_event so
                    # Stop works mid-blocking-tool.
                    _synthesize_tool_call(
                        extracted[0], extracted[1],
                        tool_executor, messages, emitter, stats,
                        abort_event=abort_event,
                        self_check_fn=self_check_fn,
                        ctx=ctx,
                    )
                    all_tool_calls.append(extracted[0])
                    continue  # Let LLM summarize the result

            # Layer 5: Anti-hedge — ONLY for truly empty replies (thinking but no output)
            # Minimal intervention — don't inject [system] user messages that break model flow.
            # Also fires after _force_finish: model sometimes responds to the "STOP" nudge with
            # pure reasoning tokens and no text — without this second chance the user sees nothing.
            _reply_is_empty = len(raw_reply.strip()) == 0 and (len(full_content) > 0 or len(reasoning_content) > 0)

            if _nudge_count < 1 and _reply_is_empty:
                _log.info(f"empty reply after thinking — retrying (nudge #{_nudge_count+1})")
                # After force_finish the model already received a "STOP" instruction; use a
                # different nudge that asks for text rather than continuing tool use.
                if _force_finish:
                    nudge_text = "Please write your final answer as a text message to the user."
                else:
                    nudge_text = "I need to continue working on this."
                messages.append({"role": "assistant", "content": nudge_text})
                _nudge_count += 1
                _nudge_cleanup = True
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
            "tool_details": all_tool_details,
            "stats": stats,
            "tok_per_sec": tok_per_sec,
            "prompt_tokens": getattr(usage, 'prompt_tokens', 0) if usage else 0,
            "completion_tokens": getattr(usage, 'completion_tokens', 0) if usage else 0,
        }
    except Exception as _e:
        _final_status = "err"
        _final_error = str(_e)[:500]
        raise
    finally:
        _finished = time.time()
        if ctx and getattr(ctx, "abort_event", None) and ctx.abort_event.is_set() and _final_status == "ok":
            _final_status = "aborted"
        _is_aborted = (_final_status == "aborted")

        # Flush partial assistant content as a message row so resume sees it
        # in conversation history. On clean exit, agent.py's reply-save path
        # handles this — skip here to avoid duplicates.
        if _is_aborted and final_content:
            try:
                db.save_message(
                    role="assistant",
                    content=final_content,
                    thread_id=_thread_id,
                    meta={
                        "interrupted": True,
                        "run_id": _run_id,
                        "partial_tokens": {
                            "input": int(stats.input_tokens or 0),
                            "output": int(stats.output_tokens or 0),
                        },
                    },
                )
            except Exception as e:
                _log.debug(f"interrupt flush failed: {e}")

        _cost = None
        try:
            _cost = pricing.compute_cost(model, stats.input_tokens, stats.output_tokens)
        except Exception:
            pass
        if _run_id is not None:
            db.finalize_agent_run(
                run_id=_run_id,
                finished_at=(None if _is_aborted else _finished),
                duration_ms=(None if _is_aborted else int((_finished - _run_started) * 1000)),
                status=_final_status,
                error=_final_error,
                result_preview=final_content.strip()[:200],
                input_tokens=stats.input_tokens,
                output_tokens=stats.output_tokens,
                cost_usd=_cost,
            )


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


def normalize_args_for_api(raw: str, repair_fn=None) -> str:
    """Return a JSON-string suitable for the assistant message's
    ``function.arguments`` field. Always returns valid JSON.

    The streaming accumulation in delta.tool_calls.function.arguments
    can leave us with an empty string (model emitted no args), or a
    fragmented string that's not parseable as-is. OpenAI's API
    silently treats ``""`` as ``"{}"``, but **Alibaba DashScope
    strictly validates** and 400s with ``InternalError.Algo.
    InvalidParameter: The "function.arguments" parameter of the code
    model must be in JSON format.``

    Mirroring OpenAI's lenient behavior client-side here keeps every
    strict-validation provider happy without per-provider branches:

      - empty / whitespace        →  ``"{}"``
      - valid JSON object         →  re-serialized canonical form
      - malformed but repairable  →  repaired JSON
      - truly broken              →  ``"{}"`` (caller's executor
                                       still sees the raw string via
                                       its own ``_parse_tool_args`` call
                                       and surfaces a tool_result error)
    """
    parsed = _parse_tool_args(raw, repair_fn)
    if parsed is None:
        return "{}"
    try:
        return json.dumps(parsed, ensure_ascii=False)
    except Exception:
        return "{}"


from utils import strip_thinking as _strip_thinking, extract_thinking as _extract_thinking
