"""Plugin slot framework — discovery via entry_points, lookup helpers.

Tests cover:
  - Known slots are enumerable
  - find / list_all / list_entries behaviors
  - Unknown slot is defensive (empty / None, no exception)
  - Cache: same lookup twice doesn't re-scan
  - clear_cache forces re-discovery
  - Test-injection helpers _override_for_test / _clear_test_overrides
  - Real entry_points enumeration with mocked importlib_metadata
  - Plugin load failure logs + skips (doesn't kill discovery)
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import plugin_registry as pr


# ── Slot enumeration ────────────────────────────────────────────────────────


def test_slots_returns_known_slot_names():
    names = pr.slots()
    assert pr.SLOT_MEMORY_BACKEND in names
    assert pr.SLOT_CONTEXT_ENGINE in names
    assert pr.SLOT_MODEL_PROVIDER in names
    assert pr.SLOT_OBSERVABILITY in names


def test_known_slots_set_is_frozen():
    """Defensive: caller can't accidentally mutate the master set."""
    # frozenset is immutable; trying to mutate raises AttributeError
    with pytest.raises(AttributeError):
        pr.KNOWN_SLOTS.add("new_slot")  # type: ignore[attr-defined]


# ── Defensive behavior ──────────────────────────────────────────────────────


def test_find_unknown_slot_returns_none():
    """Lookups on unknown slot are silent — Castor's runtime defensively
    queries optional slots that might not exist on this install."""
    assert pr.find("nonexistent_slot", "anyname") is None


def test_list_all_unknown_slot_returns_empty():
    assert pr.list_all("nonexistent_slot") == []


def test_list_entries_unknown_slot_returns_empty():
    assert pr.list_entries("nonexistent_slot") == []


def test_find_known_slot_unknown_name_returns_none():
    pr._clear_test_overrides()
    pr.clear_cache(pr.SLOT_MEMORY_BACKEND)
    assert pr.find(pr.SLOT_MEMORY_BACKEND, "made_up_plugin") is None


# ── Test injection helpers ──────────────────────────────────────────────────


def test_override_for_test_injects_plugins():
    fake_plugin = MagicMock(name="HonchoPlugin")
    pr._override_for_test(pr.SLOT_MEMORY_BACKEND, {"honcho": fake_plugin})
    try:
        assert pr.find(pr.SLOT_MEMORY_BACKEND, "honcho") is fake_plugin
        assert "honcho" in pr.list_all(pr.SLOT_MEMORY_BACKEND)
    finally:
        pr._clear_test_overrides()


def test_override_bypasses_real_discovery():
    """Test injection wins over what's actually installed. Without this,
    tests would need a real plugin package to exist."""
    pr._override_for_test(pr.SLOT_MEMORY_BACKEND, {"alpha": "value-a"})
    try:
        assert pr.find(pr.SLOT_MEMORY_BACKEND, "alpha") == "value-a"
    finally:
        pr._clear_test_overrides()


def test_clear_test_overrides_restores_real_discovery():
    pr._override_for_test(pr.SLOT_MEMORY_BACKEND, {"alpha": "value-a"})
    assert pr.list_all(pr.SLOT_MEMORY_BACKEND) == ["alpha"]
    pr._clear_test_overrides()
    pr.clear_cache(pr.SLOT_MEMORY_BACKEND)
    # Real entry_points returns whatever's installed (usually nothing).
    real = pr.list_all(pr.SLOT_MEMORY_BACKEND)
    # No fake "alpha" leaking through
    assert "alpha" not in real


def test_list_entries_returns_full_plugin_entry():
    """list_entries gives PluginEntry objects (richer than list_all)."""
    fake = MagicMock(name="HonchoPlugin")
    pr._override_for_test(pr.SLOT_MEMORY_BACKEND, {"honcho": fake})
    try:
        entries = pr.list_entries(pr.SLOT_MEMORY_BACKEND)
        assert len(entries) == 1
        e = entries[0]
        assert e.name == "honcho"
        assert e.slot == pr.SLOT_MEMORY_BACKEND
        assert e.value is fake
    finally:
        pr._clear_test_overrides()


# ── Discovery via entry_points (mocked) ─────────────────────────────────────


def _make_fake_ep(name: str, value):
    """Build a fake entry-point that loads to ``value`` on .load()."""
    ep = MagicMock(name=f"FakeEP-{name}")
    ep.name = name
    ep.load.return_value = value
    return ep


def test_discover_reads_entry_points_for_slot(monkeypatch):
    """The _discover helper enumerates entry_points under the
    castor.<slot> group and loads each."""
    pr.clear_cache(pr.SLOT_CONTEXT_ENGINE)
    pr._clear_test_overrides()
    fake_value_a = object()
    fake_value_b = object()

    def fake_entry_points(group):
        if group == "castor.context_engine":
            return [
                _make_fake_ep("engine_a", fake_value_a),
                _make_fake_ep("engine_b", fake_value_b),
            ]
        return []

    monkeypatch.setattr(pr.importlib_metadata, "entry_points",
                        fake_entry_points)

    assert pr.find(pr.SLOT_CONTEXT_ENGINE, "engine_a") is fake_value_a
    assert pr.find(pr.SLOT_CONTEXT_ENGINE, "engine_b") is fake_value_b
    assert sorted(pr.list_all(pr.SLOT_CONTEXT_ENGINE)) == ["engine_a", "engine_b"]


def test_discover_skips_broken_plugins(monkeypatch):
    """One plugin failing to import must NOT kill discovery for the rest."""
    pr.clear_cache(pr.SLOT_MODEL_PROVIDER)
    pr._clear_test_overrides()

    good = object()
    broken_ep = MagicMock(name="BrokenEP")
    broken_ep.name = "broken"
    broken_ep.load.side_effect = ImportError("missing dep")

    def fake_entry_points(group):
        if group == "castor.model_provider":
            return [
                _make_fake_ep("alpha", good),
                broken_ep,
                _make_fake_ep("beta", object()),
            ]
        return []

    monkeypatch.setattr(pr.importlib_metadata, "entry_points",
                        fake_entry_points)

    # "broken" is skipped; "alpha" and "beta" are still discoverable.
    assert pr.find(pr.SLOT_MODEL_PROVIDER, "alpha") is good
    assert pr.find(pr.SLOT_MODEL_PROVIDER, "broken") is None
    assert "broken" not in pr.list_all(pr.SLOT_MODEL_PROVIDER)
    assert {"alpha", "beta"} == set(pr.list_all(pr.SLOT_MODEL_PROVIDER))


def test_discover_handles_entry_points_failure_gracefully(monkeypatch):
    """If importlib_metadata.entry_points itself raises (super rare —
    corrupted site-packages), return empty list. Don't crash Castor."""
    pr.clear_cache(pr.SLOT_OBSERVABILITY)
    pr._clear_test_overrides()

    def fake_entry_points(group):
        raise RuntimeError("metadata corrupted")

    monkeypatch.setattr(pr.importlib_metadata, "entry_points",
                        fake_entry_points)

    assert pr.list_all(pr.SLOT_OBSERVABILITY) == []
    assert pr.find(pr.SLOT_OBSERVABILITY, "any") is None


# ── Cache behavior ──────────────────────────────────────────────────────────


def test_cache_avoids_rescanning(monkeypatch):
    """Once a slot is discovered, subsequent lookups must NOT re-scan
    entry_points (expensive — touches whole site-packages)."""
    pr.clear_cache(pr.SLOT_OBSERVABILITY)
    pr._clear_test_overrides()

    call_count = {"n": 0}

    def fake_entry_points(group):
        call_count["n"] += 1
        return [_make_fake_ep("obs_a", "value")]

    monkeypatch.setattr(pr.importlib_metadata, "entry_points",
                        fake_entry_points)

    pr.find(pr.SLOT_OBSERVABILITY, "obs_a")
    pr.find(pr.SLOT_OBSERVABILITY, "obs_a")
    pr.list_all(pr.SLOT_OBSERVABILITY)

    assert call_count["n"] == 1, f"entry_points re-scanned {call_count['n']} times — cache broken"


def test_clear_cache_forces_rescan(monkeypatch):
    """clear_cache(slot) makes the next lookup re-scan entry_points."""
    pr.clear_cache(pr.SLOT_OBSERVABILITY)
    pr._clear_test_overrides()

    call_count = {"n": 0}

    def fake_entry_points(group):
        call_count["n"] += 1
        return [_make_fake_ep("obs_a", "value")]

    monkeypatch.setattr(pr.importlib_metadata, "entry_points",
                        fake_entry_points)

    pr.find(pr.SLOT_OBSERVABILITY, "obs_a")
    assert call_count["n"] == 1

    pr.clear_cache(pr.SLOT_OBSERVABILITY)
    pr.find(pr.SLOT_OBSERVABILITY, "obs_a")
    assert call_count["n"] == 2


def test_clear_cache_none_clears_all_slots(monkeypatch):
    """clear_cache(None) wipes every slot's cache."""
    pr.clear_cache(None)
    pr._clear_test_overrides()

    call_count = {"n": 0}

    def fake_entry_points(group):
        call_count["n"] += 1
        return [_make_fake_ep("x", "y")]

    monkeypatch.setattr(pr.importlib_metadata, "entry_points",
                        fake_entry_points)

    pr.list_all(pr.SLOT_MEMORY_BACKEND)
    pr.list_all(pr.SLOT_CONTEXT_ENGINE)
    assert call_count["n"] == 2  # one per slot

    pr.clear_cache(None)  # nuke all
    pr.list_all(pr.SLOT_MEMORY_BACKEND)
    pr.list_all(pr.SLOT_CONTEXT_ENGINE)
    assert call_count["n"] == 4


# ── Entry-point group naming ────────────────────────────────────────────────


def test_entry_point_group_naming_convention():
    """Plugins use the entry-point group ``castor.<slot>``. This
    convention is documented and stable — third-party packages encode
    it in their pyproject.toml. Don't change it casually."""
    assert pr._entry_point_group("memory_backend") == "castor.memory_backend"
    assert pr._entry_point_group("context_engine") == "castor.context_engine"
    assert pr._entry_point_group("anything") == "castor.anything"
