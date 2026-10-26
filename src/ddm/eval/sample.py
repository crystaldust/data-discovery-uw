"""Draw the stratified sample of record pairs for hand labelling.

Strata (sizes in STRATA; a stratum smaller than its share is taken whole):
- strong_same_title / strong_diff_title: a record joined to an entity by a strong
  identifier (baseline merges and discovery attachments), paired with the entity's first
  record. Pairs whose titles differ are where wrong merges hide, so they get more labels
  relative to their size.
- strong_conflict: strong identifiers that disagreed and were sent to review.
- unconfirmed_guid: a podcast:guid was the only link and the titles differ (rules v2).
- fuzzy_<lower>: each discovered record's best fuzzy neighbour, by score band.
Within a stratum pairs are ordered by sha256(seed + pair key) and the first n are taken,
so the same database always yields the same sample.

The file is opened in spreadsheets and committed to a public repository, and every text
comes from untrusted feeds. So only public metadata is written; e-mail addresses are
redacted from free text, URLs keep only scheme, host and path (no credentials or tokens),
control characters are dropped, and cells that a spreadsheet would run as a formula get a
leading quote. Neither file is ever overwritten: labels take hours. When a sample is
drawn again (after a rule change), labels of pairs that reappear unchanged can be carried
over from the previous file, matched on the two Podcast Index ids.
"""

import contextlib
import csv
import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import psycopg

from ddm.ingest.classify import FUZZY_METHOD, NO_MATCH_METHOD, norm_title
from ddm.ingest.fuzzy import FUZZY_THRESHOLD, TRGM_FLOOR
from ddm.ingest.resolve import CONFLICT_METHOD, UNCONFIRMED_GUID_METHOD

SEED = "ddm-phase-4"
# Lower bounds of the fuzzy score bands; each runs to the next bound, the last to 1.0.
# Pairs enter by title similarity (TRGM_FLOOR), so a title-only look-alike scores below
# 0.3 and still needs a band of its own.
FUZZY_BANDS = (0.0, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8)
FUZZY_LABELS = 100
STRATA = (
    ("strong_diff_title", 30),
    ("strong_same_title", 50),
    ("strong_conflict", 10),
    ("unconfirmed_guid", 10),
    *(
        (f"fuzzy_{lower}", FUZZY_LABELS // len(FUZZY_BANDS) + (i < FUZZY_LABELS % len(FUZZY_BANDS)))
        for i, lower in enumerate(FUZZY_BANDS)
    ),
)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
FORMULA_PREFIXES = ("=", "+", "-", "@")
MAX_CELL_CHARS = 300
# Readable by everyone, like any file in a working copy (mkstemp starts at 0600).
NEW_FILE_MODE = 0o644
VALID_LABELS = frozenset({"same", "different", "unsure"})
DESCRIPTION_CHARS = 200
PI_URL = "https://podcastindex.org/podcast/{}"
# Sources whose keys are Podcast Index feed ids (the RSS refresh only re-reads them).
PAIR_SOURCES = ("podcastindex_dump", "podcastindex_api")
SIDE_FIELDS = (
    "title",
    "author",
    "owner",
    "website",
    "feed_url",
    "itunes_id",
    "pi_id",
    "newest_item",
    "description",
    "pi_url",
)
COLUMNS = (
    "pair_id",
    "stratum",
    "method",
    "score",
    "title_sim",
    "author_sim",
    "site_match",
    "right_class",
    *(f"left_{f}" for f in SIDE_FIELDS),
    *(f"right_{f}" for f in SIDE_FIELDS),
    "label",
    "note",
)

# First record of each entity among the Podcast Index sources: the entity's face.
REPRESENTATIVES = """
WITH rep AS (
  SELECT DISTINCT ON (r.entity_id) r.entity_id, r.source_id, r.source_key
  FROM source_records r JOIN sources s USING (source_id)
  WHERE r.entity_id IS NOT NULL AND s.name = ANY(%(sources)s)
  ORDER BY r.entity_id, r.first_seen_run, r.source_id, length(r.source_key), r.source_key
)
"""
STRONG_PAIRS = (
    REPRESENTATIVES
    + """
SELECT r.source_id, r.source_key, rep.source_id, rep.source_key, d.classification
FROM source_records r
JOIN sources s USING (source_id)
JOIN rep ON rep.entity_id = r.entity_id
LEFT JOIN discoveries d ON d.source_id = r.source_id AND d.source_key = r.source_key
WHERE s.name = ANY(%(sources)s)
  AND r.source_key <> rep.source_key  -- the same feed seen by two sources is no test
"""
)
CONFLICT_PAIRS = (
    REPRESENTATIVES
    + """
SELECT c.source_id, c.source_key, rep.source_id, rep.source_key, d.classification
FROM candidate_matches c
JOIN sources s ON s.source_id = c.source_id
JOIN rep ON rep.entity_id = c.entity_id
LEFT JOIN discoveries d ON d.source_id = c.source_id AND d.source_key = c.source_key
WHERE c.method = %(method)s AND s.name = ANY(%(sources)s)
"""
)
FUZZY_PAIRS = (
    REPRESENTATIVES
    + """
SELECT d.source_id, d.source_key, rep.source_id, rep.source_key, d.classification,
       d.score, d.evidence->'best'
FROM discoveries d
JOIN rep ON rep.entity_id = (d.evidence->'best'->>'entity_id')::uuid
WHERE d.method = ANY(%(methods)s)
  AND (d.evidence->'best'->>'title_sim')::float8 >= %(floor)s
"""
)
DETAILS = """
SELECT r.source_id, r.source_key, r.display_name, r.attrs
FROM source_records r
JOIN unnest(%s::int2[], %s::text[]) AS q(source_id, source_key)
  ON r.source_id = q.source_id AND r.source_key = q.source_key
"""


class EvalInputError(ValueError):
    """The sample or labels cannot be used as given."""


@dataclass(frozen=True)
class SampleResult:
    pairs: int
    carried: int
    dropped: int = 0  # labelled rows in the carry-over file whose label was not valid


Ref = tuple[int, str]


@dataclass(frozen=True)
class Pair:
    stratum: str
    right: Ref  # the record being judged
    left: Ref  # the existing entity's first record
    right_class: str | None
    method: str
    score: float | None = None
    features: Mapping[str, Any] | None = None

    @property
    def key(self) -> str:
        return f"{self.right[0]}:{self.right[1]}|{self.left[0]}:{self.left[1]}"


def safe_cell(value: Any) -> str:
    """Text a spreadsheet will not execute and a public repository may hold."""
    if value is None:
        return ""
    text = " ".join(CONTROL.sub(" ", str(value)).split())[:MAX_CELL_CHARS]
    text = EMAIL.sub("[email removed]", text)
    return f"'{text}" if text.startswith(FORMULA_PREFIXES) else text


def safe_url(value: Any) -> str:
    """http(s) scheme, host and path only: no credentials, tokens or fragments."""
    if not isinstance(value, str):
        return ""
    try:
        parts = urlsplit(value.strip())
        port = parts.port
    except ValueError:
        return ""
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return ""
    host = parts.hostname if port is None else f"{parts.hostname}:{port}"
    return safe_cell(f"{parts.scheme.lower()}://{host}{parts.path}")


def _band(score: float) -> str:
    lower = max(b for b in FUZZY_BANDS if score >= b)
    return f"fuzzy_{lower}"


def _strong_pairs(conn: psycopg.Connection) -> list[Pair]:
    rows = conn.execute(STRONG_PAIRS, {"sources": list(PAIR_SOURCES)}).fetchall()
    titles = _titles(conn, [(r[0], r[1]) for r in rows] + [(r[2], r[3]) for r in rows])
    pairs = []
    for right_sid, right_key, left_sid, left_key, kind in rows:
        right, left = (right_sid, right_key), (left_sid, left_key)
        same = norm_title(titles.get(right)) == norm_title(titles.get(left))
        stratum = "strong_same_title" if same else "strong_diff_title"
        pairs.append(Pair(stratum, right, left, kind or "baseline_merge", "strong_id"))
    return pairs


def _queued_pairs(conn: psycopg.Connection, stratum: str, method: str) -> list[Pair]:
    params = {"sources": list(PAIR_SOURCES), "method": method}
    return [
        Pair(stratum, (r[0], r[1]), (r[2], r[3]), r[4], method)
        for r in conn.execute(CONFLICT_PAIRS, params).fetchall()
    ]


def _fuzzy_pairs(conn: psycopg.Connection) -> list[Pair]:
    params = {
        "sources": list(PAIR_SOURCES),
        "methods": [FUZZY_METHOD, NO_MATCH_METHOD],
        "floor": TRGM_FLOOR,
    }
    pairs = []
    for right_sid, right_key, left_sid, left_key, kind, score, best in conn.execute(
        FUZZY_PAIRS, params
    ).fetchall():
        rounded = round(float(score), 4)
        right, left = (right_sid, right_key), (left_sid, left_key)
        pairs.append(Pair(_band(rounded), right, left, kind, FUZZY_METHOD, rounded, best))
    return pairs


def _titles(conn: psycopg.Connection, refs: Sequence[Ref]) -> dict[Ref, str | None]:
    return {ref: details[0] for ref, details in _details(conn, refs).items()}


def _details(conn: psycopg.Connection, refs: Sequence[Ref]) -> dict[Ref, tuple]:
    unique = list(dict.fromkeys(refs))
    rows = conn.execute(DETAILS, ([s for s, _ in unique], [k for _, k in unique])).fetchall()
    return {(sid, key): (name, attrs) for sid, key, name, attrs in rows}


def _order(pair: Pair) -> str:
    return hashlib.sha256(f"{SEED}|{pair.key}".encode()).hexdigest()


def draw(pairs: Sequence[Pair]) -> tuple[list[Pair], dict[str, dict[str, int]]]:
    """The first n pairs of each stratum in seeded hash order, and the strata sizes."""
    chosen, strata = [], {}
    for name, size in STRATA:
        members = sorted((p for p in pairs if p.stratum == name), key=_order)
        picked = members[:size]
        chosen.extend(picked)
        strata[name] = {"population": len(members), "sampled": len(picked)}
    return chosen, strata


def _side(ref: Ref, details: Mapping[Ref, tuple]) -> dict[str, str]:
    name, attrs = details.get(ref, (None, {}))
    description = attrs.get("description") or ""
    return {
        "title": safe_cell(name),
        "author": safe_cell(attrs.get("author")),
        "owner": safe_cell(attrs.get("owner_name")),
        "website": safe_url(attrs.get("link")),
        "feed_url": safe_url(attrs.get("feed_url")),
        "itunes_id": safe_cell(attrs.get("itunes_id")),
        "pi_id": safe_cell(ref[1]),
        "newest_item": safe_cell((attrs.get("newest_item_at") or "")[:10]),
        "description": safe_cell(description[:DESCRIPTION_CHARS]),
        "pi_url": PI_URL.format(ref[1]) if ref[1].isascii() and ref[1].isdigit() else "",
    }


def _row(index: int, pair: Pair, details: Mapping[Ref, tuple]) -> dict[str, Any]:
    features = pair.features or {}
    left, right = _side(pair.left, details), _side(pair.right, details)
    return {
        "pair_id": f"p{index:03d}",
        "stratum": pair.stratum,
        "method": pair.method,
        "score": "" if pair.score is None else f"{pair.score:.4f}",
        "title_sim": features.get("title_sim", ""),
        "author_sim": "" if features.get("author_sim") is None else features["author_sim"],
        "site_match": "" if features.get("site_match") is None else features["site_match"],
        "right_class": pair.right_class or "",
        **{f"left_{k}": v for k, v in left.items()},
        **{f"right_{k}": v for k, v in right.items()},
        "label": "",
        "note": "",
    }


def _stage(path: Path, write) -> str:
    """Write to a fresh temp file beside `path` (mkstemp: exclusive, never through a link)."""
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as handle:
            write(handle)
            handle.flush()
            os.fsync(handle.fileno())
        mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else NEW_FILE_MODE
        os.chmod(temporary, mode)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
        raise
    return temporary


def write_atomically(path: Path, write) -> None:
    os.replace(_stage(path, write), path)


def _previous_labels(path: Path) -> tuple[dict[tuple[str, str], tuple[str, str]], int]:
    """(left id, right id) -> (label, note) of each validly labelled pair in an earlier
    sample, and how many labelled rows were dropped because their label is not valid."""
    if not path.is_file():
        raise EvalInputError(f"labels to carry over not found: {path}")
    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            missing = {"left_pi_id", "right_pi_id", "label"} - set(reader.fieldnames or ())
            if missing:
                raise EvalInputError(f"{path.name} lacks columns {sorted(missing)}")
            labelled = [row for row in reader if (row.get("label") or "").strip()]
    except (UnicodeDecodeError, csv.Error) as exc:
        raise EvalInputError(f"cannot read {path.name}: {type(exc).__name__}") from exc
    previous, dropped = {}, 0
    for row in labelled:
        label = row["label"].strip().lower()
        if label not in VALID_LABELS:
            dropped += 1
            continue
        previous[(row["left_pi_id"], row["right_pi_id"])] = (label, row.get("note") or "")
    return previous, dropped


def _carry(rows: list[dict[str, Any]], previous) -> tuple[list[dict[str, Any]], int]:
    carried, out = 0, []
    for row in rows:
        found = previous.get((row["left_pi_id"], row["right_pi_id"]))
        if found and found[0] in VALID_LABELS:
            carried += 1
            out.append({**row, "label": found[0], "note": safe_cell(found[1])})
        else:
            out.append(row)
    return out, carried


def write_sample(
    conn: psycopg.Connection,
    labels_path: Path,
    strata_path: Path,
    carry_from: Path | None = None,
) -> SampleResult:
    """Write the pairs to label and the strata sizes. Both files are staged first and
    renamed together, so a failure never leaves labels without their strata."""
    for path in (labels_path, strata_path):
        if path.exists():
            raise EvalInputError(f"{path} already exists; move it away to draw a new sample")
    previous, dropped = _previous_labels(carry_from) if carry_from is not None else ({}, 0)
    pairs = [
        *_strong_pairs(conn),
        *_queued_pairs(conn, "strong_conflict", CONFLICT_METHOD),
        *_queued_pairs(conn, "unconfirmed_guid", UNCONFIRMED_GUID_METHOD),
        *_fuzzy_pairs(conn),
    ]
    chosen, strata = draw(pairs)
    details = _details(conn, [p.left for p in chosen] + [p.right for p in chosen])
    drawn = [_row(index, pair, details) for index, pair in enumerate(chosen, start=1)]
    rows, carried = _carry(drawn, previous)

    def write_labels(handle) -> None:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(rows)

    meta = {"seed": SEED, "threshold": FUZZY_THRESHOLD, "strata": strata}
    labels_path.parent.mkdir(parents=True, exist_ok=True)
    staged_labels = _stage(labels_path, write_labels)
    staged_strata = _stage(
        strata_path, lambda h: h.write(json.dumps(meta, indent=2, sort_keys=True) + "\n")
    )
    os.replace(staged_strata, strata_path)
    os.replace(staged_labels, labels_path)
    return SampleResult(len(rows), carried, dropped)
