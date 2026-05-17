"""Tests for skill integrity hash verification and tool namespace collision."""
import json
from pathlib import Path

import pytest

# Minimal valid skill content for testing
_VALID_SKILL = '''\
DESCRIPTION = "Test skill"
INSTRUCTION = "Test"
TOOLS = [{"type": "function", "function": {"name": "test_tool", "description": "t", "parameters": {"type": "object", "properties": {}}}}]
def execute(name, args):
    return "ok"
'''

_VALID_SKILL_B = '''\
DESCRIPTION = "Test skill B"
INSTRUCTION = "Test B"
TOOLS = [{"type": "function", "function": {"name": "test_tool_b", "description": "tb", "parameters": {"type": "object", "properties": {}}}}]
def execute(name, args):
    return "ok_b"
'''

_COLLIDING_SKILL = '''\
DESCRIPTION = "Colliding skill"
INSTRUCTION = "Collides"
TOOLS = [{"type": "function", "function": {"name": "test_tool", "description": "collision", "parameters": {"type": "object", "properties": {}}}}]
def execute(name, args):
    return "collide"
'''


@pytest.fixture
def skill_env(tmp_path, monkeypatch):
    """Set up isolated skill directories for testing."""
    import importlib

    user_dir = tmp_path / "skills"
    user_dir.mkdir()
    builtin_dir = tmp_path / "builtin_skills"
    builtin_dir.mkdir()
    manifest_path = tmp_path / "skills_manifest.json"

    # Patch module-level constants
    import skills as skills_mod
    monkeypatch.setattr(skills_mod, "USER_SKILLS_DIR", user_dir)
    monkeypatch.setattr(skills_mod, "_MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(skills_mod, "BUILTIN_SKILLS_DIR", builtin_dir)
    # Clear module cache
    skills_mod._module_cache.clear()

    return {
        "user_dir": user_dir,
        "builtin_dir": builtin_dir,
        "manifest_path": manifest_path,
        "mod": skills_mod,
    }


class TestIntegrityHash:
    def test_first_load_creates_manifest_entry(self, skill_env):
        env = skill_env
        skill_file = env["user_dir"] / "test_a.py"
        skill_file.write_text(_VALID_SKILL)

        mod = env["mod"]._load_module(skill_file)
        assert hasattr(mod, "DESCRIPTION")

        # Manifest should exist with an entry
        assert env["manifest_path"].exists()
        manifest = json.loads(env["manifest_path"].read_text())
        cache_key = str(skill_file.resolve())
        assert cache_key in manifest
        assert len(manifest[cache_key]) == 64  # SHA-256 hex

    def test_unchanged_skill_passes(self, skill_env):
        env = skill_env
        skill_file = env["user_dir"] / "test_b.py"
        skill_file.write_text(_VALID_SKILL)

        # First load — register
        env["mod"]._load_module(skill_file)
        # Clear module cache to force re-read
        env["mod"]._module_cache.clear()
        # Second load — verify (should not raise)
        mod = env["mod"]._load_module(skill_file)
        assert hasattr(mod, "DESCRIPTION")

    def test_modified_skill_blocked(self, skill_env):
        env = skill_env
        skill_file = env["user_dir"] / "test_c.py"
        skill_file.write_text(_VALID_SKILL)

        # First load — register
        env["mod"]._load_module(skill_file)
        env["mod"]._module_cache.clear()

        # Modify the file
        skill_file.write_text(_VALID_SKILL + "\n# tampered\n")

        with pytest.raises(ImportError, match="integrity check failed"):
            env["mod"]._load_module(skill_file)

    def test_builtin_skill_exempt(self, skill_env):
        env = skill_env
        # Place skill in builtin dir (not user dir)
        skill_file = env["builtin_dir"] / "builtin_test.py"
        skill_file.write_text(_VALID_SKILL)

        mod = env["mod"]._load_module(skill_file)
        assert hasattr(mod, "DESCRIPTION")

        # No manifest entry for builtin skill
        if env["manifest_path"].exists():
            manifest = json.loads(env["manifest_path"].read_text())
            assert str(skill_file.resolve()) not in manifest

    def test_corrupt_manifest_handled(self, skill_env):
        env = skill_env
        skill_file = env["user_dir"] / "test_d.py"
        skill_file.write_text(_VALID_SKILL)

        # Write corrupt manifest
        env["manifest_path"].write_text("not json{{{")

        # Should not raise — re-registers
        mod = env["mod"]._load_module(skill_file)
        assert hasattr(mod, "DESCRIPTION")

        # Manifest should be valid now
        manifest = json.loads(env["manifest_path"].read_text())
        assert str(skill_file.resolve()) in manifest


class TestToolNamespaceCollision:
    def test_duplicate_tool_name_warns(self, skill_env, caplog):
        import logging

        env = skill_env
        # Create two skills with colliding tool name
        (env["user_dir"] / "skill_a.py").write_text(_VALID_SKILL)
        (env["user_dir"] / "skill_b.py").write_text(_COLLIDING_SKILL)

        # Patch get_active to return both skills
        env["mod"]._module_cache.clear()
        original_get_active = env["mod"].get_active
        env["mod"].get_active = lambda: ["skill_a", "skill_b"]
        original_find = env["mod"]._find_skill
        env["mod"]._find_skill = lambda name: env["user_dir"] / f"{name}.py"

        try:
            with caplog.at_level(logging.WARNING, logger="skills"):
                tools = env["mod"].get_tools()
            # Should only have 1 tool named "test_tool" (first wins)
            test_tools = [t for t in tools if t.get("function", {}).get("name") == "test_tool"]
            assert len(test_tools) == 1
            assert "collision" in caplog.text.lower() or "Collision" in caplog.text
        finally:
            env["mod"].get_active = original_get_active
            env["mod"]._find_skill = original_find

    def test_unique_tools_all_returned(self, skill_env):
        env = skill_env
        (env["user_dir"] / "skill_a.py").write_text(_VALID_SKILL)
        (env["user_dir"] / "skill_b.py").write_text(_VALID_SKILL_B)

        env["mod"]._module_cache.clear()
        original_get_active = env["mod"].get_active
        env["mod"].get_active = lambda: ["skill_a", "skill_b"]
        original_find = env["mod"]._find_skill
        env["mod"]._find_skill = lambda name: env["user_dir"] / f"{name}.py"

        try:
            tools = env["mod"].get_tools()
            names = [t.get("function", {}).get("name") for t in tools]
            assert "test_tool" in names
            assert "test_tool_b" in names
        finally:
            env["mod"].get_active = original_get_active
            env["mod"]._find_skill = original_find
