# Native Anthropic Adapter — Spec for Parallel Implementation

**Status:** active spec, do not modify after parallel agents start
**Date:** 2026-05-17
**Goal:** Add a native Anthropic SDK adapter so Castor can use Claude models with **prompt caching** (~50-90% cost reduction on cached input), **extended-thinking budgets** (for Sonnet 4.6+ / Opus 4), and **structured tool-use errors** — without breaking the existing OpenAI-compatible flow for the other 9 providers.

This document is the **contract** between three parallel workstreams. Read it fully before writing code. Raise ambiguities — don't improvise.

---

## Motivation

The drayage stress-test (`g_3b2a851a00094e48`, `g_5c4e6e3dc90c4f47`) showed that **weak models capitulate** on tools requiring careful escaping (e.g. `write_file` with large markdown literals). Our anti-capitulation prompt patches + 3-layer acceptance gate **structurally prevent false-done** but don't fix the underlying issue: the model can't write the file.

A native Anthropic adapter addresses this two ways:

1. **Direct access to Claude Sonnet 4.6 / Opus 4** with first-class API features (prompt caching, thinking budget, structured tool-use). The same drayage task should succeed on Sonnet that fails on `z-ai/glm-5.1`.

2. **Cost optimization.** Currently every turn ships the entire conversation + system prompt as input tokens. Anthropic's prompt caching cuts this 50-90% on cached prefixes. For long-running goals this is material — a typical orchestrator round re-ships ~10k tokens of plan + facts + system.

Inspired by [Hermes Agent's `agent/anthropic_adapter.py`](https://github.com/nousresearch/hermes-agent) (89KB, MIT-licensed) — Nous Research ships per-provider native adapters as a design choice, exactly because OpenAI-compatible wrappers drop these features.

---

## Constraints

### Backward compatibility

- The other 9 providers (LM Studio, Ollama, OpenAI, OpenRouter, Together, Groq, DeepSeek, Perplexity, Cerebras, Mistral) keep using the OpenAI-compatible client. Their behaviour must not change.
- `agent_loop.run_loop()` must see the SAME stream-chunk shape regardless of provider. The adapter exposes a duck-typed `OpenAI`-like API.
- Tests for existing providers must not require modification.

### Optional dependency

- The `anthropic` Python SDK is the only new dependency. It is **optional** — installations without it (or without `ANTHROPIC_API_KEY`) continue to work, falling back to the OpenAI-compatible client for Anthropic models routed via OpenRouter.
- Add to `pyproject.toml` as an extras: `anthropic_native = ["anthropic>=0.50.0"]`. Top-level install does NOT pull it.
- Code path that tries to use the native adapter without the SDK installed must surface a clear error message ("install with `pip install anthropic` or set provider=openrouter").

### The OpenAI shape `agent_loop` consumes

Locked contract (read from `agent_loop.py:630-700`). The adapter emits chunks where:

```
chunk.choices: list[Choice]   # length 1 in practice
chunk.choices[0].delta:
    .content: str | None      # accumulated → assistant.content
    .reasoning: str | None    # optional, used for Gemma-style thinking
    .tool_calls: list[ToolCallDelta] | None
        each item:
            .index: int
            .id: str | None
            .function:
                .name: str | None
                .arguments: str | None  # JSON fragments concatenated
chunk.choices[0].finish_reason: str | None
    valid values: "stop" | "tool_calls" | "length" | "content_filter" | None
```

Non-streaming response (used by some code paths via `get_client().chat.completions.create(stream=False)`):

```
response.choices[0].message:
    .content: str | None
    .tool_calls: list[ToolCall] | None
        each item: .id, .type="function", .function.name, .function.arguments
    .role: "assistant"
response.choices[0].finish_reason: str
response.usage:
    .prompt_tokens: int
    .completion_tokens: int
    .total_tokens: int
```

Plus a custom field `response.usage.cache_creation_input_tokens` / `cache_read_input_tokens` exposed where available — `agent_loop` already reads these (search for "cache_" in agent_loop.py).

---

## Decomposition

Three parallel workstreams, **independent files**:

| Workstream | Files | Depends on |
|---|---|---|
| **A: Converters** | `providers_anthropic_convert.py` + `tests/test_anthropic_convert.py` | spec only |
| **B: Stream reassembler** | `providers_anthropic_stream.py` + `tests/test_anthropic_stream.py` | spec only |
| **C: Client + routing** | `providers_anthropic.py` + `providers.py` patches + `tests/test_anthropic_client.py` | A + B's interfaces (stubbed if not merged yet) |

After merge: I'll wire `pyproject.toml` extras + `EDITABLE_SETTINGS` for the thinking-budget toggle + end-to-end retest of the drayage scenario on real Claude Sonnet.

---

## Workstream A — Request/response converters (pure functions)

**New file:** `providers_anthropic_convert.py` at repo root.

### Public API

```python
def to_anthropic_request(
    *,
    model: str,
    messages: list[dict],     # OpenAI-shape messages
    tools: list[dict] | None,  # OpenAI-shape tool schemas
    max_tokens: int = 4096,
    temperature: float | None = None,
    stream: bool = False,
    # Cache + thinking go in here when workstream C wires them; for now ignore.
) -> dict:
    """Translate an OpenAI-style request payload into the Anthropic
    `messages.create()` kwargs shape. Returns a dict ready to splat
    into `anthropic.Anthropic().messages.create(**kwargs)`.

    Translations (each MUST be covered by a test):
      - {role: "system", content: ...}  →  top-level `system=` kwarg
      - {role: "user" | "assistant"}    →  Anthropic messages list
      - {role: "tool", tool_call_id, content}  →  user message with
            content: [{type: "tool_result", tool_use_id, content: <str>}]
      - assistant {tool_calls: [...]}   →  assistant message with
            content: [{type: "tool_use", id, name, input}]
      - tools schema: OpenAI {type:"function", function:{name, description, parameters}}
            →  Anthropic {name, description, input_schema}
      - stop_reason mapping (response side) — not in this function;
        the response converter handles it.

    Multi-system: if multiple system messages, concatenate with two
    newlines (Anthropic accepts one system param). Preserve order.

    Tool-result content: if `messages[i].content` for a tool message is
    a string, wrap as-is. If it's already a list of content blocks
    (rare in OpenAI shape but possible after some legacy code), pass
    through.

    Edge cases:
      - Empty messages list → ValueError.
      - First message must end up being user/tool (Anthropic rejects
        starting with assistant). If first message after system stripping
        is assistant, prepend a synthetic empty user message and log a
        warning (this mirrors what OpenRouter does today).
      - Tool result content longer than 200KB → truncate with marker
        "[truncated by adapter: X chars]"; Anthropic rejects oversized.
      - Image / multimodal content: pass through as-is for now; tests
        confirm dict structure isn't mangled.
    """


def from_anthropic_response(resp: dict) -> dict:
    """Translate Anthropic's non-streaming response dict back to the
    OpenAI-shaped dict that `agent_loop` and other callers expect.

    Anthropic response top-level keys (per their API):
      id, type, role, model, content (list of blocks), stop_reason,
      stop_sequence, usage{input_tokens, output_tokens,
      cache_creation_input_tokens?, cache_read_input_tokens?}

    Output dict (sufficient to construct an OpenAI ChatCompletion):
      {
        "id": ...,
        "model": ...,
        "choices": [{
          "index": 0,
          "message": {
            "role": "assistant",
            "content": <concatenated text blocks, or None if only tool_use>,
            "tool_calls": [
              {"id": block.id, "type": "function",
               "function": {"name": block.name,
                            "arguments": json.dumps(block.input)}}
              for block in tool_use blocks
            ] or None,
            # Custom field used by agent_loop for Gemma-style thinking;
            # populate from Anthropic "thinking" content blocks too.
            "reasoning": <concatenated thinking blocks, or None>
          },
          "finish_reason": _map_stop_reason(stop_reason)
        }],
        "usage": {
          "prompt_tokens": input_tokens,
          "completion_tokens": output_tokens,
          "total_tokens": input + output,
          "cache_creation_input_tokens": cache_creation_input_tokens or 0,
          "cache_read_input_tokens": cache_read_input_tokens or 0
        }
      }
    """


# Helper exposed for workstream B's stream reassembler too:
def _map_stop_reason(anthropic_stop: str | None) -> str | None:
    """
      "end_turn" → "stop"
      "max_tokens" → "length"
      "tool_use" → "tool_calls"
      "stop_sequence" → "stop"
      "pause_turn" → "stop"  (rare)
      None → None
      anything else → "stop" (default)
    """
```

### Tests (≥25)

`tests/test_anthropic_convert.py` — pure dict-in / dict-out, no network mocking needed.

Per converter, cover:

- `to_anthropic_request`:
  - System message extracted to `system=` kwarg, removed from messages list
  - Multiple system messages concatenated with two newlines
  - User + assistant alternation preserved
  - Tool message converts to user with tool_result block
  - Assistant tool_calls converts to tool_use blocks (id, name, input as parsed JSON)
  - Tools schema OpenAI → Anthropic shape
  - Synthetic user prepended if first non-system message is assistant
  - Empty messages list → ValueError
  - Oversized tool_result truncation
  - Stream flag passes through
  - Temperature passes through; None means don't include the key
  - Multimodal content (`{type:"image_url", image_url:{url:...}}`) passes through unchanged

- `from_anthropic_response`:
  - Text-only response → `message.content` populated, tool_calls=None
  - Tool-use-only response → content=None, tool_calls populated, finish_reason="tool_calls"
  - Mixed text + tool_use response → both populated
  - Thinking blocks → `reasoning` field populated
  - Usage block including cache_* fields → propagated to OpenAI usage
  - Each stop_reason mapping (5 cases) verified

---

## Workstream B — Stream reassembler

**New file:** `providers_anthropic_stream.py` at repo root.

### Public API

```python
def reassemble_anthropic_stream(events: Iterable[dict]) -> Iterator[ChatCompletionChunk]:
    """Given an iterable of Anthropic SSE event dicts, yield OpenAI-shaped
    `ChatCompletionChunk`-like objects.

    Anthropic stream event types (from their SDK / SSE spec):
      message_start    → first chunk, set initial metadata
      content_block_start  → entering a block (text, tool_use, thinking)
      content_block_delta  → token delta within current block
      content_block_stop   → block finished
      message_delta        → final usage + stop_reason
      message_stop         → end-of-stream sentinel
      ping                 → ignore
      error                → raise / surface

    Emission strategy:
      - For each "content_block_delta" inside a text block: emit a chunk
        with delta.content = the delta text.
      - For each "content_block_delta" inside a tool_use block (which
        carries input_json_delta fragments): emit a chunk with
        delta.tool_calls = [{index, id (only on first chunk for that
        block), function: {name (only on first chunk), arguments (the JSON delta)}}].
      - For thinking blocks: emit chunks with delta.reasoning = the delta.
      - On message_delta with stop_reason: emit a final chunk with
        delta=empty, finish_reason=mapped_stop_reason.

    The yielded chunk objects must duck-type the OpenAI ChatCompletionChunk:
    accessing chunk.choices[0].delta.content / .tool_calls / .reasoning /
    chunk.choices[0].finish_reason must work as agent_loop expects.

    Implementation: small dataclasses _Chunk, _Choice, _Delta, _ToolCallDelta,
    _ToolFnDelta with the right fields. Don't depend on pydantic — keep
    deps light.
    """


# Build a minimal duck-typed namespace so agent_loop's attribute access works:
@dataclass
class _ToolFnDelta:
    name: str | None = None
    arguments: str | None = None

@dataclass
class _ToolCallDelta:
    index: int
    id: str | None = None
    function: _ToolFnDelta | None = None
    type: str = "function"  # OpenAI always sets this on tool calls

@dataclass
class _Delta:
    content: str | None = None
    reasoning: str | None = None
    tool_calls: list[_ToolCallDelta] | None = None
    role: str | None = None  # set on first chunk: "assistant"

@dataclass
class _Choice:
    index: int = 0
    delta: _Delta = field(default_factory=_Delta)
    finish_reason: str | None = None

@dataclass
class _Chunk:
    id: str | None = None
    model: str | None = None
    choices: list[_Choice] = field(default_factory=lambda: [_Choice()])
```

### Tests (≥20)

`tests/test_anthropic_stream.py` — feed scripted event lists, assert emitted chunk shapes.

- Pure text stream: 5 content_block_delta events → chunks with content deltas in order
- Tool-use stream: content_block_start with tool_use + 3 input_json_delta events → chunks where first has id+name, rest have only arguments
- Multiple tool calls in one message (parallel tool use) → indices 0, 1, ... preserved
- Mixed: text block then tool_use block → text chunks then tool chunks
- Thinking block (server_response.type == "thinking") → delta.reasoning chunks
- Final message_delta with stop_reason → terminal chunk with finish_reason
- Each stop_reason mapping (5 cases)
- Ping events ignored
- Error event raises
- Empty stream (no events) → empty iterator, no crash

---

## Workstream C — Client + routing

**New file:** `providers_anthropic.py` at repo root.

### Public API

```python
class AnthropicNativeClient:
    """Duck-typed `OpenAI` replacement — same .chat.completions.create()
    surface, but backed by the official `anthropic` SDK.

    Constructed only when:
      - `anthropic` package is importable
      - ANTHROPIC_API_KEY is set (via env, kv, or constructor param)
      - The active provider is "anthropic" OR user explicitly opted in
        via setting `anthropic_native_routing=1` for OpenRouter Anthropic models
    """

    def __init__(self, *, api_key: str, base_url: str | None = None):
        # Lazy-import anthropic; raise a friendly error if not installed.
        try:
            import anthropic
        except ImportError:
            raise RuntimeError(
                "anthropic SDK not installed. Run: pip install 'castor[anthropic_native]' "
                "or pip install anthropic. Or use provider=openrouter for Claude models "
                "without the SDK."
            )
        self._client = anthropic.Anthropic(api_key=api_key, base_url=base_url or None)
        self.chat = self._ChatNamespace(self)

    class _ChatNamespace:
        def __init__(self, outer): self._outer = outer
        @property
        def completions(self): return self._outer

    def create(self, *, model, messages, tools=None, stream=False,
               max_tokens=4096, temperature=None, **kw):
        """OpenAI-compatible signature. Translates, calls Anthropic, translates back."""
        from providers_anthropic_convert import to_anthropic_request, from_anthropic_response
        req = to_anthropic_request(
            model=model, messages=messages, tools=tools, stream=stream,
            max_tokens=max_tokens, temperature=temperature,
        )
        if stream:
            from providers_anthropic_stream import reassemble_anthropic_stream
            ant_stream = self._client.messages.create(**req)
            # anthropic SDK streams via ant_stream as an iterator of typed events;
            # convert each event to a dict shape (or feed objects directly if the
            # reassembler accepts both). Implementer's choice — keep the
            # converter contract from workstream B as the source of truth.
            return reassemble_anthropic_stream(_iter_as_dicts(ant_stream))
        resp = self._client.messages.create(**req)
        return _OpenAIResponseShape(from_anthropic_response(resp.model_dump()))
```

### Routing in providers.py

Modify `providers.get_client()`:

```python
def get_client():
    p = get_provider()
    name = (get_provider_name() or "").lower()
    # Route Anthropic native if conditions met.
    if name == "anthropic" or (name == "openrouter" and _wants_native_anthropic()):
        key = _anthropic_api_key()  # env first, then secret store, then provider config
        if key:
            try:
                from providers_anthropic import AnthropicNativeClient
                return AnthropicNativeClient(api_key=key)
            except RuntimeError as e:
                _log.warning(f"native Anthropic adapter unavailable: {e}; "
                             f"falling back to OpenAI-compatible client")
    # Fall through to existing OpenAI-compatible path.
    ...existing code...
```

Add `PRESETS["anthropic"]` entry with `url: "https://api.anthropic.com"` (no `/v1` — Anthropic SDK takes the bare URL) and curated model list:
  `["claude-sonnet-4-5", "claude-opus-4", "claude-haiku-4", "claude-sonnet-4-6", "claude-opus-4-1"]`.

### Tests (≥10 integration)

`tests/test_anthropic_client.py` — mock the `anthropic` SDK at the module level (`monkeypatch.setattr("anthropic.Anthropic", FakeClient)`), so tests run without the package installed AND without an API key.

- Constructor raises RuntimeError when anthropic SDK absent (mock ImportError)
- `chat.completions.create(stream=False)` returns OpenAI-shaped response
- `chat.completions.create(stream=True)` returns an iterator of OpenAI-shaped chunks
- Tool calls round-trip: send OpenAI tool schema, receive OpenAI tool_calls
- System messages get hoisted to top-level
- Routing: `get_client()` returns AnthropicNativeClient for provider=anthropic + key
- Routing: falls back to OpenAI client when anthropic SDK missing
- Routing: falls back to OpenAI client when ANTHROPIC_API_KEY missing
- Routing: openrouter provider keeps OpenAI client unless explicit opt-in
- Usage block including cache_* fields propagates through

---

## Out of scope for this PR

- Prompt caching (`cache_control`) — separate follow-up PR. Adapter is built so caching can be layered on by setting `cache_control` markers in the converter; the marker insertion strategy is its own design decision.
- Thinking budget — separate follow-up; SDK supports `thinking={"type":"enabled","budget_tokens":N}`, but plumbing the toggle through `EDITABLE_SETTINGS` + UI is separate work.
- Native Bedrock / Vertex Gemini / Codex Responses adapters — same pattern but separate work.
- Tracking `cache_read_input_tokens` in the cost-pricing module (`pricing.py` doesn't currently use them).

These are *enabling decisions* — the adapter doesn't preclude them, just doesn't include them in this PR.

---

## Done criteria

- All three workstream test files green: A (~25), B (~20), C (~10) = ~55 new tests
- Existing 1073-test suite still passes (no regressions)
- `ruff check providers_anthropic.py providers_anthropic_convert.py providers_anthropic_stream.py tests/test_anthropic_*.py providers.py` clean
- `pyproject.toml` has `[project.optional-dependencies] anthropic_native = ["anthropic>=0.50.0"]`
- Without anthropic SDK installed, NONE of the new tests are skipped — they all monkey-patch the SDK
- I (the orchestrator) handle final integration: `pyproject.toml` extras + (later) end-to-end retest of `g_3b2a851a00094e48` scenario on real Sonnet via this adapter
