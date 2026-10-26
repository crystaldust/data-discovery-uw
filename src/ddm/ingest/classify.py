"""Discovery classification: new, rename_or_move, duplicate or uncertain.

Pure rules over what resolution and fuzzy matching found for one record:
- attached by a strong identifier to an entity known before the run: duplicate when it is
  the same Podcast Index feed, or the same feed address under the same title; otherwise
  rename_or_move (the address or the title changed);
- attached to an entity an earlier record of the same run created: duplicate;
- strong identifiers that disagree: uncertain (resolution queued the conflict);
- no strong hit: uncertain when the best fuzzy neighbour reaches the threshold or Podcast
  Index marks the feed a duplicate of a known one, new otherwise.
Activity is not part of the rules: most baseline entities have never been refreshed.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any
from uuid import UUID

from ddm.ingest.records import norm_title
from ddm.ingest.resolve import Attachment, conflict_method

__all__ = ["norm_title"]  # re-exported: classification and sampling compare titles

# Bump when a rule changes; stored on every discovery row.
CLASSIFIER_VERSION = 1
STRONG_METHOD = "strong_id_v1"
FUZZY_METHOD = "fuzzy_v1"
PI_DUPLICATE_METHOD = "pi_duplicate_of_v1"
NO_MATCH_METHOD = "no_match_v1"


@dataclass(frozen=True)
class Neighbour:
    """An existing entity whose title resembles the record's, with its score and features."""

    entity_id: UUID
    score: float
    features: Mapping[str, Any]


@dataclass(frozen=True)
class Classification:
    kind: str
    matched_entity_id: UUID | None
    method: str
    score: float | None
    evidence: Mapping[str, Any]


def _make(kind: str, matched: UUID | None, method: str, score, evidence) -> Classification:
    return Classification(kind, matched, method, score, MappingProxyType(dict(evidence)))


def _title_changed(record_title: str | None, entity_title: str | None) -> bool | None:
    """None when either title is unknown: unknown is not evidence of a rename."""
    a, b = norm_title(record_title), norm_title(entity_title)
    if a is None or b is None:
        return None
    return a != b


def classify_attached(
    attachment: Attachment,
    *,
    record_feed_url: str | None,
    record_title: str | None,
    entity_feed_urls: frozenset[str],
    entity_title: str | None,
) -> Classification:
    title_changed = _title_changed(record_title, entity_title)
    feed_url_changed = record_feed_url not in entity_feed_urls
    evidence = {
        "shared": [list(identifier) for identifier in attachment.shared],
        "feed_url_changed": feed_url_changed,
        "title_changed": title_changed,
        "within_run": not attachment.existing,
    }
    same_feed = any(kind == "podcastindex_id" for kind, _ in attachment.shared)
    unchanged = not feed_url_changed and title_changed is not True
    kind = "duplicate" if not attachment.existing or same_feed or unchanged else "rename_or_move"
    return _make(kind, attachment.entity_id, STRONG_METHOD, None, evidence)


def classify_conflict(suspect: UUID, features: Mapping[str, Any]) -> Classification:
    method = conflict_method(features)
    return _make("uncertain", suspect, method, None, {"conflict": dict(features)})


def _neighbour_evidence(best: Neighbour | None) -> dict[str, Any] | None:
    if best is None:
        return None
    return {"entity_id": str(best.entity_id), "score": best.score, **dict(best.features)}


def classify_unmatched(
    best: Neighbour | None, *, duplicate_of: UUID | None, threshold: float
) -> Classification:
    evidence = {"best": _neighbour_evidence(best)}
    score = best.score if best is not None else None
    if best is not None and best.score >= threshold:
        return _make("uncertain", best.entity_id, FUZZY_METHOD, score, evidence)
    if duplicate_of is not None:
        return _make("uncertain", duplicate_of, PI_DUPLICATE_METHOD, score, evidence)
    return _make("new", None, NO_MATCH_METHOD, score, evidence)
