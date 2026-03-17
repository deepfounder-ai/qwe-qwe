"""Tests for Memento experience learning: _save_experience() and _auto_context()."""

import sys
import time
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# ── Mock all dependencies before importing agent ──
import types

mock_db = types.ModuleType("db")
mock_db.kv_get = lambda *a, **kw: None
mock_db.kv_set = lambda *a, **kw: None
mock_db._get_conn = lambda: None
mock_db.get_recent_messages = lambda *a, **kw: []
mock_db.save_message = lambda *a, **kw: None
mock_db.count_messages = lambda *a, **kw: 0
mock_db.kv_get_prefix = lambda *a, **kw: {}
sys.modules["db"] = mock_db

# memory is replaced per-test to capture calls
mock_memory = types.ModuleType("memory")
mock_memory.search = lambda *a, **kw: []
mock_memory.save = lambda *a, **kw: "ok"
mock_memory.delete = lambda *a, **kw: True
sys.modules["memory"] = mock_memory

mock_logger = types.ModuleType("logger")
mock_logger.get = lambda name: types.SimpleNamespace(
    info=lambda *a, **kw: None,
    warning=lambda *a, **kw: None,
    error=lambda *a, **kw: None,
    debug=lambda *a, **kw: None,
)
mock_logger.event = lambda *a, **kw: None
sys.modules["logger"] = mock_logger

_experience_learning_enabled = 1

mock_config = types.ModuleType("config")
mock_config.LLM_BASE_URL = "http://localhost:1234/v1"
mock_config.LLM_MODEL = "test"
mock_config.LLM_API_KEY = "test"
mock_config.EMBED_BASE_URL = "http://localhost:1234/v1"
mock_config.EMBED_MODEL = "test"
mock_config.EMBED_API_KEY = "test"
mock_config.EMBED_DIM = 768
mock_config.QDRANT_MODE = "memory"
mock_config.QDRANT_PATH = "./memory"
mock_config.QDRANT_URL = "http://localhost:6333"
mock_config.QDRANT_COLLECTION = "test"
mock_config.DB_PATH = ":memory:"
mock_config.TZ_OFFSET = 0
mock_config.MAX_HISTORY_MESSAGES = 4
mock_config.MAX_MEMORY_RESULTS = 3
mock_config.MAX_EXPERIENCE_RESULTS = 2
mock_config.MAX_TOOL_ROUNDS = 10
mock_config.COMPACTION_THRESHOLD = 20
mock_config.THINKING_ENABLED = False
mock_config.EDITABLE_SETTINGS = {
    "tool_retry_max":       ("setting:tool_retry_max",       int, 3, "Max retries", 0, 5),
    "self_check_enabled":   ("setting:self_check_enabled",   int, 1, "Self-check",  0, 1),
    "experience_learning":  ("setting:experience_learning",  int, 1, "Experience learning", 0, 1),
    "max_memory_results":   ("setting:max_memory_results",   int, 3, "Memory results", 0, 10),
}
mock_config.get = lambda key: (
    _experience_learning_enabled if key == "experience_learning"
    else mock_config.EDITABLE_SETTINGS[key][2]
)
sys.modules["config"] = mock_config

mock_providers = types.ModuleType("providers")
mock_providers.get_model = lambda: "test-model"
mock_providers.get_client = lambda: None
mock_providers.ensure_model_loaded = lambda: True
sys.modules["providers"] = mock_providers

mock_soul = types.ModuleType("soul")
mock_soul.load = lambda: {}
mock_soul.to_prompt = lambda s: "system prompt"
mock_soul.get_temperature = lambda: 0.7
sys.modules["soul"] = mock_soul

mock_threads = types.ModuleType("threads")
mock_threads.get_active_id = lambda: "test"
mock_threads.get = lambda tid: None
mock_threads.touch = lambda tid: None
sys.modules["threads"] = mock_threads

mock_tools = types.ModuleType("tools")
mock_tools.get_all_tools = lambda compact=False: []
mock_tools.execute = lambda *a, **kw: "ok"
sys.modules["tools"] = mock_tools

mock_skills = types.ModuleType("skills")
mock_skills.get_instruction = lambda name: None
sys.modules["skills"] = mock_skills

import agent


# ── Helpers ──

def make_result(tools=None, reply="Done.", thinking=""):
    r = agent.TurnResult()
    r.tool_calls_made = tools or []
    r.reply = reply
    r.thinking = thinking
    r.auto_context_hits = 0
    r.json_repairs = 0
    r.retry_successes = 0
    r.self_check_fixes = 0
    return r


def capture_save():
    """Returns (patched save fn, list that receives calls)."""
    calls = []

    def _save(text, tag=None, dedup=True, thread_id=None, meta=None):
        calls.append({"text": text, "tag": tag, "thread_id": thread_id, "meta": meta})
        return "ok"

    return _save, calls


def capture_search():
    """Returns (patched search fn, list that receives calls)."""
    calls = []

    def _search(query, limit=3, tag=None, thread_id=None):
        calls.append({"query": query, "tag": tag, "thread_id": thread_id})
        return []

    return _search, calls


# ── Block 1: _save_experience() ──

def test_1_1_basic_case_format():
    """Case contains [EXP], tool name, and 'success'."""
    save_fn, calls = capture_save()
    mock_memory.save = save_fn

    result = make_result(tools=["weather_get"], reply="Погода: 15°C, дождь")
    agent._save_experience("проверь погоду", result, rounds=1, fail_count=0)
    time.sleep(0.05)  # wait for daemon thread

    assert calls, "memory.save was not called"
    text = calls[0]["text"]
    assert "[EXP]" in text
    assert "weather_get" in text
    assert "success" in text


def test_1_2_outcome_partial():
    """fail_count=1 → Result: partial."""
    save_fn, calls = capture_save()
    mock_memory.save = save_fn

    result = make_result(tools=["shell"])
    agent._save_experience("запусти скрипт", result, rounds=2, fail_count=1)
    time.sleep(0.05)

    assert calls
    assert "partial" in calls[0]["text"]


def test_1_3_outcome_failed():
    """fail_count=2 → Result: failed."""
    save_fn, calls = capture_save()
    mock_memory.save = save_fn

    result = make_result(tools=["shell"])
    agent._save_experience("сделай что-то", result, rounds=3, fail_count=2)
    time.sleep(0.05)

    assert calls
    assert "failed" in calls[0]["text"]


def test_1_4_long_input_truncated():
    """user_input > 80 chars is truncated in case text."""
    save_fn, calls = capture_save()
    mock_memory.save = save_fn

    long_input = "а" * 120
    result = make_result(tools=["shell"])
    agent._save_experience(long_input, result, rounds=1, fail_count=0)
    time.sleep(0.05)

    assert calls
    # Extract Task: field
    text = calls[0]["text"]
    task_part = text.split("| Tools:")[0].replace("[EXP] Task: ", "").strip()
    assert len(task_part) <= 80


def test_1_5_dedup_tools():
    """Repeated tools are deduplicated, order preserved."""
    save_fn, calls = capture_save()
    mock_memory.save = save_fn

    result = make_result(tools=["shell", "shell", "write_file", "shell"])
    agent._save_experience("напиши файл", result, rounds=3, fail_count=0)
    time.sleep(0.05)

    assert calls
    text = calls[0]["text"]
    # Extract Tools: field
    tools_part = text.split("| Tools: ")[1].split(" |")[0]
    assert tools_part == "shell, write_file"


def test_1_6_no_save_when_no_tools():
    """Empty tool_calls_made → memory.save not called."""
    calls = []
    mock_memory.save = lambda *a, **kw: calls.append(1) or "ok"

    result = make_result(tools=[])
    agent._save_experience("просто вопрос", result, rounds=0, fail_count=0)
    time.sleep(0.05)

    assert not calls, "memory.save should not be called for turns without tools"


def test_1_7_no_save_when_disabled():
    """experience_learning=0 → memory.save not called."""
    global _experience_learning_enabled
    _experience_learning_enabled = 0

    calls = []
    mock_memory.save = lambda *a, **kw: calls.append(1) or "ok"

    result = make_result(tools=["shell"])
    agent._save_experience("запусти что-то", result, rounds=1, fail_count=0)
    time.sleep(0.05)

    _experience_learning_enabled = 1  # restore
    assert not calls, "memory.save should not be called when experience_learning=0"


def test_1_8_tag_experience_and_no_thread():
    """Always saves with tag='experience' and thread_id=None."""
    save_fn, calls = capture_save()
    mock_memory.save = save_fn

    result = make_result(tools=["memory_save"])
    agent._save_experience("запомни это", result, rounds=1, fail_count=0)
    time.sleep(0.05)

    assert calls
    assert calls[0]["tag"] == "experience"
    assert calls[0]["thread_id"] is None


# ── Block 2: _auto_context() experience retrieval ──

def _make_exp_result(text, score, outcome_score=1.0):
    return {"text": text, "tag": "experience", "thread_id": None, "score": score,
            "ts": time.time(), "outcome_score": outcome_score}


def test_2_1_experience_injected_into_context():
    """When experience search returns results, they appear in context."""
    exp_case = "[EXP] Task: проверь погоду | Tools: weather_get | Steps: 1 | Result: success | Learned: OK"

    def _search(query, limit=3, tag=None, thread_id=None):
        if tag == "experience":
            return [_make_exp_result(exp_case, 0.8)]
        return []

    mock_memory.search = _search
    ctx = agent._auto_context("какая погода?")

    assert "[Relevant past experiences:]" in ctx
    assert exp_case in ctx


def test_2_2_low_score_filtered_out():
    """Experience cases with score <= 0.4 are not injected."""
    def _search(query, limit=3, tag=None, thread_id=None):
        if tag == "experience":
            return [_make_exp_result("[EXP] Task: something", 0.35)]
        return []

    mock_memory.search = _search
    ctx = agent._auto_context("что-то сделай")

    assert "[Relevant past experiences:]" not in ctx


def test_2_3_max_experience_results():
    """At most MAX_EXPERIENCE_RESULTS (2) cases injected."""
    def _search(query, limit=3, tag=None, thread_id=None):
        if tag == "experience":
            return [
                _make_exp_result(f"[EXP] Task: task{i}", 0.9)
                for i in range(5)
            ]
        return []

    mock_memory.search = _search
    ctx = agent._auto_context("похожая задача")

    exp_lines = [l for l in ctx.split("\n") if l.startswith("- [EXP]")]
    assert len(exp_lines) <= mock_config.MAX_EXPERIENCE_RESULTS


def test_2_4_no_search_when_disabled():
    """When experience_learning=0, memory.search with tag=experience is not called."""
    global _experience_learning_enabled
    _experience_learning_enabled = 0

    search_fn, calls = capture_search()
    mock_memory.search = search_fn

    agent._auto_context("любой запрос")
    _experience_learning_enabled = 1

    exp_calls = [c for c in calls if c["tag"] == "experience"]
    assert not exp_calls, "Should not search for experience when disabled"


def test_2_5_dedup_memory_and_experience():
    """Same text in both memory and experience appears only once."""
    shared_text = "одинаковый текст"

    def _search(query, limit=3, tag=None, thread_id=None):
        if tag == "experience":
            return [_make_exp_result(shared_text, 0.9)]
        # Regular memory search also returns same text
        return [{"text": shared_text, "tag": "general", "thread_id": None, "score": 0.8, "ts": time.time()}]

    mock_memory.search = _search
    ctx = agent._auto_context("запрос")

    count = ctx.count(shared_text)
    assert count == 1, f"Duplicate text appeared {count} times in context"


def test_2_6_experience_additive_to_memory():
    """Experience slots are additive, not replacing memory slots."""
    normal_memories = [
        {"text": f"memory {i}", "tag": "general", "thread_id": None, "score": 0.8, "ts": time.time()}
        for i in range(3)
    ]
    exp_cases = [
        _make_exp_result(f"[EXP] Task: exp{i}", 0.9)
        for i in range(2)
    ]

    def _search(query, limit=3, tag=None, thread_id=None):
        if tag == "experience":
            return exp_cases
        return normal_memories

    mock_memory.search = _search
    ctx = agent._auto_context("задача")

    mem_lines = [l for l in ctx.split("\n") if "memory" in l and l.startswith("- ")]
    exp_lines = [l for l in ctx.split("\n") if "[EXP]" in l and l.startswith("- ")]

    assert len(mem_lines) == 3, f"Expected 3 memory lines, got {len(mem_lines)}"
    assert len(exp_lines) == 2, f"Expected 2 experience lines, got {len(exp_lines)}"


# ── Block 3: Integration save → retrieve ──

def test_3_1_save_then_retrieve():
    """Full cycle: save experience case, then retrieve it via _auto_context."""
    saved_cases = []

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

    mock_memory.save = _save
    mock_memory.search = _search

    # Save a case
    result = make_result(tools=["weather_get"], reply="Погода: 20°C")
    agent._save_experience("проверь погоду в Москве", result, rounds=1, fail_count=0)
    time.sleep(0.05)

    assert saved_cases, "Case was not saved"

    # Retrieve on similar query
    ctx = agent._auto_context("какая погода в Лондоне?")
    assert "[Relevant past experiences:]" in ctx
    assert saved_cases[0]["text"] in ctx


# ── Block 4: Outcome scoring ──

def test_4_1_outcome_score_saved_in_meta():
    """outcome_score is passed in meta when saving experience."""
    save_fn, calls = capture_save()
    mock_memory.save = save_fn

    result = make_result(tools=["shell"], reply="Done")
    agent._save_experience("запусти скрипт", result, rounds=1, fail_count=0)
    time.sleep(0.05)

    assert calls
    assert calls[0]["meta"] is not None
    assert calls[0]["meta"]["outcome_score"] == 1.0  # success


def test_4_2_partial_outcome_score():
    """fail_count=1 → outcome_score=0.6 (partial)."""
    save_fn, calls = capture_save()
    mock_memory.save = save_fn

    result = make_result(tools=["shell"])
    agent._save_experience("задача", result, rounds=2, fail_count=1)
    time.sleep(0.05)

    assert calls
    assert calls[0]["meta"]["outcome_score"] == 0.6


def test_4_3_failed_outcome_score():
    """fail_count=2 → outcome_score=0.2 (failed)."""
    save_fn, calls = capture_save()
    mock_memory.save = save_fn

    result = make_result(tools=["shell"])
    agent._save_experience("задача", result, rounds=3, fail_count=2)
    time.sleep(0.05)

    assert calls
    assert calls[0]["meta"]["outcome_score"] == 0.2


def test_4_4_success_beats_failed_by_composite_score():
    """Success case with lower similarity wins over failed case with higher similarity."""
    success_case = _make_exp_result("[EXP] Task: good | Tools: shell | Steps: 1 | Result: success", 0.6, outcome_score=1.0)
    failed_case = _make_exp_result("[EXP] Task: bad | Tools: shell | Steps: 1 | Result: failed", 0.8, outcome_score=0.2)

    def _search(query, limit=3, tag=None, thread_id=None):
        if tag == "experience":
            return [failed_case, success_case]  # failed has higher raw score
        return []

    mock_memory.search = _search
    ctx = agent._auto_context("задача")

    # success: 0.6 * 1.0 = 0.6 > 0.4 → passes
    # failed:  0.8 * 0.2 = 0.16 < 0.4 → filtered out
    assert "good" in ctx
    assert "bad" not in ctx


def test_4_5_failed_case_filtered_by_composite_score():
    """Failed case with high semantic similarity still filtered out."""
    failed_case = _make_exp_result("[EXP] Task: fail | Tools: shell | Steps: 1 | Result: failed", 0.9, outcome_score=0.2)

    def _search(query, limit=3, tag=None, thread_id=None):
        if tag == "experience":
            return [failed_case]
        return []

    mock_memory.search = _search
    ctx = agent._auto_context("задача")

    # 0.9 * 0.2 = 0.18 < 0.4 → filtered
    assert "[Relevant past experiences:]" not in ctx


# ── Reset memory mock to safe default after all tests ──
mock_memory.search = lambda *a, **kw: []
mock_memory.save = lambda *a, **kw: "ok"
