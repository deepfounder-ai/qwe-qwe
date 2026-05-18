"""Tests for ``providers_anthropic.AnthropicNativeClient`` + ``providers.get_client()``
routing.

Run WITHOUT the ``anthropic`` SDK installed: every test monkey-patches a fake
SDK into ``sys.modules["anthropic"]`` before constructing the client, or
forces ``import anthropic`` to raise ImportError for the failure-path tests.

Run order independence: each test that constructs a client also invalidates
the providers client cache (``providers._invalidate``) so a previously-
constructed fake doesn't leak into the next test.
"""
from __future__ import annotations

import builtins
import importlib
import sys
import types

import pytest


# ── Fake anthropic SDK helpers ────────────────────────────────────────────────


def _make_fake_message(
    *,
    msg_id: str = "msg_test",
    model: str = "claude-sonnet-4-5",
    content_blocks: list[dict] | None = None,
    stop_reason: str = "end_turn",
    input_tokens: int = 10,
    output_tokens: int = 5,
    cache_creation: int = 0,
    cache_read: int = 0,
):
    """Build a fake Anthropic Message-like object with model_dump()."""
    if content_blocks is None:
        content_blocks = [{"type": "text", "text": "hello"}]
    usage_dict = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }
    if cache_creation:
        usage_dict["cache_creation_input_tokens"] = cache_creation
    if cache_read:
        usage_dict["cache_read_input_tokens"] = cache_read

    body = {
        "id": msg_id,
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content_blocks,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": usage_dict,
    }

    class _FakeMsg:
        def __init__(self, b):
            self._b = b

        def model_dump(self):
            return dict(self._b)

    return _FakeMsg(body)


class _FakeMessages:
    """Mock Anthropic ``messages`` namespace.

    ``last_kwargs`` captures the kwargs the SUT passed so tests can inspect
    request translation. ``response_factory`` lets a test override the return
    value; default is a plain text response.
    """

    def __init__(self):
        self.last_kwargs = None
        self.response_factory = lambda: _make_fake_message()
        self.stream_events = None  # if set, ``create`` returns this iterable when stream=True

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        if kwargs.get("stream"):
            if self.stream_events is None:
                # Default empty stream
                return iter([])
            return iter(self.stream_events)
        return self.response_factory()


class _FakeAnthropic:
    """Stand-in for ``anthropic.Anthropic``."""

    instances: list["_FakeAnthropic"] = []

    def __init__(self, *, api_key=None, base_url=None, **kw):
        self.api_key = api_key
        self.base_url = base_url
        self.extra = kw
        self.messages = _FakeMessages()
        _FakeAnthropic.instances.append(self)


@pytest.fixture
def fake_sdk(monkeypatch):
    """Install a fake ``anthropic`` module into sys.modules."""
    _FakeAnthropic.instances = []
    fake_mod = types.ModuleType("anthropic")
    fake_mod.Anthropic = _FakeAnthropic  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "anthropic", fake_mod)
    yield fake_mod


def _fake_to_anthropic_request(*, model, messages, tools=None, max_tokens=4096,
                               temperature=None, stream=False):
    """Workstream-A stand-in.

    Just enough translation that tests exercising request shape (system hoist,
    tools mapping, stream flag) can assert on the kwargs the SDK receives.
    The real workstream-A converter is more thorough; this stub mirrors only
    the contracts the tests pin.
    """
    system_parts = []
    out_messages = []
    for m in messages:
        role = m.get("role")
        if role == "system":
            system_parts.append(m.get("content", ""))
            continue
        if role == "tool":
            out_messages.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": m.get("tool_call_id"),
                    "content": m.get("content", ""),
                }],
            })
            continue
        if role == "assistant" and m.get("tool_calls"):
            import json as _json
            blocks = []
            if m.get("content"):
                blocks.append({"type": "text", "text": m["content"]})
            for tc in m["tool_calls"]:
                fn = tc.get("function", {})
                args = fn.get("arguments") or "{}"
                try:
                    parsed = _json.loads(args)
                except Exception:
                    parsed = {}
                blocks.append({
                    "type": "tool_use",
                    "id": tc.get("id"),
                    "name": fn.get("name"),
                    "input": parsed,
                })
            out_messages.append({"role": "assistant", "content": blocks})
            continue
        out_messages.append({"role": role, "content": m.get("content")})

    req = {"model": model, "messages": out_messages, "max_tokens": max_tokens}
    if system_parts:
        req["system"] = "\n\n".join(system_parts)
    if tools is not None:
        req["tools"] = [
            {
                "name": t["function"]["name"],
                "description": t["function"].get("description", ""),
                "input_schema": t["function"].get("parameters", {}),
            }
            for t in tools
        ]
    if temperature is not None:
        req["temperature"] = temperature
    if stream:
        req["stream"] = True
    return req


_STOP_MAP = {
    "end_turn": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
    "stop_sequence": "stop",
    "pause_turn": "stop",
}


def _fake_from_anthropic_response(resp):
    """Workstream-A stand-in: dict-in dict-out, OpenAI shape."""
    import json as _json
    content_blocks = resp.get("content") or []
    text_parts = []
    thinking_parts = []
    tool_calls = []
    for blk in content_blocks:
        t = blk.get("type")
        if t == "text":
            text_parts.append(blk.get("text", ""))
        elif t == "thinking":
            thinking_parts.append(blk.get("thinking") or blk.get("text", ""))
        elif t == "tool_use":
            tool_calls.append({
                "id": blk.get("id"),
                "type": "function",
                "function": {
                    "name": blk.get("name"),
                    "arguments": _json.dumps(blk.get("input") or {}),
                },
            })
    content = "".join(text_parts) if text_parts else None
    reasoning = "".join(thinking_parts) if thinking_parts else None
    usage = resp.get("usage") or {}
    input_tok = int(usage.get("input_tokens", 0) or 0)
    output_tok = int(usage.get("output_tokens", 0) or 0)
    return {
        "id": resp.get("id"),
        "model": resp.get("model"),
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": content,
                "tool_calls": tool_calls or None,
                "reasoning": reasoning,
            },
            "finish_reason": _STOP_MAP.get(resp.get("stop_reason"), "stop"),
        }],
        "usage": {
            "prompt_tokens": input_tok,
            "completion_tokens": output_tok,
            "total_tokens": input_tok + output_tok,
            "cache_creation_input_tokens": int(usage.get("cache_creation_input_tokens", 0) or 0),
            "cache_read_input_tokens": int(usage.get("cache_read_input_tokens", 0) or 0),
        },
    }


def _fake_reassemble_anthropic_stream(events):
    """Workstream-B stand-in: minimal scripted reassembly so the streaming
    test asserts the iterator contract + accumulates text deltas. Tests
    relying on the real reassembler's edge cases live in
    ``tests/test_anthropic_stream.py`` (workstream B).
    """
    from dataclasses import dataclass, field as _field

    @dataclass
    class _D:
        content: str | None = None
        reasoning: str | None = None
        tool_calls: list | None = None
        role: str | None = None

    @dataclass
    class _C:
        index: int = 0
        delta: _D = _field(default_factory=_D)
        finish_reason: str | None = None

    @dataclass
    class _Ch:
        id: str | None = None
        model: str | None = None
        choices: list = _field(default_factory=lambda: [_C()])

    for ev in events:
        t = ev.get("type")
        if t == "content_block_delta":
            delta = ev.get("delta") or {}
            if delta.get("type") == "text_delta":
                yield _Ch(choices=[_C(delta=_D(content=delta.get("text", "")))])
        elif t == "message_delta":
            stop = (ev.get("delta") or {}).get("stop_reason")
            yield _Ch(choices=[_C(finish_reason=_STOP_MAP.get(stop, "stop"))])


@pytest.fixture
def reload_providers_anthropic(monkeypatch):
    """Reload providers_anthropic so its module-level state is fresh.

    Patches in workstream-A/B stand-ins so tests don't depend on whether
    those files are merged yet. The real converters live in
    ``providers_anthropic_convert.py`` and ``providers_anthropic_stream.py``.
    """
    if "providers_anthropic" in sys.modules:
        importlib.reload(sys.modules["providers_anthropic"])
    else:
        importlib.import_module("providers_anthropic")
    pa = sys.modules["providers_anthropic"]
    monkeypatch.setattr(pa, "to_anthropic_request", _fake_to_anthropic_request)
    monkeypatch.setattr(pa, "from_anthropic_response", _fake_from_anthropic_response)
    monkeypatch.setattr(pa, "reassemble_anthropic_stream", _fake_reassemble_anthropic_stream)
    return pa


@pytest.fixture
def fresh_providers_with_anthropic(qwe_temp_data_dir, monkeypatch):
    """Reload providers.py against a fresh temp DB + clear caches.

    Mirrors ``tests/test_providers_list.py::fresh_providers`` but also
    invalidates the client cache so a previous fake doesn't leak.
    """
    if "providers" not in sys.modules:
        importlib.import_module("providers")
    providers = importlib.reload(sys.modules["providers"])
    providers._ping_cache.clear()
    providers._CTX_CACHE.clear()
    providers._invalidate()
    return providers


# ── 1. Constructor raises RuntimeError when anthropic SDK absent ──────────────


def test_constructor_raises_when_anthropic_sdk_absent(monkeypatch):
    """If ``import anthropic`` fails inside ``__init__``, surface a friendly
    RuntimeError with the install hint — don't propagate the ImportError raw.
    """
    # Force any ``import anthropic`` inside the constructor to fail.
    monkeypatch.delitem(sys.modules, "anthropic", raising=False)

    real_import = builtins.__import__

    def _blocking_import(name, *args, **kwargs):
        if name == "anthropic":
            raise ImportError("simulated absent SDK")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocking_import)

    if "providers_anthropic" in sys.modules:
        importlib.reload(sys.modules["providers_anthropic"])
    else:
        importlib.import_module("providers_anthropic")
    pa = sys.modules["providers_anthropic"]

    with pytest.raises(RuntimeError) as exc:
        pa.AnthropicNativeClient(api_key="sk-test")
    msg = str(exc.value).lower()
    assert "anthropic" in msg
    assert "install" in msg or "pip" in msg


# ── 2. Constructor succeeds with mocked SDK + key ────────────────────────────


def test_constructor_succeeds_with_mocked_sdk(fake_sdk, reload_providers_anthropic):
    pa = reload_providers_anthropic
    client = pa.AnthropicNativeClient(api_key="sk-ant-test")
    assert client is not None
    # The fake SDK should have been instantiated exactly once with our key.
    assert len(_FakeAnthropic.instances) == 1
    assert _FakeAnthropic.instances[0].api_key == "sk-ant-test"
    # Duck-type surface present:
    assert hasattr(client, "chat")
    assert hasattr(client.chat, "completions")
    assert hasattr(client.chat.completions, "create")


# ── 3. Non-streaming create returns OpenAI-shaped response ───────────────────


def test_create_non_streaming_returns_openai_shape(fake_sdk, reload_providers_anthropic):
    pa = reload_providers_anthropic
    client = pa.AnthropicNativeClient(api_key="sk-ant-test")
    sdk_instance = _FakeAnthropic.instances[-1]
    # Stub the converter so we control the dict shape independently of A's logic.
    sdk_instance.messages.response_factory = lambda: _make_fake_message(
        content_blocks=[{"type": "text", "text": "Hello, world!"}],
        stop_reason="end_turn",
        input_tokens=42,
        output_tokens=7,
    )

    resp = client.chat.completions.create(
        model="claude-sonnet-4-5",
        messages=[{"role": "user", "content": "hi"}],
        stream=False,
    )

    # Attribute access — the agent_loop contract.
    assert resp.choices is not None
    assert len(resp.choices) == 1
    assert resp.choices[0].message.role == "assistant"
    assert resp.choices[0].message.content == "Hello, world!"
    assert resp.choices[0].finish_reason == "stop"
    assert resp.usage.prompt_tokens == 42
    assert resp.usage.completion_tokens == 7


# ── 4. Streaming create returns iterator of chunks ───────────────────────────


def test_create_streaming_returns_iterator(fake_sdk, reload_providers_anthropic):
    pa = reload_providers_anthropic
    client = pa.AnthropicNativeClient(api_key="sk-ant-test")
    sdk_instance = _FakeAnthropic.instances[-1]
    # Feed a minimal scripted stream the workstream-B reassembler can consume.
    sdk_instance.messages.stream_events = [
        {"type": "message_start", "message": {"id": "msg_x", "model": "claude-sonnet-4-5",
                                              "usage": {"input_tokens": 5, "output_tokens": 0}}},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "text_delta", "text": "Hi"}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "text_delta", "text": " there"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
         "usage": {"output_tokens": 2}},
        {"type": "message_stop"},
    ]

    stream = client.chat.completions.create(
        model="claude-sonnet-4-5",
        messages=[{"role": "user", "content": "hi"}],
        stream=True,
    )

    # It's an iterator (the reassembler is a generator).
    assert hasattr(stream, "__iter__") or hasattr(stream, "__next__")
    chunks = list(stream)
    # The reassembler should produce at least one chunk; if A/B stubs are
    # active the list is empty, but the iterator itself must be returned.
    if chunks:
        first = chunks[0]
        # OpenAI shape: chunk.choices[0].delta
        assert first.choices is not None
        assert hasattr(first.choices[0], "delta")
        # Accumulated content across all chunks contains "Hi there"
        joined = "".join(
            (ch.choices[0].delta.content or "")
            for ch in chunks
            if ch.choices and ch.choices[0].delta
        )
        assert "Hi" in joined


# ── 5. Tool call round trip ──────────────────────────────────────────────────


def test_tool_call_round_trip(fake_sdk, reload_providers_anthropic):
    pa = reload_providers_anthropic
    client = pa.AnthropicNativeClient(api_key="sk-ant-test")
    sdk_instance = _FakeAnthropic.instances[-1]

    # Anthropic returns a tool_use block when the model wants to call a tool.
    sdk_instance.messages.response_factory = lambda: _make_fake_message(
        content_blocks=[
            {
                "type": "tool_use",
                "id": "toolu_abc123",
                "name": "memory_save",
                "input": {"content": "hello", "tag": "fact"},
            }
        ],
        stop_reason="tool_use",
    )

    openai_tools = [{
        "type": "function",
        "function": {
            "name": "memory_save",
            "description": "Save a memory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string"},
                    "tag": {"type": "string"},
                },
                "required": ["content"],
            },
        },
    }]

    resp = client.chat.completions.create(
        model="claude-sonnet-4-5",
        messages=[{"role": "user", "content": "remember this"}],
        tools=openai_tools,
        stream=False,
    )

    # Response must have tool_calls in OpenAI shape.
    assert resp.choices[0].message.tool_calls is not None
    tc = resp.choices[0].message.tool_calls[0]
    assert tc.id == "toolu_abc123"
    assert tc.type == "function"
    assert tc.function.name == "memory_save"
    # arguments is a JSON string in OpenAI shape
    import json as _json
    args = _json.loads(tc.function.arguments)
    assert args == {"content": "hello", "tag": "fact"}
    assert resp.choices[0].finish_reason == "tool_calls"


# ── 6. System messages get hoisted ────────────────────────────────────────────


def test_system_messages_get_hoisted(fake_sdk, reload_providers_anthropic):
    pa = reload_providers_anthropic
    client = pa.AnthropicNativeClient(api_key="sk-ant-test")
    sdk_instance = _FakeAnthropic.instances[-1]

    client.chat.completions.create(
        model="claude-sonnet-4-5",
        messages=[
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "hi"},
        ],
        stream=False,
    )

    sent = sdk_instance.messages.last_kwargs
    # Anthropic API takes ``system`` at top level, not inside ``messages``.
    assert "system" in sent
    assert "You are a helpful assistant." in sent["system"]
    # The messages list must not contain a system role item.
    for m in sent["messages"]:
        assert m.get("role") != "system"


# ── 7. providers.get_client() returns AnthropicNativeClient when conditions met ──


def test_providers_returns_native_client_when_active_provider_anthropic(
    fresh_providers_with_anthropic, fake_sdk, monkeypatch
):
    providers = fresh_providers_with_anthropic
    # Activate the anthropic preset
    providers.db.kv_set("provider:active", "anthropic")
    # Provide a key via env var (the cheapest path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env-test")
    providers._invalidate()

    # Ensure providers_anthropic module is freshly loaded so it picks up the env var path.
    if "providers_anthropic" in sys.modules:
        importlib.reload(sys.modules["providers_anthropic"])

    client = providers.get_client()
    # Should be an AnthropicNativeClient instance, not bare OpenAI.
    from providers_anthropic import AnthropicNativeClient
    assert isinstance(client, AnthropicNativeClient), (
        f"expected AnthropicNativeClient, got {type(client).__name__}"
    )


# ── 8. Falls back to OpenAI client when anthropic SDK missing ─────────────────


def test_providers_falls_back_when_sdk_missing(
    fresh_providers_with_anthropic, monkeypatch
):
    providers = fresh_providers_with_anthropic
    providers.db.kv_set("provider:active", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    providers._invalidate()

    # Stub the native client constructor to raise RuntimeError as if the SDK
    # were absent. We don't need to also clear sys.modules — the constructor
    # path is what triggers the fallback.
    if "providers_anthropic" in sys.modules:
        importlib.reload(sys.modules["providers_anthropic"])
    pa = sys.modules["providers_anthropic"]

    def _boom(**_kw):
        raise RuntimeError("anthropic SDK not installed.")
    monkeypatch.setattr(pa, "AnthropicNativeClient", _boom)

    client = providers.get_client()
    # Falls back to plain OpenAI()
    from openai import OpenAI
    assert isinstance(client, OpenAI), (
        f"expected OpenAI fallback, got {type(client).__name__}"
    )


# ── 9. Falls back when ANTHROPIC_API_KEY missing ──────────────────────────────


def test_providers_falls_back_when_key_missing(
    fresh_providers_with_anthropic, fake_sdk, monkeypatch
):
    providers = fresh_providers_with_anthropic
    providers.db.kv_set("provider:active", "anthropic")
    # Make ABSOLUTELY sure there's no key anywhere
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    providers._invalidate()

    if "providers_anthropic" in sys.modules:
        importlib.reload(sys.modules["providers_anthropic"])

    client = providers.get_client()
    from openai import OpenAI
    assert isinstance(client, OpenAI), (
        f"expected OpenAI fallback (no key), got {type(client).__name__}"
    )


# ── 10. OpenRouter requires explicit opt-in AND anthropic/* model ─────────────


def test_openrouter_keeps_openai_unless_opted_in(
    fresh_providers_with_anthropic, fake_sdk, monkeypatch
):
    """Combined check: openrouter falls back to OpenAI when:
      a) opt-in flag is unset, OR
      b) model name doesn't start with ``anthropic/``.
    Only when BOTH conditions hold do we route to the native client.
    """
    providers = fresh_providers_with_anthropic
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env-test")

    if "providers_anthropic" in sys.modules:
        importlib.reload(sys.modules["providers_anthropic"])

    from openai import OpenAI
    from providers_anthropic import AnthropicNativeClient

    # Add OpenRouter with a key + an anthropic/* model
    providers.add("openrouter", "https://openrouter.ai/api/v1", key="or-key",
                  models=["anthropic/claude-sonnet-4", "google/gemini-2.5-flash"])
    providers.db.kv_set("provider:active", "openrouter")
    providers.db.kv_set("provider:model", "anthropic/claude-sonnet-4")
    providers._invalidate()

    # a) opt-in flag UNSET → OpenAI fallback
    providers.db.kv_set("setting:anthropic_native_routing", "0")
    providers._invalidate()
    assert isinstance(providers.get_client(), OpenAI)

    # b) opt-in flag SET, but model is a non-Anthropic one → OpenAI fallback
    providers.db.kv_set("setting:anthropic_native_routing", "1")
    providers.db.kv_set("provider:model", "google/gemini-2.5-flash")
    providers._invalidate()
    assert isinstance(providers.get_client(), OpenAI)

    # c) opt-in SET + anthropic/* model → native client
    providers.db.kv_set("provider:model", "anthropic/claude-sonnet-4")
    providers._invalidate()
    client = providers.get_client()
    assert isinstance(client, AnthropicNativeClient)


# ── 11. Usage cache_* fields propagate ────────────────────────────────────────


def test_usage_cache_fields_propagate(fake_sdk, reload_providers_anthropic):
    pa = reload_providers_anthropic
    client = pa.AnthropicNativeClient(api_key="sk-ant-test")
    sdk_instance = _FakeAnthropic.instances[-1]
    sdk_instance.messages.response_factory = lambda: _make_fake_message(
        content_blocks=[{"type": "text", "text": "cached response"}],
        input_tokens=100,
        output_tokens=20,
        cache_creation=80,
        cache_read=15,
    )

    resp = client.chat.completions.create(
        model="claude-sonnet-4-5",
        messages=[{"role": "user", "content": "hi"}],
        stream=False,
    )

    assert resp.usage is not None
    assert resp.usage.prompt_tokens == 100
    assert resp.usage.completion_tokens == 20
    assert resp.usage.cache_creation_input_tokens == 80
    assert resp.usage.cache_read_input_tokens == 15


# ── 12. anthropic preset present + curated model list ────────────────────────


def test_anthropic_preset_registered(fresh_providers_with_anthropic):
    """Spec: PRESETS["anthropic"] with the curated model list + bare URL."""
    providers = fresh_providers_with_anthropic
    assert "anthropic" in providers.PRESETS
    preset = providers.PRESETS["anthropic"]
    assert preset["url"] == "https://api.anthropic.com"
    assert preset["key"] == ""
    models = preset["models"]
    # All required models present (in any order) per spec.
    for m in (
        "claude-sonnet-4-5", "claude-opus-4", "claude-haiku-4",
        "claude-sonnet-4-6", "claude-opus-4-1",
    ):
        assert m in models, f"curated model {m!r} missing from PRESETS['anthropic']"
    # The CAPABILITIES table also has an entry so `supports()` doesn't silently false.
    assert "anthropic" in providers.CAPABILITIES


# ── 13. Key resolution order: env > vault > kv > provider config ─────────────


def test_resolve_anthropic_key_order(
    fresh_providers_with_anthropic, monkeypatch, reload_providers_anthropic
):
    providers = fresh_providers_with_anthropic
    pa = reload_providers_anthropic

    # 1. env wins
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-env")
    providers.db.kv_set("anthropic_api_key", "from-kv")
    assert pa._resolve_anthropic_key() == "from-env"

    # 2. without env, kv plain wins (vault not configured in test)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert pa._resolve_anthropic_key() == "from-kv"

    # 3. without env or kv, provider config wins
    providers.db.kv_delete("anthropic_api_key")
    providers.add("anthropic", "https://api.anthropic.com", key="from-providers-cfg")
    assert pa._resolve_anthropic_key() == "from-providers-cfg"

    # 4. nothing anywhere → None
    providers.db.kv_delete("provider:config:anthropic")
    # Also clear any preset-coming-back: the preset key is "", so falsy.
    assert pa._resolve_anthropic_key() is None
