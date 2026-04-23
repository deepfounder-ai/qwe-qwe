"""Tests for Memento experience learning: _save_experience() and _auto_context().

Uses the real ``agent``, ``memory`` and ``config`` modules. Each test
monkeypatches ``memory.save`` / ``memory.search_by_vector`` (and
optionally ``config.get``) so nothing touches the real Qdrant index or
SQLite settings table. All patches auto-revert at end of test.
"""

from __future__ import annotations

import time
import types

import pytest


@pytest.fixture
def agent_mod(monkeypatch):
    """Import agent with neutral memory.embed + sparse_embed so tests don't
    need a real embedder loaded."""
    import agent
    import memory as memory_mod

    # Keep embeddings deterministic and cheap
    monkeypatch.setattr(memory_mod, "embed", lambda text: [0.0] * 384, raising=False)
    monkeypatch.setattr(
        memory_mod,
        "sparse_embed",
        lambda text: types.SimpleNamespace(indices=[0], values=[1.0]),
        raising=False,
    )
    # Default search paths: empty results (tests override per-case)
    monkeypatch.setattr(memory_mod, "search", lambda *a, **kw: [], raising=False)
    monkeypatch.setattr(memory_mod, "search_by_vector", lambda *a, **kw: [], raising=False)
    monkeypatch.setattr(memory_mod, "search_grouped", lambda *a, **kw: [], raising=False)
    monkeypatch.setattr(memory_mod, "save", lambda *a, **kw: "ok", raising=False)
    return agent


@pytest.fixture
def experience_enabled(monkeypatch):
    """Force config.get('experience_learning') to 1 regardless of real DB."""
    import config as _cfg

    real_get = _cfg.get

    def _patched(key: str):
        if key == "experience_learning":
            return 1
        return real_get(key)

    monkeypatch.setattr(_cfg, "get", _patched)


@pytest.fixture
def experience_disabled(monkeypatch):
    """Force config.get('experience_learning') to 0."""
    import config as _cfg

    real_get = _cfg.get

    def _patched(key: str):
        if key == "experience_learning":
            return 0
        return real_get(key)

    monkeypatch.setattr(_cfg, "get", _patched)


# ── Helpers ──

def _make_result(agent_mod, tools=None, reply="Done.", thinking=""):
    r = agent_mod.TurnResult()
    r.tool_calls_made = tools or []
    r.reply = reply
    r.thinking = thinking
    r.auto_context_hits = 0
    r.json_repairs = 0
    r.retry_successes = 0
    r.self_check_fixes = 0
    return r


def _capture_save(monkeypatch):
    """Install a capturing memory.save and return the calls list."""
    import memory as memory_mod

    calls: list[dict] = []

    def _save(text, tag=None, dedup=True, thread_id=None, meta=None):
        calls.append({"text": text, "tag": tag, "thread_id": thread_id, "meta": meta})
        return "ok"

    monkeypatch.setattr(memory_mod, "save", _save)
    return calls


def _install_vector_search(monkeypatch, search_fn):
    """Patch memory.search_by_vector to delegate to a test's _search(...)."""
    import memory as memory_mod

    def _mock_sbv(vec, limit=3, tag=None, thread_id=None,
                  query_text=None, score_threshold=None):
        results = search_fn("", limit=limit, tag=tag, thread_id=thread_id)
        if score_threshold is not None:
            results = [r for r in results if r["score"] >= score_threshold]
        return results

    monkeypatch.setattr(memory_mod, "search_by_vector", _mock_sbv)


# ── Block 1: _save_experience() ──

def test_1_1_basic_case_format(agent_mod, experience_enabled, monkeypatch):
    """Case contains [EXP], tool name, and 'success'."""
    calls = _capture_save(monkeypatch)
    result = _make_result(agent_mod, tools=["weather_get"], reply="Погода: 15°C, дождь")
    agent_mod._save_experience("проверь погоду", result, rounds=2, fail_count=0, _sync=True)

    assert calls, "memory.save was not called"
    text = calls[0]["text"]
    assert "[EXP]" in text
    assert "weather_get" in text
    assert "success" in text


def test_1_2_outcome_partial(agent_mod, experience_enabled, monkeypatch):
    """fail_count=1 → Result: partial."""
    calls = _capture_save(monkeypatch)
    result = _make_result(agent_mod, tools=["shell"])
    agent_mod._save_experience("запусти скрипт", result, rounds=2, fail_count=1, _sync=True)
    assert calls
    assert "partial" in calls[0]["text"]


def test_1_3_outcome_failed(agent_mod, experience_enabled, monkeypatch):
    """fail_count=2 → Result: failed."""
    calls = _capture_save(monkeypatch)
    result = _make_result(agent_mod, tools=["shell"])
    agent_mod._save_experience("сделай что-то", result, rounds=3, fail_count=2, _sync=True)
    assert calls
    assert "failed" in calls[0]["text"]


def test_1_4_long_input_truncated(agent_mod, experience_enabled, monkeypatch):
    """user_input > 80 chars is truncated in case text."""
    calls = _capture_save(monkeypatch)
    long_input = "а" * 120
    result = _make_result(agent_mod, tools=["shell"])
    agent_mod._save_experience(long_input, result, rounds=2, fail_count=0, _sync=True)

    assert calls
    text = calls[0]["text"]
    task_part = text.split("| Tools:")[0].replace("[EXP] Task: ", "").strip()
    assert len(task_part) <= 80


def test_1_5_dedup_tools(agent_mod, experience_enabled, monkeypatch):
    """Repeated tools are deduplicated, order preserved."""
    calls = _capture_save(monkeypatch)
    result = _make_result(agent_mod, tools=["shell", "shell", "write_file", "shell"])
    agent_mod._save_experience("напиши файл", result, rounds=3, fail_count=0, _sync=True)

    assert calls
    text = calls[0]["text"]
    tools_part = text.split("| Tools: ")[1].split(" |")[0]
    assert tools_part == "shell, write_file"


def test_1_6_no_save_when_no_tools(agent_mod, experience_enabled, monkeypatch):
    """Empty tool_calls_made → memory.save not called."""
    calls = _capture_save(monkeypatch)
    result = _make_result(agent_mod, tools=[])
    agent_mod._save_experience("просто вопрос", result, rounds=0, fail_count=0, _sync=True)
    assert not calls


def test_1_7_no_save_when_disabled(agent_mod, experience_disabled, monkeypatch):
    """experience_learning=0 → memory.save not called."""
    calls = _capture_save(monkeypatch)
    result = _make_result(agent_mod, tools=["shell"])
    agent_mod._save_experience("запусти что-то", result, rounds=1, fail_count=0, _sync=True)
    assert not calls


def test_1_8_tag_experience_and_no_thread(agent_mod, experience_enabled, monkeypatch):
    """Always saves with tag='experience' and thread_id=None."""
    calls = _capture_save(monkeypatch)
    result = _make_result(agent_mod, tools=["write_file"])
    agent_mod._save_experience("напиши конфиг в config.yml", result,
                                rounds=2, fail_count=0, _sync=True)

    assert calls
    assert calls[0]["tag"] == "experience"
    assert calls[0]["thread_id"] is None


# ── Block 2: _auto_context() experience retrieval ──

def _make_exp_result(text, score, outcome_score=1.0):
    return {"text": text, "tag": "experience", "thread_id": None, "score": score,
            "ts": time.time(), "outcome_score": outcome_score}


def test_2_1_experience_injected_into_context(agent_mod, experience_enabled, monkeypatch):
    exp_case = "[EXP] Task: проверь погоду | Tools: weather_get | Steps: 1 | Result: success | Learned: OK"

    def _search(query, limit=3, tag=None, thread_id=None):
        if tag == "experience":
            return [_make_exp_result(exp_case, 0.8)]
        return []

    _install_vector_search(monkeypatch, _search)
    ctx = agent_mod._auto_context("какая погода?")

    assert "[Relevant past experiences:]" in ctx
    assert exp_case in ctx


def test_2_2_low_score_filtered_out(agent_mod, experience_enabled, monkeypatch):
    def _search(query, limit=3, tag=None, thread_id=None):
        if tag == "experience":
            return [_make_exp_result("[EXP] Task: something", 0.35)]
        return []

    _install_vector_search(monkeypatch, _search)
    ctx = agent_mod._auto_context("что-то сделай")
    assert "[Relevant past experiences:]" not in ctx


def test_2_3_max_experience_results(agent_mod, experience_enabled, monkeypatch):
    """At most MAX_EXPERIENCE_RESULTS (2) cases injected."""
    import config as _cfg

    def _search(query, limit=3, tag=None, thread_id=None):
        if tag == "experience":
            return [_make_exp_result(f"[EXP] Task: task{i}", 0.9) for i in range(5)]
        return []

    _install_vector_search(monkeypatch, _search)
    ctx = agent_mod._auto_context("похожая задача")

    exp_lines = [l for l in ctx.split("\n") if l.startswith("- [EXP]")]
    assert len(exp_lines) <= _cfg.MAX_EXPERIENCE_RESULTS


def test_2_4_no_search_when_disabled(agent_mod, experience_disabled, monkeypatch):
    """When experience_learning=0, search_by_vector with tag=experience is not called."""
    import memory as memory_mod

    calls = []

    def _tracking_search(vec, limit=3, tag=None, thread_id=None,
                         query_text=None, score_threshold=None):
        calls.append({"tag": tag, "thread_id": thread_id})
        return []

    monkeypatch.setattr(memory_mod, "search_by_vector", _tracking_search)

    agent_mod._auto_context("любой запрос")

    exp_calls = [c for c in calls if c["tag"] == "experience"]
    assert not exp_calls, "Should not search for experience when disabled"


def test_2_5_dedup_memory_and_experience(agent_mod, experience_enabled, monkeypatch):
    """Same text in both memory and experience appears only once."""
    shared_text = "одинаковый текст"

    def _search(query, limit=3, tag=None, thread_id=None):
        if tag == "experience":
            return [_make_exp_result(shared_text, 0.9)]
        return [{"text": shared_text, "tag": "general", "thread_id": None,
                 "score": 0.8, "ts": time.time()}]

    _install_vector_search(monkeypatch, _search)
    ctx = agent_mod._auto_context("запрос")

    count = ctx.count(shared_text)
    assert count == 1, f"Duplicate text appeared {count} times in context"


def test_2_6_experience_additive_to_memory(agent_mod, experience_enabled, monkeypatch):
    """Experience slots are additive, not replacing memory slots."""
    normal_memories = [
        {"text": f"memory {i}", "tag": "general", "thread_id": None,
         "score": 0.8, "ts": time.time()}
        for i in range(3)
    ]
    exp_cases = [_make_exp_result(f"[EXP] Task: exp{i}", 0.9) for i in range(2)]

    def _search(query, limit=3, tag=None, thread_id=None):
        if tag == "experience":
            return exp_cases
        return normal_memories

    _install_vector_search(monkeypatch, _search)
    ctx = agent_mod._auto_context("задача")

    mem_lines = [l for l in ctx.split("\n") if "memory" in l and l.startswith("- ")]
    exp_lines = [l for l in ctx.split("\n") if "[EXP]" in l and l.startswith("- ")]

    assert len(mem_lines) == 3, f"Expected 3 memory lines, got {len(mem_lines)}"
    assert len(exp_lines) == 2, f"Expected 2 experience lines, got {len(exp_lines)}"


# ── Block 3: Integration save → retrieve ──

def test_3_1_save_then_retrieve(agent_mod, experience_enabled, monkeypatch):
    """Full cycle: save experience case, then retrieve it via _auto_context."""
    import memory as memory_mod

    saved_cases: list[dict] = []

    def _save(text, tag=None, dedup=True, thread_id=None, meta=None):
        if tag == "experience":
            saved_cases.append({"text": text, "score": 0.85})
        return "ok"

    def _search(query, limit=3, tag=None, thread_id=None):
        if tag == "experience":
            return [{"text": c["text"], "tag": "experience",
                     "thread_id": None, "score": c["score"], "ts": time.time()}
                    for c in saved_cases]
        return []

    monkeypatch.setattr(memory_mod, "save", _save)
    _install_vector_search(monkeypatch, _search)

    result = _make_result(agent_mod, tools=["weather_get"], reply="Погода: 20°C")
    agent_mod._save_experience("проверь погоду в Москве", result,
                                rounds=2, fail_count=0, _sync=True)
    assert saved_cases, "Case was not saved"

    ctx = agent_mod._auto_context("какая погода в Лондоне?")
    assert "[Relevant past experiences:]" in ctx
    assert saved_cases[0]["text"] in ctx


# ── Block 4: Outcome scoring ──

def test_4_1_outcome_score_saved_in_meta(agent_mod, experience_enabled, monkeypatch):
    calls = _capture_save(monkeypatch)
    result = _make_result(agent_mod, tools=["shell"], reply="Done")
    agent_mod._save_experience("запусти скрипт", result, rounds=2, fail_count=0, _sync=True)

    assert calls
    assert calls[0]["meta"] is not None
    assert calls[0]["meta"]["outcome_score"] == 1.0


def test_4_2_partial_outcome_score(agent_mod, experience_enabled, monkeypatch):
    calls = _capture_save(monkeypatch)
    result = _make_result(agent_mod, tools=["shell"])
    agent_mod._save_experience("задача", result, rounds=2, fail_count=1, _sync=True)

    assert calls
    assert calls[0]["meta"]["outcome_score"] == 0.6


def test_4_3_failed_outcome_score(agent_mod, experience_enabled, monkeypatch):
    calls = _capture_save(monkeypatch)
    result = _make_result(agent_mod, tools=["shell"])
    agent_mod._save_experience("задача", result, rounds=3, fail_count=2, _sync=True)

    assert calls
    assert calls[0]["meta"]["outcome_score"] == 0.2


def test_4_4_success_beats_failed_by_composite_score(agent_mod, experience_enabled, monkeypatch):
    """Success case with lower similarity wins over failed case with higher similarity.

    Both scores must clear ``EXPERIENCE_SCORE_MIN`` (0.65) so the mock's
    threshold filter doesn't drop them before the composite-score logic
    even sees them. The point of the test is the outcome-score weighting:
    0.7*1.0=0.7 beats 0.9*0.2=0.18 even though raw similarity is lower.
    """
    success_case = _make_exp_result(
        "[EXP] Task: good | Tools: shell | Steps: 1 | Result: success", 0.7, outcome_score=1.0)
    failed_case = _make_exp_result(
        "[EXP] Task: bad | Tools: shell | Steps: 1 | Result: failed", 0.9, outcome_score=0.2)

    def _search(query, limit=3, tag=None, thread_id=None):
        if tag == "experience":
            return [failed_case, success_case]
        return []

    _install_vector_search(monkeypatch, _search)
    ctx = agent_mod._auto_context("задача")

    assert "good" in ctx
    assert "bad" not in ctx


def test_4_5_failed_case_filtered_by_composite_score(agent_mod, experience_enabled, monkeypatch):
    """Failed case with high semantic similarity still filtered out."""
    failed_case = _make_exp_result(
        "[EXP] Task: fail | Tools: shell | Steps: 1 | Result: failed", 0.9, outcome_score=0.2)

    def _search(query, limit=3, tag=None, thread_id=None):
        if tag == "experience":
            return [failed_case]
        return []

    _install_vector_search(monkeypatch, _search)
    ctx = agent_mod._auto_context("задача")

    assert "[Relevant past experiences:]" not in ctx
