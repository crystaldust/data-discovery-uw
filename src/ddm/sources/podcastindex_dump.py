"""Podcast Index database export (SQLite, MIT licensed): demo slice and normalisation.

The export stores unknown text as empty strings and unknown ids or times as 0.
Normalisation turns all of those into absent values, so unknown stays unknown.
"""

import logging
import math
import sqlite3
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any

from ddm.identifiers import normalize_feed_url, normalize_guid, normalize_itunes_id
from ddm.ingest.records import NormalizedRecord, Reject, make_record, make_reject

logger = logging.getLogger(__name__)

SOURCE_NAME = "podcastindex_dump"
# Bump when normalisation changes, so the same dump is re-imported under the new rules.
NORMALIZER_VERSION = 2
DEMO_CATEGORIES = ("marketing", "technology")
MIN_EPISODES = 3
LANGUAGE_PREFIX = "en"
CATEGORY_COLUMNS = tuple(f"category{i}" for i in range(1, 11))

TEXT_FIELDS = {
    "description": "description",
    "link": "link",
    "itunesAuthor": "author",
    "itunesOwnerName": "owner_name",
    "imageUrl": "image_url",
    "itunesType": "itunes_type",
    "generator": "generator",
    "contentType": "content_type",
    "host": "host",
    "newestEnclosureUrl": "newest_enclosure_url",
    "chash": "source_content_hash",
}
# Counts and Podcast Index's own scores, where 0 is a real value.
INTEGER_FIELDS = {
    "episodeCount": "episode_count",
    "popularityScore": "popularity_score",
    "priority": "priority",
    "updateFrequency": "update_frequency",
}
# Measurements where the export writes 0 for "not known" (no HTTP status is 0).
POSITIVE_INTEGER_FIELDS = {
    "lastHttpStatus": "last_http_status",
    "newestEnclosureDuration": "newest_enclosure_duration",
}
TIME_FIELDS = {
    "lastUpdate": "last_update_at",
    "newestItemPubdate": "newest_item_at",
    "oldestItemPubdate": "oldest_item_at",
    "createdOn": "created_on",
}


@dataclass(frozen=True)
class SliceParams:
    created_before: date
    categories: tuple[str, ...] = DEMO_CATEGORIES
    min_episodes: int = MIN_EPISODES
    language_prefix: str = LANGUAGE_PREFIX


def slice_query(params: SliceParams) -> tuple[str, tuple[Any, ...]]:
    columns = ", ".join(f"lower({column})" for column in CATEGORY_COLUMNS)
    category_filter = " OR ".join(f"lower(?) IN ({columns})" for _ in params.categories)
    # Only column names (module constants) are interpolated; every value is a parameter.
    sql = f"""
        SELECT * FROM podcasts
        WHERE lower(language) LIKE ? || '%'
          AND episodeCount >= ?
          AND createdOn < ?
          AND ({category_filter})
        ORDER BY id
    """  # noqa: S608
    cutoff = int(datetime.combine(params.created_before, time(), UTC).timestamp())
    values = (params.language_prefix, params.min_episodes, cutoff, *params.categories)
    return sql, values


def _decode_text(raw: bytes) -> str:
    # Invalid UTF-8 in one row must not abort the whole read; keep it visible as U+FFFD.
    return raw.decode("utf-8", errors="replace")


def iter_slice_rows(db_path: Path, params: SliceParams) -> Iterator[dict[str, Any]]:
    if not db_path.is_file():
        raise FileNotFoundError(f"dump not found: {db_path}")
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        conn.text_factory = _decode_text
        sql, values = slice_query(params)
        for row in conn.execute(sql, values):
            yield dict(row)
    finally:
        conn.close()


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    # PostgreSQL text and jsonb cannot hold NUL characters.
    return value.replace("\x00", "").strip() or None


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value > 0 else None


def _timestamp(value: Any) -> datetime | None:
    seconds = _positive_int(value)
    if seconds is None:
        return None
    try:
        return datetime.fromtimestamp(seconds, UTC)
    except (ValueError, OverflowError, OSError):
        return None  # e.g. milliseconds written where seconds are expected: unknown, not a guess


def _json_safe(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, str):
        return value.replace("\x00", "")
    return value


def _reject(row_number: int, source_key: str | None, reason: str, row: Mapping[str, Any]):
    raw = {key: _json_safe(value) for key, value in row.items()}
    return make_reject(row_number=row_number, source_key=source_key, reason=reason, raw=raw)


def _categories(row: Mapping[str, Any]) -> list[str]:
    seen: dict[str, None] = {}
    for column in CATEGORY_COLUMNS:
        value = _text(row.get(column))
        if value:
            seen.setdefault(value.lower(), None)
    return list(seen)


def _scalar_attrs(row: Mapping[str, Any]) -> dict[str, Any]:
    attrs: dict[str, Any] = {}
    for column, key in TEXT_FIELDS.items():
        if (value := _text(row.get(column))) is not None:
            attrs[key] = value
    for column, key in INTEGER_FIELDS.items():
        value = row.get(column)
        if isinstance(value, int) and not isinstance(value, bool):
            attrs[key] = value
    for column, key in POSITIVE_INTEGER_FIELDS.items():
        if (number := _positive_int(row.get(column))) is not None:
            attrs[key] = number
    for column, key in TIME_FIELDS.items():
        if (moment := _timestamp(row.get(column))) is not None:
            attrs[key] = moment.isoformat()
    return attrs


def _attrs(row: Mapping[str, Any], feed_url: str) -> dict[str, Any]:
    attrs = {"feed_url": feed_url, **_scalar_attrs(row)}
    # The export cannot tell "not explicit" from "not declared", so only a 1 is recorded.
    if _positive_int(row.get("explicit")) is not None:
        attrs["explicit"] = True
    if (language := _text(row.get("language"))) is not None:
        attrs["language"] = language.lower()
    if (original_url := _text(row.get("originalUrl"))) is not None:
        attrs["original_url"] = original_url
    if (itunes_id := _positive_int(row.get("itunesId"))) is not None:
        attrs["itunes_id"] = itunes_id
    if (guid := normalize_guid(_text(row.get("podcastGuid")))) is not None:
        attrs["podcast_guid"] = guid
    if categories := _categories(row):
        attrs["categories"] = categories
    if (duplicate_of := _positive_int(row.get("duplicateOf"))) is not None:
        attrs["duplicate_of_podcastindex_id"] = duplicate_of
    return attrs


def _identifiers(
    row: Mapping[str, Any], source_key: str, feed_url_id: str
) -> tuple[tuple[str, str], ...]:
    """Strong identifiers in lookup order, without repeats."""
    candidates = (
        ("podcast_guid", normalize_guid(_text(row.get("podcastGuid")))),
        ("itunes_id", normalize_itunes_id(row.get("itunesId"))),
        ("podcastindex_id", source_key),
        ("feed_url", feed_url_id),
        ("feed_url", normalize_feed_url(_text(row.get("originalUrl")))),
    )
    unique: dict[tuple[str, str], None] = {}
    for kind, value in candidates:
        if value is not None:
            unique.setdefault((kind, value), None)
    return tuple(unique)


def normalize_dump_row(row_number: int, row: Mapping[str, Any]) -> NormalizedRecord | Reject:
    row_id = _positive_int(row.get("id"))
    if row_id is None:
        return _reject(row_number, None, "invalid_id", row)
    source_key = str(row_id)
    feed_url = _text(row.get("url"))
    feed_url_id = normalize_feed_url(feed_url)
    if feed_url is None or feed_url_id is None:
        return _reject(row_number, source_key, "invalid_feed_url", row)
    try:
        return make_record(
            source_key=source_key,
            display_name=_text(row.get("title")),
            attrs=_attrs(row, feed_url),
            source_updated_at=_timestamp(row.get("lastUpdate")),
            identifiers=_identifiers(row, source_key, feed_url_id),
        )
    except (ValueError, TypeError, OverflowError):
        # One unexpected value must not abort a 50k-row import; keep the row for inspection.
        logger.warning("row_number=%d source_key=%s normalisation failed", row_number, source_key)
        return _reject(row_number, source_key, "normalization_error", row)
