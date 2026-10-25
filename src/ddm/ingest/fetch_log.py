"""Refresh targets, the per-feed fetch log and mutable fetch state.

fetch_attempts is append-only: one row per feed per run, the audit trail of what was
requested and what came back. feed_fetch_state is the mutable side: the URL to request,
validators for conditional requests, back-off, and failure streaks. A failed fetch never
clears validators or last_ok_at, so one bad night does not lose what was known.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import psycopg

from ddm.sources.rss_fetch import FetchResult

OUTCOMES = frozenset({"fetched", "not_modified", "rejected", "failed", "skipped"})
VALIDATED = frozenset({"fetched", "not_modified"})
# The server answered normally; a rejected body still proves the feed is being served.
ANSWERED = frozenset({"fetched", "not_modified", "rejected"})


class RefreshInputError(ValueError):
    """The caller asked for something a refresh cannot do (bad limit, unknown run)."""


@dataclass(frozen=True)
class RefreshTarget:
    source_key: str
    entity_id: UUID
    url: str
    etag: str | None
    last_modified: str | None


@dataclass(frozen=True)
class Attempt:
    """One feed's outcome in a run: the fetch, or 'rejected' when its body did not parse."""

    source_key: str
    outcome: str
    reason: str | None
    fetch: FetchResult

    def __post_init__(self) -> None:
        if self.outcome not in OUTCOMES:
            raise ValueError(f"unknown fetch outcome {self.outcome}")


# Least recently attempted first, never-attempted before all; length-then-text is numeric
# order for digit keys without casting.
# Feeds backing off (Retry-After, refusals) are never selected, not even by --same-as.
TARGET_FILTER = """
  d.deleted_at IS NULL AND d.entity_id IS NOT NULL
  AND COALESCE(f.fetch_url, d.attrs->>'feed_url') IS NOT NULL
  AND (f.retry_after IS NULL OR f.retry_after <= %(now)s)
"""

SELECT_TARGETS = f"""
SELECT d.source_key, d.entity_id, COALESCE(f.fetch_url, d.attrs->>'feed_url'),
       f.etag, f.last_modified
FROM source_records d
LEFT JOIN feed_fetch_state f ON f.source_key = d.source_key
WHERE d.source_id = %(dump)s AND {TARGET_FILTER}
ORDER BY f.last_attempt_at ASC NULLS FIRST, length(d.source_key), d.source_key
LIMIT %(limit)s
"""  # noqa: S608 - TARGET_FILTER is a module constant

TARGETS_OF_RUN = f"""
SELECT d.source_key, d.entity_id, COALESCE(f.fetch_url, d.attrs->>'feed_url'),
       f.etag, f.last_modified
FROM fetch_attempts a
JOIN source_records d ON d.source_id = %(dump)s AND d.source_key = a.source_key
LEFT JOIN feed_fetch_state f ON f.source_key = a.source_key
WHERE a.run_id = %(run)s AND {TARGET_FILTER}
ORDER BY a.attempted_at, length(a.source_key), a.source_key
"""  # noqa: S608 - TARGET_FILTER is a module constant

UPSERT_STATE = """
INSERT INTO feed_fetch_state AS t
  (source_key, fetch_url, etag, last_modified, last_status, last_outcome, last_attempt_run,
   last_attempt_at, last_ok_at, last_fetched_run, consecutive_failures, retry_after)
VALUES (%(key)s, %(url)s, %(etag)s, %(last_modified)s, %(status)s, %(outcome)s, %(run)s,
        %(at)s, %(ok_at)s, %(fetched_run)s, %(failures)s, %(retry_after)s)
ON CONFLICT (source_key) DO UPDATE SET
  fetch_url = excluded.fetch_url,
  etag = CASE WHEN %(validated)s THEN excluded.etag ELSE t.etag END,
  last_modified = CASE WHEN %(validated)s THEN excluded.last_modified ELSE t.last_modified END,
  last_status = excluded.last_status,
  last_outcome = excluded.last_outcome,
  last_attempt_run = excluded.last_attempt_run,
  last_attempt_at = excluded.last_attempt_at,
  last_ok_at = COALESCE(excluded.last_ok_at, t.last_ok_at),
  last_fetched_run = COALESCE(excluded.last_fetched_run, t.last_fetched_run),
  consecutive_failures = CASE excluded.last_outcome
                           WHEN 'failed' THEN t.consecutive_failures + 1
                           WHEN 'skipped' THEN t.consecutive_failures
                           ELSE 0 END,
  retry_after = excluded.retry_after
"""


def _targets(rows) -> list[RefreshTarget]:
    return [RefreshTarget(*row) for row in rows]


def select_targets(
    conn: psycopg.Connection, *, dump_id: int, limit: int, now: datetime
) -> list[RefreshTarget]:
    rows = conn.execute(SELECT_TARGETS, {"dump": dump_id, "limit": limit, "now": now})
    return _targets(rows.fetchall())


def targets_of_run(
    conn: psycopg.Connection, run_id: int, *, dump_id: int, rss_id: int, now: datetime
) -> list[RefreshTarget]:
    """The feeds an earlier refresh attempted, in its order, minus any now backing off."""
    run = conn.execute(
        "SELECT source_id, run_kind FROM import_runs WHERE run_id = %s", (run_id,)
    ).fetchone()
    if run != (rss_id, "refresh"):
        raise RefreshInputError(f"run {run_id} is not a podcast refresh run")
    params = {"dump": dump_id, "run": run_id, "now": now}
    targets = _targets(conn.execute(TARGETS_OF_RUN, params).fetchall())
    if not targets:
        raise RefreshInputError(f"refresh run {run_id} has no fetch attempts to repeat")
    return targets


def _attempt_row(run_id: int, attempt: Attempt) -> tuple:
    f = attempt.fetch
    return (
        run_id,
        attempt.source_key,
        f.requested_url,
        attempt.outcome,
        attempt.reason,
        f.status,
        f.bytes_read,
        f.elapsed_ms,
        f.attempted_at,
    )


def write_attempts(conn: psycopg.Connection, run_id: int, attempts: Sequence[Attempt]) -> int:
    with (
        conn.cursor() as cur,
        cur.copy(
            "COPY fetch_attempts (run_id, source_key, url, outcome, reason, http_status, bytes,"
            " elapsed_ms, attempted_at) FROM STDIN"
        ) as copy,
    ):
        copy.set_types(
            ["int8", "text", "text", "text", "text", "int4", "int4", "int4", "timestamptz"]
        )
        for attempt in attempts:
            copy.write_row(_attempt_row(run_id, attempt))
    return len(attempts)


def _state_params(run_id: int, attempt: Attempt) -> dict:
    f = attempt.fetch
    validated = attempt.outcome in VALIDATED
    return {
        "key": attempt.source_key,
        # A permanent redirect to a URL that then failed must not replace a working address.
        "url": f.final_url if f.moved_permanently and validated else f.requested_url,
        "etag": f.etag if validated else None,
        "last_modified": f.last_modified if validated else None,
        "validated": validated,
        "status": f.status,
        "outcome": attempt.outcome,
        "run": run_id,
        "at": f.attempted_at,
        "ok_at": f.attempted_at if attempt.outcome in ANSWERED else None,
        "fetched_run": run_id if attempt.outcome == "fetched" else None,
        "failures": 1 if attempt.outcome == "failed" else 0,
        "retry_after": f.retry_after,
    }


def update_fetch_state(conn: psycopg.Connection, run_id: int, attempts: Sequence[Attempt]) -> None:
    with conn.cursor() as cur:
        cur.executemany(UPSERT_STATE, [_state_params(run_id, a) for a in attempts])


def fail_stale_runs(conn: psycopg.Connection, rss_id: int) -> int:
    """Refresh runs still 'running' were left by a crash; call only while holding the lock."""
    with conn.transaction():
        cur = conn.execute(
            """
            UPDATE import_runs SET status = 'failed', finished_at = now()
            WHERE source_id = %s AND run_kind = 'refresh' AND status = 'running'
            """,
            (rss_id,),
        )
        return cur.rowcount
