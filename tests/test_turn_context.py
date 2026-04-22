"""Cross-source isolation tests for :mod:`turn_context`.

The TurnContext refactor's whole reason to exist: two concurrent agent turns
(web + telegram in the same process) must NOT share per-turn state. These
tests run emit calls on two threads with two different ctx instances and
assert each callback only ever saw its own ctx's content.
"""
from __future__ import annotations

import threading
import time

import agent
from turn_context import TurnContext, get_current, set_current, reset


def test_default_ctx_is_noop():
    """With no ctx installed, emit helpers must be silent (not crash)."""
    # No callbacks attached — must not raise.
    agent._emit_content("hello")
    agent._emit_thinking("h")
    agent._emit_status("s")
    agent._emit_tool_call("x", "{}", "ok")
    agent._emit_recall([{"tag": "fact", "text": "x"}])


def test_ctx_routes_to_callbacks():
    """A ctx installed on the ContextVar routes emits to its callbacks only."""
    got: list[str] = []
    ctx = TurnContext(on_content=lambda t: got.append(t))
    tok = set_current(ctx)
    try:
        agent._emit_content("abc")
        agent._emit_content("def")
    finally:
        reset(tok)
    assert got == ["abc", "def"]
    # After reset: another emit must not reach this ctx.
    agent._emit_content("ignored")
    assert got == ["abc", "def"]


def test_cross_source_isolation_two_threads():
    """Web turn + Telegram turn run in parallel → no callback crosstalk.

    This is the core property the refactor set out to preserve. We build
    two TurnContexts, install each on its own thread's ContextVar via
    ``set_current``, emit a stream of labelled content chunks, and assert
    that callback A only ever saw A's labels and callback B only saw B's.
    """
    a_got: list[str] = []
    b_got: list[str] = []

    ctx_a = TurnContext(source="web", on_content=lambda t: a_got.append(t),
                         on_status=lambda t: a_got.append(f"status:{t}"))
    ctx_b = TurnContext(source="telegram", on_content=lambda t: b_got.append(t),
                         on_status=lambda t: b_got.append(f"status:{t}"))

    # Barrier so both threads enter the emit loop at about the same time,
    # maximising the chance of interleaving.
    start = threading.Barrier(2)

    def _run(ctx: TurnContext, label: str, sink: list[str]) -> None:
        # contextvars isolate per-thread automatically in the default copy:
        # each spawned thread starts with an EMPTY context (no inherited
        # active ctx). So set_current on this thread affects only this
        # thread's emit helpers.
        start.wait(timeout=5)
        tok = set_current(ctx)
        try:
            for i in range(50):
                agent._emit_content(f"{label}:{i}")
                agent._emit_status(f"{label}:{i}")
                # yield the GIL a bit so the other thread can interleave
                if i % 5 == 0:
                    time.sleep(0.0005)
        finally:
            reset(tok)

    t_a = threading.Thread(target=_run, args=(ctx_a, "A", a_got))
    t_b = threading.Thread(target=_run, args=(ctx_b, "B", b_got))
    t_a.start()
    t_b.start()
    t_a.join()
    t_b.join()

    # Every A callback must have seen only A-labelled payloads, and vice versa.
    assert a_got, "callback A received nothing"
    assert b_got, "callback B received nothing"
    for item in a_got:
        assert item.startswith("A:") or item.startswith("status:A:"), f"A leaked: {item!r}"
    for item in b_got:
        assert item.startswith("B:") or item.startswith("status:B:"), f"B leaked: {item!r}"

    # And both threads each sent 100 events (50 content + 50 status).
    assert len(a_got) == 100
    assert len(b_got) == 100


def test_pending_image_and_file_are_per_ctx():
    """image_path / file_meta live on the ctx — two ctxs don't see each other's."""
    ctx_a = TurnContext(image_path="/uploads/a.png",
                         file_meta={"name": "a.txt", "path": "/up/a.txt", "size": 10})
    ctx_b = TurnContext(image_path="/uploads/b.png",
                         file_meta={"name": "b.txt", "path": "/up/b.txt", "size": 20})
    assert ctx_a.image_path != ctx_b.image_path
    assert ctx_a.file_meta["name"] == "a.txt"
    assert ctx_b.file_meta["name"] == "b.txt"
    # Setting the legacy shim globals must not affect other ctxs that already
    # have image_path set.
    assert ctx_a.image_path == "/uploads/a.png"


def test_abort_event_is_per_ctx():
    """Setting one ctx's abort_event does NOT set another's."""
    ctx_a = TurnContext()
    ctx_b = TurnContext()
    assert ctx_a.abort_event is not ctx_b.abort_event
    ctx_a.abort_event.set()
    assert ctx_a.abort_event.is_set()
    assert not ctx_b.abort_event.is_set()


def test_legacy_shim_harvests_into_ctx():
    """Writing to the deprecated module globals must still work (back-compat).

    Pre-ctx callers did ``agent._content_callback = fn`` before ``agent.run()``.
    The harvester reads those back into the auto-built ctx. This test
    exercises the harvester directly (without spinning up the whole agent
    loop, which needs a real LLM).
    """
    received: list[str] = []

    # Simulate an old caller that sets the module global.
    agent._content_callback = lambda t: received.append(t)
    try:
        ctx = TurnContext()
        # Ctx starts with no on_content — harvester should populate it.
        assert ctx.on_content is None
        agent._harvest_legacy_slots(ctx)
        assert ctx.on_content is not None
        ctx.emit_content("via-harvest")
        assert received == ["via-harvest"]
    finally:
        # Clean up the legacy slot so other tests don't see it.
        try:
            del agent.__dict__["_content_callback"]
        except KeyError:
            pass


def test_ctx_reset_restores_previous():
    """Nested set_current / reset cycles restore the outer ctx."""
    outer = TurnContext(source="outer")
    inner = TurnContext(source="inner")
    tok_outer = set_current(outer)
    try:
        assert get_current() is outer
        tok_inner = set_current(inner)
        try:
            assert get_current() is inner
        finally:
            reset(tok_inner)
        assert get_current() is outer
    finally:
        reset(tok_outer)
    assert get_current() is not outer
    assert get_current() is not inner
