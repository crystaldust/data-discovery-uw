import dataclasses
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ddm.ingest.records import NormalizedRecord, Reject, content_hash
from ddm.sources.podcast_rss import (
    activity_status,
    contact_routes_for,
    extract_email,
    is_valid_email,
    normalize_feed,
)
from ddm.sources.rss_fetch import FetchResult

FIXTURES = Path(__file__).parent.parent / "fixtures" / "rss"
NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
FEED = "https://feeds.example.com/show.xml"
KNOWN_KEYS = {
    "feed_url",
    "moved_to",
    "new_feed_url",
    "website",
    "description",
    "language",
    "author",
    "owner_name",
    "owner_email",
    "editor_email",
    "image_url",
    "itunes_type",
    "categories",
    "explicit",
    "podcast_guid",
    "episode_count",
    "last_episode_at",
    "oldest_episode_at",
}


def fetched(body: bytes, **overrides) -> FetchResult:
    values = {
        "requested_url": FEED,
        "final_url": FEED,
        "outcome": "fetched",
        "reason": None,
        "status": 200,
        "body": body,
        "content_type": "application/rss+xml",
        "etag": None,
        "last_modified": None,
        "moved_permanently": False,
        "retry_after": None,
        "bytes_read": len(body),
        "requests": 1,
        "elapsed_ms": 5,
        "attempted_at": NOW,
    }
    return FetchResult(**{**values, **overrides})


def load(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def full_record(**overrides) -> NormalizedRecord:
    record = normalize_feed(1, "42", fetched(load("full_feed.xml"), **overrides))
    assert isinstance(record, NormalizedRecord)
    return record


def test_full_feed_becomes_a_record_keyed_by_the_podcast_index_id():
    record = full_record()

    assert record.source_key == "42"
    assert record.display_name == "The Example Show"
    assert record.source_updated_at == datetime(2026, 10, 2, 10, 0, tzinfo=UTC)
    assert dict(record.attrs) == {
        "feed_url": FEED,
        "new_feed_url": "https://new.example.com/feed.xml",
        "website": "https://show.example.com/",
        "description": "<p>Weekly talk about <b>examples</b>.</p>",
        "language": "en-us",
        "author": "Example Media",
        "owner_name": "Owner Name",
        "owner_email": "owner@example.com",
        "editor_email": "editor@example.com",
        "image_url": "https://cdn.example.com/art.jpg",
        "itunes_type": "episodic",
        "categories": ["business", "marketing", "technology"],
        "explicit": True,
        "podcast_guid": "917393e3-1b1e-5cef-ace4-edaa54e1f810",
        "episode_count": 5,
        "last_episode_at": "2026-10-02T10:00:00+00:00",
        "oldest_episode_at": "2026-01-15T09:30:00+00:00",
    }


def test_minimal_feed_keeps_only_known_values():
    record = normalize_feed(1, "7", fetched(load("minimal_feed.xml")))

    assert dict(record.attrs) == {"feed_url": FEED, "episode_count": 0}
    assert set(record.attrs) <= KNOWN_KEYS
    assert record.source_updated_at is None


def test_explicit_false_is_recorded_when_declared():
    record = normalize_feed(1, "7", fetched(load("legacy_namespaces.xml")))

    assert record.attrs["explicit"] is False


def test_identifiers_are_ordered_and_deduplicated():
    record = full_record(
        final_url="https://moved.example.com/feed",
        moved_permanently=True,
    )

    assert record.identifiers == (
        ("podcast_guid", "917393e3-1b1e-5cef-ace4-edaa54e1f810"),
        ("podcastindex_id", "42"),
        ("feed_url", "feeds.example.com/show.xml"),
        ("feed_url", "moved.example.com/feed"),
        ("feed_url", "new.example.com/feed.xml"),
    )


def test_new_feed_url_equal_to_the_requested_one_is_not_repeated():
    body = load("full_feed.xml").replace(b"https://new.example.com/feed.xml", FEED.encode())

    record = normalize_feed(1, "42", fetched(body))

    assert [i for i in record.identifiers if i[0] == "feed_url"] == [
        ("feed_url", "feeds.example.com/show.xml")
    ]


def test_moved_to_is_kept_only_for_a_real_permanent_move():
    moved = full_record(final_url="https://moved.example.com/feed", moved_permanently=True)
    temporary = full_record(final_url="https://moved.example.com/feed")
    https_only = full_record(
        final_url="https://feeds.example.com/show.xml/", moved_permanently=True
    )

    assert moved.attrs["moved_to"] == "https://moved.example.com/feed"
    assert "moved_to" not in temporary.attrs
    assert "moved_to" not in https_only.attrs


def test_http_to_https_upgrade_is_not_a_move():
    record = normalize_feed(
        1,
        "42",
        fetched(
            load("minimal_feed.xml"),
            requested_url="http://feeds.example.com/show.xml",
            final_url="https://feeds.example.com/show.xml",
            moved_permanently=True,
        ),
    )

    assert "moved_to" not in record.attrs


def test_parse_errors_become_rejects_without_the_body():
    reject = normalize_feed(3, "42", fetched(load("html_page.html"), content_type="text/html"))

    assert isinstance(reject, Reject)
    assert (reject.row_number, reject.source_key, reject.reason) == (3, "42", "unsupported_format")
    assert dict(reject.raw) == {
        "url": FEED,
        "final_url": FEED,
        "http_status": 200,
        "content_type": "text/html",
        "bytes": len(load("html_page.html")),
        "reason": "unsupported_format",
    }


def test_unexpected_errors_become_normalization_rejects(monkeypatch):
    def broken(body, fetched_at):
        raise OverflowError("boom")

    monkeypatch.setattr("ddm.sources.podcast_rss.parse_feed", broken)

    reject = normalize_feed(1, "42", fetched(b"<rss/>"))

    assert reject.reason == "normalization_error"


def test_only_fetched_results_can_be_normalised():
    with pytest.raises(ValueError, match="fetched"):
        normalize_feed(1, "42", fetched(b"", outcome="not_modified"))


def test_same_body_gives_the_same_hash_and_last_build_date_is_ignored():
    body = load("full_feed.xml")
    rebuilt = body.replace(b"Mon, 26 Oct 2026 08:00:00 GMT", b"Tue, 27 Oct 2026 08:00:00 GMT")

    first = normalize_feed(1, "42", fetched(body))
    second = normalize_feed(1, "42", fetched(body))
    third = normalize_feed(1, "42", fetched(rebuilt))

    assert content_hash(first) == content_hash(second) == content_hash(third)


def test_a_new_episode_changes_the_hash():
    body = load("full_feed.xml")
    newer = body.replace(b"Fri, 02 Oct 2026 10:00:00 +0000", b"Fri, 23 Oct 2026 10:00:00 +0000")

    assert content_hash(normalize_feed(1, "42", fetched(body))) != content_hash(
        normalize_feed(1, "42", fetched(newer))
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Ed <ED@X.COM>", "ed@x.com"),
        ("ed@x.com (Ed)", "ed@x.com"),
        ("  Owner@Example.com ", "owner@example.com"),
        ("Ed Itor", None),
        ("", None),
        (None, None),
        ("mailto:Show@Example.com", "show@example.com"),
        ("junk, second@example.com", "second@example.com"),
        ("first@example.com; second@example.com", "first@example.com"),
        ("@handle", None),
        ("a,b@x.com", "b@x.com"),
    ],
)
def test_extract_email(raw, expected):
    assert extract_email(raw) == expected


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ("a@b.co", True),
        ("first.last+tag@sub.example.com", True),
        ("a@b", False),
        ("a@@b.com", False),
        ("a b@c.com", False),
        ("-a@-b.com", False),
        ("a@b-.com", False),
        ("a@b..com", False),
        ("@b.com", False),
        (".a@b.com", False),
        ("a.@b.com", False),
        ("a..b@c.com", False),
        ('a"b@c.com', False),
        ("a<b@c.com", False),
        (f"{'a' * 65}@b.com", False),
        (f"a@{'b' * 250}.com", False),
    ],
)
def test_is_valid_email_checks_syntax_only(value, valid):
    assert is_valid_email(value) is valid


def test_contact_routes_name_their_basis_and_never_claim_deliverability():
    routes = {r.route_type: r for r in contact_routes_for(full_record())}

    assert set(routes) == {"feed_owner_email", "feed_editor_email", "website"}
    owner = routes["feed_owner_email"]
    assert (owner.value, owner.syntax_valid) == ("owner@example.com", True)
    assert owner.affiliation_status == "confirmed"
    assert "itunes:owner" in owner.affiliation_basis
    assert (owner.purpose, owner.purpose_basis.startswith("itunes:owner")) == ("unknown", True)
    editor = routes["feed_editor_email"]
    assert "managingEditor" in editor.affiliation_basis
    assert editor.purpose == "unknown"
    website = routes["website"]
    assert (website.value, website.syntax_valid, website.purpose) == (
        "https://show.example.com/",
        True,
        "general",
    )
    assert not any(hasattr(r, "deliverability_status") for r in routes.values())


def test_invalid_contact_values_are_left_out_of_attrs_and_routes():
    body = (
        load("full_feed.xml")
        .replace(b"Owner@Example.com", b"owner@localhost")
        .replace(b"https://show.example.com/", b"javascript:alert(1)")
    )

    record = normalize_feed(1, "42", fetched(body))

    assert "owner_email" not in record.attrs
    assert "website" not in record.attrs
    assert {r.route_type for r in contact_routes_for(record)} == {"feed_editor_email"}


def test_no_routes_without_contact_fields():
    record = normalize_feed(1, "7", fetched(load("minimal_feed.xml")))

    assert contact_routes_for(record) == ()


def test_contact_route_is_immutable():
    route = contact_routes_for(full_record())[0]

    with pytest.raises(dataclasses.FrozenInstanceError):
        route.value = "x"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("age", "status"),
    [
        (timedelta(days=0), "active"),
        (timedelta(days=90), "active"),
        (timedelta(days=91), "dormant"),
        (timedelta(days=365), "dormant"),
        (timedelta(days=366), "dead"),
    ],
)
def test_activity_status_thresholds(age, status):
    assert activity_status(NOW - age, NOW) == status


def test_activity_status_without_a_date_is_unknown():
    assert activity_status(None, NOW) == "unknown"
