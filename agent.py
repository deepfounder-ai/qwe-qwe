"""Core agent loop — the brain of qwe-qwe."""

import json, re
from openai import OpenAI
import config, db, tools, memory, soul

_client: OpenAI | None = None


def _strip_thinking(text: str) -> str:
    """Remove <think>...</think> blocks from model output."""
    return re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL).strip()


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

    # Recent history from SQLite
    history = db.get_recent_messages()
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


def run(user_input: str) -> TurnResult:
    """Run one agent turn: user input → (tool loops) → final response."""
    client = _get_client()
    result = TurnResult()

    # Sanitize surrogates (WSL terminal issue)
    user_input = user_input.encode("utf-8", errors="replace").decode("utf-8")
    
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
        resp = client.chat.completions.create(
            model=config.LLM_MODEL,
            messages=messages,
            tools=all_tools,
            tool_choice="auto",
            temperature=0.7,
            max_tokens=2048,
        )

        # Accumulate usage
        if resp.usage:
            result.prompt_tokens += resp.usage.prompt_tokens or 0
            result.completion_tokens += resp.usage.completion_tokens or 0
            result.total_tokens += resp.usage.total_tokens or 0

        choice = resp.choices[0]
        msg = choice.message

        # If model wants to call tools
        if msg.tool_calls:
            assistant_msg = {"role": "assistant", "content": msg.content or ""}
            assistant_msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in msg.tool_calls
            ]
            messages.append(assistant_msg)

            for tc in msg.tool_calls:
                result.tool_calls_made.append(tc.function.name)
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    args = {}

                tool_result = tools.execute(tc.function.name, args)

                tool_msg = {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": tool_result,
                }
                messages.append(tool_msg)

            rounds += 1
            continue

        # No tool calls — final response
        result.reply = _strip_thinking(msg.content or "")

        # Save to SQLite
        db.save_message("assistant", result.reply)

        # Track cumulative session tokens
        prev = int(db.kv_get("session_prompt_tokens") or "0")
        db.kv_set("session_prompt_tokens", str(prev + result.prompt_tokens))
        prev = int(db.kv_get("session_completion_tokens") or "0")
        db.kv_set("session_completion_tokens", str(prev + result.completion_tokens))
        prev = int(db.kv_get("session_turns") or "0")
        db.kv_set("session_turns", str(prev + 1))

        return result

    # Hit max rounds
    result.reply = "I've used all my tool rounds for this turn."
    db.save_message("assistant", result.reply)
    return result
