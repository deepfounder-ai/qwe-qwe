"""Shared utilities used across agent, agent_loop, tasks, etc."""

import re


def strip_thinking(text: str) -> str:
    """Remove thinking blocks from model output.

    Handles:
    - <think>...</think> tags (Qwen, Llama)
    - <|channel>thought... (Gemma)
    - Stray special tokens <|...|>
    """
    text = re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL)
    # Gemma <|channel>thought — extract reply after thought block
    if "<|channel>" in text:
        segments = re.split(r"<\|channel\>\w*\s*", text)
        reply_parts = [s.strip() for s in segments if s.strip() and len(s.strip()) > 5]
        text = reply_parts[-1] if reply_parts else ""
    text = re.sub(r"<\|[^>]+\>", "", text)
    return text.strip()


def extract_thinking(text: str) -> str:
    """Extract thinking content from <think> tags."""
    match = re.search(r"<think>(.*?)</think>", text, re.DOTALL)
    return match.group(1).strip() if match else ""
