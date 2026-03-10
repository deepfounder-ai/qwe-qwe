"""Core agent loop — the brain of qwe-qwe."""

import json, re
from openai import OpenAI
import config, db, tools


def _strip_thinking(text: str) -> str:
    """Remove <think>...</think> blocks from model output."""
    return re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL).strip()

_client: OpenAI | None = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(base_url=config.LLM_BASE_URL, api_key=config.LLM_API_KEY)
    return _client


def _build_messages(user_input: str) -> list[dict]:
    """Build minimal context: system + recent history + new user message."""
    msgs = [{"role": "system", "content": config.SYSTEM_PROMPT}]
    # Recent history from SQLite
    history = db.get_recent_messages()
    msgs.extend(history)
    # New user message
    msgs.append({"role": "user", "content": user_input})
    return msgs


class TurnResult:
    """Result of one agent turn with debug info."""
    __slots__ = ("reply", "prompt_tokens", "completion_tokens", "total_tokens",
                 "tool_calls_made", "model")

    def __init__(self):
        self.reply = ""
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.total_tokens = 0
        self.tool_calls_made: list[str] = []
        self.model = config.LLM_MODEL


def run(user_input: str) -> TurnResult:
    """Run one agent turn: user input → (tool loops) → final response."""
    client = _get_client()
    result = TurnResult()

    # Save user message
    db.save_message("user", user_input)

    messages = _build_messages(user_input)
    rounds = 0

    while rounds < config.MAX_TOOL_ROUNDS:
        resp = client.chat.completions.create(
            model=config.LLM_MODEL,
            messages=messages,
            tools=tools.TOOLS,
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
            # Add assistant message with tool calls
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

            # Execute each tool call
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
