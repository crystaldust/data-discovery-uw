"""Spike 4: how many journalists with employer history does Wikidata have for our outlets?

Usage: uv run --env-file .env python spikes/s4_wikidata_journalists.py
"""

import os
import sys
import time
from collections import defaultdict

import httpx

SPARQL_URL = "https://query.wikidata.org/sparql"
JOURNALIST = "Q1930187"
PAUSE_SECONDS = 1.5
TIMEOUT_SECONDS = 70.0

# QIDs verified by hand: name search resolved "Axios" to a river and "Forbes" to a surname.
OUTLETS = {
    "The New York Times": "Q9684",
    "The Washington Post": "Q166032",
    "The Wall Street Journal": "Q164746",
    "BBC News": "Q1160945",
    "The Guardian": "Q11148",
    "Reuters": "Q130879",
    "Associated Press": "Q40469",
    "Bloomberg News": "Q14270642",
    "CNN": "Q48340",
    "NPR": "Q671510",
    "Financial Times": "Q183399",
    "The Atlantic": "Q1542536",
    "Politico": "Q3109740",
    "Axios": "Q28230873",
    "The Verge": "Q2155442",
    "Wired": "Q520154",
    "TechCrunch": "Q1022366",
    "Forbes": "Q25161472",
    "Business Insider": "Q286707",
    "The Economist": "Q180089",
}

QUERY_TEMPLATE = """
SELECT ?person ?start ?end WHERE {{
  ?person wdt:P106 wd:{journalist};
          p:P108 ?statement.
  ?statement ps:P108 wd:{outlet}.
  OPTIONAL {{ ?statement pq:P580 ?start. }}
  OPTIONAL {{ ?statement pq:P582 ?end. }}
}}
"""


def journalists_at(http: httpx.Client, qid: str) -> list[dict]:
    query = QUERY_TEMPLATE.format(journalist=JOURNALIST, outlet=qid)
    response = http.get(SPARQL_URL, params={"query": query, "format": "json"})
    response.raise_for_status()
    return response.json()["results"]["bindings"]


def main() -> int:
    user_agent = os.environ.get("DDM_USER_AGENT", "").strip()
    if not user_agent:
        print("missing required environment variable DDM_USER_AGENT", file=sys.stderr)
        return 1

    outlets_by_person: dict[str, set[str]] = defaultdict(set)
    print("| outlet | qid | journalists | with start | current |")
    print("|---|---|---|---|---|")
    with httpx.Client(headers={"User-Agent": user_agent}, timeout=TIMEOUT_SECONDS) as http:
        for name, qid in OUTLETS.items():
            try:
                rows = journalists_at(http, qid)
            except httpx.HTTPStatusError as exc:
                print(f"| {name} | {qid} | HTTP {exc.response.status_code} | | |")
                time.sleep(PAUSE_SECONDS)
                continue
            time.sleep(PAUSE_SECONDS)

            people = {r["person"]["value"] for r in rows}
            with_start = {r["person"]["value"] for r in rows if "start" in r}
            current = {r["person"]["value"] for r in rows if "start" in r and "end" not in r}
            for person in people:
                outlets_by_person[person].add(qid)
            print(f"| {name} | {qid} | {len(people)} | {len(with_start)} | {len(current)} |")

    movers = sum(1 for outlets in outlets_by_person.values() if len(outlets) >= 2)
    print(f"\ndistinct journalists: {len(outlets_by_person)}")
    print(f"journalists linked to 2+ of these outlets (outlet-change candidates): {movers}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
