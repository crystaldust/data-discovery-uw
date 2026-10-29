"""API keys for the REST API and MCP: random, shown once, stored as SHA-256 only.

Checking and metering go through two SECURITY DEFINER functions (migration 0006), so the
web app's read-only role never reads or writes the key tables itself. Keys are never
printed or logged; only key ids are.
"""

import hashlib
import logging
import secrets
from dataclasses import dataclass

import psycopg

logger = logging.getLogger(__name__)

KEY_PREFIX = "ddm_"
KEY_BYTES = 32
MAX_KEY_CHARS = 200
DEFAULT_RATE_LIMIT = 60
SURFACES = ("api", "mcp")


@dataclass(frozen=True)
class MeterResult:
    allowed: bool
    retry_after_seconds: int


def new_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(KEY_BYTES)


def hash_key(key: str) -> bytes:
    return hashlib.sha256(key.encode("utf-8")).digest()


def create_key(
    conn: psycopg.Connection, label: str, rate_limit: int = DEFAULT_RATE_LIMIT
) -> tuple[int, str]:
    """Owner connection. Returns (key_id, key); the key exists nowhere else afterwards."""
    key = new_key()
    key_id = conn.execute(
        "INSERT INTO api_keys (label, key_hash, rate_limit_per_minute) VALUES (%s, %s, %s) "
        "RETURNING key_id",
        (label, hash_key(key), rate_limit),
    ).fetchone()[0]
    logger.info("step=api_key created key_id=%d rate_limit=%d", key_id, rate_limit)
    return key_id, key


def revoke_key(conn: psycopg.Connection, key_id: int) -> bool:
    """Owner connection. False when there is no such live key."""
    row = conn.execute(
        "UPDATE api_keys SET revoked_at = now() WHERE key_id = %s AND revoked_at IS NULL "
        "RETURNING key_id",
        (key_id,),
    ).fetchone()
    return row is not None


def check_key(conn: psycopg.Connection, key: str | None) -> int | None:
    """The key's id when it is live; None otherwise. Implausible input skips the query."""
    if not key or len(key) > MAX_KEY_CHARS:
        return None
    return conn.execute("SELECT api_key_check(%s)", (hash_key(key),)).fetchone()[0]


def meter(conn: psycopg.Connection, key_id: int, surface: str, name: str) -> MeterResult:
    """Counts one call. Opens a read-write transaction because the web pool's sessions
    default to read-only; the role still cannot write anything but through api_meter."""
    with conn.transaction():
        conn.execute("SET TRANSACTION READ WRITE")
        allowed, retry_after = conn.execute(
            "SELECT allowed, retry_after_seconds FROM api_meter(%s, %s, %s)",
            (key_id, surface, name),
        ).fetchone()
    return MeterResult(allowed, retry_after)
