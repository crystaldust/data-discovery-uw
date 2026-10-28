"""Spike 6: which outlet feeds can bylines come from?

For each candidate feed: can we fetch it with our honest User-Agent and robots.txt rules,
is it RSS or Atom, how many items name an author, how many link an author page, and what
does the feed itself say about its terms. No author names are printed, only counts, the
host and first path segment of author pages, and the first characters of the feed's own
copyright or rights notice.

Usage (GitHub Actions; this machine's DNS is unreliable):
    uv run python spikes/s6_outlet_feeds.py >> "$GITHUB_STEP_SUMMARY"
"""

import sys
from collections import Counter
from urllib.parse import urlsplit
from xml.etree.ElementTree import Element, ParseError

import defusedxml.ElementTree as safe_et
from defusedxml import DefusedXmlException

from ddm.config import load_user_agent
from ddm.sources.outlet_rss import public_url
from ddm.sources.rss_fetch import FeedFetcher

DC_NS = "{http://purl.org/dc/elements/1.1/}"
ATOM_NS = "{http://www.w3.org/2005/Atom}"
NOTICE_CHARS = 120
MAX_AUTHOR_PAGE_PREFIXES = 3
MAX_TAGS = 12
# First path segments that usually mean "a page about one writer".
AUTHOR_SEGMENTS = frozenset(
    {
        "author",
        "authors",
        "by",
        "people",
        "profile",
        "profiles",
        "contributor",
        "contributors",
        "staff",
        "writer",
        "writers",
    }
)

# Candidates only: a 404 or a refusal is a finding, recorded as is.
CANDIDATES = (
    ("NPR news", "https://feeds.npr.org/1001/rss.xml"),
    ("NPR politics", "https://feeds.npr.org/1014/rss.xml"),
    ("The Atlantic all", "https://www.theatlantic.com/feed/all/"),
    # Two Atlantic author feeds (/feed/author/<slug>/) were probed in runs 40522215641 and
    # 40522486516; their slugs are people's names and are not kept in this public file.
    ("Politico politics", "https://rss.politico.com/politics-news.xml"),
    ("Wired", "https://www.wired.com/feed/rss"),
    ("TechCrunch", "https://techcrunch.com/feed/"),
    ("The Verge", "https://www.theverge.com/rss/index.xml"),
    # Axios (api.axios.com/feed/) was dropped after the first run: robots.txt disallows it.
    ("NYT home page", "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml"),
    ("Business Insider", "https://feeds.businessinsider.com/custom/all"),
)


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def text(element: Element | None) -> str:
    return " ".join((element.text or "").split()) if element is not None else ""


def page_prefix(uri: str) -> str | None:
    parts = urlsplit(uri.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    first = parts.path.strip("/").split("/", 1)[0]
    return f"{parts.hostname}/{first}/" if first else f"{parts.hostname}/"


def rss_summary(root: Element) -> dict:
    channel = root.find("channel")
    items = channel.findall("item") if channel is not None else []
    with_author = sum(1 for i in items if text(i.find(f"{DC_NS}creator")) or text(i.find("author")))
    notice = text(channel.find("copyright")) if channel is not None else ""
    return {
        "format": "rss",
        "items": len(items),
        "with_author": with_author,
        "author_pages": Counter(),
        "notice": notice,
    }


def atom_summary(root: Element) -> dict:
    entries = root.findall(f"{ATOM_NS}entry")
    feed_author = text(root.find(f"{ATOM_NS}author/{ATOM_NS}name"))
    with_author = 0
    pages: Counter[str] = Counter()
    for entry in entries:
        if text(entry.find(f"{ATOM_NS}author/{ATOM_NS}name")) or feed_author:
            with_author += 1
        for uri in entry.findall(f"{ATOM_NS}author/{ATOM_NS}uri"):
            if prefix := page_prefix(text(uri)):
                pages[prefix] += 1
    return {
        "format": "atom",
        "items": len(entries),
        "with_author": with_author,
        "author_pages": pages,
        "notice": text(root.find(f"{ATOM_NS}rights")),
    }


def summarize(body: bytes) -> dict:
    try:
        root = safe_et.fromstring(body)
    except (DefusedXmlException, ParseError) as exc:
        return {"format": f"unparsed ({type(exc).__name__})"}
    if local(root.tag) == "rss":
        return rss_summary(root)
    if root.tag == f"{ATOM_NS}feed":
        return atom_summary(root)
    return {"format": f"other ({local(root.tag)})"}


def short_tag(tag: str) -> str:
    if "}" not in tag:
        return tag
    namespace, name = tag[1:].split("}", 1)
    known = {DC_NS[1:-1]: "dc", ATOM_NS[1:-1]: "atom"}
    return f"{known.get(namespace, urlsplit(namespace).hostname or namespace)}:{name}"


def items_of(root: Element) -> list[Element]:
    if local(root.tag) == "rss":
        channel = root.find("channel")
        return channel.findall("item") if channel is not None else []
    return root.findall(f"{ATOM_NS}entry")


def author_page_prefixes(item: Element) -> set[str]:
    """Author-page-like URLs anywhere in an item: element text or attribute values."""
    found = set()
    for element in item.iter():
        for value in (element.text or "", *element.attrib.values()):
            for token in value.split():
                prefix = page_prefix(token)
                if prefix and prefix.rstrip("/").rsplit("/", 1)[-1] in AUTHOR_SEGMENTS:
                    found.add(prefix)
    return found


def structure(body: bytes) -> dict | None:
    """Tag names only (never values), so the output carries no author names."""
    try:
        root = safe_et.fromstring(body)
    except (DefusedXmlException, ParseError):
        return None
    items = items_of(root)
    item_tags: Counter[str] = Counter()
    author_children: Counter[str] = Counter()
    pages: Counter[str] = Counter()
    for item in items:
        item_tags.update({short_tag(c.tag) for c in item})
        for child in item:
            if any(k in local(child.tag).lower() for k in ("author", "creator", "contributor")):
                author_children.update(short_tag(g.tag) for g in child)
        pages.update(author_page_prefixes(item))
    links = {
        text(i.find("link"))
        or (i.find(f"{ATOM_NS}link") is not None and i.find(f"{ATOM_NS}link").get("href", ""))
        for i in items
    }
    return {
        "item_tags": item_tags,
        "author_children": author_children,
        "pages": pages,
        "links": {link for link in links if link},
    }


def detail_row(name: str, result, found: dict | None) -> str:
    final = urlsplit(public_url(result.final_url))
    if found is None:
        return (
            "| "
            + " | ".join(cell(c) for c in (name, final.hostname, final.path, "-", "-", "-"))
            + " |"
        )
    tags = ", ".join(f"{t}×{n}" for t, n in found["item_tags"].most_common(MAX_TAGS))
    kids = ", ".join(f"{t}×{n}" for t, n in found["author_children"].most_common(MAX_TAGS))
    pages = ", ".join(f"{p}×{n}" for p, n in found["pages"].most_common(MAX_AUTHOR_PAGE_PREFIXES))
    cells = (name, final.hostname, final.path, tags or "-", kids or "-", pages or "-")
    return "| " + " | ".join(cell(c) for c in cells) + " |"


def cell(value: object) -> str:
    """Feed text goes into a public markdown table: show it as code, never as markup."""
    text = " ".join(str(value).split()).replace("`", "'")
    return f"`{text}`".replace("|", "\\|")


def row(name: str, result) -> str:
    found = summarize(result.body) if result.outcome == "fetched" and result.body else {}
    pages = found.get("author_pages") or Counter()
    prefixes = ", ".join(p for p, _ in pages.most_common(MAX_AUTHOR_PAGE_PREFIXES))
    notice = (found.get("notice") or "")[:NOTICE_CHARS]
    cells = (
        name,
        urlsplit(result.requested_url).hostname,
        result.outcome if result.reason is None else f"{result.outcome}: {result.reason}",
        result.status or "-",
        found.get("format", "-"),
        found.get("items", "-"),
        found.get("with_author", "-"),
        sum(pages.values()) if found else "-",
        prefixes or "-",
        notice or "-",
    )
    return "| " + " | ".join(cell(c) for c in cells) + " |"


def main() -> int:
    print("## Outlet feed probe\n")
    print(
        "| feed | host | outcome | status | format | items | with author "
        "| with author page | author page prefix | notice (first 120 chars) |"
    )
    print("|---|---|---|---|---|---|---|---|---|---|")
    details = []
    with FeedFetcher(load_user_agent()) as fetcher:
        for name, url in CANDIDATES:
            result = fetcher.fetch(url, etag=None, last_modified=None)
            print(row(name, result), flush=True)
            fetched = result.outcome == "fetched" and result.body
            details.append((name, result, structure(result.body) if fetched else None))
    print("\n## Item structure (tag names and counts only)\n")
    print(
        "| feed | final host | final path | item child tags | author element children "
        "| author-page-like URLs |"
    )
    print("|---|---|---|---|---|---|")
    for name, result, found in details:
        print(detail_row(name, result, found))
    return 0


if __name__ == "__main__":
    sys.exit(main())
