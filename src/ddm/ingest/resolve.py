"""Entity resolution on strong identifiers only.

A record whose identifiers hit no entity creates one; a single consistent hit attaches it.
Everything else is kept apart and queued for review, never merged automatically:
- hits on two or more different entities;
- a single hit whose entity already has a different podcast GUID or Apple ID;
- identifier values carried by too many records to mean one entity (the real dump has a
  podcast:guid shared by 320 unrelated feeds), which are not used for matching at all.
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
from ddm.ingest.records import NormalizedRecord, source_key_order

logger = logging.getLogger(__name__)

CONFLICT_METHOD = "strong_id_conflict_v1"
# Marks "strong identifiers disagree"; not comparable with fuzzy-match scores.
CONFLICT_SCORE = 1.0
# An entity has at most one of each; feed URLs are excluded because feeds move.
SINGLE_VALUED_KINDS = frozenset({"podcast_guid", "itunes_id", "wikidata_qid"})
# Genuine duplicates of one podcast rarely exceed a few feeds; more means a shared default.
MAX_RECORDS_PER_IDENTIFIER = 3

Identifier = tuple[str, str]


@dataclass(frozen=True)
class ResolutionPlan:
    assignments: tuple[tuple[str, UUID], ...]
    new_entities: tuple[tuple[UUID, str | None, str], ...]
    new_identifiers: tuple[tuple[str, str, UUID], ...]
    candidates: tuple[tuple[str, UUID, dict[str, Any]], ...]
    strong_id_merges: int


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


def plan_resolution(
    records: Iterable[NormalizedRecord],
    existing: Mapping[Identifier, UUID],
    source_name: str,
    ambiguous: frozenset[Identifier] = frozenset(),
) -> ResolutionPlan:
    index = dict(existing)
    single_values = _single_values(index)
    assignments, new_entities, new_identifiers, candidates = [], [], [], []
    merges = 0
    for record in sorted(records, key=lambda r: source_key_order(r.source_key)):
        usable = [i for i in record.identifiers if i not in ambiguous]
        hits = _hits(usable, index)
        target = next(iter(hits)) if len(hits) == 1 else None
        conflicts = _conflicts(usable, single_values.get(target, {})) if target else []
        if target and not conflicts:
            entity_id = target
            merges += 1
        else:
            entity_id = entity_id_for(source_name, record.source_key)
            new_entities.append((entity_id, record.display_name, record.source_key))
            extra = {"conflicting": conflicts} if conflicts else {}
            candidates.extend(
                (record.source_key, other, {"shared": shared, **extra})
                for other, shared in hits.items()
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
    _copy_rows(
        conn,
        "COPY entity_identifiers (id_kind, id_value, entity_id, source_id, added_run) FROM STDIN",
        ["text", "text", "uuid", "int2", "int8"],
        ((kind, value, eid, source_id, run_id) for kind, value, eid in plan.new_identifiers),
    )
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO candidate_matches
              (source_id, source_key, entity_id, method, score, features, created_run)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (source_id, source_key, entity_id, method) DO NOTHING
            """,
            [
                (source_id, key, eid, CONFLICT_METHOD, CONFLICT_SCORE, Jsonb(features), run_id)
                for key, eid, features in plan.candidates
            ],
        )
    logger.info(
        "step=resolve entities=%d identifiers=%d merges=%d candidates=%d",
        len(plan.new_entities),
        len(plan.new_identifiers),
        plan.strong_id_merges,
        len(plan.candidates),
    )
