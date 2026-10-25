"""Spike 2: check the Podcast Index incremental endpoints against the local dump.

Usage: uv run --env-file .env python spikes/s2_podcastindex_api.py [data/raw/podcastindex_feeds.db]
"""

import json
import logging
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from ddm.config import load_podcastindex_config
from ddm.sources.podcastindex import PodcastIndexClient, SourceAccessDenied

DEFAULT_DB = "data/raw/podcastindex_feeds.db"
FIXTURE_DIR = Path("tests/fixtures/podcastindex")
DAY_SECONDS = 86_400
BACKFILL_PROBE_DAYS = (1, 3, 7, 14, 30)
FIXTURE_SAMPLE_SIZE = 3


def fmt_ts(value: int | None) -> str:
    if not value:
        return "-"
    return datetime.fromtimestamp(int(value), UTC).strftime("%Y-%m-%d %H:%M")


def summarize(name: str, body: dict, list_key: str, time_key: str) -> dict:
    items = body.get(list_key) or []
    times = [int(i[time_key]) for i in items if i.get(time_key)]
    summary = {
        "endpoint": name,
        "count": len(items),
        "top_level_keys": sorted(body),
        "item_keys": sorted(items[0]) if items else [],
        "oldest": fmt_ts(min(times)) if times else "-",
        "newest": fmt_ts(max(times)) if times else "-",
    }
    print(f"### {name}\n\n```json\n{json.dumps(summary, indent=2)}\n```\n")
    return summary


def save_fixture(name: str, body: dict, list_key: str) -> None:
    value = body.get(list_key)
    if isinstance(value, list):
        value = value[:FIXTURE_SAMPLE_SIZE]
    trimmed = {**body, list_key: value}
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    (FIXTURE_DIR / f"{name}.json").write_text(json.dumps(trimmed, indent=2) + "\n")


def ids_missing_from_dump(db_path: str, feed_ids: list[int]) -> int:
    if not feed_ids or not Path(db_path).is_file():
        return -1
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        placeholders = ",".join("?" * len(feed_ids))
        found = conn.execute(
            f"select count(*) from podcasts where id in ({placeholders})", feed_ids
        ).fetchone()[0]
    finally:
        conn.close()
    return len(feed_ids) - found


def probe_backfill(client: PodcastIndexClient, now: int) -> None:
    print("### /recent/feeds backfill depth\n\n| since (days ago) | count | oldest | newest |")
    print("|---|---|---|---|")
    for days in BACKFILL_PROBE_DAYS:
        body = client.get("/recent/feeds", {"since": now - days * DAY_SECONDS, "max": 1000})
        items = body.get("feeds") or []
        times = [int(i["newestItemPublishTime"]) for i in items if i.get("newestItemPublishTime")]
        oldest = fmt_ts(min(times)) if times else "-"
        newest = fmt_ts(max(times)) if times else "-"
        print(f"| {days} | {len(items)} | {oldest} | {newest} |")
    print()


def main(db_path: str) -> int:
    logging.basicConfig(level=logging.WARNING)
    config = load_podcastindex_config()
    now = int(time.time())
    with PodcastIndexClient(config) as client:
        try:
            newfeeds = client.get("/recent/newfeeds", {"max": 1000})
            summarize("recent/newfeeds", newfeeds, "feeds", "timeAdded")
            save_fixture("recent_newfeeds", newfeeds, "feeds")

            since_day = client.get("/recent/newfeeds", {"since": now - DAY_SECONDS, "max": 1000})
            summarize("recent/newfeeds?since=-24h", since_day, "feeds", "timeAdded")

            data = client.get("/recent/data", {"max": 100})
            data_feeds = (data.get("data") or {}).get("feeds") or []
            print(f"### recent/data\n\nfeeds={len(data_feeds)} keys={sorted(data)}\n")
            save_fixture("recent_data", data, "data")

            probe_backfill(client, now)

            new_ids = [int(f["id"]) for f in newfeeds.get("feeds") or [] if f.get("id")]
            missing = ids_missing_from_dump(db_path, new_ids)
            print(f"### newfeeds vs dump\n\n{missing} of {len(new_ids)} ids are not in the dump\n")

            itunes_id = next(
                (f.get("itunesId") for f in newfeeds.get("feeds") or [] if f.get("itunesId")),
                None,
            )
            if itunes_id:
                by_itunes = client.get("/podcasts/byitunesid", {"id": itunes_id})
                feed = by_itunes.get("feed") or {}
                print(f"### podcasts/byitunesid\n\nfeed keys: {sorted(feed)}\n")
                save_fixture("podcasts_byitunesid", by_itunes, "feed")
        except SourceAccessDenied as exc:
            print(f"ACCESS DENIED status={exc.status_code} retry_after={exc.retry_after}")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB))
