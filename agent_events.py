"""Agent event system — replaces callback spaghetti with typed events."""

from dataclasses import dataclass, field
from typing import Callable
import logger

_log = logger.get("events")


@dataclass
class AgentEvent:
    """A typed event emitted during agent execution."""
    type: str
    data: dict = field(default_factory=dict)


# Event types
EVT_CONTENT_DELTA = "content_delta"      # text chunk from LLM
EVT_THINKING_DELTA = "thinking_delta"    # thinking/reasoning chunk
EVT_STATUS = "status"                    # status message (e.g. "thinking...")
EVT_TOOL_START = "tool_start"            # tool execution started
EVT_TOOL_DELTA = "tool_delta"            # tool output streaming
EVT_TOOL_END = "tool_end"               # tool execution finished
EVT_ERROR = "error"                      # error occurred
EVT_TURN_START = "turn_start"            # new turn in agent loop
EVT_TURN_END = "turn_end"               # turn completed
EVT_BUDGET_WARNING = "budget_warning"    # approaching budget limit


class EventEmitter:
    """Simple event emitter with type-based subscriptions."""

    def __init__(self):
        self._handlers: dict[str, list[Callable]] = {}
        self._global_handlers: list[Callable] = []

    def on(self, event_type: str, handler: Callable[[AgentEvent], None]):
        """Subscribe to a specific event type."""
        self._handlers.setdefault(event_type, []).append(handler)

    def on_all(self, handler: Callable[[AgentEvent], None]):
        """Subscribe to all events."""
        self._global_handlers.append(handler)

    def emit(self, event: AgentEvent):
        """Emit an event to all subscribers."""
        for h in self._global_handlers:
            try:
                h(event)
            except Exception as e:
                _log.debug(f"event handler error ({event.type}): {e}")
        for h in self._handlers.get(event.type, []):
            try:
                h(event)
            except Exception as e:
                _log.debug(f"event handler error ({event.type}): {e}")

    def clear(self):
        """Remove all handlers."""
        self._handlers.clear()
        self._global_handlers.clear()

    # Convenience methods
    def content(self, text: str):
        self.emit(AgentEvent(EVT_CONTENT_DELTA, {"text": text}))

    def thinking(self, text: str):
        self.emit(AgentEvent(EVT_THINKING_DELTA, {"text": text}))

    def status(self, text: str):
        self.emit(AgentEvent(EVT_STATUS, {"text": text}))

    def tool_start(self, name: str, args: str):
        self.emit(AgentEvent(EVT_TOOL_START, {"name": name, "args": args}))

    def tool_end(self, name: str, result: str, duration_ms: int = 0):
        self.emit(AgentEvent(EVT_TOOL_END, {"name": name, "result": result, "duration_ms": duration_ms}))

    def error(self, message: str):
        self.emit(AgentEvent(EVT_ERROR, {"message": message}))
