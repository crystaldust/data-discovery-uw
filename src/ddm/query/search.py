"""Entity search and CSV export over the entity_search materialized view.

One fixed SQL text covers every filter combination: a filter whose parameter is NULL is
true. Only module constants are joined; user input is always a bound parameter.
"""

import csv
import io
from collections.abc import Iterator
from dataclasses import astuple, dataclass, fields
from datetime import UTC, datetime, timedelta
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from ddm.query.filters import PageRequest, SearchFilters
from ddm.query.scrub import scrub
from ddm.sources.podcast_rss import activity_status

EXPORT_BATCH_ROWS = 2000
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

COLUMNS = """entity_id, entity_type, display_name, language, categories, outlets,
  last_activity_at, activity_basis, last_checked_at, imported_at, contact_routes,
  first_seen_kind"""

WHERE = r"""
FROM entity_search
WHERE NOT all_deleted
  AND (%(entity_type)s::text IS NULL OR entity_type = %(entity_type)s)
  AND (%(language)s::text IS NULL OR language = %(language)s)
  AND (%(category)s::text IS NULL OR categories @> ARRAY[%(category)s::text])
  AND (%(outlet)s::text IS NULL
       OR EXISTS (SELECT 1 FROM unnest(outlets) o WHERE lower(o) = lower(%(outlet)s)))
  AND (%(since)s::timestamptz IS NULL OR last_activity_at >= %(since)s)
  AND (%(has_contact)s::boolean IS NULL OR (contact_routes > 0) = %(has_contact)s)
  AND (%(first_seen)s::text IS NULL OR first_seen_kind = %(first_seen)s)
  AND (%(q)s::text IS NULL
       OR norm_text(%(q)s) <%% norm_name
       OR norm_name LIKE '%%' || norm_text(%(q_like)s) || '%%' ESCAPE '\')
"""

BY_NAME = f"""SELECT {COLUMNS} {WHERE}
ORDER BY norm_text(%(q)s) <<-> norm_name, entity_id
LIMIT %(limit)s OFFSET %(offset)s"""  # noqa: S608 - only module constants are interpolated

BY_ACTIVITY = f"""SELECT {COLUMNS} {WHERE}
ORDER BY last_activity_at DESC NULLS LAST, entity_id
LIMIT %(limit)s OFFSET %(offset)s"""  # noqa: S608 - only module constants are interpolated

EXPORT = f"""SELECT {COLUMNS} {WHERE}
ORDER BY last_activity_at DESC NULLS LAST, entity_id"""  # noqa: S608 - module constants only


@dataclass(frozen=True)
class EntitySummary:
    entity_id: UUID
    entity_type: str
    display_name: str | None
    language: str | None
    categories: tuple[str, ...]
    outlets: tuple[str, ...]
    last_activity_at: datetime | None
    activity_status: str
    activity_basis: str | None  # 'observed' (fetched by us) or 'historical' (catalogue)
    last_checked_at: datetime | None
    imported_at: datetime | None
    contact_routes: int
    first_seen_kind: str


@dataclass(frozen=True)
class SearchPage:
    items: tuple[EntitySummary, ...]
    limit: int
    offset: int
    has_more: bool


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _params(filters: SearchFilters, now: datetime) -> dict:
    since = now - timedelta(days=filters.active_within_days) if filters.active_within_days else None
    return {
        "entity_type": filters.entity_type,
        "language": filters.language,
        "category": filters.category,
        "outlet": filters.outlet,
        "since": since,
        "has_contact": filters.has_contact,
        "first_seen": filters.first_seen,
        "q": filters.q,
        "q_like": _escape_like(filters.q) if filters.q else None,
    }


def summary_from_row(row: dict, now: datetime) -> EntitySummary:
    return EntitySummary(
        entity_id=row["entity_id"],
        entity_type=row["entity_type"],
        display_name=scrub(row["display_name"]),
        language=row["language"],
        categories=tuple(row["categories"] or ()),
        outlets=tuple(row["outlets"] or ()),
        last_activity_at=row["last_activity_at"],
        activity_status=activity_status(row["last_activity_at"], now),
        activity_basis=row["activity_basis"],
        last_checked_at=row["last_checked_at"],
        imported_at=row["imported_at"],
        contact_routes=row["contact_routes"],
        first_seen_kind=row["first_seen_kind"],
    )


def search_entities(
    conn: psycopg.Connection,
    filters: SearchFilters,
    page: PageRequest,
    now: datetime | None = None,
) -> SearchPage:
    now = now or datetime.now(UTC)
    params = {**_params(filters, now), "limit": page.limit + 1, "offset": page.offset}
    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(BY_NAME if filters.q else BY_ACTIVITY, params).fetchall()
    items = tuple(summary_from_row(r, now) for r in rows[: page.limit])
    return SearchPage(items, page.limit, page.offset, has_more=len(rows) > page.limit)


EXPORT_HEADER = tuple(f.name for f in fields(EntitySummary))


def _cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, tuple):
        text = "; ".join(value)
    elif isinstance(value, datetime):
        text = value.isoformat()
    else:
        text = str(value)
    # A spreadsheet would run a cell starting with these as a formula.
    return f"'{text}" if text.startswith(FORMULA_PREFIXES) else text


def _csv_text(rows: list[tuple]) -> str:
    buffer = io.StringIO()
    csv.writer(buffer).writerows(rows)
    return buffer.getvalue()


def iter_export_csv(
    conn: psycopg.Connection, filters: SearchFilters, now: datetime | None = None
) -> Iterator[str]:
    """Every matching row as CSV text, header first, read EXPORT_BATCH_ROWS at a time
    through a server-side cursor (valid only inside its transaction)."""
    now = now or datetime.now(UTC)
    yield _csv_text([EXPORT_HEADER])
    with conn.transaction(), conn.cursor(name="entity_export", row_factory=dict_row) as cur:
        cur.itersize = EXPORT_BATCH_ROWS
        cur.execute(EXPORT, _params(filters, now))
        while batch := cur.fetchmany(EXPORT_BATCH_ROWS):
            summaries = (summary_from_row(r, now) for r in batch)
            yield _csv_text([tuple(_cell(v) for v in astuple(s)) for s in summaries])
