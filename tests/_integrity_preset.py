"""Integrity smoke test — exercises the full preset lifecycle end-to-end.

Run it standalone (NOT via pytest):
    QWE_DATA_DIR=<tmp> python tests/_integrity_preset.py

Prints a numbered checklist; exits 0 on success, 1 on failure.
"""
import os
import sys
import json
import tempfile
import shutil
import zipfile
import importlib
from pathlib import Path

# --- Use an isolated data dir before importing config ---
tmp_home = tempfile.mkdtemp(prefix="qwe_integrity_")
os.environ["QWE_DATA_DIR"] = tmp_home

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

for m in ("config", "db", "soul", "presets"):
    if m in sys.modules:
        importlib.reload(sys.modules[m])
    else:
        importlib.import_module(m)

import config  # noqa: E402
import db  # noqa: E402
import soul  # noqa: E402
import presets  # noqa: E402
import yaml  # noqa: E402


def step(n: int, msg: str) -> None:
    print(f"  [{n:2d}] {msg}")


def main() -> int:
    print(f"DATA_DIR   : {config.DATA_DIR}")
    print(f"PRESETS_DIR: {config.PRESETS_DIR}")
    print()

    # Baseline soul
    soul.save("name", "BaseAgent")
    soul.save("humor", "moderate")
    soul.save("brevity", "moderate")

    # Phase 1: build a valid source preset
    src = Path(tmp_home) / "src_preset"
    src.mkdir()
    manifest = {
        "schema_version": 1,
        "id": "integrity-test",
        "name": "Integrity Test",
        "category": "testing",
        "version": "1.0.0",
        "author": {"name": "Smoke"},
        "license": {"type": "free"},
        "description": {
            "short": "Integrity smoke test preset",
            "long": "Used by CI smoke tests only. Not a real preset.",
            "language": "en",
        },
        "soul": {
            "agent_name": "Integrity",
            "language": "en",
            "traits": {
                "humor": "low",
                "honesty": "high",
                "curiosity": "high",
                "brevity": "high",
                "formality": "high",
                "proactivity": "high",
                "empathy": "low",
                "creativity": "low",
            },
        },
        "system_prompt": {"path": "system_prompt.md"},
        "skills": {
            "custom": [
                {
                    "path": "skills/domain_tool.py",
                    "name": "domain_tool",
                    "description": "demo",
                }
            ]
        },
        "knowledge": [{"path": "knowledge/ref.md", "title": "Reference"}],
        "compatibility": {"qwe_qwe_version": ">=0.1.0"},
    }
    (src / "preset.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    (src / "system_prompt.md").write_text("You are Integrity, a helpful test agent.\n", encoding="utf-8")
    (src / "README.md").write_text("# Integrity\n", encoding="utf-8")
    (src / "skills").mkdir()
    (src / "skills" / "domain_tool.py").write_text(
        'DESCRIPTION = "demo"\n'
        'TOOLS = [{"function": {"name": "demo_tool", "description": "x", '
        '"parameters": {"type": "object", "properties": {}}}}]\n'
        "def execute(name, args):\n"
        '    return "demo result"\n',
        encoding="utf-8",
    )
    (src / "knowledge").mkdir()
    (src / "knowledge" / "ref.md").write_text("# Reference\n\nDomain knowledge.\n", encoding="utf-8")
    step(1, "Source preset created")

    # Phase 2: pack + load archive + validate + install
    archive = Path(tmp_home) / "integrity-test-1.0.0.qwp"
    with zipfile.ZipFile(archive, "w") as zf:
        for f in src.rglob("*"):
            if f.is_file():
                zf.write(f, f.relative_to(src))
    step(2, f"Archive packed: {archive.stat().st_size} bytes")

    info = presets.load_archive(archive)
    assert info.id == "integrity-test"
    errors = presets.validate(info)
    assert errors == [], f"validation failed: {errors}"
    result = presets.install(info)
    step(3, f"Installed via archive → {result['path']}")

    row = db.fetchone(
        "SELECT id, version, name FROM presets WHERE id = ?",
        ("integrity-test",),
    )
    assert row and row[0] == "integrity-test" and row[1] == "1.0.0"
    step(4, f"DB row OK: {row}")

    target = config.PRESETS_DIR / "integrity-test"
    assert target.is_dir()
    assert (target / "preset.yaml").exists()
    assert (target / "system_prompt.md").exists()
    assert (target / "skills" / "domain_tool.py").exists()
    assert (target / "knowledge" / "ref.md").exists()
    step(5, "All files on disk")

    leftover = list(Path(tempfile.gettempdir()).glob("qwe_preset_*"))
    assert len(leftover) == 0, f"tempdir leaked: {leftover}"
    step(6, "Archive tempdir cleaned up")

    # Phase 3: activate + verify hooks
    presets.activate("integrity-test")
    assert presets.get_active() == "integrity-test"
    backup = json.loads(db.kv_get("soul_backup"))
    assert backup["name"] == "BaseAgent"
    assert backup["humor"] == "moderate"
    step(7, f"Activated, backup={backup['name']}/{backup['humor']}")

    cur = soul.load()
    assert cur["name"] == "Integrity"
    assert cur["humor"] == "low"
    assert cur["brevity"] == "high"
    step(8, f"Soul applied: name={cur['name']} humor={cur['humor']}")

    suffix = presets.get_system_prompt_suffix()
    assert "Integrity" in suffix
    skills_dir = presets.get_active_skills_dir()
    assert skills_dir and (skills_dir / "domain_tool.py").exists()
    step(9, "Hooks return correct values")

    prompt = soul.to_prompt(cur)
    assert "## Active preset: Integrity Test" in prompt
    assert "You are Integrity" in prompt
    step(10, "soul.to_prompt() includes preset section")

    import skills as _skills
    paths = _skills._all_skill_paths()
    assert "domain_tool" in paths
    assert str(paths["domain_tool"]).startswith(str(target))
    step(11, "Skills discovery picks up preset skill")

    # Phase 4: deactivate + verify rollback
    presets.deactivate()
    assert presets.get_active() is None
    assert not db.kv_get("soul_backup")
    restored = soul.load()
    assert restored["name"] == "BaseAgent"
    assert restored["humor"] == "moderate"
    step(12, f"Deactivated → soul restored to {restored['name']}")

    assert presets.get_system_prompt_suffix() == ""
    assert presets.get_active_skills_dir() is None
    prompt_after = soul.to_prompt(restored)
    assert "## Active preset:" not in prompt_after
    step(13, "Hooks return neutral after deactivate")

    # Phase 5: dev-link by bare id
    os.environ["QWE_MARKET_PATH"] = str(Path(tmp_home) / "fake_market")
    market_root = Path(os.environ["QWE_MARKET_PATH"]) / "presets" / "testing"
    market_root.mkdir(parents=True)
    shutil.copytree(src, market_root / "integrity-test")
    presets.uninstall("integrity-test")
    assert not (config.PRESETS_DIR / "integrity-test").exists()
    assert db.fetchone("SELECT id FROM presets WHERE id = ?", ("integrity-test",)) is None
    step(14, "Uninstall cleaned everything")

    assert presets.resolve_by_id("integrity-test") is not None
    info2 = presets.load_any("integrity-test")
    presets.install(info2)
    assert presets.list_installed()[0]["id"] == "integrity-test"
    step(15, "Dev-link install by bare id works")

    # Phase 6: single-active + chain restore
    manifest2 = json.loads(json.dumps(manifest))
    manifest2["id"] = "integrity-b"
    manifest2["name"] = "Integrity B"
    manifest2["soul"]["agent_name"] = "IntegrityB"
    src2 = Path(tmp_home) / "src_b"
    src2.mkdir()
    (src2 / "preset.yaml").write_text(yaml.safe_dump(manifest2), encoding="utf-8")
    (src2 / "system_prompt.md").write_text("Second preset.\n", encoding="utf-8")
    (src2 / "README.md").write_text("# B\n", encoding="utf-8")
    (src2 / "skills").mkdir()
    (src2 / "skills" / "domain_tool.py").write_text(
        'DESCRIPTION="demo"\nTOOLS=[]\ndef execute(n,a): return ""\n',
        encoding="utf-8",
    )
    (src2 / "knowledge").mkdir()
    (src2 / "knowledge" / "ref.md").write_text("# B\n", encoding="utf-8")
    presets.install(presets.load_directory(src2))

    presets.activate("integrity-test")
    assert presets.get_active() == "integrity-test"
    presets.activate("integrity-b")
    assert presets.get_active() == "integrity-b"
    assert soul.load()["name"] == "IntegrityB"
    presets.deactivate()
    final = soul.load()
    assert final["name"] == "BaseAgent", f"chain restore broken: {final['name']}"
    step(16, "Single-active + chain restore")

    presets.uninstall("integrity-test")
    presets.uninstall("integrity-b")
    assert presets.list_installed() == []
    step(17, "Final cleanup — 0 presets installed")

    print()
    print("=== ALL 17 INTEGRITY CHECKS PASSED ===")
    return 0


if __name__ == "__main__":
    code = 0
    try:
        code = main()
    except AssertionError as e:
        print(f"\nFAIL: {e}")
        code = 1
    except Exception as e:
        import traceback
        traceback.print_exc()
        code = 1
    finally:
        shutil.rmtree(tmp_home, ignore_errors=True)
    sys.exit(code)
