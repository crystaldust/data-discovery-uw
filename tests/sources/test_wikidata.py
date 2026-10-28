import json
from pathlib import Path

import httpx
import pytest

from ddm.ingest.records import content_hash
from ddm.sources.podcastindex import SourceAccessDenied, SourceResponseError, SourceUnavailable
from ddm.sources.wikidata import (
    AUTHOR_ID_RULES,
    OUTLETS,
    SPARQL_URL,
    WikidataClient,
    WikidataSnapshot,
    author_url,
    detail_query,
    detail_rows,
    employer_query,
    employment_rows,
    fetch_people,
    normalize_people,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "wikidata"
UA = "DataDiscoveryDemo/test (+mailto:test@example.com)"
ATLANTIC = "Q1542536"
NYT = "Q9684"


def bindings(name: str) -> list[dict]:
    return json.loads((FIXTURES / name).read_text())["results"]["bindings"]


def sparql_body(rows: list[dict]) -> dict:
    return {"head": {"vars": []}, "results": {"bindings": rows}}


def make_client(handler, **kwargs):
    defaults = {"sleep": lambda s: None, "retry_wait_seconds": 0, "min_interval": 0}
    return WikidataClient(UA, transport=httpx.MockTransport(handler), **{**defaults, **kwargs})


def snapshot(employments=None, details=None) -> WikidataSnapshot:
    rows = employment_rows(ATLANTIC, bindings("employers_atlantic.json"))
    return WikidataSnapshot(
        employments=rows if employments is None else employments,
        details=detail_rows(bindings("details.json")) if details is None else details,
    )


def by_key(records):
    return {r.source_key: r for r in records}


# --- client -------------------------------------------------------------------------------


def test_client_sends_the_honest_user_agent_and_asks_for_sparql_json():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=sparql_body([]))

    with make_client(handler) as client:
        assert client.select("SELECT * WHERE {}", "test") == []

    request = seen[0]
    assert str(request.url).startswith(SPARQL_URL)
    assert request.headers["User-Agent"] == UA
    assert request.headers["Accept"] == "application/sparql-results+json"
    assert request.url.params["format"] == "json"
    assert request.url.params["query"] == "SELECT * WHERE {}"


def test_a_refusal_is_raised_at_once_with_its_retry_after():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "120"})

    with make_client(handler) as client, pytest.raises(SourceAccessDenied) as caught:
        client.select("q", "test")

    assert (caught.value.status_code, caught.value.retry_after) == (429, "120")
    assert len(calls) == 1


def test_server_errors_are_retried_and_counted():
    statuses = iter([503, 503, 200])

    def handler(request):
        status = next(statuses)
        return httpx.Response(status, json=sparql_body([]) if status == 200 else None)

    with make_client(handler) as client:
        client.select("q", "test")
        assert client.requests == 3


def test_a_source_that_keeps_failing_is_unavailable():
    with make_client(lambda r: httpx.Response(502)) as client, pytest.raises(SourceUnavailable):
        client.select("q", "test")


def test_network_errors_are_retried():
    def handler(request):
        raise httpx.ConnectError("down")

    with make_client(handler, max_attempts=2) as client, pytest.raises(SourceUnavailable):
        client.select("q", "test")
    assert client.requests == 2


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="<html>not json</html>"),
        httpx.Response(200, json={"results": {}}),
        httpx.Response(200, json=["not", "an", "object"]),
        httpx.Response(400, text="bad query"),
        httpx.Response(302, headers={"Location": "https://elsewhere.example.com/"}),
    ],
)
def test_unusable_answers_are_response_errors(response):
    with make_client(lambda r: response) as client, pytest.raises(SourceResponseError):
        client.select("q", "test")


def test_queries_are_spaced_by_the_minimum_interval():
    clock = [100.0]
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        clock[0] += seconds

    client = make_client(
        lambda r: httpx.Response(200, json=sparql_body([])),
        sleep=sleep,
        monotonic=lambda: clock[0],
        min_interval=1.5,
    )
    with client:
        client.select("q", "a")
        clock[0] += 0.5
        client.select("q", "b")

    assert slept == [pytest.approx(1.0)]


# --- queries ------------------------------------------------------------------------------


def test_employer_query_names_the_outlet_and_the_journalist_occupation():
    query = employer_query(ATLANTIC)

    assert f"wd:{ATLANTIC}" in query and "wd:Q1930187" in query
    assert "DeprecatedRank" in query


@pytest.mark.parametrize("bad", ["Q1 } DROP", "P31", ""])
def test_queries_refuse_anything_but_a_qid(bad):
    with pytest.raises(ValueError):
        employer_query(bad)
    with pytest.raises(ValueError):
        detail_query(["Q1", bad])


def test_detail_query_lists_every_person_and_property():
    query = detail_query(["Q2", "Q10"])

    assert "wd:Q2 wd:Q10" in query
    for prop in ("P856", "P2002", *AUTHOR_ID_RULES):
        assert f'"{prop}"' in query


def test_fetch_people_asks_every_outlet_then_details_in_qid_order():
    asked = []

    class FakeClient:
        def select(self, query, what):
            asked.append(what)
            if what == f"outlet={ATLANTIC}":
                return bindings("employers_atlantic.json")
            if what.startswith("details"):
                assert "wd:Q900001 wd:Q900002 wd:Q900003" in query
                assert "Q900004" not in query
                return bindings("details.json")
            return []

    found = fetch_people(FakeClient())

    assert asked[: len(OUTLETS)] == [f"outlet={qid}" for qid in OUTLETS]
    assert asked[len(OUTLETS) :] == ["details=1"]
    assert len(found.employments) == 5 and len(found.details) == 7


def test_fetch_people_with_nobody_found_asks_no_details():
    class FakeClient:
        def select(self, query, what):
            assert not what.startswith("details")
            return []

    assert fetch_people(FakeClient()) == WikidataSnapshot(employments=(), details=())


# --- normalisation ------------------------------------------------------------------------


def test_each_person_is_one_record_keyed_by_qid_and_bad_qids_are_rejected():
    records, rejects = normalize_people(snapshot())

    assert sorted(by_key(records)) == ["Q900001", "Q900002", "Q900003"]
    assert [r.reason for r in rejects if r.reason == "invalid_qid"] == ["invalid_qid"]


def test_a_label_that_is_just_the_qid_means_the_name_is_unknown():
    records, _ = normalize_people(snapshot())

    assert by_key(records)["Q900002"].display_name is None
    assert by_key(records)["Q900001"].display_name == "Avery Example"


def test_dates_keep_only_the_precision_wikidata_states():
    records, _ = normalize_people(snapshot())

    assert by_key(records)["Q900001"].attrs["employments"] == [
        {"outlet_qid": ATLANTIC, "outlet": "The Atlantic", "start": "2009", "end": "2012-06-15"},
        {"outlet_qid": ATLANTIC, "outlet": "The Atlantic", "start": "2017-04"},
    ]
    assert by_key(records)["Q900002"].attrs["employments"] == [
        {"outlet_qid": ATLANTIC, "outlet": "The Atlantic"}
    ]


def test_a_repeated_statement_is_kept_once():
    rows = employment_rows(ATLANTIC, bindings("employers_atlantic.json")[:1] * 2)

    records, _ = normalize_people(snapshot(employments=rows, details=()))

    assert len(by_key(records)["Q900001"].attrs["employments"]) == 1


def test_negative_years_are_unknown():
    row = {
        "person": {"value": "http://www.wikidata.org/entity/Q900005"},
        "start": {"value": "-0050-01-01T00:00:00Z"},
        "startPrecision": {"value": "9"},
    }

    records, _ = normalize_people(snapshot(employments=employment_rows(NYT, [row]), details=()))

    assert by_key(records)["Q900005"].attrs["employments"] == [
        {"outlet_qid": NYT, "outlet": "The New York Times"}
    ]


def test_author_ids_become_author_page_identifiers():
    records, _ = normalize_people(snapshot())
    avery = by_key(records)["Q900001"]

    assert avery.identifiers == (
        ("outlet_author_url", "nytimes.com/by/avery-example"),
        ("outlet_author_url", "theatlantic.com/author/avery-example"),
        ("wikidata_qid", "Q900001"),
    )
    assert avery.attrs["author_ids"] == {"P6791": ["avery-example"], "P8297": ["avery-example"]}
    assert avery.attrs["websites"] == ["https://avery.example.org/"]
    assert avery.attrs["x_usernames"] == ["avery_example"]


def test_invalid_external_values_are_dropped_and_rejected_but_the_person_is_kept():
    records, rejects = normalize_people(snapshot())
    blake = by_key(records)["Q900003"]

    assert blake.identifiers == (("wikidata_qid", "Q900003"),)
    assert not {"author_ids", "websites", "x_usernames"} & set(blake.attrs)
    invalid = [dict(r.raw) for r in rejects if r.reason == "invalid_external_id"]
    assert sorted(r["property"] for r in invalid) == ["P2002", "P6791", "P856"]
    assert all(r["qid"] == "Q900003" for r in invalid)


def test_reject_row_numbers_are_unique():
    _, rejects = normalize_people(snapshot())

    numbers = [r.row_number for r in rejects]
    assert len(numbers) == len(set(numbers)) == 4


def test_content_does_not_depend_on_the_order_wikidata_returns_rows():
    forward, _ = normalize_people(snapshot())
    reversed_snapshot = WikidataSnapshot(
        employments=tuple(reversed(snapshot().employments)),
        details=tuple(reversed(snapshot().details)),
    )
    backward, _ = normalize_people(reversed_snapshot)

    assert {r.source_key: content_hash(r) for r in forward} == {
        r.source_key: content_hash(r) for r in backward
    }
    assert [r.source_key for r in forward] == [r.source_key for r in backward]


@pytest.mark.parametrize(
    ("prop", "value", "expected"),
    [
        ("P6791", "a-b", "https://www.theatlantic.com/author/a-b/"),
        ("P8297", "a-b", "https://www.nytimes.com/by/a-b"),
        ("P7943", "a-b", "https://www.washingtonpost.com/people/a-b/"),
        ("P9420", "a-b", "https://www.cnn.com/profiles/a-b"),
        ("P10723", "a-b", "https://www.theguardian.com/profile/a-b"),
        ("P6791", "../../admin", None),
        ("P6791", "A-B", None),
        ("P6791", "a--b", None),
        ("P6005", "a-b", None),
    ],
)
def test_author_urls_are_built_only_from_valid_ids(prop, value, expected):
    assert author_url(prop, value) == expected


@pytest.mark.parametrize(
    ("value", "kept"),
    [
        ("https://avery.example.org/", True),
        ("http://avery.example.org/blog", True),
        ("http://[abc", False),
        ("https://user:pw@avery.example.org/", False),
        ("https://avery.example.org:99999/", False),
        ("https://a\nb.example.org/", False),
        ("https://localhost/", False),
        ("http://127.0.0.1/", False),
        ("http://[::1]/", False),
        ("http://intranet.internal/", False),
        ("javascript:alert(1)", False),
    ],
)
def test_only_public_web_addresses_are_kept_as_websites(value, kept):
    details = detail_rows(
        [
            {
                "person": {"value": "http://www.wikidata.org/entity/Q900001"},
                "prop": {"value": "P856"},
                "value": {"value": value},
            }
        ]
    )

    records, rejects = normalize_people(snapshot(details=details))

    assert ("websites" in by_key(records)["Q900001"].attrs) is kept
    assert any(r.reason == "invalid_external_id" for r in rejects) is not kept
