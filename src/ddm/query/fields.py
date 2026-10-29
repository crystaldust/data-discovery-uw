"""Which attrs keys of each source may leave the database.

Everything else stays in: above all owner_email and editor_email, which are shown only as
masked contact routes. Comparisons of whole payloads report changed key names, with any
non-public key (contact fields, internal scores) reported as one generic entry.
"""

from collections.abc import Mapping
from typing import Any

OTHER_FIELDS = "other fields"

PUBLIC_ATTRS: Mapping[int, tuple[str, ...]] = {
    # podcastindex_dump
    1: (
        "description",
        "language",
        "categories",
        "episode_count",
        "newest_item_at",
        "oldest_item_at",
        "link",
        "author",
        "owner_name",
        "itunes_type",
        "explicit",
        "feed_url",
        "original_url",
        "image_url",
        "podcast_guid",
        "itunes_id",
    ),
    # podcast_rss
    2: (
        "description",
        "language",
        "categories",
        "episode_count",
        "last_episode_at",
        "oldest_episode_at",
        "website",
        "author",
        "owner_name",
        "itunes_type",
        "explicit",
        "feed_url",
        "new_feed_url",
        "moved_to",
        "image_url",
        "podcast_guid",
    ),
    # podcastindex_api
    3: (
        "description",
        "language",
        "categories",
        "episode_count",
        "newest_item_at",
        "last_update_at",
        "created_on",
        "link",
        "author",
        "owner_name",
        "itunes_type",
        "feed_url",
        "original_url",
        "image_url",
        "podcast_guid",
        "itunes_id",
    ),
    # wikidata
    4: ("employments", "websites", "x_usernames", "author_ids"),
    # outlet_rss
    5: ("outlet", "outlet_qid", "author_url", "latest_byline_at", "bylines"),
}


def public_attrs(source_id: int, attrs: Mapping[str, Any]) -> dict[str, Any]:
    allowed = PUBLIC_ATTRS.get(source_id, ())
    return {key: attrs[key] for key in allowed if key in attrs}


def changed_fields(
    source_id: int, before: Mapping[str, Any], after: Mapping[str, Any]
) -> tuple[str, ...]:
    allowed = set(PUBLIC_ATTRS.get(source_id, ()))
    changed = {key for key in before.keys() | after.keys() if before.get(key) != after.get(key)}
    named = sorted(changed & allowed)
    return (*named, OTHER_FIELDS) if changed - allowed else tuple(named)
