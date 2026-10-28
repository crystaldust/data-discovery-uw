"""Soft deletion from complete snapshots, with a grace period.

Only a source whose run sees everything it publishes (a complete snapshot, e.g. a
Wikidata sync) can say a record is gone. One miss can be a glitch, so a record is
soft-deleted only when the last MISSED_SNAPSHOTS_BEFORE_DELETE complete, successful
snapshots (this run included) all lacked it. Deletion sets deleted_at / deleted_run and
appends a 'deleted' observation; the entity and every earlier observation stay. A record
that comes back is revived by upsert_records as a 'changed' row.

A snapshot that suddenly lacks many records more likely lost a query result than the
people, so a deletion larger than MIN_DELETE_GUARD and MAX_DELETE_SHARE of the live records
is withheld (counted, nothing deleted) for a person to look at.
"""

import logging
from dataclasses import dataclass

import psycopg
from psycopg.pq import TransactionStatus

logger = logging.getLogger(__name__)

MISSED_SNAPSHOTS_BEFORE_DELETE = 2
MAX_DELETE_SHARE = 0.05
MIN_DELETE_GUARD = 20

# This run is still 'running' when it calls this, hence "OR run_id = %(run)s".
RECENT = """
WITH recent AS (
  SELECT run_id FROM import_runs
  WHERE source_id = %(sid)s AND is_complete_snapshot
    AND (status = 'ok' OR run_id = %(run)s)
  ORDER BY run_id DESC
  LIMIT %(grace)s
)"""
MISSING = """
t.source_id = %(sid)s AND t.deleted_at IS NULL
AND (SELECT count(*) FROM recent) = %(grace)s
AND t.last_seen_run < (SELECT min(run_id) FROM recent)
"""
COUNT_MISSING = f"""{RECENT}
SELECT count(*) FILTER (WHERE {MISSING}), count(*) FILTER (WHERE t.deleted_at IS NULL)
FROM source_records t WHERE t.source_id = %(sid)s
"""  # noqa: S608 - only module constants are interpolated
MARK_DELETED = f"""{RECENT}, gone AS (
  UPDATE source_records t
  SET deleted_at = now(), deleted_run = %(run)s
  WHERE {MISSING}
  RETURNING t.source_key
)
INSERT INTO observations (run_id, source_id, source_key, change_kind, provenance, payload,
                          content_hash)
SELECT %(run)s, %(sid)s, source_key, 'deleted', 'observed', NULL, NULL FROM gone
ON CONFLICT DO NOTHING
RETURNING source_key
"""  # noqa: S608 - only module constants are interpolated


@dataclass(frozen=True)
class TombstoneResult:
    deleted: int
    withheld: int  # missing records left alone (guard, or the caller said not to delete)


def mark_missing_deleted(
    conn: psycopg.Connection, *, source_id: int, run_id: int, allow: bool = True
) -> TombstoneResult:
    """Soft-delete records absent from the last MISSED_SNAPSHOTS_BEFORE_DELETE complete
    snapshots (this one included). Call after this run's upsert, in its transaction.

    With allow=False, or past the size guard, nothing is deleted and the count is returned.
    """
    if conn.info.transaction_status != TransactionStatus.INTRANS:
        raise RuntimeError("mark_missing_deleted must run inside a transaction")
    params = {"sid": source_id, "run": run_id, "grace": MISSED_SNAPSHOTS_BEFORE_DELETE}
    missing, live = conn.execute(COUNT_MISSING, params).fetchone()
    if not allow or missing > max(MIN_DELETE_GUARD, MAX_DELETE_SHARE * live):
        if missing:
            logger.warning("step=tombstones withheld=%d live=%d allow=%s", missing, live, allow)
        return TombstoneResult(deleted=0, withheld=missing)
    deleted = len(conn.execute(MARK_DELETED, params).fetchall())
    logger.info("step=tombstones deleted=%d", deleted)
    return TombstoneResult(deleted=deleted, withheld=0)
