"""Native Anthropic adapter — duck-typed OpenAI client backed by the
anthropic SDK.

Constructed only when:
  - the ``anthropic`` Python SDK is importable, AND
  - an Anthropic API key is resolvable (env / kv / secret store / provider config), AND
  - the active provider is ``anthropic``, OR
  - the active provider is ``openrouter`` and the user has explicitly opted in
    via setting ``anthropic_native_routing=1`` for OpenRouter-routed Claude models.

This module is intentionally provider-isolated: the other 9 OpenAI-compatible
providers in ``providers.py`` keep using the bare OpenAI client. The adapter
hides Anthropic's slightly different request/response shape (system kwarg,
content blocks, stop_reason vocabulary) behind the same ``.chat.completions.create()``
method the rest of the codebase calls.

See ``docs/specs/2026-05-17-native-anthropic-adapter.md`` for the full spec.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator

import logger

_log = logger.get("providers_anthropic")


from providers_anthropic_convert import (
    from_anthropic_response,
    to_anthropic_request,
)
from providers_anthropic_stream import reassemble_anthropic_stream


# ── API key resolution ───────────────────────────────────────────────────────


def _resolve_anthropic_key() -> str | None:
    """Best-effort lookup of an Anthropic API key.

    Order, each step swallows exceptions so a missing module / corrupt store
    falls through cleanly:

      1. ``ANTHROPIC_API_KEY`` environment variable
      2. encrypted secrets vault (``secret_anthropic_api_key``)
      3. plaintext kv (``anthropic_api_key``) — legacy fallback
      4. provider config: ``providers.get_provider("anthropic")["key"]``

    Returns the first non-empty match or ``None`` if all are empty.
    """
    # 1. env
    env_key = os.environ.get("ANTHROPIC_API_KEY")
    if env_key:
        return env_key

    # 2. encrypted vault
    try:
        import vault  # local import — vault depends on db, avoid import-cycle pain
        v = vault.get("anthropic_api_key")
        if v:
            return v
    except Exception:  # nosec — fall through to the next source
        pass

    # 3. plain kv
    try:
        import db
        kv = db.kv_get("anthropic_api_key")
        if kv:
            return kv
    except Exception:
        pass

    # 4. provider config
    try:
        import providers
        p = providers.get_provider("anthropic")
        if p and p.get("key"):
            return p["key"]
    except Exception:
        pass

    return None


# ── OpenAI-shape response wrappers ───────────────────────────────────────────
#
# ``agent_loop`` and friends consume responses with attribute access
# (``response.choices[0].message.content`` etc.), not dict subscription.
# Build a recursive SimpleNamespace-style wrapper so dict-in dict-out
# converters round-trip into an object the existing code already handles.


@dataclass
class _Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass
class _ToolFnCall:
    name: str | None = None
    arguments: str | None = None


@dataclass
class _ToolCall:
    id: str | None = None
    type: str = "function"
    function: _ToolFnCall | None = None


@dataclass
class _Message:
    role: str = "assistant"
    content: str | None = None
    reasoning: str | None = None
    tool_calls: list[_ToolCall] | None = None


@dataclass
class _Choice:
    index: int = 0
    message: _Message = field(default_factory=_Message)
    finish_reason: str | None = None


@dataclass
class _Response:
    id: str | None = None
    model: str | None = None
    choices: list[_Choice] = field(default_factory=list)
    usage: _Usage | None = None


def _wrap_openai_shape(d: dict[str, Any]) -> _Response:
    """Turn the dict ``from_anthropic_response`` returns into the attribute-
    access shape the rest of the codebase expects."""
    choices_raw = d.get("choices") or []
    choices: list[_Choice] = []
    for cr in choices_raw:
        msg_raw = cr.get("message") or {}
        tool_calls_raw = msg_raw.get("tool_calls") or None
        tool_calls: list[_ToolCall] | None
        if tool_calls_raw is None:
            tool_calls = None
        else:
            tool_calls = []
            for tc in tool_calls_raw:
                fn_raw = tc.get("function") or {}
                tool_calls.append(_ToolCall(
                    id=tc.get("id"),
                    type=tc.get("type") or "function",
                    function=_ToolFnCall(
                        name=fn_raw.get("name"),
                        arguments=fn_raw.get("arguments"),
                    ),
                ))
        message = _Message(
            role=msg_raw.get("role") or "assistant",
            content=msg_raw.get("content"),
            reasoning=msg_raw.get("reasoning"),
            tool_calls=tool_calls,
        )
        choices.append(_Choice(
            index=cr.get("index", 0),
            message=message,
            finish_reason=cr.get("finish_reason"),
        ))

    usage_raw = d.get("usage") or {}
    usage: _Usage | None
    if usage_raw:
        usage = _Usage(
            prompt_tokens=int(usage_raw.get("prompt_tokens", 0) or 0),
            completion_tokens=int(usage_raw.get("completion_tokens", 0) or 0),
            total_tokens=int(usage_raw.get("total_tokens", 0) or 0),
            cache_creation_input_tokens=int(usage_raw.get("cache_creation_input_tokens", 0) or 0),
            cache_read_input_tokens=int(usage_raw.get("cache_read_input_tokens", 0) or 0),
        )
    else:
        usage = None

    return _Response(
        id=d.get("id"),
        model=d.get("model"),
        choices=choices,
        usage=usage,
    )


# ── Stream-event normalization ───────────────────────────────────────────────


def _iter_as_dicts(events: Iterable[Any]) -> Iterator[dict[str, Any]]:
    """The anthropic SDK yields typed event objects (or dicts in tests). The
    reassembler in workstream B expects dicts. Coerce each event.

    Strategy:
      - if event has ``model_dump`` (pydantic v2) → use it
      - elif event has ``dict()``                → use it (legacy)
      - elif event is already a dict             → pass through
      - else                                     → ``vars()`` fallback
    """
    for ev in events:
        if isinstance(ev, dict):
            yield ev
            continue
        dump = getattr(ev, "model_dump", None)
        if callable(dump):
            try:
                yield dump()
                continue
            except Exception:
                pass
        dump = getattr(ev, "dict", None)
        if callable(dump):
            try:
                yield dump()
                continue
            except Exception:
                pass
        try:
            yield vars(ev)
        except TypeError:
            # Unknown shape — emit an empty dict so the reassembler can skip it.
            yield {}


# ── The native client ────────────────────────────────────────────────────────


class AnthropicNativeClient:
    """Duck-typed ``OpenAI`` replacement backed by the official anthropic SDK.

    Exposes ``client.chat.completions.create(...)`` with the OpenAI signature
    so call sites in ``agent.py`` / ``agent_loop.py`` / ``synthesis.py`` etc.
    don't have to know which client they hold.

    The ``anthropic`` SDK is lazy-imported in ``__init__``: importing this
    module costs nothing if the SDK is absent, and the friendly error only
    fires when callers actually try to construct the client.
    """

    def __init__(self, *, api_key: str, base_url: str | None = None) -> None:
        try:
            import anthropic  # type: ignore[import-not-found]
        except ImportError as e:
            raise RuntimeError(
                "anthropic SDK not installed. Run: "
                "pip install 'castor[anthropic_native]' or pip install anthropic. "
                "Or use provider=openrouter for Claude models without the SDK."
            ) from e
        # base_url=None tells the SDK to use its built-in default
        # (https://api.anthropic.com). Pass an explicit value only when the
        # caller overrides it (e.g. for a regional mirror).
        kw: dict[str, Any] = {"api_key": api_key}
        if base_url:
            kw["base_url"] = base_url
        self._client = anthropic.Anthropic(**kw)
        self.chat = self._ChatNamespace(self)

    # ── Namespace shim so client.chat.completions.create(...) works ──

    class _ChatNamespace:
        def __init__(self, outer: "AnthropicNativeClient") -> None:
            self._outer = outer

        @property
        def completions(self) -> "AnthropicNativeClient":
            # The completions namespace just re-exposes the outer ``create``.
            return self._outer

    # ── The actual create method (OpenAI-compatible signature) ──

    def create(
        self,
        *,
        model: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        stream: bool = False,
        max_tokens: int = 4096,
        temperature: float | None = None,
        **_kw: Any,  # absorb unsupported kwargs (top_p, frequency_penalty, ...)
    ) -> Any:
        """OpenAI-compatible: translate, dispatch, translate back.

        Extra kwargs are silently absorbed — Anthropic's API has a much
        narrower surface than OpenAI's (no logit_bias / frequency_penalty
        / etc.), so we drop them rather than rejecting them.
        """
        req = to_anthropic_request(
            model=model,
            messages=messages,
            tools=tools,
            stream=stream,
            max_tokens=max_tokens,
            temperature=temperature,
        )

        if stream:
            ant_stream = self._client.messages.create(**req)
            return reassemble_anthropic_stream(_iter_as_dicts(ant_stream))

        resp = self._client.messages.create(**req)
        # The SDK returns a Message object; ``model_dump()`` gives the dict
        # ``from_anthropic_response`` expects. Fall back to ``dict()`` /
        # ``vars()`` for non-pydantic test fakes.
        if hasattr(resp, "model_dump") and callable(resp.model_dump):
            resp_dict = resp.model_dump()
        elif hasattr(resp, "dict") and callable(resp.dict):
            resp_dict = resp.dict()
        elif isinstance(resp, dict):
            resp_dict = resp
        else:
            resp_dict = vars(resp)
        return _wrap_openai_shape(from_anthropic_response(resp_dict))
