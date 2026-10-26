import uuid

import pytest

from ddm.ingest.classify import (
    FUZZY_METHOD,
    PI_DUPLICATE_METHOD,
    STRONG_METHOD,
    Neighbour,
    classify_attached,
    classify_conflict,
    classify_unmatched,
    norm_title,
)
from ddm.ingest.resolve import CONFLICT_METHOD, Attachment

ENTITY = uuid.UUID("00000000-0000-0000-0000-000000000001")
OTHER = uuid.UUID("00000000-0000-0000-0000-000000000002")
THRESHOLD = 0.6


def attached(*shared, existing=True):
    return Attachment("101", ENTITY, tuple(shared), existing)


def neighbour(score, entity_id=OTHER):
    return Neighbour(entity_id, score, {"title_sim": score, "author_sim": None, "site_match": 0})


def test_norm_title_ignores_case_accents_and_spacing():
    assert norm_title("  Café   Talk ") == norm_title("cafe talk") == "cafe talk"


def test_norm_title_of_nothing_is_none():
    assert norm_title(None) is None
    assert norm_title("   ") is None


def test_same_podcast_index_feed_from_another_source_is_a_duplicate():
    result = classify_attached(
        attached(("podcastindex_id", "101")),
        record_feed_url="b.com/f",
        record_title="New name",
        entity_feed_urls=frozenset({"a.com/f"}),
        entity_title="Old name",
    )

    assert (result.kind, result.matched_entity_id, result.method) == (
        "duplicate",
        ENTITY,
        STRONG_METHOD,
    )


def test_same_feed_address_and_title_is_a_duplicate():
    result = classify_attached(
        attached(("feed_url", "a.com/f")),
        record_feed_url="a.com/f",
        record_title="Show",
        entity_feed_urls=frozenset({"a.com/f"}),
        entity_title="show",
    )

    assert result.kind == "duplicate"
    assert result.evidence["shared"] == [["feed_url", "a.com/f"]]


def test_same_guid_at_a_new_address_is_a_move():
    result = classify_attached(
        attached(("podcast_guid", "g1")),
        record_feed_url="b.com/f",
        record_title="Show",
        entity_feed_urls=frozenset({"a.com/f"}),
        entity_title="Show",
    )

    assert result.kind == "rename_or_move"
    assert result.evidence["feed_url_changed"] is True
    assert result.evidence["title_changed"] is False


def test_same_apple_id_with_a_new_title_is_a_rename():
    result = classify_attached(
        attached(("itunes_id", "9")),
        record_feed_url="a.com/f",
        record_title="New name",
        entity_feed_urls=frozenset({"a.com/f"}),
        entity_title="Old name",
    )

    assert result.kind == "rename_or_move"
    assert (result.evidence["feed_url_changed"], result.evidence["title_changed"]) == (False, True)


def test_feed_address_hit_with_a_different_title_is_a_rename():
    result = classify_attached(
        attached(("feed_url", "a.com/f")),
        record_feed_url="a.com/f",
        record_title="New name",
        entity_feed_urls=frozenset({"a.com/f"}),
        entity_title="Old name",
    )

    assert result.kind == "rename_or_move"


def test_unknown_titles_do_not_count_as_a_rename():
    result = classify_attached(
        attached(("feed_url", "a.com/f")),
        record_feed_url="a.com/f",
        record_title=None,
        entity_feed_urls=frozenset({"a.com/f"}),
        entity_title="Show",
    )

    assert result.kind == "duplicate"
    assert result.evidence["title_changed"] is None


def test_a_merge_inside_the_run_is_a_duplicate_of_the_entity_the_run_created():
    result = classify_attached(
        attached(("podcast_guid", "g1"), existing=False),
        record_feed_url="b.com/f",
        record_title="Other",
        entity_feed_urls=frozenset({"a.com/f"}),
        entity_title="Show",
    )

    assert result.kind == "duplicate"
    assert result.evidence["within_run"] is True


def test_conflicts_are_uncertain_and_name_their_suspect():
    result = classify_conflict(OTHER, {"shared": [["itunes_id", "9"]]})

    assert (result.kind, result.matched_entity_id, result.method) == (
        "uncertain",
        OTHER,
        CONFLICT_METHOD,
    )


def test_a_neighbour_at_the_threshold_is_uncertain():
    result = classify_unmatched(neighbour(THRESHOLD), duplicate_of=None, threshold=THRESHOLD)

    assert (result.kind, result.matched_entity_id, result.method, result.score) == (
        "uncertain",
        OTHER,
        FUZZY_METHOD,
        THRESHOLD,
    )


def test_a_neighbour_below_the_threshold_is_new_but_kept_as_evidence():
    result = classify_unmatched(neighbour(0.59), duplicate_of=None, threshold=THRESHOLD)

    assert (result.kind, result.matched_entity_id, result.score) == ("new", None, 0.59)
    assert result.evidence["best"]["entity_id"] == str(OTHER)


def test_no_neighbour_at_all_is_new():
    result = classify_unmatched(None, duplicate_of=None, threshold=THRESHOLD)

    assert (result.kind, result.score, result.evidence["best"]) == ("new", None, None)


def test_podcast_index_duplicate_marker_makes_a_record_uncertain():
    result = classify_unmatched(neighbour(0.2), duplicate_of=ENTITY, threshold=THRESHOLD)

    assert (result.kind, result.matched_entity_id, result.method) == (
        "uncertain",
        ENTITY,
        PI_DUPLICATE_METHOD,
    )


def test_evidence_cannot_be_changed():
    result = classify_unmatched(None, duplicate_of=None, threshold=THRESHOLD)

    with pytest.raises(TypeError):
        result.evidence["best"] = "x"
