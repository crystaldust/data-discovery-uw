import csv
import io
from datetime import UTC, datetime

import pytest

from ddm.query.filters import SearchFilters, page_request, parse_filters
from ddm.query.search import EXPORT_HEADER, iter_export_csv, search_entities
from tests import seed

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 29, tzinfo=UTC)


def podcast(
    db,
    name,
    *,
    language="en",
    categories=("marketing",),
    last="2026-10-20",
    source=2,
    run=None,
    kind="historical",
    email=False,
):
    run = run or seed.run(db, source)
    eid = seed.entity(db, "podcast", name, run, kind=kind)
    time_key = "last_episode_at" if source == 2 else "newest_item_at"
    attrs = {"language": language, "categories": list(categories), "owner_email": seed.SECRET_EMAIL}
    if last:
        attrs[time_key] = f"{last}T00:00:00+00:00"
    seed.record(
        db, source, name, eid, run, attrs, provenance="observed" if source == 2 else "historical"
    )
    if email:
        seed.route(db, eid, "feed_owner_email", seed.SECRET_EMAIL, run, source_key=name)
    return eid


def names(page):
    return [item.display_name for item in page.items]


def search(reader, limit=10, offset=0, **raw):
    return search_entities(reader, parse_filters(**raw), page_request(limit, offset), now=NOW)


def test_without_a_query_results_are_ordered_by_latest_activity(db, reader):
    podcast(db, "Old", last="2023-01-01")
    podcast(db, "New", last="2026-10-25")
    podcast(db, "Unknown", last=None)
    seed.refresh(db)

    assert names(search(reader)) == ["New", "Old", "Unknown"]


def test_each_filter_narrows_the_results(db, reader):
    podcast(db, "English Marketing", email=True)
    podcast(db, "French Show", language="fr")
    podcast(db, "Tech Show", categories=("technology",))
    podcast(db, "Dormant Show", last="2025-01-01")
    podcast(db, "Found Later", kind="discovered")
    run = seed.run(db, 4)
    reporter = seed.entity(db, "journalist", "A Reporter", run)
    seed.record(
        db,
        4,
        "Q1",
        reporter,
        run,
        {"employments": [{"outlet_qid": "Q9", "outlet": "The Atlantic"}]},
    )
    seed.refresh(db)

    assert names(search(reader, language="fr")) == ["French Show"]
    assert names(search(reader, category="Technology")) == ["Tech Show"]
    assert names(search(reader, entity_type="journalist")) == ["A Reporter"]
    assert names(search(reader, outlet="the atlantic")) == ["A Reporter"]
    assert names(search(reader, has_contact=True)) == ["English Marketing"]
    assert names(search(reader, first_seen="discovered")) == ["Found Later"]
    assert "Dormant Show" not in names(search(reader, active_within_days=30))
    assert names(
        search(reader, language="en", category="marketing", has_contact=True, active_within_days=30)
    ) == ["English Marketing"]


def test_a_query_finds_close_spellings_and_ranks_the_closest_first(db, reader):
    podcast(db, "The B2B Marketing Show")
    podcast(db, "Marketing School")
    podcast(db, "Cooking Hour")
    seed.refresh(db)

    found = names(search(reader, q="marketing school"))
    typo = names(search(reader, q="marketng"))

    assert found[0] == "Marketing School" and "Cooking Hour" not in found
    assert set(typo) == {"The B2B Marketing Show", "Marketing School"}


def test_like_wildcards_in_a_query_are_literal(db, reader):
    podcast(db, "100% Growth")
    podcast(db, "1000 Growth")
    seed.refresh(db)

    assert "100% Growth" in names(search(reader, q="100%"))
    assert names(search(reader, q="0_")) == []


def test_deleted_entities_are_not_found(db, reader):
    run = seed.run(db, 4)
    gone = seed.run(db, 4)
    eid = seed.entity(db, "journalist", "Gone Reporter", run)
    seed.record(db, 4, "Q2", eid, run, {"employments": []}, deleted_run=gone)
    seed.refresh(db)

    assert search(reader, q="Gone Reporter").items == ()


def test_paging_reports_whether_there_is_more(db, reader):
    for i in range(3):
        podcast(db, f"Show {i}", last=f"2026-10-0{i + 1}")
    seed.refresh(db)

    first = search(reader, limit=2)
    second = search(reader, limit=2, offset=2)

    assert names(first) == ["Show 2", "Show 1"] and first.has_more
    assert names(second) == ["Show 0"] and not second.has_more


def test_summaries_carry_status_and_basis_but_no_email(db, reader):
    podcast(db, "Live Show", email=True)
    podcast(db, "Catalogue Show", source=1, last="2026-10-01")
    seed.refresh(db)

    items = {i.display_name: i for i in search(reader).items}

    assert items["Live Show"].activity_status == "active"
    assert items["Live Show"].activity_basis == "observed"
    assert items["Live Show"].contact_routes == 1
    assert items["Catalogue Show"].activity_basis == "historical"
    assert seed.SECRET_EMAIL not in repr(items)


def test_an_empty_database_gives_an_empty_page(reader):
    page = search(reader)

    assert page.items == () and not page.has_more


def read_csv(chunks):
    return list(csv.reader(io.StringIO("".join(chunks))))


def test_export_returns_every_matching_row_across_batches(db, reader):
    run = seed.run(db, 1)
    for i in range(2500):
        podcast(db, f"Bulk {i}", source=1, run=run, language="en" if i % 2 else "de")
    seed.refresh(db)

    rows = read_csv(iter_export_csv(reader, SearchFilters(), now=NOW))
    german = read_csv(iter_export_csv(reader, parse_filters(language="de"), now=NOW))

    assert tuple(rows[0]) == EXPORT_HEADER
    assert len(rows) == 2501
    assert len(german) == 1251
    assert seed.SECRET_EMAIL not in "".join(",".join(r) for r in rows)


def test_export_neutralises_spreadsheet_formulas(db, reader):
    podcast(db, '=HYPERLINK("http://evil.example")')
    podcast(db, "+1 Show")
    seed.refresh(db)

    rows = read_csv(iter_export_csv(reader, SearchFilters(), now=NOW))
    cells = {r[EXPORT_HEADER.index("display_name")] for r in rows[1:]}

    assert cells == {'\'=HYPERLINK("http://evil.example")', "'+1 Show"}


def test_export_joins_lists_and_formats_dates(db, reader):
    podcast(db, "Listed", categories=("business", "marketing"))
    seed.refresh(db)

    row = read_csv(iter_export_csv(reader, SearchFilters(), now=NOW))[1]
    record = dict(zip(EXPORT_HEADER, row, strict=True))

    assert record["categories"] == "business; marketing"
    assert record["last_activity_at"] == "2026-10-20T00:00:00+00:00"
    assert record["outlets"] == ""
