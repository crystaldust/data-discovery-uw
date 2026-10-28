"""Outlet RSS bylines: which feeds, who is a person, and one record per author per outlet.

A record is "this person's recent bylines at this outlet", keyed by the outlet's author
page when one is known, else by outlet and normalised name. Only an author page names one
person, so it is the only identifier; a name alone never merges anything.

An author page is attached only when it cannot belong to someone else:
- in an author feed (one URL per Wikidata author id), to bylines whose name matches the
  Wikidata label of that author; co-authors in the same feed get no page;
- in a section feed, from an item's own author element, when the item has exactly one
  author, that author is a person, and the element pairs the name with one URI of the
  outlet's own author-page form;
- never to a byline with a non-person part, nor to a namesake whose own link names
  another page.
Feeds were chosen with the user after spike 6 (docs/spike-findings.md section 7).
"""

import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit

from ddm.identifiers import normalize_author_url
from ddm.ingest.records import NormalizedRecord, make_record, norm_title
from ddm.sources.byline_parse import BylineItem, ParsedFeed
from ddm.sources.podcast_rss import ContactRoute
from ddm.sources.wikidata import AUTHOR_ID_RULES, OUTLETS, author_url

SOURCE_NAME = "outlet_rss"
NORMALIZER_VERSION = 1
MAX_BYLINES = 5
FEED_KEY_PREFIX = "outlet_feed:"
MIN_NAME_WORDS, MAX_NAME_WORDS = 2, 5
MAX_NAME_CHARS = 80
AUTHOR_BASIS = "byline in the outlet's own feed"
AUTHOR_PURPOSE_BASIS = "author page; no stated purpose"
SEPARATORS = re.compile(r"\s*[,;&]\s*|\s+(?:and|with)\s+")
LEADING_BY = re.compile(r"^by\s+", re.IGNORECASE)
NAME_SUFFIX = re.compile(r"(?:jr|sr|ii|iii|iv)\.?", re.IGNORECASE)
# A name carrying an address is dropped whole: emails must never reach storage.
EMAIL_MARKERS = ("@", "<", ">", "mailto:")
# Path segments after which a URL names one writer; hidden from logs and public output.
AUTHOR_SEGMENTS = frozenset({"author", "authors", "by", "people", "profile", "profiles"})
NON_PERSON_MARKERS = re.compile(
    r"\b(?:associated press|reuters|staff|editors|editorial board|newsroom|wire services"
    r"|contributors|npr|bloomberg|the atlantic|politico"
    # Roles that follow a name ("Jane Roe, Senior Editor") are not people either.
    r"|editor|correspondent|columnist|reporter|bureau|desk|special to)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class OutletFeed:
    """A feed to read; an author feed carries its author's page and Wikidata label."""

    outlet_qid: str
    outlet: str
    url: str
    author_url: str | None = None
    author_name: str | None = None

    @property
    def key(self) -> str:
        """fetch_attempts / feed_fetch_state key; the prefix keeps it apart from podcasts."""
        return FEED_KEY_PREFIX + self.url

    @property
    def label(self) -> str:
        """How logs name the feed: an author feed's URL carries the author's name."""
        return f"{self.outlet} author feed" if self.author_url else self.url


def public_url(url: str) -> str:
    """Scheme, host and path with author slugs hidden; for logs and public CI output."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<invalid url>"
    if not parts.scheme or not parts.hostname:
        return "<invalid url>"
    segments = parts.path.split("/")
    hidden = [
        "<author>" if i and segments[i - 1].lower() in AUTHOR_SEGMENTS and seg else seg
        for i, seg in enumerate(segments)
    ]
    return f"{parts.scheme}://{parts.hostname}{'/'.join(hidden)}"


SECTION_FEEDS: tuple[OutletFeed, ...] = (
    OutletFeed("Q1542536", "The Atlantic", "https://www.theatlantic.com/feed/all/"),
    OutletFeed(
        "Q9684",
        "The New York Times",
        "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml",
    ),
    OutletFeed("Q3109740", "Politico", "https://rss.politico.com/politics-news.xml"),
    OutletFeed("Q1022366", "TechCrunch", "https://techcrunch.com/feed/"),
)
# Wikidata author-id property -> that outlet's per-author feed ($1 is the id).
AUTHOR_FEEDS = MappingProxyType({"P6791": "https://www.theatlantic.com/feed/author/$1/"})


def author_feed(prop: str, slug: str, author_name: str | None) -> OutletFeed | None:
    template = AUTHOR_FEEDS.get(prop)
    page = author_url(prop, slug)
    if template is None or page is None:
        return None
    outlet_qid = AUTHOR_ID_RULES[prop].outlet_qid
    return OutletFeed(
        outlet_qid=outlet_qid,
        outlet=OUTLETS[outlet_qid],
        url=template.replace("$1", slug),
        author_url=normalize_author_url(page),
        author_name=author_name,
    )


def _is_person(name: str) -> bool:
    words = name.split()
    return (
        MIN_NAME_WORDS <= len(words) <= MAX_NAME_WORDS
        and len(name) <= MAX_NAME_CHARS
        and any(c.isalpha() for c in name)
        and not any(marker in name.lower() for marker in EMAIL_MARKERS)
        and NON_PERSON_MARKERS.search(name) is None
    )


def split_authors(raw: str) -> tuple[tuple[str, ...], int]:
    """(people, number of non-person parts) in one byline text."""
    text = LEADING_BY.sub("", " ".join(raw.split()))
    parts: list[str] = []
    for part in SEPARATORS.split(text):
        if parts and NAME_SUFFIX.fullmatch(part):
            parts[-1] = f"{parts[-1]}, {part}"  # "King, Jr." is one person
        elif part:
            parts.append(part)
    people = [p for p in parts if _is_person(p)]
    return tuple(dict.fromkeys(people)), len(parts) - len(people)


def _outlet_page(outlet_qid: str, uri: str) -> str | None:
    """The normalised author page if the URI has this outlet's own author-page form."""
    page = normalize_author_url(uri)
    if page is None:
        return None
    slug = page.rsplit("/", 1)[-1]
    for prop, rule in AUTHOR_ID_RULES.items():
        if rule.outlet_qid == outlet_qid and normalize_author_url(author_url(prop, slug)) == page:
            return page
    return None


@dataclass
class _Author:
    """One record being assembled. Mutable on purpose, like ingest batches; never escapes."""

    outlet_qid: str
    outlet: str
    page: str | None
    names: Counter = field(default_factory=Counter)
    bylines: set = field(default_factory=set)


def _byline(item: BylineItem) -> tuple[str | None, str | None, str | None]:
    published = item.published_at.isoformat() if item.published_at else None
    return (published, item.link, item.title)


def _item_pages(feed: OutletFeed, item: BylineItem) -> dict[str, set[str]]:
    """Outlet author pages per normalised name, from the item's own (name, uri) pairs."""
    pages: dict[str, set[str]] = {}
    for name, uri in item.author_uris:
        if (page := _outlet_page(feed.outlet_qid, uri)) is not None:
            pages.setdefault(norm_title(name) or "", set()).add(page)
    return pages


def _page_for(feed: OutletFeed, name: str, item_pages, sole_person: bool) -> str | None:
    """An author page only where it cannot be someone else's (see the module docstring)."""
    own = item_pages.get(norm_title(name) or "", set())
    if feed.author_url and norm_title(name) == norm_title(feed.author_name):
        # A namesake whose own link names another page is somebody else.
        return feed.author_url if own <= {feed.author_url} else None
    return next(iter(own)) if sole_person and len(own) == 1 else None


def _people_of(feed: OutletFeed, item: BylineItem) -> tuple[list[tuple[str, str | None]], int]:
    """(name, author page or None) per person in the item, and the non-person count.

    Pages are given only to clean bylines: one with a non-person part (an organisation,
    a one-word name) could pair the page with the wrong part, so it gets none."""
    people: list[str] = []
    excluded = 0
    for raw in item.authors:
        found, skipped = split_authors(raw)
        people.extend(p for p in found if p not in people)
        excluded += skipped
    if excluded:
        return [(name, None) for name in people], excluded
    pages = _item_pages(feed, item)
    sole = len(item.authors) == 1 and len(people) == 1
    return [(name, _page_for(feed, name, pages, sole)) for name in people], excluded


def _window(bylines: Iterable[Mapping[str, str]]) -> list[dict[str, str]]:
    """The newest MAX_BYLINES, undated last; one per article (by link when there is one),
    the first given winning, so a newer copy replaces an older one."""
    unique: dict[Any, dict[str, str]] = {}
    for byline in bylines:
        key = byline.get("link") or (byline.get("published_at"), byline.get("title"))
        unique.setdefault(key, dict(byline))
    by_link = sorted(unique.values(), key=lambda b: (b.get("link", ""), b.get("title", "")))
    newest = sorted(by_link, key=lambda b: b.get("published_at", ""), reverse=True)
    dated = [b for b in newest if b.get("published_at")]
    return (dated + [b for b in newest if not b.get("published_at")])[:MAX_BYLINES]


def _with_window(record: NormalizedRecord, bylines: list[dict[str, str]]) -> NormalizedRecord:
    attrs = {k: v for k, v in record.attrs.items() if k not in ("bylines", "latest_byline_at")}
    attrs["bylines"] = bylines
    if bylines and bylines[0].get("published_at"):
        attrs["latest_byline_at"] = bylines[0]["published_at"]
    return make_record(
        source_key=record.source_key,
        display_name=record.display_name,
        attrs=attrs,
        source_updated_at=record.source_updated_at,
        identifiers=record.identifiers,
    )


def merge_window(record: NormalizedRecord, previous: Mapping[str, Any]) -> NormalizedRecord:
    """The record with its stored bylines folded in. A feed that answered 304 this run, or
    showed fewer items, must not make an author's recent work look like it went away."""
    stored = [b for b in previous.get("bylines") or () if isinstance(b, Mapping)]
    return _with_window(record, _window([*record.attrs["bylines"], *stored]))


def _record(key: str, author: _Author) -> NormalizedRecord:
    # Newest first before de-duplication, so the latest copy of an article wins.
    seen = sorted(author.bylines, key=lambda b: tuple(v or "" for v in b), reverse=True)
    bylines = [
        {k: v for k, v in zip(("published_at", "link", "title"), b, strict=True) if v} for b in seen
    ]
    attrs: dict[str, Any] = {"outlet_qid": author.outlet_qid, "outlet": author.outlet}
    if author.page:
        attrs["author_url"] = author.page
    name = min(author.names.items(), key=lambda kv: (-kv[1], kv[0]))[0]
    base = make_record(
        source_key=key,
        display_name=name,
        attrs={**attrs, "bylines": []},
        source_updated_at=None,
        identifiers=(("outlet_author_url", author.page),) if author.page else (),
    )
    return _with_window(base, _window(bylines))


def aggregate(
    feed_results: Iterable[tuple[OutletFeed, ParsedFeed, datetime]],
) -> tuple[list[NormalizedRecord], int]:
    """One record per author per outlet in key order, and the organisation bylines skipped."""
    authors: dict[str, _Author] = {}
    excluded = 0
    for feed, parsed, _attempted_at in feed_results:
        for item in parsed.items:
            people, skipped = _people_of(feed, item)
            excluded += skipped
            for name, page in people:
                key = page or f"{feed.outlet_qid}:{norm_title(name)}"
                author = authors.setdefault(key, _Author(feed.outlet_qid, feed.outlet, page))
                author.names[name] += 1
                author.bylines.add(_byline(item))
    return [_record(key, authors[key]) for key in sorted(authors)], excluded


def contact_routes_for(record: NormalizedRecord) -> tuple[ContactRoute, ...]:
    page = record.attrs.get("author_url")
    if not page:
        return ()
    return (
        ContactRoute(
            route_type="outlet_author_page",
            value=f"https://{page}",
            syntax_valid=True,
            affiliation_status="confirmed",
            affiliation_basis=AUTHOR_BASIS,
            purpose="unknown",
            purpose_basis=AUTHOR_PURPOSE_BASIS,
        ),
    )
