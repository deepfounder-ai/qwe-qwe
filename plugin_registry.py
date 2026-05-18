"""Plugin slot framework — formalizes extension points for third-party packages.

Inspired by Hermes Agent's `plugins/` layout (memory/context_engine/
model-providers/observability/...) — a thin registry that lets packages
register themselves under named slots via Python's standard
``importlib.metadata.entry_points`` mechanism.

## Slot model

A "slot" is a category of plug-in (e.g. ``memory_backend``,
``context_engine``). Plugins register against a slot under a name, and
Castor's runtime can look up registered implementations by name.

Built-in code is NOT a plugin — ``memory.py``, ``providers.py`` and
friends remain the in-tree default for each slot. Plugins are
ALTERNATIVES that a downstream user can opt into via configuration
(e.g. set ``memory_backend=honcho`` to use the Honcho plugin).

## Distribution

Plugin packages declare entry points in their ``pyproject.toml``:

```toml
[project.entry-points."castor.memory_backend"]
honcho = "castor_honcho:Plugin"

[project.entry-points."castor.context_engine"]
qdrant_compressor = "castor_qdrant_compressor:Plugin"
```

The string ``castor_honcho:Plugin`` resolves to the ``Plugin`` attribute
of the ``castor_honcho`` module. That attribute can be anything —
a class, a factory function, a dataclass — Castor's per-slot consumer
decides what to do with it.

## Discovery

``find(slot, name)`` returns the resolved object (or ``None`` if absent).
``list_all(slot)`` returns all registered names for a slot. Discovery is
lazy — entry-points are scanned on first access per slot and cached
for the process lifetime.

## Not in scope (separate PRs)

- Converting in-tree modules (``memory.py``, ``providers.py``) to use
  this slot pattern — they keep working as the "built-in default" until
  a future PR migrates each.
- UI for browsing/selecting plugins — needs the API layer first.
- Plugin sandboxing — plugins are full Python modules loaded into the
  process; trusting them is the user's call.
- Auto-install (`pip install castor-honcho`) on lookup — explicit only.
"""
from __future__ import annotations

import importlib.metadata as importlib_metadata
import logging
from dataclasses import dataclass
from typing import Any

_log = logging.getLogger("castor.plugin_registry")


# Canonical slot names. Adding a new slot means adding a constant here +
# documenting what plugins under it look like. Constants are uppercase
# for "this is a registry identifier"; the actual entry-point group name
# is the lowercase ``castor.<slot>``.
SLOT_MEMORY_BACKEND = "memory_backend"
SLOT_CONTEXT_ENGINE = "context_engine"
SLOT_MODEL_PROVIDER = "model_provider"
SLOT_OBSERVABILITY = "observability"


# All slots Castor knows about. Querying an unknown slot is a no-op
# (empty list / None return) — keeps misconfigured callers silent
# rather than blowing up.
KNOWN_SLOTS: frozenset[str] = frozenset({
    SLOT_MEMORY_BACKEND,
    SLOT_CONTEXT_ENGINE,
    SLOT_MODEL_PROVIDER,
    SLOT_OBSERVABILITY,
})


@dataclass(frozen=True)
class PluginEntry:
    """One registered plugin. ``name`` is the entry-point name (e.g.
    ``"honcho"``), ``slot`` is the category, ``value`` is the loaded
    Python object (class / factory / module / whatever the slot's
    consumer expects).
    """
    name: str
    slot: str
    value: Any


# Process-lifetime cache: per-slot → {name: PluginEntry}. Discovery
# populates this on first access. Tests can override via _override_for_test.
_cache: dict[str, dict[str, PluginEntry]] = {}

# Test-injection override: when set for a slot, the cache is bypassed
# and this is returned verbatim. Cleared by _clear_test_overrides.
_test_overrides: dict[str, dict[str, PluginEntry]] = {}


def _entry_point_group(slot: str) -> str:
    """The Python entry-point group name for a slot. Convention:
    ``castor.<slot>``."""
    return f"castor.{slot}"


def _discover(slot: str) -> dict[str, PluginEntry]:
    """Scan entry points for a slot, load each, return name → entry map.

    Failures during loading (broken package, ImportError, attribute
    missing) are logged at WARNING and skipped — never raise from this
    function. A single broken plugin must not kill the whole slot's
    discovery.
    """
    if slot not in KNOWN_SLOTS:
        return {}
    group = _entry_point_group(slot)
    out: dict[str, PluginEntry] = {}
    try:
        eps = importlib_metadata.entry_points(group=group)
    except Exception as e:  # pragma: no cover — defensive
        _log.warning(f"failed to enumerate entry_points for {group}: {e}")
        return {}
    for ep in eps:
        try:
            value = ep.load()
        except Exception as e:
            _log.warning(
                f"plugin '{ep.name}' under slot '{slot}' failed to load: {e}"
            )
            continue
        out[ep.name] = PluginEntry(name=ep.name, slot=slot, value=value)
    return out


def _get_slot(slot: str) -> dict[str, PluginEntry]:
    """Resolve a slot's plugins, using test overrides if set, otherwise
    the lazy discovery cache.
    """
    if slot in _test_overrides:
        return _test_overrides[slot]
    if slot not in _cache:
        _cache[slot] = _discover(slot)
    return _cache[slot]


# ── Public API ──────────────────────────────────────────────────────────────


def find(slot: str, name: str) -> Any | None:
    """Look up a plugin by slot + name. Returns the loaded object or None.

    Unknown slots → None (defensive). Unknown names within a known slot
    → None.
    """
    if slot not in KNOWN_SLOTS:
        return None
    entries = _get_slot(slot)
    entry = entries.get(name)
    return entry.value if entry else None


def list_all(slot: str) -> list[str]:
    """All registered plugin names for a slot. Empty list for unknown
    slot. Order is whatever ``entry_points`` returns (typically
    alphabetic within a package, undefined across packages).
    """
    if slot not in KNOWN_SLOTS:
        return []
    return list(_get_slot(slot).keys())


def list_entries(slot: str) -> list[PluginEntry]:
    """Like ``list_all`` but returns the full PluginEntry objects."""
    if slot not in KNOWN_SLOTS:
        return []
    return list(_get_slot(slot).values())


def slots() -> list[str]:
    """All slot names Castor knows about, in declaration order."""
    return [
        SLOT_MEMORY_BACKEND,
        SLOT_CONTEXT_ENGINE,
        SLOT_MODEL_PROVIDER,
        SLOT_OBSERVABILITY,
    ]


def clear_cache(slot: str | None = None) -> None:
    """Force re-discovery on next access. ``None`` clears all slots.

    Called by integration code that knows entry points have changed
    (rare — usually the process restart handles this). Mostly for tests.
    """
    if slot is None:
        _cache.clear()
    else:
        _cache.pop(slot, None)


# ── Test injection ──────────────────────────────────────────────────────────


def _override_for_test(slot: str, plugins: dict[str, Any]) -> None:
    """Test helper: inject a fake plugin map for a slot.

    The map is ``{name: value}`` — the helper wraps each value in a
    ``PluginEntry`` automatically. Cleared by ``_clear_test_overrides``.
    """
    _test_overrides[slot] = {
        name: PluginEntry(name=name, slot=slot, value=value)
        for name, value in plugins.items()
    }


def _clear_test_overrides() -> None:
    """Test helper: remove all test injections so subsequent lookups
    fall back to real entry-point discovery."""
    _test_overrides.clear()
