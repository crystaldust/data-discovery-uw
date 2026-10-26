"""Incremental discovery from Podcast Index's newly added feeds.

Flow: take the discovery lock (one run at a time) -> mark crashed runs failed -> start
after the cursor (first run: at the newest feed id the dump holds, a deliberate overlap
that classifies as duplicate) -> register a new run -> page /recent/newfeeds in id order:
stop at a listing too young to have been parsed, or at `limit` English feeds; skip other
languages and dead feeds without a lookup; look up the rest, normalise, keep those inside
the demo slice -> every BATCH_SIZE feeds or MAX_BATCH_SECONDS, one transaction under the
import lock writes rejects, ambiguous identifiers, discover_batch, the cursor and
cumulative metrics -> ANALYZE -> 'ok', or 'partial' when the source refused or failed.

A refusal (401/403/429) ends the run at once, is recorded in source_backoff, and later
runs wait until it expires; it is never retried with another identity. One feed whose
lookup fails is rejected as 'lookup_failed' and passed, so it cannot wedge the cursor;
MAX_CONSECUTIVE_FAILURES in a row mean the source is down, and the run stops before them.
The cursor only moves past feeds whose batch committed, so the next run picks up exactly
where this one stopped. Ambiguity is counted over identifiers already known to be
ambiguous plus this batch: unlike the dump, the API never shows the whole population.
"""

import logging
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import psycopg

from ddm.ingest.backoff import check_backoff, record_refusal
from ddm.ingest.discover_dump import DiscoveryInputError, metrics_of
from ddm.ingest.discovery import discover_batch
from ddm.ingest.fuzzy import FUZZY_THRESHOLD
from ddm.ingest.records import (
    NormalizedRecord,
    Reject,
    fingerprint_item,
    input_fingerprint,
    make_reject,
    safe_raw,
)
from ddm.ingest.resolve import (
    RESOLUTION_RULES,
    find_ambiguous_identifiers,
    load_ambiguous_identifiers,
    write_ambiguous_identifiers,
)
from ddm.ingest.runs import (
    IMPORT_LOCK_KEY,
    RunMetrics,
    add_metrics,
    append_rejects,
    finish_run,
    register_run,
    source_id_of,
    write_metrics,
)
from ddm.sources.podcastindex import SourceAccessDenied, SourceResponseError, SourceUnavailable
from ddm.sources.podcastindex_api import (
    NORMALIZER_VERSION,
    SOURCE_NAME,
    fetch_feed,
    has_known_age,
    in_demo_slice,
    is_english,
    is_settled,
    iter_newfeeds,
    normalize_api_feed,
)
from ddm.sources.podcastindex_dump import SOURCE_NAME as DUMP_SOURCE_NAME
from ddm.sources.rss_fetch import REFUSED_BACKOFF

logger = logging.getLogger(__name__)

# Session-level, held for the whole run; distinct from the refresh lock.
DISCOVERY_LOCK_KEY = 4_242_300
BATCH_SIZE = 100
# The connection idles while feeds are looked up; Neon drops it after 5 idle minutes.
MAX_BATCH_SECONDS = 60.0
DEFAULT_LIMIT = 2000
MAX_LIMIT = 10_000
# Failed lookups in a row that mean the source is down rather than one feed being broken.
MAX_CONSECUTIVE_FAILURES = 3
# Listings scanned per English feed allowed: most new feeds are not English, but a source
# that keeps serving listings must not keep a run going forever.
LISTINGS_PER_LOOKUP = 20
LOOKUP_ERRORS = (SourceUnavailable, SourceResponseError)
PROVENANCE = "observed"
ANALYZE_TABLES = (
    "source_records, observations, entities, entity_identifiers, candidate_matches,"
    " discoveries, discovery_cursors"
)
UPSERT_CURSOR = """
INSERT INTO discovery_cursors (source_id, last_feed_id, updated_run) VALUES (%s, %s, %s)
ON CONFLICT (source_id) DO UPDATE
  SET last_feed_id = excluded.last_feed_id, updated_run = excluded.updated_run
  WHERE discovery_cursors.last_feed_id < excluded.last_feed_id  -- never moves backwards
"""
ZERO_COUNTS = RunMetrics(
    rows_in=0,
    rows_rejected=0,
    rows_skipped=0,
    rows_new=0,
    rows_changed=0,
    rows_unchanged=0,
    entities_created=0,
    identifiers_added=0,
    strong_id_merges=0,
    candidates_created=0,
    ambiguous_identifiers=0,
    discovered_new=0,
    discovered_rename_or_move=0,
    discovered_duplicate=0,
    discovered_uncertain=0,
)


class DiscoveryBusy(RuntimeError):
    """Another newfeeds discovery holds the discovery lock."""


class ApiClient(Protocol):
    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]: ...


class _Counting:
    """Counts API calls for the run metrics; the client itself does the rate limiting."""

    def __init__(self, client: ApiClient):
        self._client = client
        self.requests = 0

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.requests += 1
        return self._client.get(path, params)


@dataclass(frozen=True)
class NewfeedsReport:
    run_id: int
    status: str
    stopped: str  # caught_up / unsettled / limit / listing_cap / refused / unavailable
    cursor: int | None
    metrics: RunMetrics


@dataclass
class _Batch:
    """Feeds handled since the last commit. Mutable on purpose: it is a write buffer."""

    records: list[NormalizedRecord] = field(default_factory=list)
    rejects: list[Reject] = field(default_factory=list)
    english: int = 0
    skipped: int = 0
    last_feed_id: int | None = None

    def empty(self) -> bool:
        return self.last_feed_id is None


@dataclass
class _Held:
    """Listings after a failed lookup, held until a later lookup shows the source is up.

    Mutable on purpose, like _Batch. Each item is (listing, row number); a row number of
    None marks a listing that would have been skipped anyway.
    """

    items: list[tuple[dict[str, Any], int | None]] = field(default_factory=list)
    failures: int = 0


def _flush(held: _Held, batch: _Batch) -> _Held:
    """The source answered again: file held failures as rejects and pass them."""
    for listed, row_number in held.items:
        if row_number is None:
            batch.skipped += 1
        else:
            batch.english += 1
            batch.rejects.append(
                make_reject(
                    row_number=row_number,
                    source_key=str(listed["id"]),
                    reason="lookup_failed",
                    raw=safe_raw(listed),
                )
            )
        batch.last_feed_id = listed["id"]
    return _Held()


def _start_at(conn: psycopg.Connection, api_id: int, dump_id: int) -> int:
    row = conn.execute(
        "SELECT last_feed_id FROM discovery_cursors WHERE source_id = %s", (api_id,)
    ).fetchone()
    if row is not None:
        return row[0] + 1
    newest = conn.execute(
        "SELECT max(source_key::bigint) FROM source_records WHERE source_id = %s", (dump_id,)
    ).fetchone()[0]
    if newest is None:
        raise DiscoveryInputError("import the baseline first (ddm import-baseline)")
    return newest


def _fail_stale_runs(conn: psycopg.Connection, api_id: int) -> int:
    with conn.transaction():
        return conn.execute(
            "UPDATE import_runs SET status = 'failed', finished_at = now()"
            " WHERE source_id = %s AND run_kind = 'discovery' AND status = 'running'",
            (api_id,),
        ).rowcount


def _commit(conn, batch: _Batch, *, api_id: int, run_id: int, total: RunMetrics) -> RunMetrics:
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (IMPORT_LOCK_KEY,))
        append_rejects(conn, run_id, batch.rejects)
        found = find_ambiguous_identifiers(batch.records)
        write_ambiguous_identifiers(conn, found, source_id=api_id, run_id=run_id)
        counts = discover_batch(
            conn,
            batch.records,
            source_name=SOURCE_NAME,
            source_id=api_id,
            run_id=run_id,
            provenance=PROVENANCE,
            ambiguous=load_ambiguous_identifiers(conn),
            threshold=FUZZY_THRESHOLD,
        )
        conn.execute(UPSERT_CURSOR, (api_id, batch.last_feed_id, run_id))
        added = metrics_of(
            counts,
            rows_in=batch.english,
            rows_rejected=len(batch.rejects),
            rows_skipped=batch.skipped,
            ambiguous_identifiers=len(found),
        )
        updated = add_metrics(total, added)
        write_metrics(conn, run_id, updated)
    return updated


def _handle(api: _Counting, listed: dict[str, Any], row_number: int, batch: _Batch) -> None:
    """Look up one English listing and file it as a record, a reject or a skip.

    Counted only once the lookup answered, so a refusal mid-lookup leaves the batch
    exactly as it was: the listing is neither filed nor passed by the cursor.
    """
    feed_id = listed["id"]
    full = fetch_feed(api, feed_id)
    batch.english += 1
    if full is None:
        reject = make_reject(
            row_number=row_number,
            source_key=str(feed_id),
            reason="not_found",
            raw=safe_raw(listed),
        )
        batch.rejects.append(reject)
        return
    result = normalize_api_feed(row_number, listed, full)
    if isinstance(result, Reject):
        batch.rejects.append(result)
    elif in_demo_slice(result):
        batch.records.append(result)
    else:
        batch.skipped += 1


@dataclass(frozen=True)
class _Context:
    as_of: datetime
    limit: int
    batch_size: int
    monotonic: Callable[[], float]
    api_id: int
    run_id: int
    refused_backoff: timedelta


def _batch_full(batch: _Batch, size: int) -> bool:
    return len(batch.records) + len(batch.rejects) + batch.skipped >= size


def _wanted(listed: dict[str, Any]) -> bool:
    """Worth a full lookup: a known age, not dead, English."""
    return has_known_age(listed) and not listed.get("dead") and is_english(listed)


def _stop_reason(listed: dict[str, Any], seen: int, english: int, ctx: _Context) -> str | None:
    if seen >= ctx.limit * LISTINGS_PER_LOOKUP:
        return "listing_cap"
    if has_known_age(listed) and not is_settled(listed, ctx.as_of):
        return "unsettled"
    if _wanted(listed) and english >= ctx.limit:
        return "limit"
    return None


def _walk(conn, api: _Counting, listings: Iterator[dict[str, Any]], ctx: _Context):
    total, batch, held = ZERO_COUNTS, _Batch(), _Held()
    seen = english = 0
    deadline = ctx.monotonic() + MAX_BATCH_SECONDS
    stopped = "caught_up"
    try:
        for listed in listings:
            if reason := _stop_reason(listed, seen, english, ctx):
                stopped = reason
                break
            seen += 1
            if not _wanted(listed):
                if held.items:
                    held.items.append((listed, None))
                    continue
                batch.skipped += 1
            else:
                english += 1
                try:
                    _handle(api, listed, english, batch)
                except LOOKUP_ERRORS as exc:
                    logger.warning("lookup failed feed_id=%s error=%s", listed["id"], exc)
                    held.items.append((listed, english))
                    held.failures += 1
                    if held.failures >= MAX_CONSECUTIVE_FAILURES:
                        stopped = "unavailable"
                        break
                    continue
                held = _flush(held, batch)
            batch.last_feed_id = listed["id"]
            if _batch_full(batch, ctx.batch_size) or ctx.monotonic() >= deadline:
                total = _commit(conn, batch, api_id=ctx.api_id, run_id=ctx.run_id, total=total)
                batch, deadline = _Batch(), ctx.monotonic() + MAX_BATCH_SECONDS
    except SourceAccessDenied as exc:
        logger.warning("podcastindex refused status=%d; stopping this run", exc.status_code)
        stopped = "refused"
        _remember_refusal(conn, exc, ctx)
    except LOOKUP_ERRORS as exc:  # while paging the listings
        logger.warning("podcastindex unavailable; stopping this run error=%s", exc)
        stopped = "unavailable"
    if not batch.empty():
        total = _commit(conn, batch, api_id=ctx.api_id, run_id=ctx.run_id, total=total)
    return stopped, total


def _remember_refusal(conn, exc: SourceAccessDenied, ctx: _Context) -> None:
    until = record_refusal(
        conn,
        ctx.api_id,
        status=exc.status_code,
        retry_after_header=exc.retry_after,
        now=ctx.as_of,
        refused_backoff=ctx.refused_backoff,
        run_id=ctx.run_id,
    )
    logger.warning("podcastindex backoff until=%s", until.isoformat())


def _register(conn, api_id: int, start: int, as_of: datetime, limit: int):
    params = {
        "start_at": start,
        "limit": limit,
        "normalizer_version": NORMALIZER_VERSION,
        "resolution_rules": RESOLUTION_RULES,
        "started_at": as_of.isoformat(),
    }
    run = register_run(
        conn,
        source_id=api_id,
        run_kind="discovery",
        fingerprint=input_fingerprint(SOURCE_NAME, params, [fingerprint_item(str(start), b"")]),
        is_complete_snapshot=False,
        params=params,
    )
    if run.status != "running" or run.resumed:
        raise RuntimeError(f"discovery fingerprint collided with run {run.run_id}")
    return run


def _locked(conn, client: ApiClient, *, limit, now, monotonic, batch_size, refused_backoff):
    started = monotonic()
    as_of = now()
    api_id = source_id_of(conn, SOURCE_NAME)
    dump_id = source_id_of(conn, DUMP_SOURCE_NAME)
    check_backoff(conn, api_id, as_of)
    if stale := _fail_stale_runs(conn, api_id):
        logger.warning("marked %d crashed discovery run(s) failed", stale)
    start = _start_at(conn, api_id, dump_id)
    run = _register(conn, api_id, start, as_of, limit)
    logger.info("run_id=%d start_at=%d limit=%d", run.run_id, start, limit)
    api = _Counting(client)
    ctx = _Context(as_of, limit, batch_size, monotonic, api_id, run.run_id, refused_backoff)
    try:
        stopped, total = _walk(conn, api, iter_newfeeds(api, start_at=start), ctx)
        conn.execute(f"ANALYZE {ANALYZE_TABLES}")
        size = conn.execute("SELECT pg_database_size(current_database())").fetchone()[0]
        metrics = RunMetrics(
            **{
                **asdict(total),
                "http_requests": api.requests,
                "duration_ms": int((monotonic() - started) * 1000),
                "db_size_bytes": size,
            }
        )
        with conn.transaction():
            write_metrics(conn, run.run_id, metrics)
    except BaseException:  # KeyboardInterrupt too: the run must not stay 'running'
        _best_effort(lambda: finish_run(conn, run.run_id, "failed"))
        raise
    status = "partial" if stopped in ("refused", "unavailable") else "ok"
    finish_run(conn, run.run_id, status)
    cursor = conn.execute(
        "SELECT last_feed_id FROM discovery_cursors WHERE source_id = %s", (api_id,)
    ).fetchone()
    return NewfeedsReport(run.run_id, status, stopped, cursor[0] if cursor else None, metrics)


def _best_effort(action: Callable[[], object]) -> None:
    try:
        action()
    except psycopg.Error as exc:
        logger.warning("cleanup failed error=%s", type(exc).__name__)


def discover_newfeeds(
    conn: psycopg.Connection,
    client: ApiClient,
    *,
    limit: int = DEFAULT_LIMIT,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    monotonic: Callable[[], float] = time.monotonic,
    batch_size: int = BATCH_SIZE,
    refused_backoff: timedelta = REFUSED_BACKOFF,
) -> NewfeedsReport:
    """Classify feeds Podcast Index added after the cursor. Expects an autocommit connection."""
    if not 1 <= limit <= MAX_LIMIT:
        raise DiscoveryInputError(f"limit must be between 1 and {MAX_LIMIT}")
    if not conn.autocommit:
        raise DiscoveryInputError("discovery needs an autocommit connection")
    if not conn.execute("SELECT pg_try_advisory_lock(%s)", (DISCOVERY_LOCK_KEY,)).fetchone()[0]:
        raise DiscoveryBusy("another newfeeds discovery is running")
    try:
        return _locked(
            conn,
            client,
            limit=limit,
            now=now,
            monotonic=monotonic,
            batch_size=batch_size,
            refused_backoff=refused_backoff,
        )
    finally:
        if not (conn.closed or conn.broken):
            _best_effort(
                lambda: conn.execute("SELECT pg_advisory_unlock(%s)", (DISCOVERY_LOCK_KEY,))
            )
