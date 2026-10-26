"""Fuzzy recall for records no strong identifier matched. Proposes, never merges.

Candidates are the entities whose normalised titles are nearest by trigram distance (GiST
KNN), excluding the current run's own entities: run ids are taken at registration but
data appears at commit, so 'an earlier run id' would miss a later run that committed
first. Each pair gets three features:
- title_sim: pg_trgm similarity of the normalised titles;
- author_sim: best normalised Levenshtein similarity between the two sides' author and
  owner names, at least DM_FLOOR when their Daitch-Mokotoff codes overlap; None when
  either side has no names. Below AUTHOR_MIN it adds nothing to the score (kept as a
  feature, since unrelated names still share some letters);
- site_match: 1 when both sides name the same own website (not a shared hosting platform)
  or the same cover art, 0 when they could be compared and differ, None otherwise.
Unknown features count as 0 in the score: a title-only resemblance stays below the
threshold rather than flooding the review queue. What that misses is measured by the
labelled sample, not assumed away.
"""

from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit

import psycopg
from psycopg.pq import TransactionStatus

from ddm.ingest.classify import Neighbour
from ddm.ingest.records import NormalizedRecord

# Bump when candidate generation, features or weights change; part of each run's input.
SCORER_VERSION = 1
# Provisional until the labelled sample calibrates it (see eval/report.md).
FUZZY_THRESHOLD = 0.6
# Below this trigram similarity two titles share almost nothing; not worth a lookup.
TRGM_FLOOR = 0.3
# Generic titles have many look-alikes; five nearest keep the queue and the query small.
KNN_LIMIT = 5
# Records per neighbour query: large enough to amortise round trips, small enough for Neon.
QUERY_BATCH = 500
TITLE_WEIGHT = 0.55
AUTHOR_WEIGHT = 0.25
SITE_WEIGHT = 0.20
# Two unrelated names still share letters (about 0.2-0.4); below this they add nothing.
AUTHOR_MIN = 0.5
# Names that sound alike (Daitch-Mokotoff) but are spelled differently, e.g. transliterations.
DM_FLOOR = 0.8
SCORE_DIGITS = 4
# Hosts shared by unrelated shows; the same one on both sides says nothing.
SHARED_HOSTS = (
    "anchor.fm",
    "spotify.com",
    "apple.com",
    "buzzsprout.com",
    "libsyn.com",
    "podbean.com",
    "spreaker.com",
    "soundcloud.com",
    "captivate.fm",
    "transistor.fm",
    "simplecast.com",
    "megaphone.fm",
    "redcircle.com",
    "podomatic.com",
    "blubrry.com",
    "youtube.com",
    "google.com",
    "feedburner.com",
    "substack.com",
    "wordpress.com",
    "blogspot.com",
    "squarespace.com",
    "podcastpage.io",
    "podpage.com",
    "riverside.fm",
    "acast.com",
    "audioboom.com",
    "omny.fm",
    "iheart.com",
    "linktr.ee",
)

NEIGHBOURS = """
SELECT q.source_key, n.entity_id, n.title_sim,
       r.attrs->>'link', r.attrs->>'image_url', names.lev_sim, names.dm_overlap
FROM unnest(%(keys)s::text[], %(titles)s::text[], %(authors)s::text[], %(owners)s::text[])
     AS q(source_key, title, author, owner)
CROSS JOIN LATERAL (
  SELECT e.entity_id, similarity(e.norm_name, norm_text(q.title)) AS title_sim
  FROM entities e
  WHERE e.entity_type = 'podcast' AND e.first_seen_run <> %(run)s
    AND e.norm_name %% norm_text(q.title)
  ORDER BY e.norm_name <-> norm_text(q.title), e.entity_id
  LIMIT %(k)s
) n
LEFT JOIN LATERAL (
  SELECT s.attrs FROM source_records s
  WHERE s.entity_id = n.entity_id
  ORDER BY s.first_seen_run, s.source_id, length(s.source_key), s.source_key
  LIMIT 1
) r ON true
LEFT JOIN LATERAL (
  SELECT max(1 - levenshtein(a.v, b.v)::real / greatest(length(a.v), length(b.v), 1))
           AS lev_sim,
         bool_or(daitch_mokotoff(a.v) && daitch_mokotoff(b.v)) AS dm_overlap
  FROM unnest(ARRAY[q.author, q.owner]) AS a0(name)
  CROSS JOIN LATERAL (SELECT left(norm_text(a0.name), 255) AS v) a
  CROSS JOIN unnest(ARRAY[r.attrs->>'author', r.attrs->>'owner_name']) AS b0(name)
  CROSS JOIN LATERAL (SELECT left(norm_text(b0.name), 255) AS v) b
  WHERE a.v <> '' AND b.v <> ''
) names ON true
"""


def site_host(url: str | None) -> str | None:
    if not url:
        return None
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return None
    host = parts.hostname.rstrip(".").lower()
    return host.removeprefix("www.") or None


def _is_shared(host: str) -> bool:
    return any(host == shared or host.endswith(f".{shared}") for shared in SHARED_HOSTS)


def site_match(
    record_link: str | None,
    record_image: str | None,
    entity_link: str | None,
    entity_image: str | None,
) -> int | None:
    if record_image and record_image == entity_image:
        return 1
    a, b = site_host(record_link), site_host(entity_link)
    if a is not None and b is not None:
        return int(a == b and not _is_shared(a))
    if record_image and entity_image:
        return 0
    return None


def author_similarity(lev_sim: float | None, *, dm_overlap: bool | None) -> float | None:
    if lev_sim is None:
        return None
    return round(max(lev_sim, DM_FLOOR) if dm_overlap else lev_sim, SCORE_DIGITS)


def score(title_sim: float, author_sim: float | None, site: int | None) -> float:
    author = author_sim if author_sim is not None and author_sim >= AUTHOR_MIN else 0
    total = TITLE_WEIGHT * title_sim + AUTHOR_WEIGHT * author + SITE_WEIGHT * (site or 0)
    return round(min(total, 1.0), SCORE_DIGITS)


def _neighbour(record: NormalizedRecord, row: Sequence[Any]) -> Neighbour:
    _, entity_id, title_sim, link, image, lev_sim, dm_overlap = row
    features = {
        "title_sim": round(float(title_sim), SCORE_DIGITS),
        "author_sim": author_similarity(lev_sim, dm_overlap=dm_overlap),
        "site_match": site_match(
            record.attrs.get("link"), record.attrs.get("image_url"), link, image
        ),
    }
    total = score(features["title_sim"], features["author_sim"], features["site_match"])
    return Neighbour(entity_id, total, MappingProxyType(features))


def _query(conn: psycopg.Connection, records: Sequence[NormalizedRecord], run_id: int):
    params = {
        "keys": [r.source_key for r in records],
        "titles": [r.display_name for r in records],
        "authors": [r.attrs.get("author") for r in records],
        "owners": [r.attrs.get("owner_name") for r in records],
        "run": run_id,
        "k": KNN_LIMIT,
    }
    return conn.execute(NEIGHBOURS, params).fetchall()


def best_neighbours(
    conn: psycopg.Connection, records: Sequence[NormalizedRecord], *, run_id: int
) -> dict[str, tuple[Neighbour, ...]]:
    """Nearest existing entities per record, best score first; records with none are absent.

    Must run inside the caller's transaction: the trigram floor is set for it only.
    """
    if conn.info.transaction_status != TransactionStatus.INTRANS:
        # Outside one, set_config(..., true) silently does nothing and the floor is ignored.
        raise RuntimeError("best_neighbours must run inside a transaction")
    conn.execute("SELECT set_config('pg_trgm.similarity_threshold', %s, true)", (str(TRGM_FLOOR),))
    by_key: Mapping[str, NormalizedRecord] = {r.source_key: r for r in records}
    titled = [r for r in records if r.display_name]
    found: dict[str, list[Neighbour]] = {}
    for start in range(0, len(titled), QUERY_BATCH):
        for row in _query(conn, titled[start : start + QUERY_BATCH], run_id):
            found.setdefault(row[0], []).append(_neighbour(by_key[row[0]], row))
    return {
        key: tuple(sorted(ns, key=lambda n: (-n.score, str(n.entity_id))))
        for key, ns in found.items()
    }
