"""One refresh batch: fetch outside any transaction, then write everything atomically.

The write runs under the shared import lock, because entities and identifiers are shared
with the baseline import, and records the run's cumulative metrics in the same transaction,
so committed data and its counts can never disagree.
"""

import logging
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import psycopg

from ddm.ingest.contacts import touch_contact_routes, upsert_contact_routes
from ddm.ingest.fetch_log import Attempt, RefreshTarget, update_fetch_state, write_attempts
from ddm.ingest.records import NormalizedRecord, Reject
from ddm.ingest.resolve import (
    apply_attachments,
    load_ambiguous_identifiers,
    load_identifier_index,
    plan_identifier_attachments,
)
from ddm.ingest.runs import (
    IMPORT_LOCK_KEY,
    RunMetrics,
    add_metrics,
    append_rejects,
    write_metrics,
)
from ddm.ingest.upsert import upsert_records
from ddm.sources.podcast_rss import SOURCE_NAME, contact_routes_for, normalize_feed
from ddm.sources.rss_fetch import FetchResult

logger = logging.getLogger(__name__)

PROVENANCE = "observed"


class Fetcher(Protocol):
    def fetch(self, url: str, *, etag: str | None, last_modified: str | None) -> FetchResult: ...


@dataclass(frozen=True)
class Batch:
    targets: tuple[RefreshTarget, ...]
    attempts: tuple[Attempt, ...]
    records: tuple[NormalizedRecord, ...]
    rejects: tuple[Reject, ...]


def _internal_failure(target: RefreshTarget, attempted_at: datetime) -> FetchResult:
    return FetchResult(
        requested_url=target.url,
        final_url=target.url,
        outcome="failed",
        reason="internal_error",
        status=None,
        body=None,
        content_type=None,
        etag=target.etag,
        last_modified=target.last_modified,
        moved_permanently=False,
        retry_after=None,
        bytes_read=0,
        requests=0,
        elapsed_ms=0,
        attempted_at=attempted_at,
    )


def _attempt(
    fetcher: Fetcher, target: RefreshTarget, row_number: int
) -> tuple[Attempt, NormalizedRecord | Reject | None]:
    result = fetcher.fetch(target.url, etag=target.etag, last_modified=target.last_modified)
    if result.outcome != "fetched":
        return Attempt(target.source_key, result.outcome, result.reason, result), None
    normalized = normalize_feed(row_number, target.source_key, result)
    if isinstance(normalized, Reject):
        return Attempt(target.source_key, "rejected", normalized.reason, result), normalized
    return Attempt(target.source_key, "fetched", None, result), normalized


def fetch_batch(
    fetcher: Fetcher,
    targets: Sequence[RefreshTarget],
    first_row: int,
    now: Callable[[], datetime],
    stop: Callable[[], bool] = lambda: False,
) -> Batch:
    """Fetch targets in order until done or `stop()` says the batch must be written now.

    The returned batch covers only the targets actually fetched (at least one).
    """
    attempts, records, rejects = [], [], []
    fetched: list[RefreshTarget] = []
    for row_number, target in enumerate(targets, start=first_row):
        try:
            attempt, normalized = _attempt(fetcher, target, row_number)
        except Exception as exc:
            # A bug or a surprise from one feed must not cost the batch or stall every run;
            # the feed is recorded as failed and rotates to the back like any other failure.
            # Class name only: an exception message can quote feed content, emails included.
            logger.warning("source_key=%s internal error=%s", target.source_key, type(exc).__name__)
            attempt, normalized = (
                Attempt(
                    target.source_key, "failed", "internal_error", _internal_failure(target, now())
                ),
                None,
            )
        fetched.append(target)
        attempts.append(attempt)
        if isinstance(normalized, Reject):
            rejects.append(normalized)
        elif normalized is not None:
            records.append(normalized)
        if stop():
            break
    return Batch(tuple(fetched), tuple(attempts), tuple(records), tuple(rejects))


def write_batch(
    conn: psycopg.Connection, batch: Batch, *, rss_id: int, run_id: int, previous: RunMetrics
) -> RunMetrics:
    """Write one fetched batch atomically; returns the run's cumulative metrics."""
    entity_by_key = {t.source_key: t.entity_id for t in batch.targets}
    attempted_at = {a.source_key: a.fetch.attempted_at for a in batch.attempts}
    not_modified = [a.source_key for a in batch.attempts if a.outcome == "not_modified"]
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (IMPORT_LOCK_KEY,))
        write_attempts(conn, run_id, batch.attempts)
        append_rejects(conn, run_id, batch.rejects)
        upserted = upsert_records(
            conn,
            source_id=rss_id,
            run_id=run_id,
            provenance=PROVENANCE,
            records=batch.records,
            entity_ids=entity_by_key,
        )
        # Loaded under the lock, so a concurrent import cannot slip identifiers in between.
        ambiguous = load_ambiguous_identifiers(conn)
        usable = (i for r in batch.records for i in r.identifiers if i not in ambiguous)
        index = load_identifier_index(conn, usable)
        plan = plan_identifier_attachments(
            batch.records, entity_by_key, index, SOURCE_NAME, ambiguous
        )
        candidates = apply_attachments(conn, plan, source_id=rss_id, run_id=run_id)
        routes = [
            (entity_by_key[r.source_key], r.source_key, route, attempted_at[r.source_key])
            for r in batch.records
            for route in contact_routes_for(r)
        ]
        upsert_contact_routes(conn, routes, source_id=rss_id, run_id=run_id)
        touch_contact_routes(
            conn, [(k, attempted_at[k]) for k in not_modified], source_id=rss_id, run_id=run_id
        )
        update_fetch_state(conn, run_id, batch.attempts)
        outcomes = Counter(a.outcome for a in batch.attempts)
        counted = RunMetrics(
            rows_in=len(batch.targets),
            rows_rejected=len(batch.rejects),
            rows_new=len(upserted.new_keys),
            rows_changed=len(upserted.changed_keys),
            rows_unchanged=upserted.unchanged + outcomes["not_modified"],
            identifiers_added=len(plan.new_identifiers),
            candidates_created=candidates,
            http_requests=sum(a.fetch.requests for a in batch.attempts),
            bytes_fetched=sum(a.fetch.bytes_read for a in batch.attempts),
            rows_not_modified=outcomes["not_modified"],
            rows_failed=outcomes["failed"],
            rows_skipped=outcomes["skipped"],
        )
        total = add_metrics(previous, counted)
        write_metrics(conn, run_id, total)
    return total
