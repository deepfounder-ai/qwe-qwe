"""Variant A: orchestrator → subagent feedback channel via
``previous_attempt_feedback``.

Tests cover end-to-end wiring:
  - dispatch_subagent accepts the param
  - tool layer truncates + passes through to subagent.run_subagent
  - subagent.run_subagent prepends a system message before user prompt
  - db.update_subtask persists last_rejection_reason on the plan
  - goal_events log subagent_dispatched with feedback_preview
  - empty / whitespace-only feedback is treated as None
"""
from __future__ import annotations

from unittest.mock import patch

import db
import subagent
import tools


# ── db.update_subtask plumbing ─────────────────────────────────────────────


def test_update_subtask_persists_last_rejection_reason(qwe_temp_data_dir):
    gid = db.create_goal(user_input="x", source="cli")
    db.set_goal_plan(gid, [{
        "title": "find carriers",
        "description": "...",
        "done_condition": {"kind": "shell_returns_zero", "spec": {"cmd": "true"}},
    }])
    db.update_subtask(
        gid, "st_1",
        last_rejection_reason="prior attempt found 0, try OpenCorporates",
    )
    plan = db.get_goal_plan(gid)
    st = plan["subtasks"][0]
    assert st["last_rejection_reason"] == "prior attempt found 0, try OpenCorporates"


def test_update_subtask_clears_rejection_reason_on_empty_string(qwe_temp_data_dir):
    """Passing an explicit empty string clears the field (None)."""
    gid = db.create_goal(user_input="x", source="cli")
    db.set_goal_plan(gid, [{
        "title": "x", "description": "",
        "done_condition": {"kind": "shell_returns_zero", "spec": {"cmd": "true"}},
    }])
    db.update_subtask(gid, "st_1", last_rejection_reason="first feedback")
    assert db.get_goal_plan(gid)["subtasks"][0]["last_rejection_reason"] == "first feedback"

    db.update_subtask(gid, "st_1", last_rejection_reason="")
    # Stored as None (empty → null) so UI doesn't render a blank yellow box
    assert db.get_goal_plan(gid)["subtasks"][0]["last_rejection_reason"] is None


def test_update_subtask_caps_rejection_reason_at_4000_chars(qwe_temp_data_dir):
    gid = db.create_goal(user_input="x", source="cli")
    db.set_goal_plan(gid, [{
        "title": "x", "description": "",
        "done_condition": {"kind": "shell_returns_zero", "spec": {"cmd": "true"}},
    }])
    long_feedback = "x" * 10_000
    db.update_subtask(gid, "st_1", last_rejection_reason=long_feedback)
    st = db.get_goal_plan(gid)["subtasks"][0]
    assert len(st["last_rejection_reason"]) == 4000


def test_set_goal_plan_initializes_last_rejection_reason_to_none(qwe_temp_data_dir):
    """Fresh plan: every subtask has the field, defaulted to None."""
    gid = db.create_goal(user_input="x", source="cli")
    plan = db.set_goal_plan(gid, [{
        "title": "x", "description": "",
        "done_condition": {"kind": "shell_returns_zero", "spec": {"cmd": "true"}},
    }])
    assert plan["subtasks"][0]["last_rejection_reason"] is None


# ── subagent.run_subagent injection ────────────────────────────────────────


def test_subagent_prepends_feedback_as_second_system_message(qwe_temp_data_dir,
                                                              monkeypatch):
    """The feedback becomes a {role:"system"} message right after the
    role prompt, BEFORE the user request. Subagent reads it as a
    "what to avoid" directive at top of context."""
    captured = {}

    def fake_run_loop(*, messages, **kw):
        captured["messages"] = list(messages)
        return {"reply": "done", "rounds": 1, "tools_used": [], "cost_usd": 0,
                "prompt_tokens": 0, "completion_tokens": 0}

    monkeypatch.setattr(subagent, "run_loop", fake_run_loop)
    monkeypatch.setattr(subagent.providers, "get_client", lambda: object())
    monkeypatch.setattr(subagent.providers, "get_model", lambda: "fake-model")

    gid = db.create_goal(user_input="x", source="cli")
    db.set_goal_plan(gid, [{
        "title": "find", "description": "",
        "done_condition": {"kind": "shell_returns_zero", "spec": {"cmd": "true"}},
    }])

    subagent.run_subagent(
        goal_id=gid,
        subtask_id="st_1",
        subagent_type="research",
        prompt="Find 30 more carriers",
        previous_attempt_feedback="skip FMCSA, rate-limited",
    )

    msgs = captured["messages"]
    # Shape: [system(role prompt), system(feedback), user(prompt)]
    assert len(msgs) == 3
    assert msgs[0]["role"] == "system"
    # The role prompt (research subagent.md) comes first
    assert msgs[1]["role"] == "system"
    assert "PREVIOUS ATTEMPT FEEDBACK" in msgs[1]["content"]
    assert "skip FMCSA" in msgs[1]["content"]
    assert "rate-limited" in msgs[1]["content"]
    assert msgs[2]["role"] == "user"
    assert "Find 30 more carriers" in msgs[2]["content"]


def test_subagent_omits_feedback_block_when_none(qwe_temp_data_dir, monkeypatch):
    """No feedback param → no second system message, just role + user."""
    captured = {}
    def fake_run_loop(*, messages, **kw):
        captured["messages"] = list(messages)
        return {"reply": "done", "rounds": 1, "tools_used": [], "cost_usd": 0,
                "prompt_tokens": 0, "completion_tokens": 0}
    monkeypatch.setattr(subagent, "run_loop", fake_run_loop)
    monkeypatch.setattr(subagent.providers, "get_client", lambda: object())
    monkeypatch.setattr(subagent.providers, "get_model", lambda: "fake-model")

    gid = db.create_goal(user_input="x", source="cli")
    db.set_goal_plan(gid, [{
        "title": "x", "description": "",
        "done_condition": {"kind": "shell_returns_zero", "spec": {"cmd": "true"}},
    }])
    subagent.run_subagent(
        goal_id=gid, subtask_id="st_1", subagent_type="research",
        prompt="Find carriers",
    )

    msgs = captured["messages"]
    assert len(msgs) == 2  # role + user, no feedback
    assert msgs[0]["role"] == "system"
    assert msgs[1]["role"] == "user"


def test_subagent_treats_whitespace_only_feedback_as_none(qwe_temp_data_dir,
                                                          monkeypatch):
    """Empty strings and whitespace-only feedback don't get injected —
    no point shipping an empty 'PREVIOUS ATTEMPT FEEDBACK:' block."""
    captured = {}
    def fake_run_loop(*, messages, **kw):
        captured["messages"] = list(messages)
        return {"reply": "ok", "rounds": 1, "tools_used": [], "cost_usd": 0,
                "prompt_tokens": 0, "completion_tokens": 0}
    monkeypatch.setattr(subagent, "run_loop", fake_run_loop)
    monkeypatch.setattr(subagent.providers, "get_client", lambda: object())
    monkeypatch.setattr(subagent.providers, "get_model", lambda: "fake-model")

    gid = db.create_goal(user_input="x", source="cli")
    db.set_goal_plan(gid, [{
        "title": "x", "description": "",
        "done_condition": {"kind": "shell_returns_zero", "spec": {"cmd": "true"}},
    }])
    for empty in ("", "   ", "\n\n", "\t"):
        subagent.run_subagent(
            goal_id=gid, subtask_id="st_1", subagent_type="research",
            prompt="x",
            previous_attempt_feedback=empty,
        )
        msgs = captured["messages"]
        assert len(msgs) == 2, f"feedback {empty!r} should be ignored"


def test_subagent_truncates_feedback_at_4000(qwe_temp_data_dir, monkeypatch):
    captured = {}
    def fake_run_loop(*, messages, **kw):
        captured["messages"] = list(messages)
        return {"reply": "x", "rounds": 1, "tools_used": [], "cost_usd": 0,
                "prompt_tokens": 0, "completion_tokens": 0}
    monkeypatch.setattr(subagent, "run_loop", fake_run_loop)
    monkeypatch.setattr(subagent.providers, "get_client", lambda: object())
    monkeypatch.setattr(subagent.providers, "get_model", lambda: "fake-model")

    gid = db.create_goal(user_input="x", source="cli")
    db.set_goal_plan(gid, [{
        "title": "x", "description": "",
        "done_condition": {"kind": "shell_returns_zero", "spec": {"cmd": "true"}},
    }])
    subagent.run_subagent(
        goal_id=gid, subtask_id="st_1", subagent_type="research",
        prompt="x",
        previous_attempt_feedback="y" * 10_000,
    )
    feedback_msg = captured["messages"][1]["content"]
    # Feedback body is ≤ 4000 chars; the framing text adds a few hundred
    # but the y-run itself is capped
    y_run = feedback_msg.split("PREVIOUS ATTEMPT FEEDBACK from orchestrator:")[1]
    y_run = y_run.split("Take this into account")[0]
    assert len(y_run.strip()) == 4000


# ── tool layer wiring ──────────────────────────────────────────────────────


def test_dispatch_subagent_impl_passes_feedback_through(qwe_temp_data_dir,
                                                        monkeypatch):
    """End-to-end via tools._dispatch_subagent_impl: the orchestrator
    calls dispatch_subagent with feedback, the param threads through
    to subagent.run_subagent without mutation (besides truncation)."""
    captured = {}

    def fake_run_subagent(**kwargs):
        captured.update(kwargs)
        return "ok"

    monkeypatch.setattr(subagent, "run_subagent", fake_run_subagent)

    gid = db.create_goal(user_input="x", source="cli")
    db.set_goal_plan(gid, [{
        "title": "x", "description": "",
        "done_condition": {"kind": "shell_returns_zero", "spec": {"cmd": "true"}},
    }])

    # Bind active goal so _require_goal_id() works
    from turn_context import TurnContext
    tools._set_turn_ctx(TurnContext(source="cli", goal_id=gid))
    try:
        result = tools._dispatch_subagent_impl({
            "type": "research",
            "prompt": "x",
            "subtask_id": "st_1",
            "previous_attempt_feedback": "previous attempt failed because of X",
        })
    finally:
        tools._set_turn_ctx(None)

    assert result == "ok"
    assert captured["previous_attempt_feedback"] == "previous attempt failed because of X"


def test_dispatch_subagent_impl_persists_feedback_to_plan(qwe_temp_data_dir,
                                                          monkeypatch):
    """When orchestrator passes feedback, the plan gets last_rejection_reason
    stamped on the subtask for UI/audit — before the subagent even runs."""
    monkeypatch.setattr(subagent, "run_subagent", lambda **kw: "ok")

    gid = db.create_goal(user_input="x", source="cli")
    db.set_goal_plan(gid, [{
        "title": "x", "description": "",
        "done_condition": {"kind": "shell_returns_zero", "spec": {"cmd": "true"}},
    }])

    from turn_context import TurnContext
    tools._set_turn_ctx(TurnContext(source="cli", goal_id=gid))
    try:
        tools._dispatch_subagent_impl({
            "type": "research",
            "prompt": "x",
            "subtask_id": "st_1",
            "previous_attempt_feedback": "skip FMCSA next time",
        })
    finally:
        tools._set_turn_ctx(None)

    st = db.get_goal_plan(gid)["subtasks"][0]
    assert st["last_rejection_reason"] == "skip FMCSA next time"
    # Plus the auto-bump
    assert st["attempts"] == 1


def test_dispatch_subagent_impl_rejects_non_string_feedback(qwe_temp_data_dir,
                                                             monkeypatch):
    """Numbers / dicts / lists / None passed as feedback → treated as None.
    Defensive — the LLM could emit a non-string under model bugs."""
    captured = {}

    def fake_run_subagent(**kwargs):
        captured["feedback"] = kwargs.get("previous_attempt_feedback")
        return "ok"

    monkeypatch.setattr(subagent, "run_subagent", fake_run_subagent)

    gid = db.create_goal(user_input="x", source="cli")
    db.set_goal_plan(gid, [{
        "title": "x", "description": "",
        "done_condition": {"kind": "shell_returns_zero", "spec": {"cmd": "true"}},
    }])

    from turn_context import TurnContext
    tools._set_turn_ctx(TurnContext(source="cli", goal_id=gid))
    try:
        for bad in (123, {"x": 1}, ["a", "b"], None):
            tools._dispatch_subagent_impl({
                "type": "research",
                "prompt": "x",
                "subtask_id": "st_1",
                "previous_attempt_feedback": bad,
            })
            assert captured["feedback"] is None, f"feedback={bad!r} → should be None"
    finally:
        tools._set_turn_ctx(None)


# ── Event log ──────────────────────────────────────────────────────────────


def test_subagent_dispatched_event_includes_feedback_preview(qwe_temp_data_dir,
                                                               monkeypatch):
    """Goal timeline records whether each dispatch carried feedback,
    so audit/UI can show '(with feedback)' vs '(fresh attempt)'."""
    monkeypatch.setattr(subagent, "run_loop",
                        lambda **kw: {"reply": "ok", "rounds": 1,
                                       "tools_used": [], "cost_usd": 0,
                                       "prompt_tokens": 0, "completion_tokens": 0})
    monkeypatch.setattr(subagent.providers, "get_client", lambda: object())
    monkeypatch.setattr(subagent.providers, "get_model", lambda: "fake-model")

    gid = db.create_goal(user_input="x", source="cli")
    db.set_goal_plan(gid, [{
        "title": "x", "description": "",
        "done_condition": {"kind": "shell_returns_zero", "spec": {"cmd": "true"}},
    }])
    subagent.run_subagent(
        goal_id=gid, subtask_id="st_1", subagent_type="research",
        prompt="x",
        previous_attempt_feedback="critical: skip FMCSA",
    )

    events = db.get_goal_events(gid, limit=50)
    dispatched = [e for e in events if e["event_type"] == "subagent_dispatched"]
    assert len(dispatched) == 1
    payload = dispatched[0]["payload"]
    if isinstance(payload, str):
        import json
        payload = json.loads(payload)
    assert "feedback_preview" in payload
    assert "skip FMCSA" in payload["feedback_preview"]


def test_subagent_dispatched_event_feedback_is_none_when_omitted(
        qwe_temp_data_dir, monkeypatch):
    """No feedback → ``feedback_preview`` is None in the event payload
    (timeline can render this as 'fresh attempt')."""
    monkeypatch.setattr(subagent, "run_loop",
                        lambda **kw: {"reply": "ok", "rounds": 1,
                                       "tools_used": [], "cost_usd": 0,
                                       "prompt_tokens": 0, "completion_tokens": 0})
    monkeypatch.setattr(subagent.providers, "get_client", lambda: object())
    monkeypatch.setattr(subagent.providers, "get_model", lambda: "fake-model")

    gid = db.create_goal(user_input="x", source="cli")
    db.set_goal_plan(gid, [{
        "title": "x", "description": "",
        "done_condition": {"kind": "shell_returns_zero", "spec": {"cmd": "true"}},
    }])
    subagent.run_subagent(
        goal_id=gid, subtask_id="st_1", subagent_type="research",
        prompt="x",
    )

    events = db.get_goal_events(gid, limit=50)
    dispatched = [e for e in events if e["event_type"] == "subagent_dispatched"]
    payload = dispatched[0]["payload"]
    if isinstance(payload, str):
        import json
        payload = json.loads(payload)
    assert payload.get("feedback_preview") is None
