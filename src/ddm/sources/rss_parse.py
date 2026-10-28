"""Safe, field-for-field parsing of podcast RSS channels.

defusedxml refuses entity declarations and external references, so a hostile feed cannot
read local files or expand into gigabytes. Each value is taken from exactly one tag, so a
contact address can always be traced to the tag that published it (feedparser merges
itunes:author, itunes:owner and managingEditor into one field). Only direct children of
<channel> are read: an <item>'s itunes:author is not the show's author. Episode content is
not kept; only the item count and the newest and oldest publication dates.
"""

import io
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from xml.etree.ElementTree import Element, ParseError

import defusedxml.ElementTree as safe_et
from defusedxml import DefusedXmlException

# Namespace URIs are compared lower-cased: feeds in the wild spell them in mixed case.
ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"
PODCAST_NAMESPACES = frozenset(
    {
        "https://podcastindex.org/namespace/1.0",
        "https://github.com/podcastindex-org/podcast-namespace/blob/main/docs/1.0.md",
    }
)
# Scheduled or mistyped future dates say nothing about whether a show is active.
FUTURE_TOLERANCE = timedelta(days=1)
EXPLICIT_VALUES = {
    "yes": True,
    "true": True,
    "explicit": True,
    "no": False,
    "false": False,
    "clean": False,
}
PARSE_REASONS = frozenset(
    {"unsafe_xml", "malformed_xml", "unsupported_format", "no_channel", "too_complex"}
)
# Real feeds with thousands of episodes stay well below this; it bounds parser memory.
MAX_ELEMENTS = 250_000
MAX_CATEGORIES = 50
# Untrusted text is capped before it is hashed and stored.
MAX_TEXT_CHARS = 2_000
MAX_DESCRIPTION_CHARS = 10_000


class FeedParseError(ValueError):
    """The body is not a readable RSS feed; `reason` is one of PARSE_REASONS."""

    def __init__(self, reason: str):
        if reason not in PARSE_REASONS:
            raise ValueError(f"unknown parse reason {reason}")
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ParsedChannel:
    title: str | None
    link: str | None
    description: str | None
    language: str | None
    itunes_author: str | None
    owner_name: str | None
    owner_email: str | None
    managing_editor: str | None
    image_url: str | None
    podcast_guid: str | None
    new_feed_url: str | None
    itunes_type: str | None
    explicit: bool | None
    categories: tuple[str, ...]
    episode_count: int
    newest_episode_at: datetime | None
    oldest_episode_at: datetime | None


def _split(tag: object) -> tuple[str, str]:
    """(lower-cased namespace, local name); comments and processing instructions give ''."""
    if not isinstance(tag, str):
        return ("", "")
    if tag.startswith("{"):
        namespace, _, local = tag[1:].partition("}")
        return (namespace.lower(), local)
    return ("", tag)


def _is(element: Element, namespace: str, local: str) -> bool:
    ns, name = _split(element.tag)
    if name != local:
        return False
    return ns in PODCAST_NAMESPACES if namespace == "podcast" else ns == namespace


def clean_text(value: str | None, limit: int = MAX_TEXT_CHARS) -> str | None:
    if value is None:
        return None
    # PostgreSQL text and jsonb cannot hold NUL characters.
    return value.replace("\x00", "").strip()[:limit].strip() or None


def children(parent: Element, namespace: str, local: str) -> Iterator[Element]:
    return (child for child in parent if _is(child, namespace, local))


def first_text(
    parent: Element, namespace: str, local: str, limit: int = MAX_TEXT_CHARS
) -> str | None:
    for child in children(parent, namespace, local):
        if (value := clean_text(child.text, limit)) is not None:
            return value
    return None


def _first_attr(parent: Element, namespace: str, local: str, attr: str) -> str | None:
    for child in children(parent, namespace, local):
        if (value := clean_text(child.get(attr))) is not None:
            return value
    return None


def _owner_field(channel: Element, local: str) -> str | None:
    for owner in children(channel, ITUNES_NS, "owner"):
        if (value := first_text(owner, ITUNES_NS, local)) is not None:
            return value
    return None


def _image_url(channel: Element) -> str | None:
    href = _first_attr(channel, ITUNES_NS, "image", "href")
    if href is not None:
        return href
    for image in children(channel, "", "image"):
        if (url := first_text(image, "", "url")) is not None:
            return url
    return None


def _categories(channel: Element) -> tuple[str, ...]:
    """itunes:category texts depth-first, nested ones included, deduplicated and capped.

    Iterative on purpose: a hostile feed can nest thousands of levels deep.
    """
    found: dict[str, None] = {}
    stack = list(reversed(list(children(channel, ITUNES_NS, "category"))))
    while stack and len(found) < MAX_CATEGORIES:
        category = stack.pop()
        if (text := clean_text(category.get("text"))) is not None:
            found.setdefault(text.lower(), None)
        stack.extend(reversed(list(children(category, ITUNES_NS, "category"))))
    return tuple(found)


def _lower(value: str | None) -> str | None:
    return value.lower() if value is not None else None


def _explicit(channel: Element) -> bool | None:
    value = first_text(channel, ITUNES_NS, "explicit")
    return EXPLICIT_VALUES.get(value.lower()) if value is not None else None


def parse_date(value: str) -> datetime | None:
    try:
        moment = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        try:
            moment = datetime.fromisoformat(value)
        except ValueError:
            return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    try:
        return moment.astimezone(UTC)
    except OverflowError:
        return None  # e.g. year 9999 with a negative offset: unknown, not a reason to reject


def _episode_dates(items: list[Element], fetched_at: datetime) -> list[datetime]:
    latest_allowed = fetched_at + FUTURE_TOLERANCE
    dates = []
    for episode in items:
        raw = first_text(episode, "", "pubDate")
        moment = parse_date(raw) if raw is not None else None
        if moment is not None and moment <= latest_allowed:
            dates.append(moment)
    return dates


def _root_of(body: bytes) -> Element:
    events = safe_et.iterparse(
        io.BytesIO(body),
        events=("start",),
        forbid_dtd=False,
        forbid_entities=True,
        forbid_external=True,
    )
    for count, _ in enumerate(events, start=1):
        if count > MAX_ELEMENTS:
            raise FeedParseError("too_complex")
    return events.root


def parse_xml_root(body: bytes) -> Element:
    """The root of any XML document, parsed safely; raises FeedParseError."""
    try:
        return _root_of(body)
    except DefusedXmlException as exc:
        raise FeedParseError("unsafe_xml") from exc
    except ParseError as exc:
        raise FeedParseError("malformed_xml") from exc


def _channel_of(body: bytes) -> Element:
    root = parse_xml_root(body)
    if _split(root.tag)[1] != "rss":
        raise FeedParseError("unsupported_format")
    channel = next(children(root, "", "channel"), None)
    if channel is None:
        raise FeedParseError("no_channel")
    return channel


def parse_feed(body: bytes, fetched_at: datetime) -> ParsedChannel:
    """Parse one RSS body; raises FeedParseError when it is not a readable RSS feed."""
    channel = _channel_of(body)
    items = list(children(channel, "", "item"))
    dates = _episode_dates(items, fetched_at)
    return ParsedChannel(
        title=first_text(channel, "", "title"),
        link=first_text(channel, "", "link"),
        description=first_text(channel, "", "description", MAX_DESCRIPTION_CHARS)
        or first_text(channel, ITUNES_NS, "summary", MAX_DESCRIPTION_CHARS),
        language=_lower(first_text(channel, "", "language")),
        itunes_author=first_text(channel, ITUNES_NS, "author"),
        owner_name=_owner_field(channel, "name"),
        owner_email=_owner_field(channel, "email"),
        managing_editor=first_text(channel, "", "managingEditor"),
        image_url=_image_url(channel),
        podcast_guid=first_text(channel, "podcast", "guid"),
        new_feed_url=first_text(channel, ITUNES_NS, "new-feed-url"),
        itunes_type=_lower(first_text(channel, ITUNES_NS, "type")),
        explicit=_explicit(channel),
        categories=_categories(channel),
        episode_count=len(items),
        newest_episode_at=max(dates, default=None),
        oldest_episode_at=min(dates, default=None),
    )
