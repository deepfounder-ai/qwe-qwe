"""Business presets — install, activate, and manage domain-specific agent configs.

A preset is a `.qwp` archive (zip) or a directory containing:
  preset.yaml       — manifest with soul, skills, knowledge, compatibility
  system_prompt.md  — role/tone/domain instructions
  skills/*.py       — Python tool modules
  knowledge/*.md    — markdown knowledge base files

Lifecycle:
  load_archive / load_directory → validate → install → activate
  get_active / get_system_prompt_suffix / get_active_skills_dir are called
  by soul.py and skills/__init__.py on every prompt build.
  deactivate restores the soul backup. uninstall removes files + DB row.

State:
  ~/.qwe-qwe/presets/<id>/         — extracted preset contents
  DB:
    presets table                  — one row per installed preset
    kv.active_preset               — id of the currently active preset (or unset)
    kv.soul_backup                 — JSON snapshot of the soul traits before activation

Only one preset may be active at a time. Activating preset B while A is active
will deactivate A first.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import config
import db
import logger

_log = logger.get("presets")


# ── Schema locations ────────────────────────────────────────────────────

# The canonical schema ships with the main repo so validation never depends on
# the market repo being checked out. It mirrors the schema in qwe-qwe market.
_SCHEMA_PATH = Path(__file__).parent / "schemas" / "preset.schema.yaml"

_REQUIRED_FILES = ("preset.yaml",)

# Files we actively use from a preset — anything else is copied as-is but not
# touched. Skills / knowledge / system_prompt paths come from preset.yaml.


# ── Data classes ────────────────────────────────────────────────────────

@dataclass
class PresetInfo:
    """In-memory handle for a loaded (but not yet installed) preset."""
    id: str
    version: str
    name: str
    category: str
    author: dict
    license: dict
    manifest: dict
    source_dir: Path              # directory on disk containing preset.yaml
    source_kind: str = "directory"  # "archive" | "directory"
    origin_path: str | None = None  # original path user passed (archive or dir)


# ── YAML / schema helpers ───────────────────────────────────────────────

def _load_yaml(path: Path) -> dict:
    import yaml
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} must be a YAML mapping, got {type(data).__name__}")
    return data


def _load_schema() -> dict | None:
    """Load the JSON schema. Returns None if missing — validation then only
    checks required top-level keys and file existence."""
    if not _SCHEMA_PATH.exists():
        return None
    try:
        return _load_yaml(_SCHEMA_PATH)
    except Exception as e:
        _log.warning(f"failed to load preset schema: {e}")
        return None


# ── Loaders ─────────────────────────────────────────────────────────────

def load_directory(preset_dir: Path | str) -> PresetInfo:
    """Load a preset from an unpacked directory."""
    d = Path(preset_dir).expanduser().resolve()
    if not d.is_dir():
        raise FileNotFoundError(f"not a directory: {d}")
    manifest_path = d / "preset.yaml"
    if not manifest_path.exists():
        raise FileNotFoundError(f"preset.yaml not found in {d}")
    manifest = _load_yaml(manifest_path)
    return _info_from_manifest(manifest, source_dir=d, source_kind="directory",
                               origin_path=str(d))


def load_archive(archive_path: Path | str) -> PresetInfo:
    """Extract a .qwp / .zip archive to a temp dir and load it.

    Caller is responsible for letting install() copy the contents out before
    the temp dir is cleaned up (install() uses shutil.copytree which reads
    everything synchronously, so the temp dir can be removed after install()
    returns).
    """
    ap = Path(archive_path).expanduser().resolve()
    if not ap.is_file():
        raise FileNotFoundError(f"archive not found: {ap}")
    if not zipfile.is_zipfile(ap):
        raise ValueError(f"not a zip archive: {ap}")

    tmp = Path(tempfile.mkdtemp(prefix="qwe_preset_"))
    try:
        with zipfile.ZipFile(ap, "r") as zf:
            # Safety: block path traversal
            for member in zf.namelist():
                if member.startswith("/") or ".." in Path(member).parts:
                    raise ValueError(f"unsafe archive member: {member}")
            zf.extractall(tmp)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise

    # Some zips have a single root folder, some don't. Find the preset.yaml.
    manifest_path = _find_manifest(tmp)
    if not manifest_path:
        shutil.rmtree(tmp, ignore_errors=True)
        raise FileNotFoundError(f"preset.yaml not found inside archive {ap.name}")

    manifest = _load_yaml(manifest_path)
    return _info_from_manifest(manifest, source_dir=manifest_path.parent,
                               source_kind="archive", origin_path=str(ap))


def _find_manifest(root: Path) -> Path | None:
    direct = root / "preset.yaml"
    if direct.exists():
        return direct
    # Try one level deep (single wrapper folder)
    for child in root.iterdir():
        if child.is_dir() and (child / "preset.yaml").exists():
            return child / "preset.yaml"
    return None


def _info_from_manifest(manifest: dict, *, source_dir: Path, source_kind: str,
                        origin_path: str | None) -> PresetInfo:
    try:
        return PresetInfo(
            id=manifest["id"],
            version=manifest.get("version", "0.0.0"),
            name=manifest.get("name", manifest["id"]),
            category=manifest.get("category", "uncategorized"),
            author=manifest.get("author", {}),
            license=manifest.get("license", {"type": "free"}),
            manifest=manifest,
            source_dir=source_dir,
            source_kind=source_kind,
            origin_path=origin_path,
        )
    except KeyError as e:
        raise ValueError(f"preset.yaml missing required field: {e}")


# ── Dev-link resolver (QWE_MARKET_PATH) ─────────────────────────────────

def resolve_by_id(preset_id: str) -> Path | None:
    """If QWE_MARKET_PATH is set, search its presets/<category>/<id>/ tree."""
    market = os.environ.get("QWE_MARKET_PATH")
    if not market:
        return None
    root = Path(market).expanduser() / "presets"
    if not root.is_dir():
        return None
    # Exact match first
    for category_dir in root.iterdir():
        if not category_dir.is_dir():
            continue
        candidate = category_dir / preset_id
        if (candidate / "preset.yaml").exists():
            return candidate.resolve()
    return None


def load_any(source: str | Path) -> PresetInfo:
    """Load a preset from: archive path, directory path, or bare preset id
    (if QWE_MARKET_PATH is set)."""
    src = str(source)
    p = Path(src).expanduser()
    if p.is_dir():
        return load_directory(p)
    if p.is_file():
        return load_archive(p)
    # Bare id — dev-link lookup
    resolved = resolve_by_id(src)
    if resolved:
        return load_directory(resolved)
    raise FileNotFoundError(
        f"preset source not found: {src!r}. "
        f"Provide a path to a .qwp archive, a directory, or a preset id "
        f"(with QWE_MARKET_PATH set for the latter)."
    )


# ── Validation ──────────────────────────────────────────────────────────

def validate(info: PresetInfo) -> list[str]:
    """Return a list of validation errors; empty list means valid."""
    errors: list[str] = []
    manifest = info.manifest

    # 1. JSON schema (if jsonschema + schema file available)
    schema = _load_schema()
    if schema is not None:
        try:
            import jsonschema
            jsonschema.validate(manifest, schema)
        except ImportError:
            _log.debug("jsonschema not installed, skipping schema check")
        except Exception as e:  # ValidationError (+ subclasses)
            # Short, human-readable
            msg = str(e).splitlines()[0]
            errors.append(f"schema: {msg}")
    else:
        # Fallback: minimum required top-level keys
        for key in ("schema_version", "id", "name", "category", "version",
                    "author", "license", "soul", "system_prompt", "compatibility"):
            if key not in manifest:
                errors.append(f"schema: missing required field '{key}'")

    # 2. id / directory consistency
    if not re.match(r"^[a-z0-9]+(-[a-z0-9]+)*$", info.id):
        errors.append(f"id must be lowercase-kebab, got {info.id!r}")

    # 3. Referenced files exist
    src = info.source_dir
    sp = manifest.get("system_prompt") or {}
    if isinstance(sp, dict) and sp.get("path"):
        if not (src / sp["path"]).exists():
            errors.append(f"system_prompt.path not found: {sp['path']}")

    skills_block = manifest.get("skills") or {}
    for entry in (skills_block.get("custom") or []):
        pth = entry.get("path")
        if pth and not (src / pth).exists():
            errors.append(f"skills.custom path not found: {pth}")
        name = entry.get("name") or ""
        if name and not re.match(r"^[a-z_][a-z0-9_]*$", name):
            errors.append(f"skills.custom name invalid: {name!r}")

    for entry in (manifest.get("knowledge") or []):
        pth = entry.get("path")
        if pth and not (src / pth).exists():
            errors.append(f"knowledge path not found: {pth}")

    return errors


# ── Install / Uninstall ─────────────────────────────────────────────────

def preset_dir(preset_id: str) -> Path:
    return config.PRESETS_DIR / preset_id


def install(info: PresetInfo, *, overwrite: bool = False) -> dict:
    """Copy preset contents into ~/.qwe-qwe/presets/<id>/ and register it."""
    errors = validate(info)
    if errors:
        raise ValueError("preset validation failed:\n  - " + "\n  - ".join(errors))

    target = preset_dir(info.id)
    if target.exists():
        if not overwrite:
            raise FileExistsError(
                f"preset '{info.id}' is already installed. "
                f"Uninstall it first or pass overwrite=True."
            )
        # If it's the active preset, deactivate before overwriting
        if get_active() == info.id:
            deactivate()
        shutil.rmtree(target)

    # Copy source_dir → target
    shutil.copytree(info.source_dir, target)

    # Register in DB
    db.execute(
        """INSERT OR REPLACE INTO presets
           (id, version, name, category, author_name, license_type,
            manifest_json, installed_at, source_path)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            info.id,
            info.version,
            info.name,
            info.category,
            (info.author or {}).get("name", ""),
            (info.license or {}).get("type", "free"),
            json.dumps(info.manifest, ensure_ascii=False),
            time.time(),
            info.origin_path or str(info.source_dir),
        ),
    )

    # Clean up temp archive extraction dir (source_dir points inside it)
    if info.source_kind == "archive":
        _cleanup_temp(info.source_dir)

    _log.info(f"installed preset: {info.id} v{info.version} → {target}")
    return {
        "id": info.id,
        "version": info.version,
        "name": info.name,
        "category": info.category,
        "path": str(target),
    }


def _cleanup_temp(source_dir: Path) -> None:
    """Remove the tempdir created by load_archive, if present."""
    # source_dir may be tempdir itself or a child of it; walk up to find
    # the qwe_preset_ tempdir root.
    p = source_dir.resolve()
    for parent in [p, *p.parents]:
        if parent.name.startswith("qwe_preset_") and parent.parent == Path(tempfile.gettempdir()).resolve():
            shutil.rmtree(parent, ignore_errors=True)
            return


def uninstall(preset_id: str) -> None:
    """Remove a preset and all its side-effects."""
    if get_active() == preset_id:
        deactivate()

    # Clear indexed knowledge (by file_path in the preset's knowledge/ dir)
    try:
        import rag
        k_dir = preset_dir(preset_id) / "knowledge"
        if k_dir.exists():
            for f in k_dir.rglob("*"):
                if f.is_file():
                    rag._delete_file_chunks(str(f.resolve()))
    except Exception as e:
        _log.warning(f"uninstall: knowledge cleanup failed: {e}")

    # Remove on-disk contents
    d = preset_dir(preset_id)
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)

    # Remove DB row
    db.execute("DELETE FROM presets WHERE id = ?", (preset_id,))
    _log.info(f"uninstalled preset: {preset_id}")


# ── Registry queries ────────────────────────────────────────────────────

def list_installed() -> list[dict]:
    rows = db.fetchall(
        """SELECT id, version, name, category, author_name, license_type,
                  manifest_json, installed_at, source_path
           FROM presets ORDER BY installed_at DESC"""
    )
    active = get_active()
    result = []
    for r in rows:
        try:
            manifest = json.loads(r[6])
        except Exception:
            manifest = {}
        result.append({
            "id": r[0],
            "version": r[1],
            "name": r[2],
            "category": r[3],
            "author_name": r[4] or "",
            "license_type": r[5] or "free",
            "installed_at": r[7],
            "source_path": r[8] or "",
            "manifest": manifest,
            "active": r[0] == active,
        })
    return result


def get_info(preset_id: str) -> dict | None:
    for item in list_installed():
        if item["id"] == preset_id:
            return item
    return None


# ── Active preset + activation ──────────────────────────────────────────

def get_active() -> str | None:
    val = db.kv_get("active_preset")
    return val or None


def _soul_keys() -> list[str]:
    """All soul keys (builtins + custom) currently defined in soul.py."""
    import soul
    soul._load_custom_traits()
    return list(soul.DEFAULTS.keys())


def _snapshot_current_soul() -> dict:
    import soul
    return soul.load()


def _restore_soul(snapshot: dict) -> None:
    import soul
    for key, val in snapshot.items():
        try:
            soul.save(key, val)
        except Exception as e:
            _log.debug(f"soul restore: {key}={val} → {e}")


def _apply_soul_from_manifest(manifest: dict) -> None:
    import soul
    block = manifest.get("soul") or {}
    if not block:
        return
    # Name + language
    if "agent_name" in block:
        soul.save("name", block["agent_name"])
    if "language" in block:
        # Map ISO code to human label where possible — soul stores the label
        lang_map = {"ru": "Russian", "en": "English", "es": "Spanish",
                    "de": "German", "fr": "French", "zh": "Chinese",
                    "ja": "Japanese", "pt": "Portuguese", "it": "Italian"}
        label = lang_map.get(block["language"], block["language"])
        soul.save("language", label)
    # Traits
    for trait, level in (block.get("traits") or {}).items():
        try:
            soul.save(trait, level)
        except Exception as e:
            _log.debug(f"apply soul: {trait}={level} → {e}")
    # Custom traits — add them so they appear in DEFAULTS
    for ct in (block.get("custom_traits") or []):
        try:
            soul.add_trait(ct["name"], ct.get("description", ""), ct.get("description", ""),
                           ct.get("level", "moderate"))
        except Exception as e:
            _log.debug(f"apply custom trait {ct}: {e}")


def activate(preset_id: str) -> dict:
    """Back up current soul, apply preset soul/skills/prompt/knowledge."""
    info = get_info(preset_id)
    if not info:
        raise ValueError(f"preset not installed: {preset_id}")

    # Deactivate whatever is currently active (restores its soul backup)
    current = get_active()
    if current and current != preset_id:
        deactivate()

    # Back up current soul as JSON
    snapshot = _snapshot_current_soul()
    db.kv_set("soul_backup", json.dumps(snapshot, ensure_ascii=False))

    # Apply preset soul
    _apply_soul_from_manifest(info["manifest"])

    # Index knowledge files via RAG under preset:<id> tag
    _index_knowledge(preset_id, info["manifest"])

    # Mark active (last — so failures don't leave a dangling active marker)
    db.kv_set("active_preset", preset_id)
    _log.info(f"activated preset: {preset_id}")
    return {"id": preset_id, "name": info["name"]}


def deactivate() -> None:
    """Restore the soul backup and clear the active preset marker."""
    current = get_active()
    if not current:
        return
    raw = db.kv_get("soul_backup")
    if raw:
        try:
            snapshot = json.loads(raw)
            _restore_soul(snapshot)
        except Exception as e:
            _log.warning(f"deactivate: soul restore failed: {e}")
    # Clear markers
    db.kv_set("active_preset", "")
    db.execute("DELETE FROM kv WHERE key = ?", ("soul_backup",))
    _log.info(f"deactivated preset: {current}")


def _index_knowledge(preset_id: str, manifest: dict) -> None:
    """Index the preset's knowledge files via rag with a preset:<id> tag."""
    k_list = manifest.get("knowledge") or []
    if not k_list:
        return
    try:
        import rag
    except Exception as e:
        _log.warning(f"rag import failed, skipping knowledge index: {e}")
        return
    base = preset_dir(preset_id)
    tag = f"preset:{preset_id}"
    for entry in k_list:
        pth = entry.get("path")
        if not pth:
            continue
        full = (base / pth).resolve()
        if not full.exists():
            _log.debug(f"knowledge path missing: {full}")
            continue
        try:
            rag.index_file(str(full), tags=[tag])
        except Exception as e:
            _log.warning(f"index knowledge {full.name}: {e}")


# ── Hooks called by soul.py and skills/__init__.py ──────────────────────

def get_system_prompt_suffix() -> str:
    """Return the preset's system_prompt text to append to soul.to_prompt().
    Empty string when no preset is active."""
    pid = get_active()
    if not pid:
        return ""
    info = get_info(pid)
    if not info:
        return ""
    manifest = info["manifest"]
    sp = manifest.get("system_prompt") or {}
    # Inline text wins over path
    text = sp.get("text")
    if not text:
        pth = sp.get("path")
        if pth:
            full = preset_dir(pid) / pth
            if full.exists():
                try:
                    text = full.read_text(encoding="utf-8")
                except Exception as e:
                    _log.debug(f"read system_prompt {full}: {e}")
    return (text or "").strip()


def get_active_skills_dir() -> Path | None:
    """Return the directory containing the active preset's custom skills."""
    pid = get_active()
    if not pid:
        return None
    d = preset_dir(pid) / "skills"
    return d if d.exists() else None


def get_active_info() -> dict | None:
    pid = get_active()
    return get_info(pid) if pid else None
