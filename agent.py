"""Core agent loop — the brain of NanoClaw."""

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


def run(user_input: str) -> str:
    """Run one agent turn: user input → (tool loops) → final response."""
    client = _get_client()

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
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    args = {}

                result = tools.execute(tc.function.name, args)

                tool_msg = {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                }
                messages.append(tool_msg)

            rounds += 1
            continue

        # No tool calls — final response
        reply = _strip_thinking(msg.content or "")

        # Save to SQLite
        if msg.tool_calls:
            tc_data = [
                {"id": tc.id, "name": tc.function.name, "arguments": tc.function.arguments}
                for tc in msg.tool_calls
            ]
            db.save_message("assistant", reply, tool_calls=tc_data)
        else:
            db.save_message("assistant", reply)

        return reply

    # Hit max rounds
    final = "I've used all my tool rounds for this turn. Here's what I have so far."
    db.save_message("assistant", final)
    return final
