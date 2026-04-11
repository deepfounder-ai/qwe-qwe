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

# ── Security limits ─────────────────────────────────────────────────────

# Preset IDs must match this regex. Enforced on every public function that
# accepts an id so a crafted input can never leak into filesystem operations.
_ID_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

# Hard cap on the TOTAL uncompressed size of a .qwp archive's contents.
# Prevents zipbombs that would fill the disk when `extractall()` runs.
_MAX_EXTRACT_BYTES = 64 * 1024 * 1024  # 64 MB
_MAX_EXTRACT_FILES = 2000              # hard cap on file count per archive


def _ensure_id(preset_id: str) -> str:
    """Reject anything that isn't a clean lowercase-kebab id."""
    if not isinstance(preset_id, str) or not _ID_RE.match(preset_id):
        raise ValueError(
            f"invalid preset id {preset_id!r}: must be lowercase-kebab "
            f"(matches {_ID_RE.pattern})"
        )
    return preset_id


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

    Hardened against:
      * path traversal (absolute paths on any OS, `..` components, backslashes)
      * symlinks and hardlinks inside the archive
      * zip bombs (total uncompressed size + file count caps)

    Caller is responsible for calling `install()` (which cleans up the temp
    dir) OR `cleanup(info)` if validation fails before install.
    """
    ap = Path(archive_path).expanduser().resolve()
    if not ap.is_file():
        raise FileNotFoundError(f"archive not found: {ap}")
    if not zipfile.is_zipfile(ap):
        raise ValueError(f"not a zip archive: {ap}")

    tmp = Path(tempfile.mkdtemp(prefix="qwe_preset_"))
    tmp_resolved = tmp.resolve()
    try:
        with zipfile.ZipFile(ap, "r") as zf:
            _validate_zip_members(zf, tmp_resolved)
            # Extract one member at a time so we never touch anything whose
            # destination resolves outside the tempdir.
            for info in zf.infolist():
                if info.is_dir():
                    continue
                # Re-assert safety after normalisation
                dest = (tmp / info.filename).resolve()
                if not _is_within(dest, tmp_resolved):
                    raise ValueError(f"unsafe archive member: {info.filename}")
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info, "r") as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise

    manifest_path = _find_manifest(tmp)
    if not manifest_path:
        shutil.rmtree(tmp, ignore_errors=True)
        raise FileNotFoundError(f"preset.yaml not found inside archive {ap.name}")

    try:
        manifest = _load_yaml(manifest_path)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return _info_from_manifest(manifest, source_dir=manifest_path.parent,
                               source_kind="archive", origin_path=str(ap))


def _is_within(child: Path, parent: Path) -> bool:
    """True if `child` is equal to or inside `parent` (both resolved)."""
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _validate_zip_members(zf: "zipfile.ZipFile", tmp_resolved: Path) -> None:
    """Enforce path-traversal / symlink / zip-bomb rules on the archive."""
    total_uncompressed = 0
    file_count = 0
    for info in zf.infolist():
        name = info.filename
        # 1. Reject obvious absolute paths and parent refs — cross-platform.
        if not name or name.startswith(("/", "\\")):
            raise ValueError(f"unsafe archive member (absolute path): {name}")
        # Normalise backslashes so PurePosixPath logic works on Windows zips.
        posix_name = name.replace("\\", "/")
        parts = Path(posix_name).parts
        if any(part == ".." for part in parts):
            raise ValueError(f"unsafe archive member (parent ref): {name}")
        # Reject drive-letter or UNC paths baked into the filename.
        if len(posix_name) >= 2 and posix_name[1] == ":":
            raise ValueError(f"unsafe archive member (drive letter): {name}")
        # 2. Reject symlinks / hardlinks.
        # On unix ZIP, symlink entries have external_attr high bits set to 0xA.
        mode = info.external_attr >> 16
        if mode and (mode & 0o170000) == 0o120000:  # S_IFLNK
            raise ValueError(f"unsafe archive member (symlink): {name}")
        # 3. Zip-bomb guard — sum uncompressed size, cap file count.
        if info.is_dir():
            continue
        file_count += 1
        if file_count > _MAX_EXTRACT_FILES:
            raise ValueError(
                f"archive has too many files (>{_MAX_EXTRACT_FILES})"
            )
        total_uncompressed += int(info.file_size or 0)
        if total_uncompressed > _MAX_EXTRACT_BYTES:
            raise ValueError(
                f"archive uncompressed size exceeds "
                f"{_MAX_EXTRACT_BYTES // (1024 * 1024)} MB cap"
            )
        # 4. Final resolve — path must land inside tmp.
        dest = (tmp_resolved / posix_name).resolve()
        if not _is_within(dest, tmp_resolved):
            raise ValueError(f"unsafe archive member (escapes tempdir): {name}")


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
    if not _ID_RE.match(info.id):
        errors.append(f"id must be lowercase-kebab, got {info.id!r}")

    # 3. Referenced files exist AND are confined to the preset directory.
    src = info.source_dir
    try:
        src_resolved = src.resolve()
    except Exception:
        errors.append(f"source_dir cannot be resolved: {src}")
        return errors

    def _check_path(field: str, rel: str) -> None:
        """Every path in the manifest must stay under src_resolved."""
        if not rel:
            return
        if os.path.isabs(rel):
            errors.append(f"{field}: absolute paths not allowed ({rel!r})")
            return
        try:
            full = (src / rel).resolve()
        except Exception as e:
            errors.append(f"{field}: cannot resolve ({rel!r}): {e}")
            return
        if not _is_within(full, src_resolved):
            errors.append(f"{field}: path escapes preset dir ({rel!r})")
            return
        if not full.exists():
            errors.append(f"{field}: not found ({rel!r})")

    sp = manifest.get("system_prompt") or {}
    if isinstance(sp, dict) and sp.get("path"):
        _check_path("system_prompt.path", sp["path"])

    skills_block = manifest.get("skills") or {}
    for entry in (skills_block.get("custom") or []):
        pth = entry.get("path") or ""
        if pth:
            _check_path("skills.custom.path", pth)
        name = entry.get("name") or ""
        if name and not re.match(r"^[a-z_][a-z0-9_]*$", name):
            errors.append(f"skills.custom name invalid: {name!r}")

    for entry in (manifest.get("knowledge") or []):
        pth = entry.get("path") or ""
        if pth:
            _check_path("knowledge.path", pth)

    # 4. Validate skill files as real Python modules with the expected API.
    # This is what gates C2 (RCE by install): any `.py` under the preset
    # that lives in skills/custom must pass skills.validate_skill() before
    # it gets copied into ~/.qwe-qwe/presets/<id>/skills/ and exec'd later.
    # We do a basic syntax+shape check; we don't sandbox execution.
    try:
        import skills as _skills
    except Exception as e:
        _log.warning(f"skills module unavailable, cannot validate preset skills: {e}")
    else:
        for entry in (skills_block.get("custom") or []):
            pth = entry.get("path") or ""
            if not pth:
                continue
            full = (src / pth).resolve()
            if not full.exists() or not _is_within(full, src_resolved):
                continue  # already reported above
            try:
                is_valid, skill_errors = _skills.validate_skill(str(full))
            except Exception as e:
                errors.append(f"skills.custom {pth}: validation crashed — {e}")
                continue
            if not is_valid:
                for skill_err in skill_errors or ["invalid skill"]:
                    errors.append(f"skills.custom {pth}: {skill_err}")

    return errors


# ── Install / Uninstall ─────────────────────────────────────────────────

def preset_dir(preset_id: str) -> Path:
    """Get the on-disk directory for an installed preset.

    Raises ValueError if the id is not a valid lowercase-kebab slug.
    This is the single chokepoint for turning a user string into a
    filesystem path, so it MUST stay strict.
    """
    _ensure_id(preset_id)
    return config.PRESETS_DIR / preset_id


def install(info: PresetInfo, *, overwrite: bool = False) -> dict:
    """Copy preset contents into ~/.qwe-qwe/presets/<id>/ and register it.

    Always cleans up:
      * the source archive tempdir (if info was loaded via load_archive)
      * a partially-written target directory on any failure during copy
    """
    # Fail fast on bad id BEFORE touching the filesystem.
    _ensure_id(info.id)

    try:
        errors = validate(info)
        if errors:
            raise ValueError(
                "preset validation failed:\n  - " + "\n  - ".join(errors)
            )

        target = preset_dir(info.id)
        # Enforce that the target stays under PRESETS_DIR even if someone
        # crafts a funky id that slipped past the regex on an older version.
        if not _is_within(target.resolve().parent if target.exists() else target.parent.resolve(),
                          config.PRESETS_DIR.resolve()):
            raise ValueError(f"preset target {target} escapes PRESETS_DIR")

        if target.exists():
            if not overwrite:
                raise FileExistsError(
                    f"preset '{info.id}' is already installed. "
                    f"Uninstall it first or pass overwrite=True."
                )
            # If it's the active preset, deactivate before overwriting
            if get_active() == info.id:
                deactivate()
            shutil.rmtree(target, ignore_errors=True)

        # Copy source_dir → target — rollback on any failure so we don't
        # leave a half-copied preset for the next install attempt to trip on.
        try:
            shutil.copytree(info.source_dir, target)
        except Exception:
            shutil.rmtree(target, ignore_errors=True)
            raise

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

        _log.info(f"installed preset: {info.id} v{info.version} → {target}")
        return {
            "id": info.id,
            "version": info.version,
            "name": info.name,
            "category": info.category,
            "path": str(target),
        }
    finally:
        # Always clean up the archive tempdir — whether install succeeded,
        # failed validation, or raised mid-copy.
        if info.source_kind == "archive":
            _cleanup_temp(info.source_dir)


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
    """Remove a preset and all its side-effects.

    Idempotent: uninstalling a preset that isn't registered is a no-op
    (returns without touching the filesystem). This prevents API fuzzing
    from triggering a shutil.rmtree with a traversal-crafted id.
    """
    _ensure_id(preset_id)

    # Only proceed if this id actually exists in our registry.
    row = db.fetchone("SELECT id FROM presets WHERE id = ?", (preset_id,))
    if not row:
        _log.debug(f"uninstall no-op: {preset_id} not installed")
        return

    if get_active() == preset_id:
        deactivate()

    d = preset_dir(preset_id)
    # Belt-and-suspenders: even if _ensure_id is bypassed somehow, refuse
    # to delete anything outside PRESETS_DIR.
    try:
        d_resolved = d.resolve()
    except Exception:
        d_resolved = d
    if not _is_within(d_resolved, config.PRESETS_DIR.resolve()):
        _log.error(f"uninstall refused: {d} escapes PRESETS_DIR")
        return

    # Clear indexed knowledge before the dir disappears.
    try:
        import rag
        k_dir = d / "knowledge"
        if k_dir.exists():
            for f in k_dir.rglob("*"):
                if f.is_file():
                    rag._delete_file_chunks(str(f.resolve()))
    except Exception as e:
        _log.warning(f"uninstall: knowledge cleanup failed: {e}")

    # Remove on-disk contents
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
    # Custom traits — add them so they appear in DEFAULTS.
    # The preset schema only exposes a single `description` field for a
    # custom trait; soul.add_trait wants separate low/high polarity labels.
    # Use generic defaults so the trait still functions as a gradient, and
    # prepend the description to the high-pole label where possible.
    for ct in (block.get("custom_traits") or []):
        try:
            desc = (ct.get("description") or "").strip()
            high_label = desc if desc else f"very {ct['name']}"
            low_label = f"not {ct['name']}"
            soul.add_trait(
                ct["name"],
                low_label,
                high_label,
                ct.get("level", "moderate"),
            )
        except Exception as e:
            _log.debug(f"apply custom trait {ct}: {e}")


def activate(preset_id: str) -> dict:
    """Back up current soul, apply preset soul/skills/prompt/knowledge.

    If any step during soul application fails, the original soul is
    restored from the snapshot and the backup is cleared — leaving the
    system in a consistent state (as if activate() was never called).
    """
    _ensure_id(preset_id)
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

    # Apply preset soul — on failure, restore from snapshot so we never
    # leave the agent with a half-applied personality.
    try:
        _apply_soul_from_manifest(info["manifest"])
        # Index knowledge files via RAG under preset:<id> tag
        _index_knowledge(preset_id, info["manifest"])
    except Exception as e:
        _log.error(f"activate {preset_id} failed mid-application: {e}; rolling back")
        try:
            _restore_soul(snapshot)
        finally:
            db.execute("DELETE FROM kv WHERE key = ?", ("soul_backup",))
        raise

    # Mark active (last — so failures above never leave a dangling active marker)
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
    """Index the preset's knowledge files via rag with a preset:<id> tag.

    Every referenced path must already have been confirmed by validate()
    to resolve inside the preset dir — but we re-check here so future
    refactors don't accidentally drop the guard.
    """
    k_list = manifest.get("knowledge") or []
    if not k_list:
        return
    try:
        import rag
    except Exception as e:
        _log.warning(f"rag import failed, skipping knowledge index: {e}")
        return
    base = preset_dir(preset_id)
    base_resolved = base.resolve()
    tag = f"preset:{preset_id}"
    for entry in k_list:
        pth = entry.get("path")
        if not pth:
            continue
        try:
            full = (base / pth).resolve()
        except Exception:
            _log.warning(f"knowledge path unresolvable: {pth}")
            continue
        if not _is_within(full, base_resolved):
            _log.error(f"knowledge path escapes preset dir: {pth}")
            continue
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
    Empty string when no preset is active or the path escapes the preset dir.
    """
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
            base = preset_dir(pid)
            base_resolved = base.resolve()
            try:
                full = (base / pth).resolve()
            except Exception:
                return ""
            if not _is_within(full, base_resolved):
                _log.error(f"system_prompt.path escapes preset dir: {pth}")
                return ""
            if full.exists():
                try:
                    text = full.read_text(encoding="utf-8", errors="replace")
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
