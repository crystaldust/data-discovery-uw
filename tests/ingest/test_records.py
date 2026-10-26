import dataclasses
from datetime import UTC, date, datetime

import pytest

from ddm.ingest.records import (
    NormalizedRecord,
    Reject,
    canonical_json,
    content_hash,
    fingerprint_item,
    input_fingerprint,
    make_record,
    make_reject,
    source_key_order,
)


def build(**overrides):
    fields = {
        "source_key": "1",
        "display_name": "Show",
        "attrs": {"language": "en", "episode_count": 3},
        "source_updated_at": datetime(2023, 1, 1, tzinfo=UTC),
        "identifiers": (("podcastindex_id", "1"),),
    }
    return make_record(**{**fields, **overrides})


def test_content_hash_ignores_attribute_order():
    first = build(attrs={"language": "en", "episode_count": 3})
    second = build(attrs={"episode_count": 3, "language": "en"})

    assert content_hash(first) == content_hash(second)


def test_content_hash_changes_when_an_attribute_changes():
    assert content_hash(build()) != content_hash(build(attrs={"language": "fr"}))


def test_content_hash_changes_when_only_the_title_changes():
    assert content_hash(build()) != content_hash(build(display_name="Renamed Show"))


def test_content_hash_changes_when_source_updated_at_changes():
    later = build(source_updated_at=datetime(2024, 1, 1, tzinfo=UTC))

    assert content_hash(build()) != content_hash(later)


def test_content_hash_ignores_identifiers_because_they_derive_from_attrs():
    other_ids = build(identifiers=(("podcastindex_id", "1"), ("itunes_id", "9")))

    assert content_hash(build()) == content_hash(other_ids)


def test_content_hash_is_32_bytes_of_sha256():
    assert len(content_hash(build())) == 32


def test_record_is_immutable_including_its_attrs():
    record = build()

    with pytest.raises(dataclasses.FrozenInstanceError):
        record.display_name = "changed"  # type: ignore[misc]
    with pytest.raises(TypeError):
        record.attrs["language"] = "fr"  # type: ignore[index]


def test_make_record_copies_attrs_so_later_caller_changes_do_not_leak_in():
    attrs = {"language": "en"}
    record = build(attrs=attrs)

    attrs["language"] = "fr"

    assert record.attrs["language"] == "en"


def test_canonical_json_sorts_keys_and_serialises_dates():
    value = {"b": 1, "a": date(2024, 1, 1), "c": datetime(2024, 1, 1, tzinfo=UTC)}

    assert canonical_json(value) == ('{"a":"2024-01-01","b":1,"c":"2024-01-01T00:00:00+00:00"}')


def test_canonical_json_keeps_non_ascii_text():
    assert canonical_json({"t": "Café"}) == '{"t":"Café"}'


def test_input_fingerprint_does_not_depend_on_row_order():
    hashes = [b"\x01" * 32, b"\x02" * 32, b"\x03" * 32]

    first = input_fingerprint("src", {"p": 1}, hashes)
    second = input_fingerprint("src", {"p": 1}, list(reversed(hashes)))

    assert first == second
    assert len(first) == 64


def test_input_fingerprint_changes_with_params_source_or_content():
    hashes = [b"\x01" * 32]
    base = input_fingerprint("src", {"p": 1}, hashes)

    assert input_fingerprint("src", {"p": 2}, hashes) != base
    assert input_fingerprint("other", {"p": 1}, hashes) != base
    assert input_fingerprint("src", {"p": 1}, [b"\x02" * 32]) != base


def test_fingerprint_item_depends_on_the_key_as_well_as_the_content():
    digest = content_hash(build())

    assert fingerprint_item("1", digest) != fingerprint_item("2", digest)
    assert fingerprint_item("1", digest) == fingerprint_item("1", digest)


def test_swapping_keys_between_rows_changes_the_input_fingerprint():
    a, b = content_hash(build(display_name="A")), content_hash(build(display_name="B"))

    original = input_fingerprint("src", {}, [fingerprint_item("1", a), fingerprint_item("2", b)])
    swapped = input_fingerprint("src", {}, [fingerprint_item("1", b), fingerprint_item("2", a)])

    assert original != swapped


def test_make_reject_copies_raw_into_a_read_only_mapping():
    raw = {"id": 0}
    reject = make_reject(row_number=1, source_key=None, reason="invalid_id", raw=raw)

    raw["id"] = 5

    assert reject.raw["id"] == 0
    with pytest.raises(TypeError):
        reject.raw["id"] = 1  # type: ignore[index]


def test_reject_is_immutable():
    reject = Reject(row_number=1, source_key="1", reason="invalid_id", raw={"id": 0})

    with pytest.raises(dataclasses.FrozenInstanceError):
        reject.reason = "other"  # type: ignore[misc]


def test_source_key_order_sorts_numbers_by_value_then_text_keys():
    keys = ["Q42", "10", "9", "Q7"]

    assert sorted(keys, key=source_key_order) == ["9", "10", "Q42", "Q7"]


def test_normalized_record_type_is_exported():
    assert isinstance(build(), NormalizedRecord)


def test_norm_title_drops_latin_accents_only():
    from ddm.ingest.records import norm_title

    assert norm_title("Café Crème") == "cafe creme"
    assert norm_title("がっこう") != norm_title("かっこう")  # dakuten change the word
    assert norm_title("हिंदी") != norm_title("हद")  # vowel signs are letters here
