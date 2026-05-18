"""Central commands registry — metadata-only single source of truth.

Pins the shape of every command, the resolution rules, and the surfaces
each command opts into. Inspired by Hermes Agent's `COMMAND_REGISTRY`
pattern (downstream consumers derive their behavior from one list, never
duplicate it).
"""
from __future__ import annotations

import pytest

import commands


# ── Registry shape sanity ────────────────────────────────────────────────────


def test_registry_is_nonempty():
    assert len(commands.COMMAND_REGISTRY) > 0


def test_every_entry_has_at_least_one_valid_surface():
    """No orphan commands — every entry exposes itself to at least one
    surface, and every surface id is from the closed set."""
    for cmd in commands.COMMAND_REGISTRY:
        assert cmd.surfaces, f"{cmd.name}: empty surfaces — pointless entry"
        for s in cmd.surfaces:
            assert s in commands.SURFACES, f"{cmd.name}: bad surface '{s}'"


def test_every_entry_has_description_and_category():
    for cmd in commands.COMMAND_REGISTRY:
        assert cmd.description, f"{cmd.name}: empty description"
        assert len(cmd.description) <= 256, (
            f"{cmd.name}: description {len(cmd.description)} chars > 256 "
            f"(Telegram BotCommand limit)"
        )
        assert cmd.category, f"{cmd.name}: empty category"


def test_command_names_are_unique():
    """No two entries share a canonical name."""
    names = [c.name for c in commands.COMMAND_REGISTRY]
    assert len(names) == len(set(names)), (
        f"duplicate command names: {[n for n in names if names.count(n) > 1]}"
    )


def test_command_names_have_no_leading_slash():
    """Canonical names store WITHOUT the slash — the slash is a surface
    convention. Mixing causes lookup bugs."""
    for cmd in commands.COMMAND_REGISTRY:
        assert not cmd.name.startswith("/"), (
            f"{cmd.name}: stored with leading slash; should be bare"
        )


def test_aliases_do_not_collide_with_names_or_each_other():
    """An alias for one command must not be the canonical name of another,
    and aliases between commands must not overlap."""
    seen: dict[str, str] = {}  # alias-or-name → which command owns it
    for cmd in commands.COMMAND_REGISTRY:
        for token in (cmd.name, *cmd.aliases):
            if token in seen and seen[token] != cmd.name:
                pytest.fail(
                    f"token '{token}' claimed by both {seen[token]!r} and "
                    f"{cmd.name!r}"
                )
            seen[token] = cmd.name


# ── Lookup helpers ──────────────────────────────────────────────────────────


def test_by_name_returns_correct_entry():
    cmd = commands.by_name("help")
    assert cmd is not None
    assert cmd.name == "help"


def test_by_name_strips_leading_slash():
    """User input arrives as "/help" — lookup must work either way."""
    assert commands.by_name("/help") == commands.by_name("help")


def test_by_name_is_case_insensitive():
    """Case-insensitive — /HELP / /Help / /help all map to the same entry."""
    cmd = commands.by_name("HELP")
    assert cmd is not None and cmd.name == "help"


def test_by_name_returns_none_for_unknown():
    assert commands.by_name("totally-made-up-command") is None
    assert commands.by_name("") is None
    assert commands.by_name("/") is None
    assert commands.by_name("   ") is None


def test_resolve_handles_aliases():
    """If we add an alias to a command, resolve should find it via that
    alias too. We use 'preset' which has 'presets' as an alias."""
    canonical = commands.by_name("preset")
    assert canonical is not None
    via_alias = commands.resolve("presets")
    assert via_alias is not None
    assert via_alias.name == canonical.name


def test_resolve_takes_first_token_only():
    """Users type `/cron list 5` — resolve must look up "cron", not the
    whole string."""
    cmd = commands.resolve("cron list --filter foo")
    assert cmd is not None
    assert cmd.name == "cron"


def test_resolve_strips_leading_slash():
    assert commands.resolve("/help") == commands.resolve("help")


def test_resolve_returns_none_for_empty_or_unknown():
    assert commands.resolve("") is None
    assert commands.resolve("/") is None
    assert commands.resolve("nonexistent") is None


# ── Surface filtering ──────────────────────────────────────────────────────


def test_for_surface_tg_returns_only_tg_commands():
    tg_cmds = commands.for_surface("tg")
    assert all("tg" in c.surfaces for c in tg_cmds)
    # Sanity: must include /help (it's everywhere)
    assert "help" in {c.name for c in tg_cmds}


def test_for_surface_cli_returns_only_cli_commands():
    cli_cmds = commands.for_surface("cli")
    assert all("cli" in c.surfaces for c in cli_cmds)


def test_for_surface_web_returns_only_web_commands():
    web_cmds = commands.for_surface("web")
    assert all("web" in c.surfaces for c in web_cmds)
    # /cron and /tools are web-specific entry points
    names = {c.name for c in web_cmds}
    assert "cron" in names


def test_for_surface_unknown_returns_empty():
    """Defensive: misconfigured client passes ``surface=android`` → empty
    list, not an exception (the API endpoint stays cheap and silent)."""
    assert commands.for_surface("android") == []
    assert commands.for_surface("") == []
    assert commands.for_surface("ANYTHING") == []


def test_for_surface_preserves_registry_order():
    """/help renders in this order; tests pin it so reordering the
    registry is a conscious change."""
    cli_cmds = commands.for_surface("cli")
    all_cli = [c for c in commands.COMMAND_REGISTRY if "cli" in c.surfaces]
    assert [c.name for c in cli_cmds] == [c.name for c in all_cli]


# ── Categories grouping ──────────────────────────────────────────────────────


def test_categories_for_groups_by_category():
    groups = commands.categories_for("tg")
    assert isinstance(groups, dict)
    # Every command appears exactly once across categories
    total = sum(len(v) for v in groups.values())
    assert total == len(commands.for_surface("tg"))


def test_categories_for_unknown_surface_empty_dict():
    assert commands.categories_for("nope") == {}


def test_all_names_matches_registry():
    """Sanity: all_names() returns one name per entry, in order."""
    names = commands.all_names()
    assert len(names) == len(commands.COMMAND_REGISTRY)
    assert names == [c.name for c in commands.COMMAND_REGISTRY]


# ── Telegram integration round-trip ─────────────────────────────────────────


def test_telegram_get_commands_uses_registry(monkeypatch):
    """telegram_bot.get_commands() must derive from the central registry —
    not a duplicate in-module dict like before."""
    import telegram_bot
    payload = telegram_bot.get_commands()
    registry_tg = commands.for_surface("tg")
    # Same count
    assert len(payload) == len(registry_tg)
    # Same names in same order
    assert [e["command"] for e in payload] == [c.name for c in registry_tg]
    # Each description ≤256 (Telegram BotCommand cap — enforced by truncation)
    for entry in payload:
        assert len(entry["description"]) <= 256


# ── Server API round-trip ───────────────────────────────────────────────────


def test_api_commands_endpoint_returns_web_surface(qwe_temp_data_dir):
    """GET /api/commands defaults to surface=web and returns the right shape."""
    from fastapi.testclient import TestClient
    import server  # noqa: F401 — registers the route on the app
    with TestClient(server.app) as client:
        r = client.get("/api/commands")
        assert r.status_code == 200
        body = r.json()
        assert body["surface"] == "web"
        names = {c["name"] for c in body["commands"]}
        web_names = {c.name for c in commands.for_surface("web")}
        assert names == web_names


def test_api_commands_endpoint_respects_surface_param(qwe_temp_data_dir):
    from fastapi.testclient import TestClient
    import server  # noqa: F401
    with TestClient(server.app) as client:
        r = client.get("/api/commands?surface=tg")
        assert r.status_code == 200
        body = r.json()
        assert body["surface"] == "tg"
        names = {c["name"] for c in body["commands"]}
        tg_names = {c.name for c in commands.for_surface("tg")}
        assert names == tg_names


def test_api_commands_endpoint_unknown_surface_empty_list(qwe_temp_data_dir):
    """Unknown surface: empty commands list, status 200, surface echoed back."""
    from fastapi.testclient import TestClient
    import server  # noqa: F401
    with TestClient(server.app) as client:
        r = client.get("/api/commands?surface=android")
        assert r.status_code == 200
        body = r.json()
        assert body["surface"] == "android"
        assert body["commands"] == []
