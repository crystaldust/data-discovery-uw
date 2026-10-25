"""Upsert normalised records into source_records and append observations.

Only rows whose content hash changed (or that come back after a soft delete) are
written and observed; unchanged rows just get last_seen_run. Replaying a run writes
nothing new because observations are keyed by run and the upsert is hash-guarded.
Resolved entity ids are written with the row, and an existing entity id is never
replaced, so resolution does not cost a second rewrite of the table.
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from uuid import UUID

import psycopg
from psycopg.pq import TransactionStatus
from psycopg.types.json import Jsonb

from ddm.ingest.records import NormalizedRecord, content_hash, source_key_order

logger = logging.getLogger(__name__)

# Dropped first so several batches can be upserted in one transaction.
DROP_STAGING = "DROP TABLE IF EXISTS pg_temp.stg"
CREATE_STAGING = """
CREATE TEMP TABLE stg (
  source_key text PRIMARY KEY,
  entity_id uuid,
  display_name text,
  attrs jsonb,
  content_hash bytea,
  source_updated_at timestamptz
) ON COMMIT DROP
"""

# PostgreSQL 18: RETURNING old.* is NULL for inserted rows, which separates new from changed.
UPSERT_AND_OBSERVE = """
WITH up AS (
  INSERT INTO source_records AS t
    (source_id, source_key, entity_id, provenance, display_name, attrs, content_hash,
     source_updated_at, first_seen_run, last_seen_run, last_changed_run)
  SELECT %(sid)s, s.source_key, s.entity_id, %(prov)s, s.display_name, s.attrs, s.content_hash,
         s.source_updated_at, %(run)s, %(run)s, %(run)s
  FROM stg s
  ON CONFLICT (source_id, source_key) DO UPDATE
    SET entity_id = COALESCE(t.entity_id, excluded.entity_id),
        display_name = excluded.display_name,
        attrs = excluded.attrs,
        content_hash = excluded.content_hash,
        source_updated_at = excluded.source_updated_at,
        last_seen_run = excluded.last_seen_run,
        last_changed_run = excluded.last_changed_run,
        deleted_at = NULL,
        deleted_run = NULL
  WHERE t.content_hash IS DISTINCT FROM excluded.content_hash OR t.deleted_at IS NOT NULL
  RETURNING t.source_key, (old.source_key IS NULL) AS is_new
), obs AS (
  INSERT INTO observations (run_id, source_id, source_key, change_kind, provenance, payload,
                            content_hash)
  SELECT %(run)s, %(sid)s, up.source_key,
         CASE WHEN up.is_new THEN 'new' ELSE 'changed' END,
         %(prov)s,
         -- Unknown values are left out, and times are rendered in UTC whatever the session zone.
         jsonb_strip_nulls(jsonb_build_object(
           'display_name', s.display_name,
           'attrs', s.attrs,
           'source_updated_at',
           to_char(s.source_updated_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"+00:00"'))),
         s.content_hash
  FROM up JOIN stg s USING (source_key)
  ON CONFLICT DO NOTHING
)
SELECT source_key, is_new FROM up
"""

TOUCH_UNCHANGED = """
UPDATE source_records t
SET last_seen_run = %(run)s,
    entity_id = COALESCE(t.entity_id, s.entity_id)
FROM stg s
WHERE t.source_id = %(sid)s AND t.source_key = s.source_key AND t.last_seen_run <> %(run)s
"""


@dataclass(frozen=True)
class UpsertResult:
    new_keys: tuple[str, ...]
    changed_keys: tuple[str, ...]
    unchanged: int


def _check_unique_keys(records: Sequence[NormalizedRecord]) -> None:
    seen: set[str] = set()
    for record in records:
        if record.source_key in seen:
            raise ValueError(f"duplicate source_key in batch: {record.source_key}")
        seen.add(record.source_key)


def _stage(
    conn: psycopg.Connection,
    records: Sequence[NormalizedRecord],
    entity_ids: Mapping[str, UUID],
) -> None:
    conn.execute(DROP_STAGING)
    conn.execute(CREATE_STAGING)
    with (
        conn.cursor() as cur,
        cur.copy(
            "COPY stg (source_key, entity_id, display_name, attrs, content_hash,"
            " source_updated_at) FROM STDIN"
        ) as copy,
    ):
        copy.set_types(["text", "uuid", "text", "jsonb", "bytea", "timestamptz"])
        for r in records:
            copy.write_row(
                (
                    r.source_key,
                    entity_ids.get(r.source_key),
                    r.display_name,
                    Jsonb(dict(r.attrs)),
                    content_hash(r),
                    r.source_updated_at,
                )
            )
    # A freshly copied temp table has no statistics; without them the joins plan badly.
    conn.execute("ANALYZE stg")


def upsert_records(
    conn: psycopg.Connection,
    *,
    source_id: int,
    run_id: int,
    provenance: str,
    records: Sequence[NormalizedRecord],
    entity_ids: Mapping[str, UUID] | None = None,
) -> UpsertResult:
    """Must run inside the caller's transaction (the staging table drops on commit)."""
    if conn.info.transaction_status != TransactionStatus.INTRANS:
        raise RuntimeError("upsert_records must run inside a transaction")
    _check_unique_keys(records)
    if not records:
        return UpsertResult(new_keys=(), changed_keys=(), unchanged=0)
    _stage(conn, records, entity_ids or {})
    params = {"sid": source_id, "run": run_id, "prov": provenance}
    written = conn.execute(UPSERT_AND_OBSERVE, params).fetchall()
    unchanged = conn.execute(TOUCH_UNCHANGED, params).rowcount
    new_keys = tuple(sorted((k for k, is_new in written if is_new), key=source_key_order))
    changed_keys = tuple(sorted((k for k, is_new in written if not is_new), key=source_key_order))
    logger.info(
        "step=upsert new=%d changed=%d unchanged=%d", len(new_keys), len(changed_keys), unchanged
    )
    return UpsertResult(new_keys=new_keys, changed_keys=changed_keys, unchanged=unchanged)
