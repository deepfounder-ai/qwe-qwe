"""Tests for `agent_loop._extract_tool_from_text`.

This is the layer-2 fallback that pulls a tool call out of the model's
*text* output when the provider didn't emit a native `delta.tool_calls`.
Five patterns are recognised — see the function's body for the exact
shapes. These tests pin each one and the negative cases.

Pattern 5 was added to fix #10 — some LM Studio / Ollama-served Qwen
variants emit `!<function_call:{"call": ..., "arguments": ...}>` and
without that pattern the call would render as raw text and the model
would loop forever ("infinite reply" symptom).
"""

from __future__ import annotations

from agent_loop import _extract_tool_from_text

TOOLS = {"browser_open", "memory_search", "shell", "write_file"}


# ── Pattern 1: <tool_call>{"name": "...", "arguments": {...}}</tool_call> ──

def test_pattern1_qwen_tool_call_block():
    text = '<tool_call>{"name": "shell", "arguments": {"command": "ls"}}</tool_call>'
    assert _extract_tool_from_text(text, TOOLS) == ("shell", {"command": "ls"})


def test_pattern1_unknown_tool_returns_none():
    text = '<tool_call>{"name": "not_a_real_tool", "arguments": {}}</tool_call>'
    assert _extract_tool_from_text(text, TOOLS) is None


# ── Pattern 2: tool_name({"key": "value"}) ──

def test_pattern2_function_call_with_dict():
    text = 'I will run shell({"command": "echo hi"}) for you.'
    assert _extract_tool_from_text(text, TOOLS) == ("shell", {"command": "echo hi"})


# ── Pattern 3: tool_name(key="value") ──

def test_pattern3_function_call_with_kwargs():
    text = 'Calling browser_open(url="https://example.com")'
    assert _extract_tool_from_text(text, TOOLS) == (
        "browser_open", {"url": "https://example.com"})


# ── Pattern 4: tool name mentioned + URL nearby (browser only) ──

def test_pattern4_browser_with_nearby_url():
    text = "Let me use browser_open and visit https://example.com"
    assert _extract_tool_from_text(text, TOOLS) == (
        "browser_open", {"url": "https://example.com"})


def test_pattern4_non_browser_tool_with_url_returns_none():
    # Pattern 4 only triggers for tools whose name contains "browser"
    text = "Run shell on https://example.com"
    assert _extract_tool_from_text(text, TOOLS) is None


# ── Pattern 5 (NEW): !<function_call:{"call": "...", "arguments": {...}}> ──

def test_pattern5_function_call_wrapper_with_call_key():
    # The exact format from #10 — uses "call" instead of "name"
    text = '!<function_call:{"call": "memory_search", "arguments": {"query": "user goals"}}>'
    assert _extract_tool_from_text(text, TOOLS) == (
        "memory_search", {"query": "user goals"})


def test_pattern5_accepts_name_key_too():
    # Defensive: if a model uses "name" inside this wrapper, accept it
    text = '!<function_call:{"name": "shell", "arguments": {"command": "pwd"}}>'
    assert _extract_tool_from_text(text, TOOLS) == ("shell", {"command": "pwd"})


def test_pattern5_arguments_null_becomes_empty_dict():
    # The original bug repro — arguments: null in the issue body
    text = '!<function_call:{"call": "memory_search", "arguments": null}>'
    assert _extract_tool_from_text(text, TOOLS) == ("memory_search", {})


def test_pattern5_arguments_string_becomes_empty_dict():
    # Defensive: if a model emits arguments as a string instead of object,
    # treat as empty rather than crashing or returning the string
    text = '!<function_call:{"call": "shell", "arguments": "ls -la"}>'
    assert _extract_tool_from_text(text, TOOLS) == ("shell", {})


def test_pattern5_unknown_tool_returns_none():
    text = '!<function_call:{"call": "magical_tool", "arguments": {}}>'
    assert _extract_tool_from_text(text, TOOLS) is None


def test_pattern5_missing_call_and_name_returns_none():
    text = '!<function_call:{"arguments": {"x": 1}}>'
    assert _extract_tool_from_text(text, TOOLS) is None


def test_pattern5_invalid_json_returns_none():
    text = '!<function_call:{not valid json}>'
    assert _extract_tool_from_text(text, TOOLS) is None


def test_pattern5_nested_dict_in_arguments():
    # The non-greedy `.*?` plus the literal `>` anchor after `\}` make
    # the regex correctly span the entire JSON even when arguments
    # contain a nested dict — guards against the obvious "lazy quantifier
    # would stop at first `}`" reading of the regex.
    text = ('!<function_call:{"call": "memory_search", '
            '"arguments": {"query": "x", "filter": {"tag": "user"}}}>')
    assert _extract_tool_from_text(text, TOOLS) == (
        "memory_search", {"query": "x", "filter": {"tag": "user"}})


def test_pattern5_in_middle_of_prose():
    # Real models often surround the tool call with explanatory text
    text = (
        "Let me search memory first.\n"
        '!<function_call:{"call": "memory_search", "arguments": {"query": "API keys"}}>\n'
        "Then I'll act on the result."
    )
    assert _extract_tool_from_text(text, TOOLS) == (
        "memory_search", {"query": "API keys"})


# ── General negatives ──

def test_empty_text_returns_none():
    assert _extract_tool_from_text("", TOOLS) is None


def test_empty_tool_names_returns_none():
    assert _extract_tool_from_text("any text", set()) is None


def test_plain_prose_returns_none():
    assert _extract_tool_from_text(
        "Hello, just a plain sentence with no tool call.", TOOLS) is None
