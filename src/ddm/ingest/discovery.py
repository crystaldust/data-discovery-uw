"""Discovery of one batch of records: resolve, classify, propose, write.

Shared by the dump window and the newfeeds pipeline. Only records whose key this source
has never stored are classified; known keys are upserted as usual (a changed record is
observed) but get no discovery row, so the same input twice writes nothing new.

Steps, all in the caller's transaction:
1. resolve first-seen records on strong identifiers (new entities are 'discovered');
2. classify attached records and conflicts (classify.py);
3. look up fuzzy neighbours for the rest; queue those at or above the threshold, and
   feeds Podcast Index marks as duplicates of a known feed, for review;
4. upsert every record with its entity, then write one discovery row per first-seen record.
"""

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from ddm.identifiers import normalize_feed_url
from ddm.ingest.classify import (
    CLASSIFIER_VERSION,
    FUZZY_METHOD,
    PI_DUPLICATE_METHOD,
    Classification,
    Neighbour,
    classify_attached,
    classify_conflict,
    classify_unmatched,
)
from ddm.ingest.fuzzy import best_neighbours
from ddm.ingest.records import NormalizedRecord, source_key_order
from ddm.ingest.resolve import (
    Candidate,
    Identifier,
    ResolutionPlan,
    apply_resolution,
    load_entity_titles,
    load_identifier_index,
    plan_resolution,
    write_scored_candidates,
)
from ddm.ingest.upsert import upsert_records

logger = logging.getLogger(__name__)

FIRST_SEEN_KIND = "discovered"
ENTITY_TYPE = "podcast"
# Podcast Index's duplicateOf is a marker, not a measured similarity.
PI_DUPLICATE_SCORE = 1.0
# Both Podcast Index sources share one key space: the feed id.
PODCASTINDEX_SOURCES = ("podcastindex_dump", "podcastindex_api")
KINDS = ("new", "rename_or_move", "duplicate", "uncertain")


@dataclass(frozen=True)
class DiscoveryCounts:
    new: int = 0
    rename_or_move: int = 0
    duplicate: int = 0
    uncertain: int = 0
    entities_created: int = 0
    identifiers_added: int = 0
    strong_id_merges: int = 0
    candidates_created: int = 0
    rows_new: int = 0
    rows_changed: int = 0
    rows_unchanged: int = 0


def _known_keys(conn: psycopg.Connection, source_id: int, keys: list[str]) -> set[str]:
    rows = conn.execute(
        "SELECT source_key FROM source_records WHERE source_id = %s AND source_key = ANY(%s)",
        (source_id, keys),
    ).fetchall()
    return {key for (key,) in rows}


def _entities(conn: psycopg.Connection, ids: Iterable[UUID]) -> dict[UUID, tuple]:
    """Title and creating run of each entity."""
    rows = conn.execute(
        "SELECT entity_id, display_name, first_seen_run FROM entities WHERE entity_id = ANY(%s)",
        (list(ids),),
    ).fetchall()
    return {entity_id: (name, run) for entity_id, name, run in rows}


def _feed_urls(index: Mapping[Identifier, UUID]) -> dict[UUID, frozenset[str]]:
    urls: dict[UUID, set[str]] = {}
    for (kind, value), entity_id in index.items():
        if kind == "feed_url":
            urls.setdefault(entity_id, set()).add(value)
    return {entity_id: frozenset(values) for entity_id, values in urls.items()}


def _duplicate_targets(
    conn: psycopg.Connection,
    records: Sequence[NormalizedRecord],
    batch_entities: Mapping[str, UUID],
) -> dict[str, UUID]:
    """Entity of the feed each record's Podcast Index duplicateOf points at: one already
    stored, or one first seen in this batch (both sources share the feed-id key space)."""
    wanted = {
        r.source_key: str(r.attrs["duplicate_of_podcastindex_id"])
        for r in records
        if r.attrs.get("duplicate_of_podcastindex_id") is not None
    }
    if not wanted:
        return {}
    rows = conn.execute(
        """
        SELECT DISTINCT ON (r.source_key) r.source_key, r.entity_id
        FROM source_records r JOIN sources s USING (source_id)
        WHERE s.name = ANY(%s) AND r.source_key = ANY(%s) AND r.entity_id IS NOT NULL
        ORDER BY r.source_key, r.source_id
        """,
        (list(PODCASTINDEX_SOURCES), list(set(wanted.values()))),
    ).fetchall()
    entity_by_feed = {**batch_entities, **dict(rows)}
    return {
        key: entity_by_feed[feed]
        for key, feed in wanted.items()
        if feed in entity_by_feed and feed != key
    }


def _classify_strong(
    conn: psycopg.Connection,
    plan: ResolutionPlan,
    records: Mapping[str, NormalizedRecord],
    index: Mapping[Identifier, UUID],
    run_id: int,
) -> dict[str, Classification]:
    feed_urls = _feed_urls(index)
    known = _entities(conn, {a.entity_id for a in plan.attached if a.existing})
    result = {}
    for attachment in plan.attached:
        record = records[attachment.source_key]
        title, created_by = known.get(attachment.entity_id, (None, run_id))
        # An entity an earlier batch of this run created is as new as one from this batch,
        # so the class does not depend on how the run was split into batches.
        same_run = replace(attachment, existing=attachment.existing and created_by != run_id)
        result[attachment.source_key] = classify_attached(
            same_run,
            record_feed_url=normalize_feed_url(record.attrs.get("feed_url")),
            record_title=record.display_name,
            entity_feed_urls=feed_urls.get(attachment.entity_id, frozenset()),
            entity_title=title,
        )
    for key, suspect, features in plan.candidates:
        result.setdefault(key, classify_conflict(suspect, features))
    return result


def _fuzzy_candidates(
    key: str, neighbours: Sequence[Neighbour], threshold: float
) -> list[Candidate]:
    return [
        Candidate(key, n.entity_id, FUZZY_METHOD, n.score, n.features)
        for n in neighbours
        if n.score >= threshold
    ]


def _classify_unmatched(
    conn: psycopg.Connection,
    records: Sequence[NormalizedRecord],
    *,
    run_id: int,
    threshold: float,
    batch_entities: Mapping[str, UUID],
) -> tuple[dict[str, Classification], list[Candidate]]:
    found = best_neighbours(conn, records, run_id=run_id)
    duplicates = _duplicate_targets(conn, records, batch_entities)
    result, candidates = {}, []
    for record in records:
        key = record.source_key
        neighbours = found.get(key, ())
        best = neighbours[0] if neighbours else None
        result[key] = classify_unmatched(
            best, duplicate_of=duplicates.get(key), threshold=threshold
        )
        candidates.extend(_fuzzy_candidates(key, neighbours, threshold))
        if key in duplicates:
            feed = record.attrs["duplicate_of_podcastindex_id"]
            features = {"duplicate_of_podcastindex_id": feed}
            candidates.append(
                Candidate(key, duplicates[key], PI_DUPLICATE_METHOD, PI_DUPLICATE_SCORE, features)
            )
    return result, candidates


def write_discoveries(
    conn: psycopg.Connection,
    classified: Mapping[str, Classification],
    entity_ids: Mapping[str, UUID],
    *,
    source_id: int,
    run_id: int,
) -> None:
    with (
        conn.cursor() as cur,
        cur.copy(
            "COPY discoveries (run_id, source_id, source_key, classification, entity_id,"
            " matched_entity_id, method, score, evidence, classifier_version) FROM STDIN"
        ) as copy,
    ):
        copy.set_types(
            ["int8", "int2", "text", "text", "uuid", "uuid", "text", "float4", "jsonb", "int2"]
        )
        for key in sorted(classified, key=source_key_order):
            c = classified[key]
            copy.write_row(
                (
                    run_id,
                    source_id,
                    key,
                    c.kind,
                    entity_ids[key],
                    c.matched_entity_id,
                    c.method,
                    c.score,
                    Jsonb(dict(c.evidence)),
                    CLASSIFIER_VERSION,
                )
            )


def discover_batch(
    conn: psycopg.Connection,
    records: Sequence[NormalizedRecord],
    *,
    source_name: str,
    source_id: int,
    run_id: int,
    provenance: str,
    ambiguous: frozenset[Identifier],
    threshold: float,
) -> DiscoveryCounts:
    """Must run inside the caller's transaction."""
    known = _known_keys(conn, source_id, [r.source_key for r in records])
    fresh = [r for r in records if r.source_key not in known]
    by_key = {r.source_key: r for r in fresh}
    index = load_identifier_index(
        conn, (i for r in fresh for i in r.identifiers if i not in ambiguous)
    )
    titles = load_entity_titles(conn, index.values())
    plan = plan_resolution(fresh, index, source_name, ambiguous, titles)
    apply_resolution(
        conn,
        plan,
        source_id=source_id,
        run_id=run_id,
        entity_type=ENTITY_TYPE,
        first_seen_kind=FIRST_SEEN_KIND,
    )
    classified = _classify_strong(conn, plan, by_key, index, run_id)
    unmatched = [r for r in fresh if r.source_key not in classified]
    entity_ids = dict(plan.assignments)
    fuzzy, candidates = _classify_unmatched(
        conn, unmatched, run_id=run_id, threshold=threshold, batch_entities=entity_ids
    )
    classified.update(fuzzy)
    proposed = write_scored_candidates(conn, candidates, source_id=source_id, run_id=run_id)
    upserted = upsert_records(
        conn,
        source_id=source_id,
        run_id=run_id,
        provenance=provenance,
        records=records,
        entity_ids=entity_ids,
    )
    write_discoveries(conn, classified, entity_ids, source_id=source_id, run_id=run_id)
    per_kind = {kind: 0 for kind in KINDS}
    for c in classified.values():
        per_kind[c.kind] += 1
    logger.info(
        "step=classify new=%d rename_or_move=%d duplicate=%d uncertain=%d",
        *(per_kind[k] for k in KINDS),
    )
    return DiscoveryCounts(
        **per_kind,
        entities_created=len(plan.new_entities),
        identifiers_added=len(plan.new_identifiers),
        strong_id_merges=plan.strong_id_merges,
        candidates_created=len(plan.candidates) + proposed,
        rows_new=len(upserted.new_keys),
        rows_changed=len(upserted.changed_keys),
        rows_unchanged=upserted.unchanged,
    )
