import uuid

import pytest

from ddm.identifiers import entity_id_for
from ddm.ingest.records import make_record
from ddm.ingest.resolve import (
    CONFLICT_METHOD,
    MAX_RECORDS_PER_IDENTIFIER,
    RESOLUTION_RULES,
    UNCONFIRMED_GUID_METHOD,
    Attachment,
    find_ambiguous_identifiers,
    plan_identifier_attachments,
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
        [record("5", ("podcast_guid", "g"))],
        {("podcast_guid", "g"): existing_id},
        SOURCE,
        titles={existing_id: "Show 5"},  # same title: the GUID is confirmed
    )

    assert plan.assignments == (("5", existing_id),)
    assert plan.new_entities == ()
    assert plan.new_identifiers == (("podcastindex_id", "5", existing_id),)
    assert plan.strong_id_merges == 1


def test_conflicting_identifiers_create_a_separate_entity_and_candidates():
    x, y = uuid.uuid4(), uuid.uuid4()
    existing = {("podcast_guid", "g"): x, ("itunes_id", "9"): y}
    conflicted = record("3", ("podcast_guid", "g"), ("itunes_id", "9"), ("feed_url", "c"))
    # Same title as the GUID owner, so the GUID counts and the record really hits two.
    titles = {x: "Show 3", y: "Another title"}

    plan = plan_resolution([conflicted], existing, SOURCE, titles=titles)

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
        record("1", ("podcast_guid", "g"), name="Same Show"),
        record("2", ("podcast_guid", "g"), ("itunes_id", "9"), name="Same Show"),
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


def test_a_shared_author_page_merges_records_whose_names_differ():
    records = [
        record("1", ("outlet_author_url", "theatlantic.com/author/a-b"), name="Jane Q. Roe"),
        record("2", ("outlet_author_url", "theatlantic.com/author/a-b"), name="Jane Roe"),
    ]

    plan = plan_resolution(records, {}, SOURCE)

    assert dict(plan.assignments) == {"1": eid("1"), "2": eid("1")}
    assert plan.strong_id_merges == 1


def test_an_existing_author_page_confirms_a_match_whatever_the_name():
    existing_id = uuid.uuid4()
    existing = {("outlet_author_url", "theatlantic.com/author/a-b"): existing_id}

    plan = plan_resolution(
        [record("2", ("outlet_author_url", "theatlantic.com/author/a-b"), name="J. Roe")],
        existing,
        SOURCE,
        titles={existing_id: "Jane Roe"},
    )

    assert dict(plan.assignments) == {"2": existing_id}
    assert plan.candidates == ()


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


RSS = "podcast_rss"
E1, E2 = uuid.UUID(int=1), uuid.UUID(int=2)


def baseline_index():
    return {
        ("podcastindex_id", "1"): E1,
        ("feed_url", "a.com/1"): E1,
        ("podcast_guid", "g1"): E1,
        ("podcastindex_id", "2"): E2,
        ("feed_url", "b.com/2"): E2,
    }


def attach(records, existing=None, ambiguous=frozenset()):
    entity_by_key = {"1": E1, "2": E2}
    index = baseline_index() if existing is None else existing
    return plan_identifier_attachments(records, entity_by_key, index, RSS, ambiguous)


def test_new_identifier_is_attached_to_the_records_entity():
    plan = attach([record("1", ("feed_url", "moved.com/1"))])

    assert plan.new_identifiers == (("feed_url", "moved.com/1", E1),)
    assert (plan.candidates, plan.single_value_conflicts) == ((), 0)


def test_identifiers_the_entity_already_has_are_not_attached_again():
    plan = attach([record("1", ("feed_url", "a.com/1"), ("podcast_guid", "g1"))])

    assert plan.new_identifiers == ()


def test_identifier_of_another_entity_becomes_a_candidate_not_an_attachment():
    plan = attach([record("1", ("feed_url", "b.com/2"))])

    assert plan.new_identifiers == ()
    assert plan.candidates == (
        ("1", E2, {"shared": [["feed_url", "b.com/2"]], "observed_in": RSS}),
    )


def test_two_identifiers_of_one_other_entity_give_one_candidate():
    plan = attach([record("1", ("feed_url", "b.com/2"), ("podcastindex_id", "2"))])

    assert plan.candidates == (
        (
            "1",
            E2,
            {"shared": [["feed_url", "b.com/2"], ["podcastindex_id", "2"]], "observed_in": RSS},
        ),
    )


def test_a_second_guid_for_an_entity_is_counted_as_a_conflict_and_not_attached():
    plan = attach([record("1", ("podcast_guid", "g2"))])

    assert plan.new_identifiers == ()
    assert plan.single_value_conflicts == 1


def test_ambiguous_identifiers_are_skipped():
    plan = attach(
        [record("1", ("podcast_guid", "shared"))],
        existing={("podcastindex_id", "1"): E1},
        ambiguous=frozenset({("podcast_guid", "shared")}),
    )

    assert plan.new_identifiers == ()


def test_identifiers_attached_earlier_in_the_batch_are_seen_by_later_records():
    plan = attach([record("1", ("feed_url", "new.com/x")), record("2", ("feed_url", "new.com/x"))])

    assert plan.new_identifiers == (("feed_url", "new.com/x", E1),)
    assert plan.candidates == (
        ("2", E1, {"shared": [["feed_url", "new.com/x"]], "observed_in": RSS}),
    )


def test_attachment_planning_does_not_modify_the_given_index():
    index = baseline_index()

    attach([record("1", ("feed_url", "moved.com/1"))], existing=index)

    assert index == baseline_index()


def test_attachment_plan_of_nothing_is_empty():
    plan = attach([])

    assert (plan.new_identifiers, plan.candidates, plan.single_value_conflicts) == ((), (), 0)


def test_a_record_hitting_an_existing_entity_is_listed_as_attached():
    existing_id = uuid.uuid4()
    existing = {("podcast_guid", "g1"): existing_id, ("feed_url", "a.com/f"): existing_id}

    plan = plan_resolution(
        [record("2", ("podcast_guid", "g1"), ("feed_url", "b.com/f"))],
        existing,
        SOURCE,
        titles={existing_id: "Show 2"},
    )

    assert plan.attached == (
        Attachment("2", existing_id, (("podcast_guid", "g1"),), existing=True),
    )


def test_a_merge_inside_the_batch_is_attached_to_an_entity_created_by_the_batch():
    records = [record("1", ("itunes_id", "9")), record("2", ("itunes_id", "9"))]

    plan = plan_resolution(records, {}, SOURCE)

    assert plan.attached == (Attachment("2", eid("1"), (("itunes_id", "9"),), existing=False),)


def test_conflicts_and_records_without_hits_are_not_attached():
    by_apple, by_guid = uuid.uuid4(), uuid.uuid4()
    existing = {("itunes_id", "9"): by_apple, ("podcast_guid", "g1"): by_guid}
    records = [
        record("1", ("itunes_id", "9"), ("podcast_guid", "g1")),  # hits two entities
        record("3", ("feed_url", "c.com/f")),  # hits nothing
    ]
    titles = {by_apple: "Other", by_guid: "Show 1"}

    plan = plan_resolution(records, existing, SOURCE, titles=titles)

    assert plan.attached == ()


# --- v2: a podcast:guid alone is not enough (labelled sample, phase 4) -------------------


def test_resolution_rules_are_versioned():
    assert RESOLUTION_RULES == 2


def test_a_guid_shared_by_two_differently_titled_shows_is_queued_not_merged():
    records = [
        record("1", ("podcast_guid", "g"), name="This Week Health: Newsroom"),
        record("2", ("podcast_guid", "g"), name="UnHack with Drex DeFord"),
    ]

    plan = plan_resolution(records, {}, SOURCE)

    assert dict(plan.assignments) == {"1": eid("1"), "2": eid("2")}
    assert plan.attached == ()
    assert plan.candidates == (
        ("2", eid("1"), {"shared": [["podcast_guid", "g"]], "unconfirmed_guid": True}),
    )
    assert plan.strong_id_merges == 0


def test_a_guid_with_the_same_title_still_merges():
    records = [
        record("1", ("podcast_guid", "g"), name="Distrohoppers' Digest"),
        record("2", ("podcast_guid", "g"), name="  distrohoppers'   DIGEST "),
    ]

    plan = plan_resolution(records, {}, SOURCE)

    assert dict(plan.assignments) == {"1": eid("1"), "2": eid("1")}


@pytest.mark.parametrize("corroboration", [("itunes_id", "9"), ("feed_url", "a.com/f")])
def test_a_guid_backed_by_an_apple_id_or_feed_address_merges_despite_a_new_title(corroboration):
    records = [
        record("1", ("podcast_guid", "g"), corroboration, name="Old name"),
        record("2", ("podcast_guid", "g"), corroboration, name="New name"),
    ]

    plan = plan_resolution(records, {}, SOURCE)

    assert dict(plan.assignments) == {"1": eid("1"), "2": eid("1")}


def test_existing_entity_titles_decide_a_guid_only_match():
    existing_id = uuid.uuid4()
    existing = {("podcast_guid", "g"): existing_id}
    titles = {existing_id: "Check Point ReelTalk"}

    renamed = plan_resolution(
        [record("2", ("podcast_guid", "g"), name="Technophile ReelTalk")],
        existing,
        SOURCE,
        titles=titles,
    )
    same = plan_resolution(
        [record("3", ("podcast_guid", "g"), name="Check Point ReelTalk")],
        existing,
        SOURCE,
        titles=titles,
    )

    assert dict(renamed.assignments) == {"2": eid("2")}
    assert dict(same.assignments) == {"3": existing_id}


def test_an_unknown_title_does_not_confirm_a_guid():
    records = [
        record("1", ("podcast_guid", "g"), name="Show"),
        make_record(
            source_key="2",
            display_name=None,
            attrs={},
            source_updated_at=None,
            identifiers=(("podcastindex_id", "2"), ("podcast_guid", "g")),
        ),
    ]

    plan = plan_resolution(records, {}, SOURCE)

    assert dict(plan.assignments)["2"] == eid("2")


def test_a_copied_guid_family_still_joins_its_real_duplicates():
    records = [
        record("1", ("podcast_guid", "G"), name="Alpha"),
        record("2", ("podcast_guid", "G"), ("itunes_id", "5"), name="Beta"),
        record("3", ("podcast_guid", "G"), name="Beta"),  # same show as 2
        record("4", ("podcast_guid", "G"), ("itunes_id", "5"), name="Beta"),  # same as 2
    ]

    plan = plan_resolution(records, {}, SOURCE)

    owners = dict(plan.assignments)
    assert owners["1"] == eid("1")
    assert owners["2"] == owners["3"] == owners["4"] == eid("2")


def test_a_guid_only_hit_needs_the_entity_title_to_be_known_to_the_planner():
    existing_id = uuid.uuid4()

    with pytest.raises(ValueError, match="title"):
        plan_resolution(
            [record("2", ("podcast_guid", "g"))], {("podcast_guid", "g"): existing_id}, SOURCE
        )


def test_unconfirmed_guid_candidates_are_named_apart_from_real_conflicts():
    assert UNCONFIRMED_GUID_METHOD == "unconfirmed_guid_v1"


def test_a_guid_hit_with_another_title_gives_way_to_an_apple_id_hit():
    by_guid, by_apple = uuid.uuid4(), uuid.uuid4()
    existing = {("podcast_guid", "g"): by_guid, ("itunes_id", "9"): by_apple}
    titles = {by_guid: "Somebody Else's Show", by_apple: "Show 3"}

    plan = plan_resolution(
        [record("3", ("podcast_guid", "g"), ("itunes_id", "9"))], existing, SOURCE, titles=titles
    )

    assert dict(plan.assignments) == {"3": by_apple}
