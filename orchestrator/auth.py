"""
API key authentication for the orchestrator.

Stores keys in auth.db. Generates a master key on first startup.
Local dev bypass: if CDMAD_LOCAL_DEV=1, auth is skipped.
"""
from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3

AUTH_DB_PATH = os.path.join(os.path.dirname(__file__), "auth.db")


def _connect(db_path: str | None = None) -> sqlite3.Connection:
    return sqlite3.connect(db_path or AUTH_DB_PATH)


def init_auth_db(db_path: str | None = None) -> None:
    conn = _connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS api_keys (
            key_hash TEXT PRIMARY KEY,
            label TEXT,
            created_at REAL
        )
    """)
    conn.commit()
    conn.close()


def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def generate_api_key(label: str = "master", db_path: str | None = None) -> str:
    """Generate a new API key, store its hash, return the plaintext key."""
    import time

    key = secrets.token_hex(32)
    conn = _connect(db_path)
    conn.execute(
        "INSERT INTO api_keys (key_hash, label, created_at) VALUES (?, ?, ?)",
        (_hash_key(key), label, time.time()),
    )
    conn.commit()
    conn.close()
    return key


def validate_key(key: str, db_path: str | None = None) -> bool:
    """Check if an API key is valid."""
    conn = _connect(db_path)
    row = conn.execute(
        "SELECT 1 FROM api_keys WHERE key_hash = ?",
        (_hash_key(key),),
    ).fetchone()
    conn.close()
    return row is not None


def ensure_master_key(db_path: str | None = None) -> str | None:
    """On first startup, generate a master key if none exists. Returns the key or None."""
    conn = _connect(db_path)
    row = conn.execute(
        "SELECT 1 FROM api_keys WHERE label = 'master'"
    ).fetchone()
    conn.close()
    if row is not None:
        return None
    key = generate_api_key("master", db_path)
    return key


def is_local_dev_mode() -> bool:
    """Check if local dev bypass is active."""
    return os.environ.get("CDMAD_LOCAL_DEV", "0") == "1"
