"""Encrypted secrets vault — stores sensitive data in SQLite with Fernet encryption."""

import os
import base64
import hashlib

try:
    from cryptography.fernet import Fernet
except ImportError:
    Fernet = None  # graceful degradation — checked in _get_fernet()

import db
import logger

_log = logger.get("secrets")

# Key stored in user data directory (not in git repo)
import config
_KEY_FILE = os.path.join(str(config.DATA_DIR), ".vault_key")

# Auto-migrate: if old key exists in project dir, copy to new location
_OLD_KEY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".vault_key")
if os.path.exists(_OLD_KEY_FILE) and not os.path.exists(_KEY_FILE):
    import shutil
    shutil.copy2(_OLD_KEY_FILE, _KEY_FILE)
    os.chmod(_KEY_FILE, 0o600)


def _get_fernet():
    """Get or create Fernet cipher."""
    if Fernet is None:
        raise ImportError(
            "Vault requires 'cryptography' package. "
            "Install it: pip install cryptography"
        )
    if os.path.exists(_KEY_FILE):
        with open(_KEY_FILE, "rb") as f:
            key = f.read()
    else:
        key = Fernet.generate_key()
        with open(_KEY_FILE, "wb") as f:
            f.write(key)
        os.chmod(_KEY_FILE, 0o600)
        _log.info("vault key created")
    return Fernet(key)


def _ensure_table():
    db.execute("""
        CREATE TABLE IF NOT EXISTS secrets (
            key TEXT PRIMARY KEY,
            value BLOB NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
    """)


def save(key: str, value: str) -> str:
    """Encrypt and store a secret."""
    import time
    _ensure_table()
    key = key.strip().lower()
    if not key:
        return "✗ Key required"

    f = _get_fernet()
    encrypted = f.encrypt(str(value).encode())
    now = time.time()

    db.execute(
        "INSERT INTO secrets (key, value, created_at, updated_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=?, updated_at=?",
        (key, encrypted, now, now, encrypted, now)
    )
    logger.event("secret_saved", key=key)
    return f"✓ Secret '{key}' saved"


def get(key: str) -> str | None:
    """Decrypt and return a secret."""
    _ensure_table()
    key = key.strip().lower()
    row = db.fetchone("SELECT value FROM secrets WHERE key=?", (key,))
    if not row:
        return None
    f = _get_fernet()
    try:
        return f.decrypt(row[0]).decode()
    except Exception as e:
        _log.error(f"failed to decrypt secret '{key}': {e}")
        return None


def delete(key: str) -> str:
    """Delete a secret."""
    _ensure_table()
    key = key.strip().lower()
    rowcount = db.execute("DELETE FROM secrets WHERE key=?", (key,))
    if rowcount:
        logger.event("secret_deleted", key=key)
        return f"✓ Secret '{key}' deleted"
    return f"✗ Secret '{key}' not found"


def list_keys() -> list[str]:
    """List all secret keys (not values!)."""
    _ensure_table()
    rows = db.fetchall("SELECT key FROM secrets ORDER BY key")
    return [r[0] for r in rows]
