"""Contact routes published by the shows themselves.

A route is upserted when a fetched feed shows it, and touched (checked_run and checked_at
advanced) when the feed answers 304. Only routes stamped by the feed's last parsed fetch
(feed_fetch_state.last_fetched_run) are touched: a route missing from that body, including
when the body had no routes at all, keeps its old stamp, so a 304 never makes it look
freshly verified. Touching must run before this batch updates feed_fetch_state.
Deliverability stays 'unchecked'; checking it needs a paid service the employer has not
approved.
"""

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

import psycopg

from ddm.sources.podcast_rss import ContactRoute

PROVENANCE = "observed"

UPSERT_ROUTE = """
INSERT INTO contact_routes AS t
  (entity_id, route_type, value, syntax_valid, affiliation_status, affiliation_basis,
   purpose, purpose_basis, source_id, source_key, provenance, first_seen_run, checked_run,
   checked_at)
VALUES (%(entity_id)s, %(route_type)s, %(value)s, %(syntax_valid)s, %(affiliation_status)s,
        %(affiliation_basis)s, %(purpose)s, %(purpose_basis)s, %(sid)s, %(source_key)s,
        %(provenance)s, %(run)s, %(run)s, %(checked_at)s)
ON CONFLICT (entity_id, route_type, value, source_id) DO UPDATE
  SET syntax_valid = excluded.syntax_valid,
      affiliation_status = excluded.affiliation_status,
      affiliation_basis = excluded.affiliation_basis,
      purpose = excluded.purpose,
      purpose_basis = excluded.purpose_basis,
      source_key = excluded.source_key,
      checked_run = excluded.checked_run,
      checked_at = excluded.checked_at
  WHERE t.checked_run <> excluded.checked_run
"""

TOUCH_ROUTES = """
UPDATE contact_routes c
SET checked_run = %(run)s, checked_at = q.checked_at
FROM unnest(%(keys)s::text[], %(times)s::timestamptz[]) AS q(source_key, checked_at)
JOIN feed_fetch_state f ON f.source_key = q.source_key
WHERE c.source_id = %(sid)s AND c.source_key = q.source_key
  AND c.checked_run = f.last_fetched_run AND c.checked_run <> %(run)s
"""

RouteRow = tuple[UUID, str, ContactRoute, datetime]


def upsert_contact_routes(
    conn: psycopg.Connection, rows: Sequence[RouteRow], *, source_id: int, run_id: int
) -> int:
    """Rows are (entity_id, source_key, route, checked_at); returns how many were given."""
    params = [
        {
            "entity_id": entity_id,
            "route_type": route.route_type,
            "value": route.value,
            "syntax_valid": route.syntax_valid,
            "affiliation_status": route.affiliation_status,
            "affiliation_basis": route.affiliation_basis,
            "purpose": route.purpose,
            "purpose_basis": route.purpose_basis,
            "sid": source_id,
            "source_key": source_key,
            "provenance": PROVENANCE,
            "run": run_id,
            "checked_at": checked_at,
        }
        for entity_id, source_key, route, checked_at in rows
    ]
    with conn.cursor() as cur:
        cur.executemany(UPSERT_ROUTE, params)
    return len(params)


def touch_contact_routes(
    conn: psycopg.Connection,
    keys: Sequence[tuple[str, datetime]],
    *,
    source_id: int,
    run_id: int,
) -> int:
    """Re-verify the current routes of feeds that answered 304; returns rows touched."""
    if not keys:
        return 0
    params = {
        "run": run_id,
        "sid": source_id,
        "keys": [key for key, _ in keys],
        "times": [checked_at for _, checked_at in keys],
    }
    return conn.execute(TOUCH_ROUTES, params).rowcount
