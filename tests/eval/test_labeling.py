import csv

import pytest

from ddm.eval.labeling import LabelFile, load_pairs, save_pairs, with_label
from ddm.eval.sample import COLUMNS, EvalInputError
from tests.eval.test_report import pair


def write_csv(path, rows, fieldnames=COLUMNS, bom=False):
    with path.open("w", newline="", encoding="utf-8-sig" if bom else "utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return path


def rows():
    return [pair("p001", "strong_same_title", ""), pair("p002", "fuzzy_0.5", "same")]


def test_pairs_load_with_their_columns_and_tolerate_a_byte_order_mark(tmp_path):
    path = write_csv(tmp_path / "labels.csv", rows(), bom=True)

    loaded = load_pairs(path)

    assert isinstance(loaded, LabelFile)
    assert loaded.columns == COLUMNS
    assert [r["pair_id"] for r in loaded.rows] == ["p001", "p002"]


def test_a_file_without_the_label_column_is_rejected(tmp_path):
    path = tmp_path / "labels.csv"
    path.write_text("pair_id;label\np001;same\n", encoding="utf-8")

    with pytest.raises(EvalInputError, match="columns"):
        load_pairs(path)


def test_labelling_returns_a_new_file_and_leaves_the_old_one_unchanged(tmp_path):
    original = load_pairs(write_csv(tmp_path / "labels.csv", rows()))

    updated = with_label(original, "p001", "different", "other host")

    assert updated.rows[0]["label"] == "different"
    assert updated.rows[0]["note"] == "other host"
    assert original.rows[0]["label"] == ""
    assert updated.rows[1] == original.rows[1]


@pytest.mark.parametrize("label", ["same", "different", "unsure", ""])
def test_the_three_labels_and_clearing_are_accepted(tmp_path, label):
    original = load_pairs(write_csv(tmp_path / "labels.csv", rows()))

    assert with_label(original, "p002", label, "").rows[1]["label"] == label


def test_any_other_label_is_rejected(tmp_path):
    original = load_pairs(write_csv(tmp_path / "labels.csv", rows()))

    with pytest.raises(EvalInputError, match="label"):
        with_label(original, "p001", "yes", "")


def test_an_unknown_pair_is_rejected(tmp_path):
    original = load_pairs(write_csv(tmp_path / "labels.csv", rows()))

    with pytest.raises(EvalInputError, match="p999"):
        with_label(original, "p999", "same", "")


def test_notes_are_kept_on_one_line_and_bounded(tmp_path):
    original = load_pairs(write_csv(tmp_path / "labels.csv", rows()))

    updated = with_label(original, "p001", "unsure", "line one\nline two\x00" + "x" * 1000)

    assert "\n" not in updated.rows[0]["note"]
    assert "\x00" not in updated.rows[0]["note"]
    assert len(updated.rows[0]["note"]) <= 500


def test_a_note_cannot_become_a_spreadsheet_formula(tmp_path):
    original = load_pairs(write_csv(tmp_path / "labels.csv", rows()))

    assert with_label(original, "p001", "same", "=1+1").rows[0]["note"] == "'=1+1"


def test_saving_round_trips_every_column_and_keeps_the_column_order(tmp_path):
    path = write_csv(tmp_path / "labels.csv", rows())
    updated = with_label(load_pairs(path), "p001", "same", "ok")

    save_pairs(path, updated)

    assert load_pairs(path) == updated
    assert path.read_text(encoding="utf-8").splitlines()[0].startswith('"pair_id","stratum"')
    assert not list(tmp_path.glob(".*.tmp"))


def test_saving_never_writes_through_a_planted_temp_file_link(tmp_path):
    path = write_csv(tmp_path / "labels.csv", rows())
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me", encoding="utf-8")
    (tmp_path / ".labels.csv.tmp").symlink_to(victim)

    save_pairs(path, with_label(load_pairs(path), "p001", "same", ""))

    assert victim.read_text(encoding="utf-8") == "keep me"
    assert load_pairs(path).rows[0]["label"] == "same"
