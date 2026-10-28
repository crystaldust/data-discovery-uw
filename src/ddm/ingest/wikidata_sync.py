"""Journalist sync from Wikidata: one complete snapshot of the outlets' journalists per run.

Flow: take the journalist lock (shared with the byline job) -> refuse to start inside a
backoff -> mark crashed Wikidata runs failed -> register the run (the historical
baseline until a complete snapshot has succeeded, a refresh after) -> run every query ->
normalise -> in one transaction under the shared import lock write rejects, resolve new
people, attach new identifiers to known ones, upsert records, contact routes and soft
deletions, and metrics -> ANALYZE -> finish 'ok'.

All or nothing: if any query is refused or fails, nothing is written (a refusal only
records the backoff) and the run ends 'partial'. A missing outlet would otherwise drop an
employer from everyone who works there and look like a change. Idempotence comes from the
content-hash guard; the fingerprint includes the start time, so content that changes back
(A -> B -> A) is still written.
"""

import logging
import time
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

import psycopg

from ddm.identifiers import normalize_author_url
from ddm.ingest.backoff import check_backoff, record_refusal
from ddm.ingest.contacts import RouteRow, upsert_contact_routes
from ddm.ingest.records import NormalizedRecord, Reject, input_fingerprint
from ddm.ingest.resolve import (
    RESOLUTION_RULES,
    AttachmentPlan,
    Identifier,
    ResolutionPlan,
    apply_attachments,
    apply_resolution,
    find_ambiguous_identifiers,
    load_ambiguous_identifiers,
    load_entity_titles,
    load_identifier_index,
    plan_identifier_attachments,
    plan_resolution,
    write_ambiguous_identifiers,
)
from ddm.ingest.runs import (
    IMPORT_LOCK_KEY,
    RunHandle,
    RunMetrics,
    finish_run,
    register_run,
    source_id_of,
    write_metrics,
    write_rejects,
)
from ddm.ingest.tombstones import mark_missing_deleted
from ddm.ingest.upsert import upsert_records
from ddm.sources.podcast_rss import ContactRoute
from ddm.sources.podcastindex import SourceAccessDenied, SourceResponseError, SourceUnavailable
from ddm.sources.wikidata import (
    NORMALIZER_VERSION,
    OUTLETS,
    SOURCE_NAME,
    author_url,
    fetch_people,
    normalize_people,
)

logger = logging.getLogger(__name__)

# Session-level, shared by sync-journalists and sync-bylines: one journalist job at a time.
JOURNALIST_LOCK_KEY = 4_242_400
ENTITY_TYPE = "journalist"
AUTHOR_PAGE_KIND = "outlet_author_url"
ANALYZE_TABLES = "source_records, observations, entities, entity_identifiers, contact_routes"
WEBSITE_BASIS = "Wikidata P856 statement (crowd-sourced)"
AUTHOR_PAGE_BASIS = "Wikidata {prop} statement (crowd-sourced)"
WEBSITE_PURPOSE_BASIS = "personal website; no stated purpose"
AUTHOR_PAGE_PURPOSE_BASIS = "author page; no stated purpose"

KNOWN_ENTITIES = """
SELECT source_key, entity_id FROM source_records
WHERE source_id = %s AND entity_id IS NOT NULL AND source_key = ANY(%s)
"""
EMPLOYERS_ON_RECORD = """
SELECT DISTINCT e->>'outlet_qid'
FROM source_records s
CROSS JOIN LATERAL jsonb_array_elements(s.attrs->'employments') AS e
WHERE s.source_id = %s AND s.deleted_at IS NULL
"""
HAS_BASELINE = """
SELECT EXISTS (SELECT 1 FROM import_runs
               WHERE source_id = %s AND is_complete_snapshot AND status = 'ok')
"""


class JournalistInputError(ValueError):
    """The job was called in a way it cannot run (e.g. without autocommit)."""


class JournalistSyncBusy(RuntimeError):
    """Another journalist job holds the journalist lock."""


class SparqlClient(Protocol):
    requests: int

    def select(self, query: str, what: str) -> list[dict]: ...


@dataclass(frozen=True)
class JournalistSyncReport:
    run_id: int
    status: str  # ok | partial
    provenance: str  # historical | observed
    stopped: str | None  # None, refused or unavailable
    metrics: RunMetrics
    deletions_withheld: int = 0  # missing records not deleted (empty outlet, size guard)


def best_effort(action: Callable[[], object], what: str) -> None:
    """Cleanup that must not replace the error being raised (e.g. on a dropped connection)."""
    try:
        action()
    except psycopg.Error as exc:
        logger.warning("%s failed during cleanup error=%s", what, type(exc).__name__)


def _unlock(conn: psycopg.Connection) -> None:
    if conn.closed or conn.broken:  # a dropped connection has released its lock already
        return
    conn.execute("SELECT pg_advisory_unlock(%s)", (JOURNALIST_LOCK_KEY,))


@contextmanager
def journalist_lock(conn: psycopg.Connection) -> Iterator[None]:
    """Hold the session-level journalist lock; the connection must be autocommit."""
    if not conn.autocommit:
        # Fetching happens outside transactions and the lock must outlive the writes.
        raise JournalistInputError("journalist jobs need an autocommit connection")
    if not conn.execute("SELECT pg_try_advisory_lock(%s)", (JOURNALIST_LOCK_KEY,)).fetchone()[0]:
        raise JournalistSyncBusy("another journalist job is running")
    try:
        yield
    finally:
        best_effort(lambda: _unlock(conn), "journalist unlock")


def fail_stale_runs(conn: psycopg.Connection, source_id: int) -> int:
    """Runs of this source still 'running' were left by a crash; call under the lock."""
    with conn.transaction():
        return conn.execute(
            "UPDATE import_runs SET status = 'failed', finished_at = now()"
            " WHERE source_id = %s AND status = 'running'",
            (source_id,),
        ).rowcount


def _register(conn: psycopg.Connection, source_id: int, as_of: datetime, baseline: bool):
    params = {
        "normalizer_version": NORMALIZER_VERSION,
        "resolution_rules": RESOLUTION_RULES,
        "outlets": len(OUTLETS),
        "started_at": as_of.isoformat(),
    }
    run = register_run(
        conn,
        source_id=source_id,
        run_kind="historical_backfill" if baseline else "refresh",
        fingerprint=input_fingerprint(SOURCE_NAME, params, []),
        is_complete_snapshot=True,
        params=params,
    )
    if run.status != "running" or run.resumed:
        raise RuntimeError(f"journalist sync fingerprint collided with run {run.run_id}")
    return run


def _routes(record: NormalizedRecord) -> list[ContactRoute]:
    routes = [
        ContactRoute(
            "website", url, True, "unconfirmed", WEBSITE_BASIS, "unknown", WEBSITE_PURPOSE_BASIS
        )
        for url in record.attrs.get("websites", ())
    ]
    for prop, values in record.attrs.get("author_ids", {}).items():
        for value in values:
            page = normalize_author_url(author_url(prop, value))
            basis = AUTHOR_PAGE_BASIS.format(prop=prop)
            routes.append(
                ContactRoute(
                    "outlet_author_page",
                    f"https://{page}",
                    True,
                    "unconfirmed",
                    basis,
                    "unknown",
                    AUTHOR_PAGE_PURPOSE_BASIS,
                )
            )
    return routes


def _route_rows(records, entity_ids: dict[str, UUID], checked_at: datetime) -> list[RouteRow]:
    return [
        (entity_ids[r.source_key], r.source_key, route, checked_at)
        for r in records
        if r.source_key in entity_ids
        for route in _routes(r)
    ]


def _ambiguous_in(records: Sequence[NormalizedRecord]) -> dict[Identifier, int]:
    """Shared defaults, and any author page two people claim: Wikidata has duplicate items
    and bad edits, and a page that may be either person must not attach bylines to one."""
    found = find_ambiguous_identifiers(records)
    pages = Counter(i for r in records for i in set(r.identifiers) if i[0] == AUTHOR_PAGE_KIND)
    return {**found, **{page: n for page, n in pages.items() if n > 1}}


@dataclass(frozen=True)
class _Resolved:
    entity_ids: dict[str, UUID]
    plan: ResolutionPlan
    attached: AttachmentPlan
    attached_candidates: int
    ambiguous_found: int


def _resolve(conn, records: Sequence[NormalizedRecord], *, source_id, run_id, provenance):
    """New people get entities; known people gain new identifiers, never a re-merge."""
    rows = conn.execute(KNOWN_ENTITIES, (source_id, [r.source_key for r in records])).fetchall()
    known = dict(rows)
    found_ambiguous = _ambiguous_in(records)
    write_ambiguous_identifiers(conn, found_ambiguous, source_id=source_id, run_id=run_id)
    ambiguous = load_ambiguous_identifiers(conn)
    index = load_identifier_index(
        conn, (i for r in records for i in r.identifiers if i not in ambiguous)
    )
    fresh = [r for r in records if r.source_key not in known]
    titles = load_entity_titles(conn, index.values())
    plan = plan_resolution(fresh, index, SOURCE_NAME, ambiguous, titles)
    apply_resolution(
        conn,
        plan,
        source_id=source_id,
        run_id=run_id,
        entity_type=ENTITY_TYPE,
        first_seen_kind=provenance,
    )
    index.update({(kind, value): eid for kind, value, eid in plan.new_identifiers})
    attach = plan_identifier_attachments(
        [r for r in records if r.source_key in known], known, index, SOURCE_NAME, ambiguous
    )
    created = apply_attachments(conn, attach, source_id=source_id, run_id=run_id)
    entity_ids = {**known, **dict(plan.assignments)}
    return _Resolved(entity_ids, plan, attach, created, len(found_ambiguous))


def _missing_outlets(conn, source_id: int, seen: frozenset[str]) -> set[str]:
    """Outlets live records name that this snapshot returned nobody for."""
    rows = conn.execute(EMPLOYERS_ON_RECORD, (source_id,)).fetchall()
    return {outlet for (outlet,) in rows} - seen


def _write(
    conn: psycopg.Connection,
    records: Sequence[NormalizedRecord],
    rejects: Sequence[Reject],
    *,
    ctx: "_Context",
    outlets_seen: frozenset[str],
) -> tuple[RunMetrics, int]:
    """Everything one snapshot writes, in the caller's transaction; returns the metrics and
    how many deletions were withheld."""
    source_id, run_id = ctx.source_id, ctx.run.run_id
    write_rejects(conn, run_id, rejects)
    resolved = _resolve(
        conn, records, source_id=source_id, run_id=run_id, provenance=ctx.provenance
    )
    upserted = upsert_records(
        conn,
        source_id=source_id,
        run_id=run_id,
        provenance=ctx.provenance,
        records=records,
        entity_ids=resolved.entity_ids,
    )
    routes = _route_rows(records, resolved.entity_ids, ctx.as_of)
    upsert_contact_routes(conn, routes, source_id=source_id, run_id=run_id)
    # An outlet that came back empty more likely lost its query than all its people.
    if missing := _missing_outlets(conn, source_id, outlets_seen):
        logger.warning("outlets returned nobody: %s; deletions withheld", sorted(missing))
    tombstones = mark_missing_deleted(conn, source_id=source_id, run_id=run_id, allow=not missing)
    plan, attach = resolved.plan, resolved.attached
    return RunMetrics(
        rows_in=len(records) + len(rejects),
        rows_rejected=len(rejects),
        rows_new=len(upserted.new_keys),
        rows_changed=len(upserted.changed_keys),
        rows_unchanged=upserted.unchanged,
        entities_created=len(plan.new_entities),
        identifiers_added=len(plan.new_identifiers) + len(attach.new_identifiers),
        strong_id_merges=plan.strong_id_merges,
        candidates_created=len(plan.candidates) + resolved.attached_candidates,
        ambiguous_identifiers=resolved.ambiguous_found,
        rows_deleted=tombstones.deleted,
    ), tombstones.withheld


def _finish(
    conn,
    run: RunHandle,
    counted: RunMetrics,
    *,
    requests: int,
    started: float,
    monotonic: Callable[[], float],
    status: str,
) -> RunMetrics:
    if status == "ok":
        conn.execute(f"ANALYZE {ANALYZE_TABLES}")
    size = conn.execute("SELECT pg_database_size(current_database())").fetchone()[0]
    metrics = RunMetrics(
        **{
            **asdict(counted),
            "http_requests": requests,
            "duration_ms": int((monotonic() - started) * 1000),
            "db_size_bytes": size,
        }
    )
    with conn.transaction():
        write_metrics(conn, run.run_id, metrics)
    finish_run(conn, run.run_id, status)
    return metrics


@dataclass(frozen=True)
class _Context:
    source_id: int
    run: RunHandle
    as_of: datetime
    provenance: str
    refused_backoff: timedelta


def _snapshot(conn, client: SparqlClient, ctx: _Context):
    """(snapshot, None) or (None, why it stopped); a refusal records the backoff."""
    try:
        return fetch_people(client), None
    except SourceAccessDenied as exc:
        logger.warning("wikidata refused status=%d; nothing written", exc.status_code)
        until = record_refusal(
            conn,
            ctx.source_id,
            status=exc.status_code,
            retry_after_header=exc.retry_after,
            now=ctx.as_of,
            refused_backoff=ctx.refused_backoff,
            run_id=ctx.run.run_id,
        )
        logger.warning("wikidata backoff until=%s", until.isoformat())
        return None, "refused"
    except (SourceUnavailable, SourceResponseError) as exc:
        logger.warning("wikidata unavailable; nothing written error=%s", type(exc).__name__)
        return None, "unavailable"


def _sync(conn, client: SparqlClient, ctx: _Context, started, monotonic) -> JournalistSyncReport:
    snapshot, stopped = _snapshot(conn, client, ctx)
    finish = dict(requests=client.requests, started=started, monotonic=monotonic)
    if snapshot is None:
        metrics = _finish(conn, ctx.run, RunMetrics(), status="partial", **finish)
        return JournalistSyncReport(ctx.run.run_id, "partial", ctx.provenance, stopped, metrics)
    records, rejects = normalize_people(snapshot)
    seen = frozenset(r.outlet_qid for r in snapshot.employments)
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (IMPORT_LOCK_KEY,))
        counted, withheld = _write(conn, records, rejects, ctx=ctx, outlets_seen=seen)
        write_metrics(conn, ctx.run.run_id, counted)
    # Withheld deletions need a look: 'partial', so this run does not count as one of the
    # complete snapshots a later deletion relies on.
    status = "partial" if withheld else "ok"
    metrics = _finish(conn, ctx.run, counted, status=status, **finish)
    return JournalistSyncReport(ctx.run.run_id, status, ctx.provenance, None, metrics, withheld)


def sync_journalists(
    conn: psycopg.Connection,
    client: SparqlClient,
    *,
    refused_backoff: timedelta,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    monotonic: Callable[[], float] = time.monotonic,
) -> JournalistSyncReport:
    """One Wikidata snapshot of the outlets' journalists. Expects an autocommit connection."""
    with journalist_lock(conn):
        started, as_of = monotonic(), now()
        source_id = source_id_of(conn, SOURCE_NAME)
        check_backoff(conn, source_id, as_of)
        if stale := fail_stale_runs(conn, source_id):
            logger.warning("marked %d crashed journalist sync run(s) failed", stale)
        baseline = not conn.execute(HAS_BASELINE, (source_id,)).fetchone()[0]
        run = _register(conn, source_id, as_of, baseline)
        provenance = "historical" if baseline else "observed"
        logger.info("run_id=%d provenance=%s", run.run_id, provenance)
        ctx = _Context(source_id, run, as_of, provenance, refused_backoff)
        try:
            return _sync(conn, client, ctx, started, monotonic)
        except BaseException:  # KeyboardInterrupt too: the run must not stay 'running'
            best_effort(lambda: finish_run(conn, run.run_id, "failed"), f"run_id={run.run_id}")
            raise
