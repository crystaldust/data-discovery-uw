import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from ddm.ingest.records import NormalizedRecord, Reject
from ddm.sources.podcastindex import SourceAccessDenied
from ddm.sources.podcastindex_api import (
    MAX_FEED_ID,
    SETTLE_DAYS,
    fetch_feed,
    has_known_age,
    in_demo_slice,
    is_english,
    is_settled,
    iter_newfeeds,
    normalize_api_feed,
)
from tests.sources.test_podcastindex import make_client

FIXTURES = Path(__file__).parent.parent / "fixtures" / "podcastindex"
NOW = datetime(2026, 10, 30, tzinfo=UTC)


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


def full_feed(**overrides):
    return {**fixture("podcasts_byfeedid.json")["feed"], **overrides}


def listed_feed(**overrides):
    return {**fixture("recent_newfeeds_from_id.json")["feeds"][0], **overrides}


class Pages:
    """Answers /recent/newfeeds like the real API: ids >= feedid, ascending, at most max."""

    def __init__(self, ids):
        self.ids = sorted(ids)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        start = int(request.url.params["feedid"])
        size = int(request.url.params["max"])
        page = [i for i in self.ids if i >= start][:size]
        return httpx.Response(200, json={"status": True, "feeds": [{"id": i} for i in page]})


def test_newfeeds_pages_forward_from_the_start_without_repeats_or_gaps():
    pages = Pages(range(10, 25))

    with make_client(pages) as client:
        ids = [f["id"] for f in iter_newfeeds(client, start_at=12, page_size=5)]

    assert ids == list(range(12, 25))
    assert [r.url.params["feedid"] for r in pages.requests] == ["12", "17", "22", "25"]


def test_newfeeds_stops_on_an_empty_page():
    with make_client(Pages([])) as client:
        assert list(iter_newfeeds(client, start_at=1, page_size=5)) == []


def test_newfeeds_sends_the_honest_user_agent():
    pages = Pages([1])

    with make_client(pages) as client:
        list(iter_newfeeds(client, start_at=1))

    assert pages.requests[0].headers["User-Agent"] == "DataDiscoveryDemo/test"


def test_a_refusal_stops_paging_and_is_not_retried():
    calls = []

    def refuse(request):
        calls.append(request)
        return httpx.Response(403)

    with make_client(refuse) as client, pytest.raises(SourceAccessDenied):
        list(iter_newfeeds(client, start_at=1))

    assert len(calls) == 1


def test_fetch_feed_returns_the_full_feed():
    def answer(request):
        assert request.url.params["id"] == "8087486"
        return httpx.Response(200, json=fixture("podcasts_byfeedid.json"))

    with make_client(answer) as client:
        feed = fetch_feed(client, 8087486)

    assert feed["podcastGuid"] == "cc21ce02-c550-5400-aa8e-a6519f24aace"


@pytest.mark.parametrize("body", [{"status": "true", "feed": []}, {"status": "true"}])
def test_fetch_feed_of_an_unknown_id_is_none(body):
    with make_client(lambda request: httpx.Response(200, json=body)) as client:
        assert fetch_feed(client, 1) is None


def test_normalized_api_feed_uses_the_dump_attribute_names():
    record = normalize_api_feed(1, listed_feed(), full_feed())

    assert isinstance(record, NormalizedRecord)
    assert record.source_key == "8087486"
    assert record.display_name == "Tumbas Abiertas"
    attrs = dict(record.attrs)
    assert attrs["feed_url"] == "https://anchor.fm/s/118d81b20/podcast/rss"
    assert attrs["podcast_guid"] == "cc21ce02-c550-5400-aa8e-a6519f24aace"
    assert attrs["itunes_id"] == 6825066418
    assert attrs["author"] == attrs["owner_name"] == "Lystra"
    assert attrs["link"] == "https://podcasters.spotify.com/pod/show/lystra38"
    assert attrs["language"] == "es-mx"
    assert attrs["episode_count"] == 1
    assert attrs["categories"] == ["history"]
    assert attrs["created_on"] == datetime.fromtimestamp(1792998673, UTC).isoformat()
    assert attrs["last_update_at"] == datetime.fromtimestamp(1792998717, UTC).isoformat()


def test_normalized_api_feed_has_the_dump_identifier_order():
    record = normalize_api_feed(1, listed_feed(), full_feed())

    assert record.identifiers == (
        ("podcast_guid", "cc21ce02-c550-5400-aa8e-a6519f24aace"),
        ("itunes_id", "6825066418"),
        ("podcastindex_id", "8087486"),
        ("feed_url", "anchor.fm/s/118d81b20/podcast/rss"),
    )


def test_unknown_api_values_are_left_out():
    feed = full_feed(itunesId=0, podcastGuid="", author=None, link="", lastUpdateTime=0)

    attrs = dict(normalize_api_feed(1, listed_feed(), feed).attrs)

    assert not {"itunes_id", "podcast_guid", "author", "link", "last_update_at"} & attrs.keys()


def test_categories_are_lowercase_unique_and_in_id_order():
    feed = full_feed(categories={"9": "Marketing", "3": "Business", "12": "marketing"})

    assert normalize_api_feed(1, listed_feed(), feed).attrs["categories"] == [
        "business",
        "marketing",
    ]


def test_a_feed_podcast_index_has_not_parsed_yet_is_rejected():
    feed = full_feed(title="", lastParseTime=0)

    result = normalize_api_feed(3, listed_feed(), feed)

    assert isinstance(result, Reject)
    assert (result.row_number, result.reason, result.source_key) == (3, "not_parsed", "8087486")


def test_an_invalid_feed_address_is_rejected():
    result = normalize_api_feed(1, listed_feed(), full_feed(url="not a url"))

    assert isinstance(result, Reject) and result.reason == "invalid_feed_url"


def test_demo_slice_needs_english_three_episodes_and_a_demo_category():
    def record(**overrides):
        base = {"language": "en-us", "episodeCount": 5, "categories": {"1": "Technology"}}
        return normalize_api_feed(1, listed_feed(), full_feed(**{**base, **overrides}))

    assert in_demo_slice(record())
    assert not in_demo_slice(record(language="es"))
    assert not in_demo_slice(record(episodeCount=2))
    assert not in_demo_slice(record(categories={"1": "History"}))


def test_only_english_listings_are_worth_a_full_lookup():
    assert is_english(listed_feed(language="EN-gb"))
    assert not is_english(listed_feed(language="es-mx"))
    assert not is_english(listed_feed(language=None))


def test_a_listing_is_settled_once_podcast_index_has_had_time_to_parse_it():
    added = int(NOW.timestamp())

    assert is_settled(listed_feed(timeAdded=added - SETTLE_DAYS * 86400), NOW)
    assert not is_settled(listed_feed(timeAdded=added - 3600), NOW)
    assert not is_settled(listed_feed(timeAdded=None), NOW)


def test_ids_are_normalised_to_bounded_integers_while_paging():
    def answer(request):
        if request.url.params["feedid"] != "5":
            return httpx.Response(200, json={"feeds": []})
        feeds = [{"id": "5"}, {"id": MAX_FEED_ID + 1}, {"id": -3}, {"id": "x"}, {"id": 7.5}]
        return httpx.Response(200, json={"feeds": feeds})

    with make_client(answer) as client:
        listed = list(iter_newfeeds(client, start_at=5))

    assert listed == [{"id": 5}]


def test_a_full_feed_for_another_id_is_rejected():
    result = normalize_api_feed(1, listed_feed(), full_feed(id=1, feedId=1))

    assert isinstance(result, Reject) and result.reason == "id_mismatch"


def test_reject_raw_values_are_safe_for_the_database():
    feed = full_feed(title="", lastParseTime=0, description="a\x00b\ud800c" + "x" * 5000)

    raw = normalize_api_feed(1, listed_feed(), feed).raw

    description = raw["feed"]["description"]
    assert "\x00" not in description and "\ud800" not in description
    assert len(description) <= 2000
    description.encode("utf-8")  # no lone surrogates left


def test_lone_surrogates_in_titles_are_replaced():
    record = normalize_api_feed(1, listed_feed(), full_feed(title="Bad \ud800 title"))

    record.display_name.encode("utf-8")


def test_a_listing_without_a_time_added_has_no_known_age():
    assert has_known_age(listed_feed())
    assert not has_known_age(listed_feed(timeAdded=None))
    assert not has_known_age(listed_feed(timeAdded=0))
