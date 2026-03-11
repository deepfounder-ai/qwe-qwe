"""Encrypted secrets vault — stores sensitive data in SQLite with Fernet encryption."""

import os
import base64
import hashlib
from cryptography.fernet import Fernet
import db
import logger

_log = logger.get("secrets")

# Key derived from machine-specific seed + DB file path
# Not military-grade, but keeps secrets encrypted at rest
_KEY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".vault_key")


def _get_fernet() -> Fernet:
    """Get or create Fernet cipher."""
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
    conn = db._get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS secrets (
            key TEXT PRIMARY KEY,
            value BLOB NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
    """)
    conn.commit()


def save(key: str, value: str) -> str:
    """Encrypt and store a secret."""
    import time
    _ensure_table()
    key = key.strip().lower()
    if not key:
        return "✗ Key required"

    f = _get_fernet()
    encrypted = f.encrypt(value.encode())
    now = time.time()

    conn = db._get_conn()
    conn.execute(
        "INSERT INTO secrets (key, value, created_at, updated_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=?, updated_at=?",
        (key, encrypted, now, now, encrypted, now)
    )
    conn.commit()
    logger.event("secret_saved", key=key)
    return f"✓ Secret '{key}' saved"


def get(key: str) -> str | None:
    """Decrypt and return a secret."""
    _ensure_table()
    key = key.strip().lower()
    conn = db._get_conn()
    row = conn.execute("SELECT value FROM secrets WHERE key=?", (key,)).fetchone()
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
    conn = db._get_conn()
    r = conn.execute("DELETE FROM secrets WHERE key=?", (key,))
    conn.commit()
    if r.rowcount:
        logger.event("secret_deleted", key=key)
        return f"✓ Secret '{key}' deleted"
    return f"✗ Secret '{key}' not found"


def list_keys() -> list[str]:
    """List all secret keys (not values!)."""
    _ensure_table()
    conn = db._get_conn()
    rows = conn.execute("SELECT key FROM secrets ORDER BY key").fetchall()
    return [r[0] for r in rows]
