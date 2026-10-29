"""Records first seen by discovery runs, newest run first."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from ddm.query.filters import ENTITY_TYPES, PageRequest, choice
from ddm.query.scrub import scrub

CLASSIFICATIONS = ("new", "rename_or_move", "duplicate", "uncertain")
# Sources whose runs classify first-seen records: discover-dump, discover-newfeeds and
# sync-bylines. RSS refreshes and the Wikidata snapshot do not.
SOURCES = ("podcastindex_dump", "podcastindex_api", "outlet_rss")

LIST = """
SELECT d.run_id, r.finished_at AS run_at, s.name AS source, d.entity_id,
       e.display_name AS entity_name, e.entity_type, d.classification,
       d.matched_entity_id, m.display_name AS matched_name, d.method, d.score
FROM discoveries d
JOIN sources s USING (source_id)
JOIN import_runs r ON r.run_id = d.run_id
JOIN entities e ON e.entity_id = d.entity_id
LEFT JOIN entities m ON m.entity_id = d.matched_entity_id
WHERE (%(classification)s::text IS NULL OR d.classification = %(classification)s)
  AND (%(source)s::text IS NULL OR s.name = %(source)s)
  AND (%(entity_type)s::text IS NULL OR e.entity_type = %(entity_type)s)
ORDER BY d.run_id DESC, d.source_key
LIMIT %(limit)s OFFSET %(offset)s"""


@dataclass(frozen=True)
class DiscoveryRow:
    run_id: int
    run_at: datetime | None
    source: str
    entity_id: UUID
    entity_name: str | None
    entity_type: str
    classification: str
    matched_entity_id: UUID | None
    matched_name: str | None
    method: str
    score: float | None


@dataclass(frozen=True)
class DiscoveryPage:
    items: tuple[DiscoveryRow, ...]
    limit: int
    offset: int
    has_more: bool


def list_recent_discoveries(
    conn: psycopg.Connection,
    page: PageRequest,
    *,
    classification: Any = None,
    source: Any = None,
    entity_type: Any = None,
) -> DiscoveryPage:
    params = {
        "classification": choice("classification", classification, CLASSIFICATIONS),
        "source": choice("source", source, SOURCES),
        "entity_type": choice("entity_type", entity_type, ENTITY_TYPES),
        "limit": page.limit + 1,
        "offset": page.offset,
    }
    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(LIST, params).fetchall()
    items = tuple(DiscoveryRow(**scrub(r)) for r in rows[: page.limit])
    return DiscoveryPage(items, page.limit, page.offset, has_more=len(rows) > page.limit)
