"""Background task runner — sub-agent tasks that run sequentially."""

import threading, queue, time, json, re
from openai import OpenAI
import config, db, memory, tools, providers

_task_queue: queue.Queue = queue.Queue()
_results: list[dict] = []  # [{id, task, status, result, ts}]
_worker_started = False
_lock = threading.Lock()
_task_counter = 0


def _strip_thinking(text: str) -> str:
    return re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL).strip()


def _run_task(task_id: int, task_desc: str):
    """Run a single task through the LLM with tools."""
    client = providers.get_client()

    messages = [
        {"role": "system", "content": (
            "You are a background worker. Complete the task efficiently.\n"
            "Use tools to accomplish the task. Be concise.\n"
            "When done, summarize what you did in one sentence."
        )},
        {"role": "user", "content": task_desc},
    ]

    all_tools = tools.get_all_tools()
    rounds = 0
    max_rounds = 5

    while rounds < max_rounds:
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

        # Final response
        reply = _strip_thinking(msg.content or "")
        _save_result(task_id, task_desc, "done", reply)
        return

    _save_result(task_id, task_desc, "done", "Task completed (max rounds reached)")


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
