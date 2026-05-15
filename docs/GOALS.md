# Goals — long-running, durable agent tasks

The Goals runtime turns the agent from a chat copilot into a backend-resident
worker that can run for hours, survive process restarts, and decompose
complex jobs into focused subtasks.

It is **task-agnostic by design**. Anything you can express as "do this thing
that takes minutes to hours and may need multiple steps" can be a Goal.

## What is a Goal

A Goal is one user request that:

- Is persisted in SQLite (`goals` table) the moment it's created
- Gets picked up by the `castor-worker` daemon process (separate from the
  web server)
- Runs an **Orchestrator** LLM that maintains a plan (list of subtasks)
- Dispatches **Subagents** with fresh contexts for heavy work
- Checkpoints state every N rounds so a crash → restart resumes mid-task
- Has its own structured fact store (`goal_facts`) that survives context
  compaction
- Optionally enforces a USD or wall-clock budget cap

The Orchestrator never sees a Subagent's raw reasoning — only its final
result string. That's the trick that keeps the parent's context window
small over hours of work.

## What kinds of tasks fit

The system has **no hardcoded domain knowledge**. The four subagent types
cover most agent capabilities:

| Subagent type | What it does | Example use |
|---------------|--------------|-------------|
| `research` | Fetch + read web pages / JSON APIs, return summarised findings | "Summarise this paper", "What does the GitHub API return for repo X" |
| `browser` | Drive a real Playwright browser — click, fill, navigate | "Log in to X and export a CSV", "Take a screenshot of dashboard Y" |
| `scraper` | Extract structured data (rows / tables) as JSON | "Pull every job listing from URLs A,B,C as JSON" |
| `code` | Read + write files, run shell | "Refactor X across the codebase", "Run the test suite", "Generate config Z" |

The Orchestrator itself can do lightweight inline work with `http_request`,
`read_file`/`write_file`, `shell`, and `memory_*` tools — anything that's
1-2 tool calls and doesn't need a fresh context window.

## Example goals across domains

### Research / analysis

> "Read the top 10 papers from arXiv this week on diffusion models and
> write a 2-paragraph summary of trends to ~/diffusion_trends.md"

Plan the orchestrator might build:
1. `research` subagent: fetch the arXiv listing, return URLs
2. `research` subagent (or N parallel): fetch + summarise each paper
3. `code` subagent: write the consolidated markdown file

### Code refactor

> "Rename function `fetchUser` to `getUser` across the repo, update all
> callers and tests, then run pytest"

1. `code` subagent: grep for occurrences, get list of files
2. `code` subagent: rewrite each file
3. `code` subagent: run `pytest tests/`, return pass/fail + count

### Data pipeline

> "Process orders.csv: group by region, compute revenue per region, write
> the result to summary.json"

1. Inline: `read_file("orders.csv")`, save as fact
2. `code` subagent: compute groups + aggregates, write `summary.json`

### API integration

> "Pull every Stripe customer, filter to active subscriptions, save the
> emails to active_subs.txt"

1. `research` (or inline): `http_request` to Stripe API, page through
   results, save customer IDs as facts
2. `code` subagent: filter the saved data, write the output file

### Web automation

> "Log in to my dashboard, export the last-30-days report, save the CSV
> to ~/reports/"

1. `browser` subagent: log in, navigate, click export, capture download URL
2. Inline: `http_request` to download the CSV, save it

### Document generation

> "Read the spec at docs/feature_spec.md, generate a TODO list of
> implementation tasks, write to TODO.md"

1. Inline: `read_file("docs/feature_spec.md")`
2. `code` subagent: write structured TODO based on the spec

### Web scraping

> "Find 100 widget vendors on directory site X, save each as JSON with
> name + url + phone"

1. `browser` subagent: search + paginate through the directory
2. `scraper` subagent: extract vendor rows as structured JSON

### Long migration

> "Find every .py file using the deprecated `oldlib` import and rewrite
> them to use `newlib`'s equivalent, batch by 20 files, run tests after
> each batch"

1. `code` subagent: list affected files
2. Per batch: `code` subagent rewrites + runs tests
3. Orchestrator tracks batch progress in facts, retries failed batches

## What makes it durable

| Failure mode | What happens |
|---|---|
| WS client disconnects | Goal keeps running — workers don't depend on WS |
| User closes tab | Same. Goal status visible at `GET /api/goals/{id}` |
| `castor-worker` crashes | launchd / systemd restarts it. New worker claims the goal via expired lease. Resumes from last checkpoint. |
| `kill -9` mid-round | Same as above |
| LLM provider returns 500 | Goal pauses with `provider_unreachable`, retries on next worker tick |
| Subagent fails | Orchestrator sees the error string in tool result, can retry or skip the subtask |
| Context window fills | Compaction trims old messages; `goal_facts` are NEVER compacted, so durable state survives |
| Process restart | Up to last checkpoint (default every 3 rounds) of work preserved |

## How to use

### Create a goal via API

```bash
curl -X POST http://localhost:7860/api/goals \
  -H 'Content-Type: application/json' \
  -d '{
    "user_input": "Read all .py files under src/, find any usage of deprecated_func, return a list",
    "source": "api",
    "budget_usd": 5.00,
    "budget_seconds": 3600
  }'
# → {"id": "g_abc123...", "status": "pending"}
```

### Check progress

```bash
curl http://localhost:7860/api/goals/g_abc123...
curl http://localhost:7860/api/goals/g_abc123.../events
```

### Pause / abort

```bash
curl -X POST http://localhost:7860/api/goals/g_abc123.../pause
curl -X POST http://localhost:7860/api/goals/g_abc123.../abort
```

### Run the worker

```bash
# Foreground (for testing)
python -m worker

# Or install as a system service (macOS)
python scripts/install_worker.py
launchctl load ~/Library/LaunchAgents/com.castor.worker.plist
```

## Configuration

Tunable via `EDITABLE_SETTINGS` (kv store, configurable from Settings UI):

| Setting | Default | Purpose |
|---|---|---|
| `worker_concurrency` | 1 | How many goals one worker runs in parallel |
| `worker_poll_interval_sec` | 5 | How often the worker checks for new goals |
| `checkpoint_round_interval` | 3 | Save a checkpoint every N orchestrator rounds |

## Where things live

```
goals                table  — durable queue + status machine
goal_checkpoints     table  — gzipped messages snapshots (last 5 per goal)
goal_facts           table  — structured key/value scratch pad per goal
goal_events          table  — append-only event log for observability
agent_runs           table  — per-LLM-call cost + token accounting

prompts/orchestrator.md         — orchestrator system prompt
prompts/subagent_<type>.md      — subagent system prompts (4 types)

orchestrator.py      — Orchestrator loop (system + plan, dispatches subagents)
subagent.py          — Subagent runtime + tool whitelist per type
goal_runner.py       — asyncio bridge between worker and orchestrator
worker.py            — daemon: poll → claim → heartbeat → run
db.py                — all goal-runtime CRUD: create_goal, claim_next_goal,
                       save_checkpoint, set_goal_plan, fact_save, etc.
```

## Where we are in the roadmap

This document describes what's shipped today (Phase 1 + 2 of the long-running
agent plan in `docs/superpowers/plans/2026-05-15-long-running-agent-architecture.md`).

Still ahead:
- **Phase 3**: per-goal persistent browser context (so a login from
  subtask 1 carries over to subtask 2..N across worker restarts)
- **Phase 4**: smarter loop detection (hash result, not just args), smart
  compaction that preserves the plan + recent subtask summaries
- **Phase 5**: Web UI tab for live goal monitoring, pause/resume from UI
- **Phase 6**: migrate scheduler routines + telegram + CLI to use the
  Goals API as the long-running execution backend
