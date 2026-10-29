"""Shared fixtures: a fake Podcast Index dump and a disposable local Postgres."""

import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import psycopg
import pytest

LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

# Copied verbatim from `.schema podcasts` of the 2026-10-25 dump.
DUMP_DDL = """
CREATE TABLE podcasts (
    id INTEGER PRIMARY KEY,
    url TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    lastUpdate INTEGER,
    link TEXT NOT NULL,
    lastHttpStatus INTEGER,
    dead INTEGER,
    contentType TEXT NOT NULL,
    itunesId INTEGER,
    originalUrl TEXT NOT NULL,
    itunesAuthor TEXT NOT NULL,
    itunesOwnerName TEXT NOT NULL,
    explicit INTEGER,
    imageUrl TEXT NOT NULL,
    itunesType TEXT NOT NULL,
    generator TEXT NOT NULL,
    newestItemPubdate INTEGER,
    language TEXT NOT NULL,
    oldestItemPubdate INTEGER,
    episodeCount INTEGER,
    popularityScore INTEGER,
    priority INTEGER,
    createdOn INTEGER,
    updateFrequency INTEGER,
    chash TEXT NOT NULL,
    host TEXT NOT NULL,
    newestEnclosureUrl TEXT NOT NULL,
    podcastGuid TEXT NOT NULL,
    description TEXT NOT NULL,
    category1 TEXT NOT NULL,
    category2 TEXT NOT NULL,
    category3 TEXT NOT NULL,
    category4 TEXT NOT NULL,
    category5 TEXT NOT NULL,
    category6 TEXT NOT NULL,
    category7 TEXT NOT NULL,
    category8 TEXT NOT NULL,
    category9 TEXT NOT NULL,
    category10 TEXT NOT NULL,
    newestEnclosureDuration INTEGER,
    podcastId INTEGER,
    duplicateOf INTEGER
);
"""

# How the real dump fills unknowns: empty strings for text, 0 for integers.
DEFAULT_DUMP_ROW = {
    "title": "",
    "lastUpdate": 1690000000,
    "link": "",
    "lastHttpStatus": 200,
    "dead": 0,
    "contentType": "",
    "itunesId": 0,
    "originalUrl": "",
    "itunesAuthor": "",
    "itunesOwnerName": "",
    "explicit": 0,
    "imageUrl": "",
    "itunesType": "",
    "generator": "",
    "newestItemPubdate": 1690000000,
    "language": "en",
    "oldestItemPubdate": 1600000000,
    "episodeCount": 10,
    "popularityScore": 0,
    "priority": 0,
    "createdOn": 1650000000,
    "updateFrequency": 0,
    "chash": "",
    "host": "",
    "newestEnclosureUrl": "",
    "podcastGuid": "",
    "description": "",
    **{f"category{i}": "" for i in range(1, 11)},
    "category1": "marketing",
    "newestEnclosureDuration": 0,
    "duplicateOf": "",
}


def dump_row(row_id: int, **overrides) -> dict:
    """A dump row inside the demo slice unless overrides move it out."""
    return {
        **DEFAULT_DUMP_ROW,
        "id": row_id,
        "podcastId": row_id,
        "url": f"https://feeds.example.com/{row_id}.xml",
        "title": f"Show {row_id}",
        **overrides,
    }


def make_dump(directory: Path, rows: list[dict]) -> Path:
    path = directory / "podcastindex_feeds.db"
    conn = sqlite3.connect(path)
    try:
        conn.execute(DUMP_DDL)
        for row in rows:
            columns = ", ".join(row)
            placeholders = ", ".join("?" for _ in row)
            conn.execute(
                f"INSERT INTO podcasts ({columns}) VALUES ({placeholders})",  # noqa: S608
                tuple(row.values()),
            )
        conn.commit()
    finally:
        conn.close()
    return path


@pytest.fixture(scope="session")
def pg_url() -> str:
    url = os.environ.get("DDM_TEST_DATABASE_URL", "").strip()
    if not url:
        pytest.skip("DDM_TEST_DATABASE_URL not set; run `docker compose up -d db`")
    if urlsplit(url).hostname not in LOCAL_HOSTS:
        pytest.fail("DDM_TEST_DATABASE_URL must point at a local database; tests wipe it")
    return url


@pytest.fixture
def fresh_db(pg_url):
    with psycopg.connect(pg_url, autocommit=True) as admin:
        admin.execute("DROP SCHEMA public CASCADE")
        admin.execute("CREATE SCHEMA public")
    # Same mode as the CLI: autocommit, with explicit conn.transaction() blocks.
    with psycopg.connect(pg_url, autocommit=True) as conn:
        yield conn


# The read-only role the web app uses on Neon, recreated locally: cluster-wide, so it
# survives the schema wipe, while the schema grants and default privileges are redone
# after each wipe, before migrating, as on Neon.
READER_ROLE = "ddm_reader"
READER_PASSWORD = "ddm-reader-local-only"
ENSURE_READER = f"""DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{READER_ROLE}') THEN
    CREATE ROLE {READER_ROLE} LOGIN PASSWORD '{READER_PASSWORD}';
  ELSE
    ALTER ROLE {READER_ROLE} LOGIN PASSWORD '{READER_PASSWORD}';
  END IF;
END $$"""  # noqa: S608 - module constants only


@pytest.fixture
def reader_db(fresh_db):
    """The wiped test database with the read-only role set up; migrate after this."""
    fresh_db.execute(ENSURE_READER)
    fresh_db.execute(f"GRANT USAGE ON SCHEMA public TO {READER_ROLE}")
    fresh_db.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO {READER_ROLE}"
    )
    return fresh_db


@pytest.fixture
def reader_url(pg_url) -> str:
    """Same host and database as the test database, as the read-only role."""
    parts = urlsplit(pg_url)
    host = parts.hostname if ":" not in (parts.hostname or "") else f"[{parts.hostname}]"
    netloc = f"{READER_ROLE}:{READER_PASSWORD}@{host}" + (f":{parts.port}" if parts.port else "")
    return parts._replace(netloc=netloc).geturl()


@pytest.fixture
def fresh_minute():
    """Metering counts per clock minute: start a burst of calls early in a minute, so it
    stays inside one window. The local database and this process share a clock."""
    second = datetime.now().second
    if second > 45:
        time.sleep(61 - second)
