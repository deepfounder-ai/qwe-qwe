"""Goal orchestrator — Phase 2 of the long-running agent runtime.

The orchestrator is the "main LLM" of a Goal. It maintains the plan
(via goal_plan_set / subtask_update), decides what to do next, and either
executes simple steps inline OR dispatches subagents (Phase 2c) for heavy
work. Subagents run with FRESH contexts so the orchestrator's window stays
small even across hours of work.

Architecture differences from chat-mode `agent.run`:

  - No `soul.py` prompt — we use `prompts/orchestrator.md` instead. The
    orchestrator is not a chat assistant.
  - No `auto_context` recall — facts live in `goal_facts`, the orchestrator
    fetches them explicitly via the `fact_get` tool.
  - Restricted tool set: orchestrator-management tools + lightweight ops
    (http_request, read/write_file, shell, memory_*). Browser tools and other
    heavy ops are reachable ONLY via `dispatch_subagent` (added in 2c).
  - No message persistence to the `messages` table — orchestrator state lives
    in `goal_checkpoints` and is restored from the latest checkpoint on resume.

The orchestrator's tools mutate goal state via the active TurnContext.goal_id
(read by tools._require_goal_id). That's why we must always pass `ctx` with
a non-None `goal_id` when calling run_orchestrator.
"""
from __future__ import annotations

import time
from pathlib import Path

import config
import db
import logger
import providers
import tools
from agent_budget import BudgetLimits
from agent_events import EventEmitter
from agent_loop import run_loop
from turn_context import TurnContext

_log = logger.get("orchestrator")


# Where the orchestrator's system prompt lives. Read once on first use,
# cached for the lifetime of the process (it changes only on disk edits +
# code restart, same as any other prompt asset).
_PROMPT_PATH = Path(__file__).parent / "prompts" / "orchestrator.md"
_prompt_cache: str | None = None


def _load_system_prompt() -> str:
    global _prompt_cache
    if _prompt_cache is None:
        _prompt_cache = _PROMPT_PATH.read_text(encoding="utf-8")
    return _prompt_cache


# Tools the orchestrator is allowed to call. Subagent dispatch lands in 2c —
# until then the orchestrator does everything inline.
_ORCHESTRATOR_TOOL_NAMES: set[str] = {
    # Goal management (Phase 2)
    "goal_plan_set", "subtask_update", "fact_save", "fact_get",
    # Lightweight ops the orchestrator may want to do inline.
    "memory_save", "memory_search",
    "http_request",
    "read_file", "write_file",
    "shell",
    "send_file",
    # Subagent dispatch (Phase 2c will register this)
    "dispatch_subagent",
}


def _get_orchestrator_tools() -> list[dict]:
    """Return the OpenAI-format tool schemas the orchestrator is allowed to call.

    Pulled fresh each turn so newly-registered tools (e.g. dispatch_subagent
    from Phase 2c) show up without restart.
    """
    all_tools = tools._get_all_tools_full()
    return [t for t in all_tools
            if t.get("function", {}).get("name") in _ORCHESTRATOR_TOOL_NAMES]


def run_orchestrator(goal_id: str, ctx: TurnContext) -> dict:
    """Drive one goal from start (or last checkpoint) to terminal status.

    Synchronous — runs inside the worker's thread executor so the worker's
    asyncio loop stays responsive for heartbeats / signal handling.

    Returns a dict with at least::

        {"reply": str, "rounds": int, "tools_used": list[str], "cost_usd": float}

    ``ctx`` MUST have ``goal_id`` set so the orchestrator's tools can find
    the active goal.
    """
    if not ctx.goal_id:
        raise ValueError("run_orchestrator requires ctx.goal_id")

    goal = db.get_goal(goal_id)
    if not goal:
        raise ValueError(f"goal {goal_id} not found")

    # ── Build initial messages ──
    # Resume from the latest checkpoint when possible. The checkpoint's
    # messages already include the system prompt + user input from the
    # original start — we trust it as-is. This keeps the prompt cache hot
    # across resumes.
    checkpoint = db.load_latest_checkpoint(goal_id)
    start_round = 0
    if checkpoint and checkpoint.get("messages"):
        messages = checkpoint["messages"]
        start_round = checkpoint["round_num"]
        _log.info(f"[{goal_id}] resuming orchestrator from round {start_round}")
    else:
        system = _load_system_prompt()
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": goal["user_input"]},
        ]
        _log.info(f"[{goal_id}] starting orchestrator fresh")

    # ── Provider + tools ──
    client = providers.get_client()
    model = providers.get_model()
    tool_schemas = _get_orchestrator_tools()
    _log.info(f"[{goal_id}] orchestrator tools: {[t['function']['name'] for t in tool_schemas]}")

    # ── Emitter: silent (no UI streaming) but tap into status for log lines ──
    emitter = EventEmitter()
    emitter.on("status", lambda e: _log.info(f"[{goal_id}] {e.data.get('text', '')}"))

    # ── Wire the round-complete callback if not already on ctx ──
    # goal_runner sets one up — but if someone calls run_orchestrator directly
    # (e.g. a test), wire a no-op so checkpoints can still land.
    if ctx.on_round_complete is None:
        ctx.on_round_complete = _default_checkpoint_callback(goal_id, start_round)

    # ── Run the loop ──
    # Cap the orchestrator at a hard turn budget so a runaway plan can't
    # rack up 200+ LLM calls before someone notices. The cap is generous
    # — a real long-running goal should mostly delegate to subagents, so
    # the orchestrator itself shouldn't need many rounds.
    orch_max_turns = int(config.get("orchestrator_max_turns") or 80)
    budget = BudgetLimits(max_turns=orch_max_turns)
    turn_start = time.time()
    try:
        result = run_loop(
            client=client,
            model=model,
            messages=messages,
            tools=tool_schemas,
            emitter=emitter,
            budget=budget,
            temperature=0.3,  # orchestrators benefit from low temp for tool-calling reliability
            presence_penalty=0.0,
            max_tokens=2048,
            tool_executor=tools.execute,
            ctx=ctx,
        )
    except Exception:
        _log.exception(f"[{goal_id}] run_loop crashed")
        raise

    duration_ms = int((time.time() - turn_start) * 1000)
    reply = result.get("reply", "") or ""
    _log.info(
        f"[{goal_id}] orchestrator done: rounds={result.get('tool_calls', 'n/a')}, "
        f"reply_len={len(reply)}, duration_ms={duration_ms}"
    )

    return {
        "reply": reply,
        "rounds": result.get("rounds", 0),
        "tools_used": result.get("tool_calls", []),
        "cost_usd": result.get("cost_usd", 0.0),
        "prompt_tokens": result.get("prompt_tokens", 0),
        "completion_tokens": result.get("completion_tokens", 0),
    }


def _default_checkpoint_callback(goal_id: str, start_round: int):
    """Fallback checkpoint cadence when nobody else wired one up."""
    interval = max(1, int(config.get("checkpoint_round_interval") or 3))

    def _cb(round_num: int, messages: list[dict]) -> None:
        global_round = start_round + round_num
        if global_round <= 0 or (global_round % interval) != 0:
            return
        plan = db.get_goal_plan(goal_id) or {}
        facts = db.fact_get(goal_id)
        try:
            db.save_checkpoint(goal_id, global_round, subtask_index=-1,
                               messages=messages, plan=plan, facts=facts)
        except Exception:
            _log.exception(f"checkpoint failed for {goal_id} round {global_round}")
    return _cb
