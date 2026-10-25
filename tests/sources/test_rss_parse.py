from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ddm.sources.rss_parse import (
    MAX_CATEGORIES,
    MAX_DESCRIPTION_CHARS,
    MAX_ELEMENTS,
    MAX_TEXT_CHARS,
    FeedParseError,
    parse_feed,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "rss"
FETCHED_AT = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)


def load(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def channel_xml(inner: str, items: str = "") -> bytes:
    return (
        '<?xml version="1.0"?>'
        '<rss xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"><channel>'
        f"{inner}{items}</channel></rss>"
    ).encode()


EPISODE = "Sun, 01 Mar 2026 10:00:00 +0000"


def item(pub_date: str) -> str:
    return f"<item><pubDate>{pub_date}</pubDate></item>"


def test_full_feed_reads_every_channel_field():
    channel = parse_feed(load("full_feed.xml"), FETCHED_AT)

    assert channel.title == "The Example Show"
    assert channel.link == "https://show.example.com/"
    assert channel.description == "<p>Weekly talk about <b>examples</b>.</p>"
    assert channel.language == "en-us"
    assert channel.itunes_author == "Example Media"
    assert channel.owner_name == "Owner Name"
    assert channel.owner_email == "Owner@Example.com"
    assert channel.managing_editor == "editor@example.com (Ed Itor)"
    assert channel.image_url == "https://cdn.example.com/art.jpg"
    assert channel.podcast_guid == "917393E3-1B1E-5CEF-ACE4-EDAA54E1F810"
    assert channel.new_feed_url == "https://new.example.com/feed.xml"
    assert channel.itunes_type == "episodic"
    assert channel.explicit is True


def test_item_level_author_does_not_override_the_channel_author():
    channel = parse_feed(load("full_feed.xml"), FETCHED_AT)

    assert channel.itunes_author == "Example Media"


def test_nested_categories_are_flattened_lowercased_and_deduplicated():
    channel = parse_feed(load("full_feed.xml"), FETCHED_AT)

    assert channel.categories == ("business", "marketing", "technology")


def test_episode_dates_ignore_future_and_unparseable_values():
    channel = parse_feed(load("full_feed.xml"), FETCHED_AT)

    assert channel.episode_count == 5
    assert channel.newest_episode_at == datetime(2026, 10, 2, 10, 0, tzinfo=UTC)
    assert channel.oldest_episode_at == datetime(2026, 1, 15, 9, 30, tzinfo=UTC)


def test_a_date_within_the_future_tolerance_still_counts():
    soon = (FETCHED_AT + timedelta(hours=6)).strftime("%a, %d %b %Y %H:%M:%S +0000")

    channel = parse_feed(channel_xml("<title>t</title>", item(soon)), FETCHED_AT)

    assert channel.newest_episode_at == FETCHED_AT + timedelta(hours=6)


def test_iso_dates_with_an_offset_are_converted_to_utc():
    body = channel_xml("<title>t</title>", item("2026-03-01T10:00:00+02:00"))

    channel = parse_feed(body, FETCHED_AT)

    assert channel.newest_episode_at == datetime(2026, 3, 1, 8, 0, tzinfo=UTC)


def test_rfc822_date_without_a_zone_is_read_as_utc():
    body = channel_xml("<title>t</title>", item("Sun, 01 Mar 2026 10:00:00"))

    channel = parse_feed(body, FETCHED_AT)

    assert channel.newest_episode_at == datetime(2026, 3, 1, 10, 0, tzinfo=UTC)


def test_description_falls_back_to_itunes_summary():
    channel = parse_feed(load("legacy_namespaces.xml"), FETCHED_AT)

    assert channel.description == "Only a summary here."


def test_image_falls_back_to_the_rss_image_url():
    body = channel_xml("<image><url>https://cdn.example.com/x.png</url></image>")

    assert parse_feed(body, FETCHED_AT).image_url == "https://cdn.example.com/x.png"


def test_minimal_feed_leaves_unknown_fields_empty():
    channel = parse_feed(load("minimal_feed.xml"), FETCHED_AT)

    assert channel.title == "Minimal Show"
    assert channel.episode_count == 0
    assert (channel.newest_episode_at, channel.oldest_episode_at) == (None, None)
    assert channel.categories == ()
    assert channel.explicit is None
    assert all(
        getattr(channel, name) is None
        for name in (
            "link",
            "description",
            "language",
            "itunes_author",
            "owner_name",
            "owner_email",
            "managing_editor",
            "image_url",
            "podcast_guid",
            "new_feed_url",
            "itunes_type",
        )
    )


def test_legacy_namespace_uris_and_declared_encoding_are_understood():
    channel = parse_feed(load("legacy_namespaces.xml"), FETCHED_AT)

    assert channel.title == "Café Legacy"
    assert channel.itunes_author == "Legacy Author"
    assert channel.podcast_guid == "aaaaaaaa-bbbb-5ccc-8ddd-eeeeeeeeeeee"
    assert channel.explicit is False


def test_utf8_bom_is_accepted():
    body = b"\xef\xbb\xbf" + load("minimal_feed.xml")

    assert parse_feed(body, FETCHED_AT).title == "Minimal Show"


def test_external_entities_are_refused_without_reading_them():
    with pytest.raises(FeedParseError) as excinfo:
        parse_feed(load("xxe_feed.xml"), FETCHED_AT)

    assert excinfo.value.reason == "unsafe_xml"
    assert "localhost" not in str(excinfo.value)


def test_unescaped_ampersand_is_malformed():
    with pytest.raises(FeedParseError) as excinfo:
        parse_feed(load("malformed_feed.xml"), FETCHED_AT)

    assert excinfo.value.reason == "malformed_xml"


def test_html_page_is_an_unsupported_format():
    with pytest.raises(FeedParseError) as excinfo:
        parse_feed(load("html_page.html"), FETCHED_AT)

    assert excinfo.value.reason == "unsupported_format"


def test_atom_feed_is_an_unsupported_format():
    body = b'<feed xmlns="http://www.w3.org/2005/Atom"><title>a</title></feed>'

    with pytest.raises(FeedParseError) as excinfo:
        parse_feed(body, FETCHED_AT)

    assert excinfo.value.reason == "unsupported_format"


def test_rss_without_a_channel_is_refused():
    with pytest.raises(FeedParseError) as excinfo:
        parse_feed(b"<rss/>", FETCHED_AT)

    assert excinfo.value.reason == "no_channel"


def test_blank_title_is_unknown():
    assert parse_feed(channel_xml("<title>   </title>"), FETCHED_AT).title is None


def test_first_non_empty_channel_value_wins():
    body = channel_xml("<title></title><title>Second</title><title>Third</title>")

    assert parse_feed(body, FETCHED_AT).title == "Second"


@pytest.mark.parametrize(
    ("value", "expected"),
    [("yes", True), ("TRUE", True), ("explicit", True), ("no", False), ("clean", False)],
)
def test_explicit_values_map_to_booleans(value, expected):
    body = channel_xml(f"<itunes:explicit>{value}</itunes:explicit>")

    assert parse_feed(body, FETCHED_AT).explicit is expected


def test_unknown_explicit_value_is_unknown():
    body = channel_xml("<itunes:explicit>maybe</itunes:explicit>")

    assert parse_feed(body, FETCHED_AT).explicit is None


def test_parse_error_reason_must_be_known():
    with pytest.raises(ValueError, match="unknown parse reason"):
        FeedParseError("whatever")


def test_deeply_nested_categories_do_not_exhaust_the_stack():
    depth = 5_000
    nested = '<itunes:category text="c">' * depth + "</itunes:category>" * depth

    channel = parse_feed(channel_xml(nested), FETCHED_AT)

    assert channel.categories == ("c",)


def test_categories_are_capped():
    many = "".join(f'<itunes:category text="c{i}"/>' for i in range(MAX_CATEGORIES + 10))

    assert len(parse_feed(channel_xml(many), FETCHED_AT).categories) == MAX_CATEGORIES


def test_too_many_elements_is_refused():
    body = channel_xml("<title>t</title>", "<item/>" * (MAX_ELEMENTS + 1))

    with pytest.raises(FeedParseError) as excinfo:
        parse_feed(body, FETCHED_AT)

    assert excinfo.value.reason == "too_complex"


def test_out_of_range_episode_dates_are_ignored():
    body = channel_xml("<title>t</title>", item("Fri, 31 Dec 9999 23:59:59 -0100") + item(EPISODE))

    channel = parse_feed(body, FETCHED_AT)

    assert channel.newest_episode_at == datetime(2026, 3, 1, 10, 0, tzinfo=UTC)


def test_long_text_is_truncated():
    body = channel_xml(f"<title>{'t' * 5000}</title><description>{'d' * 50000}</description>")

    channel = parse_feed(body, FETCHED_AT)

    assert len(channel.title) == MAX_TEXT_CHARS
    assert len(channel.description) == MAX_DESCRIPTION_CHARS
