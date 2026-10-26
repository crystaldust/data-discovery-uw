import csv
import json

import pytest

from ddm.eval.report import EvalInputError, build_report, read_labels
from ddm.eval.sample import COLUMNS


def pair(pair_id, stratum, label, score="", right_class="", **extra):
    number = sum(ord(c) for c in pair_id)
    row = {column: "" for column in COLUMNS}
    row.update(
        pair_id=pair_id,
        stratum=stratum,
        score=score,
        right_class=right_class,
        left_title=f"Left {pair_id}",
        right_title=f"Right {pair_id}",
        left_pi_id=str(number * 10 + 1),
        right_pi_id=str(number * 10 + 2),
        label=label,
        **extra,
    )
    return row


def write(tmp_path, rows, populations):
    labels = tmp_path / "labels.csv"
    with labels.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    strata = tmp_path / "strata.json"
    strata.write_text(
        json.dumps(
            {
                "seed": "s",
                "threshold": 0.6,
                "strata": {
                    name: {"population": n, "sampled": sum(r["stratum"] == name for r in rows)}
                    for name, n in populations.items()
                },
            }
        ),
        encoding="utf-8",
    )
    return labels, strata


def sample_rows():
    rows = [pair(f"s{i}", "strong_same_title", "same") for i in range(4)]
    rows += [pair("d0", "strong_diff_title", "same", right_class="rename_or_move")]
    rows += [pair("d1", "strong_diff_title", "different")]
    rows += [pair("c0", "strong_conflict", "same"), pair("c1", "strong_conflict", "unsure")]
    rows += [pair("f0", "fuzzy_0.8", "same", score="0.91", right_class="uncertain")]
    rows += [pair("f1", "fuzzy_0.8", "same", score="0.85", right_class="uncertain")]
    rows += [pair("f2", "fuzzy_0.5", "different", score="0.52", right_class="new")]
    rows += [pair("f3", "fuzzy_0.5", "same", score="0.55", right_class="new")]
    return rows


POPULATIONS = {
    "strong_same_title": 400,
    "strong_diff_title": 40,
    "strong_conflict": 50,
    "fuzzy_0.8": 100,
    "fuzzy_0.5": 600,
}


def test_report_states_auto_merge_precision_weighted_by_population(tmp_path):
    report = build_report(*write(tmp_path, sample_rows(), POPULATIONS))

    # (400 * 4/4 + 40 * 1/2) / 440
    assert "| Weighted (both strata) | 440 | 6 | 95.5% |" in report


def test_report_lists_band_precision_and_a_threshold_table(tmp_path):
    report = build_report(*write(tmp_path, sample_rows(), POPULATIONS))

    assert "| fuzzy_0.8 | 0.8–1.0 | 100 | 2 | 100.0% |" in report
    # Threshold 0.8: 100 true pairs above, 600 * 1/2 = 300 below -> recall 25%.
    assert "| 0.8 | 100.0% | 25.0% |" in report


def test_unsure_labels_are_reported_and_left_out_of_the_rates(tmp_path):
    report = build_report(*write(tmp_path, sample_rows(), POPULATIONS))

    assert "Unsure: 1" in report
    assert "| strong_conflict | 50 | 1 | 100.0% |" in report


def test_report_shows_real_examples_of_each_class_with_links(tmp_path):
    report = build_report(*write(tmp_path, sample_rows(), POPULATIONS))

    assert "Right d0" in report
    assert "https://podcastindex.org/podcast/1482)" in report  # rebuilt from the feed id


def test_the_same_labels_always_give_the_same_report(tmp_path):
    paths = write(tmp_path, sample_rows(), POPULATIONS)

    assert build_report(*paths) == build_report(*paths)


def test_an_unknown_label_is_rejected_with_its_line_number(tmp_path):
    rows = sample_rows()
    rows[2] = {**rows[2], "label": "yes"}
    labels, _ = write(tmp_path, rows, POPULATIONS)

    with pytest.raises(EvalInputError, match="line 4"):
        read_labels(labels)


def test_labels_are_case_insensitive_and_blank_means_not_labelled_yet(tmp_path):
    rows = sample_rows()
    rows[0] = {**rows[0], "label": " SAME "}
    rows[1] = {**rows[1], "label": ""}
    labels, strata = write(tmp_path, rows, POPULATIONS)

    report = build_report(labels, strata)

    assert "Not labelled yet: 1" in report


def test_untrusted_titles_cannot_inject_markdown(tmp_path):
    rows = sample_rows()
    rows[4] = {**rows[4], "right_title": "x](https://evil.example) ![t](https://t.example)\n# H"}
    report = build_report(*write(tmp_path, rows, POPULATIONS))

    assert "](https://evil.example)" not in report
    assert "\n# H" not in report


def test_a_non_numeric_feed_id_gets_no_link(tmp_path):
    rows = sample_rows()
    rows[4] = {**rows[4], "right_pi_id": "javascript:alert(1)"}

    report = build_report(*write(tmp_path, rows, POPULATIONS))

    assert "javascript" not in report


def test_a_file_saved_with_a_byte_order_mark_still_reads(tmp_path):
    labels, strata = write(tmp_path, sample_rows(), POPULATIONS)
    labels.write_bytes(b"\xef\xbb\xbf" + labels.read_bytes())

    assert "Weighted (both strata)" in build_report(labels, strata)


def test_a_file_without_the_expected_columns_is_rejected(tmp_path):
    labels, _ = write(tmp_path, sample_rows(), POPULATIONS)
    labels.write_text("pair_id;label\np001;same\n", encoding="utf-8")

    with pytest.raises(EvalInputError, match="columns"):
        read_labels(labels)


def test_duplicate_pair_ids_are_rejected(tmp_path):
    rows = sample_rows()
    rows[1] = {**rows[1], "pair_id": rows[0]["pair_id"]}
    labels, _ = write(tmp_path, rows, POPULATIONS)

    with pytest.raises(EvalInputError, match="duplicate pair_id"):
        read_labels(labels)


def test_a_stratum_missing_from_strata_json_is_rejected(tmp_path):
    populations = {k: v for k, v in POPULATIONS.items() if k != "fuzzy_0.5"}

    with pytest.raises(EvalInputError, match="fuzzy_0.5"):
        build_report(*write(tmp_path, sample_rows(), populations))


def test_no_threshold_is_recommended_while_a_band_is_unlabelled(tmp_path):
    rows = [r if r["stratum"] != "fuzzy_0.5" else {**r, "label": ""} for r in sample_rows()]

    report = build_report(*write(tmp_path, rows, POPULATIONS))

    assert "Labelling is not complete" in report


def test_the_unconfirmed_guid_queue_is_reported_on_its_own(tmp_path):
    rows = sample_rows() + [
        pair("u0", "unconfirmed_guid", "same"),
        pair("u1", "unconfirmed_guid", "different"),
    ]
    populations = {**POPULATIONS, "unconfirmed_guid": 64}

    report = build_report(*write(tmp_path, rows, populations))

    assert "| unconfirmed_guid | 64 | 2 | 50.0% |" in report
