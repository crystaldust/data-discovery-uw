"""Counts for the metrics page: everything here is computed from the database.

Accuracy is not: it comes from hand labels and lives in eval/report.md.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

import psycopg
from psycopg.rows import dict_row

RECENT_RUNS = 20

ENTITY_COUNTS = """
SELECT entity_type, first_seen_kind, count(*)::int AS entities
FROM entities GROUP BY 1, 2 ORDER BY 1, 2"""

# "Verified" means seen by a run that fetched the record itself, not a catalogue import.
FRESHNESS = """
SELECT s.name AS source, count(r.*)::int AS records,
       count(*) FILTER (WHERE r.provenance = 'observed'
                        AND l.finished_at >= %(now)s - interval '7 days')::int AS verified_7d,
       count(*) FILTER (WHERE r.provenance = 'observed'
                        AND l.finished_at >= %(now)s - interval '30 days')::int AS verified_30d
FROM sources s
JOIN source_records r ON r.source_id = s.source_id AND r.deleted_at IS NULL
JOIN import_runs l ON l.run_id = r.last_seen_run
GROUP BY s.source_id, s.name ORDER BY s.source_id"""

ROUTES = """
SELECT route_type, affiliation_status, purpose, count(*)::int AS routes
FROM contact_routes_public GROUP BY 1, 2, 3 ORDER BY 1, 2, 3"""

DISCOVERIES = """
SELECT s.name AS source, d.classification, count(*)::int AS records
FROM discoveries d JOIN sources s USING (source_id)
GROUP BY s.source_id, s.name, d.classification ORDER BY s.source_id, d.classification"""

PENDING = "SELECT count(*)::int FROM candidate_matches WHERE status = 'pending'"

RUNS = """
SELECT r.run_id, s.name AS source, r.run_kind, r.status, r.started_at, r.finished_at,
       m.rows_new, m.rows_changed, m.rows_deleted, m.http_requests, m.duration_ms
FROM import_runs r
JOIN sources s USING (source_id)
LEFT JOIN run_metrics m USING (run_id)
ORDER BY r.run_id DESC LIMIT %(runs)s"""

DATABASE = """
SELECT pg_database_size(current_database()) AS db_size_bytes,
       (SELECT max(refreshed_at) FROM entity_search) AS search_refreshed_at"""


@dataclass(frozen=True)
class EntityCount:
    entity_type: str
    first_seen_kind: str
    entities: int


@dataclass(frozen=True)
class Freshness:
    source: str
    records: int
    verified_7d: int
    verified_30d: int


@dataclass(frozen=True)
class RouteCount:
    route_type: str
    affiliation_status: str
    purpose: str
    routes: int


@dataclass(frozen=True)
class DiscoveryCount:
    source: str
    classification: str
    records: int


@dataclass(frozen=True)
class RunRow:
    run_id: int
    source: str
    run_kind: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    rows_new: int | None
    rows_changed: int | None
    rows_deleted: int | None
    http_requests: int | None
    duration_ms: int | None


@dataclass(frozen=True)
class Metrics:
    entities: tuple[EntityCount, ...]
    freshness: tuple[Freshness, ...]
    contact_routes: tuple[RouteCount, ...]
    discoveries: tuple[DiscoveryCount, ...]
    pending_candidates: int
    recent_runs: tuple[RunRow, ...]
    db_size_bytes: int
    search_refreshed_at: datetime | None


def get_metrics(conn: psycopg.Connection, now: datetime | None = None) -> Metrics:
    params = {"now": now or datetime.now(UTC), "runs": RECENT_RUNS}
    with conn.cursor(row_factory=dict_row) as cur:

        def rows(sql: str, kind: type) -> tuple:
            return tuple(kind(**r) for r in cur.execute(sql, params).fetchall())

        pending = cur.execute(PENDING).fetchone()["count"]
        database = cur.execute(DATABASE).fetchone()
        return Metrics(
            entities=rows(ENTITY_COUNTS, EntityCount),
            freshness=rows(FRESHNESS, Freshness),
            contact_routes=rows(ROUTES, RouteCount),
            discoveries=rows(DISCOVERIES, DiscoveryCount),
            pending_candidates=pending,
            recent_runs=rows(RUNS, RunRow),
            db_size_bytes=database["db_size_bytes"],
            search_refreshed_at=database["search_refreshed_at"],
        )
