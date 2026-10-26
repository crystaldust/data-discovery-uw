import sqlite3
from datetime import UTC, date, datetime

import pytest

from ddm.ingest.records import NormalizedRecord, Reject
from ddm.sources.podcastindex_dump import (
    SliceParams,
    iter_slice_rows,
    normalize_dump_row,
    slice_query,
)
from tests.conftest import dump_row, make_dump

CUTOFF = date(2024, 1, 1)
AFTER_CUTOFF = 1710000000  # 2024-03-09


def keys_in_slice(tmp_path, rows):
    path = make_dump(tmp_path, rows)
    return [row["id"] for row in iter_slice_rows(path, SliceParams(created_before=CUTOFF))]


def test_slice_keeps_matching_rows_ordered_by_id(tmp_path):
    rows = [dump_row(3), dump_row(1), dump_row(2, category1="", category7="technology")]

    assert keys_in_slice(tmp_path, rows) == [1, 2, 3]


@pytest.mark.parametrize(
    "overrides",
    [
        {"language": "fr"},
        {"episodeCount": 2},
        {"createdOn": AFTER_CUTOFF},
        {"category1": "comedy"},
    ],
)
def test_slice_excludes_rows_outside_the_demo_population(tmp_path, overrides):
    rows = [dump_row(1), dump_row(2, **overrides)]

    assert keys_in_slice(tmp_path, rows) == [1]


def test_slice_matches_language_and_category_case_insensitively(tmp_path):
    rows = [dump_row(1, language="EN-us", category1="Marketing")]

    assert keys_in_slice(tmp_path, rows) == [1]


def test_slice_query_passes_values_as_parameters():
    sql, values = slice_query(SliceParams(created_before=CUTOFF))

    assert "marketing" not in sql
    assert values == (
        "en",
        3,
        int(datetime(2024, 1, 1, tzinfo=UTC).timestamp()),
        "marketing",
        "technology",
    )


def test_slice_with_created_from_keeps_only_the_window(tmp_path):
    rows = [dump_row(1), dump_row(2, createdOn=AFTER_CUTOFF), dump_row(3, createdOn=1800000000)]
    path = make_dump(tmp_path, rows)
    window = SliceParams(created_from=CUTOFF, created_before=date(2026, 1, 1))

    keys = [row["id"] for row in iter_slice_rows(path, window)]

    assert keys == [2]


def test_slice_query_without_created_from_has_no_lower_bound():
    sql, _ = slice_query(SliceParams(created_before=CUTOFF))

    assert "createdOn >=" not in sql


def test_iter_slice_rows_rejects_a_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        list(iter_slice_rows(tmp_path / "missing.db", SliceParams(created_before=CUTOFF)))


def test_iter_slice_rows_opens_the_dump_read_only(tmp_path):
    path = make_dump(tmp_path, [dump_row(1)])
    before = path.read_bytes()

    list(iter_slice_rows(path, SliceParams(created_before=CUTOFF)))

    assert path.read_bytes() == before


def normalize(**overrides):
    result = normalize_dump_row(1, dump_row(1, **overrides))
    assert isinstance(result, NormalizedRecord)
    return result


def test_normalize_maps_identity_title_and_update_time():
    record = normalize(title="  The Show  ", lastUpdate=1700000000)

    assert record.source_key == "1"
    assert record.display_name == "The Show"
    assert record.source_updated_at == datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)
    assert record.attrs["last_update_at"] == "2023-11-14T22:13:20+00:00"
    assert record.attrs["feed_url"] == "https://feeds.example.com/1.xml"


def test_normalize_turns_empty_title_into_null():
    assert normalize(title="").display_name is None


def test_normalize_drops_empty_text_instead_of_storing_placeholders():
    record = normalize(itunesAuthor="", description="  ", link="")

    assert "author" not in record.attrs
    assert "description" not in record.attrs
    assert "link" not in record.attrs
    assert "" not in record.attrs.values()


def test_normalize_maps_text_fields_to_attrs():
    record = normalize(
        description="About",
        link="https://example.com",
        itunesAuthor="Ann",
        itunesOwnerName="Ann Co",
        imageUrl="https://example.com/i.png",
        itunesType="episodic",
        generator="Gen",
        contentType="application/rss+xml",
        host="example.com",
        newestEnclosureUrl="https://example.com/e.mp3",
        chash="abc",
    )

    expected = {
        "description": "About",
        "link": "https://example.com",
        "author": "Ann",
        "owner_name": "Ann Co",
        "image_url": "https://example.com/i.png",
        "itunes_type": "episodic",
        "generator": "Gen",
        "content_type": "application/rss+xml",
        "host": "example.com",
        "newest_enclosure_url": "https://example.com/e.mp3",
        "source_content_hash": "abc",
    }
    assert {key: record.attrs[key] for key in expected} == expected


def test_normalize_keeps_zero_counts_and_scores_because_zero_is_a_real_value():
    record = normalize(episodeCount=0, popularityScore=0, priority=0, updateFrequency=0)

    assert record.attrs["episode_count"] == 0
    assert record.attrs["popularity_score"] == 0
    assert record.attrs["priority"] == 0
    assert record.attrs["update_frequency"] == 0


def test_normalize_treats_zero_measurements_as_unknown():
    record = normalize(lastHttpStatus=0, newestEnclosureDuration=0)

    assert "last_http_status" not in record.attrs
    assert "newest_enclosure_duration" not in record.attrs


def test_normalize_keeps_positive_measurements():
    record = normalize(lastHttpStatus=200, newestEnclosureDuration=1800)

    assert record.attrs["last_http_status"] == 200
    assert record.attrs["newest_enclosure_duration"] == 1800


@pytest.mark.parametrize(("value", "expected"), [(1, True), (0, None), (None, None)])
def test_normalize_records_explicit_only_when_declared(value, expected):
    assert normalize(explicit=value).attrs.get("explicit") is expected


def test_normalize_strips_nul_characters_that_postgres_cannot_store():
    record = normalize(title="Bad\x00Title", description="a\x00b")

    assert record.display_name == "BadTitle"
    assert record.attrs["description"] == "ab"


def test_normalize_treats_out_of_range_timestamps_as_unknown():
    record = normalize(lastUpdate=10**15, createdOn=10**15)

    assert record.source_updated_at is None
    assert "created_on" not in record.attrs


def test_normalize_ignores_a_malformed_original_url():
    record = normalize(originalUrl="http://[::1")

    assert [kind for kind, _ in record.identifiers].count("feed_url") == 1


def test_rejects_keep_raw_values_json_safe():
    raw = dump_row(7, url="", description=b"\x01\x02", title="a\x00b")

    result = normalize_dump_row(1, raw)

    assert isinstance(result, Reject)
    assert result.raw["description"] == "0102"
    assert result.raw["title"] == "ab"


def test_iter_slice_rows_survives_invalid_utf8(tmp_path):
    path = make_dump(tmp_path, [dump_row(1)])
    conn = sqlite3.connect(path)
    conn.execute("UPDATE podcasts SET title = CAST(X'4261C328' AS TEXT) WHERE id = 1")
    conn.commit()
    conn.close()

    rows = list(iter_slice_rows(path, SliceParams(created_before=CUTOFF)))

    assert rows[0]["title"] == "Ba�("


def test_iter_slice_rows_handles_paths_with_uri_characters(tmp_path):
    directory = tmp_path / "odd #dir?"
    directory.mkdir()
    path = make_dump(directory, [dump_row(1)])

    assert [r["id"] for r in iter_slice_rows(path, SliceParams(created_before=CUTOFF))] == [1]


def test_normalize_omits_null_integers():
    record = normalize(updateFrequency=None, lastHttpStatus=None)

    assert "update_frequency" not in record.attrs
    assert "last_http_status" not in record.attrs


def test_normalize_lowercases_language():
    assert normalize(language=" EN-US ").attrs["language"] == "en-us"


def test_normalize_collects_categories_lowercased_and_deduplicated():
    record = normalize(
        category1="Marketing", category2="", category3="marketing", category4="Business"
    )

    assert record.attrs["categories"] == ["marketing", "business"]


def test_normalize_omits_categories_when_all_empty():
    assert "categories" not in normalize(category1="").attrs


def test_normalize_converts_unix_times_and_treats_zero_as_unknown():
    record = normalize(createdOn=1700000000, newestItemPubdate=0, oldestItemPubdate=None)

    assert record.attrs["created_on"] == "2023-11-14T22:13:20+00:00"
    assert "newest_item_at" not in record.attrs
    assert "oldest_item_at" not in record.attrs


def test_normalize_unknown_last_update_gives_null_source_time():
    assert normalize(lastUpdate=0).source_updated_at is None


@pytest.mark.parametrize(("value", "expected"), [("", None), (0, None), (123, 123)])
def test_normalize_records_duplicate_of_only_when_it_is_a_positive_integer(value, expected):
    attrs = normalize(duplicateOf=value).attrs

    assert attrs.get("duplicate_of_podcastindex_id") == expected


def test_normalize_builds_strong_identifiers_in_lookup_order():
    record = normalize(
        podcastGuid=" ABC ",
        itunesId=555,
        url="https://feeds.example.com/1.xml",
        originalUrl="http://old.example.com/feed/",
    )

    assert record.identifiers == (
        ("podcast_guid", "abc"),
        ("itunes_id", "555"),
        ("podcastindex_id", "1"),
        ("feed_url", "feeds.example.com/1.xml"),
        ("feed_url", "old.example.com/feed"),
    )
    assert record.attrs["podcast_guid"] == "abc"
    assert record.attrs["itunes_id"] == 555
    assert record.attrs["original_url"] == "http://old.example.com/feed/"


def test_normalize_skips_missing_itunes_id_and_guid():
    record = normalize(itunesId=0, podcastGuid="")

    assert [kind for kind, _ in record.identifiers] == ["podcastindex_id", "feed_url"]
    assert "itunes_id" not in record.attrs


def test_normalize_does_not_repeat_an_original_url_equal_to_the_url():
    record = normalize(url="https://a.com/f.xml", originalUrl="http://a.com/f.xml/")

    assert [kind for kind, _ in record.identifiers].count("feed_url") == 1


def test_normalize_ignores_an_unusable_original_url():
    record = normalize(originalUrl="not a url")

    assert [kind for kind, _ in record.identifiers].count("feed_url") == 1
    assert record.attrs["original_url"] == "not a url"


def test_normalize_drops_dead_and_podcast_id_columns():
    attrs = normalize().attrs

    assert "dead" not in attrs
    assert "podcast_id" not in attrs


@pytest.mark.parametrize("url", ["", "ftp://x/y", "nonsense"])
def test_normalize_rejects_rows_without_a_usable_feed_url(url):
    raw = dump_row(7, url=url)

    result = normalize_dump_row(4, raw)

    assert result == Reject(row_number=4, source_key="7", reason="invalid_feed_url", raw=raw)


@pytest.mark.parametrize("row_id", [0, -5, None, "x"])
def test_normalize_rejects_rows_without_a_valid_id(row_id):
    result = normalize_dump_row(1, dump_row(1, id=row_id))

    assert isinstance(result, Reject)
    assert result.reason == "invalid_id"
    assert result.source_key is None
