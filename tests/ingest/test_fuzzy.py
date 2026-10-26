import hashlib
import uuid

import pytest
from psycopg.types.json import Jsonb

from ddm.db.migrate import migrate
from ddm.ingest.fuzzy import (
    AUTHOR_MIN,
    AUTHOR_WEIGHT,
    SITE_WEIGHT,
    TITLE_WEIGHT,
    author_similarity,
    best_neighbours,
    score,
    site_host,
    site_match,
)
from ddm.ingest.records import make_record


@pytest.mark.parametrize(
    ("url", "host"),
    [
        ("https://www.Example.com/about", "example.com"),
        ("http://shows.example.com", "shows.example.com"),
        ("not a url", None),
        ("ftp://example.com/x", None),
        (None, None),
    ],
)
def test_site_host_is_the_lowercase_host_without_www(url, host):
    assert site_host(url) == host


def test_same_own_website_matches():
    assert site_match("https://www.acme.com", None, "http://acme.com/podcast", None) == 1


def test_same_hosting_platform_is_not_evidence():
    assert site_match("https://anchor.fm/a", None, "https://anchor.fm/b", None) == 0
    assert site_match("https://a.podbean.com", None, "https://a.podbean.com", None) == 0


def test_identical_cover_art_matches_even_on_a_shared_platform():
    image = "https://cdn.example.com/art.jpg"

    assert site_match("https://anchor.fm/a", image, "https://anchor.fm/b", image) == 1


def test_site_match_is_unknown_when_one_side_has_nothing_to_compare():
    assert site_match(None, None, "https://acme.com", None) is None


def test_author_similarity_is_unknown_without_names_on_both_sides():
    assert author_similarity(None, dm_overlap=None) is None


def test_sounding_alike_lifts_author_similarity_to_the_floor():
    assert author_similarity(0.5, dm_overlap=True) == 0.8
    assert author_similarity(0.9, dm_overlap=True) == 0.9
    assert author_similarity(0.5, dm_overlap=False) == 0.5


def test_score_weights_title_author_and_site_and_counts_unknown_as_zero():
    assert TITLE_WEIGHT + AUTHOR_WEIGHT + SITE_WEIGHT == pytest.approx(1.0)
    assert score(1.0, 1.0, 1) == 1.0
    assert score(1.0, None, None) == TITLE_WEIGHT
    assert score(0.5, 0.5, 0) == round(0.5 * TITLE_WEIGHT + 0.5 * AUTHOR_WEIGHT, 4)


def test_unrelated_names_add_nothing_to_the_score():
    below = AUTHOR_MIN - 0.01

    assert score(1.0, below, None) == score(1.0, None, None)
    assert score(1.0, AUTHOR_MIN, None) > score(1.0, None, None)


# --- nearest neighbours in the database ---------------------------------------------------


def _run(conn):
    return conn.execute(
        "insert into import_runs (source_id, run_kind, input_fingerprint, is_complete_snapshot)"
        " values (1, 'historical_backfill', %s, true) returning run_id",
        (uuid.uuid4().hex,),
    ).fetchone()[0]


def _entity(conn, run_id, key, title, **attrs):
    entity_id = uuid.uuid4()
    conn.execute(
        "insert into entities (entity_id, entity_type, display_name, first_seen_kind,"
        " first_seen_run) values (%s, 'podcast', %s, 'historical', %s)",
        (entity_id, title, run_id),
    )
    conn.execute(
        "insert into source_records (source_id, source_key, entity_id, provenance, display_name,"
        " attrs, content_hash, first_seen_run, last_seen_run, last_changed_run)"
        " values (1, %s, %s, 'historical', %s, %s, %s, %s, %s, %s)",
        (
            key,
            entity_id,
            title,
            Jsonb(attrs),
            hashlib.sha256(key.encode()).digest(),
            run_id,
            run_id,
            run_id,
        ),
    )
    return entity_id


def candidate(key, title, **attrs):
    return make_record(
        source_key=key, display_name=title, attrs=attrs, source_updated_at=None, identifiers=()
    )


@pytest.fixture
def db(fresh_db):
    migrate(fresh_db)
    return fresh_db


@pytest.mark.integration
def test_the_same_title_by_the_same_author_scores_above_a_different_author(db):
    run_id = _run(db)
    same = _entity(db, run_id, "1", "Growth Marketing Weekly", author="Jane Doe")
    other = _entity(db, run_id, "2", "Growth Marketing Weekly!", author="Bob Smith")
    later = _run(db)

    with db.transaction():
        found = best_neighbours(
            db, [candidate("101", "Growth Marketing Weekly", author="Jane Doe")], run_id=later
        )

    ranked = found["101"]
    assert [n.entity_id for n in ranked[:2]] == [same, other]
    assert ranked[0].score > ranked[1].score
    assert ranked[0].features["author_sim"] == 1.0


@pytest.mark.integration
def test_entities_created_by_the_current_run_are_not_neighbours(db):
    run_id = _run(db)
    _entity(db, run_id, "1", "Growth Marketing Weekly")

    with db.transaction():
        found = best_neighbours(db, [candidate("101", "Growth Marketing Weekly")], run_id=run_id)

    assert found == {}


@pytest.mark.integration
def test_records_without_a_title_or_similar_names_have_no_neighbours(db):
    run_id = _run(db)
    _entity(db, run_id, "1", "Growth Marketing Weekly")
    later = _run(db)

    with db.transaction():
        found = best_neighbours(
            db, [candidate("101", None), candidate("102", "Quantum Cooking")], run_id=later
        )

    assert found == {}


@pytest.mark.integration
def test_neighbour_lookup_is_deterministic(db):
    run_id = _run(db)
    for key in ("1", "2", "3"):
        _entity(db, run_id, key, "The Marketing Show")
    later = _run(db)
    records = [candidate("101", "The Marketing Show")]

    with db.transaction():
        first = best_neighbours(db, records, run_id=later)
    with db.transaction():
        second = best_neighbours(db, records, run_id=later)

    assert first == second
    assert len(first["101"]) == 3


@pytest.mark.integration
def test_entities_of_a_later_registered_run_that_committed_first_are_neighbours(db):
    current = _run(db)
    later = _run(db)
    other = _entity(db, later, "1", "Growth Marketing Weekly")

    with db.transaction():
        found = best_neighbours(db, [candidate("101", "Growth Marketing Weekly")], run_id=current)

    assert found["101"][0].entity_id == other


@pytest.mark.integration
def test_neighbour_lookup_refuses_to_run_outside_a_transaction(db):
    with pytest.raises(RuntimeError, match="transaction"):
        best_neighbours(db, [candidate("101", "Anything")], run_id=1)
