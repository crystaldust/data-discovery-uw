import json

import pytest

from ddm.db.migrate import migrate
from ddm.ops.counts import (
    CountDiff,
    CountsInputError,
    compare_counts,
    counts_json,
    read_counts,
    table_counts,
)
from tests import seed


def test_equal_counts_have_no_differences():
    assert compare_counts({"a": 1, "b": 2}, {"b": 2, "a": 1}) == ()


def test_missing_extra_and_different_relations_are_all_reported():
    diffs = compare_counts({"a": 1, "b": 2, "c": 3}, {"a": 1, "b": 5, "d": 0})

    assert diffs == (
        CountDiff("b", 2, 5),
        CountDiff("c", 3, None),
        CountDiff("d", None, 0),
    )


def test_counts_round_trip_through_a_file(tmp_path):
    path = tmp_path / "counts.json"
    path.write_text(counts_json({"z": 1, "a": 2}))

    assert read_counts(path) == {"a": 2, "z": 1}
    assert list(json.loads(path.read_text())) == ["a", "z"]


@pytest.mark.parametrize("body", ["not json", "[1, 2]", '{"a": "1"}', '{"a": true}'])
def test_a_malformed_counts_file_is_refused(tmp_path, body):
    path = tmp_path / "counts.json"
    path.write_text(body)

    with pytest.raises(CountsInputError):
        read_counts(path)


def test_a_missing_counts_file_is_refused(tmp_path):
    with pytest.raises(CountsInputError, match="cannot read"):
        read_counts(tmp_path / "nope.json")


@pytest.mark.integration
def test_table_counts_cover_every_table_and_the_search_view(fresh_db):
    migrate(fresh_db)
    run = seed.run(fresh_db, 1)
    seed.entity(fresh_db, "podcast", "One", run)
    seed.refresh(fresh_db)

    counts = table_counts(fresh_db)

    assert counts["entities"] == 1 and counts["import_runs"] == 1
    assert counts["entity_search"] == 1
    assert counts["schema_migrations"] == 7
    assert "contact_routes" in counts and "api_usage" in counts


@pytest.mark.integration
def test_excluded_relations_are_left_out(fresh_db):
    migrate(fresh_db)

    counts = table_counts(fresh_db, exclude=["api_usage"])

    assert "api_usage" not in counts and "entities" in counts
