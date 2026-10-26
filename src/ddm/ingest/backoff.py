"""Backing off a source that refused us (401/403/429), across runs.

A refusal ends the run; this makes the next run wait as well instead of asking again at
once. The wait is the source's Retry-After when it sent one, otherwise a day for a rate
limit (429) and the configured refused backoff for 401/403. A run never starts before the
recorded time, and never with another identity.
"""

from datetime import datetime, timedelta

import psycopg

from ddm.sources.rss_fetch import RATE_LIMIT_BACKOFF, parse_retry_after

UPSERT_BACKOFF = """
INSERT INTO source_backoff (source_id, retry_after, http_status, set_run)
VALUES (%s, %s, %s, %s)
ON CONFLICT (source_id) DO UPDATE
  SET retry_after = excluded.retry_after, http_status = excluded.http_status,
      set_run = excluded.set_run
"""


class DiscoveryBackoff(RuntimeError):
    """The source refused an earlier run and asked us to wait."""


def check_backoff(conn: psycopg.Connection, source_id: int, now: datetime) -> None:
    row = conn.execute(
        "SELECT retry_after, http_status FROM source_backoff WHERE source_id = %s",
        (source_id,),
    ).fetchone()
    if row is not None and row[0] > now:
        raise DiscoveryBackoff(
            f"the source refused an earlier run (status {row[1]}); not asking again before "
            f"{row[0].isoformat()}"
        )


def record_refusal(
    conn: psycopg.Connection,
    source_id: int,
    *,
    status: int,
    retry_after_header: str | None,
    now: datetime,
    refused_backoff: timedelta,
    run_id: int,
) -> datetime:
    retry_after = parse_retry_after(retry_after_header, now)
    if retry_after is None:
        retry_after = now + (RATE_LIMIT_BACKOFF if status == 429 else refused_backoff)
    with conn.transaction():
        conn.execute(UPSERT_BACKOFF, (source_id, retry_after, status, run_id))
    return retry_after
