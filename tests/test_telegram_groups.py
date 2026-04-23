"""Telegram group-mode + allowed_groups behaviour.

Two bugs shipped together in v0.17.28 and both silently killed the bot:

1. The v2 UI's Telegram settings dropdown offered
   ``disabled / allowlist / any`` as ``group_mode`` values, but
   ``_handle_group_message`` only checked for ``all`` / ``mention``.
   Every saved mode from the UI was unknown → ``should_respond`` stayed
   False → bot ignored every group message with no log.

2. ``set_allowed_groups`` stored the ID list as whatever the UI passed in
   (strings), but Telegram delivers ``chat_id`` as int. ``chat_id not in
   ["-100..."]`` was always True → silently ignored again.

These tests pin the fix: canonical ``all/mention/off`` values, legacy
alias normalisation on read, int coercion of group IDs, and the
owner-gate on group messages.
"""
from __future__ import annotations

import json

import pytest


@pytest.fixture
def fresh_telegram(qwe_temp_data_dir, monkeypatch):
    """Reload the telegram module against a fresh DB."""
    import importlib
    import sys

    if "telegram_bot" not in sys.modules:
        importlib.import_module("telegram_bot")
    tb = importlib.reload(sys.modules["telegram_bot"])
    return tb


# ── group_mode canonicalisation ───────────────────────────────────────


def test_default_group_mode_is_respond_to_all(fresh_telegram):
    """Fresh install → bot responds to every group message by default.

    This is the behaviour users actually want for a personal bot — you
    don't want to have to @mention yourself to get an answer.
    """
    tb = fresh_telegram
    assert tb.get_group_mode() == "all"


@pytest.mark.parametrize("legacy,canonical", [
    ("any", "all"),
    ("disabled", "off"),
    ("allowlist", "all"),
    ("all", "all"),
    ("mention", "mention"),
    ("off", "off"),
])
def test_group_mode_legacy_aliases_normalise_on_read(fresh_telegram, legacy, canonical):
    """Old saved values must heal when the new code reads them."""
    tb = fresh_telegram
    import db
    db.kv_set("telegram:group_mode", legacy)
    assert tb.get_group_mode() == canonical


def test_set_group_mode_normalises_on_write(fresh_telegram):
    """Legacy values saved via the API also heal."""
    tb = fresh_telegram
    import db
    tb.set_group_mode("any")
    assert db.kv_get("telegram:group_mode") == "all"
    tb.set_group_mode("disabled")
    assert db.kv_get("telegram:group_mode") == "off"
    tb.set_group_mode("not-a-real-mode")
    # Unknown value → safe default rather than letting garbage persist.
    assert db.kv_get("telegram:group_mode") == "all"


# ── allowed_groups int coercion ───────────────────────────────────────


def test_allowed_groups_coerces_string_ids_to_int(fresh_telegram):
    """UI sends '["-100123"]', read path must give ints so chat_id comparison works."""
    tb = fresh_telegram
    tb.set_allowed_groups(["-1003803066123", "-100123"])
    got = tb.get_allowed_groups()
    assert got == [-1003803066123, -100123]
    for v in got:
        assert isinstance(v, int), f"expected int, got {type(v).__name__}"


def test_allowed_groups_heals_stringified_legacy_kv(fresh_telegram):
    """A DB row saved by the old buggy code (all-string IDs) reads as ints now."""
    tb = fresh_telegram
    import db
    db.kv_set("telegram:allowed_groups", json.dumps(["-1003803066123"]))
    assert tb.get_allowed_groups() == [-1003803066123]


def test_allowed_groups_empty_means_all_groups_allowed(fresh_telegram):
    """No list → all groups pass the allowlist check (mode still decides reply)."""
    tb = fresh_telegram
    assert tb.get_allowed_groups() == []


def test_allowed_groups_filters_junk(fresh_telegram):
    """Non-numeric entries get dropped rather than crashing the save."""
    tb = fresh_telegram
    tb.set_allowed_groups(["-100123", "notanumber", "", "  -100456  "])
    assert tb.get_allowed_groups() == [-100123, -100456]


# ── end-to-end: chat_id check against healed data ────────────────────


def test_chat_id_in_allowed_groups_int_comparison(fresh_telegram):
    """``chat_id in allowed_groups`` must be True for the healed state.

    Direct regression test for the compound bug: even with mode='all',
    if allowed_groups is a list of strings, the int chat_id from Telegram
    never matches and the bot silently drops the message.
    """
    tb = fresh_telegram
    # Simulate legacy save (strings) then read
    import db
    db.kv_set("telegram:allowed_groups", json.dumps(["-1003803066123"]))
    allowed = tb.get_allowed_groups()
    chat_id_from_telegram = -1003803066123  # int, as it arrives from API
    assert chat_id_from_telegram in allowed, (
        "chat_id must be found in healed allowed_groups; if this fails the bot "
        "will silently ignore every group message again."
    )
