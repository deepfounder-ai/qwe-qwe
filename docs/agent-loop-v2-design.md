# Agent Loop v2 — Design Doc

## Goal

Refactor qwe-qwe agent loop inspired by claw-code-agent architecture.
Keep qwe-qwe's unique features (soul, knowledge graph, skills, memory), upgrade the core loop.

## Current Problems

1. JSON repair is complex but still fails on small models
2. No continuation on max_tokens truncation
3. Single budget metric (max_rounds) — no token/cost tracking
4. Callback spaghetti (5 separate callback globals)
5. Anti-hedge nudge is fragile and pollutes history
6. Tool execution mixed with agent loop logic

## What We Take from claw-code-agent

### 1. Clean Agent Loop Structure
```python
for turn in range(max_turns):
    # 1. Check budget
    # 2. Call LLM (stream)
    # 3. If tool_calls → execute each → append results → continue
    # 4. If no tool_calls:
    #    - If finish_reason == 'length' → continuation prompt → continue
    #    - Else → return final response
```

### 2. Continuation Handling
When model hits max_tokens (finish_reason='length'), inject:
```
[system] Your response was truncated. Continue exactly where you left off.
```
Then remove this message after model continues. No history pollution.

### 3. Budget System
```python
@dataclass
class Budget:
    max_turns: int = 30
    max_tool_calls: int = 100
    max_input_tokens: int = 0  # 0 = unlimited
    max_output_tokens: int = 0
    
    def check(self, stats: TurnStats) -> tuple[bool, str]:
        """Returns (exceeded, reason)"""
```

### 4. StreamEvent System
Replace 5 global callbacks with one event emitter:
```python
class AgentEvent:
    type: str  # content_delta, thinking_delta, tool_start, tool_delta, tool_end, status, error
    data: dict

class EventEmitter:
    def emit(self, event: AgentEvent): ...
    def on(self, type: str, handler: Callable): ...
```

### 5. Tool Registry
```python
@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    handler: Callable[[dict], str]
    
    def to_openai_schema(self) -> dict: ...

class ToolRegistry:
    def register(self, tool: Tool): ...
    def execute(self, name: str, args: dict) -> str: ...
    def get_schemas(self) -> list[dict]: ...
```

## What We Keep from qwe-qwe

- JSON repair (small models need it)
- Self-check for dangerous tools (shell, write_file)
- Soul system (personality, prompts)
- Knowledge graph (3-layer memory)
- Skills system (pluggable modules)
- MCP integration
- Tool search meta-tool (token savings)
- Experience learning
- Spicy Duck :)

## Implementation Plan

### Phase 1: Extract Tool Registry (agent_tools_v2.py)
- Decouple tool definitions from execution
- Clean handler signatures: `(args: dict) -> str`
- ToolRegistry class with register/execute/get_schemas
- Migrate existing tools one by one

### Phase 2: Event System (agent_events.py)
- AgentEvent dataclass
- EventEmitter with type-based listeners
- Replace _status_callback, _thinking_callback, _content_callback, _tool_call_callback
- Server/CLI/Telegram subscribe to events

### Phase 3: Budget System (agent_budget.py)
- Budget dataclass with multiple limits
- Check at start of each turn + before tool execution
- Track real token usage from stream
- Replace single max_tool_rounds

### Phase 4: Rewrite Agent Loop (agent_loop.py)
- Clean loop: call LLM → process tools → check budget → continue/stop
- Continuation handling for max_tokens
- Remove anti-hedge (continuation handles truncation naturally)
- Remove nudge messages (no history pollution)
- Keep JSON repair as fallback
- Keep self-check for dangerous tools

### Phase 5: Integrate
- agent.py becomes thin wrapper around agent_loop
- server.py subscribes to events
- CLI subscribes to events
- telegram_bot.py subscribes to events
- All existing features work through new loop

## File Structure

```
agent_loop.py      — new core loop (replaces _run_inner)
agent_tools_v2.py  — tool registry + execution
agent_events.py    — event system
agent_budget.py    — budget tracking
agent.py           — thin wrapper (run, _build_messages, experience, etc.)
```

## Migration Strategy

1. Build new modules alongside existing code
2. Add feature flag: `config.get("agent_loop_v2")` 
3. When v2 stable, remove v1 code
4. No breaking changes to server.py/cli.py/telegram_bot.py API
