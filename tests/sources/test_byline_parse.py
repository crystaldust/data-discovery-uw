from datetime import UTC, datetime
from pathlib import Path

import pytest

from ddm.sources.byline_parse import MAX_ITEMS, MAX_TITLE_CHARS, parse_bylines
from ddm.sources.rss_parse import FeedParseError

FIXTURES = Path(__file__).parent.parent / "fixtures"
FETCHED_AT = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)


def load(name: str, folder: str = "bylines") -> bytes:
    return (FIXTURES / folder / name).read_bytes()


def test_rss_items_give_every_dc_creator_title_link_and_date():
    feed = parse_bylines(load("rss_dc_creator.xml"), FETCHED_AT)
    first = feed.items[0]

    assert (feed.format, feed.rights) == ("rss", "Copyright 2026 Example Media")
    assert first.authors == ("Jane Roe", "John Doe")
    assert first.author_uris == ()
    assert first.title == "Budget vote delayed again"
    assert first.link == "https://news.example.com/politics/budget-vote"
    assert first.published_at == datetime(2026, 10, 26, 10, 0, tzinfo=UTC)


def test_rss_author_keeps_the_name_and_never_the_email():
    feed = parse_bylines(load("rss_dc_creator.xml"), FETCHED_AT)

    assert feed.items[1].authors == ("Sam Writer",)
    assert feed.items[2].authors == ()
    assert "@" not in repr(feed)


def test_dates_more_than_a_day_ahead_are_unknown():
    feed = parse_bylines(load("rss_dc_creator.xml"), FETCHED_AT)

    assert feed.items[3].published_at is None
    assert feed.items[3].authors == ("By Jane Roe and John Doe",)


def test_atom_authors_inside_rss_items_give_names_and_author_pages():
    feed = parse_bylines(load("rss_atom_author.xml"), FETCHED_AT)
    item = feed.items[0]

    assert item.authors == ("Avery Example",)
    assert item.author_uris == (
        ("Avery Example", "https://www.monthly.example.com/author/avery-example/"),
    )
    assert item.published_at == datetime(2026, 10, 27, 16, 0, tzinfo=UTC)


def test_atom_entries_use_their_own_author_and_fall_back_to_the_feed_author():
    feed = parse_bylines(load("atom_author.xml"), FETCHED_AT)
    review, news = feed.items

    assert (feed.format, feed.rights) == ("atom", "© Example Tech 2026")
    assert review.authors == ("Blake Sample",)
    assert review.author_uris == (
        ("Blake Sample", "https://tech.example.com/authors/blake-sample"),
    )
    assert review.link == "https://tech.example.com/review"
    assert review.published_at == datetime(2026, 10, 26, 13, 30, tzinfo=UTC)
    assert news.authors == ("Example Tech Staff",)
    assert news.link == "https://tech.example.com/news"
    assert news.published_at == datetime(2026, 10, 25, 8, 0, tzinfo=UTC)


def test_atom_author_emails_are_not_kept():
    feed = parse_bylines(load("atom_author.xml"), FETCHED_AT)

    assert "blake@" not in repr(feed)


def test_entity_declarations_are_refused():
    with pytest.raises(FeedParseError) as caught:
        parse_bylines(load("xxe_byline.xml"), FETCHED_AT)

    assert caught.value.reason == "unsafe_xml"


def test_an_html_page_is_not_a_feed():
    with pytest.raises(FeedParseError) as caught:
        parse_bylines(load("html_page.html", folder="rss"), FETCHED_AT)

    assert caught.value.reason in {"malformed_xml", "unsupported_format"}


def test_another_root_element_is_unsupported():
    with pytest.raises(FeedParseError) as caught:
        parse_bylines(b"<opml><body/></opml>", FETCHED_AT)

    assert caught.value.reason == "unsupported_format"


def test_rss_without_a_channel_has_no_items():
    assert parse_bylines(b"<rss/>", FETCHED_AT).items == ()


def test_titles_are_capped_and_items_are_limited():
    item = f"<item><title>{'x' * (MAX_TITLE_CHARS + 50)}</title></item>"
    body = f"<rss><channel>{item * (MAX_ITEMS + 5)}</channel></rss>".encode()

    feed = parse_bylines(body, FETCHED_AT)

    assert len(feed.items) == MAX_ITEMS
    assert len(feed.items[0].title) == MAX_TITLE_CHARS


def test_empty_values_are_unknown():
    body = b"<rss><channel><item><title> </title><link></link></item></channel></rss>"

    item = parse_bylines(body, FETCHED_AT).items[0]

    assert (item.title, item.link, item.published_at, item.authors) == (None, None, None, ())


@pytest.mark.parametrize(
    "creator",
    [
        "<dc:creator>Jane Roe jane.roe@example.com</dc:creator>",
        "<dc:creator>Jane Roe &lt;jane@example.com&gt;</dc:creator>",
        "<atom:author><atom:name>Jane Roe (jane@example.com)</atom:name></atom:author>",
        "<author>Jane Roe &lt;jane@example.com&gt;</author>",
        "<author>jane@example.com (desk@example.com)</author>",
    ],
)
def test_names_carrying_an_email_are_dropped_wherever_they_appear(creator):
    body = (
        '<rss xmlns:dc="http://purl.org/dc/elements/1.1/"'
        ' xmlns:atom="http://www.w3.org/2005/Atom">'
        f"<channel><item>{creator}</item></channel></rss>"
    ).encode()

    feed = parse_bylines(body, FETCHED_AT)

    assert feed.items[0].authors == ()
    assert "@" not in repr(feed)


@pytest.mark.parametrize(
    "link",
    ["javascript:alert(1)", "data:text/html,x", "//no-scheme.example.com/a", "https://"],
)
def test_links_that_are_not_web_addresses_are_unknown(link):
    body = f"<rss><channel><item><link>{link}</link></item></channel></rss>".encode()

    assert parse_bylines(body, FETCHED_AT).items[0].link is None


def test_author_uris_stay_paired_with_their_own_name():
    body = (
        b'<rss xmlns:atom="http://www.w3.org/2005/Atom"><channel><item>'
        b"<atom:author><atom:name>Jane Roe</atom:name></atom:author>"
        b"<atom:author><atom:name>The Editors</atom:name>"
        b"<atom:uri>https://www.monthly.example.com/author/the-editors/</atom:uri></atom:author>"
        b"<atom:author><atom:uri>https://www.monthly.example.com/author/nameless/</atom:uri>"
        b"</atom:author></item></channel></rss>"
    )

    item = parse_bylines(body, FETCHED_AT).items[0]

    assert item.authors == ("Jane Roe", "The Editors")
    assert item.author_uris == (
        ("The Editors", "https://www.monthly.example.com/author/the-editors/"),
    )
