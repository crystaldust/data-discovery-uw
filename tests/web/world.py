"""A small catalogue for the entry-point tests: two podcasts and a moved journalist."""

from tests import seed


def build(db) -> dict:
    dump_run = seed.run(db, 1, "2026-10-01T00:00:00Z", kind="historical_backfill")
    rss_run = seed.run(db, 2, "2026-10-27T00:00:00Z")
    show = seed.entity(db, "podcast", "B2B Marketing Show", dump_run)
    seed.record(db, 1, "1", show, dump_run, {"language": "en", "categories": ["marketing"]})
    seed.record(
        db,
        2,
        "1",
        show,
        rss_run,
        {
            "language": "en",
            "categories": ["marketing", "business"],
            "last_episode_at": "2026-10-20T00:00:00+00:00",
            "owner_email": seed.SECRET_EMAIL,
            "website": "javascript:alert(1)",
            "feed_url": "https://feeds.example.com/1.xml",
        },
        provenance="observed",
    )
    seed.observation(db, rss_run, 2, "1", "new", {"owner_email": seed.SECRET_EMAIL})
    seed.route(db, show, "feed_owner_email", seed.SECRET_EMAIL, rss_run, source_key="1")
    old = seed.entity(db, "podcast", "Old Cooking Hour", dump_run)
    seed.record(
        db,
        1,
        "2",
        old,
        dump_run,
        {
            "language": "en",
            "categories": ["food"],
            "newest_item_at": "2022-01-01T00:00:00+00:00",
        },
    )
    wd_run = seed.run(db, 4, "2026-10-26T00:00:00Z")
    reporter = seed.entity(db, "journalist", "Some Reporter", wd_run, kind="discovered")
    seed.record(
        db,
        4,
        "Q1",
        reporter,
        wd_run,
        {"employments": [{"outlet_qid": "Q9", "outlet": "The New York Times"}]},
    )
    seed.discovery(db, wd_run, 4, "Q1", "new", reporter)
    seed.refresh(db)
    return {"show": show, "old": old, "reporter": reporter}
