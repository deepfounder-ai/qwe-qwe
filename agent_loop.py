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

    while True:
        # ── Budget check ──
        decision = check_budget(budget, stats)
        if decision.exceeded:
            _log.warning(f"budget exceeded: {decision.reason}")
            emitter.status(f"Budget: {decision.reason}")
            break

        # Budget warning to model
        warning = warning_check(budget, stats)
        if warning:
            messages.append({"role": "user", "content": f"[system] {warning}. Give final answer."})
            emitter.emit(AgentEvent(EVT_BUDGET_WARNING, {"message": warning}))

        stats.add_turn()
        emitter.emit(AgentEvent("turn_start", {"turn": stats.turns}))

        # ── Call LLM ──
        stream_start = time.time()
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
        for chunk in stream:
            # Usage from final chunk
            if hasattr(chunk, 'usage') and chunk.usage:
                usage = chunk.usage

            delta = chunk.choices[0].delta if chunk.choices else None
            if not delta:
                continue

            finish_reason = chunk.choices[0].finish_reason

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

                # Detect <think> tags in content
                if not in_think and "<think>" in full_content:
                    in_think = True
                    emitter.status("thinking...")
                elif in_think and "</think>" in full_content:
                    in_think = False
                    emitter.status("writing reply...")
                elif "<|channel>" in full_content and not in_think:
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

        stream_ms = int((time.time() - stream_start) * 1000)

        # Track tokens
        if usage:
            stats.add_tokens(
                input_tok=getattr(usage, 'prompt_tokens', 0),
                output_tok=getattr(usage, 'completion_tokens', 0),
            )

        _log.info(f"turn {stats.turns}: finish={finish_reason}, content={len(full_content)}, "
                   f"tools={len(tool_calls_data)}, stream_ms={stream_ms}")

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

                    # Execute tool
                    emitter.tool_start(tc["name"], json.dumps(args, ensure_ascii=False)[:80])
                    stats.add_tool_call()
                    all_tool_calls.append(tc["name"])

                    tool_start = time.time()
                    try:
                        tool_result = tool_executor(tc["name"], args)
                    except Exception as e:
                        tool_result = f"Error: {e}"
                        stats.add_error()
                    tool_ms = int((time.time() - tool_start) * 1000)

                    result_short = tool_result.replace("\n", " ")[:150]
                    emitter.tool_end(tc["name"], result_short, tool_ms)

                # Append tool result to messages
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": tool_result,
                })

            continue  # Next turn

        # ── No tool calls — check finish reason ──
        # Strip thinking tags from content
        raw_reply = _strip_thinking(full_content)
        thinking_content = reasoning_content or _extract_thinking(full_content)

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
    tok_per_sec = 0
    if usage and stream_ms > 0:
        output_tokens = getattr(usage, 'completion_tokens', 0)
        tok_per_sec = round(output_tokens / (stream_ms / 1000), 1)

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
    # Gemma <|channel>thought
    if text.strip().startswith("<|channel>thought"):
        lines = text.split("\n")
        reply_lines = []
        in_reply = False
        for line in lines:
            if line.strip().startswith("<|channel>"):
                continue
            if not in_reply and len(line.strip()) > 30 and any(c in line for c in "абвгдежзийклмнопрстуфхцчшщьыъэюяАБВ"):
                in_reply = True
            if in_reply:
                reply_lines.append(line)
        text = "\n".join(reply_lines) if reply_lines else re.sub(r"<\|channel\>thought\b\s*", "", text, flags=re.DOTALL)
    text = re.sub(r"<\|[^>]+\>", "", text)
    return text.strip()


def _extract_thinking(text: str) -> str:
    """Extract thinking content from <think> tags."""
    match = re.search(r"<think>(.*?)</think>", text, re.DOTALL)
    return match.group(1).strip() if match else ""
