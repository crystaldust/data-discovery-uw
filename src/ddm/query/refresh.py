"""Rebuild the search view after a job run (owner connection, outside a transaction)."""

import logging

import psycopg

logger = logging.getLogger(__name__)


def refresh_search(conn: psycopg.Connection) -> int:
    """Readers keep seeing the old rows until the new ones are ready. Returns the row count."""
    conn.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY entity_search")
    rows = conn.execute("SELECT count(*) FROM entity_search").fetchone()[0]
    logger.info("step=refresh_search rows=%d", rows)
    return rows
