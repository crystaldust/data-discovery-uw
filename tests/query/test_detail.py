import uuid
from dataclasses import asdict

import pytest

from ddm.query.detail import MAX_OBSERVATIONS, get_entity, parse_entity_id
from ddm.query.fields import OTHER_FIELDS
from ddm.query.filters import QueryInputError
from tests import seed

pytestmark = pytest.mark.integration


def moved_journalist(db):
    """A Wikidata person who now writes for another outlet: one entity, two records."""
    wd_run = seed.run(db, 4, "2026-10-26T00:00:00Z")
    rss_run = seed.run(db, 5, "2026-10-28T00:00:00Z")
    eid = seed.entity(db, "journalist", "Some Reporter", wd_run, kind="historical")
    seed.record(
        db,
        4,
        "Q1",
        eid,
        wd_run,
        {"employments": [{"outlet_qid": "Q9", "outlet": "The New York Times"}]},
        name="Some Reporter",
    )
    seed.observation(
        db,
        wd_run,
        4,
        "Q1",
        "new",
        {"employments": [{"outlet_qid": "Q9", "outlet": "The New York Times"}]},
        provenance="historical",
    )
    page = "theatlantic.com/author/some-reporter"
    seed.record(
        db,
        5,
        page,
        eid,
        rss_run,
        {
            "outlet": "The Atlantic",
            "author_url": page,
            "latest_byline_at": "2026-10-28T09:00:00+00:00",
        },
        provenance="observed",
        name="Some Reporter",
    )
    seed.observation(db, rss_run, 5, page, "new", {"outlet": "The Atlantic"})
    seed.identifier(db, "wikidata_qid", "Q1", eid, 4, wd_run)
    seed.identifier(db, "outlet_author_url", page, eid, 5, rss_run)
    seed.route(
        db,
        eid,
        "outlet_author_page",
        f"https://{page}",
        rss_run,
        source_id=5,
        source_key=page,
        affiliation="confirmed",
    )
    seed.discovery(
        db,
        rss_run,
        5,
        page,
        "rename_or_move",
        eid,
        matched=eid,
        method="outlet_author_url",
        score=1.0,
    )
    return eid


def test_a_moved_journalist_shows_both_records_and_their_observations(db, reader):
    eid = moved_journalist(db)
    seed.refresh(db)

    detail = get_entity(reader, eid)

    assert [r.source for r in detail.records] == ["wikidata", "outlet_rss"]
    assert {o.source for o in detail.observations} == {"wikidata", "outlet_rss"}
    assert [i.kind for i in detail.identifiers] == ["outlet_author_url", "wikidata_qid"]
    assert detail.discoveries[0].classification == "rename_or_move"
    assert detail.discoveries[0].matched_name == "Some Reporter"
    assert detail.contact_routes[0].display_value.startswith("https://theatlantic.com/")
    assert detail.summary.outlets == ("The Atlantic", "The New York Times")


def podcast_with_email(db):
    dump_run = seed.run(db, 1, "2026-10-25T00:00:00Z", kind="historical_backfill")
    eid = seed.entity(db, "podcast", "Mail Show", dump_run)
    seed.record(db, 1, "7", eid, dump_run, {"language": "en", "generator": "x"})
    first = seed.run(db, 2, "2026-10-26T00:00:00Z")
    second = seed.run(db, 2, "2026-10-27T00:00:00Z")
    third = seed.run(db, 2, "2026-10-28T00:00:00Z")
    payloads = [
        {"language": "en", "owner_email": "old@example.org", "episode_count": 3},
        {"language": "en", "owner_email": seed.SECRET_EMAIL, "episode_count": 3},
        {"language": "en", "owner_email": seed.SECRET_EMAIL, "episode_count": 4},
    ]
    seed.record(db, 2, "7", eid, first, payloads[-1], provenance="observed", last_seen_run=third)
    for run, kind, payload in zip(
        (first, second, third), ("new", "changed", "changed"), payloads, strict=True
    ):
        seed.observation(db, run, 2, "7", kind, payload)
    seed.route(db, eid, "feed_owner_email", seed.SECRET_EMAIL, third, source_key="7")
    return eid


def test_a_podcast_detail_masks_emails_and_names_only_changed_public_fields(db, reader):
    eid = podcast_with_email(db)
    seed.refresh(db)

    detail = get_entity(reader, eid)
    rss = next(r for r in detail.records if r.source == "podcast_rss")

    assert detail.contact_routes[0].display_value == "j***@example.org"
    assert [o.changed_fields for o in detail.observations] == [
        ("episode_count",),
        (OTHER_FIELDS,),
        (),
    ]
    assert "owner_email" not in rss.attrs
    assert "generator" not in detail.records[0].attrs
    assert seed.SECRET_EMAIL not in repr(asdict(detail))
    assert "old@example.org" not in repr(asdict(detail))


def test_observations_are_capped(db, reader):
    run = seed.run(db, 1)
    eid = seed.entity(db, "podcast", "Busy", run)
    seed.record(db, 2, "busy", eid, run, {}, provenance="observed")
    for i in range(MAX_OBSERVATIONS + 5):
        seed.observation(
            db, seed.run(db, 2), 2, "busy", "changed" if i else "new", {"episode_count": i}
        )
    seed.refresh(db)

    observations = get_entity(reader, eid).observations

    assert len(observations) == MAX_OBSERVATIONS
    assert observations[-1].changed_fields == ("episode_count",)


def test_pending_candidates_are_shown_on_both_entities(db, reader):
    run = seed.run(db, 1)
    target = seed.entity(db, "podcast", "Growth Show", run)
    other = seed.entity(db, "podcast", "Growth Show!", run)
    seed.record(db, 1, "o", other, run, {})
    seed.candidate(db, 1, "o", target, run, score=0.72)
    seed.refresh(db)

    on_target = get_entity(reader, target).candidates
    on_other = get_entity(reader, other).candidates

    assert on_target[0].other_name == "Growth Show!" and on_target[0].score == pytest.approx(0.72)
    assert on_other[0].other_name == "Growth Show"


def test_an_unknown_entity_is_none(db, reader):
    assert get_entity(reader, uuid.uuid4()) is None


def test_parse_entity_id_refuses_anything_but_a_uuid():
    eid = uuid.uuid4()

    assert parse_entity_id(str(eid)) == eid
    with pytest.raises(QueryInputError, match="entity_id"):
        parse_entity_id("not-a-uuid")


def test_emails_inside_free_text_and_names_are_masked_everywhere(db, reader):
    run = seed.run(db, 2)
    eid = seed.entity(db, "podcast", "Mail jane.doe@example.org", run)
    seed.record(
        db,
        2,
        "m",
        eid,
        run,
        {
            "description": "Pitch us at jane.doe@example.org",
            "author": "jane.doe@example.org (Jane)",
            "feed_url": "https://user:pw@feeds.example.com/m.xml",
        },
        provenance="observed",
        name="Mail jane.doe@example.org",
    )
    seed.identifier(db, "feed_url", "feeds.example.com/m.xml?u=jane.doe@example.org", eid, 2, run)
    seed.route(
        db,
        eid,
        "website",
        "https://show.example.com/?contact=jane.doe@example.org",
        run,
        source_key="m",
    )
    seed.refresh(db)

    detail = repr(asdict(get_entity(reader, eid)))

    assert "jane.doe@example.org" not in detail and "user:pw" not in detail
    assert "j***@example.org" in detail


def test_routes_of_a_deleted_record_are_not_shown(db, reader):
    run = seed.run(db, 2)
    gone = seed.run(db, 2)
    eid = seed.entity(db, "podcast", "Moved Away", run)
    seed.record(db, 1, "live", eid, run, {})
    seed.record(db, 2, "old", eid, run, {}, provenance="observed", deleted_run=gone)
    seed.route(db, eid, "website", "https://old.example.com", run, source_key="old")
    seed.refresh(db)

    detail = get_entity(reader, eid)

    assert detail.contact_routes == ()
    assert detail.summary.contact_routes == 0
    assert [r.deleted_at is not None for r in detail.records] == [False, True]
