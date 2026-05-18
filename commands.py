"""Central registry of all user-facing slash commands.

Inspired by Hermes Agent's `hermes_cli/commands.py` — a single source of
truth that every downstream consumer derives from automatically:

  - CLI (``cli.py``)               — chain of ``if user_input.startswith("/X")``
                                     handlers; can validate names against
                                     this registry + emit "did you mean?"
                                     suggestions for typos.
  - Telegram bot (``telegram_bot.py``) — ``setMyCommands`` API call uses
                                         the descriptions and names from
                                         here, so the predictive-input menu
                                         in Telegram is always in sync.
  - Web UI (``static/index.html``) — frontend can fetch
                                     ``GET /api/commands?surface=web`` to
                                     drive future slash-autocomplete UX.
  - ``/help`` rendering            — every surface that wants a help screen
                                     filters this list by its own surface
                                     id.

This module deliberately stores ONLY METADATA — never handler callables.
Per-surface dispatch logic differs sharply (the Rich panel printed by
the CLI for ``/cron`` is nothing like the navigation push the Web UI does
for the same command), so the handler bodies live with each surface's
existing code. Centralising metadata is the win we go for here.

Adding a new command: append a ``CommandDef`` literal below. Tests pin
the registry's shape — empty descriptions, conflicting names, or invalid
surface ids all fail the test suite.

Aliases vs canonical names: ``resolve("preset-list")`` returns the
``CommandDef`` for ``preset`` if that alias is registered. Each surface
should call ``resolve(token)`` before dispatch so the user can use any
declared alias.
"""
from __future__ import annotations

from dataclasses import dataclass, field


# All valid surface identifiers. Adding a new one (e.g. ``"discord"``,
# ``"slack"`` once we ship multi-platform gateway in P2.4) means
# extending this constant + the per-surface filter call sites.
SURFACES: frozenset[str] = frozenset({"cli", "tg", "web"})


# Categories used by ``/help`` to group commands when rendering.
# Free-form strings; common values are documented for consistency.
# Suggested values: ``"session"``, ``"settings"``, ``"info"``, ``"skills"``,
# ``"automation"``, ``"data"``.


@dataclass(frozen=True)
class CommandDef:
    """One slash command's metadata. Immutable on purpose — the registry
    is a static list, not a runtime mutable store. To add commands at
    plugin-load time, build a separate dynamic list and merge into this
    when querying via the helpers below.
    """
    name: str                     # canonical, NO leading slash
    description: str              # ≤256 chars (Telegram BotCommand limit)
    category: str                 # see notes above
    surfaces: frozenset[str]      # subset of SURFACES
    aliases: tuple[str, ...] = field(default_factory=tuple)
    args_hint: str | None = None  # e.g. "[arg]" — for /help rendering


# ── The registry ────────────────────────────────────────────────────────────
#
# Order here is the order /help should render. Keep grouped by category
# for human readability.
COMMAND_REGISTRY: list[CommandDef] = [
    # ── info ──
    CommandDef(
        name="help",
        description="Show available commands",
        category="info",
        surfaces=frozenset({"cli", "tg", "web"}),
    ),
    CommandDef(
        name="status",
        description="Agent status (model, provider, memory)",
        category="info",
        surfaces=frozenset({"tg"}),
    ),
    CommandDef(
        name="stats",
        description="Session statistics",
        category="info",
        surfaces=frozenset({"tg"}),
    ),
    CommandDef(
        name="doctor",
        description="Run diagnostics on all components",
        category="info",
        surfaces=frozenset({"tg"}),
    ),
    CommandDef(
        name="chatid",
        description="Show chat ID and topic ID",
        category="info",
        surfaces=frozenset({"tg"}),
    ),
    CommandDef(
        name="logs",
        description="View recent agent logs",
        category="info",
        surfaces=frozenset({"cli"}),
    ),

    # ── session ──
    CommandDef(
        name="thread",
        description="Show / switch / list conversation threads",
        category="session",
        surfaces=frozenset({"cli"}),
        args_hint="[list|switch <id>]",
    ),
    CommandDef(
        name="threads",
        description="List conversation threads",
        category="session",
        surfaces=frozenset({"tg"}),
    ),
    CommandDef(
        name="clear",
        description="Clear conversation in this thread",
        category="session",
        surfaces=frozenset({"tg"}),
    ),
    CommandDef(
        name="resume",
        description="Resume the last interrupted task",
        category="session",
        surfaces=frozenset({"tg"}),
    ),

    # ── settings ──
    CommandDef(
        name="model",
        description="Show current model and provider",
        category="settings",
        surfaces=frozenset({"cli", "tg"}),
    ),
    CommandDef(
        name="provider",
        description="Show / switch LLM provider",
        category="settings",
        surfaces=frozenset({"cli"}),
        args_hint="[name]",
    ),
    CommandDef(
        name="soul",
        description="Show personality traits",
        category="settings",
        surfaces=frozenset({"cli", "tg"}),
    ),
    CommandDef(
        name="profile",
        description="View / edit user profile",
        category="settings",
        surfaces=frozenset({"tg"}),
    ),
    CommandDef(
        name="settings",
        description="View / edit agent settings",
        category="settings",
        surfaces=frozenset({"tg"}),
    ),
    CommandDef(
        name="thinking",
        description="Toggle thinking mode on/off",
        category="settings",
        surfaces=frozenset({"tg"}),
    ),
    CommandDef(
        name="voice",
        description="Toggle voice mode (TTS) for this chat",
        category="settings",
        surfaces=frozenset({"tg"}),
    ),
    CommandDef(
        name="telegram",
        description="Configure Telegram bot integration",
        category="settings",
        surfaces=frozenset({"cli"}),
    ),

    # ── skills / data ──
    CommandDef(
        name="skills",
        description="List active skills",
        category="skills",
        surfaces=frozenset({"cli", "tg"}),
    ),
    CommandDef(
        name="preset",
        description="Activate / deactivate / list presets",
        category="skills",
        surfaces=frozenset({"cli"}),
        args_hint="[activate|deactivate|list <name>]",
        aliases=("presets",),
    ),
    CommandDef(
        name="mcp",
        description="Manage MCP servers",
        category="skills",
        surfaces=frozenset({"cli"}),
        args_hint="[list|add|remove|toggle]",
    ),
    CommandDef(
        name="wiki",
        description="Search local knowledge base / wiki",
        category="skills",
        surfaces=frozenset({"cli"}),
        args_hint="<query>",
    ),
    CommandDef(
        name="file",
        description="Attach a file to the next message",
        category="skills",
        surfaces=frozenset({"cli"}),
        args_hint="<path>",
    ),
    CommandDef(
        name="memory",
        description="Search agent memory",
        category="skills",
        surfaces=frozenset({"tg"}),
        args_hint="<query>",
    ),
    CommandDef(
        name="recall",
        description="Search memory and reply with matches",
        category="skills",
        surfaces=frozenset({"web"}),
        args_hint="<query>",
    ),
    CommandDef(
        name="remember",
        description="Save a fact to memory",
        category="skills",
        surfaces=frozenset({"web"}),
        args_hint="<fact>",
    ),
    CommandDef(
        name="tools",
        description="Open tool palette",
        category="skills",
        surfaces=frozenset({"web"}),
    ),

    # ── automation ──
    CommandDef(
        name="cron",
        description="List scheduled tasks (CLI: print, Web: navigate, TG: list)",
        category="automation",
        surfaces=frozenset({"cli", "tg", "web"}),
    ),
    CommandDef(
        name="heartbeat",
        description="Manage periodic tasks checklist",
        category="automation",
        surfaces=frozenset({"tg"}),
    ),
]


# ── Helpers ─────────────────────────────────────────────────────────────────


def by_name(name: str) -> CommandDef | None:
    """Look up a command by its canonical name. Strips leading ``/``."""
    name = name.lstrip("/").lower().strip()
    if not name:
        return None
    for cmd in COMMAND_REGISTRY:
        if cmd.name == name:
            return cmd
    return None


def resolve(token: str) -> CommandDef | None:
    """Resolve a name OR alias to its canonical CommandDef. The first
    word of ``token`` is taken; aliases match too. Used by surfaces that
    want users to be able to type any registered alias.
    """
    if not token:
        return None
    stripped = token.lstrip("/").strip()
    if not stripped:
        return None
    parts = stripped.split()
    if not parts:
        return None
    first = parts[0].lower()
    if not first:
        return None
    for cmd in COMMAND_REGISTRY:
        if cmd.name == first or first in cmd.aliases:
            return cmd
    return None


def for_surface(surface: str) -> list[CommandDef]:
    """Return all commands exposed on ``surface`` (e.g. ``"tg"``).

    Empty list if the surface id is unknown — never raises. Order matches
    registry order so /help rendering is stable.
    """
    if surface not in SURFACES:
        return []
    return [cmd for cmd in COMMAND_REGISTRY if surface in cmd.surfaces]


def all_names() -> list[str]:
    """All canonical names, in registry order. Useful for autocomplete."""
    return [cmd.name for cmd in COMMAND_REGISTRY]


def categories_for(surface: str) -> dict[str, list[CommandDef]]:
    """Group commands for a surface by category, preserving registry order
    within each category. Empty dict if surface unknown.

    /help renders typically iterate this and print one section per
    category.
    """
    out: dict[str, list[CommandDef]] = {}
    for cmd in for_surface(surface):
        out.setdefault(cmd.category, []).append(cmd)
    return out
