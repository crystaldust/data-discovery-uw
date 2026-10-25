"""Podcast refresh: re-fetch known baseline feeds and record what changed.

Flow: take the refresh lock (one refresh at a time) -> mark crashed refresh runs failed ->
pick targets (least recently attempted, or the feeds of an earlier run with same_as) ->
register a new run -> per batch: fetch outside any transaction, then in one transaction
under the shared import lock write attempts, rejects, records, identifiers, contact routes,
fetch state and cumulative metrics -> ANALYZE -> finish 'ok', or 'partial' when any feed
could not be fetched.

Records attach to the baseline record's entity; a refresh never creates or merges
entities. Idempotence comes from the content-hash guard, 304s and run-keyed observations,
not from the run fingerprint: every refresh is a new run, so the fingerprint includes the
start time. A failed fetch writes nothing but its attempt and fetch state.
"""

import logging
import time
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from urllib.parse import urlsplit

import psycopg

from ddm.ingest.fetch_log import (
    RefreshInputError,
    RefreshTarget,
    fail_stale_runs,
    select_targets,
    targets_of_run,
)
from ddm.ingest.records import fingerprint_item, input_fingerprint
from ddm.ingest.refresh_batch import Fetcher, fetch_batch, write_batch
from ddm.ingest.runs import (
    RunMetrics,
    finish_run,
    register_run,
    source_id_of,
    write_metrics,
)
from ddm.sources.podcast_rss import NORMALIZER_VERSION, SOURCE_NAME, activity_status
from ddm.sources.podcastindex_dump import SOURCE_NAME as DUMP_SOURCE_NAME
from ddm.sources.rss_fetch import interleave_by_host

logger = logging.getLogger(__name__)

# Session-level: held for the whole run so two refreshes never fetch the same feeds.
REFRESH_LOCK_KEY = 4_242_200
BATCH_SIZE = 100
# A batch is also written once fetching it has taken this long: the database connection
# sits idle while feeds are fetched, and Neon suspends an idle compute after 5 minutes,
# which drops the connection and the session lock with it.
MAX_BATCH_SECONDS = 60.0
DEFAULT_LIMIT = 1000
MAX_LIMIT = 5000
ANALYZE_TABLES = (
    "source_records, observations, entity_identifiers, contact_routes, feed_fetch_state,"
    " fetch_attempts"
)
ZERO_COUNTS = RunMetrics(
    rows_in=0,
    rows_rejected=0,
    rows_new=0,
    rows_changed=0,
    rows_unchanged=0,
    identifiers_added=0,
    candidates_created=0,
    http_requests=0,
    bytes_fetched=0,
    rows_not_modified=0,
    rows_failed=0,
    rows_skipped=0,
)


class RefreshBusy(RuntimeError):
    """Another refresh holds the refresh lock."""


@dataclass(frozen=True)
class RefreshReport:
    run_id: int
    status: str
    metrics: RunMetrics
    outcomes: Mapping[str, int]  # fetched / not_modified / rejected / failed / skipped
    activity: Mapping[str, int]  # active / dormant / dead / unknown over parsed records


def _host(target: RefreshTarget) -> str:
    try:
        return (urlsplit(target.url).hostname or "").lower()
    except ValueError:
        return ""


def _targets(
    conn: psycopg.Connection, *, same_as: int | None, limit: int, now: datetime, ids: tuple
) -> list[RefreshTarget]:
    dump_id, rss_id = ids
    if same_as is not None:
        found = targets_of_run(conn, same_as, dump_id=dump_id, rss_id=rss_id, now=now)
    else:
        found = select_targets(conn, dump_id=dump_id, limit=limit, now=now)
    return interleave_by_host(found, key=_host)


def _run(conn, fetcher, *, targets, run_id, rss_id, as_of, now, monotonic, batch_size):
    total, outcomes, activity = ZERO_COUNTS, Counter(), Counter()
    start = 0
    while start < len(targets):
        deadline = monotonic() + MAX_BATCH_SECONDS
        batch = fetch_batch(
            fetcher,
            targets[start : start + batch_size],
            start + 1,
            now,
            stop=lambda deadline=deadline: monotonic() >= deadline,
        )
        total = write_batch(conn, batch, rss_id=rss_id, run_id=run_id, previous=total)
        outcomes.update(a.outcome for a in batch.attempts)
        activity.update(activity_status(r.source_updated_at, as_of) for r in batch.records)
        start += len(batch.targets)
        logger.info("run_id=%d progress=%d/%d", run_id, start, len(targets))
    return total, outcomes, activity


def _refresh_locked(conn, fetcher, *, limit, same_as, monotonic, now, batch_size):
    started = monotonic()
    as_of = now()
    ids = (source_id_of(conn, DUMP_SOURCE_NAME), source_id_of(conn, SOURCE_NAME))
    rss_id = ids[1]
    stale = fail_stale_runs(conn, rss_id)
    if stale:
        logger.warning("marked %d crashed refresh run(s) failed", stale)
    targets = _targets(conn, same_as=same_as, limit=limit, now=as_of, ids=ids)
    params = {
        "limit": None if same_as is not None else limit,
        "same_as": same_as,
        "normalizer_version": NORMALIZER_VERSION,
        "started_at": as_of.isoformat(),
        "targets": len(targets),
    }
    items = [fingerprint_item(t.source_key, b"") for t in targets]
    run = register_run(
        conn,
        source_id=rss_id,
        run_kind="refresh",
        fingerprint=input_fingerprint(SOURCE_NAME, params, items),
        is_complete_snapshot=False,
        params=params,
    )
    if run.status != "running" or run.resumed:
        raise RuntimeError(f"refresh fingerprint collided with run {run.run_id}")
    logger.info("run_id=%d targets=%d same_as=%s", run.run_id, len(targets), same_as)
    try:
        total, outcomes, activity = _run(
            conn,
            fetcher,
            targets=targets,
            run_id=run.run_id,
            rss_id=rss_id,
            as_of=as_of,
            now=now,
            monotonic=monotonic,
            batch_size=batch_size,
        )
        conn.execute(f"ANALYZE {ANALYZE_TABLES}")
        size = conn.execute("SELECT pg_database_size(current_database())").fetchone()[0]
        metrics = RunMetrics(
            **{
                **asdict(total),
                "duration_ms": int((monotonic() - started) * 1000),
                "db_size_bytes": size,
            }
        )
        with conn.transaction():
            write_metrics(conn, run.run_id, metrics)
    except BaseException:  # KeyboardInterrupt too: the run must not stay 'running'
        _best_effort(lambda: finish_run(conn, run.run_id, "failed"), f"run_id={run.run_id} finish")
        raise
    status = "partial" if metrics.rows_failed else "ok"
    finish_run(conn, run.run_id, status)
    return RefreshReport(
        run.run_id,
        status,
        metrics,
        MappingProxyType(dict(outcomes)),
        MappingProxyType(dict(activity)),
    )


def _best_effort(action: Callable[[], object], what: str) -> None:
    """Cleanup that must not replace the error being raised (e.g. on a dropped connection)."""
    try:
        action()
    except psycopg.Error as exc:
        logger.warning("%s failed during cleanup error=%s", what, type(exc).__name__)


def _unlock(conn: psycopg.Connection) -> None:
    # A dropped connection has already released its session lock.
    if conn.closed or conn.broken:
        return
    row = conn.execute("SELECT pg_advisory_unlock(%s)", (REFRESH_LOCK_KEY,)).fetchone()
    if not row[0]:
        logger.warning("refresh lock was not held at unlock")


def refresh_podcasts(
    conn: psycopg.Connection,
    fetcher: Fetcher,
    *,
    limit: int = DEFAULT_LIMIT,
    same_as: int | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    batch_size: int = BATCH_SIZE,
) -> RefreshReport:
    """Refresh up to `limit` baseline feeds. Expects an autocommit, direct connection."""
    if not 1 <= limit <= MAX_LIMIT:
        raise RefreshInputError(f"limit must be between 1 and {MAX_LIMIT}")
    if not conn.autocommit:
        # Batches must commit one by one, and the session lock must outlive them.
        raise RefreshInputError("refresh needs an autocommit connection")
    if not conn.execute("SELECT pg_try_advisory_lock(%s)", (REFRESH_LOCK_KEY,)).fetchone()[0]:
        raise RefreshBusy("another podcast refresh is running")
    try:
        return _refresh_locked(
            conn,
            fetcher,
            limit=limit,
            same_as=same_as,
            monotonic=monotonic,
            now=now,
            batch_size=batch_size,
        )
    finally:
        _best_effort(lambda: _unlock(conn), "refresh unlock")
