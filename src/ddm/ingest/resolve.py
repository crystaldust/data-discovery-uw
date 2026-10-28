"""Entity resolution on strong identifiers only.

A record whose identifiers hit no entity creates one; a single consistent hit attaches it.
Everything else is kept apart and queued for review, never merged automatically:
- hits on two or more different entities;
- a single hit whose entity already has a different podcast GUID or Apple ID;
- identifier values carried by too many records to mean one entity (the real dump has a
  podcast:guid shared by 320 unrelated feeds), which are not used for matching at all;
- a podcast:guid that is the only shared identifier, when the titles differ: publishers
  who start a show by copying another one copy its GUID too. In the labelled sample 24 of
  25 wrong automatic merges were exactly this (rules v2). Such a match needs the same
  normalised title, an Apple ID, a feed address or a Podcast Index id to merge; otherwise
  it is queued with 'unconfirmed_guid' in its features.
Fuzzy matching lives elsewhere.
"""

import logging
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from ddm.identifiers import entity_id_for
from ddm.ingest.records import NormalizedRecord, norm_title, source_key_order

logger = logging.getLogger(__name__)

CONFLICT_METHOD = "strong_id_conflict_v1"
# A podcast:guid was the only link and the titles differ (rules v2): queued, not merged.
UNCONFIRMED_GUID_METHOD = "unconfirmed_guid_v1"
# Marks "strong identifiers disagree"; not comparable with fuzzy-match scores.
CONFLICT_SCORE = 1.0
# An entity has at most one of each; feed URLs are excluded because feeds move.
SINGLE_VALUED_KINDS = frozenset({"podcast_guid", "itunes_id", "wikidata_qid"})
# Genuine duplicates of one podcast rarely exceed a few feeds; more means a shared default.
MAX_RECORDS_PER_IDENTIFIER = 3
# Bump when a merge rule changes; part of every import's and discovery's input.
RESOLUTION_RULES = 2
# Identifiers that confirm a podcast:guid match on their own. outlet_author_url only
# appears on journalist records, so podcast resolution is unchanged and RESOLUTION_RULES
# stays 2.
CONFIRMING_KINDS = frozenset(
    {"itunes_id", "feed_url", "podcastindex_id", "wikidata_qid", "outlet_author_url"}
)

Identifier = tuple[str, str]


@dataclass(frozen=True)
class Attachment:
    """A record joined to an entity by a strong identifier; `existing` means the entity
    was known before this batch rather than created by an earlier record in it."""

    source_key: str
    entity_id: UUID
    shared: tuple[Identifier, ...]
    existing: bool


@dataclass(frozen=True)
class Candidate:
    """A pair queued for review, with the method that proposed it and its score."""

    source_key: str
    entity_id: UUID
    method: str
    score: float
    features: Mapping[str, Any]


@dataclass(frozen=True)
class ResolutionPlan:
    assignments: tuple[tuple[str, UUID], ...]
    new_entities: tuple[tuple[UUID, str | None, str], ...]
    new_identifiers: tuple[tuple[str, str, UUID], ...]
    candidates: tuple[tuple[str, UUID, dict[str, Any]], ...]
    strong_id_merges: int
    attached: tuple[Attachment, ...] = ()


@dataclass(frozen=True)
class AttachmentPlan:
    new_identifiers: tuple[tuple[str, str, UUID], ...]
    candidates: tuple[tuple[str, UUID, dict[str, Any]], ...]
    single_value_conflicts: int


def find_ambiguous_identifiers(records: Iterable[NormalizedRecord]) -> dict[Identifier, int]:
    counts = Counter(i for r in records for i in set(r.identifiers))
    return {i: n for i, n in counts.items() if n > MAX_RECORDS_PER_IDENTIFIER}


def _hits(identifiers: Iterable[Identifier], index: Mapping[Identifier, UUID]) -> dict[UUID, list]:
    """Entities hit, in identifier order, with the identifiers each one shares."""
    hits: dict[UUID, list] = {}
    for kind, value in identifiers:
        entity_id = index.get((kind, value))
        if entity_id is not None:
            hits.setdefault(entity_id, []).append([kind, value])
    return hits


def _single_values(index: Mapping[Identifier, UUID]) -> dict[UUID, dict[str, str]]:
    values: dict[UUID, dict[str, str]] = {}
    for (kind, value), entity_id in index.items():
        if kind in SINGLE_VALUED_KINDS:
            values.setdefault(entity_id, {}).setdefault(kind, value)
    return values


def _conflicts(identifiers: Iterable[Identifier], entity_values: Mapping[str, str]) -> list:
    return [
        [kind, entity_values[kind], value]
        for kind, value in identifiers
        if kind in entity_values and entity_values[kind] != value
    ]


def _guid_only(shared: list) -> bool:
    return not any(kind in CONFIRMING_KINDS for kind, _ in shared)


def _title_of(entity_titles: Mapping[UUID, str | None], entity_id: UUID) -> str | None:
    if entity_id not in entity_titles:
        # Without it every GUID-only match would look unconfirmed: fail loudly instead.
        raise ValueError(f"no title known for entity {entity_id}; pass titles= for `existing`")
    return entity_titles[entity_id]


def _same_title(a: str | None, b: str | None) -> bool:
    na, nb = norm_title(a), norm_title(b)
    return na is not None and na == nb


def _narrow(hits: dict[UUID, list], title: str | None, entity_titles) -> dict[UUID, list]:
    """GUID-only hits give way to an entity a confirming identifier hits, unless the titles
    agree too (then it is a genuine conflict and stays for review)."""
    if all(_guid_only(shared) for shared in hits.values()):
        return hits
    return {
        e: shared
        for e, shared in hits.items()
        if not _guid_only(shared) or _same_title(title, _title_of(entity_titles, e))
    }


def _split_sibling(shared: list, title: str | None, splits, entity_titles) -> UUID | None:
    """An entity this batch already split off under the same GUID, with the same title."""
    for kind, value in shared:
        for entity_id in splits.get((kind, value), ()):
            if _same_title(title, entity_titles[entity_id]):
                return entity_id
    return None


def plan_resolution(
    records: Iterable[NormalizedRecord],
    existing: Mapping[Identifier, UUID],
    source_name: str,
    ambiguous: frozenset[Identifier] = frozenset(),
    titles: Mapping[UUID, str | None] | None = None,
) -> ResolutionPlan:
    """`titles` are the display names of the entities in `existing`.

    A GUID stays owned by the entity that first had it. Within one batch a record split off
    under a copied GUID can still be joined by later records with its title; across runs
    such a record is only compared with the first owner (known limitation, never a wrong
    merge: the pair goes to review).
    """
    index = dict(existing)
    known = frozenset(existing.values())
    entity_titles = dict(titles or {})
    single_values = _single_values(index)
    splits: dict[Identifier, list[UUID]] = {}
    assignments, new_entities, new_identifiers, candidates, attached = [], [], [], [], []
    merges = 0
    for record in sorted(records, key=lambda r: source_key_order(r.source_key)):
        usable = [i for i in record.identifiers if i not in ambiguous]
        all_hits = _hits(usable, index)
        hits = _narrow(all_hits, record.display_name, entity_titles)
        target = next(iter(hits)) if len(hits) == 1 else None
        conflicts = _conflicts(usable, single_values.get(target, {})) if target else []
        unconfirmed = (
            bool(target)
            and _guid_only(hits[target])
            and not _same_title(record.display_name, _title_of(entity_titles, target))
        )
        if unconfirmed and not conflicts:
            sibling = _split_sibling(hits[target], record.display_name, splits, entity_titles)
            if sibling is not None:
                hits, target, unconfirmed = {sibling: hits[target]}, sibling, False
        if target and not conflicts and not unconfirmed:
            entity_id = target
            merges += 1
            shared = tuple((kind, value) for kind, value in hits[target])
            attached.append(Attachment(record.source_key, target, shared, target in known))
        else:
            entity_id = entity_id_for(source_name, record.source_key)
            new_entities.append((entity_id, record.display_name, record.source_key))
            entity_titles[entity_id] = record.display_name
            extra = {"conflicting": conflicts} if conflicts else {}
            if unconfirmed and not conflicts:
                extra = {"unconfirmed_guid": True}
                for identifier in hits[target]:
                    splits.setdefault(tuple(identifier), []).append(entity_id)
            # Narrowing only decides whether to merge; a reviewer sees every entity hit.
            queued = all_hits if conflicts or len(hits) > 1 else hits
            candidates.extend(
                (record.source_key, other, {"shared": shared, **extra})
                for other, shared in queued.items()
            )
        for kind, value in usable:
            if (kind, value) not in index:
                index[(kind, value)] = entity_id
                new_identifiers.append((kind, value, entity_id))
                if kind in SINGLE_VALUED_KINDS:
                    single_values.setdefault(entity_id, {}).setdefault(kind, value)
        assignments.append((record.source_key, entity_id))
    return ResolutionPlan(
        assignments=tuple(assignments),
        new_entities=tuple(new_entities),
        new_identifiers=tuple(new_identifiers),
        candidates=tuple(candidates),
        strong_id_merges=merges,
        attached=tuple(attached),
    )


def plan_identifier_attachments(
    records: Iterable[NormalizedRecord],
    entity_by_key: Mapping[str, UUID],
    existing: Mapping[Identifier, UUID],
    observed_in: str,
    ambiguous: frozenset[Identifier] = frozenset(),
) -> AttachmentPlan:
    """Identifiers seen on records whose entity is already known: append, never re-merge.

    A new identifier joins the record's entity unless it is a second value of a
    single-valued kind (counted, skipped). One already owned by another entity is queued
    as a candidate for review, as in plan_resolution.
    """
    index = dict(existing)
    single_values = _single_values(index)
    new_identifiers, candidates = [], []
    conflicts = 0
    for record in sorted(records, key=lambda r: source_key_order(r.source_key)):
        entity_id = entity_by_key[record.source_key]
        shared: dict[UUID, list] = {}
        for kind, value in record.identifiers:
            if (kind, value) in ambiguous:
                continue
            owner = index.get((kind, value))
            if owner is None:
                known = single_values.get(entity_id, {})
                if kind in SINGLE_VALUED_KINDS and known.get(kind, value) != value:
                    conflicts += 1
                    continue
                index[(kind, value)] = entity_id
                new_identifiers.append((kind, value, entity_id))
                if kind in SINGLE_VALUED_KINDS:
                    single_values.setdefault(entity_id, {})[kind] = value
            elif owner != entity_id:
                shared.setdefault(owner, []).append([kind, value])
        candidates.extend(
            (record.source_key, other, {"shared": pairs, "observed_in": observed_in})
            for other, pairs in shared.items()
        )
    return AttachmentPlan(
        new_identifiers=tuple(new_identifiers),
        candidates=tuple(candidates),
        single_value_conflicts=conflicts,
    )


def load_identifier_index(
    conn: psycopg.Connection, pairs: Iterable[Identifier]
) -> dict[Identifier, UUID]:
    """Identifiers of every entity this batch hits (all of them, for the consistency check)."""
    unique = list(dict.fromkeys(pairs))
    if not unique:
        return {}
    kinds = [kind for kind, _ in unique]
    values = [value for _, value in unique]
    rows = conn.execute(
        """
        WITH hit AS (
          SELECT DISTINCT i.entity_id
          FROM entity_identifiers i
          JOIN unnest(%s::text[], %s::text[]) AS q(id_kind, id_value)
            ON i.id_kind = q.id_kind AND i.id_value = q.id_value
        )
        SELECT i.id_kind, i.id_value, i.entity_id
        FROM entity_identifiers i JOIN hit USING (entity_id)
        """,
        (kinds, values),
    ).fetchall()
    return {(kind, value): entity_id for kind, value, entity_id in rows}


def load_entity_titles(
    conn: psycopg.Connection, entity_ids: Iterable[UUID]
) -> dict[UUID, str | None]:
    """Display names of the entities a batch hits, for confirming GUID-only matches."""
    ids = list(set(entity_ids))
    if not ids:
        return {}
    rows = conn.execute(
        "SELECT entity_id, display_name FROM entities WHERE entity_id = ANY(%s)", (ids,)
    ).fetchall()
    return dict(rows)


def load_ambiguous_identifiers(conn: psycopg.Connection) -> frozenset[Identifier]:
    rows = conn.execute("SELECT id_kind, id_value FROM ambiguous_identifiers").fetchall()
    return frozenset((kind, value) for kind, value in rows)


def write_ambiguous_identifiers(
    conn: psycopg.Connection, found: Mapping[Identifier, int], *, source_id: int, run_id: int
) -> None:
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO ambiguous_identifiers
              (id_kind, id_value, record_count, source_id, first_run)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (id_kind, id_value) DO NOTHING
            """,
            [(kind, value, count, source_id, run_id) for (kind, value), count in found.items()],
        )


def _copy_rows(conn: psycopg.Connection, statement: str, types: Sequence[str], rows) -> None:
    with conn.cursor() as cur, cur.copy(statement) as copy:
        copy.set_types(list(types))
        for row in rows:
            copy.write_row(row)


def write_identifiers(
    conn: psycopg.Connection,
    rows: Iterable[tuple[str, str, UUID]],
    *,
    source_id: int,
    run_id: int,
) -> None:
    _copy_rows(
        conn,
        "COPY entity_identifiers (id_kind, id_value, entity_id, source_id, added_run) FROM STDIN",
        ["text", "text", "uuid", "int2", "int8"],
        ((kind, value, eid, source_id, run_id) for kind, value, eid in rows),
    )


def conflict_method(features: Mapping[str, Any]) -> str:
    return UNCONFIRMED_GUID_METHOD if features.get("unconfirmed_guid") else CONFLICT_METHOD


def write_candidates(
    conn: psycopg.Connection,
    candidates: Iterable[tuple[str, UUID, dict[str, Any]]],
    *,
    source_id: int,
    run_id: int,
) -> int:
    """Queue strong-identifier conflicts for review; returns how many were new."""
    return write_scored_candidates(
        conn,
        (Candidate(key, eid, conflict_method(f), CONFLICT_SCORE, f) for key, eid, f in candidates),
        source_id=source_id,
        run_id=run_id,
    )


def write_scored_candidates(
    conn: psycopg.Connection,
    candidates: Iterable[Candidate],
    *,
    source_id: int,
    run_id: int,
) -> int:
    """Queue candidates for review; returns how many were new (repeats are ignored)."""
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO candidate_matches
              (source_id, source_key, entity_id, method, score, features, created_run)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (source_id, source_key, entity_id, method) DO NOTHING
            """,
            [
                (
                    source_id,
                    c.source_key,
                    c.entity_id,
                    c.method,
                    c.score,
                    Jsonb(dict(c.features)),
                    run_id,
                )
                for c in candidates
            ],
        )
        return max(cur.rowcount, 0)


def apply_resolution(
    conn: psycopg.Connection,
    plan: ResolutionPlan,
    *,
    source_id: int,
    run_id: int,
    entity_type: str,
    first_seen_kind: str,
) -> None:
    """Write new entities, identifiers and candidates inside the caller's transaction.

    Record-to-entity assignments are not written here: the upsert stores them with the
    row itself, so resolving never rewrites every source_records row a second time.
    """
    _copy_rows(
        conn,
        "COPY entities (entity_id, entity_type, display_name, first_seen_kind, first_seen_run)"
        " FROM STDIN",
        ["uuid", "text", "text", "text", "int8"],
        ((eid, entity_type, name, first_seen_kind, run_id) for eid, name, _ in plan.new_entities),
    )
    write_identifiers(conn, plan.new_identifiers, source_id=source_id, run_id=run_id)
    write_candidates(conn, plan.candidates, source_id=source_id, run_id=run_id)
    logger.info(
        "step=resolve entities=%d identifiers=%d merges=%d candidates=%d",
        len(plan.new_entities),
        len(plan.new_identifiers),
        plan.strong_id_merges,
        len(plan.candidates),
    )


def apply_attachments(
    conn: psycopg.Connection, plan: AttachmentPlan, *, source_id: int, run_id: int
) -> int:
    """Write attached identifiers and candidates in the caller's transaction.

    Returns the number of candidates that were new: a conflict seen again on the next
    refresh is already queued and must not be counted twice.
    """
    write_identifiers(conn, plan.new_identifiers, source_id=source_id, run_id=run_id)
    created = write_candidates(conn, plan.candidates, source_id=source_id, run_id=run_id)
    logger.info(
        "step=attach identifiers=%d candidates=%d single_value_conflicts=%d",
        len(plan.new_identifiers),
        created,
        plan.single_value_conflicts,
    )
    return created
