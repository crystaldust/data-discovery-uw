import uuid
from datetime import timedelta

import pytest

from ddm.db.migrate import migrate
from ddm.ingest.byline_classify import (
    BYLINE_FUZZY_METHOD,
    BYLINE_THRESHOLD,
    byline_neighbours,
    classify_byline_attached,
    classify_byline_unmatched,
    known_outlets,
)
from ddm.ingest.classify import NO_MATCH_METHOD, STRONG_METHOD, Neighbour
from ddm.ingest.records import make_record
from ddm.ingest.resolve import Attachment
from ddm.ingest.runs import register_run
from ddm.ingest.wikidata_sync import sync_journalists

ENTITY = uuid.UUID("00000000-0000-0000-0000-000000000001")
ATLANTIC, NYT = "Q1542536", "Q9684"
PAGE = ("outlet_author_url", "theatlantic.com/author/avery-example")


def attached(existing=True):
    return Attachment("theatlantic.com/author/avery-example", ENTITY, (PAGE,), existing)


def byline(name, outlet, key=None):
    return make_record(
        source_key=key or f"{outlet}:{name.lower()}",
        display_name=name,
        attrs={"outlet_qid": outlet, "outlet": "x", "bylines": []},
        source_updated_at=None,
        identifiers=(),
    )


# --- pure classification -----------------------------------------------------------------


def test_a_strong_hit_at_a_known_outlet_is_a_duplicate():
    result = classify_byline_attached(
        attached(), outlet_qid=ATLANTIC, known_outlets=frozenset({ATLANTIC})
    )

    assert (result.kind, result.matched_entity_id, result.method) == (
        "duplicate",
        ENTITY,
        STRONG_METHOD,
    )
    assert result.evidence["outlet_changed"] is False


def test_a_strong_hit_at_an_outlet_the_person_was_not_known_for_is_a_move():
    result = classify_byline_attached(
        attached(), outlet_qid=ATLANTIC, known_outlets=frozenset({NYT})
    )

    assert (result.kind, result.matched_entity_id) == ("rename_or_move", ENTITY)
    assert result.evidence == {
        "shared": [list(PAGE)],
        "outlet": ATLANTIC,
        "known_outlets": [NYT],
        "outlet_changed": True,
        "within_run": False,
    }


def test_a_strong_hit_on_an_entity_made_this_run_is_a_duplicate():
    result = classify_byline_attached(
        attached(existing=False), outlet_qid=ATLANTIC, known_outlets=frozenset()
    )

    assert result.kind == "duplicate"
    assert result.evidence["within_run"] is True


def test_a_neighbour_at_the_threshold_is_uncertain():
    best = Neighbour(ENTITY, BYLINE_THRESHOLD, {"name_sim": 1.0, "outlet_match": None})

    result = classify_byline_unmatched(best, threshold=BYLINE_THRESHOLD)

    assert (result.kind, result.matched_entity_id, result.method) == (
        "uncertain",
        ENTITY,
        BYLINE_FUZZY_METHOD,
    )


def test_a_weak_or_missing_neighbour_is_new_and_kept_as_evidence():
    weak = Neighbour(ENTITY, 0.59, {"name_sim": 0.84, "outlet_match": 0})

    result = classify_byline_unmatched(weak, threshold=BYLINE_THRESHOLD)

    assert (result.kind, result.matched_entity_id, result.method) == ("new", None, NO_MATCH_METHOD)
    assert result.evidence["best"]["entity_id"] == str(ENTITY)
    assert classify_byline_unmatched(None, threshold=BYLINE_THRESHOLD).evidence == {"best": None}


# --- database ----------------------------------------------------------------------------


@pytest.fixture
def journalists(fresh_db):
    """Avery (The Atlantic, per Wikidata) and Blake, from one Wikidata sync."""
    from tests.ingest.test_wikidata_sync import ATLANTIC_PEOPLE, DETAILS, NOW, FakeClient

    migrate(fresh_db)
    report = sync_journalists(
        fresh_db,
        FakeClient(ATLANTIC_PEOPLE, DETAILS),
        now=lambda: NOW,
        refused_backoff=timedelta(days=30),
    )
    return fresh_db, report.run_id


def byline_run(conn):
    return register_run(
        conn,
        source_id=5,
        run_kind="discovery",
        fingerprint="bylines",
        is_complete_snapshot=False,
        params={},
    ).run_id


def entity_named(conn, name):
    return conn.execute(
        "select entity_id from entities where display_name = %s", (name,)
    ).fetchone()[0]


@pytest.mark.integration
def test_known_outlets_come_from_wikidata_employers(journalists):
    conn, _ = journalists
    avery = entity_named(conn, "Avery Example")

    assert known_outlets(conn, [avery, ENTITY]) == {avery: frozenset({ATLANTIC})}


@pytest.mark.integration
def test_same_name_at_another_outlet_scores_the_name_weight(journalists):
    conn, _ = journalists
    run_id = byline_run(conn)

    with conn.transaction():
        found = byline_neighbours(conn, [byline("Avery Example", NYT)], run_id=run_id)

    best = found[f"{NYT}:avery example"][0]
    assert best.entity_id == entity_named(conn, "Avery Example")
    assert best.score == pytest.approx(0.7)
    assert dict(best.features) == {"name_sim": 1.0, "outlet_match": 0}


@pytest.mark.integration
def test_same_name_at_a_known_outlet_scores_one_but_is_still_only_a_neighbour(journalists):
    conn, _ = journalists
    run_id = byline_run(conn)

    with conn.transaction():
        found = byline_neighbours(conn, [byline("Avery Example", ATLANTIC)], run_id=run_id)

    assert found[f"{ATLANTIC}:avery example"][0].score == pytest.approx(1.0)


@pytest.mark.integration
def test_entities_of_this_run_and_podcasts_are_not_neighbours(journalists):
    conn, wikidata_run = journalists
    conn.execute(
        "insert into entities (entity_id, entity_type, display_name, first_seen_kind,"
        " first_seen_run) values (gen_random_uuid(), 'podcast', 'Blake Sample', 'observed', %s)",
        (wikidata_run,),
    )

    with conn.transaction():
        same_run = byline_neighbours(conn, [byline("Avery Example", NYT)], run_id=wikidata_run)
    with conn.transaction():
        later = byline_neighbours(conn, [byline("Blake Sample", NYT)], run_id=byline_run(conn))

    assert same_run == {}
    assert [n.entity_id for n in later[f"{NYT}:blake sample"]] == [
        entity_named(conn, "Blake Sample")
    ]
    assert len(later[f"{NYT}:blake sample"]) == 1


@pytest.mark.integration
def test_an_entity_without_a_wikidata_record_has_an_unknown_outlet_match(journalists):
    conn, wikidata_run = journalists
    conn.execute(
        "insert into entities (entity_id, entity_type, display_name, first_seen_kind,"
        " first_seen_run) values (gen_random_uuid(), 'journalist', 'Casey Writer', 'observed',"
        " %s)",
        (wikidata_run,),
    )

    with conn.transaction():
        found = byline_neighbours(conn, [byline("Casey Writer", NYT)], run_id=byline_run(conn))

    best = found[f"{NYT}:casey writer"][0]
    assert best.features["outlet_match"] is None
    assert best.score == pytest.approx(0.7)


@pytest.mark.integration
def test_records_without_a_name_are_not_looked_up(journalists):
    conn, _ = journalists
    nameless = make_record(
        source_key="k",
        display_name=None,
        attrs={"outlet_qid": NYT},
        source_updated_at=None,
        identifiers=(),
    )

    with conn.transaction():
        assert byline_neighbours(conn, [nameless], run_id=byline_run(conn)) == {}


@pytest.mark.integration
def test_neighbours_must_be_looked_up_inside_a_transaction(journalists):
    conn, _ = journalists

    with pytest.raises(RuntimeError, match="transaction"):
        byline_neighbours(conn, [byline("Avery Example", NYT)], run_id=1)
