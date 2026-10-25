"""Numbered plain-SQL migrations recorded in schema_migrations.

Each file runs whole (function bodies use $$, so it is never split on ';') in its own
transaction. An advisory lock serialises concurrent runners, and a checksum refuses to
continue if a file was edited after it was applied: add a new migration instead.
"""

import hashlib
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import psycopg

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
FILENAME_PATTERN = re.compile(r"(\d{4})_([a-z0-9_]+)\.sql")
MIGRATION_LOCK_KEY = 4_242_001
# The import upsert relies on PostgreSQL 18's RETURNING old.
MIN_SERVER_VERSION_NUM = 180000

CREATE_LEDGER = """
CREATE TABLE IF NOT EXISTS schema_migrations (
  version     integer PRIMARY KEY,
  name        text NOT NULL,
  checksum    text NOT NULL,
  applied_at  timestamptz NOT NULL DEFAULT now()
)
"""


class MigrationError(RuntimeError):
    """The migration files or the database are not in a state we can migrate safely."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str
    checksum: str


def discover(directory: Path = MIGRATIONS_DIR) -> tuple[Migration, ...]:
    migrations: dict[int, Migration] = {}
    for path in sorted(directory.iterdir()):
        if ".sql" not in path.name:
            continue
        match = FILENAME_PATTERN.fullmatch(path.name)
        if match is None:
            raise MigrationError(f"migration file name must look like 0001_name.sql: {path.name}")
        version = int(match.group(1))
        if version in migrations:
            raise MigrationError(f"duplicate migration version {version}")
        body = path.read_bytes()
        migrations[version] = Migration(
            version=version,
            name=match.group(2),
            sql=body.decode("utf-8"),
            checksum=hashlib.sha256(body).hexdigest(),
        )
    return tuple(migrations[v] for v in sorted(migrations))


def _check_server_version(conn: psycopg.Connection) -> None:
    version = conn.info.server_version
    if version < MIN_SERVER_VERSION_NUM:
        raise MigrationError(f"PostgreSQL 18 or newer is required, server is {version}")


def _applied_checksums(conn: psycopg.Connection) -> dict[int, str]:
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK_KEY,))
        conn.execute(CREATE_LEDGER)
        rows = conn.execute("SELECT version, checksum FROM schema_migrations").fetchall()
    return dict(rows)


def _verify_history(migrations: Sequence[Migration], applied: dict[int, str]) -> None:
    """Applied files must still exist unchanged, and new files must come after them."""
    by_version = {m.version: m for m in migrations}
    for version, checksum in applied.items():
        migration = by_version.get(version)
        if migration is None:
            raise MigrationError(f"applied migration {version:04d} is missing")
        if migration.checksum != checksum:
            raise MigrationError(f"migration {version:04d} was modified after being applied")
    latest = max(applied, default=0)
    for migration in migrations:
        if migration.version not in applied and migration.version < latest:
            raise MigrationError(
                f"migration {migration.version:04d} is older than applied version {latest:04d}"
            )


def _apply(conn: psycopg.Connection, migration: Migration) -> bool:
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK_KEY,))
        already = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version = %s", (migration.version,)
        ).fetchone()
        if already:
            return False
        # No parameters: psycopg uses the simple protocol, which runs every statement in the file.
        conn.execute(migration.sql.encode("utf-8"))
        conn.execute(
            "INSERT INTO schema_migrations (version, name, checksum) VALUES (%s, %s, %s)",
            (migration.version, migration.name, migration.checksum),
        )
    logger.info("applied version=%d name=%s", migration.version, migration.name)
    return True


def migrate(
    conn: psycopg.Connection, migrations: Sequence[Migration] | None = None
) -> tuple[Migration, ...]:
    """Apply pending migrations in order and return the ones applied by this call."""
    pending_source = discover() if migrations is None else tuple(migrations)
    _check_server_version(conn)
    applied = _applied_checksums(conn)
    _verify_history(pending_source, applied)
    pending = [m for m in pending_source if m.version not in applied]
    return tuple(m for m in pending if _apply(conn, m))
