"""Subagent runtime — Phase 2c of the long-running agent architecture.

A subagent is a fresh LLM context spawned by the orchestrator for a single
focused subtask. It has:

  - Its own system prompt (per type: research/browser/scraper/code)
  - A restricted tool whitelist (subset of the full tool surface)
  - A hard round cap (default 20)
  - No persistent message history — its reasoning trace is discarded
    after it returns

Only the final result string flows back to the orchestrator. This is what
keeps the orchestrator's context window from filling up over the course of
an hours-long goal — same trick Claude Code uses with the Task tool.

The subagent runs synchronously inside the same process as the orchestrator
(no IPC, no spawn). It calls :func:`agent_loop.run_loop` directly with a
restricted tool list and a fresh ``messages = [system, user(prompt)]``.

Sharing within a goal:
  - The browser session persists (Phase 3 will give each goal its own
    ``BrowserContext`` keyed by goal_id; for now subagents share the
    process-global browser).
  - ``goal_facts`` is readable/writable from subagents — the orchestrator
    can pass ``shared_context.keys`` and we'll auto-inject those facts
    into the subagent's user prompt.
  - ``memory_*`` tools read/write the cross-goal Qdrant store.

Tool whitelist per type is the load-bearing security boundary: a research
subagent can't accidentally execute shell commands; a code subagent can't
accidentally drive a browser.
"""
from __future__ import annotations

import time
from pathlib import Path

import db
import logger
import providers
import tools
from agent_budget import BudgetLimits
from agent_events import EventEmitter
from agent_loop import run_loop
from turn_context import TurnContext

_log = logger.get("subagent")


# Restricted tool whitelist per subagent type. The set is intentionally small —
# every extra tool is a chance for the LLM to wander off the focused task.
SUBAGENT_TOOLS: dict[str, set[str]] = {
    "research": {
        "http_request",
        "browser_open", "browser_snapshot",
        "memory_save", "memory_search",
        # Cross-retry state: a subagent that hits budget mid-task can
        # still persist what it learned (selectors, page state, partial
        # results) so the next attempt picks up where this one left off.
        "fact_save", "fact_get",
    },
    "browser": {
        "browser_set_visible", "browser_open", "browser_snapshot",
        "browser_accessibility", "browser_click", "browser_fill",
        "browser_eval", "browser_wait_for", "browser_press_key",
        "browser_screenshot", "browser_back", "browser_forward",
        "browser_reload",
        "fact_save", "fact_get",
    },
    "scraper": {
        "browser_open", "browser_snapshot", "browser_eval",
        "memory_save",
        "fact_save", "fact_get",
    },
    "code": {
        "read_file", "write_file", "shell",
        "memory_search",
        "fact_save", "fact_get",
    },
}


# Where the per-type system prompts live. Loaded once, cached for process life.
_PROMPTS_DIR = Path(__file__).parent / "prompts"
_prompt_cache: dict[str, str] = {}


def _load_prompt(subagent_type: str) -> str:
    if subagent_type not in _prompt_cache:
        path = _PROMPTS_DIR / f"subagent_{subagent_type}.md"
        _prompt_cache[subagent_type] = path.read_text(encoding="utf-8")
    return _prompt_cache[subagent_type]


def _get_subagent_tools(
    subagent_type: str,
    extra_tools: list[str] | None = None,
) -> list[dict]:
    """Return only the OpenAI-schema tool defs the subagent type is allowed to use.

    ``extra_tools`` lets the orchestrator widen the whitelist on a per-dispatch
    basis — useful when a user-installed skill (e.g. ``linkedin_lead_gen_*``)
    should be available to THIS particular subagent run. The dispatcher
    decides which extras are safe; we don't validate against any policy here.
    """
    allowed = set(SUBAGENT_TOOLS.get(subagent_type, set()))
    if extra_tools:
        allowed.update(t for t in extra_tools if isinstance(t, str) and t.strip())
    if not allowed:
        return []
    all_tools = tools._get_all_tools_full()
    return [
        t for t in all_tools
        if t.get("function", {}).get("name") in allowed
    ]


# Hard cap on the result string the subagent passes back to the orchestrator.
# Beyond this we truncate — the orchestrator should rarely need raw scraped
# content (that's what fact_save is for).
MAX_RESULT_CHARS = 8000


def run_subagent(
    *,
    goal_id: str,
    subtask_id: str,
    subagent_type: str,
    prompt: str,
    shared_context: dict | None = None,
    max_rounds: int = 20,
    parent_ctx: TurnContext | None = None,
    extra_tools: list[str] | None = None,
) -> str:
    """Run one subagent to completion. Returns its result string.

    Synchronous: the orchestrator's tool dispatch path calls this directly
    and blocks until it returns. The subagent's run_loop uses the same
    provider + model as the orchestrator.

    Truncates the final reply at MAX_RESULT_CHARS — the orchestrator's
    context budget shouldn't be eaten by one subagent's verbose output.
    """
    if subagent_type not in SUBAGENT_TOOLS:
        return (
            f"Error: unknown subagent type {subagent_type!r}. "
            f"Valid types: {sorted(SUBAGENT_TOOLS.keys())}"
        )
    if not prompt or not prompt.strip():
        return "Error: subagent prompt is empty."

    system = _load_prompt(subagent_type)
    user_msg = _format_user_message(goal_id, prompt, shared_context)

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_msg},
    ]
    sub_tools = _get_subagent_tools(subagent_type, extra_tools=extra_tools)

    # Build a sub-TurnContext that:
    #   - Inherits the parent's abort_event (so an aborted goal stops the
    #     subagent mid-flight).
    #   - Keeps goal_id set so any tool that needs it (memory, etc.) can read it.
    #   - Drops the on_round_complete callback — subagent rounds don't
    #     trigger goal checkpoints; only orchestrator rounds do.
    sub_ctx = TurnContext(
        source=f"subagent_{subagent_type}",
        abort_event=(parent_ctx.abort_event if parent_ctx else None) or None,
        goal_id=goal_id,
        on_round_complete=None,
    )

    db.log_goal_event(goal_id, "subagent_dispatched", {
        "subtask_id": subtask_id,
        "type": subagent_type,
        "max_rounds": max_rounds,
        "prompt_preview": prompt[:200],
    })

    _log.info(
        f"[goal={goal_id} subtask={subtask_id}] dispatching {subagent_type} "
        f"subagent (max_rounds={max_rounds})"
    )

    client = providers.get_client()
    model = providers.get_model()

    emitter = EventEmitter()
    started = time.time()
    # Enforce max_rounds at run_loop's existing budget gate (max_turns).
    # Without this the subagent runs unbounded — we found this in production
    # when a browser subagent kept turning past 130 rounds while logging into
    # LinkedIn. The budget gate triggers _force_finish on the run_loop side
    # which gives the LLM a chance to produce a final summary before exit.
    budget = BudgetLimits(max_turns=int(max_rounds) if max_rounds else 20)
    try:
        result = run_loop(
            client=client,
            model=model,
            messages=messages,
            tools=sub_tools,
            emitter=emitter,
            budget=budget,
            temperature=0.3,
            presence_penalty=0.0,
            max_tokens=2048,
            tool_executor=tools.execute,
            ctx=sub_ctx,
        )
    except Exception as e:
        _log.exception(
            f"[goal={goal_id} subtask={subtask_id}] subagent crashed: {e}"
        )
        db.log_goal_event(goal_id, "subagent_failed", {
            "subtask_id": subtask_id,
            "type": subagent_type,
            "error": f"{type(e).__name__}: {e}",
        })
        return f"Subagent crashed: {type(e).__name__}: {e}"

    duration_ms = int((time.time() - started) * 1000)
    reply = (result.get("reply") or "").strip()

    if not reply:
        db.log_goal_event(goal_id, "subagent_completed", {
            "subtask_id": subtask_id,
            "type": subagent_type,
            "result_len": 0,
            "duration_ms": duration_ms,
        })
        return "Subagent produced no text result. (Did it return only via tool calls?)"

    truncated = False
    if len(reply) > MAX_RESULT_CHARS:
        reply = reply[:MAX_RESULT_CHARS] + f"\n[...truncated at {MAX_RESULT_CHARS} chars]"
        truncated = True

    db.log_goal_event(goal_id, "subagent_completed", {
        "subtask_id": subtask_id,
        "type": subagent_type,
        "result_len": len(reply),
        "truncated": truncated,
        "duration_ms": duration_ms,
        "rounds": result.get("rounds"),
    })

    return reply


def _format_user_message(
    goal_id: str,
    prompt: str,
    shared_context: dict | None,
) -> str:
    """Build the subagent's user message.

    The user message is everything the subagent knows. We prepend any
    facts the orchestrator opted into via ``shared_context.keys`` so the
    subagent doesn't need to call fact_get itself.
    """
    parts = [prompt.strip()]
    if shared_context and isinstance(shared_context, dict):
        keys = shared_context.get("keys") or []
        if keys:
            facts = db.fact_get(goal_id, keys=list(keys))
            if facts:
                parts.append("\n\nKNOWN FACTS (from earlier subtasks):")
                for k, v in facts.items():
                    parts.append(f"- {k}: {v}")
        extras = shared_context.get("extras") or {}
        if isinstance(extras, dict) and extras:
            parts.append("\n\nEXTRA CONTEXT (passed inline):")
            for k, v in extras.items():
                parts.append(f"- {k}: {v}")
    return "\n".join(parts)
