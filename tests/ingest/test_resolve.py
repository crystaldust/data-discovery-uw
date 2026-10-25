import uuid

from ddm.identifiers import entity_id_for
from ddm.ingest.records import make_record
from ddm.ingest.resolve import (
    CONFLICT_METHOD,
    MAX_RECORDS_PER_IDENTIFIER,
    find_ambiguous_identifiers,
    plan_resolution,
)

SOURCE = "podcastindex_dump"


def record(key, *identifiers, name=None):
    return make_record(
        source_key=key,
        display_name=name or f"Show {key}",
        attrs={},
        source_updated_at=None,
        identifiers=(("podcastindex_id", key), *identifiers),
    )


def eid(key):
    return entity_id_for(SOURCE, key)


def test_record_without_hits_creates_an_entity_with_all_its_identifiers():
    plan = plan_resolution([record("1", ("feed_url", "a.com/f"))], {}, SOURCE)

    assert plan.assignments == (("1", eid("1")),)
    assert plan.new_entities == ((eid("1"), "Show 1", "1"),)
    assert plan.new_identifiers == (
        ("podcastindex_id", "1", eid("1")),
        ("feed_url", "a.com/f", eid("1")),
    )
    assert plan.candidates == ()
    assert plan.strong_id_merges == 0


def test_records_sharing_an_apple_id_become_one_entity():
    records = [record("1", ("itunes_id", "9")), record("2", ("itunes_id", "9"), ("feed_url", "b"))]

    plan = plan_resolution(records, {}, SOURCE)

    assert plan.assignments == (("1", eid("1")), ("2", eid("1")))
    assert [e[0] for e in plan.new_entities] == [eid("1")]
    assert ("feed_url", "b", eid("1")) in plan.new_identifiers
    assert ("podcastindex_id", "2", eid("1")) in plan.new_identifiers
    assert plan.strong_id_merges == 1


def test_original_url_matching_another_feed_url_merges_a_migrated_feed():
    records = [
        record("1", ("feed_url", "old.com/f")),
        record("2", ("feed_url", "new.com/f"), ("feed_url", "old.com/f")),
    ]

    plan = plan_resolution(records, {}, SOURCE)

    assert dict(plan.assignments)["2"] == eid("1")


def test_record_hitting_an_existing_entity_attaches_to_it():
    existing_id = uuid.uuid4()

    plan = plan_resolution(
        [record("5", ("podcast_guid", "g"))], {("podcast_guid", "g"): existing_id}, SOURCE
    )

    assert plan.assignments == (("5", existing_id),)
    assert plan.new_entities == ()
    assert plan.new_identifiers == (("podcastindex_id", "5", existing_id),)
    assert plan.strong_id_merges == 1


def test_conflicting_identifiers_create_a_separate_entity_and_candidates():
    x, y = uuid.uuid4(), uuid.uuid4()
    existing = {("podcast_guid", "g"): x, ("itunes_id", "9"): y}
    conflicted = record("3", ("podcast_guid", "g"), ("itunes_id", "9"), ("feed_url", "c"))

    plan = plan_resolution([conflicted], existing, SOURCE)

    assert plan.assignments == (("3", eid("3")),)
    assert plan.new_entities == ((eid("3"), "Show 3", "3"),)
    assert plan.new_identifiers == (
        ("podcastindex_id", "3", eid("3")),
        ("feed_url", "c", eid("3")),
    )
    assert plan.candidates == (
        ("3", x, {"shared": [["podcast_guid", "g"]]}),
        ("3", y, {"shared": [["itunes_id", "9"]]}),
    )
    assert plan.strong_id_merges == 0


def test_strong_identifiers_are_transitive_in_processing_order():
    records = [
        record("1", ("podcast_guid", "g")),
        record("2", ("podcast_guid", "g"), ("itunes_id", "9")),
        record("3", ("itunes_id", "9")),
    ]

    plan = plan_resolution(records, {}, SOURCE)

    assert {entity for _, entity in plan.assignments} == {eid("1")}
    assert plan.strong_id_merges == 2


def test_records_are_processed_in_numeric_key_order():
    records = [record("10", ("itunes_id", "9")), record("9", ("itunes_id", "9"))]

    plan = plan_resolution(records, {}, SOURCE)

    assert plan.assignments == (("9", eid("9")), ("10", eid("9")))


def test_plan_does_not_mutate_the_existing_index():
    existing = {("itunes_id", "9"): uuid.uuid4()}
    snapshot = dict(existing)

    plan_resolution([record("1", ("itunes_id", "9"), ("feed_url", "z"))], existing, SOURCE)

    assert existing == snapshot


def test_same_input_gives_the_same_plan():
    records = [record("1", ("itunes_id", "9")), record("2", ("itunes_id", "9"))]

    assert plan_resolution(records, {}, SOURCE) == plan_resolution(records, {}, SOURCE)


def test_empty_input_gives_an_empty_plan():
    plan = plan_resolution([], {}, SOURCE)

    assert (plan.assignments, plan.new_entities, plan.candidates) == ((), (), ())


def test_conflict_method_name_is_versioned():
    assert CONFLICT_METHOD == "strong_id_conflict_v1"


def test_single_hit_with_a_different_guid_is_not_merged():
    records = [
        record("1", ("itunes_id", "9"), ("podcast_guid", "g1")),
        record("2", ("itunes_id", "9"), ("podcast_guid", "g2")),
    ]

    plan = plan_resolution(records, {}, SOURCE)

    assert dict(plan.assignments) == {"1": eid("1"), "2": eid("2")}
    assert plan.candidates == (
        (
            "2",
            eid("1"),
            {"shared": [["itunes_id", "9"]], "conflicting": [["podcast_guid", "g1", "g2"]]},
        ),
    )
    assert ("podcast_guid", "g2", eid("2")) in plan.new_identifiers
    assert ("itunes_id", "9", eid("2")) not in plan.new_identifiers
    assert plan.strong_id_merges == 0


def test_consistency_check_uses_identifiers_of_existing_entities():
    existing_id = uuid.uuid4()
    existing = {("itunes_id", "9"): existing_id, ("podcast_guid", "g1"): existing_id}

    plan = plan_resolution(
        [record("2", ("itunes_id", "9"), ("podcast_guid", "g2"))], existing, SOURCE
    )

    assert dict(plan.assignments) == {"2": eid("2")}
    assert plan.candidates[0][1] == existing_id


def test_feed_urls_may_differ_because_an_entity_can_have_several():
    records = [
        record("1", ("itunes_id", "9"), ("feed_url", "a.com/f")),
        record("2", ("itunes_id", "9"), ("feed_url", "b.com/f")),
    ]

    plan = plan_resolution(records, {}, SOURCE)

    assert dict(plan.assignments) == {"1": eid("1"), "2": eid("1")}


def test_ambiguous_identifiers_are_never_used_to_merge():
    records = [record(str(k), ("podcast_guid", "shared")) for k in (1, 2)]

    plan = plan_resolution(records, {}, SOURCE, ambiguous=frozenset({("podcast_guid", "shared")}))

    assert dict(plan.assignments) == {"1": eid("1"), "2": eid("2")}
    assert all(kind != "podcast_guid" for kind, _, _ in plan.new_identifiers)
    assert plan.candidates == ()


def test_identifiers_on_more_than_three_records_are_ambiguous():
    records = [record(str(k), ("podcast_guid", "shared"), ("itunes_id", "9")) for k in range(4)]
    records.append(record("10", ("itunes_id", "8")))

    ambiguous = find_ambiguous_identifiers(records)

    assert ambiguous == {("podcast_guid", "shared"): 4, ("itunes_id", "9"): 4}


def test_three_records_sharing_an_identifier_are_not_ambiguous():
    records = [record(str(k), ("podcast_guid", "g")) for k in range(MAX_RECORDS_PER_IDENTIFIER)]

    assert find_ambiguous_identifiers(records) == {}
