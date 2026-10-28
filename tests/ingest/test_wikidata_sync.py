from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from ddm.db.migrate import migrate
from ddm.ingest.backoff import DiscoveryBackoff
from ddm.ingest.wikidata_sync import (
    JOURNALIST_LOCK_KEY,
    JournalistInputError,
    JournalistSyncBusy,
    sync_journalists,
)
from ddm.sources.podcastindex import SourceAccessDenied, SourceUnavailable

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 28, tzinfo=UTC)
ATLANTIC, NYT = "Q1542536", "Q9684"
ENTITY_URI = "http://www.wikidata.org/entity/"


def person(qid, name, start=None):
    row = {
        "person": {"type": "uri", "value": ENTITY_URI + qid},
        "personLabel": {"type": "literal", "value": name},
    }
    if start:
        row["start"] = {"type": "literal", "value": f"{start}-01-01T00:00:00Z"}
        row["startPrecision"] = {"type": "literal", "value": "9"}
    return row


def detail(qid, prop, value):
    return {
        "person": {"type": "uri", "value": ENTITY_URI + qid},
        "prop": {"type": "literal", "value": prop},
        "value": {"type": "literal", "value": value},
    }


class FakeClient:
    """Answers like WDQS from fixed rows; can refuse or fail on the n-th request."""

    def __init__(self, people, details=(), *, refuse_at=None, fail_at=None, retry_after=None):
        self.people = people
        self.details = list(details)
        self.refuse_at, self.fail_at, self.retry_after = refuse_at, fail_at, retry_after
        self.requests = 0

    def select(self, query, what):
        self.requests += 1
        if self.requests == self.refuse_at:
            raise SourceAccessDenied(429, self.retry_after, "sparql")
        if self.requests == self.fail_at:
            raise SourceUnavailable("down")
        if what.startswith("outlet="):
            return self.people.get(what.removeprefix("outlet="), [])
        return [
            d for d in self.details if f"wd:{d['person']['value'][len(ENTITY_URI) :]} " in query
        ]


ATLANTIC_PEOPLE = {ATLANTIC: [person("Q1", "Avery Example", 2017), person("Q2", "Blake Sample")]}
DETAILS = [
    detail("Q1", "P6791", "avery-example"),
    detail("Q1", "P856", "https://avery.example.org/"),
]


@pytest.fixture
def db(fresh_db):
    migrate(fresh_db)
    return fresh_db


def sync(conn, client, now=NOW):
    return sync_journalists(conn, client, now=lambda: now, refused_backoff=timedelta(days=30))


def scalar(conn, sql, params=()):
    return conn.execute(sql, params).fetchone()[0]


def entity_of(conn, qid):
    return scalar(
        conn, "select entity_id from source_records where source_id = 4 and source_key = %s", (qid,)
    )


def test_the_first_complete_sync_is_the_historical_baseline(db):
    report = sync(db, FakeClient(ATLANTIC_PEOPLE, DETAILS))

    assert (report.status, report.provenance, report.stopped) == ("ok", "historical", None)
    assert report.metrics.rows_new == 2 and report.metrics.entities_created == 2
    assert report.metrics.http_requests == 21
    kinds = db.execute("select distinct entity_type, first_seen_kind from entities").fetchall()
    assert kinds == [("journalist", "historical")]
    run_kind = scalar(db, "select run_kind from import_runs where run_id = %s", (report.run_id,))
    assert run_kind == "historical_backfill"


def test_people_get_qid_and_author_page_identifiers_and_unconfirmed_routes(db):
    sync(db, FakeClient(ATLANTIC_PEOPLE, DETAILS))
    avery = entity_of(db, "Q1")

    identifiers = db.execute(
        "select id_kind, id_value from entity_identifiers where entity_id = %s order by 1",
        (avery,),
    ).fetchall()
    assert identifiers == [
        ("outlet_author_url", "theatlantic.com/author/avery-example"),
        ("wikidata_qid", "Q1"),
    ]
    routes = db.execute(
        "select route_type, value, affiliation_status, purpose from contact_routes"
        " where entity_id = %s order by 1",
        (avery,),
    ).fetchall()
    assert routes == [
        (
            "outlet_author_page",
            "https://theatlantic.com/author/avery-example",
            "unconfirmed",
            "unknown",
        ),
        ("website", "https://avery.example.org/", "unconfirmed", "unknown"),
    ]


def test_the_same_data_again_writes_no_new_observations(db):
    sync(db, FakeClient(ATLANTIC_PEOPLE, DETAILS))

    report = sync(db, FakeClient(ATLANTIC_PEOPLE, DETAILS), now=NOW + timedelta(days=1))

    assert report.provenance == "observed"
    assert (report.metrics.rows_new, report.metrics.rows_changed) == (0, 0)
    assert report.metrics.entities_created == 0
    assert scalar(db, "select count(*) from observations where run_id = %s", (report.run_id,)) == 0


def test_a_new_employer_is_a_changed_observation_of_the_same_entity(db):
    sync(db, FakeClient(ATLANTIC_PEOPLE, DETAILS))
    avery = entity_of(db, "Q1")
    moved = {**ATLANTIC_PEOPLE, NYT: [person("Q1", "Avery Example", 2024)]}

    report = sync(db, FakeClient(moved, DETAILS), now=NOW + timedelta(days=1))

    assert report.metrics.rows_changed == 1
    assert entity_of(db, "Q1") == avery
    payload = scalar(
        db,
        "select payload from observations where run_id = %s and source_key = 'Q1'",
        (report.run_id,),
    )
    assert [e["outlet_qid"] for e in payload["attrs"]["employments"]] == [ATLANTIC, NYT]


def test_a_person_missing_from_two_complete_snapshots_is_soft_deleted(db):
    sync(db, FakeClient(ATLANTIC_PEOPLE, DETAILS))
    only_avery = {ATLANTIC: ATLANTIC_PEOPLE[ATLANTIC][:1]}
    sync(db, FakeClient(only_avery, DETAILS), now=NOW + timedelta(days=1))

    report = sync(db, FakeClient(only_avery, DETAILS), now=NOW + timedelta(days=2))

    assert report.metrics.rows_deleted == 1
    assert scalar(db, "select deleted_at is not null from source_records where source_key = 'Q2'")
    assert scalar(db, "select count(*) from entities") == 2


def test_a_refusal_writes_nothing_and_backs_off(db):
    report = sync(db, FakeClient(ATLANTIC_PEOPLE, DETAILS, refuse_at=3))

    assert (report.status, report.stopped) == ("partial", "refused")
    assert scalar(db, "select count(*) from source_records") == 0
    assert scalar(db, "select http_status from source_backoff where source_id = 4") == 429
    with pytest.raises(DiscoveryBackoff):
        sync(db, FakeClient(ATLANTIC_PEOPLE, DETAILS))


def test_an_unavailable_source_writes_nothing(db):
    report = sync(db, FakeClient(ATLANTIC_PEOPLE, DETAILS, fail_at=21))

    assert (report.status, report.stopped) == ("partial", "unavailable")
    assert scalar(db, "select count(*) from source_records") == 0
    assert report.metrics.http_requests == 21


def test_a_partial_run_does_not_make_the_next_one_observed(db):
    sync(db, FakeClient(ATLANTIC_PEOPLE, DETAILS, fail_at=1))

    report = sync(db, FakeClient(ATLANTIC_PEOPLE, DETAILS), now=NOW + timedelta(hours=1))

    assert report.provenance == "historical"


def test_a_new_author_id_is_attached_to_the_existing_person(db):
    sync(db, FakeClient(ATLANTIC_PEOPLE, DETAILS))
    more = [*DETAILS, detail("Q2", "P8297", "blake-sample")]

    report = sync(db, FakeClient(ATLANTIC_PEOPLE, more), now=NOW + timedelta(days=1))

    assert report.metrics.identifiers_added == 1
    owner = scalar(
        db,
        "select entity_id from entity_identifiers where id_value = 'nytimes.com/by/blake-sample'",
    )
    assert owner == entity_of(db, "Q2")


def test_an_author_id_another_person_already_has_becomes_ambiguous(db):
    sync(db, FakeClient(ATLANTIC_PEOPLE, DETAILS))
    clash = [*DETAILS, detail("Q2", "P6791", "avery-example")]

    report = sync(db, FakeClient(ATLANTIC_PEOPLE, clash), now=NOW + timedelta(days=1))

    assert report.metrics.ambiguous_identifiers == 1
    assert entity_of(db, "Q2") != entity_of(db, "Q1")
    owners = scalar(
        db,
        "select count(*) from entity_identifiers"
        " where id_value = 'theatlantic.com/author/avery-example'",
    )
    assert owners == 1  # still only Q1's; bylines skip it while it is ambiguous


def test_an_author_page_claimed_by_two_people_is_ambiguous_and_never_merges(db):
    people = {ATLANTIC: [person("Q1", "Avery Example"), person("Q3", "Avery Example")]}
    details = [detail("Q1", "P6791", "avery-example"), detail("Q3", "P6791", "avery-example")]

    report = sync(db, FakeClient(people, details))

    assert report.metrics.entities_created == 2
    assert report.metrics.ambiguous_identifiers == 1
    assert (
        scalar(db, "select count(*) from ambiguous_identifiers where id_kind = 'outlet_author_url'")
        == 1
    )
    assert (
        scalar(db, "select count(*) from entity_identifiers where id_kind = 'outlet_author_url'")
        == 0
    )


def test_a_held_lock_means_busy(db, pg_url):
    with psycopg.connect(pg_url, autocommit=True) as other:
        other.execute("select pg_advisory_lock(%s)", (JOURNALIST_LOCK_KEY,))
        with pytest.raises(JournalistSyncBusy):
            sync(db, FakeClient(ATLANTIC_PEOPLE, DETAILS))


def test_a_transactional_connection_is_refused(pg_url, db):
    with psycopg.connect(pg_url) as conn, pytest.raises(JournalistInputError):
        sync(conn, FakeClient(ATLANTIC_PEOPLE, DETAILS))


def test_a_crashed_run_is_marked_failed_and_the_lock_is_released(db, pg_url):
    class Broken(FakeClient):
        def select(self, query, what):
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        sync(db, Broken(ATLANTIC_PEOPLE))

    assert scalar(db, "select status from import_runs") == "failed"
    with psycopg.connect(pg_url, autocommit=True) as other:
        assert scalar(other, "select pg_try_advisory_lock(%s)", (JOURNALIST_LOCK_KEY,))


def test_an_outlet_that_comes_back_empty_withholds_deletions(db):
    both = {**ATLANTIC_PEOPLE, NYT: [person("Q5", "Casey Writer")]}
    sync(db, FakeClient(both, DETAILS))
    sync(db, FakeClient(ATLANTIC_PEOPLE, DETAILS), now=NOW + timedelta(days=1))

    report = sync(db, FakeClient(ATLANTIC_PEOPLE, DETAILS), now=NOW + timedelta(days=2))

    assert report.status == "partial"
    assert report.deletions_withheld == 1
    assert report.metrics.rows_deleted == 0
    assert scalar(db, "select count(*) from source_records where deleted_at is not null") == 0
