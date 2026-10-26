import pytest

from ddm.db.migrate import migrate
from ddm.ingest.baseline import import_baseline
from ddm.ingest.discovery import discover_batch
from ddm.ingest.records import NormalizedRecord, make_record
from ddm.ingest.runs import register_run
from ddm.sources.podcastindex_dump import normalize_dump_row
from tests.conftest import dump_row, make_dump

pytestmark = pytest.mark.integration

AFTER_CUTOFF = 1710000000  # 2024-03-09
THRESHOLD = 0.6


def baseline_rows():
    return [
        dump_row(1, podcastGuid="g1", title="Show One"),
        dump_row(2, title="Second Show"),
        dump_row(3, title="Show Three"),
        dump_row(4, podcastGuid="g4", title="Fourth"),
        dump_row(5, itunesId=905, title="Fifth"),
        dump_row(
            6, title="Growth Marketing Weekly", itunesAuthor="Jane Doe", link="https://janedoe.com"
        ),
        dump_row(7, title="Seventh"),
        dump_row(8, podcastGuid="g8", title="Eighth"),
    ]


def candidate_rows():
    late = {"createdOn": AFTER_CUTOFF}
    return [
        dump_row(101, podcastGuid="g1", title="Show One", **late),  # moved -> rename_or_move
        dump_row(102, url="http://feeds.example.com/3.xml", title="Show Three", **late),  # dup
        # A GUID under another title does not count (v2); the Apple ID of 5 decides.
        dump_row(103, podcastGuid="g4", itunesId=905, title="Mixed", **late),  # rename of 5
        dump_row(
            104,
            title="Growth Marketing Weekly",
            itunesAuthor="Jane Doe",
            link="https://www.janedoe.com/",
            **late,
        ),  # fuzzy -> uncertain
        dump_row(
            105,
            title="Growth Marketing Weekly",
            itunesAuthor="Someone Else",
            link="https://anchor.fm/x",
            **late,
        ),  # title only -> new
        dump_row(106, title="Totally Unrelated Zebra", duplicateOf=7, **late),  # PI says dup
        dump_row(107, podcastGuid="g-new", title="Brand New Alpha", **late),  # new
        dump_row(108, podcastGuid="g-new", title="Brand New Beta", **late),  # GUID only -> review
        dump_row(109, podcastGuid="g8", title="Ninth Quite Different", **late),  # ambiguous
    ]


def normalized(rows) -> list[NormalizedRecord]:
    return [normalize_dump_row(i, row) for i, row in enumerate(rows, start=1)]


@pytest.fixture
def db(fresh_db, tmp_path):
    migrate(fresh_db)
    import_baseline(fresh_db, make_dump(tmp_path, baseline_rows() + candidate_rows()))
    return fresh_db


def new_run(conn, fingerprint):
    return register_run(
        conn,
        source_id=1,
        run_kind="discovery",
        fingerprint=fingerprint,
        is_complete_snapshot=False,
        params={},
    ).run_id


def discover(
    conn,
    records,
    *,
    fingerprint="d1",
    source_name="podcastindex_dump",
    source_id=1,
    ambiguous=frozenset({("podcast_guid", "g8")}),
):
    run_id = new_run(conn, fingerprint)
    with conn.transaction():
        counts = discover_batch(
            conn,
            records,
            source_name=source_name,
            source_id=source_id,
            run_id=run_id,
            provenance="historical",
            ambiguous=ambiguous,
            threshold=THRESHOLD,
        )
    return run_id, counts


def classes(conn, run_id):
    rows = conn.execute(
        "select source_key, classification, method from discoveries where run_id = %s",
        (run_id,),
    ).fetchall()
    return {key: (kind, method) for key, kind, method in rows}


def entity_of(conn, key, source_id=1):
    return conn.execute(
        "select entity_id from source_records where source_id = %s and source_key = %s",
        (source_id, key),
    ).fetchone()[0]


def count(conn, table):
    return conn.execute(f"select count(*) from {table}").fetchone()[0]  # noqa: S608


def test_each_candidate_gets_the_expected_class(db):
    run_id, counts = discover(db, normalized(candidate_rows()))

    assert classes(db, run_id) == {
        "101": ("rename_or_move", "strong_id_v1"),
        "102": ("duplicate", "strong_id_v1"),
        "103": ("rename_or_move", "strong_id_v1"),
        "104": ("uncertain", "fuzzy_v1"),
        "105": ("new", "no_match_v1"),
        "106": ("uncertain", "pi_duplicate_of_v1"),
        "107": ("new", "no_match_v1"),
        "108": ("uncertain", "unconfirmed_guid_v1"),
        "109": ("new", "no_match_v1"),
    }
    assert (counts.new, counts.rename_or_move, counts.duplicate, counts.uncertain) == (3, 2, 1, 3)


def test_attached_records_join_the_existing_entity_and_add_an_observation(db):
    run_id, _ = discover(db, normalized(candidate_rows()))

    assert entity_of(db, "101") == entity_of(db, "1")
    assert entity_of(db, "102") == entity_of(db, "3")
    observed = db.execute(
        "select change_kind from observations where run_id = %s and source_key = '101'",
        (run_id,),
    ).fetchone()
    assert observed == ("new",)


def test_uncertain_and_new_records_get_their_own_discovered_entity(db):
    discover(db, normalized(candidate_rows()))

    for key, baseline in (("103", "4"), ("104", "6"), ("106", "7"), ("109", "8")):
        assert entity_of(db, key) != entity_of(db, baseline)
    kinds = db.execute(
        "select distinct e.first_seen_kind from discoveries d join entities e using (entity_id)"
        " where d.classification in ('new', 'uncertain')"
    ).fetchall()
    assert kinds == [("discovered",)]


def test_review_candidates_carry_their_method_and_score(db):
    discover(db, normalized(candidate_rows()))

    rows = db.execute(
        "select source_key, method, score from candidate_matches where source_key like '10%'"
    ).fetchall()
    by_key = {(key, method): score for key, method, score in rows}
    assert by_key[("104", "fuzzy_v1")] >= THRESHOLD
    assert ("106", "pi_duplicate_of_v1") in by_key
    assert ("108", "unconfirmed_guid_v1") in by_key
    assert not any(key == "105" for key, _ in by_key)


def test_new_records_keep_their_best_neighbour_as_evidence(db):
    run_id, _ = discover(db, normalized(candidate_rows()))

    evidence = db.execute(
        "select evidence from discoveries where run_id = %s and source_key = '105'", (run_id,)
    ).fetchone()[0]
    assert evidence["best"]["entity_id"] == str(entity_of(db, "6"))
    assert evidence["best"]["score"] < THRESHOLD


def test_a_feed_already_known_from_another_source_is_a_duplicate(db):
    record = make_record(
        source_key="2",
        display_name="Second Show",
        attrs={"feed_url": "https://feeds.example.com/2.xml"},
        source_updated_at=None,
        identifiers=(("podcastindex_id", "2"), ("feed_url", "feeds.example.com/2.xml")),
    )

    run_id, _ = discover(db, [record], source_name="podcastindex_api", source_id=3)

    assert classes(db, run_id) == {"2": ("duplicate", "strong_id_v1")}
    assert entity_of(db, "2", source_id=3) == entity_of(db, "2")


def test_the_same_input_again_writes_nothing_new(db):
    records = normalized(candidate_rows())
    discover(db, records)
    before = {t: count(db, t) for t in ("discoveries", "observations", "entities")}

    discover(db, records, fingerprint="d2")

    assert {t: count(db, t) for t in before} == before


def test_a_duplicate_marker_pointing_into_the_same_batch_is_recognised(db):
    late = {"createdOn": AFTER_CUTOFF}
    rows = [
        dump_row(120, title="Quiet Original Show", **late),
        dump_row(121, title="Loud Copy Elsewhere", duplicateOf=120, **late),
    ]

    run_id, _ = discover(db, normalized(rows))

    assert classes(db, run_id)["121"] == ("uncertain", "pi_duplicate_of_v1")
    suspect = db.execute(
        "select matched_entity_id from discoveries where run_id = %s and source_key = '121'",
        (run_id,),
    ).fetchone()[0]
    assert suspect == entity_of(db, "120")
