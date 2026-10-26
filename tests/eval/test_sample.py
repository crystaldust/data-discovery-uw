import csv
import json

import pytest

from ddm.db.migrate import migrate
from ddm.eval.sample import (
    COLUMNS,
    STRATA,
    EvalInputError,
    Pair,
    _carry,
    draw,
    safe_cell,
    safe_url,
    write_sample,
)
from ddm.ingest.baseline import import_baseline
from ddm.ingest.discover_dump import discover_dump
from tests.conftest import make_dump
from tests.ingest.test_discovery import baseline_rows, candidate_rows


def pair(stratum, key):
    return Pair(stratum, (1, key), (1, "1"), None, "strong_id")


def test_draw_takes_at_most_each_strata_share_and_reports_population():
    pairs = [pair("strong_conflict", str(k)) for k in range(25)]

    chosen, strata = draw(pairs)

    assert len(chosen) == dict(STRATA)["strong_conflict"] == 10
    assert strata["strong_conflict"] == {"population": 25, "sampled": 10}
    assert strata["strong_same_title"] == {"population": 0, "sampled": 0}


def test_draw_is_deterministic_whatever_the_input_order():
    pairs = [pair("strong_conflict", str(k)) for k in range(25)]

    assert draw(pairs)[0] == draw(list(reversed(pairs)))[0]


def test_strata_shares_add_up_to_two_hundred():
    assert sum(size for _, size in STRATA) == 200


@pytest.mark.parametrize(
    ("raw", "cell"),
    [
        ('=HYPERLINK("http://evil")', '\'=HYPERLINK("http://evil")'),
        ("+1 Show", "'+1 Show"),
        ("-Minus", "'-Minus"),
        ("@SUM(A1)", "'@SUM(A1)"),
        ("Line one\nline\ttwo\x00", "Line one line two"),
        ("Contact jane.doe+pod@example.co.uk now", "Contact [email removed] now"),
        (None, ""),
        (12345, "12345"),
    ],
)
def test_cells_are_safe_to_open_in_a_spreadsheet_and_publish(raw, cell):
    assert safe_cell(raw) == cell


@pytest.mark.parametrize(
    ("url", "cleaned"),
    [
        ("https://example.com/feed.xml?token=secret#x", "https://example.com/feed.xml"),
        ("https://user:pass@example.com/feed", "https://example.com/feed"),
        ("mailto:jane@example.com", ""),
        ("javascript:alert(1)", ""),
        ("not a url", ""),
        (None, ""),
    ],
)
def test_urls_keep_only_scheme_host_and_path(url, cleaned):
    assert safe_url(url) == cleaned


@pytest.fixture
def discovered(fresh_db, tmp_path):
    migrate(fresh_db)
    path = make_dump(tmp_path, baseline_rows() + candidate_rows())
    import_baseline(fresh_db, path)
    discover_dump(fresh_db, path)
    return fresh_db


def read_rows(path):
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


@pytest.mark.integration
def test_sample_covers_strong_merges_conflicts_and_fuzzy_neighbours(discovered, tmp_path):
    labels, strata = tmp_path / "eval" / "labels.csv", tmp_path / "eval" / "strata.json"

    result = write_sample(discovered, labels, strata)

    rows = read_rows(labels)
    assert len(rows) == result.pairs
    assert tuple(rows[0]) == COLUMNS
    by_stratum = {r["stratum"] for r in rows}
    # Under rules v2 this small fixture has no differently titled strong merge; draw() and
    # its unit tests cover that stratum.
    assert {"strong_same_title", "unconfirmed_guid"} <= by_stratum
    assert any(s.startswith("fuzzy_") for s in by_stratum)
    moved = next(r for r in rows if r["right_pi_id"] == "101")
    assert (moved["left_pi_id"], moved["right_class"]) == ("1", "rename_or_move")
    assert moved["right_pi_url"] == "https://podcastindex.org/podcast/101"
    assert all(r["label"] == "" for r in rows)
    meta = json.loads(strata.read_text())
    # 108 and 109 share only a GUID under a new title; 103 merged on its Apple ID.
    assert meta["strata"]["unconfirmed_guid"]["population"] == 2
    assert meta["strata"]["strong_conflict"]["population"] == 0


@pytest.mark.integration
def test_the_same_database_gives_the_same_sample(discovered, tmp_path):
    first, second = tmp_path / "a.csv", tmp_path / "b.csv"

    write_sample(discovered, first, tmp_path / "a.json")
    write_sample(discovered, second, tmp_path / "b.json")

    assert first.read_bytes() == second.read_bytes()


@pytest.mark.integration
@pytest.mark.parametrize("existing", ["labels.csv", "strata.json"])
def test_an_existing_sample_file_is_never_overwritten(discovered, tmp_path, existing):
    (tmp_path / existing).write_text("pair_id;label\np001;same\n", encoding="utf-8")

    with pytest.raises(EvalInputError, match="already exists"):
        write_sample(discovered, tmp_path / "labels.csv", tmp_path / "strata.json")

    assert (tmp_path / existing).read_text(encoding="utf-8") == "pair_id;label\np001;same\n"


@pytest.mark.integration
def test_labels_of_pairs_drawn_again_are_carried_over(discovered, tmp_path):
    old_labels = tmp_path / "old.csv"
    write_sample(discovered, old_labels, tmp_path / "old.json")
    rows = read_rows(old_labels)
    rows[0] = {**rows[0], "label": "same", "note": "checked"}
    with old_labels.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    result = write_sample(
        discovered, tmp_path / "new.csv", tmp_path / "new.json", carry_from=old_labels
    )

    carried = read_rows(tmp_path / "new.csv")
    first = next(r for r in carried if r["pair_id"] == rows[0]["pair_id"])
    assert (first["label"], first["note"]) == ("same", "checked")
    assert result.carried == 1
    assert sum(1 for r in carried if r["label"]) == 1


@pytest.mark.integration
def test_carrying_from_a_missing_file_is_an_error(discovered, tmp_path):
    with pytest.raises(EvalInputError, match="nope.csv"):
        write_sample(
            discovered, tmp_path / "l.csv", tmp_path / "s.json", carry_from=tmp_path / "nope.csv"
        )


def test_carried_labels_must_be_valid_labels():
    rows = [{"left_pi_id": "1", "right_pi_id": "2", "label": "", "note": ""}]
    previous = {("1", "2"): ("=HYPERLINK(1)", "x")}

    carried, count = _carry(rows, previous)

    assert (carried[0]["label"], count) == ("", 0)


@pytest.mark.integration
def test_carrying_from_a_file_without_feed_ids_is_an_error(discovered, tmp_path):
    old = tmp_path / "old.csv"
    old.write_text("pair_id,label\np001,same\n", encoding="utf-8")

    with pytest.raises(EvalInputError, match="left_pi_id"):
        write_sample(discovered, tmp_path / "l.csv", tmp_path / "s.json", carry_from=old)


def test_invalid_carried_labels_are_counted_as_dropped():
    rows = [{"left_pi_id": "1", "right_pi_id": "2", "label": "", "note": ""}]

    _, count = _carry(rows, {("1", "2"): ("yes", "")})

    assert count == 0


def test_unconfirmed_guid_pairs_have_their_own_stratum():
    names = dict(STRATA)

    assert names["unconfirmed_guid"] == 10
