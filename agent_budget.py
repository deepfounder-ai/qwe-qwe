"""Agent budget system — multi-dimensional limits for agent execution."""

from dataclasses import dataclass, field
import config


@dataclass
class BudgetLimits:
    """Configurable budget limits. 0 = unlimited."""
    max_turns: int = 0   # 0 = unlimited — rely on loop detection instead
    max_tool_calls: int = 0  # 0 = unlimited — per-tool frequency limit handles loops
    max_input_tokens: int = 0
    max_output_tokens: int = 0

    @classmethod
    def from_config(cls) -> "BudgetLimits":
        return cls(
            max_turns=config.get("max_tool_rounds"),
        )


@dataclass
class BudgetStats:
    """Tracks resource usage during agent execution."""
    turns: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_errors: int = 0

    def add_turn(self):
        self.turns += 1

    def add_tool_call(self):
        self.tool_calls += 1

    def add_tokens(self, input_tok: int = 0, output_tok: int = 0):
        self.input_tokens += input_tok
        self.output_tokens += output_tok

    def add_error(self):
        self.total_errors += 1


@dataclass
class BudgetDecision:
    """Result of a budget check."""
    exceeded: bool
    reason: str = ""


def check_budget(limits: BudgetLimits, stats: BudgetStats) -> BudgetDecision:
    """Check if any budget limit is exceeded."""
    if limits.max_turns and stats.turns >= limits.max_turns:
        return BudgetDecision(True, f"max turns ({limits.max_turns}) reached")
    if limits.max_tool_calls and stats.tool_calls >= limits.max_tool_calls:
        return BudgetDecision(True, f"max tool calls ({limits.max_tool_calls}) reached")
    if limits.max_input_tokens and stats.input_tokens >= limits.max_input_tokens:
        return BudgetDecision(True, f"input token budget ({limits.max_input_tokens}) exceeded")
    if limits.max_output_tokens and stats.output_tokens >= limits.max_output_tokens:
        return BudgetDecision(True, f"output token budget ({limits.max_output_tokens}) exceeded")
    return BudgetDecision(False)


def warning_check(limits: BudgetLimits, stats: BudgetStats) -> str | None:
    """Check if approaching a limit (80% threshold). Returns warning or None."""
    if limits.max_turns and stats.turns >= limits.max_turns - 2:
        remaining = limits.max_turns - stats.turns
        return f"{remaining} turns left — wrap up soon"
    if limits.max_tool_calls and stats.tool_calls >= limits.max_tool_calls * 0.8:
        return f"approaching tool call limit ({stats.tool_calls}/{limits.max_tool_calls})"
    return None
