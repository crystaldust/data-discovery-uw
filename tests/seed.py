"""Rows written straight into the tables, for read-side tests that need exact shapes
(several sources per entity, deleted records, observations, routes) without running jobs."""

import hashlib
import json
import uuid
from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

# The one address every leak test looks for in responses.
SECRET_EMAIL = "jane.secret@example.org"


def run(
    conn: psycopg.Connection,
    source_id: int,
    finished_at: str = "2026-10-27T00:00:00Z",
    kind: str = "refresh",
    status: str = "ok",
) -> int:
    return conn.execute(
        """INSERT INTO import_runs (source_id, run_kind, input_fingerprint,
             is_complete_snapshot, started_at, finished_at, status)
           VALUES (%s, %s, %s, false, %s::timestamptz - interval '1 minute', %s, %s)
           RETURNING run_id""",
        (source_id, kind, uuid.uuid4().hex, finished_at, finished_at, status),
    ).fetchone()[0]


def entity(
    conn: psycopg.Connection,
    entity_type: str,
    name: str | None,
    run_id: int,
    kind: str = "historical",
) -> uuid.UUID:
    entity_id = uuid.uuid4()
    conn.execute(
        """INSERT INTO entities (entity_id, entity_type, display_name, first_seen_kind,
             first_seen_run) VALUES (%s, %s, %s, %s, %s)""",
        (entity_id, entity_type, name, kind, run_id),
    )
    return entity_id


def _digest(value: Any) -> bytes:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).digest()


def record(
    conn: psycopg.Connection,
    source_id: int,
    key: str,
    entity_id: uuid.UUID | None,
    run_id: int,
    attrs: dict[str, Any],
    provenance: str = "historical",
    name: str | None = None,
    last_seen_run: int | None = None,
    deleted_run: int | None = None,
) -> None:
    conn.execute(
        """INSERT INTO source_records (source_id, source_key, entity_id, provenance,
             display_name, attrs, content_hash, first_seen_run, last_seen_run,
             last_changed_run, deleted_at, deleted_run)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                   CASE WHEN %s::bigint IS NULL THEN NULL ELSE now() END, %s)""",
        (
            source_id,
            key,
            entity_id,
            provenance,
            name,
            Jsonb(attrs),
            _digest(attrs),
            run_id,
            last_seen_run or run_id,
            last_seen_run or run_id,
            deleted_run,
            deleted_run,
        ),
    )


def observation(
    conn: psycopg.Connection,
    run_id: int,
    source_id: int,
    key: str,
    change_kind: str,
    payload: dict[str, Any] | None,
    provenance: str = "observed",
    observed_at: datetime | str | None = None,
) -> None:
    conn.execute(
        """INSERT INTO observations (run_id, source_id, source_key, change_kind, provenance,
             payload, content_hash, observed_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, coalesce(%s::timestamptz, now()))""",
        (
            run_id,
            source_id,
            key,
            change_kind,
            provenance,
            Jsonb(payload) if payload is not None else None,
            _digest(payload) if payload is not None else None,
            observed_at,
        ),
    )


def route(
    conn: psycopg.Connection,
    entity_id: uuid.UUID,
    route_type: str,
    value: str,
    run_id: int,
    source_id: int = 2,
    source_key: str = "k",
    affiliation: str = "unconfirmed",
    purpose: str = "unknown",
) -> None:
    conn.execute(
        """INSERT INTO contact_routes (entity_id, route_type, value, syntax_valid,
             affiliation_status, affiliation_basis, purpose, purpose_basis, source_id,
             source_key, provenance, first_seen_run, checked_run, checked_at)
           VALUES (%s, %s, %s, true, %s, 'feed', %s, 'none stated', %s, %s, 'observed',
                   %s, %s, now())""",
        (entity_id, route_type, value, affiliation, purpose, source_id, source_key, run_id, run_id),
    )


def identifier(
    conn: psycopg.Connection,
    kind: str,
    value: str,
    entity_id: uuid.UUID,
    source_id: int,
    run_id: int,
) -> None:
    conn.execute(
        """INSERT INTO entity_identifiers (id_kind, id_value, entity_id, source_id, added_run)
           VALUES (%s, %s, %s, %s, %s)""",
        (kind, value, entity_id, source_id, run_id),
    )


def discovery(
    conn: psycopg.Connection,
    run_id: int,
    source_id: int,
    key: str,
    classification: str,
    entity_id: uuid.UUID,
    matched: uuid.UUID | None = None,
    method: str = "none",
    score: float | None = None,
) -> None:
    conn.execute(
        """INSERT INTO discoveries (run_id, source_id, source_key, classification, entity_id,
             matched_entity_id, method, score, evidence, classifier_version)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, '{}', 1)""",
        (run_id, source_id, key, classification, entity_id, matched, method, score),
    )


def candidate(
    conn: psycopg.Connection,
    source_id: int,
    key: str,
    entity_id: uuid.UUID,
    run_id: int,
    score: float = 0.7,
    method: str = "title_trgm_v1",
) -> None:
    conn.execute(
        """INSERT INTO candidate_matches (source_id, source_key, entity_id, method, score,
             features, created_run) VALUES (%s, %s, %s, %s, %s, '{}', %s)""",
        (source_id, key, entity_id, method, score, run_id),
    )


def refresh(conn: psycopg.Connection) -> None:
    conn.execute("REFRESH MATERIALIZED VIEW entity_search")
