"""Polite HTTP fetching of public podcast feeds.

Rules (employer requirements, not tuning knobs), applied to every hop of a redirect chain:
- every request carries the configured, honest User-Agent, no credentials and no proxy;
- robots.txt is fetched once per host per run with our own client and obeyed, including
  Crawl-delay (a host asking for more than MAX_CRAWL_DELAY_SECONDS is skipped);
- requests to one host are spaced at least MIN_HOST_INTERVAL_SECONDS apart, measured from
  the end of the previous response;
- a 401/403/429 (on a feed or on robots.txt) stops all requests to that host for the rest
  of the run, records a Retry-After, and is never retried with another identity;
- only public addresses are contacted (see rss_net).
One feed's failure is returned as a FetchResult, never raised, so a run keeps going.
"""

import logging
import time
from collections.abc import Callable, Hashable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Self
from urllib.parse import urljoin, urlsplit

import httpx
from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from ddm.sources.rss_net import AddressRefused, Resolver, public_addresses, public_only_transport
from ddm.sources.rss_net import resolve_host as default_resolve_host
from ddm.sources.rss_robots import (
    ALLOW_ALL,
    MAX_CRAWL_DELAY_SECONDS,
    MAX_ROBOTS_BYTES,
    RobotsRules,
    parse_robots,
)

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_SECONDS = 10.0
READ_TIMEOUT_SECONDS = 20.0
# httpx timeouts apply per read; this bounds a whole response, so a server dripping one
# byte at a time cannot stall a sequential run.
MAX_REQUEST_SECONDS = 45.0
MAX_BODY_BYTES = 10 * 1024 * 1024
MAX_REDIRECTS = 5
MIN_HOST_INTERVAL_SECONDS = 1.0
DEFAULT_MAX_ATTEMPTS = 2
DEFAULT_RETRY_WAIT_SECONDS = 2.0
RATE_LIMIT_BACKOFF = timedelta(days=1)
# A 401/403 without Retry-After is taken to last; the feed is not asked again for a month
# unless DDM_REFUSED_BACKOFF_DAYS says otherwise.
REFUSED_BACKOFF = timedelta(days=30)
MAX_RETRY_AFTER = timedelta(days=30)
PERMANENT_REDIRECTS = frozenset({301, 308})
REDIRECTS = frozenset({301, 302, 303, 307, 308})
REFUSED_STATUSES = frozenset({401, 403, 429})
ALLOWED_SCHEMES = frozenset({"http", "https"})
ACCEPT = "application/rss+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.5"
# Only encodings httpx decodes without optional packages; the body cap counts decoded bytes.
ACCEPT_ENCODING = "gzip, deflate"
# Robots fetch failures that RFC 9309 treats as "unavailable", i.e. no rules.
ROBOTS_UNAVAILABLE = frozenset({"too_many_redirects", "bad_redirect", "invalid_url"})


@dataclass(frozen=True)
class FetchResult:
    requested_url: str
    final_url: str
    outcome: str  # fetched | not_modified | failed | skipped
    reason: str | None  # None only for fetched / not_modified
    status: int | None
    body: bytes | None
    content_type: str | None
    etag: str | None
    last_modified: str | None
    moved_permanently: bool  # at least one hop, and every hop was 301/308
    retry_after: datetime | None
    bytes_read: int  # decoded body bytes, robots.txt included
    requests: int  # HTTP requests made for this feed, robots.txt included
    elapsed_ms: int
    attempted_at: datetime


@dataclass
class _Tally:
    """Per-fetch counters, mutable but local to one fetch() call."""

    requests: int = 0
    bytes_read: int = 0


@dataclass(frozen=True)
class _Context:
    url: str
    etag: str | None
    last_modified: str | None
    started: float
    attempted_at: datetime
    tally: _Tally


@dataclass(frozen=True)
class _Response:
    status: int
    url: str
    host: str
    headers: httpx.Headers
    body: bytes | None
    hops: tuple[int, ...]


class _Failure(Exception):
    """A terminal result of one fetch: failed, or skipped by our own politeness rules."""

    def __init__(
        self,
        reason: str,
        *,
        outcome: str = "failed",
        status: int | None = None,
        url: str | None = None,
        retry_after: datetime | None = None,
    ):
        super().__init__(reason)
        self.reason = reason
        self.outcome = outcome
        self.status = status
        self.url = url
        self.retry_after = retry_after

    def at(self, url: str) -> "_Failure":
        return _Failure(
            self.reason,
            outcome=self.outcome,
            status=self.status,
            url=url,
            retry_after=self.retry_after,
        )


class _Retryable(_Failure):
    """5xx or a network error: worth one more attempt."""


@dataclass(frozen=True)
class _RobotsVerdict:
    rules: RobotsRules = ALLOW_ALL
    failure: _Failure | None = None


@dataclass(frozen=True)
class _Target:
    scheme: str
    host: str
    port: int | None

    def robots_url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        port = f":{self.port}" if self.port is not None else ""
        return f"{self.scheme}://{host}{port}/robots.txt"


@dataclass
class _HostState:
    robots: dict[tuple[str, str, int | None], _RobotsVerdict] = field(default_factory=dict)
    refused: dict[str, datetime] = field(default_factory=dict)
    last_request: dict[str, float] = field(default_factory=dict)
    interval: dict[str, float] = field(default_factory=dict)


def _target(url: str) -> _Target | None:
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme.lower() not in ALLOWED_SCHEMES or not parts.hostname:
        return None
    if parts.username is not None or parts.password is not None:
        return None  # credentials would send an Authorization header: not an anonymous GET
    return _Target(parts.scheme.lower(), parts.hostname.lower(), port)


def _redact(url: str) -> str:
    """Scheme, host and path only: query strings of private feeds can carry tokens."""
    try:
        parts = urlsplit(url)
        return f"{parts.scheme}://{parts.hostname}{parts.path}"
    except ValueError:
        return "<invalid url>"


def parse_retry_after(value: str | None, now: datetime) -> datetime | None:
    if value is None:
        return None
    value = value.strip()
    if value.isascii() and value.isdecimal():
        seconds = min(int(value), int(MAX_RETRY_AFTER.total_seconds()))
        moment = now + timedelta(seconds=seconds)
    else:
        try:
            moment = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
    return min(max(moment, now), now + MAX_RETRY_AFTER)


def interleave_by_host[T](items: Iterable[T], key: Callable[[T], Hashable]) -> list[T]:
    """Round-robin over hosts, keeping each host's items in their original order."""
    groups: dict[Hashable, list[T]] = {}
    for item in items:
        groups.setdefault(key(item), []).append(item)
    queues = list(groups.values())
    longest = max((len(q) for q in queues), default=0)
    return [q[i] for i in range(longest) for q in queues if i < len(q)]


class FeedFetcher:
    def __init__(
        self,
        user_agent: str,
        *,
        transport: httpx.BaseTransport | None = None,
        resolve_host: Resolver = default_resolve_host,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] = time.sleep,
        min_host_interval: float = MIN_HOST_INTERVAL_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        retry_wait_seconds: float = DEFAULT_RETRY_WAIT_SECONDS,
        max_body_bytes: int = MAX_BODY_BYTES,
        max_request_seconds: float = MAX_REQUEST_SECONDS,
        refused_backoff: timedelta = REFUSED_BACKOFF,
        log_url: Callable[[str], str] | None = None,
    ):
        """`log_url` renders a URL for the logs (default: scheme, host and path); callers
        whose paths carry personal names (author feeds) pass one that hides them."""
        self._user_agent = user_agent
        self._log_url = log_url or _redact
        self._resolve_host = resolve_host
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._sleep = sleep
        self._min_host_interval = min_host_interval
        self._max_attempts = max_attempts
        self._retry_wait_seconds = retry_wait_seconds
        self._max_body_bytes = max_body_bytes
        self._max_request_seconds = max_request_seconds
        self._refused_backoff = refused_backoff
        self._hosts = _HostState()
        self._http = httpx.Client(
            timeout=httpx.Timeout(READ_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS),
            transport=transport or public_only_transport(resolve_host),
            follow_redirects=False,
            trust_env=False,  # never pick up HTTP(S)_PROXY: no proxies, and no SSRF bypass
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def fetch(self, url: str, *, etag: str | None, last_modified: str | None) -> FetchResult:
        ctx = _Context(url, etag, last_modified, self._monotonic(), self._wall_clock(), _Tally())
        conditional = {}
        if etag:
            conditional["If-None-Match"] = etag
        if last_modified:
            conditional["If-Modified-Since"] = last_modified
        try:
            response = self._with_retries(lambda: self._follow(url, conditional, ctx.tally))
        except _Failure as failure:
            logger.info(
                "rss fetch url=%s outcome=%s reason=%s",
                self._log_url(url),
                failure.outcome,
                failure.reason,
            )
            return self._make(
                ctx,
                failure.outcome,
                failure.reason,
                status=failure.status,
                final_url=failure.url or url,
                retry_after=failure.retry_after,
            )
        return self._result_of(ctx, response)

    def _make(
        self, ctx: _Context, outcome: str, reason: str | None = None, **values
    ) -> FetchResult:
        fields = {
            "final_url": ctx.url,
            "status": None,
            "body": None,
            "content_type": None,
            "etag": ctx.etag,
            "last_modified": ctx.last_modified,
            "moved_permanently": False,
            "retry_after": None,
            **values,
        }
        return FetchResult(
            requested_url=ctx.url,
            outcome=outcome,
            reason=reason,
            bytes_read=ctx.tally.bytes_read,
            requests=ctx.tally.requests,
            elapsed_ms=max(0, int((self._monotonic() - ctx.started) * 1000)),
            attempted_at=ctx.attempted_at,
            **fields,
        )

    def _result_of(self, ctx: _Context, response: _Response) -> FetchResult:
        status, headers = response.status, response.headers
        moved = bool(response.hops) and all(h in PERMANENT_REDIRECTS for h in response.hops)
        common = {"status": status, "final_url": response.url, "moved_permanently": moved}
        logger.info("rss fetch url=%s status=%d", self._log_url(response.url), status)
        if status == 304:
            return self._make(
                ctx,
                "not_modified",
                etag=headers.get("ETag") or ctx.etag,
                last_modified=headers.get("Last-Modified") or ctx.last_modified,
                **common,
            )
        if 200 <= status < 300:
            return self._make(
                ctx,
                "fetched",
                body=response.body,
                content_type=headers.get("Content-Type"),
                etag=headers.get("ETag"),
                last_modified=headers.get("Last-Modified"),
                **common,
            )
        if status in REFUSED_STATUSES:
            retry_after = self._refuse(response.host, status, headers)
            return self._make(ctx, "failed", "refused", retry_after=retry_after, **common)
        return self._make(ctx, "failed", f"http_{status}", **common)

    def _refuse(self, host: str, status: int, headers: httpx.Headers) -> datetime:
        now = self._wall_clock()
        retry_after = parse_retry_after(headers.get("Retry-After"), now)
        if retry_after is None:
            retry_after = now + (RATE_LIMIT_BACKOFF if status == 429 else self._refused_backoff)
        self._hosts.refused[host] = retry_after
        logger.warning(
            "rss host refused host=%s status=%d; no more requests this run", host, status
        )
        return retry_after

    def _with_retries[T](self, call: Callable[[], T]) -> T:
        retrying = Retrying(
            stop=stop_after_attempt(self._max_attempts),
            wait=wait_exponential(multiplier=self._retry_wait_seconds),
            retry=retry_if_exception_type(_Retryable),
            sleep=self._sleep,
            reraise=True,
        )
        return retrying(call)

    def _admit(self, target: _Target, url: str, tally: _Tally) -> None:
        """Raise unless our rules allow requesting `url` (refused host, robots.txt)."""
        if target.host in self._hosts.refused:
            raise _Failure(
                "host_refused",
                outcome="skipped",
                url=url,
                retry_after=self._hosts.refused[target.host],
            )
        verdict = self._robots_for(target, tally)
        if verdict.failure is not None:
            raise verdict.failure.at(url)
        delay = verdict.rules.crawl_delay(self._user_agent)
        if delay is not None and delay > MAX_CRAWL_DELAY_SECONDS:
            raise _Failure("crawl_delay_too_long", outcome="skipped", url=url)
        if not verdict.rules.allows(url, self._user_agent):
            raise _Failure("robots_disallowed", outcome="skipped", url=url)

    def _robots_for(self, target: _Target, tally: _Tally) -> _RobotsVerdict:
        key = (target.scheme, target.host, target.port)
        if key not in self._hosts.robots:
            verdict = self._load_robots(target, tally)
            delay = verdict.rules.crawl_delay(self._user_agent)
            if delay is not None:
                self._hosts.interval[target.host] = max(self._min_host_interval, delay)
            self._hosts.robots[key] = verdict
        return self._hosts.robots[key]

    def _load_robots(self, target: _Target, tally: _Tally) -> _RobotsVerdict:
        url = target.robots_url()
        try:
            response = self._with_retries(lambda: self._follow(url, {}, tally, robots=True))
        except _Failure as failure:
            if failure.reason in ROBOTS_UNAVAILABLE:
                return _RobotsVerdict()
            if failure.reason in ("dns_error", "non_public_address"):
                return _RobotsVerdict(failure=_Failure(failure.reason))
            return _RobotsVerdict(failure=_Failure("robots_unreachable", status=failure.status))
        if response.status in REFUSED_STATUSES:
            retry_after = self._refuse(response.host, response.status, response.headers)
            if response.host == target.host:
                failure = _Failure("refused", status=response.status, retry_after=retry_after)
                return _RobotsVerdict(failure=failure)
            return _RobotsVerdict()
        if 200 <= response.status < 300:
            return _RobotsVerdict(rules=parse_robots(response.body or b""))
        # RFC 9309 section 2.3.1.3: any other 4xx means there are no rules.
        return _RobotsVerdict()

    def _follow(
        self, url: str, conditional: dict[str, str], tally: _Tally, *, robots: bool = False
    ) -> _Response:
        """GET with redirects followed by hand, each hop admitted and address-checked."""
        hops: list[int] = []
        current = url
        for hop in range(MAX_REDIRECTS + 1):
            target = _target(current)
            if target is None:
                raise _Failure("invalid_url", url=current)
            if not robots:
                self._admit(target, current, tally)
            self._check_public(target.host, current)
            headers = {
                "User-Agent": self._user_agent,
                "Accept": ACCEPT,
                "Accept-Encoding": ACCEPT_ENCODING,
                **conditional,
            }
            status, response_headers, body = self._get(current, target.host, headers, tally, robots)
            if status in REDIRECTS:
                location = response_headers.get("Location")
                if not location:
                    raise _Failure("bad_redirect", status=status, url=current)
                if hop == MAX_REDIRECTS:
                    raise _Failure("too_many_redirects", status=status, url=current)
                hops.append(status)
                current = urljoin(current, location)
                continue
            if status >= 500:
                raise _Retryable("http_5xx", status=status, url=current)
            return _Response(status, current, target.host, response_headers, body, tuple(hops))
        raise AssertionError("unreachable")  # pragma: no cover

    def _get(
        self, url: str, host: str, headers: dict[str, str], tally: _Tally, robots: bool
    ) -> tuple[int, httpx.Headers, bytes | None]:
        self._throttle(host)
        tally.requests += 1
        deadline = self._monotonic() + self._max_request_seconds
        try:
            with self._http.stream("GET", url, headers=headers) as response:
                body = self._read_body(response, tally, deadline, robots, url)
                return response.status_code, response.headers, body
        except httpx.TransportError as exc:
            logger.warning(
                "rss network error url=%s error=%s", self._log_url(url), type(exc).__name__
            )
            raise _Retryable("network_error", url=url) from exc
        except httpx.DecodingError as exc:
            raise _Failure("decoding_error", url=url) from exc
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise _Failure("request_error", url=url) from exc
        finally:
            self._hosts.last_request[host] = self._monotonic()

    def _read_body(
        self, response: httpx.Response, tally: _Tally, deadline: float, robots: bool, url: str
    ) -> bytes | None:
        if not 200 <= response.status_code < 300:
            return None
        limit = MAX_ROBOTS_BYTES if robots else self._max_body_bytes
        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_bytes():
            chunks.append(chunk)
            size += len(chunk)
            tally.bytes_read += len(chunk)
            if self._monotonic() > deadline:
                raise _Failure("too_slow", status=response.status_code, url=url)
            if size > limit:
                if robots:
                    return b"".join(chunks)[:limit]  # RFC 9309: parse what fits
                raise _Failure("too_large", status=response.status_code, url=url)
        return b"".join(chunks)

    def _throttle(self, host: str) -> None:
        last = self._hosts.last_request.get(host)
        if last is None:
            return
        interval = self._hosts.interval.get(host, self._min_host_interval)
        wait = interval - (self._monotonic() - last)
        if wait > 0:
            self._sleep(wait)

    def _check_public(self, host: str, url: str) -> None:
        try:
            public_addresses(host, self._resolve_host)
        except AddressRefused as exc:
            raise _Failure(exc.reason, url=url) from exc
