"""One entity with everything known about it, from whitelisted fields only.

Observation payloads hold emails: they are compared here, key by key, and dropped. Only
the names of changed public fields leave this module. Every list has a fixed upper bound.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from ddm.query.fields import changed_fields, public_attrs
from ddm.query.filters import QueryInputError
from ddm.query.scrub import scrub
from ddm.query.search import COLUMNS, EntitySummary, summary_from_row

MAX_OBSERVATIONS = 50
MAX_DISCOVERIES = 20
MAX_CANDIDATES = 20

SUMMARY = f"SELECT {COLUMNS} FROM entity_search WHERE entity_id = %(id)s"  # noqa: S608

IDENTIFIERS = """
SELECT i.id_kind AS kind, i.id_value AS value, s.name AS source, i.added_run,
       r.finished_at AS added_at
FROM entity_identifiers i
JOIN sources s USING (source_id)
JOIN import_runs r ON r.run_id = i.added_run
WHERE i.entity_id = %(id)s
ORDER BY i.id_kind, i.id_value"""

RECORDS = """
SELECT r.source_id, s.name AS source, r.provenance, r.display_name, r.attrs,
       f.finished_at AS first_seen_at, l.finished_at AS last_seen_at, r.deleted_at
FROM source_records r
JOIN sources s USING (source_id)
JOIN import_runs f ON f.run_id = r.first_seen_run
JOIN import_runs l ON l.run_id = r.last_seen_run
WHERE r.entity_id = %(id)s
ORDER BY r.source_id, r.source_key"""

# Routes of a record that has since been deleted are not shown; the record itself is,
# marked as removed (history is kept, not erased).
ROUTES = """
SELECT c.route_type, c.display_value, c.syntax_valid, c.deliverability_status,
       c.affiliation_status, c.affiliation_basis, c.purpose, c.purpose_basis,
       s.name AS source, c.provenance, c.checked_at
FROM contact_routes_public c
JOIN sources s USING (source_id)
JOIN source_records r ON (r.source_id, r.source_key) = (c.source_id, c.source_key)
WHERE c.entity_id = %(id)s AND r.deleted_at IS NULL
ORDER BY c.route_type, c.route_id"""

# lag() runs over all of a record's observations before LIMIT, so the oldest shown row
# still has its predecessor to compare with.
OBSERVATIONS = f"""
SELECT * FROM (
  SELECT o.run_id, o.observed_at, o.source_id, s.name AS source, o.provenance,
         o.change_kind, o.payload,
         lag(o.payload) OVER (PARTITION BY o.source_id, o.source_key ORDER BY o.run_id)
           AS previous
  FROM observations o
  JOIN source_records r USING (source_id, source_key)
  JOIN sources s ON s.source_id = o.source_id
  WHERE r.entity_id = %(id)s
) x
ORDER BY observed_at DESC, run_id DESC
LIMIT {MAX_OBSERVATIONS}"""  # noqa: S608 - only module constants are interpolated

DISCOVERIES = f"""
SELECT d.run_id, r.finished_at AS run_at, s.name AS source, sr.display_name AS record_name,
       d.classification, d.method, d.score, d.entity_id, d.matched_entity_id,
       m.display_name AS matched_name
FROM discoveries d
JOIN sources s USING (source_id)
JOIN import_runs r ON r.run_id = d.run_id
LEFT JOIN source_records sr ON (sr.source_id, sr.source_key) = (d.source_id, d.source_key)
LEFT JOIN entities m ON m.entity_id = d.matched_entity_id
WHERE d.entity_id = %(id)s OR d.matched_entity_id = %(id)s
ORDER BY d.run_id DESC, d.source_key
LIMIT {MAX_DISCOVERIES}"""  # noqa: S608 - only module constants are interpolated

# A pending candidate links a record to an entity it may be. Shown on both sides: the
# entity the record belongs to, and the suggested entity.
CANDIDATES = f"""
SELECT s.name AS source, sr.display_name AS record_name, c.method, c.score,
       CASE WHEN c.entity_id = %(id)s THEN sr.entity_id ELSE c.entity_id END AS other_id,
       CASE WHEN c.entity_id = %(id)s THEN re.display_name ELSE te.display_name END
         AS other_name
FROM candidate_matches c
JOIN sources s USING (source_id)
LEFT JOIN source_records sr ON (sr.source_id, sr.source_key) = (c.source_id, c.source_key)
LEFT JOIN entities re ON re.entity_id = sr.entity_id
JOIN entities te ON te.entity_id = c.entity_id
WHERE c.status = 'pending' AND (c.entity_id = %(id)s OR sr.entity_id = %(id)s)
ORDER BY c.score DESC, c.candidate_id
LIMIT {MAX_CANDIDATES}"""  # noqa: S608 - only module constants are interpolated


@dataclass(frozen=True)
class Identifier:
    kind: str
    value: str
    source: str
    added_run: int
    added_at: datetime | None


@dataclass(frozen=True)
class SourceRecordView:
    source: str
    provenance: str
    display_name: str | None
    attrs: Mapping[str, Any]
    first_seen_at: datetime | None
    last_seen_at: datetime | None
    deleted_at: datetime | None


@dataclass(frozen=True)
class ContactRouteView:
    route_type: str
    display_value: str
    syntax_valid: bool
    deliverability_status: str
    affiliation_status: str
    affiliation_basis: str
    purpose: str
    purpose_basis: str
    source: str
    provenance: str
    checked_at: datetime


@dataclass(frozen=True)
class ObservationView:
    run_id: int
    observed_at: datetime
    source: str
    provenance: str
    change_kind: str
    changed_fields: tuple[str, ...]


@dataclass(frozen=True)
class DiscoveryView:
    run_id: int
    run_at: datetime | None
    source: str
    record_name: str | None
    classification: str
    method: str
    score: float | None
    entity_id: UUID
    matched_entity_id: UUID | None
    matched_name: str | None


@dataclass(frozen=True)
class CandidateView:
    source: str
    record_name: str | None
    method: str
    score: float
    other_id: UUID | None
    other_name: str | None


@dataclass(frozen=True)
class EntityDetail:
    summary: EntitySummary
    identifiers: tuple[Identifier, ...]
    records: tuple[SourceRecordView, ...]
    contact_routes: tuple[ContactRouteView, ...]
    observations: tuple[ObservationView, ...]
    discoveries: tuple[DiscoveryView, ...]
    candidates: tuple[CandidateView, ...]


def parse_entity_id(value: str) -> UUID:
    try:
        return UUID(value)
    except (ValueError, TypeError, AttributeError):
        raise QueryInputError("entity_id must be a UUID") from None


def _record(row: dict) -> SourceRecordView:
    return SourceRecordView(
        source=row["source"],
        provenance=row["provenance"],
        display_name=scrub(row["display_name"]),
        attrs=scrub(public_attrs(row["source_id"], row["attrs"])),
        first_seen_at=row["first_seen_at"],
        last_seen_at=row["last_seen_at"],
        deleted_at=row["deleted_at"],
    )


def _observation(row: dict) -> ObservationView:
    changed: tuple[str, ...] = ()
    if row["change_kind"] == "changed" and row["previous"] is not None:
        changed = changed_fields(row["source_id"], row["previous"], row["payload"])
    return ObservationView(
        run_id=row["run_id"],
        observed_at=row["observed_at"],
        source=row["source"],
        provenance=row["provenance"],
        change_kind=row["change_kind"],
        changed_fields=changed,
    )


def get_entity(
    conn: psycopg.Connection, entity_id: UUID, now: datetime | None = None
) -> EntityDetail | None:
    """None when the entity is unknown or not yet in the search view (refreshed after
    every job run)."""
    params = {"id": entity_id}
    with conn.cursor(row_factory=dict_row) as cur:
        summary = cur.execute(SUMMARY, params).fetchone()
        if summary is None:
            return None

        def rows(sql: str) -> list[dict]:
            return cur.execute(sql, params).fetchall()

        return EntityDetail(
            summary=summary_from_row(summary, now or datetime.now(UTC)),
            identifiers=tuple(Identifier(**scrub(r)) for r in rows(IDENTIFIERS)),
            records=tuple(_record(r) for r in rows(RECORDS)),
            contact_routes=tuple(ContactRouteView(**scrub(r)) for r in rows(ROUTES)),
            observations=tuple(_observation(r) for r in rows(OBSERVATIONS)),
            discoveries=tuple(DiscoveryView(**scrub(r)) for r in rows(DISCOVERIES)),
            candidates=tuple(CandidateView(**scrub(r)) for r in rows(CANDIDATES)),
        )
