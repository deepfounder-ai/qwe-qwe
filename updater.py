"""qwe-qwe updater — safe pull, migrate, restart.

Usage:
    import updater
    result = updater.perform_update(on_progress=print)
"""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import config
import logger

_log = logger.get("updater")

# Built-in skills tracked in git (from .gitignore exceptions)
BUILTIN_SKILLS = {"__init__.py", "weather.py", "notes.py", "timer.py",
                  "soul_editor.py", "skill_creator.py"}

BACKUP_DIR = config.BACKUPS_DIR
MAX_BACKUPS = 5


# ── Helpers ──

def _root() -> Path:
    """Project root (where .git lives)."""
    return Path(__file__).parent


def _git(*args, timeout: int = 30) -> subprocess.CompletedProcess:
    """Run a git command in project root."""
    return subprocess.run(
        ["git"] + list(args),
        cwd=_root(),
        capture_output=True, text=True, timeout=timeout,
    )


def _pip(*args, timeout: int = 120) -> subprocess.CompletedProcess:
    """Run pip in the current venv."""
    pip_path = Path(sys.executable).parent / "pip"
    return subprocess.run(
        [str(pip_path)] + list(args),
        cwd=_root(),
        capture_output=True, text=True, timeout=timeout,
    )


def _current_version() -> str:
    """Get current version from pyproject.toml (not cached metadata)."""
    try:
        import tomllib
        with open(_root() / "pyproject.toml", "rb") as f:
            return tomllib.load(f)["project"]["version"]
    except Exception:
        try:
            import importlib.metadata
            return importlib.metadata.version("qwe-qwe")
        except Exception:
            return "unknown"


# ── Check ──

def check() -> dict:
    """Check if an update is available.
    Returns {available, current, latest, commits_behind, error}."""
    current = _current_version()

    r = _git("fetch", "origin", "main", "--quiet")
    if r.returncode != 0:
        return {"available": False, "current": current, "latest": current,
                "commits_behind": 0, "error": f"git fetch failed: {r.stderr.strip()}"}

    # Count commits behind
    r = _git("rev-list", "--count", "HEAD..origin/main")
    behind = int(r.stdout.strip()) if r.returncode == 0 else 0

    # Get remote version from pyproject.toml
    r = _git("show", "origin/main:pyproject.toml")
    latest = current
    if r.returncode == 0:
        for line in r.stdout.splitlines():
            if line.strip().startswith("version"):
                latest = line.split("=", 1)[1].strip().strip('"').strip("'")
                break

    return {
        "available": behind > 0,
        "current": current,
        "latest": latest,
        "commits_behind": behind,
        "error": None,
    }


# ── Skill conflict detection ──

def detect_skill_conflicts() -> list[dict]:
    """Check if incoming update changes built-in skills that user may have modified."""
    conflicts = []
    skills_dir = _root() / "skills"

    # Check which built-in skills are changing in the update
    r = _git("diff", "--name-only", "HEAD..origin/main", "--", "skills/")
    if r.returncode != 0 or not r.stdout.strip():
        return conflicts

    changed_files = set(Path(f).name for f in r.stdout.strip().splitlines())

    for fname in changed_files:
        if fname in BUILTIN_SKILLS:
            # Check if user modified the built-in skill locally
            r2 = _git("diff", "--name-only", "HEAD", "--", f"skills/{fname}")
            if r2.returncode == 0 and r2.stdout.strip():
                conflicts.append({"file": fname, "type": "modified_locally"})

    # Check if new built-in skills clash with user-created skills
    r = _git("diff", "--name-only", "--diff-filter=A", "HEAD..origin/main", "--", "skills/")
    if r.returncode == 0 and r.stdout.strip():
        new_files = set(Path(f).name for f in r.stdout.strip().splitlines())
        for fname in new_files:
            user_file = skills_dir / fname
            if user_file.exists() and fname not in BUILTIN_SKILLS:
                conflicts.append({"file": fname, "type": "user_skill_overwritten"})

    return conflicts


# ── Backup ──

def backup_db() -> str | None:
    """Backup SQLite database. Returns backup path or None on error."""
    db_path = Path(config.DB_PATH)
    if not db_path.exists():
        return None

    BACKUP_DIR.mkdir(exist_ok=True)
    ts = int(time.time())
    backup_path = BACKUP_DIR / f"qwe_qwe.db.{ts}"

    try:
        shutil.copy2(db_path, backup_path)
        _log.info(f"database backed up to {backup_path}")

        # Rotate old backups
        backups = sorted(BACKUP_DIR.glob("qwe_qwe.db.*"), key=lambda f: f.stat().st_mtime)
        for old in backups[:-MAX_BACKUPS]:
            old.unlink(missing_ok=True)

        return str(backup_path)
    except Exception as e:
        _log.error(f"backup failed: {e}")
        return None


# ── Deps check ──

def _deps_changed() -> bool:
    """Check if pyproject.toml dependencies changed between HEAD and origin/main."""
    r_old = _git("show", "HEAD:pyproject.toml")
    r_new = _git("show", "origin/main:pyproject.toml")
    if r_old.returncode != 0 or r_new.returncode != 0:
        return True  # can't tell — assume changed

    def _extract_deps(text: str) -> set:
        in_deps = False
        deps = set()
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("dependencies"):
                in_deps = True
                continue
            if in_deps:
                if stripped == "]":
                    break
                dep = stripped.strip('",').strip()
                if dep:
                    deps.add(dep)
        return deps

    return _extract_deps(r_old.stdout) != _extract_deps(r_new.stdout)


# ── Pull ──

def pull_code() -> tuple[bool, str]:
    """Pull latest code. Returns (success, message)."""
    # Try fast-forward first (safest)
    r = _git("pull", "--ff-only", "origin", "main", timeout=60)
    if r.returncode == 0:
        return True, r.stdout.strip() or "Updated successfully"

    # ff-only failed — local diverged. Try rebase.
    _log.warning(f"ff-only failed: {r.stderr.strip()}, trying rebase")
    r = _git("pull", "--rebase", "origin", "main", timeout=60)
    if r.returncode == 0:
        return True, r.stdout.strip() or "Updated with rebase"

    # Don't force-reset — let the user resolve conflicts manually
    _log.warning(f"rebase failed: {r.stderr.strip()}")
    return False, (
        "Update failed: local changes conflict with remote. "
        "Run manually: git stash && git pull origin main && git stash pop"
    )


# ── Reinstall ──

def reinstall_deps() -> tuple[bool, str]:
    """Reinstall package + dependencies."""
    r = _pip("install", "-q", "-e", ".", timeout=180)
    if r.returncode == 0:
        return True, "Dependencies installed"
    return False, f"pip install failed: {r.stderr.strip()[:200]}"


# ── Migrations ──

def run_migrations():
    """Force DB migrations to run on next connection."""
    import db
    db._migrated = False
    # Force a new connection in this thread to trigger migrations
    if hasattr(db._local, "conn"):
        try:
            db._local.conn.close()
        except Exception:
            pass
        del db._local.conn
    db._get_conn()
    _log.info("migrations applied")


# ── Full update flow ──

def perform_update(on_progress=None) -> dict:
    """Run the full update pipeline.

    on_progress(step: str, status: str, detail: str) is called at each phase.
    Returns {success, old_version, new_version, steps, restart_needed, error}.
    """
    def emit(step, status, detail=""):
        _log.info(f"update: [{step}] {status} — {detail}")
        if on_progress:
            try:
                on_progress(step, status, detail)
            except Exception:
                pass

    steps = []
    old_version = _current_version()

    # ── 1. Preflight ──
    emit("preflight", "checking", "Verifying git repository...")

    if not (_root() / ".git").is_dir():
        emit("preflight", "error", "Not a git repository")
        return {"success": False, "error": "Not a git repository — update requires git installation",
                "old_version": old_version, "new_version": old_version, "steps": [], "restart_needed": False}

    # Check for uncommitted tracked changes
    r = _git("status", "--porcelain", "-uno")  # -uno: only tracked files
    if r.returncode == 0 and r.stdout.strip():
        # Stash local changes
        emit("preflight", "stashing", "Stashing local changes...")
        _git("stash", "push", "-m", f"qwe-qwe-update-{int(time.time())}")

    steps.append({"step": "preflight", "status": "ok"})
    emit("preflight", "ok", "Repository ready")

    # ── 2. Check for updates ──
    emit("fetch", "checking", "Checking for updates...")
    info = check()

    if info.get("error"):
        emit("fetch", "error", info["error"])
        return {"success": False, "error": info["error"],
                "old_version": old_version, "new_version": old_version, "steps": steps, "restart_needed": False}

    if not info["available"]:
        emit("fetch", "ok", f"Already up to date (v{old_version})")
        steps.append({"step": "fetch", "status": "ok", "detail": "Already up to date"})
        return {"success": True, "old_version": old_version, "new_version": old_version,
                "steps": steps, "restart_needed": False, "error": None}

    emit("fetch", "ok", f"v{info['current']} → v{info['latest']} ({info['commits_behind']} commits)")
    steps.append({"step": "fetch", "status": "ok", "detail": f"{info['commits_behind']} commits behind"})

    # ── 3. Skill conflicts ──
    emit("conflicts", "checking", "Checking skill conflicts...")
    conflicts = detect_skill_conflicts()
    if conflicts:
        names = ", ".join(c["file"] for c in conflicts)
        emit("conflicts", "warning", f"Conflicts: {names} (backed up)")
        # Backup conflicting user skills
        BACKUP_DIR.mkdir(exist_ok=True)
        ts = int(time.time())
        for c in conflicts:
            src = _root() / "skills" / c["file"]
            if src.exists():
                dst = BACKUP_DIR / f"{c['file']}.{ts}.bak"
                shutil.copy2(src, dst)
        steps.append({"step": "conflicts", "status": "warning", "detail": names})
    else:
        emit("conflicts", "ok", "No conflicts")
        steps.append({"step": "conflicts", "status": "ok"})

    # ── 4. Backup database ──
    emit("backup", "running", "Backing up database...")
    backup_path = backup_db()
    if backup_path:
        emit("backup", "ok", f"Saved to {backup_path}")
        steps.append({"step": "backup", "status": "ok", "detail": backup_path})
    else:
        emit("backup", "ok", "No database to backup")
        steps.append({"step": "backup", "status": "ok", "detail": "No DB"})

    # ── 5. Check if deps will change ──
    deps_need_update = _deps_changed()

    # ── 6. Pull code ──
    emit("pull", "running", "Pulling latest code...")
    ok, msg = pull_code()
    if not ok:
        emit("pull", "error", msg)
        steps.append({"step": "pull", "status": "error", "detail": msg})
        return {"success": False, "error": msg,
                "old_version": old_version, "new_version": old_version, "steps": steps, "restart_needed": False}

    emit("pull", "ok", msg)
    steps.append({"step": "pull", "status": "ok", "detail": msg})

    # ── 7. Reinstall (always — to update version metadata + deps) ──
    new_version_on_disk = _current_version()
    version_changed = new_version_on_disk != old_version
    if deps_need_update or version_changed:
        reason = "dependencies changed" if deps_need_update else f"version bumped to {new_version_on_disk}"
        emit("deps", "running", f"Reinstalling ({reason})...")
        ok, msg = reinstall_deps()
        status = "ok" if ok else "warning"
        emit("deps", status, msg)
        steps.append({"step": "deps", "status": status, "detail": msg})
    else:
        emit("deps", "skipped", "Dependencies unchanged")
        steps.append({"step": "deps", "status": "skipped"})

    # ── 8. Migrations ──
    emit("migrate", "running", "Running database migrations...")
    try:
        run_migrations()
        emit("migrate", "ok", "Migrations applied")
        steps.append({"step": "migrate", "status": "ok"})
    except Exception as e:
        emit("migrate", "warning", f"Migration note: {e}")
        steps.append({"step": "migrate", "status": "warning", "detail": str(e)})

    # ── Done ──
    new_version = _current_version()
    emit("done", "ok", f"Updated to v{new_version}")

    return {
        "success": True,
        "old_version": old_version,
        "new_version": new_version,
        "steps": steps,
        "restart_needed": True,
        "error": None,
    }


# ── Restart ──

def restart_process():
    """Replace current process with a fresh one (for server restart after update)."""
    _log.info("restarting process via os.execv")
    os.execv(sys.executable, [sys.executable] + sys.argv)
