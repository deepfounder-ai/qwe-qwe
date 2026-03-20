"""Background task runner — sub-agent tasks with chain-of-workers continuation."""

import threading, queue, time, json, re
from openai import OpenAI
import config, db, memory, tools, providers
import logger

_log = logger.get("tasks")

_task_queue: queue.Queue = queue.Queue()
_results: list[dict] = []  # [{id, task, status, result, ts}]
_worker_started = False
_lock = threading.Lock()
_task_counter = 0

MAX_ROUNDS_PER_WORKER = 15
MAX_WORKER_DEPTH = 3  # max chain continuations (total rounds = 15 * 3 = 45)


def _strip_thinking(text: str) -> str:
    return re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL).strip()


def _build_worker_prompt(depth: int = 0, continuation: str | None = None) -> str:
    """Build system prompt for background worker with self-knowledge."""
    data_dir = str(config.DATA_DIR)
    parts = [
        "You are a background worker. Complete the task step by step.",
        "Use tools to accomplish the task. Be concise.",
        "",
        "YOUR FILE SYSTEM:",
        f"- Data dir: {data_dir}/",
        f"- Logs: {data_dir}/logs/qwe-qwe.log, {data_dir}/logs/errors.log",
        f"- Workspace: {data_dir}/workspace/",
        f"- Database: {config.DB_PATH}",
        "",
        "TOOLS YOU SHOULD USE:",
        "- secret_save/secret_get — for API keys, tokens, passwords (encrypted vault)",
        "- memory_save/memory_search — for persistent info across tasks (Qdrant vector DB)",
        "- schedule_task — create cron jobs",
        "- shell — run commands (combine steps when possible)",
        "- read_file/write_file — file operations",
        "",
        "MEMORY STRATEGY: save important intermediate results with memory_save() so they",
        "persist even if this worker ends. Use tags: 'task' for progress, 'fact' for data.",
        "Search memory_search() first if you need info from previous steps.",
    ]

    if continuation:
        parts.extend([
            "",
            f"CONTINUATION (worker #{depth}): a previous worker ran out of rounds.",
            "Here is the handoff summary — continue from where it left off:",
            f"---",
            continuation,
            f"---",
            "Do NOT repeat completed steps. Pick up from REMAINING.",
        ])

    parts.extend([
        "",
        "When done, summarize what you did in one sentence.",
    ])

    return "\n".join(parts)


_HANDOFF_PROMPT = (
    "You ran out of tool rounds. Generate a structured handoff summary for the next worker.\n"
    "Format EXACTLY:\n"
    "COMPLETED: <what you finished, with exact names/paths/keys used>\n"
    "STATE: <key values — secret names, file paths, variable values>\n"
    "REMAINING: <what still needs to be done>\n"
    "Be specific — include exact secret key names, file paths, IDs. The next worker has NO other context."
)


def _generate_handoff(client, messages: list[dict], task_desc: str) -> str | None:
    """Ask model to generate structured handoff summary."""
    handoff_messages = messages + [
        {"role": "user", "content": _HANDOFF_PROMPT}
    ]
    try:
        resp = client.chat.completions.create(
            model=providers.get_model(),
            messages=handoff_messages,
            temperature=0.3,
            max_tokens=512,
        )
        summary = _strip_thinking(resp.choices[0].message.content or "")
        if summary and len(summary) > 20:
            return summary
    except Exception as e:
        _log.warning(f"handoff generation failed: {e}")
    return None


def _run_task(task_id: int, task_desc: str, depth: int = 0,
              continuation: str | None = None):
    """Run a single task through the LLM with tools.

    If max rounds exhausted and depth < MAX_WORKER_DEPTH:
      1. Ask model for structured handoff summary
      2. Save progress to memory (Qdrant)
      3. Spawn continuation worker with summary
    """
    client = providers.get_client()
    system_prompt = _build_worker_prompt(depth, continuation)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": task_desc},
    ]

    all_tools = tools.get_all_tools()
    rounds = 0
    last_had_tool_calls = False

    while rounds < MAX_ROUNDS_PER_WORKER:
        try:
            resp = client.chat.completions.create(
                model=providers.get_model(),
                messages=messages,
                tools=all_tools,
                tool_choice="auto",
                temperature=0.5,
                max_tokens=1024,
            )
        except Exception as e:
            _save_result(task_id, task_desc, "error", str(e))
            return

        msg = resp.choices[0].message

        if msg.tool_calls:
            last_had_tool_calls = True
            assistant_msg = {"role": "assistant", "content": msg.content or ""}
            assistant_msg["tool_calls"] = [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in msg.tool_calls
            ]
            messages.append(assistant_msg)

            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    args = {}
                result = tools.execute(tc.function.name, args)
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

            rounds += 1
            continue

        # Final response — task completed naturally
        reply = _strip_thinking(msg.content or "")
        _save_result(task_id, task_desc, "done", reply)
        last_had_tool_calls = False
        return

    # ── Rounds exhausted — attempt chain continuation ──
    if not last_had_tool_calls:
        # Model finished talking, just ran out on the last round
        _save_result(task_id, task_desc, "done", "Task completed (max rounds)")
        return

    if depth >= MAX_WORKER_DEPTH - 1:
        _log.warning(f"task #{task_id} exhausted all {MAX_WORKER_DEPTH} worker chains")
        _save_result(task_id, task_desc, "done",
                     f"Task partially completed (exhausted {MAX_WORKER_DEPTH} worker chains, "
                     f"{MAX_WORKER_DEPTH * MAX_ROUNDS_PER_WORKER} total rounds)")
        return

    # Generate handoff
    _log.info(f"task #{task_id} worker #{depth} exhausted rounds, generating handoff...")
    handoff = _generate_handoff(client, messages, task_desc)

    if not handoff:
        _save_result(task_id, task_desc, "done", "Task partially completed (handoff failed)")
        return

    # Save progress to Qdrant for cross-worker persistence
    try:
        memory.save(
            f"[TASK PROGRESS #{task_id}] {task_desc[:100]}\n{handoff}",
            tag="task"
        )
        _log.info(f"task #{task_id} progress saved to memory")
    except Exception as e:
        _log.warning(f"failed to save task progress to memory: {e}")

    # Chain: spawn continuation in same thread (not via queue — keep task_id)
    _log.info(f"task #{task_id} continuing with worker #{depth + 1}")
    _run_task(task_id, task_desc, depth=depth + 1, continuation=handoff)


def _save_result(task_id: int, task_desc: str, status: str, result: str):
    with _lock:
        # Update existing entry if present (for registered tasks)
        for r in _results:
            if r["id"] == task_id:
                r["status"] = status
                r["result"] = result
                r["ts"] = time.time()
                return
        _results.append({
            "id": task_id,
            "task": task_desc,
            "status": status,
            "result": result,
            "ts": time.time(),
        })
        # Prevent unbounded growth — keep last 100 results
        while len(_results) > 100:
            _results.pop(0)


def _worker():
    """Background worker thread — processes tasks one by one."""
    while True:
        task_id, task_desc = _task_queue.get()
        try:
            _run_task(task_id, task_desc)
        except Exception as e:
            _save_result(task_id, task_desc, "error", str(e))
        finally:
            _task_queue.task_done()


def spawn(task_desc: str) -> int:
    """Add a task to the queue. Returns task id."""
    global _worker_started, _task_counter

    if not _worker_started:
        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        _worker_started = True

    with _lock:
        _task_counter += 1
        task_id = _task_counter

    _task_queue.put((task_id, task_desc))
    return task_id


def get_results(clear: bool = True) -> list[dict]:
    """Get completed task results."""
    with _lock:
        results = list(_results)
        if clear:
            _results.clear()
    return results


def register(name: str, description: str = "") -> int:
    """Register an external background task (not via queue). Returns task id.
    Use update() to change status/result when done."""
    global _task_counter
    with _lock:
        _task_counter += 1
        task_id = _task_counter
        _results.append({
            "id": task_id,
            "task": description or name,
            "name": name,
            "status": "running",
            "result": "",
            "ts": time.time(),
        })
    return task_id


def update(task_id: int, status: str, result: str = ""):
    """Update status/result of a registered task."""
    with _lock:
        for r in _results:
            if r["id"] == task_id:
                r["status"] = status
                r["result"] = result
                r["ts"] = time.time()
                return


def pending_count() -> int:
    with _lock:
        running = sum(1 for r in _results if r.get("status") == "running")
    return _task_queue.qsize() + running


def completed_count() -> int:
    with _lock:
        return sum(1 for r in _results if r.get("status") != "running")


def get_running() -> list[dict]:
    """Get list of currently running tasks (for system prompt injection)."""
    with _lock:
        return [{"name": r.get("name", ""), "task": r["task"], "result": r.get("result", "")}
                for r in _results if r.get("status") == "running"]
