from datetime import UTC, datetime

import pytest

from ddm.ingest.records import content_hash
from ddm.sources.byline_parse import BylineItem, ParsedFeed
from ddm.sources.outlet_rss import (
    FEED_KEY_PREFIX,
    MAX_BYLINES,
    SECTION_FEEDS,
    OutletFeed,
    aggregate,
    author_feed,
    contact_routes_for,
    merge_window,
    public_url,
    split_authors,
)

ATLANTIC, NYT = "Q1542536", "Q9684"
AT = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
SECTION = OutletFeed(ATLANTIC, "The Atlantic", "https://www.theatlantic.com/feed/all/")
NYT_SECTION = OutletFeed(NYT, "The New York Times", "https://rss.nytimes.com/x.xml")
PAGE = "theatlantic.com/author/avery-example"


def item(*authors, uris=(), link="https://a.example.com/1", title="T", day=1):
    """`uris` are (name, uri) pairs, or bare URIs paired with the first author."""
    published = datetime(2026, 10, day, tzinfo=UTC) if day else None
    pairs = tuple(u if isinstance(u, tuple) else (authors[0], u) for u in uris)
    return BylineItem(tuple(authors), pairs, title, link, published)


def feed_of(*items) -> ParsedFeed:
    return ParsedFeed(format="rss", rights=None, items=tuple(items))


def by_key(records):
    return {r.source_key: r for r in records}


@pytest.mark.parametrize(
    ("raw", "people", "excluded"),
    [
        ("Jane Roe", ("Jane Roe",), 0),
        ("By Jane Roe and John Doe", ("Jane Roe", "John Doe"), 0),
        ("by Jane Roe, John Doe & Sam Q. Writer", ("Jane Roe", "John Doe", "Sam Q. Writer"), 0),
        ("Jane Roe; John Doe", ("Jane Roe", "John Doe"), 0),
        ("Martin Luther King, Jr.", ("Martin Luther King, Jr.",), 0),
        ("The Associated Press", (), 1),
        ("Jane Roe and The Associated Press", ("Jane Roe",), 1),
        ("NPR Staff", (), 1),
        ("Reuters", (), 1),
        ("The Editorial Board", (), 1),
        ("Cher", (), 1),
        ("A Very Long Name With Too Many Words", (), 1),
        ("12345 67890", (), 1),
        ("Andrew Anderson", ("Andrew Anderson",), 0),
        ("Jane   Roe", ("Jane Roe",), 0),
        ("Jane Roe and Jane Roe", ("Jane Roe",), 0),
        ("Jane Roe jane@example.com", (), 1),
        ("Jane Roe, Senior Editor", ("Jane Roe",), 1),
        ("Jane Roe with John Doe", ("Jane Roe", "John Doe"), 0),
        ("Special to The Times", (), 1),
        ("Jane Roe <jr@example.com>", (), 1),
        ("Jane Roe mailto:jr", (), 1),
        ("", (), 0),
    ],
)
def test_split_authors_keeps_people_and_counts_the_rest(raw, people, excluded):
    assert split_authors(raw) == (people, excluded)


def test_author_feeds_are_built_only_from_valid_atlantic_ids():
    found = author_feed("P6791", "avery-example", "Avery Example")

    assert found == OutletFeed(
        ATLANTIC,
        "The Atlantic",
        "https://www.theatlantic.com/feed/author/avery-example/",
        PAGE,
        "Avery Example",
    )
    assert found.key == FEED_KEY_PREFIX + found.url
    assert author_feed("P6791", "../x", "A B") is None
    assert author_feed("P8297", "avery-example", "A B") is None


def test_the_chosen_section_feeds_are_the_four_outlets():
    assert {f.outlet for f in SECTION_FEEDS} == {
        "The Atlantic",
        "The New York Times",
        "Politico",
        "TechCrunch",
    }
    assert all(f.author_url is None and f.url.startswith("https://") for f in SECTION_FEEDS)


def test_a_byline_with_an_outlet_author_page_is_keyed_and_identified_by_it():
    uri = "https://www.theatlantic.com/author/avery-example/"

    records, excluded = aggregate([(SECTION, feed_of(item("Avery Example", uris=[uri])), AT)])

    record = by_key(records)[PAGE]
    assert excluded == 0
    assert record.identifiers == (("outlet_author_url", PAGE),)
    assert record.display_name == "Avery Example"
    assert record.attrs["author_url"] == PAGE
    assert record.attrs["outlet_qid"] == ATLANTIC


def test_an_author_page_of_another_outlet_is_not_an_identifier():
    uri = "https://www.nytimes.com/by/avery-example"

    records, _ = aggregate([(SECTION, feed_of(item("Avery Example", uris=[uri])), AT)])

    assert list(by_key(records)) == [f"{ATLANTIC}:avery example"]
    assert records[0].identifiers == ()


def test_an_item_page_is_not_guessed_between_co_authors():
    uris = [
        ("Ann Bee", "https://www.theatlantic.com/author/a-b/"),
        ("Cid Dee", "https://www.theatlantic.com/author/c-d/"),
    ]

    records, _ = aggregate([(SECTION, feed_of(item("Ann Bee", "Cid Dee", uris=uris)), AT)])

    assert all(r.identifiers == () for r in records)


@pytest.mark.parametrize("other", ["The Atlantic Editors", "Bob"])
def test_a_page_of_a_non_person_co_author_never_goes_to_the_person(other):
    page = ("The Atlantic Editors", "https://www.theatlantic.com/author/the-atlantic/")
    parsed = feed_of(item("Jane Doe", other, uris=[(other, page[1])]))

    records, _ = aggregate([(SECTION, parsed, AT)])

    assert all(r.identifiers == () for r in records)


def test_a_page_paired_with_another_name_is_not_given_to_the_only_person():
    uri = ("Someone Else", "https://www.theatlantic.com/author/someone-else/")

    records, _ = aggregate([(SECTION, feed_of(item("Jane Doe", uris=[uri])), AT)])

    assert records[0].identifiers == ()


def test_an_author_feed_namesake_with_their_own_page_gets_no_page():
    own = author_feed("P6791", "avery-example", "Avery Example")
    namesake = ("Avery Example", "https://www.theatlantic.com/author/avery-example-2/")

    records, _ = aggregate([(own, feed_of(item("Avery Example", uris=[namesake])), AT)])

    assert records[0].identifiers == ()


def test_an_author_feed_item_with_a_non_person_part_gets_no_page():
    own = author_feed("P6791", "avery-example", "Avery Example")

    records, _ = aggregate([(own, feed_of(item("Avery Example and Staff")), AT)])

    assert records[0].identifiers == ()


def test_in_an_author_feed_only_that_author_gets_the_page():
    own = author_feed("P6791", "avery-example", "Avery Example")
    parsed = feed_of(item("Avery Example and Blake Sample"))

    records, _ = aggregate([(own, parsed, AT)])

    found = by_key(records)
    assert found[PAGE].display_name == "Avery Example"
    assert found[f"{ATLANTIC}:blake sample"].identifiers == ()


def test_bylines_without_a_page_are_keyed_by_outlet_and_normalised_name():
    records, _ = aggregate([(NYT_SECTION, feed_of(item("José  Pérez"), item("Jose Perez")), AT)])

    assert list(by_key(records)) == [f"{NYT}:jose perez"]


def test_organisation_bylines_are_counted_not_kept():
    records, excluded = aggregate([(NYT_SECTION, feed_of(item("The Associated Press")), AT)])

    assert (records, excluded) == ([], 1)


def test_one_author_across_feeds_keeps_the_newest_bylines_in_a_fixed_order():
    items = [item("Jane Roe", link=f"https://a.example.com/{d}", day=d) for d in range(1, 8)]
    undated = item("Jane Roe", link="https://a.example.com/undated", day=None)
    first = (NYT_SECTION, feed_of(*items[:4], undated), AT)
    second = (NYT_SECTION, feed_of(*items[4:]), AT)

    records, _ = aggregate([first, second])

    bylines = records[0].attrs["bylines"]
    assert len(bylines) == MAX_BYLINES
    assert [b["link"] for b in bylines] == [f"https://a.example.com/{d}" for d in (7, 6, 5, 4, 3)]
    assert records[0].attrs["latest_byline_at"] == "2026-10-07T00:00:00+00:00"


def test_display_name_is_the_most_common_spelling_then_the_smallest():
    parsed = feed_of(item("JANE ROE"), item("Jane Roe", link="b"), item("Jane Roe", link="c"))

    records, _ = aggregate([(NYT_SECTION, parsed, AT)])

    assert records[0].display_name == "Jane Roe"


def test_content_does_not_depend_on_feed_or_item_order():
    a = (NYT_SECTION, feed_of(item("Jane Roe", link="x", day=2), item("Jane Roe", link="y")), AT)
    b = (SECTION, feed_of(item("Jane Roe", link="z", day=3)), AT)
    reversed_a = (a[0], feed_of(*reversed(a[1].items)), AT)

    forward, _ = aggregate([a, b])
    backward, _ = aggregate([b, reversed_a])

    assert [(r.source_key, content_hash(r)) for r in forward] == [
        (r.source_key, content_hash(r)) for r in backward
    ]


def test_unknown_values_are_absent():
    bare = BylineItem(("Jane Roe",), (), None, None, None)

    records, _ = aggregate([(NYT_SECTION, feed_of(bare), AT)])

    assert records[0].attrs["bylines"] == [{}]
    assert "latest_byline_at" not in records[0].attrs
    assert "author_url" not in records[0].attrs


def test_a_record_with_an_author_page_gives_one_confirmed_route():
    own = author_feed("P6791", "avery-example", "Avery Example")
    records, _ = aggregate([(own, feed_of(item("Avery Example")), AT)])

    (route,) = contact_routes_for(records[0])

    assert route.route_type == "outlet_author_page"
    assert route.value == f"https://{PAGE}"
    assert (route.affiliation_status, route.purpose) == ("confirmed", "unknown")


def test_a_record_without_an_author_page_gives_no_route():
    records, _ = aggregate([(NYT_SECTION, feed_of(item("Jane Roe")), AT)])

    assert contact_routes_for(records[0]) == ()


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://www.theatlantic.com/feed/author/avery-example/?x=1",
            "https://www.theatlantic.com/feed/author/<author>/",
        ),
        ("https://www.nytimes.com/by/avery-example", "https://www.nytimes.com/by/<author>"),
        ("https://techcrunch.com/feed/", "https://techcrunch.com/feed/"),
        ("not a url", "<invalid url>"),
    ],
)
def test_public_url_hides_author_slugs(url, expected):
    assert public_url(url) == expected


def test_feed_labels_name_no_author():
    own = author_feed("P6791", "avery-example", "Avery Example")

    assert "avery" not in own.label.lower()
    assert SECTION.label == SECTION.url


def test_a_new_window_is_merged_with_the_stored_one_newest_first_by_link():
    old = [
        {"published_at": "2026-10-03T00:00:00+00:00", "link": "https://a.example.com/3"},
        {"published_at": "2026-10-01T00:00:00+00:00", "link": "https://a.example.com/1"},
    ]
    new_items = [
        item("Jane Roe", link="https://a.example.com/2", day=2),
        item("Jane Roe", link="https://a.example.com/3", title="Retitled", day=3),
    ]
    (record,), _ = aggregate([(NYT_SECTION, feed_of(*new_items), AT)])

    merged = merge_window(record, {"bylines": old, "latest_byline_at": old[0]["published_at"]})

    assert [b["link"] for b in merged.attrs["bylines"]] == [
        "https://a.example.com/3",
        "https://a.example.com/2",
        "https://a.example.com/1",
    ]
    assert merged.attrs["bylines"][0]["title"] == "Retitled"
    assert merged.attrs["latest_byline_at"] == "2026-10-03T00:00:00+00:00"
    assert merged.source_key == record.source_key


def test_merging_the_same_window_again_changes_nothing():
    (record,), _ = aggregate([(NYT_SECTION, feed_of(item("Jane Roe")), AT)])

    assert content_hash(merge_window(record, dict(record.attrs))) == content_hash(record)


def test_one_article_in_two_feeds_is_one_byline():
    a = item("Jane Roe", link="https://a.example.com/x", title="T")
    b = item("Jane Roe", link="https://a.example.com/x", title="T  (updated)")

    records, _ = aggregate([(NYT_SECTION, feed_of(a), AT), (NYT_SECTION, feed_of(b), AT)])

    assert len(records[0].attrs["bylines"]) == 1
