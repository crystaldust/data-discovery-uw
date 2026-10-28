"""Classifying first-seen bylines: new, rename_or_move, duplicate or uncertain.

- attached by an author page to a person known before the run: duplicate when the outlet
  is one the person was already known for (a Wikidata employer at any time, or an earlier
  byline), else rename_or_move: the same person now writes for another outlet;
- attached to a person this run created: duplicate;
- strong identifiers that disagree: uncertain (classify.classify_conflict);
- no strong hit: uncertain when the best name neighbour reaches the threshold, new
  otherwise. A name never merges anything, even an exact one at the same outlet.

Neighbour score (provisional, not calibrated): NAME_WEIGHT * name similarity (pg_trgm)
+ OUTLET_WEIGHT * outlet match (1 when the neighbour is known for the byline's outlet, 0
when it is known for others only, unknown counts as 0). The same name at another outlet
scores 0.7 and reaches the review queue; so does the same name at the same outlet (1.0).
"""

from collections.abc import Iterable, Mapping, Sequence
from types import MappingProxyType
from typing import Any
from uuid import UUID

import psycopg
from psycopg.pq import TransactionStatus

from ddm.ingest.classify import NO_MATCH_METHOD, STRONG_METHOD, Classification, Neighbour
from ddm.ingest.records import NormalizedRecord
from ddm.ingest.resolve import Attachment

# Bump when candidate generation, features or weights change; part of each run's input.
BYLINE_SCORER_VERSION = 1
BYLINE_THRESHOLD = 0.6
NAME_WEIGHT = 0.7
OUTLET_WEIGHT = 0.3
# Below this trigram similarity two names share almost nothing; not worth a lookup.
NAME_FLOOR = 0.3
KNN_LIMIT = 5
QUERY_BATCH = 500
SCORE_DIGITS = 4
BYLINE_FUZZY_METHOD = "byline_name_outlet_v1"

# The outlets each of a person's live journalist records knows them for.
OUTLETS_OF_RECORD = """
CROSS JOIN LATERAL (
  SELECT e->>'outlet_qid' AS outlet
  FROM jsonb_array_elements(
    CASE WHEN s.source_id = (SELECT source_id FROM sources WHERE name = 'wikidata')
         THEN s.attrs->'employments' ELSE '[]'::jsonb END) e
  UNION ALL
  SELECT s.attrs->>'outlet_qid'
  WHERE s.source_id = (SELECT source_id FROM sources WHERE name = 'outlet_rss')
) o
"""
JOURNALIST_RECORDS = """
s.deleted_at IS NULL
AND s.source_id IN (SELECT source_id FROM sources WHERE name IN ('wikidata', 'outlet_rss'))
"""

KNOWN_OUTLETS = f"""
SELECT s.entity_id, array_agg(DISTINCT o.outlet)
FROM source_records s
{OUTLETS_OF_RECORD}
WHERE s.entity_id = ANY(%s) AND o.outlet IS NOT NULL AND {JOURNALIST_RECORDS}
GROUP BY s.entity_id
"""  # noqa: S608 - only module constants are interpolated

NEIGHBOURS = f"""
SELECT q.source_key, n.entity_id, n.name_sim, m.outlet_match
FROM unnest(%(keys)s::text[], %(names)s::text[], %(outlets)s::text[])
     AS q(source_key, name, outlet)
CROSS JOIN LATERAL (
  SELECT e.entity_id, similarity(e.norm_name, norm_text(q.name)) AS name_sim
  FROM entities e
  WHERE e.entity_type = 'journalist' AND e.first_seen_run <> %(run)s
    AND e.norm_name %% norm_text(q.name)
  ORDER BY e.norm_name <-> norm_text(q.name), e.entity_id
  LIMIT %(k)s
) n
LEFT JOIN LATERAL (
  SELECT bool_or(o.outlet = q.outlet)::int AS outlet_match
  FROM source_records s
  {OUTLETS_OF_RECORD}
  WHERE s.entity_id = n.entity_id AND {JOURNALIST_RECORDS}
) m ON true
"""  # noqa: S608 - only module constants are interpolated


def _make(kind: str, matched: UUID | None, method: str, score, evidence) -> Classification:
    return Classification(kind, matched, method, score, MappingProxyType(dict(evidence)))


def classify_byline_attached(
    attachment: Attachment, *, outlet_qid: str, known_outlets: frozenset[str]
) -> Classification:
    within_run = not attachment.existing
    moved = not within_run and outlet_qid not in known_outlets
    evidence = {
        "shared": [list(identifier) for identifier in attachment.shared],
        "outlet": outlet_qid,
        "known_outlets": sorted(known_outlets),
        "outlet_changed": moved,
        "within_run": within_run,
    }
    kind = "rename_or_move" if moved else "duplicate"
    return _make(kind, attachment.entity_id, STRONG_METHOD, None, evidence)


def classify_byline_unmatched(best: Neighbour | None, *, threshold: float) -> Classification:
    evidence: dict[str, Any] = {"best": None}
    if best is None:
        return _make("new", None, NO_MATCH_METHOD, None, evidence)
    evidence["best"] = {"entity_id": str(best.entity_id), "score": best.score, **best.features}
    if best.score >= threshold:
        return _make("uncertain", best.entity_id, BYLINE_FUZZY_METHOD, best.score, evidence)
    return _make("new", None, NO_MATCH_METHOD, best.score, evidence)


def known_outlets(
    conn: psycopg.Connection, entity_ids: Iterable[UUID]
) -> dict[UUID, frozenset[str]]:
    """Outlets each person is known for; people known for none are absent."""
    ids = list(set(entity_ids))
    if not ids:
        return {}
    rows = conn.execute(KNOWN_OUTLETS, (ids,)).fetchall()
    return {entity_id: frozenset(outlets) for entity_id, outlets in rows}


def _neighbour(row: Sequence[Any]) -> Neighbour:
    _, entity_id, name_sim, outlet_match = row
    features = {"name_sim": round(float(name_sim), SCORE_DIGITS), "outlet_match": outlet_match}
    total = NAME_WEIGHT * features["name_sim"] + OUTLET_WEIGHT * (outlet_match or 0)
    return Neighbour(entity_id, round(min(total, 1.0), SCORE_DIGITS), MappingProxyType(features))


def byline_neighbours(
    conn: psycopg.Connection, records: Sequence[NormalizedRecord], *, run_id: int
) -> Mapping[str, tuple[Neighbour, ...]]:
    """Nearest journalists by name per byline record, best first; records with none absent.

    Must run inside the caller's transaction: the trigram floor is set for it only.
    """
    if conn.info.transaction_status != TransactionStatus.INTRANS:
        raise RuntimeError("byline_neighbours must run inside a transaction")
    conn.execute("SELECT set_config('pg_trgm.similarity_threshold', %s, true)", (str(NAME_FLOOR),))
    named = [r for r in records if r.display_name]
    found: dict[str, list[Neighbour]] = {}
    for start in range(0, len(named), QUERY_BATCH):
        chunk = named[start : start + QUERY_BATCH]
        params = {
            "keys": [r.source_key for r in chunk],
            "names": [r.display_name for r in chunk],
            "outlets": [r.attrs.get("outlet_qid") for r in chunk],
            "run": run_id,
            "k": KNN_LIMIT,
        }
        for row in conn.execute(NEIGHBOURS, params).fetchall():
            found.setdefault(row[0], []).append(_neighbour(row))
    return {
        key: tuple(sorted(ns, key=lambda n: (-n.score, str(n.entity_id))))
        for key, ns in found.items()
    }
