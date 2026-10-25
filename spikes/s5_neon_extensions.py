"""Spike 5: Neon version, extensions, fuzzy-match functions and a read-only role.

Usage: uv run --env-file .env python spikes/s5_neon_extensions.py

Needs DATABASE_URL (direct, owner role); the pooler URL is derived from it.
Creates the extensions and the ddm_reader role, appends DATABASE_URL_READONLY
to .env without printing it, and drops the scratch table it creates.
"""

import os
import secrets
import sys
import time
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg import sql

from ddm.config import pooled_neon_url

EXTENSIONS = ("pg_trgm", "fuzzystrmatch", "unaccent")
READER_ROLE = "ddm_reader"
SCRATCH_TABLE = "spike_scratch"
CONNECT_TIMEOUT_SECONDS = 15


def require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        print(f"missing required environment variable {name}", file=sys.stderr)
        sys.exit(1)
    return value


def step(label: str, conn: psycopg.Connection, query: str) -> None:
    started = time.perf_counter()
    try:
        cursor = conn.execute(query)
        row = cursor.fetchone() if cursor.description else None
        value = row[0] if row else cursor.statusmessage
        print(f"| {label} | ok | {value} | {time.perf_counter() - started:.2f}s |")
    except psycopg.Error as exc:
        print(f"| {label} | FAILED | {type(exc).__name__}: {exc.diag.message_primary} | |")


def with_credentials(url: str, user: str, password: str) -> str:
    parts = urlsplit(url)
    host = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    return urlunsplit(parts._replace(netloc=f"{user}:{password}@{host}{port}"))


def ensure_reader(conn: psycopg.Connection, password: str) -> None:
    exists = conn.execute("select 1 from pg_roles where rolname = %s", (READER_ROLE,)).fetchone()
    action = "alter" if exists else "create"
    conn.execute(
        sql.SQL("{} role {} login password {}").format(
            sql.SQL(action), sql.Identifier(READER_ROLE), sql.Literal(password)
        )
    )
    database = conn.execute("select current_database()").fetchone()[0]
    conn.execute(
        sql.SQL("grant connect on database {} to {}").format(
            sql.Identifier(database), sql.Identifier(READER_ROLE)
        )
    )
    conn.execute(sql.SQL("grant usage on schema public to {}").format(sql.Identifier(READER_ROLE)))
    conn.execute(
        sql.SQL("alter default privileges in schema public grant select on tables to {}").format(
            sql.Identifier(READER_ROLE)
        )
    )


def check_reader_is_read_only(reader_url: str) -> None:
    with psycopg.connect(reader_url, connect_timeout=CONNECT_TIMEOUT_SECONDS) as conn:
        count = conn.execute(f"select count(*) from {SCRATCH_TABLE}").fetchone()[0]  # noqa: S608
        print(f"| reader can select | ok | {count} rows | |")
        conn.rollback()
        for label, statement in (
            ("reader insert rejected", f"insert into {SCRATCH_TABLE} values (2)"),  # noqa: S608
            ("reader create table rejected", "create table spike_reader_attempt (id int)"),
        ):
            try:
                conn.execute(statement)
                print(f"| {label} | FAILED: write succeeded | | |")
            except psycopg.errors.InsufficientPrivilege:
                print(f"| {label} | ok | permission denied | |")
            conn.rollback()


def main() -> int:
    direct_url = require("DATABASE_URL")
    pooled_url = pooled_neon_url(direct_url)
    reader_password = secrets.token_urlsafe(24)

    print("| step | result | value | time |\n|---|---|---|---|")
    with psycopg.connect(
        direct_url, autocommit=True, connect_timeout=CONNECT_TIMEOUT_SECONDS
    ) as conn:
        step("server version (direct)", conn, "show server_version")
        for extension in EXTENSIONS:
            step(
                f"create extension {extension}",
                conn,
                f"create extension if not exists {extension}",
            )
        step(
            "installed extensions",
            conn,
            "select string_agg(extname || ' ' || extversion, ', ') from pg_extension",
        )
        step("similarity", conn, "select similarity('The Marketing Show', 'Marketing Show, The')")
        step("word_similarity", conn, "select word_similarity('marketing', 'B2B Marketing Daily')")
        step("daitch_mokotoff", conn, "select daitch_mokotoff('Schwarzenegger')::text")
        step("levenshtein", conn, "select levenshtein('podcast', 'podcasts')")
        step("unaccent", conn, "select unaccent('Café Société')")
        conn.execute(f"create table if not exists {SCRATCH_TABLE} (id int)")
        conn.execute(f"insert into {SCRATCH_TABLE} values (1)")  # noqa: S608
        ensure_reader(conn, reader_password)
        conn.execute(
            sql.SQL("grant select on {} to {}").format(
                sql.Identifier(SCRATCH_TABLE), sql.Identifier(READER_ROLE)
            )
        )
        print(f"| reader role {READER_ROLE} | ok | created or password rotated | |")

    with psycopg.connect(pooled_url, connect_timeout=CONNECT_TIMEOUT_SECONDS) as conn:
        step("server version (pooled)", conn, "show server_version")

    reader_url = with_credentials(pooled_url, READER_ROLE, reader_password)
    try:
        check_reader_is_read_only(reader_url)
    finally:
        with psycopg.connect(
            direct_url, autocommit=True, connect_timeout=CONNECT_TIMEOUT_SECONDS
        ) as conn:
            conn.execute(f"drop table if exists {SCRATCH_TABLE}")

    with open(".env", "a", encoding="utf-8") as env_file:
        env_file.write(f"DATABASE_URL_READONLY={reader_url}\n")
    print("\nDATABASE_URL_READONLY appended to .env (value not printed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
