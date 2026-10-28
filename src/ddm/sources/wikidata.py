"""Wikidata journalists: the outlets, author-page id rules, a SPARQL client, and the
normalisation of query results into one record per person.

Wikidata is crowd-edited, so every QID that goes into a query and every external id that
goes into a URL is validated first. The Query Service asks for an identifying User-Agent
and one query at a time; a 401/403/429 is surfaced at once and never retried with another
identity. Unknown values (no English label, no dates, dates less precise than a year) are
absent from attrs, never guessed.
"""

import ipaddress
import logging
import re
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from string import Template
from types import MappingProxyType
from typing import Any, Self
from urllib.parse import urlsplit

import httpx
from tenacity import (
    RetryError,
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ddm.identifiers import is_ascii_digits, normalize_author_url, normalize_qid
from ddm.ingest.records import NormalizedRecord, Reject, make_record, make_reject, safe_raw
from ddm.sources.podcastindex import (
    ACCESS_DENIED_STATUSES,
    SourceAccessDenied,
    SourceResponseError,
    SourceUnavailable,
)

logger = logging.getLogger(__name__)

SOURCE_NAME = "wikidata"
NORMALIZER_VERSION = 1
SPARQL_URL = "https://query.wikidata.org/sparql"
ACCEPT = "application/sparql-results+json"
JOURNALIST = "Q1930187"
# WDQS: 60 s per query, 60 s of processing per minute per User-Agent and IP.
MIN_INTERVAL_SECONDS = 1.5
TIMEOUT_SECONDS = 70.0
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_RETRY_WAIT_SECONDS = 2.0
DETAIL_CHUNK = 200
PRECISION_YEAR, PRECISION_MONTH, PRECISION_DAY = 9, 10, 11
WEBSITE_PROP = "P856"
X_USERNAME_PROP = "P2002"
X_USERNAME_PATTERN = re.compile(r"[A-Za-z0-9_]{1,15}")
MAX_WEBSITE_CHARS = 500
# Names that only resolve inside a network; a public person's website is never one.
PRIVATE_HOST_SUFFIXES = (".localhost", ".internal", ".local", ".lan", ".home.arpa")
DATE_PATTERN = re.compile(r"\+?(\d{4,})-(\d{2})-(\d{2})T")
SLUG_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")

# QIDs verified by hand in spike 4: name search resolved "Axios" to a river and "Forbes"
# to a surname.
OUTLETS: Mapping[str, str] = MappingProxyType(
    {
        "Q9684": "The New York Times",
        "Q166032": "The Washington Post",
        "Q164746": "The Wall Street Journal",
        "Q1160945": "BBC News",
        "Q11148": "The Guardian",
        "Q130879": "Reuters",
        "Q40469": "Associated Press",
        "Q14270642": "Bloomberg News",
        "Q48340": "CNN",
        "Q671510": "NPR",
        "Q183399": "Financial Times",
        "Q1542536": "The Atlantic",
        "Q3109740": "Politico",
        "Q28230873": "Axios",
        "Q2155442": "The Verge",
        "Q520154": "Wired",
        "Q1022366": "TechCrunch",
        "Q25161472": "Forbes",
        "Q286707": "Business Insider",
        "Q180089": "The Economist",
    }
)


@dataclass(frozen=True)
class AuthorIdRule:
    """A Wikidata external-id property for an outlet's own author pages ($1 is the id)."""

    outlet_qid: str
    pattern: re.Pattern[str]
    url_template: str


AUTHOR_ID_RULES: Mapping[str, AuthorIdRule] = MappingProxyType(
    {
        "P6791": AuthorIdRule("Q1542536", SLUG_PATTERN, "https://www.theatlantic.com/author/$1/"),
        "P8297": AuthorIdRule("Q9684", SLUG_PATTERN, "https://www.nytimes.com/by/$1"),
        "P7943": AuthorIdRule("Q166032", SLUG_PATTERN, "https://www.washingtonpost.com/people/$1/"),
        "P9420": AuthorIdRule("Q48340", SLUG_PATTERN, "https://www.cnn.com/profiles/$1"),
        "P10723": AuthorIdRule("Q11148", SLUG_PATTERN, "https://www.theguardian.com/profile/$1"),
    }
)
DETAIL_PROPS = (WEBSITE_PROP, X_USERNAME_PROP, *AUTHOR_ID_RULES)

EMPLOYER_QUERY = Template(
    """SELECT ?person ?personLabel ?start ?startPrecision ?end ?endPrecision WHERE {
  ?person wdt:P106 wd:$journalist; p:P108 ?st.
  ?st ps:P108 wd:$outlet.
  FILTER NOT EXISTS { ?st wikibase:rank wikibase:DeprecatedRank }
  OPTIONAL { ?st pqv:P580 ?sv.
             ?sv wikibase:timeValue ?start; wikibase:timePrecision ?startPrecision. }
  OPTIONAL { ?st pqv:P582 ?ev.
             ?ev wikibase:timeValue ?end; wikibase:timePrecision ?endPrecision. }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}"""
)
DETAIL_QUERY = Template(
    """SELECT ?person ?prop ?value WHERE {
  VALUES ?person { $people }
  VALUES (?prop ?p) { $props }
  ?person ?p ?value.
}"""
)


@dataclass(frozen=True)
class EmploymentRow:
    """One employer statement as WDQS returned it; values are untrusted strings."""

    person: str | None
    label: str | None
    outlet_qid: str
    start: str | None
    start_precision: str | None
    end: str | None
    end_precision: str | None


@dataclass(frozen=True)
class DetailRow:
    person: str | None
    prop: str | None
    value: str | None


@dataclass(frozen=True)
class WikidataSnapshot:
    employments: tuple[EmploymentRow, ...]
    details: tuple[DetailRow, ...]


class _Retryable(Exception):
    pass


def author_url(prop: str, value: str) -> str | None:
    """The outlet's author page for a valid id, or None (never a URL built from junk)."""
    rule = AUTHOR_ID_RULES.get(prop)
    if rule is None or not isinstance(value, str) or not rule.pattern.fullmatch(value):
        return None
    return rule.url_template.replace("$1", value)


def _checked_qid(value: str) -> str:
    if normalize_qid(value) != value:
        raise ValueError(f"not a bare QID: {value!r}")
    return value


def employer_query(outlet_qid: str) -> str:
    return EMPLOYER_QUERY.substitute(journalist=JOURNALIST, outlet=_checked_qid(outlet_qid))


def detail_query(qids: Iterable[str]) -> str:
    people = " ".join(f"wd:{_checked_qid(q)}" for q in qids)
    props = " ".join(f'("{p}" wdt:{p})' for p in DETAIL_PROPS)
    return DETAIL_QUERY.substitute(people=people, props=props)


def _value(binding: Mapping[str, Any], name: str) -> str | None:
    """An optional SPARQL variable without a value has no key at all."""
    cell = binding.get(name)
    value = cell.get("value") if isinstance(cell, Mapping) else None
    return value if isinstance(value, str) else None


def employment_rows(outlet_qid: str, bindings: Iterable[Mapping]) -> tuple[EmploymentRow, ...]:
    return tuple(
        EmploymentRow(
            person=_value(b, "person"),
            label=_value(b, "personLabel"),
            outlet_qid=outlet_qid,
            start=_value(b, "start"),
            start_precision=_value(b, "startPrecision"),
            end=_value(b, "end"),
            end_precision=_value(b, "endPrecision"),
        )
        for b in bindings
    )


def detail_rows(bindings: Iterable[Mapping]) -> tuple[DetailRow, ...]:
    return tuple(
        DetailRow(person=_value(b, "person"), prop=_value(b, "prop"), value=_value(b, "value"))
        for b in bindings
    )


class WikidataClient:
    def __init__(
        self,
        user_agent: str,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        retry_wait_seconds: float = DEFAULT_RETRY_WAIT_SECONDS,
        min_interval: float = MIN_INTERVAL_SECONDS,
    ):
        self._headers = {"User-Agent": user_agent, "Accept": ACCEPT}
        self._sleep = sleep
        self._monotonic = monotonic
        self._max_attempts = max_attempts
        self._retry_wait_seconds = retry_wait_seconds
        self._min_interval = min_interval
        self._last_request_at: float | None = None
        self.requests = 0
        # No proxies and no redirects, as for every other outbound client.
        self._http = httpx.Client(
            timeout=TIMEOUT_SECONDS, transport=transport, trust_env=False, follow_redirects=False
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def select(self, query: str, what: str) -> list[dict[str, Any]]:
        """Bindings of one SELECT query; `what` names it in logs instead of the query text."""
        retrying = Retrying(
            stop=stop_after_attempt(self._max_attempts),
            wait=wait_exponential(multiplier=self._retry_wait_seconds),
            retry=retry_if_exception_type(_Retryable),
            sleep=self._sleep,
            reraise=False,
        )
        try:
            return retrying(self._select_once, query, what)
        except RetryError as exc:
            raise SourceUnavailable(
                f"wikidata unavailable after {self._max_attempts} attempts what={what}"
            ) from exc

    def _select_once(self, query: str, what: str) -> list[dict[str, Any]]:
        self._respect_min_interval()
        self.requests += 1
        try:
            response = self._http.get(
                SPARQL_URL, params={"query": query, "format": "json"}, headers=self._headers
            )
        except httpx.TransportError as exc:
            logger.warning("wikidata network error what=%s error=%s", what, type(exc).__name__)
            raise _Retryable(type(exc).__name__) from exc
        logger.info("wikidata request what=%s status=%s", what, response.status_code)
        if response.status_code in ACCESS_DENIED_STATUSES:
            raise SourceAccessDenied(
                response.status_code, response.headers.get("Retry-After"), "sparql"
            )
        if response.status_code >= 500:
            raise _Retryable(f"status={response.status_code}")
        if response.status_code >= 300:
            raise SourceResponseError(f"status={response.status_code} what={what}")
        return _bindings_of(response, what)

    def _respect_min_interval(self) -> None:
        if self._last_request_at is not None and self._min_interval > 0:
            elapsed = self._monotonic() - self._last_request_at
            if elapsed < self._min_interval:
                self._sleep(self._min_interval - elapsed)
        self._last_request_at = self._monotonic()


def _bindings_of(response: httpx.Response, what: str) -> list[dict[str, Any]]:
    try:
        body = response.json()
    except ValueError as exc:
        raise SourceResponseError(f"body is not JSON what={what}") from exc
    results = body.get("results") if isinstance(body, dict) else None
    rows = results.get("bindings") if isinstance(results, dict) else None
    if not isinstance(rows, list):
        raise SourceResponseError(f"no results.bindings list what={what}")
    return [row for row in rows if isinstance(row, dict)]


def _qid_order(qid: str) -> int:
    return int(qid[1:])


def fetch_people(client: WikidataClient) -> WikidataSnapshot:
    """Every outlet's journalists, then their details in QID order, DETAIL_CHUNK at a time."""
    employments: list[EmploymentRow] = []
    for outlet_qid in OUTLETS:
        found = client.select(employer_query(outlet_qid), f"outlet={outlet_qid}")
        employments.extend(employment_rows(outlet_qid, found))
    people = sorted({q for r in employments if (q := normalize_qid(r.person))}, key=_qid_order)
    details: list[DetailRow] = []
    for number, first in enumerate(range(0, len(people), DETAIL_CHUNK), start=1):
        chunk = people[first : first + DETAIL_CHUNK]
        details.extend(detail_rows(client.select(detail_query(chunk), f"details={number}")))
    return WikidataSnapshot(employments=tuple(employments), details=tuple(details))


def _date(value: str | None, precision: str | None) -> str | None:
    """Truncated to the stated precision; a month-precise date's '-01' is a placeholder."""
    if value is None or precision is None or not is_ascii_digits(precision):
        return None
    match = DATE_PATTERN.match(value)
    level = int(precision)
    if match is None or level < PRECISION_YEAR or int(match.group(1)) == 0:
        return None
    year, month, day = match.groups()
    if level == PRECISION_YEAR:
        return year
    return f"{year}-{month}" if level == PRECISION_MONTH else f"{year}-{month}-{day}"


def _employment(row: EmploymentRow) -> dict[str, str]:
    found = {"outlet_qid": row.outlet_qid, "outlet": OUTLETS.get(row.outlet_qid, row.outlet_qid)}
    if (start := _date(row.start, row.start_precision)) is not None:
        found["start"] = start
    if (end := _date(row.end, row.end_precision)) is not None:
        found["end"] = end
    return found


def _employments(rows: Iterable[EmploymentRow]) -> list[dict[str, str]]:
    unique = {tuple(sorted(_employment(r).items())): _employment(r) for r in rows}
    return sorted(
        unique.values(), key=lambda e: (e["outlet_qid"], e.get("start", ""), e.get("end", ""))
    )


def _is_public_host(host: str) -> bool:
    if host == "localhost" or host.endswith(PRIVATE_HOST_SUFFIXES):
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return True
    return False  # an IP literal is not a personal website


def _valid_website(value: str) -> bool:
    """A public http(s) address: no credentials, no bad port, no whitespace or controls.

    Stored as a contact route and possibly shown or fetched later, so it is strict."""
    if len(value) > MAX_WEBSITE_CHARS or any(c.isspace() or not c.isprintable() for c in value):
        return False
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return False
    return (
        parts.scheme in ("http", "https")
        and bool(parts.hostname)
        and parts.username is None
        and parts.password is None
        and (port is None or port > 0)
        and _is_public_host(parts.hostname or "")
    )


def _valid_detail(prop: str, value: str) -> bool:
    if prop == WEBSITE_PROP:
        return _valid_website(value)
    if prop == X_USERNAME_PROP:
        return X_USERNAME_PATTERN.fullmatch(value) is not None
    return author_url(prop, value) is not None


def _label(qid: str, rows: Iterable[EmploymentRow]) -> str | None:
    """Without an English label WDQS returns the QID itself: the name is unknown."""
    labels = sorted({" ".join(r.label.split()) for r in rows if r.label})
    label = labels[0] if labels else None
    return None if not label or label == qid else label


def _person(
    qid: str, rows: list[EmploymentRow], details: list[DetailRow]
) -> tuple[NormalizedRecord, list[tuple[str, str, str]]]:
    """The person's record and the (qid, property, value) triples that failed validation."""
    valid: dict[str, set[str]] = {}
    invalid = []
    for row in details:
        if row.prop not in DETAIL_PROPS or row.value is None:
            continue
        if _valid_detail(row.prop, row.value):
            valid.setdefault(row.prop, set()).add(row.value)
        else:
            invalid.append((qid, row.prop, row.value))
    attrs: dict[str, Any] = {"employments": _employments(rows)}
    if websites := valid.get(WEBSITE_PROP):
        attrs["websites"] = sorted(websites)
    if usernames := valid.get(X_USERNAME_PROP):
        attrs["x_usernames"] = sorted(usernames)
    author_ids = {p: sorted(valid[p]) for p in sorted(AUTHOR_ID_RULES) if p in valid}
    if author_ids:
        attrs["author_ids"] = author_ids
    pages = {
        normalize_author_url(author_url(p, v)) for p, values in author_ids.items() for v in values
    }
    identifiers = sorted(
        {("wikidata_qid", qid), *(("outlet_author_url", page) for page in pages if page)}
    )
    record = make_record(
        source_key=qid,
        display_name=_label(qid, rows),
        attrs=attrs,
        source_updated_at=None,
        identifiers=tuple(identifiers),
    )
    return record, invalid


def normalize_people(snapshot: WikidataSnapshot) -> tuple[list[NormalizedRecord], list[Reject]]:
    """One record per valid QID, in QID order; rejects for bad QIDs and bad external ids."""
    people: dict[str, list[EmploymentRow]] = {}
    bad_people: set[tuple[str, str]] = set()
    for row in snapshot.employments:
        qid = normalize_qid(row.person)
        if qid is None:
            bad_people.add((row.person or "", row.outlet_qid))
        else:
            people.setdefault(qid, []).append(row)
    details: dict[str, list[DetailRow]] = {}
    for row in snapshot.details:
        qid = normalize_qid(row.person)
        if qid in people:
            details.setdefault(qid, []).append(row)
    records, invalid = [], []
    for qid in sorted(people, key=_qid_order):
        record, bad_values = _person(qid, people[qid], details.get(qid, []))
        records.append(record)
        invalid.extend(bad_values)
    return records, _rejects(sorted(bad_people), sorted(invalid))


def _rejects(
    bad_people: list[tuple[str, str]], invalid: list[tuple[str, str, str]]
) -> list[Reject]:
    rejects = [
        make_reject(
            row_number=number,
            source_key=None,
            reason="invalid_qid",
            raw=safe_raw({"person": person, "outlet_qid": outlet}),
        )
        for number, (person, outlet) in enumerate(bad_people, start=1)
    ]
    first = len(rejects) + 1
    rejects.extend(
        make_reject(
            row_number=number,
            source_key=qid,
            reason="invalid_external_id",
            raw=safe_raw({"qid": qid, "property": prop, "value": value}),
        )
        for number, (qid, prop, value) in enumerate(invalid, start=first)
    )
    return rejects
