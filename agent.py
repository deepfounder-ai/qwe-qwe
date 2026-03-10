"""Core agent loop — the brain of qwe-qwe."""

import json, re, sys
from openai import OpenAI
from rich.console import Console
import config, db, tools, memory, soul

_client: OpenAI | None = None
_console = Console()


def _strip_thinking(text: str) -> str:
    """Remove <think>...</think> blocks from model output."""
    return re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL).strip()


def _extract_thinking(text: str) -> str | None:
    """Extract thinking content."""
    m = re.search(r"<think>(.*?)</think>", text, re.DOTALL)
    return m.group(1).strip() if m else None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(base_url=config.LLM_BASE_URL, api_key=config.LLM_API_KEY)
    return _client


def _auto_context(user_input: str) -> str:
    """Auto-retrieve relevant memories for the user's message."""
    try:
        results = memory.search(user_input, limit=config.MAX_MEMORY_RESULTS)
        if not results:
            return ""
        lines = ["[Relevant context from memory:]"]
        for r in results:
            if r["score"] > 0.3:  # only include if somewhat relevant
                lines.append(f"- [{r['tag']}] {r['text']}")
        if len(lines) == 1:
            return ""
        return "\n".join(lines)
    except Exception:
        return ""


def _build_messages(user_input: str) -> list[dict]:
    """Build minimal context: soul + auto-context + recent history + user message."""
    # Soul → compact system prompt
    agent_soul = soul.load()
    system_text = soul.to_prompt(agent_soul)

    # Auto-retrieve from Qdrant
    context = _auto_context(user_input)
    if context:
        system_text += "\n\n" + context

    msgs = [{"role": "system", "content": system_text}]

    # Recent history from SQLite (skip if it would create invalid sequences)
    history = db.get_recent_messages()

    # Ensure history starts with user (not assistant) after system
    while history and history[0]["role"] != "user":
        history.pop(0)

    # Remove trailing user messages (we'll add the new one)
    while history and history[-1]["role"] == "user":
        history.pop()

    msgs.extend(history)

    # New user message
    msgs.append({"role": "user", "content": user_input})
    return msgs


class TurnResult:
    """Result of one agent turn with debug info."""
    __slots__ = ("reply", "prompt_tokens", "completion_tokens", "total_tokens",
                 "tool_calls_made", "model", "auto_context_hits")

    def __init__(self):
        self.reply = ""
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.total_tokens = 0
        self.tool_calls_made: list[str] = []
        self.model = config.LLM_MODEL
        self.auto_context_hits = 0


def _maybe_compact():
    """Auto-compact: summarize old messages into memory when history gets long."""
    count = db.count_messages()
    if count < config.COMPACTION_THRESHOLD:
        return

    # Get oldest messages (keep recent ones)
    keep_recent = config.MAX_HISTORY_MESSAGES
    to_compact = db.get_oldest_messages(count - keep_recent)
    if len(to_compact) < 4:
        return

    # Build conversation text for summarization
    convo = "\n".join(f"{m['role']}: {m['content'][:200]}" for m in to_compact if m['content'])

    # Summarize via LLM
    client = _get_client()
    try:
        resp = client.chat.completions.create(
            model=config.LLM_MODEL,
            messages=[
                {"role": "system", "content": "Extract ONLY important facts from this conversation: user preferences, decisions, names, tasks, technical info. If nothing important — reply with just 'SKIP'. No greetings or chitchat. Be very concise."},
                {"role": "user", "content": convo},
            ],
            temperature=0.3,
            max_tokens=256,
        )
        summary = _strip_thinking(resp.choices[0].message.content or "")
        if summary and summary.strip().upper() != "SKIP":
            memory.save(summary, tag="session")

        # Cleanup old session summaries (>7 days)
        memory.cleanup(max_age_days=7, tag="session")

        # Delete compacted messages
        ids = [m["id"] for m in to_compact]
        db.delete_messages_by_ids(ids)
    except Exception:
        pass  # don't break the flow if compaction fails


def run(user_input: str) -> TurnResult:
    """Run one agent turn: user input → (tool loops) → final response."""
    client = _get_client()
    result = TurnResult()

    # Sanitize surrogates (WSL terminal issue)
    user_input = user_input.encode("utf-8", errors="replace").decode("utf-8")

    # Auto-compact if history is too long
    _maybe_compact()

    # Save user message
    db.save_message("user", user_input)

    messages = _build_messages(user_input)

    # Count auto-context hits (memories injected into system prompt)
    system_content = messages[0]["content"]
    if "[Relevant context from memory:]" in system_content:
        result.auto_context_hits = system_content.count("\n- [")

    rounds = 0

    while rounds < config.MAX_TOOL_ROUNDS:
        all_tools = tools.get_all_tools()

        # Stream the response
        stream = client.chat.completions.create(
            model=config.LLM_MODEL,
            messages=messages,
            tools=all_tools,
            tool_choice="auto",
            temperature=0.7,
            max_tokens=2048,
            stream=True,
        )

        # Collect streamed response
        full_content = ""
        tool_calls_data: dict[int, dict] = {}  # index -> {id, name, arguments}
        in_think = False
        think_shown = False
        finish_reason = None

        for chunk in stream:
            delta = chunk.choices[0].delta if chunk.choices else None
            if not delta:
                continue

            finish_reason = chunk.choices[0].finish_reason

            # Stream content (text)
            if delta.content:
                full_content += delta.content

                # Show thinking in real-time (dimmed)
                text = delta.content
                if "<think>" in text:
                    in_think = True
                    if not think_shown:
                        _console.print("  [dim]💭 ", end="")
                        think_shown = True
                    text = text.replace("<think>", "")
                if "</think>" in text:
                    in_think = False
                    text = text.replace("</think>", "")
                    _console.print("[/]", end="")

                if in_think and text.strip():
                    # Show thinking dimmed, truncated
                    clean = text.replace("\n", " ").strip()
                    if clean:
                        print(clean[:80], end="", flush=True)
                elif not in_think and not think_shown:
                    pass  # will be shown as final reply

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

        if think_shown:
            _console.print()  # newline after thinking

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

                # Show tool call
                try:
                    args = json.loads(tc["arguments"])
                    args_short = json.dumps(args, ensure_ascii=False)
                    if len(args_short) > 80:
                        args_short = args_short[:80] + "..."
                except Exception:
                    args = {}
                    args_short = tc["arguments"][:80]

                _console.print(f"  [cyan]🔧 {tc['name']}[/]([dim]{args_short}[/])")

                tool_result = tools.execute(tc["name"], args)

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
            continue

        # No tool calls — final response
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

        result.reply = raw_reply
        db.save_message("assistant", result.reply)

        # Track session tokens (estimate from content length since streaming doesn't give usage)
        est_tokens = len(full_content) // 4
        prev = int(db.kv_get("session_completion_tokens") or "0")
        db.kv_set("session_completion_tokens", str(prev + est_tokens))
        prev = int(db.kv_get("session_turns") or "0")
        db.kv_set("session_turns", str(prev + 1))

        return result

    result.reply = "I've used all my tool rounds for this turn."
    db.save_message("assistant", result.reply)
    return result
