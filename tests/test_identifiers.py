import uuid

import pytest

from ddm.identifiers import (
    DDM_ENTITY_NAMESPACE,
    entity_id_for,
    normalize_feed_url,
    normalize_guid,
    normalize_itunes_id,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("http://A.com/f.xml", "a.com/f.xml"),
        ("https://a.com/f.xml/", "a.com/f.xml"),
        ("  https://Feeds.Example.COM./podcast.xml  ", "feeds.example.com/podcast.xml"),
        ("https://a.com:443/f", "a.com/f"),
        ("http://a.com:80/f", "a.com/f"),
        ("https://a.com:8443/f", "a.com:8443/f"),
        ("https://a.com/f?id=1#section", "a.com/f?id=1"),
        ("https://a.com", "a.com"),
        ("https://a.com/", "a.com"),
        ("https://www.a.com/f", "www.a.com/f"),
        ("https://a.com/Path/Case.xml", "a.com/Path/Case.xml"),
        ("https://a.com:80/f", "a.com:80/f"),
        ("http://a.com:443/f", "a.com:443/f"),
        ("http://[::1]:8080/f", "[::1]:8080/f"),
        ("http://[::1]/f", "[::1]/f"),
    ],
)
def test_normalize_feed_url_canonicalises_equivalent_forms(raw, expected):
    assert normalize_feed_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "ftp://a.com/f",
        "https://",
        "feeds.example.com/x",
        "https://a.com:99999/",
        "http://[::1",
    ],
)
def test_normalize_feed_url_returns_none_for_unusable_values(raw):
    assert normalize_feed_url(raw) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (123, "123"),
        ("456", "456"),
        (" 789 ", "789"),
        (0, None),
        (-1, None),
        ("abc", None),
        ("", None),
        (None, None),
        (True, None),
        (1.5, None),
        ("²", None),
        ("١٢", None),
    ],
)
def test_normalize_itunes_id_accepts_only_positive_integers(raw, expected):
    assert normalize_itunes_id(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("  ABC-def ", "abc-def"), ("", None), ("   ", None), (None, None)],
)
def test_normalize_guid_trims_and_lowercases(raw, expected):
    assert normalize_guid(raw) == expected


def test_entity_id_is_deterministic_for_the_same_source_record():
    first = entity_id_for("podcastindex_dump", "42")
    second = entity_id_for("podcastindex_dump", "42")

    assert first == second
    assert first == uuid.uuid5(DDM_ENTITY_NAMESPACE, "podcastindex_dump:42")


def test_entity_id_differs_between_source_records():
    assert entity_id_for("podcastindex_dump", "42") != entity_id_for("podcastindex_dump", "43")
    assert entity_id_for("podcastindex_dump", "42") != entity_id_for("wikidata", "42")
