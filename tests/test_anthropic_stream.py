"""Tests for providers_anthropic_stream.reassemble_anthropic_stream().

Feeds scripted Anthropic SSE event lists into the reassembler and asserts the
emitted chunks satisfy the OpenAI ChatCompletionChunk duck-type contract that
agent_loop.run_loop() consumes (see docs/specs/2026-05-17-native-anthropic-adapter.md
section "OpenAI shape `agent_loop` consumes").
"""

from __future__ import annotations

import pytest

from providers_anthropic_stream import (
    _Chunk,
    _Choice,
    _Delta,
    _ToolCallDelta,
    _ToolFnDelta,
    reassemble_anthropic_stream,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _collect(events):
    """Drive the iterator to a concrete list."""
    return list(reassemble_anthropic_stream(events))


def _text_stream_events(deltas, stop_reason="end_turn"):
    """Build a minimal text-only Anthropic stream event sequence."""
    events = [
        {
            "type": "message_start",
            "message": {"id": "msg_test", "model": "claude-sonnet-4-5"},
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        },
    ]
    for text in deltas:
        events.append(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": text},
            }
        )
    events.extend(
        [
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                "usage": {"output_tokens": 42},
            },
            {"type": "message_stop"},
        ]
    )
    return events


# ---------------------------------------------------------------------------
# Duck-type contract — chunks satisfy the OpenAI shape agent_loop reads
# ---------------------------------------------------------------------------


def test_chunk_satisfies_agent_loop_duck_type():
    """Critical: agent_loop accesses chunk.choices[0].delta.{content,reasoning,
    tool_calls} and chunk.choices[0].finish_reason via attribute access.
    """
    chunks = _collect(_text_stream_events(["hi"]))
    assert chunks, "stream produced no chunks"
    chunk = chunks[0]
    # Attribute access must work (would AttributeError on a dict-only shape)
    assert isinstance(chunk.choices, list)
    assert len(chunk.choices) == 1
    assert hasattr(chunk.choices[0], "delta")
    assert hasattr(chunk.choices[0], "finish_reason")
    assert hasattr(chunk.choices[0].delta, "content")
    assert hasattr(chunk.choices[0].delta, "reasoning")
    assert hasattr(chunk.choices[0].delta, "tool_calls")
    assert hasattr(chunk.choices[0].delta, "role")


def test_chunk_dataclass_types():
    """Emitted chunks are the documented dataclasses (not raw dicts)."""
    chunks = _collect(_text_stream_events(["x"]))
    for chunk in chunks:
        assert isinstance(chunk, _Chunk)
        for ch in chunk.choices:
            assert isinstance(ch, _Choice)
            assert isinstance(ch.delta, _Delta)


# ---------------------------------------------------------------------------
# Empty / ping / error
# ---------------------------------------------------------------------------


def test_empty_stream_yields_nothing():
    assert _collect([]) == []


def test_empty_stream_does_not_crash():
    # Iterator must be exhaustible cleanly.
    it = reassemble_anthropic_stream(iter([]))
    with pytest.raises(StopIteration):
        next(it)


def test_ping_events_are_ignored():
    events = [
        {"type": "ping"},
        {"type": "ping"},
        {"type": "ping"},
    ]
    assert _collect(events) == []


def test_ping_interleaved_with_content_does_not_emit_extra():
    events = [
        {"type": "message_start", "message": {"id": "m", "model": "claude"}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
        {"type": "ping"},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hi"}},
        {"type": "ping"},
        {"type": "content_block_stop", "index": 0},
    ]
    chunks = _collect(events)
    # message_start (role) + one text delta = 2 chunks. No extras from pings.
    assert len(chunks) == 2


def test_error_event_raises_runtime_error():
    events = [
        {"type": "message_start", "message": {"id": "m", "model": "claude"}},
        {"type": "error", "error": {"type": "overloaded_error", "message": "Server overloaded"}},
    ]
    with pytest.raises(RuntimeError, match="Server overloaded"):
        _collect(events)


def test_error_event_without_message_still_raises():
    events = [{"type": "error", "error": {}}]
    with pytest.raises(RuntimeError):
        _collect(events)


# ---------------------------------------------------------------------------
# message_start kickoff
# ---------------------------------------------------------------------------


def test_message_start_emits_role_assistant_kickoff():
    events = [{"type": "message_start", "message": {"id": "msg_1", "model": "claude-sonnet-4-6"}}]
    chunks = _collect(events)
    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.choices[0].delta.role == "assistant"
    assert chunk.choices[0].delta.content is None
    assert chunk.choices[0].delta.tool_calls is None
    assert chunk.id == "msg_1"
    assert chunk.model == "claude-sonnet-4-6"


def test_only_first_chunk_carries_role():
    chunks = _collect(_text_stream_events(["one", "two"]))
    # First chunk = role kickoff, rest = content / finish_reason.
    assert chunks[0].choices[0].delta.role == "assistant"
    for chunk in chunks[1:]:
        assert chunk.choices[0].delta.role is None


def test_id_and_model_propagate_to_subsequent_chunks():
    chunks = _collect(_text_stream_events(["x"]))
    for chunk in chunks:
        assert chunk.id == "msg_test"
        assert chunk.model == "claude-sonnet-4-5"


# ---------------------------------------------------------------------------
# Pure text stream
# ---------------------------------------------------------------------------


def test_pure_text_stream_five_deltas():
    """Spec requirement: 5 content_block_delta events -> chunks with content
    deltas in order."""
    deltas = ["Hello ", "world", "! ", "This is ", "Claude."]
    chunks = _collect(_text_stream_events(deltas))
    # Filter to content-bearing chunks (skip role kickoff + finish_reason).
    content_chunks = [c for c in chunks if c.choices[0].delta.content is not None]
    assert len(content_chunks) == 5
    collected = [c.choices[0].delta.content for c in content_chunks]
    assert collected == deltas


def test_text_chunk_has_no_tool_calls_or_reasoning():
    chunks = _collect(_text_stream_events(["hi"]))
    text_chunk = next(c for c in chunks if c.choices[0].delta.content is not None)
    assert text_chunk.choices[0].delta.tool_calls is None
    assert text_chunk.choices[0].delta.reasoning is None


def test_empty_text_delta_emits_no_chunk():
    """An empty-string text_delta should not be emitted (defends against
    extra chunks the consumer would need to filter)."""
    events = [
        {"type": "message_start", "message": {"id": "m", "model": "c"}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "x"}},
        {"type": "content_block_stop", "index": 0},
    ]
    chunks = _collect(events)
    content_chunks = [c for c in chunks if c.choices[0].delta.content is not None]
    assert len(content_chunks) == 1
    assert content_chunks[0].choices[0].delta.content == "x"


# ---------------------------------------------------------------------------
# Tool-use stream
# ---------------------------------------------------------------------------


def test_tool_use_kickoff_carries_id_and_name_with_empty_args():
    events = [
        {"type": "message_start", "message": {"id": "m", "model": "claude"}},
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "tool_use", "id": "toolu_abc", "name": "shell"},
        },
        {"type": "content_block_stop", "index": 0},
    ]
    chunks = _collect(events)
    tool_chunks = [c for c in chunks if c.choices[0].delta.tool_calls]
    assert len(tool_chunks) == 1
    tc = tool_chunks[0].choices[0].delta.tool_calls[0]
    assert isinstance(tc, _ToolCallDelta)
    assert tc.index == 0
    assert tc.id == "toolu_abc"
    assert isinstance(tc.function, _ToolFnDelta)
    assert tc.function.name == "shell"
    assert tc.function.arguments == ""
    assert tc.type == "function"


def test_tool_use_three_input_json_deltas_continuation_chunks_have_only_arguments():
    """Spec requirement: tool-use stream with kickoff + 3 input_json_delta
    events -> chunks where first has id+name, rest have only arguments."""
    events = [
        {"type": "message_start", "message": {"id": "m", "model": "claude"}},
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "tool_use", "id": "toolu_xyz", "name": "write_file"},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": '{"path"'},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": ': "foo.md", '},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": '"content": "hi"}'},
        },
        {"type": "content_block_stop", "index": 0},
    ]
    chunks = _collect(events)
    tool_chunks = [c for c in chunks if c.choices[0].delta.tool_calls]
    assert len(tool_chunks) == 4  # 1 kickoff + 3 continuations

    # First chunk = kickoff.
    kickoff_tc = tool_chunks[0].choices[0].delta.tool_calls[0]
    assert kickoff_tc.id == "toolu_xyz"
    assert kickoff_tc.function.name == "write_file"
    assert kickoff_tc.function.arguments == ""

    # Continuation chunks have only arguments, no id, no name.
    for idx, chunk in enumerate(tool_chunks[1:]):
        tc = chunk.choices[0].delta.tool_calls[0]
        assert tc.id is None, f"continuation chunk {idx} unexpectedly carried id"
        assert tc.function.name is None, f"continuation chunk {idx} unexpectedly carried name"
        assert tc.function.arguments is not None

    # Reassembling the arguments yields the full JSON.
    all_args = "".join(
        chunk.choices[0].delta.tool_calls[0].function.arguments
        for chunk in tool_chunks[1:]
    )
    assert all_args == '{"path": "foo.md", "content": "hi"}'


def test_tool_use_continuation_preserves_index():
    """Continuation chunks must carry the same block index as the kickoff
    so the consumer's tool_calls_data accumulator picks the right slot."""
    events = [
        {"type": "message_start", "message": {"id": "m", "model": "claude"}},
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "tool_use", "id": "t1", "name": "shell"},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": "{}"},
        },
    ]
    chunks = _collect(events)
    tool_chunks = [c for c in chunks if c.choices[0].delta.tool_calls]
    for chunk in tool_chunks:
        assert chunk.choices[0].delta.tool_calls[0].index == 0


# ---------------------------------------------------------------------------
# Parallel tool use
# ---------------------------------------------------------------------------


def test_parallel_tool_use_preserves_indices():
    """Spec requirement: multiple tool calls in one message (parallel tool
    use) -> indices 0, 1, ... preserved."""
    events = [
        {"type": "message_start", "message": {"id": "m", "model": "claude"}},
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "tool_use", "id": "t0", "name": "shell"},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": "{}"},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "tool_use", "id": "t1", "name": "read_file"},
        },
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "input_json_delta", "partial_json": '{"path":"x"}'},
        },
        {"type": "content_block_stop", "index": 1},
    ]
    chunks = _collect(events)
    tool_chunks = [c for c in chunks if c.choices[0].delta.tool_calls]
    # Group by index.
    by_index = {}
    for chunk in tool_chunks:
        tc = chunk.choices[0].delta.tool_calls[0]
        by_index.setdefault(tc.index, []).append(tc)
    assert set(by_index.keys()) == {0, 1}
    # Each had its own kickoff (id + name).
    assert by_index[0][0].id == "t0"
    assert by_index[0][0].function.name == "shell"
    assert by_index[1][0].id == "t1"
    assert by_index[1][0].function.name == "read_file"


def test_parallel_tool_use_continuation_routes_to_correct_index():
    """Block indices must route deltas correctly when interleaved."""
    events = [
        {"type": "message_start", "message": {"id": "m", "model": "claude"}},
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "tool_use", "id": "t0", "name": "a"},
        },
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "tool_use", "id": "t1", "name": "b"},
        },
        # Anthropic in practice interleaves rarely, but the reassembler
        # must handle it.
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": '{"x":'},
        },
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "input_json_delta", "partial_json": '{"y":'},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": "1}"},
        },
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "input_json_delta", "partial_json": "2}"},
        },
    ]
    chunks = _collect(events)
    args_by_index = {0: [], 1: []}
    for chunk in chunks:
        if not chunk.choices[0].delta.tool_calls:
            continue
        tc = chunk.choices[0].delta.tool_calls[0]
        if tc.function and tc.function.arguments and tc.function.name is None:
            args_by_index[tc.index].append(tc.function.arguments)
    assert "".join(args_by_index[0]) == '{"x":1}'
    assert "".join(args_by_index[1]) == '{"y":2}'


# ---------------------------------------------------------------------------
# Mixed text + tool_use
# ---------------------------------------------------------------------------


def test_mixed_text_then_tool_use():
    """Spec requirement: text block then tool_use block -> text chunks
    then tool chunks."""
    events = [
        {"type": "message_start", "message": {"id": "m", "model": "claude"}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "Let me check that."},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "tool_use", "id": "t_a", "name": "shell"},
        },
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "input_json_delta", "partial_json": '{"command":"ls"}'},
        },
        {"type": "content_block_stop", "index": 1},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use"},
            "usage": {"output_tokens": 7},
        },
    ]
    chunks = _collect(events)
    # Drop the role kickoff for ordering analysis.
    payload_chunks = [
        c for c in chunks
        if c.choices[0].delta.content is not None
        or c.choices[0].delta.tool_calls
        or c.choices[0].finish_reason is not None
    ]
    # Text comes first, then tool, then finish_reason.
    assert payload_chunks[0].choices[0].delta.content == "Let me check that."
    # Next come the tool chunks.
    tool_idx = next(
        i for i, c in enumerate(payload_chunks) if c.choices[0].delta.tool_calls
    )
    # All tool chunks have index 1 (the tool_use block index).
    for chunk in payload_chunks[tool_idx:-1]:
        if chunk.choices[0].delta.tool_calls:
            assert chunk.choices[0].delta.tool_calls[0].index == 1
    # Last chunk carries finish_reason="tool_calls" (mapped from "tool_use").
    assert payload_chunks[-1].choices[0].finish_reason == "tool_calls"


# ---------------------------------------------------------------------------
# Thinking blocks
# ---------------------------------------------------------------------------


def test_thinking_block_emits_reasoning():
    """Spec requirement: thinking block -> delta.reasoning chunks."""
    events = [
        {"type": "message_start", "message": {"id": "m", "model": "claude-opus-4"}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}},
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "Hmm, let me consider..."},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": " the user wants X."},
        },
        {"type": "content_block_stop", "index": 0},
    ]
    chunks = _collect(events)
    reasoning_chunks = [c for c in chunks if c.choices[0].delta.reasoning is not None]
    assert len(reasoning_chunks) == 2
    assert reasoning_chunks[0].choices[0].delta.reasoning == "Hmm, let me consider..."
    assert reasoning_chunks[1].choices[0].delta.reasoning == " the user wants X."
    # Reasoning chunks must not also carry content or tool_calls.
    for chunk in reasoning_chunks:
        assert chunk.choices[0].delta.content is None
        assert chunk.choices[0].delta.tool_calls is None


def test_thinking_then_text_in_same_stream():
    events = [
        {"type": "message_start", "message": {"id": "m", "model": "claude"}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}},
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "thinking..."},
        },
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "text"}},
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "text_delta", "text": "Here's the answer."},
        },
        {"type": "content_block_stop", "index": 1},
    ]
    chunks = _collect(events)
    reasoning_seen = False
    content_seen = False
    for chunk in chunks:
        if chunk.choices[0].delta.reasoning:
            reasoning_seen = True
            # Content must not have been seen yet.
            assert not content_seen, "thinking emitted after text"
        if chunk.choices[0].delta.content:
            content_seen = True
    assert reasoning_seen and content_seen


# ---------------------------------------------------------------------------
# Stop reason mapping (5 cases — same as workstream A)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "anthropic_stop,expected_openai",
    [
        ("end_turn", "stop"),
        ("max_tokens", "length"),
        ("tool_use", "tool_calls"),
        ("stop_sequence", "stop"),
        ("pause_turn", "stop"),
    ],
)
def test_stop_reason_mapping(anthropic_stop, expected_openai):
    events = _text_stream_events(["hi"], stop_reason=anthropic_stop)
    chunks = _collect(events)
    # The terminal chunk carries finish_reason.
    terminal = [c for c in chunks if c.choices[0].finish_reason is not None]
    assert len(terminal) == 1
    assert terminal[0].choices[0].finish_reason == expected_openai


def test_unknown_stop_reason_defaults_to_stop():
    events = _text_stream_events(["x"], stop_reason="something_weird")
    chunks = _collect(events)
    terminal = [c for c in chunks if c.choices[0].finish_reason is not None]
    assert len(terminal) == 1
    assert terminal[0].choices[0].finish_reason == "stop"


def test_message_delta_without_stop_reason_emits_no_terminal_chunk():
    events = [
        {"type": "message_start", "message": {"id": "m", "model": "c"}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "x"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {}, "usage": {"output_tokens": 1}},
        {"type": "message_stop"},
    ]
    chunks = _collect(events)
    assert all(c.choices[0].finish_reason is None for c in chunks)


def test_message_stop_alone_does_not_crash():
    events = [{"type": "message_stop"}]
    assert _collect(events) == []


# ---------------------------------------------------------------------------
# Robustness — bad / unknown / out-of-order events
# ---------------------------------------------------------------------------


def test_unknown_event_type_is_ignored():
    events = [
        {"type": "message_start", "message": {"id": "m", "model": "c"}},
        {"type": "definitely_not_a_real_event", "foo": "bar"},
        {"type": "message_stop"},
    ]
    chunks = _collect(events)
    # Only the role kickoff was emitted.
    assert len(chunks) == 1
    assert chunks[0].choices[0].delta.role == "assistant"


def test_non_dict_events_are_skipped():
    events = [
        None,
        "not a dict",
        42,
        {"type": "message_start", "message": {"id": "m", "model": "c"}},
    ]
    chunks = _collect(events)
    # Only the message_start produced a chunk.
    assert len(chunks) == 1


def test_content_block_delta_without_known_block_routes_via_delta_type():
    """If content_block_start was missed (defensive), routing by delta.type
    still works."""
    events = [
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "hi"},
        },
    ]
    chunks = _collect(events)
    content_chunks = [c for c in chunks if c.choices[0].delta.content is not None]
    assert len(content_chunks) == 1
    assert content_chunks[0].choices[0].delta.content == "hi"


def test_content_block_stop_without_prior_start_does_not_crash():
    events = [
        {"type": "content_block_stop", "index": 42},
    ]
    assert _collect(events) == []


def test_message_delta_without_delta_key_does_not_crash():
    events = [{"type": "message_delta"}]
    assert _collect(events) == []


def test_message_start_without_message_field_does_not_crash():
    events = [{"type": "message_start"}]
    chunks = _collect(events)
    assert len(chunks) == 1
    assert chunks[0].choices[0].delta.role == "assistant"
    # No id / model captured.
    assert chunks[0].id is None
    assert chunks[0].model is None


# ---------------------------------------------------------------------------
# Realistic round-trip — accumulator behavior matches what agent_loop does
# ---------------------------------------------------------------------------


def test_full_round_trip_accumulator_mimics_agent_loop():
    """Walk a realistic tool-call stream the same way agent_loop does
    (see agent_loop.py:680-693) and assert the accumulator ends with the
    full id+name+arguments."""
    events = [
        {"type": "message_start", "message": {"id": "msg_1", "model": "claude-sonnet-4-6"}},
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "tool_use", "id": "toolu_01", "name": "shell"},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": '{"command":'},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": '"ls -la"}'},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use"},
            "usage": {"output_tokens": 12},
        },
        {"type": "message_stop"},
    ]
    # Re-implement the agent_loop accumulator pattern.
    tool_calls_data = {}
    finish_reason = None
    for chunk in reassemble_anthropic_stream(events):
        delta = chunk.choices[0].delta
        if chunk.choices[0].finish_reason:
            finish_reason = chunk.choices[0].finish_reason
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

    assert tool_calls_data == {
        0: {"id": "toolu_01", "name": "shell", "arguments": '{"command":"ls -la"}'}
    }
    assert finish_reason == "tool_calls"


def test_full_round_trip_text_accumulator():
    events = _text_stream_events(["Hel", "lo, ", "world"])
    full_content = ""
    finish_reason = None
    for chunk in reassemble_anthropic_stream(events):
        delta = chunk.choices[0].delta
        if delta.content:
            full_content += delta.content
        if chunk.choices[0].finish_reason:
            finish_reason = chunk.choices[0].finish_reason
    assert full_content == "Hello, world"
    assert finish_reason == "stop"


def test_iterator_can_be_exhausted_with_generator_input():
    """Reassembler accepts any Iterable[dict], including a generator."""
    def gen():
        yield {"type": "message_start", "message": {"id": "m", "model": "c"}}
        yield {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}}
        yield {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "hi"},
        }
    chunks = list(reassemble_anthropic_stream(gen()))
    content_chunks = [c for c in chunks if c.choices[0].delta.content is not None]
    assert len(content_chunks) == 1
    assert content_chunks[0].choices[0].delta.content == "hi"


# ---------------------------------------------------------------------------
# Tool kickoff with role
# ---------------------------------------------------------------------------


def test_tool_kickoff_when_no_message_start_still_sets_role_on_first_emitted_chunk():
    """If a caller skips message_start and goes straight to a tool block,
    the role still ends up on the first emitted chunk."""
    events = [
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "tool_use", "id": "t0", "name": "shell"},
        },
    ]
    chunks = _collect(events)
    tool_chunks = [c for c in chunks if c.choices[0].delta.tool_calls]
    assert len(tool_chunks) == 1
    assert tool_chunks[0].choices[0].delta.role == "assistant"


def test_text_first_chunk_when_no_message_start_sets_role():
    events = [
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "x"}},
    ]
    chunks = _collect(events)
    content_chunks = [c for c in chunks if c.choices[0].delta.content is not None]
    assert len(content_chunks) == 1
    assert content_chunks[0].choices[0].delta.role == "assistant"
