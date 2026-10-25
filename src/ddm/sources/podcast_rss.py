"""Normalise a fetched podcast RSS channel into a podcast_rss source record.

The record is keyed by the Podcast Index feed id, the same key as the baseline record, so
a feed that moves keeps one record. Fetch bookkeeping (ETag, Last-Modified, status) stays
out of attrs: a new validator must not count as a content change, and neither may
lastBuildDate, which many hosts bump on every render. Activity is derived at read time
from last_episode_at, never stored, because it changes with the clock, not the feed.
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.utils import getaddresses
from typing import Any
from urllib.parse import urlsplit

from ddm.identifiers import normalize_feed_url, normalize_guid
from ddm.ingest.records import NormalizedRecord, Reject, make_record, make_reject
from ddm.sources.rss_fetch import FetchResult
from ddm.sources.rss_parse import FeedParseError, ParsedChannel, parse_feed

logger = logging.getLogger(__name__)

SOURCE_NAME = "podcast_rss"
# Bump when normalisation changes; recorded in each refresh run's params.
NORMALIZER_VERSION = 1
ACTIVE_WITHIN = timedelta(days=90)
DORMANT_WITHIN = timedelta(days=365)
MAX_EMAIL_LENGTH = 254
MAX_EMAIL_LOCAL_LENGTH = 64
DOMAIN_LABEL = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?")
# RFC 5322 dot-atom without quoting: printable specials allowed, no leading, trailing or
# doubled dots. Quoted local parts are legal but too rare in feeds to be worth accepting.
LOCAL_PART = re.compile(r"[a-z0-9!#$%&'*+/=?^_`{|}~-]+(\.[a-z0-9!#$%&'*+/=?^_`{|}~-]+)*")
MAILTO = "mailto:"
WEB_SCHEMES = frozenset({"http", "https"})

OWNER_BASIS = "published in the show's own feed (itunes:owner)"
EDITOR_BASIS = "published in the show's own feed (managingEditor)"
WEBSITE_BASIS = "channel link in the show's own feed"
EMAIL_PURPOSE_BASIS = "itunes:owner is the directory contact; the feed states no purpose"
WEBSITE_PURPOSE_BASIS = "show website"


@dataclass(frozen=True)
class ContactRoute:
    """One published way to reach a show. Deliverability is never claimed here."""

    route_type: str
    value: str
    syntax_valid: bool
    affiliation_status: str
    affiliation_basis: str
    purpose: str
    purpose_basis: str


def extract_email(raw: str | None) -> str | None:
    """The first valid address in 'Name <a@b>', 'a@b (Name)', 'mailto:a@b' or a list.

    Lower-cased; None when no valid address is present, so junk never reaches attrs.
    """
    if not raw:
        return None
    # Feeds separate several addresses with commas or semicolons.
    for _, address in getaddresses([raw.replace(";", ",")]):
        address = address.strip().lower().removeprefix(MAILTO)
        if is_valid_email(address):
            return address
    return None


def is_valid_email(value: str) -> bool:
    """Syntax only; says nothing about whether mail would be delivered."""
    if len(value) > MAX_EMAIL_LENGTH or value.count("@") != 1:
        return False
    local, domain = value.lower().split("@")
    if len(local) > MAX_EMAIL_LOCAL_LENGTH or not LOCAL_PART.fullmatch(local):
        return False
    labels = domain.split(".")
    return len(labels) >= 2 and all(DOMAIN_LABEL.fullmatch(label) for label in labels)


def _is_web_url(value: str) -> bool:
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    return parts.scheme.lower() in WEB_SCHEMES and bool(parts.hostname)


def activity_status(last_episode_at: datetime | None, as_of: datetime) -> str:
    if last_episode_at is None:
        return "unknown"
    age = as_of - last_episode_at
    if age <= ACTIVE_WITHIN:
        return "active"
    if age <= DORMANT_WITHIN:
        return "dormant"
    return "dead"


def _moved_to(fetch: FetchResult) -> str | None:
    if not fetch.moved_permanently:
        return None
    if normalize_feed_url(fetch.final_url) == normalize_feed_url(fetch.requested_url):
        return None  # http -> https or a trailing slash is the same feed
    return fetch.final_url


def _attrs(channel: ParsedChannel, fetch: FetchResult) -> dict[str, Any]:
    candidates = {
        "feed_url": fetch.requested_url,
        "moved_to": _moved_to(fetch),
        "new_feed_url": channel.new_feed_url,
        # Kept only when it is a web address: it may later be rendered as a link.
        "website": channel.link if channel.link and _is_web_url(channel.link) else None,
        "description": channel.description,
        "language": channel.language,
        "author": channel.itunes_author,
        "owner_name": channel.owner_name,
        "owner_email": extract_email(channel.owner_email),
        "editor_email": extract_email(channel.managing_editor),
        "image_url": channel.image_url,
        "itunes_type": channel.itunes_type,
        "categories": list(channel.categories) or None,
        "explicit": channel.explicit,
        "podcast_guid": normalize_guid(channel.podcast_guid),
        "episode_count": channel.episode_count,
        "last_episode_at": _iso(channel.newest_episode_at),
        "oldest_episode_at": _iso(channel.oldest_episode_at),
    }
    return {key: value for key, value in candidates.items() if value is not None}


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment is not None else None


def _identifiers(attrs: dict[str, Any], source_key: str) -> tuple[tuple[str, str], ...]:
    """Strong identifiers in lookup order, without repeats."""
    candidates = (
        ("podcast_guid", attrs.get("podcast_guid")),
        ("podcastindex_id", source_key),
        ("feed_url", normalize_feed_url(attrs["feed_url"])),
        ("feed_url", normalize_feed_url(attrs.get("moved_to"))),
        ("feed_url", normalize_feed_url(attrs.get("new_feed_url"))),
    )
    unique: dict[tuple[str, str], None] = {}
    for kind, value in candidates:
        if value is not None:
            unique.setdefault((kind, value), None)
    return tuple(unique)


def _reject(row_number: int, source_key: str, reason: str, fetch: FetchResult) -> Reject:
    # The body is not kept: it is large and it is the publisher's content.
    raw = {
        "url": fetch.requested_url,
        "final_url": fetch.final_url,
        "http_status": fetch.status,
        "content_type": fetch.content_type,
        "bytes": len(fetch.body or b""),
        "reason": reason,
    }
    return make_reject(row_number=row_number, source_key=source_key, reason=reason, raw=raw)


def normalize_feed(
    row_number: int, source_key: str, fetch: FetchResult
) -> NormalizedRecord | Reject:
    if fetch.outcome != "fetched" or fetch.body is None:
        raise ValueError("only fetched results with a body can be normalised")
    try:
        channel = parse_feed(fetch.body, fetch.attempted_at)
        attrs = _attrs(channel, fetch)
        return make_record(
            source_key=source_key,
            display_name=channel.title,
            attrs=attrs,
            source_updated_at=channel.newest_episode_at,
            identifiers=_identifiers(attrs, source_key),
        )
    except FeedParseError as exc:
        return _reject(row_number, source_key, exc.reason, fetch)
    except (ValueError, TypeError, OverflowError, RecursionError):
        # One unexpected value must not abort a refresh; keep the facts for inspection.
        logger.warning("row_number=%d source_key=%s normalisation failed", row_number, source_key)
        return _reject(row_number, source_key, "normalization_error", fetch)


def _email_route(route_type: str, value: str, basis: str) -> ContactRoute:
    return ContactRoute(
        route_type=route_type,
        value=value,
        syntax_valid=is_valid_email(value),  # always True today: invalid ones are dropped
        affiliation_status="confirmed",
        affiliation_basis=basis,
        purpose="unknown",
        purpose_basis=EMAIL_PURPOSE_BASIS,
    )


def contact_routes_for(record: NormalizedRecord) -> tuple[ContactRoute, ...]:
    attrs = record.attrs
    routes = []
    if (owner := attrs.get("owner_email")) is not None:
        routes.append(_email_route("feed_owner_email", owner, OWNER_BASIS))
    if (editor := attrs.get("editor_email")) is not None:
        routes.append(_email_route("feed_editor_email", editor, EDITOR_BASIS))
    if (website := attrs.get("website")) is not None:
        routes.append(
            ContactRoute(
                route_type="website",
                value=website,
                syntax_valid=True,  # attrs only keep http(s) websites
                affiliation_status="confirmed",
                affiliation_basis=WEBSITE_BASIS,
                purpose="general",
                purpose_basis=WEBSITE_PURPOSE_BASIS,
            )
        )
    return tuple(routes)
