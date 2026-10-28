"""Safe parsing of bylines from outlet RSS 2.0 and Atom feeds.

Only byline metadata is read: author names, author page URIs, the headline, the link and
the date. Article text, summaries and images are never read. Author emails (RSS <author>
is "email (Name)", Atom has <email>) are dropped here so they cannot reach storage.
Outlets put authors in different places, all of which are read:
- RSS: every <dc:creator>, else <author>; some outlets (The Atlantic) embed an Atom
  <atom:author> with <atom:name> and <atom:uri> inside each <item>;
- Atom: <entry><author>, falling back to the <feed>-level author (RFC 4287 4.2.1).
"""

import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit
from xml.etree.ElementTree import Element

from ddm.sources.rss_parse import (
    FUTURE_TOLERANCE,
    MAX_TEXT_CHARS,
    FeedParseError,
    children,
    clean_text,
    parse_date,
    parse_xml_root,
)

# children() compares lower-cased namespace URIs.
DC_NS = "http://purl.org/dc/elements/1.1/"
ATOM_NS = "http://www.w3.org/2005/atom"
ATOM_FEED_TAG = "{http://www.w3.org/2005/atom}feed"
MAX_ITEMS = 200
MAX_TITLE_CHARS = 300
MAX_AUTHOR_CHARS = 200
# "desk@example.com (Jane Roe)": the name in parentheses.
RSS_AUTHOR_NAME = re.compile(r"\((?P<name>[^()]+)\)\s*$")
WEB_SCHEMES = frozenset({"http", "https"})


@dataclass(frozen=True)
class BylineItem:
    authors: tuple[str, ...]  # raw byline texts; splitting "A and B" happens later
    # (name, uri) for each author element that carries both; never paired by position.
    author_uris: tuple[tuple[str, str], ...]
    title: str | None
    link: str | None
    published_at: datetime | None


@dataclass(frozen=True)
class ParsedFeed:
    format: str  # rss | atom
    rights: str | None
    items: tuple[BylineItem, ...]


def _squash(value: str | None, limit: int = MAX_TEXT_CHARS) -> str | None:
    cleaned = clean_text(value)
    return " ".join(cleaned.split())[:limit].strip() or None if cleaned else None


def _texts(parent: Element, namespace: str, local: str, limit: int) -> Iterator[str]:
    for child in children(parent, namespace, local):
        if (value := _squash(child.text, limit)) is not None:
            yield value


def _first(parent: Element, namespace: str, local: str, limit: int = MAX_TEXT_CHARS):
    return next(_texts(parent, namespace, local, limit), None)


def _date(raw: str | None, fetched_at: datetime) -> datetime | None:
    moment = parse_date(raw) if raw is not None else None
    if moment is None or moment > fetched_at + FUTURE_TOLERANCE:
        return None
    return moment


def _without_email(names) -> tuple[str, ...]:
    """Any name that carries an address is dropped whole, wherever the feed put it."""
    return tuple(name for name in names if "@" not in name)


def _rss_author_name(raw: str) -> str | None:
    if "@" not in raw:
        return raw
    match = RSS_AUTHOR_NAME.search(raw)
    return _squash(match.group("name"), MAX_AUTHOR_CHARS) if match else None


def _web_link(value: str | None) -> str | None:
    """Only http(s) addresses with a host: a javascript: link must never be stored."""
    if value is None:
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    return value if parts.scheme.lower() in WEB_SCHEMES and parts.hostname else None


def _atom_people(parent: Element) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...]]:
    """Names of the <author> children of an entry, a feed or an RSS item, and (name, uri)
    for those that carry both, so a URI is never matched to someone else's name."""
    names, pairs = [], []
    for author in children(parent, ATOM_NS, "author"):
        name = _first(author, ATOM_NS, "name", MAX_AUTHOR_CHARS)
        if name is None or "@" in name:
            continue
        names.append(name)
        if (uri := _first(author, ATOM_NS, "uri")) is not None:
            pairs.append((name, uri))
    return tuple(names), tuple(pairs)


def _rss_item(item: Element, fetched_at: datetime) -> BylineItem:
    names, uris = _atom_people(item)
    if not names:
        names = _without_email(_texts(item, DC_NS, "creator", MAX_AUTHOR_CHARS))
    if not names:
        raw = (_rss_author_name(t) for t in _texts(item, "", "author", MAX_AUTHOR_CHARS))
        names = _without_email(name for name in raw if name)
    return BylineItem(
        authors=names,
        author_uris=uris,
        title=_first(item, "", "title", MAX_TITLE_CHARS),
        link=_web_link(_first(item, "", "link")),
        published_at=_date(_first(item, "", "pubDate") or _first(item, DC_NS, "date"), fetched_at),
    )


def _atom_link(entry: Element) -> str | None:
    for link in children(entry, ATOM_NS, "link"):
        if link.get("rel", "alternate") == "alternate":
            if (href := _web_link(_squash(link.get("href")))) is not None:
                return href
    return None


def _atom_entry(entry: Element, fallback, fetched_at: datetime) -> BylineItem:
    names, uris = _atom_people(entry)
    if not names:
        names, uris = fallback
    raw_date = _first(entry, ATOM_NS, "published") or _first(entry, ATOM_NS, "updated")
    return BylineItem(
        authors=names,
        author_uris=uris,
        title=_first(entry, ATOM_NS, "title", MAX_TITLE_CHARS),
        link=_atom_link(entry),
        published_at=_date(raw_date, fetched_at),
    )


def _is_rss(root: Element) -> bool:
    return isinstance(root.tag, str) and root.tag.rpartition("}")[2] == "rss"


def parse_bylines(body: bytes, fetched_at: datetime) -> ParsedFeed:
    """Bylines of one feed body; raises FeedParseError when it is neither RSS nor Atom."""
    root = parse_xml_root(body)
    if _is_rss(root):
        channel = next(children(root, "", "channel"), None)
        if channel is None:
            return ParsedFeed(format="rss", rights=None, items=())
        items = list(children(channel, "", "item"))[:MAX_ITEMS]
        return ParsedFeed(
            format="rss",
            rights=_first(channel, "", "copyright"),
            items=tuple(_rss_item(item, fetched_at) for item in items),
        )
    if isinstance(root.tag, str) and root.tag.lower() == ATOM_FEED_TAG:
        fallback = _atom_people(root)
        entries = list(children(root, ATOM_NS, "entry"))[:MAX_ITEMS]
        return ParsedFeed(
            format="atom",
            rights=_first(root, ATOM_NS, "rights"),
            items=tuple(_atom_entry(entry, fallback, fetched_at) for entry in entries),
        )
    raise FeedParseError("unsupported_format")
