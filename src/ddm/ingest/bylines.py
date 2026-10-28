"""Byline discovery from outlet feeds: who is writing where, attached to known people only
by an author page.

Flow: take the journalist lock -> mark crashed byline runs failed -> pick the section feeds
and up to `author_feeds` per-author feeds of Wikidata journalists (least recently
attempted first, none still backing off) -> register the run -> fetch every feed outside
any transaction, stopping after MAX_FETCH_SECONDS (the database connection sits idle
meanwhile and Neon drops it after 5 minutes) -> aggregate bylines into one record per
author per outlet -> in one transaction under the shared import lock write attempts,
rejects, resolution, classification, candidates, records, discoveries, contact routes,
fetch state and metrics -> ANALYZE -> 'ok', or 'partial' when a feed failed.

Only first-seen keys are classified (byline_classify.py), so the same feeds twice write
no new discoveries; a known key is upserted (a new byline is a 'changed' observation) and
may still gain an author page. A feed that answers 304 or omits someone says nothing
about them: bylines are never a complete snapshot and never soft-delete.
"""

import logging
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from urllib.parse import urlsplit
from uuid import UUID

import psycopg

from ddm.ingest.byline_classify import (
    BYLINE_FUZZY_METHOD,
    BYLINE_SCORER_VERSION,
    BYLINE_THRESHOLD,
    byline_neighbours,
    classify_byline_attached,
    classify_byline_unmatched,
    known_outlets,
)
from ddm.ingest.classify import Classification, classify_conflict
from ddm.ingest.contacts import upsert_contact_routes
from ddm.ingest.discovery import FIRST_SEEN_KIND, KINDS, write_discoveries
from ddm.ingest.fetch_log import Attempt, update_fetch_state, write_attempts
from ddm.ingest.records import (
    NormalizedRecord,
    Reject,
    input_fingerprint,
    make_reject,
    safe_raw,
)
from ddm.ingest.refresh_batch import Fetcher
from ddm.ingest.resolve import (
    Candidate,
    ResolutionPlan,
    apply_attachments,
    apply_resolution,
    load_ambiguous_identifiers,
    load_entity_titles,
    load_identifier_index,
    plan_identifier_attachments,
    plan_resolution,
    write_scored_candidates,
)
from ddm.ingest.runs import (
    IMPORT_LOCK_KEY,
    RunMetrics,
    append_rejects,
    finish_run,
    register_run,
    source_id_of,
    write_metrics,
)
from ddm.ingest.upsert import upsert_records
from ddm.ingest.wikidata_sync import (
    AUTHOR_PAGE_KIND,
    ENTITY_TYPE,
    best_effort,
    fail_stale_runs,
    journalist_lock,
)
from ddm.sources.byline_parse import ParsedFeed, parse_bylines
from ddm.sources.outlet_rss import (
    AUTHOR_FEEDS,
    NORMALIZER_VERSION,
    SECTION_FEEDS,
    SOURCE_NAME,
    OutletFeed,
    aggregate,
    author_feed,
    contact_routes_for,
    merge_window,
)
from ddm.sources.rss_fetch import FetchResult, interleave_by_host
from ddm.sources.rss_parse import FeedParseError
from ddm.sources.wikidata import SOURCE_NAME as WIKIDATA_SOURCE_NAME

logger = logging.getLogger(__name__)

DEFAULT_AUTHOR_FEEDS = 50
MAX_AUTHOR_FEEDS = 200
MAX_FETCH_SECONDS = 180.0
PROVENANCE = "observed"
PARSE_FAILED = "parse_failed"
ANALYZE_TABLES = (
    "source_records, observations, entities, entity_identifiers, contact_routes,"
    " candidate_matches, discoveries, feed_fetch_state, fetch_attempts"
)

AUTHOR_IDS = """
SELECT s.display_name, p.prop, v.slug
FROM source_records s
CROSS JOIN LATERAL jsonb_each(s.attrs->'author_ids') AS p(prop, slugs)
CROSS JOIN LATERAL jsonb_array_elements_text(p.slugs) AS v(slug)
WHERE s.source_id = %s AND s.deleted_at IS NULL AND s.entity_id IS NOT NULL
  AND p.prop = ANY(%s)
ORDER BY s.source_key, p.prop, v.slug
"""
FETCH_STATE = """
SELECT source_key, etag, last_modified, last_attempt_at, retry_after
FROM feed_fetch_state WHERE source_key = ANY(%s)
"""
KNOWN_ENTITIES = """
SELECT source_key, entity_id FROM source_records
WHERE source_id = %s AND entity_id IS NOT NULL AND source_key = ANY(%s)
"""
STORED_ATTRS = """
SELECT source_key, attrs FROM source_records WHERE source_id = %s AND source_key = ANY(%s)
"""


class BylineInputError(ValueError):
    """The caller asked for something the byline job cannot do."""


@dataclass(frozen=True)
class BylineReport:
    run_id: int
    status: str
    metrics: RunMetrics
    outcomes: Mapping[str, int]  # fetched / not_modified / rejected / failed / skipped
    organisation_bylines: int
    bylines: int
    authors: int


@dataclass(frozen=True)
class _Target:
    feed: OutletFeed
    etag: str | None
    last_modified: str | None


@dataclass(frozen=True)
class _Fetched:
    attempts: tuple[Attempt, ...]
    feeds: tuple[tuple[OutletFeed, ParsedFeed, datetime], ...]
    rejects: tuple[Reject, ...]


def _host(target: _Target) -> str:
    return (urlsplit(target.feed.url).hostname or "").lower()


def _targets(conn, wikidata_id: int, author_feeds: int, now: datetime) -> list[_Target]:
    rows = conn.execute(AUTHOR_IDS, (wikidata_id, list(AUTHOR_FEEDS))).fetchall()
    ambiguous = load_ambiguous_identifiers(conn)
    # A page two people claim could be either's; its feed would attach bylines to one.
    authors = {
        f.url: f
        for name, prop, slug in rows
        if (f := author_feed(prop, slug, name)) is not None
        and (AUTHOR_PAGE_KIND, f.author_url) not in ambiguous
    }
    feeds = [*SECTION_FEEDS, *authors.values()]
    state = {
        key: rest for key, *rest in conn.execute(FETCH_STATE, ([f.key for f in feeds],)).fetchall()
    }
    ready = [f for f in feeds if not (f.key in state and (state[f.key][3] or now) > now)]
    by_age = sorted(
        (f for f in ready if f.author_url),
        key=lambda f: (f.key in state, state.get(f.key, [None] * 3)[2] or now, f.key),
    )
    chosen = [f for f in ready if not f.author_url] + by_age[:author_feeds]
    targets = [_Target(f, *(state.get(f.key, [None, None])[:2])) for f in chosen]
    return interleave_by_host(targets, key=_host)


def _internal_failure(target: _Target, attempted_at: datetime) -> FetchResult:
    return FetchResult(
        requested_url=target.feed.url,
        final_url=target.feed.url,
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


def _fetch_one(fetcher: Fetcher, target: _Target, row_number: int, now):
    """(attempt, parsed feed or None, reject or None); never raises for one feed."""
    key = target.feed.key  # stored; logs use the label, which names no author
    try:
        result = fetcher.fetch(
            target.feed.url, etag=target.etag, last_modified=target.last_modified
        )
    except Exception as exc:  # one feed's surprise must not cost the run
        logger.warning("feed=%s internal error=%s", target.feed.label, type(exc).__name__)
        return (
            Attempt(key, "failed", "internal_error", _internal_failure(target, now())),
            None,
            None,
        )
    if result.outcome != "fetched" or result.body is None:
        return Attempt(key, result.outcome, result.reason, result), None, None
    try:
        parsed = parse_bylines(result.body, result.attempted_at)
    except FeedParseError as exc:
        reason = exc.reason
    except Exception as exc:  # a hostile body must cost one feed, not the run's fetches
        logger.warning("feed=%s parse error=%s", target.feed.label, type(exc).__name__)
        reason = PARSE_FAILED
    else:
        return Attempt(key, "fetched", None, result), parsed, None
    reject = make_reject(
        row_number=row_number, source_key=key, reason=reason, raw=safe_raw({"url": target.feed.url})
    )
    return Attempt(key, "rejected", reason, result), None, reject


def _fetch_all(fetcher, targets: Sequence[_Target], now, monotonic) -> _Fetched:
    deadline = monotonic() + MAX_FETCH_SECONDS
    attempts, feeds, rejects = [], [], []
    for row_number, target in enumerate(targets, start=1):
        attempt, parsed, reject = _fetch_one(fetcher, target, row_number, now)
        attempts.append(attempt)
        if parsed is not None:
            feeds.append((target.feed, parsed, attempt.fetch.attempted_at))
        if reject is not None:
            rejects.append(reject)
        if monotonic() >= deadline:
            logger.warning(
                "fetch budget spent; %d feed(s) left for the next run", len(targets) - row_number
            )
            break
    return _Fetched(tuple(attempts), tuple(feeds), tuple(rejects))


def _classify_strong(conn, plan: ResolutionPlan, by_key) -> dict[str, Classification]:
    """Known outlets are read before this run's records are upserted, or every byline would
    count its own outlet as known."""
    outlets = known_outlets(conn, {a.entity_id for a in plan.attached})
    result = {}
    for a in plan.attached:
        # a.existing is False for an entity this run created (plan_resolution's view).
        result[a.source_key] = classify_byline_attached(
            a,
            outlet_qid=by_key[a.source_key].attrs["outlet_qid"],
            known_outlets=outlets.get(a.entity_id, frozenset()),
        )
    for key, suspect, features in plan.candidates:
        result.setdefault(key, classify_conflict(suspect, features))
    return result


def _classify_rest(conn, records, run_id, threshold):
    neighbours = byline_neighbours(conn, records, run_id=run_id)
    classified, candidates = {}, []
    for record in records:
        found = neighbours.get(record.source_key, ())
        classified[record.source_key] = classify_byline_unmatched(
            found[0] if found else None, threshold=threshold
        )
        candidates.extend(
            Candidate(record.source_key, n.entity_id, BYLINE_FUZZY_METHOD, n.score, n.features)
            for n in found
            if n.score >= threshold
        )
    return classified, candidates


def _with_stored_windows(conn, records, source_id: int) -> list[NormalizedRecord]:
    """Known authors keep their stored bylines next to this run's (see merge_window)."""
    keys = [r.source_key for r in records]
    stored = dict(conn.execute(STORED_ATTRS, (source_id, keys)).fetchall())
    return [merge_window(r, stored[r.source_key]) if r.source_key in stored else r for r in records]


@dataclass(frozen=True)
class _Classified:
    plan: ResolutionPlan
    classified: dict[str, Classification]
    candidates_created: int


def _resolve_and_classify(conn, fresh, *, source_id, run_id, ambiguous, index, threshold):
    """Entities for first-seen records, their classes, and review candidates."""
    titles = load_entity_titles(conn, index.values())
    plan = plan_resolution(fresh, index, SOURCE_NAME, ambiguous, titles)
    apply_resolution(
        conn,
        plan,
        source_id=source_id,
        run_id=run_id,
        entity_type=ENTITY_TYPE,
        first_seen_kind=FIRST_SEEN_KIND,
    )
    classified = _classify_strong(conn, plan, {r.source_key: r for r in fresh})
    rest, candidates = _classify_rest(
        conn, [r for r in fresh if r.source_key not in classified], run_id, threshold
    )
    proposed = write_scored_candidates(conn, candidates, source_id=source_id, run_id=run_id)
    return _Classified(plan, {**classified, **rest}, len(plan.candidates) + proposed)


def discover_bylines(
    conn: psycopg.Connection,
    records: Sequence[NormalizedRecord],
    *,
    source_id: int,
    run_id: int,
    threshold: float,
) -> tuple[RunMetrics, dict[str, UUID]]:
    """Resolve, classify, queue and upsert byline records; in the caller's transaction."""
    known = dict(conn.execute(KNOWN_ENTITIES, (source_id, [r.source_key for r in records])))
    ambiguous = load_ambiguous_identifiers(conn)
    index = load_identifier_index(
        conn, (i for r in records for i in r.identifiers if i not in ambiguous)
    )
    done = _resolve_and_classify(
        conn,
        [r for r in records if r.source_key not in known],
        source_id=source_id,
        run_id=run_id,
        ambiguous=ambiguous,
        index=index,
        threshold=threshold,
    )
    plan = done.plan
    index.update({(kind, value): eid for kind, value, eid in plan.new_identifiers})
    attach = plan_identifier_attachments(
        [r for r in records if r.source_key in known], known, index, SOURCE_NAME, ambiguous
    )
    attached_candidates = apply_attachments(conn, attach, source_id=source_id, run_id=run_id)
    entity_ids = {**known, **dict(plan.assignments)}
    upserted = upsert_records(
        conn,
        source_id=source_id,
        run_id=run_id,
        provenance=PROVENANCE,
        records=_with_stored_windows(conn, records, source_id),
        entity_ids=entity_ids,
    )
    write_discoveries(conn, done.classified, entity_ids, source_id=source_id, run_id=run_id)
    per_kind = Counter(c.kind for c in done.classified.values())
    logger.info("step=classify %s", " ".join(f"{k}={per_kind[k]}" for k in KINDS))
    return RunMetrics(
        rows_new=len(upserted.new_keys),
        rows_changed=len(upserted.changed_keys),
        rows_unchanged=upserted.unchanged,
        entities_created=len(plan.new_entities),
        identifiers_added=len(plan.new_identifiers) + len(attach.new_identifiers),
        strong_id_merges=plan.strong_id_merges,
        candidates_created=done.candidates_created + attached_candidates,
        discovered_new=per_kind["new"],
        discovered_rename_or_move=per_kind["rename_or_move"],
        discovered_duplicate=per_kind["duplicate"],
        discovered_uncertain=per_kind["uncertain"],
    ), entity_ids


def _fetch_metrics(fetched: _Fetched, records) -> RunMetrics:
    outcomes = Counter(a.outcome for a in fetched.attempts)
    return RunMetrics(
        rows_in=len(records) + len(fetched.rejects),
        rows_rejected=len(fetched.rejects),
        http_requests=sum(a.fetch.requests for a in fetched.attempts),
        bytes_fetched=sum(a.fetch.bytes_read for a in fetched.attempts),
        rows_not_modified=outcomes["not_modified"],
        rows_failed=outcomes["failed"],
        rows_skipped=outcomes["skipped"],
    )


def _write(conn, fetched: _Fetched, records, *, source_id, run_id, checked_at) -> RunMetrics:
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (IMPORT_LOCK_KEY,))
        write_attempts(conn, run_id, fetched.attempts)
        append_rejects(conn, run_id, fetched.rejects)
        found, entity_ids = discover_bylines(
            conn, records, source_id=source_id, run_id=run_id, threshold=BYLINE_THRESHOLD
        )
        routes = [
            (entity_ids[r.source_key], r.source_key, route, checked_at)
            for r in records
            for route in contact_routes_for(r)
        ]
        upsert_contact_routes(conn, routes, source_id=source_id, run_id=run_id)
        update_fetch_state(conn, run_id, fetched.attempts)
        counted = RunMetrics(
            **{
                **asdict(_fetch_metrics(fetched, records)),
                **{k: v for k, v in asdict(found).items() if v is not None},
            }
        )
        write_metrics(conn, run_id, counted)
    return counted


def _register(conn, source_id: int, as_of: datetime, author_feeds: int, targets: int):
    params = {
        "normalizer_version": NORMALIZER_VERSION,
        "scorer_version": BYLINE_SCORER_VERSION,
        "threshold": BYLINE_THRESHOLD,
        "author_feeds": author_feeds,
        "feeds": targets,
        "started_at": as_of.isoformat(),
    }
    run = register_run(
        conn,
        source_id=source_id,
        run_kind="discovery",
        fingerprint=input_fingerprint(SOURCE_NAME, params, []),
        is_complete_snapshot=False,
        params=params,
    )
    if run.status != "running" or run.resumed:
        raise RuntimeError(f"byline fingerprint collided with run {run.run_id}")
    return run


def _run(conn, fetcher, run_id, source_id, targets, as_of, now, monotonic, started):
    fetched = _fetch_all(fetcher, targets, now, monotonic)
    records, organisations = aggregate(fetched.feeds)
    counted = _write(conn, fetched, records, source_id=source_id, run_id=run_id, checked_at=as_of)
    conn.execute(f"ANALYZE {ANALYZE_TABLES}")
    size = conn.execute("SELECT pg_database_size(current_database())").fetchone()[0]
    metrics = RunMetrics(
        **{
            **asdict(counted),
            "duration_ms": int((monotonic() - started) * 1000),
            "db_size_bytes": size,
        }
    )
    with conn.transaction():
        write_metrics(conn, run_id, metrics)
    status = "partial" if metrics.rows_failed else "ok"
    finish_run(conn, run_id, status)
    return BylineReport(
        run_id=run_id,
        status=status,
        metrics=metrics,
        outcomes=MappingProxyType(dict(Counter(a.outcome for a in fetched.attempts))),
        organisation_bylines=organisations,
        bylines=sum(len(p.items) for _, p, _ in fetched.feeds),
        authors=len(records),
    )


def sync_bylines(
    conn: psycopg.Connection,
    fetcher: Fetcher,
    *,
    author_feeds: int = DEFAULT_AUTHOR_FEEDS,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    monotonic: Callable[[], float] = time.monotonic,
) -> BylineReport:
    """Read the outlet feeds once. Expects an autocommit, direct connection."""
    if not 1 <= author_feeds <= MAX_AUTHOR_FEEDS:
        raise BylineInputError(f"author feeds must be between 1 and {MAX_AUTHOR_FEEDS}")
    with journalist_lock(conn):
        started, as_of = monotonic(), now()
        source_id = source_id_of(conn, SOURCE_NAME)
        if stale := fail_stale_runs(conn, source_id):
            logger.warning("marked %d crashed byline run(s) failed", stale)
        wikidata_id = source_id_of(conn, WIKIDATA_SOURCE_NAME)
        targets = _targets(conn, wikidata_id, author_feeds, as_of)
        run = _register(conn, source_id, as_of, author_feeds, len(targets))
        logger.info("run_id=%d feeds=%d", run.run_id, len(targets))
        try:
            return _run(
                conn, fetcher, run.run_id, source_id, targets, as_of, now, monotonic, started
            )
        except BaseException:  # KeyboardInterrupt too: the run must not stay 'running'
            best_effort(lambda: finish_run(conn, run.run_id, "failed"), f"run_id={run.run_id}")
            raise
