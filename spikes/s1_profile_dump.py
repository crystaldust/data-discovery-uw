"""Spike 1: profile the Podcast Index SQLite dump and size the demo population.

Usage: uv run python spikes/s1_profile_dump.py data/raw/podcastindex_feeds.db
"""

import sqlite3
import sys
from pathlib import Path

BASELINE_CUTOFF = "2024-01-01"
DEMO_CATEGORIES = ("marketing", "technology")
MIN_EPISODES = 3

CATEGORY_COLUMNS = ", ".join(f"category{i}" for i in range(1, 11))
CATEGORY_FILTER = " or ".join(f"'{c}' in ({CATEGORY_COLUMNS})" for c in DEMO_CATEGORIES)

DEMO_SLICE = f"""
select * from podcasts
where lower(language) like 'en%'
  and episodeCount >= {MIN_EPISODES}
  and ({CATEGORY_FILTER})
"""

QUERIES = {
    "whole dump": """
        select count(*) as rows,
               min(id) as min_id,
               max(id) as max_id,
               datetime(min(createdOn), 'unixepoch') as first_created,
               datetime(max(createdOn), 'unixepoch') as last_created,
               sum(createdOn is null) as created_null,
               sum(dead <> 0) as dead_rows,
               sum(typeof(duplicateOf) = 'integer') as duplicate_marked,
               sum(podcastId <> id) as podcast_id_differs
        from podcasts
    """,
    "demo slice": f"""
        with f as ({DEMO_SLICE})
        select count(*) as feeds,
               sum(createdOn < strftime('%s', '{BASELINE_CUTOFF}')) as baseline,
               sum(createdOn >= strftime('%s', '{BASELINE_CUTOFF}')) as later,
               sum(url <> originalUrl) as url_moved,
               sum(newestItemPubdate >= strftime('%s', 'now', '-90 days')) as active_90d,
               sum(podcastGuid <> '') as has_guid,
               sum(itunesId > 0) as has_itunes
        from f
    """,
    "later feeds sharing a strong id with the baseline": f"""
        with f as ({DEMO_SLICE}),
             b as (select * from f where createdOn < strftime('%s', '{BASELINE_CUTOFF}')),
             l as (select * from f where createdOn >= strftime('%s', '{BASELINE_CUTOFF}'))
        select
          (select count(*) from l
            where itunesId > 0
              and itunesId in (select itunesId from b where itunesId > 0)) as same_itunes_id,
          (select count(*) from l
            where podcastGuid <> ''
              and podcastGuid in (select podcastGuid from b where podcastGuid <> ''))
            as same_podcast_guid
    """,
}


def run_query(conn: sqlite3.Connection, sql: str) -> tuple[list[str], tuple]:
    cursor = conn.execute(sql)
    columns = [d[0] for d in cursor.description]
    return columns, cursor.fetchone()


def to_markdown(title: str, columns: list[str], row: tuple) -> str:
    header = "| " + " | ".join(columns) + " |"
    divider = "|" + "---|" * len(columns)
    values = "| " + " | ".join(str(v) for v in row) + " |"
    return f"### {title}\n\n{header}\n{divider}\n{values}\n"


def main(db_path: str) -> int:
    path = Path(db_path)
    if not path.is_file():
        print(f"database not found: {path}", file=sys.stderr)
        return 1
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        for title, sql in QUERIES.items():
            columns, row = run_query(conn, sql)
            print(to_markdown(title, columns, row))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
