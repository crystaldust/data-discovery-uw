"""Podcast Index API: newly added feeds, full feed lookups and their normalisation.

/recent/newfeeds with `feedid` returns feeds whose id is >= feedid in ascending order
(confirmed against the live API on 2026-10-26), so paging forward from a cursor neither
skips nor repeats. Listings carry no podcast GUID, categories or episode count; a feed
worth keeping is looked up with /podcasts/byfeedid. A listing younger than SETTLE_DAYS may
not have been parsed by Podcast Index yet (empty title, no update time).

Records use the dump's attribute names and identifier order, so both sources resolve and
classify the same way. Unknown values (0, empty strings, null) are left out.
"""

from collections.abc import Iterator, Mapping
from datetime import datetime, timedelta
from typing import Any

from ddm.identifiers import is_ascii_digits, normalize_feed_url, normalize_guid, normalize_itunes_id
from ddm.ingest.records import NormalizedRecord, Reject, make_record, make_reject, safe_raw
from ddm.sources.podcastindex import PodcastIndexClient
from ddm.sources.podcastindex_dump import (
    DEMO_CATEGORIES,
    LANGUAGE_PREFIX,
    MIN_EPISODES,
    clean_text,
    positive_int,
    unix_time,
)

SOURCE_NAME = "podcastindex_api"
# Bump when normalisation changes.
NORMALIZER_VERSION = 1
PAGE_SIZE = 1000
# Podcast Index parses a new feed within hours; two days leaves room for retries.
SETTLE_DAYS = 2
# Feed ids are about 8 million today; anything near 2**53 is not a real id, and the cursor
# and keys must stay exact integers.
MAX_FEED_ID = 2**53 - 1

TEXT_FIELDS = {
    "originalUrl": "original_url",
    "link": "link",
    "description": "description",
    "image": "image_url",
    "author": "author",
    "ownerName": "owner_name",
    "generator": "generator",
    "contentType": "content_type",
    "itunesType": "itunes_type",
    "chash": "source_content_hash",
}
TIME_FIELDS = {"lastUpdateTime": "last_update_at", "newestItemPubdate": "newest_item_at"}


def feed_id_of(feed: Mapping[str, Any]) -> int | None:
    value = feed.get("id")
    if isinstance(value, str) and is_ascii_digits(value):
        value = int(value)
    number = positive_int(value)
    return number if number is not None and number <= MAX_FEED_ID else None


def iter_newfeeds(
    client: PodcastIndexClient, *, start_at: int, page_size: int = PAGE_SIZE
) -> Iterator[dict[str, Any]]:
    """Listings with id >= start_at, ascending, until a page comes back empty."""
    next_id = start_at
    while True:
        body = client.get("/recent/newfeeds", {"feedid": next_id, "max": page_size})
        feeds = body.get("feeds")
        listed = feeds[:page_size] if isinstance(feeds, list) else []
        valid = [{**f, "id": feed_id_of(f)} for f in listed if isinstance(f, dict)]
        page = sorted(
            (f for f in valid if f["id"] is not None and f["id"] >= next_id),
            key=lambda f: f["id"],
        )
        if not page:
            return
        yield from page
        next_id = page[-1]["id"] + 1


def fetch_feed(client: PodcastIndexClient, feed_id: int) -> dict[str, Any] | None:
    feed = client.get("/podcasts/byfeedid", {"id": feed_id}).get("feed")
    return feed if isinstance(feed, dict) and feed else None


def is_english(listed: Mapping[str, Any]) -> bool:
    language = clean_text(listed.get("language"))
    return language is not None and language.lower().startswith(LANGUAGE_PREFIX)


def has_known_age(listed: Mapping[str, Any]) -> bool:
    return unix_time(listed.get("timeAdded")) is not None


def is_settled(listed: Mapping[str, Any], now: datetime) -> bool:
    added = unix_time(listed.get("timeAdded"))
    return added is not None and added <= now - timedelta(days=SETTLE_DAYS)


def _categories(full: Mapping[str, Any]) -> list[str]:
    raw = full.get("categories")
    if not isinstance(raw, Mapping):
        return []
    ordered = sorted(raw.items(), key=lambda item: (len(str(item[0])), str(item[0])))
    seen: dict[str, None] = {}
    for _, name in ordered:
        if (text := clean_text(name)) is not None:
            seen.setdefault(text.lower(), None)
    return list(seen)


def _attrs(listed: Mapping[str, Any], full: Mapping[str, Any], feed_url: str) -> dict[str, Any]:
    attrs: dict[str, Any] = {"feed_url": feed_url}
    for field, key in TEXT_FIELDS.items():
        if (value := clean_text(full.get(field))) is not None:
            attrs[key] = value
    if (language := clean_text(full.get("language"))) is not None:
        attrs["language"] = language.lower()
    if (itunes_id := positive_int(full.get("itunesId"))) is not None:
        attrs["itunes_id"] = itunes_id
    if (guid := normalize_guid(clean_text(full.get("podcastGuid")))) is not None:
        attrs["podcast_guid"] = guid
    count = full.get("episodeCount")
    if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
        attrs["episode_count"] = count
    for field, key in TIME_FIELDS.items():
        if (moment := unix_time(full.get(field))) is not None:
            attrs[key] = moment.isoformat()
    created = unix_time(full.get("createdOn")) or unix_time(listed.get("timeAdded"))
    if created is not None:
        attrs["created_on"] = created.isoformat()
    if categories := _categories(full):
        attrs["categories"] = categories
    return attrs


def _identifiers(
    full: Mapping[str, Any], source_key: str, feed_url_id: str
) -> tuple[tuple[str, str], ...]:
    candidates = (
        ("podcast_guid", normalize_guid(clean_text(full.get("podcastGuid")))),
        ("itunes_id", normalize_itunes_id(full.get("itunesId"))),
        ("podcastindex_id", source_key),
        ("feed_url", feed_url_id),
        ("feed_url", normalize_feed_url(clean_text(full.get("originalUrl")))),
    )
    unique: dict[tuple[str, str], None] = {}
    for kind, value in candidates:
        if value is not None:
            unique.setdefault((kind, value), None)
    return tuple(unique)


def _reject(row_number: int, source_key: str | None, reason: str, listed, full) -> Reject:
    return make_reject(
        row_number=row_number,
        source_key=source_key,
        reason=reason,
        raw={"listed": safe_raw(listed), "feed": safe_raw(full)},
    )


def normalize_api_feed(
    row_number: int, listed: Mapping[str, Any], full: Mapping[str, Any]
) -> NormalizedRecord | Reject:
    feed_id = feed_id_of(listed)
    if feed_id is None:
        return _reject(row_number, None, "invalid_id", listed, full)
    source_key = str(feed_id)
    if feed_id_of(full) not in (None, feed_id):
        return _reject(row_number, source_key, "id_mismatch", listed, full)
    title = clean_text(full.get("title"))
    if title is None and not positive_int(full.get("lastParseTime")):
        return _reject(row_number, source_key, "not_parsed", listed, full)
    feed_url = clean_text(full.get("url"))
    feed_url_id = normalize_feed_url(feed_url)
    if feed_url is None or feed_url_id is None:
        return _reject(row_number, source_key, "invalid_feed_url", listed, full)
    return make_record(
        source_key=source_key,
        display_name=title,
        attrs=_attrs(listed, full, feed_url),
        source_updated_at=unix_time(full.get("lastUpdateTime")),
        identifiers=_identifiers(full, source_key, feed_url_id),
    )


def in_demo_slice(record: NormalizedRecord) -> bool:
    """The dump slice's rules: English, at least MIN_EPISODES, a demo category."""
    attrs = record.attrs
    categories = set(attrs.get("categories") or ())
    return (
        (attrs.get("language") or "").startswith(LANGUAGE_PREFIX)
        and (attrs.get("episode_count") or 0) >= MIN_EPISODES
        and bool(categories & set(DEMO_CATEGORIES))
    )
