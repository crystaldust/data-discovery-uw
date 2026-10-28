from datetime import UTC, datetime, timedelta

import httpx
import pytest

from ddm.sources.rss_fetch import FeedFetcher, interleave_by_host

UA = "DataDiscoveryDemo/0.1 (+mailto:test@example.com)"
NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
PUBLIC_IP = "93.184.216.34"
FEED = "https://feeds.example.com/show.xml"
RSS = b"<rss><channel><title>x</title></channel></rss>"


class FakeClock:
    def __init__(self, step: float = 0.0):
        self.now = 0.0
        self.step = step
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def tick(self) -> None:
        self.now += self.step


class Recorder:
    """Routes by path; '/robots.txt' answers 404 unless a route says otherwise."""

    def __init__(self, routes, clock=None):
        self.routes = routes
        self.clock = clock
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.clock is not None:
            self.clock.tick()
        key = f"{request.url.host}{request.url.path}"
        route = self.routes.get(key, self.routes.get(request.url.path))
        if route is None:
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            raise AssertionError(f"unexpected request {request.url}")
        if callable(route):
            return route(request)
        if isinstance(route, list):
            return route.pop(0)
        return route

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]

    def feed_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path != "/robots.txt"]


def make_fetcher(handler, *, resolve=lambda host: [PUBLIC_IP], clock=None, **kwargs):
    clock = clock or FakeClock()
    return FeedFetcher(
        UA,
        transport=httpx.MockTransport(handler),
        resolve_host=resolve,
        monotonic=clock.monotonic,
        wall_clock=lambda: NOW,
        sleep=clock.sleep,
        retry_wait_seconds=0,
        **kwargs,
    )


def fetch(fetcher, url=FEED, etag=None, last_modified=None):
    return fetcher.fetch(url, etag=etag, last_modified=last_modified)


def ok(body=RSS, **headers):
    return httpx.Response(200, content=body, headers=headers)


def redirect(status, location):
    return httpx.Response(status, headers={"Location": location})


def test_sends_the_configured_user_agent_and_conditional_headers():
    handler = Recorder({"/show.xml": ok()})

    fetch(make_fetcher(handler), etag='"v1"', last_modified="Mon, 26 Oct 2026 08:00:00 GMT")

    request = handler.feed_requests()[0]
    assert request.headers["User-Agent"] == UA
    assert request.headers["If-None-Match"] == '"v1"'
    assert request.headers["If-Modified-Since"] == "Mon, 26 Oct 2026 08:00:00 GMT"
    assert all(r.headers["User-Agent"] == UA for r in handler.requests)


def test_no_conditional_headers_without_validators():
    handler = Recorder({"/show.xml": ok()})

    fetch(make_fetcher(handler))

    request = handler.feed_requests()[0]
    assert "If-None-Match" not in request.headers
    assert "If-Modified-Since" not in request.headers


def test_200_returns_the_body_and_new_validators():
    handler = Recorder(
        {"/show.xml": ok(ETag='"v2"', **{"Last-Modified": "x", "Content-Type": "text/xml"})}
    )

    result = fetch(make_fetcher(handler))

    assert (result.outcome, result.reason, result.status) == ("fetched", None, 200)
    assert result.body == RSS
    assert (result.etag, result.last_modified) == ('"v2"', "x")
    assert result.content_type == "text/xml"
    assert result.final_url == FEED
    assert result.moved_permanently is False
    assert result.attempted_at == NOW


def test_304_keeps_the_previous_validators():
    handler = Recorder({"/show.xml": httpx.Response(304)})

    result = fetch(make_fetcher(handler), etag='"v1"', last_modified="lm")

    assert (result.outcome, result.status, result.body) == ("not_modified", 304, None)
    assert (result.etag, result.last_modified) == ('"v1"', "lm")


def test_304_prefers_validators_sent_with_it():
    handler = Recorder({"/show.xml": httpx.Response(304, headers={"ETag": '"v3"'})})

    result = fetch(make_fetcher(handler), etag='"v1"', last_modified="lm")

    assert (result.etag, result.last_modified) == ('"v3"', "lm")


def test_chain_of_permanent_redirects_marks_the_feed_moved():
    handler = Recorder(
        {
            "/show.xml": redirect(301, "https://feeds.example.com/a.xml"),
            "/a.xml": redirect(308, "/b.xml"),
            "/b.xml": ok(),
        }
    )

    result = fetch(make_fetcher(handler))

    assert result.outcome == "fetched"
    assert result.moved_permanently is True
    assert result.final_url == "https://feeds.example.com/b.xml"


def test_a_temporary_hop_means_the_feed_has_not_moved():
    handler = Recorder(
        {"/show.xml": redirect(301, "/a.xml"), "/a.xml": redirect(302, "/b.xml"), "/b.xml": ok()}
    )

    result = fetch(make_fetcher(handler))

    assert result.moved_permanently is False
    assert result.final_url == "https://feeds.example.com/b.xml"


def test_validators_are_sent_on_every_hop_so_redirected_feeds_can_answer_304():
    handler = Recorder({"/show.xml": redirect(302, "/a.xml"), "/a.xml": httpx.Response(304)})

    result = fetch(make_fetcher(handler), etag='"v1"')

    assert all(r.headers["If-None-Match"] == '"v1"' for r in handler.feed_requests())
    assert result.outcome == "not_modified"


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "fe80::1%en0"])
def test_redirect_to_a_private_address_is_refused(address):
    handler = Recorder({"/show.xml": redirect(302, "http://internal.example.com/x")})

    def resolve(host):
        return [address] if host == "internal.example.com" else [PUBLIC_IP]

    result = fetch(make_fetcher(handler, resolve=resolve))

    assert (result.outcome, result.reason) == ("failed", "non_public_address")
    assert all(r.url.host != "internal.example.com" for r in handler.requests)


def test_literal_private_ip_is_refused_without_resolving():
    handler = Recorder({})

    def resolve(host):
        raise AssertionError("must not resolve a literal address")

    result = fetch(make_fetcher(handler, resolve=resolve), url="http://10.0.0.1/feed")

    assert (result.outcome, result.reason, result.requests) == ("failed", "non_public_address", 0)


def test_dns_failure_is_reported():
    def resolve(host):
        raise OSError("no such host")

    result = fetch(make_fetcher(Recorder({}), resolve=resolve))

    assert (result.outcome, result.reason) == ("failed", "dns_error")


def test_too_many_redirects():
    routes = {f"/r{i}": redirect(302, f"/r{i + 1}") for i in range(10)}
    handler = Recorder({**routes, "/show.xml": redirect(302, "/r0")})

    result = fetch(make_fetcher(handler))

    assert (result.outcome, result.reason) == ("failed", "too_many_redirects")
    assert len(handler.feed_requests()) == 6


def test_redirect_without_location():
    handler = Recorder({"/show.xml": httpx.Response(301)})

    result = fetch(make_fetcher(handler))

    assert (result.outcome, result.reason, result.status) == ("failed", "bad_redirect", 301)


def test_403_stops_all_requests_to_that_host_for_the_run():
    handler = Recorder(
        {
            "/show.xml": httpx.Response(403),
            "/other.xml": ok(),
            "elsewhere.example.org/x.xml": ok(),
        }
    )
    fetcher = make_fetcher(handler)

    refused = fetch(fetcher)
    blocked = fetch(fetcher, url="https://feeds.example.com/other.xml")
    elsewhere = fetch(fetcher, url="https://elsewhere.example.org/x.xml")

    assert (refused.outcome, refused.reason, refused.status) == ("failed", "refused", 403)
    assert refused.retry_after == NOW + timedelta(days=30)
    assert (blocked.outcome, blocked.reason, blocked.requests) == ("skipped", "host_refused", 0)
    assert blocked.retry_after == refused.retry_after
    assert elsewhere.outcome == "fetched"
    assert [r.url.path for r in handler.feed_requests()] == ["/show.xml", "/x.xml"]


def test_refused_backoff_can_be_configured_and_is_inherited_by_skipped_feeds():
    handler = Recorder({"/show.xml": httpx.Response(403), "/other.xml": ok()})
    fetcher = make_fetcher(handler, refused_backoff=timedelta(days=3))

    refused = fetch(fetcher)
    blocked = fetch(fetcher, url="https://feeds.example.com/other.xml")

    assert refused.retry_after == NOW + timedelta(days=3)
    assert blocked.retry_after == refused.retry_after


def test_configured_refused_backoff_does_not_override_retry_after():
    handler = Recorder({"/show.xml": httpx.Response(403, headers={"Retry-After": "120"})})
    fetcher = make_fetcher(handler, refused_backoff=timedelta(days=3))

    assert fetch(fetcher).retry_after == NOW + timedelta(seconds=120)


def test_429_without_retry_after_ignores_the_refused_backoff():
    handler = Recorder({"/show.xml": httpx.Response(429)})
    fetcher = make_fetcher(handler, refused_backoff=timedelta(days=3))

    assert fetch(fetcher).retry_after == NOW + timedelta(days=1)


def test_429_records_retry_after_in_seconds_and_is_not_retried():
    handler = Recorder({"/show.xml": httpx.Response(429, headers={"Retry-After": "120"})})

    result = fetch(make_fetcher(handler))

    assert (result.outcome, result.reason) == ("failed", "refused")
    assert result.retry_after == NOW + timedelta(seconds=120)
    assert len(handler.feed_requests()) == 1


def test_429_retry_after_as_an_http_date():
    handler = Recorder(
        {"/show.xml": httpx.Response(429, headers={"Retry-After": "Tue, 27 Oct 2026 12:00:00 GMT"})}
    )

    result = fetch(make_fetcher(handler))

    assert result.retry_after == NOW + timedelta(days=1)


def test_429_without_retry_after_backs_off_a_day():
    result = fetch(make_fetcher(Recorder({"/show.xml": httpx.Response(429)})))

    assert result.retry_after == NOW + timedelta(days=1)


def test_retry_after_is_capped_at_thirty_days():
    handler = Recorder({"/show.xml": httpx.Response(429, headers={"Retry-After": "99999999"})})

    result = fetch(make_fetcher(handler))

    assert result.retry_after == NOW + timedelta(days=30)


def test_retry_after_in_the_past_means_now_and_garbage_is_ignored():
    past = Recorder(
        {"/show.xml": httpx.Response(403, headers={"Retry-After": "Mon, 01 Jan 2024 00:00:00 GMT"})}
    )
    garbage = Recorder({"/show.xml": httpx.Response(403, headers={"Retry-After": "soon"})})

    assert fetch(make_fetcher(past)).retry_after == NOW
    assert fetch(make_fetcher(garbage)).retry_after == NOW + timedelta(days=30)


def test_5xx_is_retried_then_reported():
    handler = Recorder({"/show.xml": [httpx.Response(503), httpx.Response(503)]})

    result = fetch(make_fetcher(handler))

    assert (result.outcome, result.reason, result.status) == ("failed", "http_5xx", 503)
    assert len(handler.feed_requests()) == 2


def test_5xx_then_success_is_fetched():
    handler = Recorder({"/show.xml": [httpx.Response(503), ok()]})

    result = fetch(make_fetcher(handler))

    assert result.outcome == "fetched"
    assert result.requests == 3  # robots + two feed attempts


def test_network_error_is_retried_then_reported():
    def boom(request):
        raise httpx.ConnectError("refused", request=request)

    handler = Recorder({"/show.xml": boom})

    result = fetch(make_fetcher(handler))

    assert (result.outcome, result.reason, result.status) == ("failed", "network_error", None)
    assert len(handler.feed_requests()) == 2


def test_404_is_not_retried():
    handler = Recorder({"/show.xml": httpx.Response(404)})

    result = fetch(make_fetcher(handler))

    assert (result.outcome, result.reason, result.status) == ("failed", "http_404", 404)
    assert len(handler.feed_requests()) == 1


def test_body_over_the_limit_is_too_large():
    handler = Recorder({"/show.xml": ok(b"x" * 2048)})

    result = fetch(make_fetcher(handler, max_body_bytes=1024))

    assert (result.outcome, result.reason, result.body) == ("failed", "too_large", None)
    assert result.bytes_read > 1024


def test_robots_disallow_skips_the_feed_without_requesting_it():
    handler = Recorder({"/robots.txt": httpx.Response(200, text="User-agent: *\nDisallow: /")})

    result = fetch(make_fetcher(handler))

    assert (result.outcome, result.reason) == ("skipped", "robots_disallowed")
    assert handler.paths() == ["/robots.txt"]


def test_robots_group_for_our_agent_is_honoured():
    robots = "User-agent: DataDiscoveryDemo\nDisallow: /show.xml\n\nUser-agent: *\nAllow: /\n"
    handler = Recorder({"/robots.txt": httpx.Response(200, text=robots), "/other.xml": ok()})
    fetcher = make_fetcher(handler)

    blocked = fetch(fetcher)
    allowed = fetch(fetcher, url="https://feeds.example.com/other.xml")

    assert blocked.outcome == "skipped"
    assert allowed.outcome == "fetched"


def test_robots_is_fetched_once_per_host():
    handler = Recorder({"/show.xml": ok(), "/other.xml": ok()})
    fetcher = make_fetcher(handler)

    fetch(fetcher)
    fetch(fetcher, url="https://feeds.example.com/other.xml")

    assert handler.paths().count("/robots.txt") == 1


def test_robots_5xx_makes_the_host_unreachable_for_the_run():
    handler = Recorder({"/robots.txt": httpx.Response(500), "/other.xml": ok()})
    fetcher = make_fetcher(handler)

    first = fetch(fetcher)
    second = fetch(fetcher, url="https://feeds.example.com/other.xml")

    assert (first.outcome, first.reason) == ("failed", "robots_unreachable")
    assert (second.reason, second.requests) == ("robots_unreachable", 0)
    assert handler.feed_requests() == []


def test_robots_403_refuses_the_host_without_requesting_the_feed():
    handler = Recorder({"/robots.txt": httpx.Response(403), "/show.xml": ok()})

    result = fetch(make_fetcher(handler))

    assert (result.outcome, result.reason, result.status) == ("failed", "refused", 403)
    assert handler.feed_requests() == []


def test_robots_404_allows_everything():
    handler = Recorder({"/robots.txt": httpx.Response(404), "/show.xml": ok()})

    assert fetch(make_fetcher(handler)).outcome == "fetched"


def test_robots_429_refuses_the_host():
    handler = Recorder({"/robots.txt": httpx.Response(429, headers={"Retry-After": "60"})})
    fetcher = make_fetcher(handler)

    first = fetch(fetcher)
    second = fetch(fetcher, url="https://feeds.example.com/other.xml")

    assert (first.outcome, first.reason, first.status) == ("failed", "refused", 429)
    assert first.retry_after == NOW + timedelta(seconds=60)
    assert (second.outcome, second.reason, second.requests) == ("skipped", "host_refused", 0)


def test_robots_body_counts_towards_requests_and_bytes():
    robots = "User-agent: *\nAllow: /\n"
    handler = Recorder({"/robots.txt": httpx.Response(200, text=robots), "/show.xml": ok()})

    result = fetch(make_fetcher(handler))

    assert result.requests == 2
    assert result.bytes_read == len(robots) + len(RSS)


def test_requests_to_one_host_are_spaced_from_the_end_of_the_previous_response():
    clock = FakeClock(step=0.4)
    handler = Recorder({"/show.xml": ok(), "/other.xml": ok()}, clock=clock)
    fetcher = make_fetcher(handler, clock=clock)

    fetch(fetcher)
    clock.now += 0.3
    fetch(fetcher, url="https://feeds.example.com/other.xml")

    assert clock.sleeps == [pytest.approx(1.0), pytest.approx(0.7)]


def test_different_hosts_are_not_throttled_against_each_other():
    clock = FakeClock()
    handler = Recorder({"/show.xml": ok(), "/x.xml": ok()})
    fetcher = make_fetcher(handler, clock=clock)

    fetch(fetcher)
    fetch(fetcher, url="https://other.example.org/x.xml")

    # One wait per host, between its robots.txt and its feed; none across hosts.
    assert clock.sleeps == [pytest.approx(1.0), pytest.approx(1.0)]


@pytest.mark.parametrize(
    "url", ["ftp://feeds.example.com/x", "not a url", "http:///x", "http://[::1"]
)
def test_invalid_urls_fail_without_requests(url):
    handler = Recorder({})

    result = fetch(make_fetcher(handler), url=url)

    assert (result.outcome, result.reason, result.requests) == ("failed", "invalid_url", 0)
    assert handler.requests == []


def test_fetcher_is_a_context_manager():
    with make_fetcher(Recorder({"/show.xml": ok()})) as fetcher:
        assert fetch(fetcher).outcome == "fetched"


def test_interleave_by_host_takes_hosts_in_turn_and_keeps_order_within_a_host():
    urls = [
        "https://a.example.com/1",
        "https://a.example.com/2",
        "https://b.example.com/1",
        "https://a.example.com/3",
        "https://c.example.com/1",
    ]

    ordered = interleave_by_host(urls, key=lambda u: httpx.URL(u).host)

    assert ordered == [urls[0], urls[2], urls[4], urls[1], urls[3]]


def test_robots_of_a_redirect_target_host_is_obeyed():
    handler = Recorder(
        {
            "/show.xml": redirect(302, "https://cdn.example.org/show.xml"),
            "cdn.example.org/robots.txt": httpx.Response(200, text="User-agent: *\nDisallow: /"),
        }
    )

    result = fetch(make_fetcher(handler))

    assert (result.outcome, result.reason) == ("skipped", "robots_disallowed")
    assert result.final_url == "https://cdn.example.org/show.xml"
    assert [r.url.host for r in handler.feed_requests()] == ["feeds.example.com"]


def test_the_host_that_refused_is_blocked_not_the_one_that_redirected():
    handler = Recorder(
        {
            "/show.xml": redirect(302, "https://cdn.example.org/show.xml"),
            "cdn.example.org/show.xml": httpx.Response(403),
            "/other.xml": redirect(302, "https://cdn.example.org/other.xml"),
            "/third.xml": ok(),
        }
    )
    fetcher = make_fetcher(handler)

    refused = fetch(fetcher)
    redirected_again = fetch(fetcher, url="https://feeds.example.com/other.xml")
    origin_still_open = fetch(fetcher, url="https://feeds.example.com/third.xml")

    assert refused.reason == "refused"
    assert (redirected_again.outcome, redirected_again.reason) == ("skipped", "host_refused")
    assert origin_still_open.outcome == "fetched"
    cdn_feed_requests = [r for r in handler.feed_requests() if r.url.host == "cdn.example.org"]
    assert len(cdn_feed_requests) == 1


def test_robots_wildcards_and_longest_match_follow_rfc_9309():
    robots = "User-agent: *\nDisallow: /*.xml$\nDisallow: /feeds/\nAllow: /feeds/ok\n"
    handler = Recorder(
        {"/robots.txt": httpx.Response(200, text=robots), "/feeds/ok": ok(), "/rss": ok()}
    )
    fetcher = make_fetcher(handler)

    assert fetch(fetcher).reason == "robots_disallowed"
    assert fetch(fetcher, url="https://feeds.example.com/feeds/ok").outcome == "fetched"
    assert fetch(fetcher, url="https://feeds.example.com/rss").outcome == "fetched"


def test_robots_with_a_byte_order_mark_is_still_obeyed():
    body = "\ufeffUser-agent: *\nDisallow: /".encode()
    handler = Recorder({"/robots.txt": httpx.Response(200, content=body)})

    assert fetch(make_fetcher(handler)).reason == "robots_disallowed"


def test_crawl_delay_widens_the_interval_for_that_host():
    clock = FakeClock()
    robots = "User-agent: *\nCrawl-delay: 5\n"
    handler = Recorder(
        {"/robots.txt": httpx.Response(200, text=robots), "/show.xml": ok()}, clock=clock
    )

    fetch(make_fetcher(handler, clock=clock))

    assert clock.sleeps == [pytest.approx(5.0)]


def test_a_crawl_delay_beyond_the_cap_skips_the_host():
    robots = "User-agent: *\nCrawl-delay: 3600\n"
    handler = Recorder({"/robots.txt": httpx.Response(200, text=robots)})

    result = fetch(make_fetcher(handler))

    assert (result.outcome, result.reason) == ("skipped", "crawl_delay_too_long")
    assert handler.feed_requests() == []


def test_corrupt_compressed_body_is_a_failure_not_an_exception():
    def garbage(request):
        stream = httpx.ByteStream(b"not gzip at all")
        return httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=stream)

    handler = Recorder({"/show.xml": garbage})

    result = fetch(make_fetcher(handler))

    assert (result.outcome, result.reason) == ("failed", "decoding_error")


def test_unresolvable_host_names_are_dns_errors():
    def resolve(host):
        raise UnicodeError("label too long")

    result = fetch(make_fetcher(Recorder({}), resolve=resolve), url="https://a..b.example/x")

    assert (result.outcome, result.reason) == ("failed", "dns_error")


def test_absurd_retry_after_is_capped_instead_of_overflowing():
    handler = Recorder({"/show.xml": httpx.Response(429, headers={"Retry-After": "9" * 20})})

    assert fetch(make_fetcher(handler)).retry_after == NOW + timedelta(days=30)


def test_a_slow_drip_response_is_abandoned():
    clock = FakeClock(step=100.0)
    handler = Recorder({"/show.xml": ok()}, clock=clock)

    result = fetch(make_fetcher(handler, clock=clock, max_request_seconds=45))

    assert (result.outcome, result.reason) == ("failed", "too_slow")


def test_proxy_environment_variables_are_ignored(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example.net:3128")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.example.net:3128")
    handler = Recorder({"/show.xml": ok()})

    result = fetch(make_fetcher(handler))

    assert result.outcome == "fetched"
    assert len(handler.requests) == 2


def test_only_identity_compatible_encodings_are_requested():
    handler = Recorder({"/show.xml": ok()})

    fetch(make_fetcher(handler))

    assert handler.feed_requests()[0].headers["Accept-Encoding"] == "gzip, deflate"


def test_logged_urls_go_through_the_given_redactor(caplog):
    handler = Recorder({"/show.xml": ok(), "/gone.xml": httpx.Response(404)})
    fetcher = make_fetcher(handler, log_url=lambda url: "feed#1")

    with caplog.at_level("INFO", logger="ddm.sources.rss_fetch"):
        fetch(fetcher)
        fetch(fetcher, url="https://feeds.example.com/gone.xml")

    assert "show.xml" not in caplog.text and "gone.xml" not in caplog.text
    assert "url=feed#1" in caplog.text
