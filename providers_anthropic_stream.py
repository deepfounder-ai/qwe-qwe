"""Anthropic SSE stream reassembler.

Converts an iterable of Anthropic streaming event dicts into an iterator of
OpenAI-shaped `ChatCompletionChunk`-like objects. The emitted chunks duck-type
the OpenAI streaming contract that `agent_loop.run_loop()` consumes:

    chunk.choices[0].delta.content      # str | None
    chunk.choices[0].delta.reasoning    # str | None
    chunk.choices[0].delta.tool_calls   # list[_ToolCallDelta] | None
        each item: .index, .id, .function.name, .function.arguments
    chunk.choices[0].finish_reason      # str | None

This module is intentionally dependency-free (stdlib only) and never touches
the network or the `anthropic` SDK. Workstream C (`providers_anthropic.py`)
feeds events into `reassemble_anthropic_stream()`; this module owns the shape
translation only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Iterator, Optional

# Workstream A owns _map_stop_reason — shared with the response converter
# so streaming and non-streaming paths translate stop reasons identically.
from providers_anthropic_convert import _map_stop_reason


# ---------------------------------------------------------------------------
# Duck-typed OpenAI ChatCompletionChunk shape
# ---------------------------------------------------------------------------


@dataclass
class _ToolFnDelta:
    name: Optional[str] = None
    arguments: Optional[str] = None


@dataclass
class _ToolCallDelta:
    index: int = 0
    id: Optional[str] = None
    function: Optional[_ToolFnDelta] = None
    type: str = "function"  # OpenAI always sets this on tool calls


@dataclass
class _Delta:
    content: Optional[str] = None
    reasoning: Optional[str] = None
    tool_calls: Optional[list] = None  # list[_ToolCallDelta] | None
    role: Optional[str] = None  # "assistant" on the first chunk only


@dataclass
class _Choice:
    index: int = 0
    delta: _Delta = field(default_factory=_Delta)
    finish_reason: Optional[str] = None


@dataclass
class _Chunk:
    id: Optional[str] = None
    model: Optional[str] = None
    choices: list = field(default_factory=lambda: [_Choice()])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_chunk(
    *,
    content: Optional[str] = None,
    reasoning: Optional[str] = None,
    tool_calls: Optional[list] = None,
    finish_reason: Optional[str] = None,
    role: Optional[str] = None,
    chunk_id: Optional[str] = None,
    model: Optional[str] = None,
) -> _Chunk:
    """Build a duck-typed chunk with the given delta fields."""
    delta = _Delta(
        content=content,
        reasoning=reasoning,
        tool_calls=tool_calls,
        role=role,
    )
    choice = _Choice(index=0, delta=delta, finish_reason=finish_reason)
    return _Chunk(id=chunk_id, model=model, choices=[choice])


# ---------------------------------------------------------------------------
# Main reassembler
# ---------------------------------------------------------------------------


def reassemble_anthropic_stream(events: Iterable[dict]) -> Iterator[_Chunk]:
    """Yield OpenAI-shaped chunks from a sequence of Anthropic SSE events.

    Event handling:
      - ``message_start``         emit a role-only kickoff chunk
        (carries ``delta.role="assistant"`` + id/model from
        ``message.id`` / ``message.model``).
      - ``content_block_start``   begin tracking the block by its ``index``.
        For ``tool_use`` blocks, emit a kickoff chunk carrying the tool id +
        function.name with an empty arguments string (so the consumer's
        accumulator captures both).
      - ``content_block_delta``   emit a chunk routed by the current block
        type at that index:
            text     -> delta.content
            thinking -> delta.reasoning
            tool_use -> delta.tool_calls=[{index, function: {arguments: ...}}]
      - ``content_block_stop``    clear the per-index block-type tracking;
        no chunk emitted.
      - ``message_delta``         if ``delta.stop_reason`` is set, emit a
        terminal chunk with ``finish_reason=_map_stop_reason(...)``.
      - ``message_stop``          end-of-stream sentinel, no chunk emitted.
      - ``ping``                  ignored.
      - ``error``                 raise ``RuntimeError`` with the message.

    Empty iterable yields an empty iterator without crashing.
    """
    # Per-stream state
    stream_id: Optional[str] = None
    stream_model: Optional[str] = None
    role_emitted = False
    # block_index -> block_type ("text" | "tool_use" | "thinking")
    block_types: dict[int, str] = {}
    # block_index -> True once the tool_use kickoff chunk has been emitted
    tool_kickoff_done: dict[int, bool] = {}

    for event in events:
        if not isinstance(event, dict):
            continue
        etype = event.get("type")

        if etype == "ping":
            continue

        if etype == "error":
            # Anthropic surfaces error events as {"type": "error",
            # "error": {"type": "...", "message": "..."}}.
            err = event.get("error") or {}
            msg = err.get("message") if isinstance(err, dict) else None
            raise RuntimeError(msg or "anthropic stream error")

        if etype == "message_start":
            msg = event.get("message") or {}
            if isinstance(msg, dict):
                stream_id = msg.get("id") or stream_id
                stream_model = msg.get("model") or stream_model
            # Emit a role-only chunk so consumers see the assistant role on
            # the first chunk, matching OpenAI's streaming behavior.
            yield _make_chunk(
                role="assistant",
                chunk_id=stream_id,
                model=stream_model,
            )
            role_emitted = True
            continue

        if etype == "content_block_start":
            idx = event.get("index")
            if not isinstance(idx, int):
                continue
            block = event.get("content_block") or {}
            btype = block.get("type") if isinstance(block, dict) else None
            if btype not in ("text", "tool_use", "thinking"):
                # Unknown block type — track loosely so deltas don't crash.
                block_types[idx] = btype or "text"
                continue
            block_types[idx] = btype

            if btype == "tool_use":
                # Kickoff chunk for a new tool call: carries id + name, empty
                # arguments. This populates tool_calls_data[idx] in the
                # consumer accumulator before any input_json_delta arrives.
                tool_id = block.get("id") if isinstance(block, dict) else None
                tool_name = block.get("name") if isinstance(block, dict) else None
                tc = _ToolCallDelta(
                    index=idx,
                    id=tool_id,
                    function=_ToolFnDelta(name=tool_name, arguments=""),
                )
                yield _make_chunk(
                    tool_calls=[tc],
                    role="assistant" if not role_emitted else None,
                    chunk_id=stream_id,
                    model=stream_model,
                )
                role_emitted = True
                tool_kickoff_done[idx] = True
            # text / thinking blocks: no chunk emitted on start.
            continue

        if etype == "content_block_delta":
            idx = event.get("index")
            if not isinstance(idx, int):
                continue
            btype = block_types.get(idx)
            delta = event.get("delta") or {}
            if not isinstance(delta, dict):
                continue
            dtype = delta.get("type")

            # Route by either the recorded block type OR the delta's own
            # type marker. The Anthropic SDK carries both; we prefer the
            # block type because it's authoritative, then fall back to
            # the delta type for robustness against missing block_start.
            if btype == "text" or dtype == "text_delta":
                text = delta.get("text")
                if text:
                    yield _make_chunk(
                        content=text,
                        role="assistant" if not role_emitted else None,
                        chunk_id=stream_id,
                        model=stream_model,
                    )
                    role_emitted = True
            elif btype == "thinking" or dtype == "thinking_delta":
                rtext = delta.get("thinking")
                if rtext:
                    yield _make_chunk(
                        reasoning=rtext,
                        role="assistant" if not role_emitted else None,
                        chunk_id=stream_id,
                        model=stream_model,
                    )
                    role_emitted = True
            elif btype == "tool_use" or dtype == "input_json_delta":
                partial = delta.get("partial_json")
                if partial is None:
                    continue
                # Continuation chunk: only arguments, no id/name.
                tc = _ToolCallDelta(
                    index=idx,
                    id=None,
                    function=_ToolFnDelta(name=None, arguments=partial),
                )
                yield _make_chunk(
                    tool_calls=[tc],
                    role="assistant" if not role_emitted else None,
                    chunk_id=stream_id,
                    model=stream_model,
                )
                role_emitted = True
            # else: unknown delta type — silently ignored.
            continue

        if etype == "content_block_stop":
            idx = event.get("index")
            if isinstance(idx, int):
                block_types.pop(idx, None)
                tool_kickoff_done.pop(idx, None)
            continue

        if etype == "message_delta":
            delta = event.get("delta") or {}
            stop_reason = delta.get("stop_reason") if isinstance(delta, dict) else None
            if stop_reason is not None:
                yield _make_chunk(
                    finish_reason=_map_stop_reason(stop_reason),
                    chunk_id=stream_id,
                    model=stream_model,
                )
            continue

        if etype == "message_stop":
            # End-of-stream sentinel. message_delta already emitted the
            # finish_reason; nothing to do here.
            continue

        # Unknown event types are ignored (forwards-compatible).


__all__ = [
    "reassemble_anthropic_stream",
    "_Chunk",
    "_Choice",
    "_Delta",
    "_ToolCallDelta",
    "_ToolFnDelta",
]
