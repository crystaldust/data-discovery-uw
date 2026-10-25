import hashlib

import httpx
import pytest

from ddm.config import PodcastIndexConfig
from ddm.sources.podcastindex import (
    PodcastIndexClient,
    SourceAccessDenied,
    SourceUnavailable,
    build_auth_headers,
)

CONFIG = PodcastIndexConfig(api_key="k", api_secret="s", user_agent="DataDiscoveryDemo/test")
FIXED_NOW = 1700000000


def make_client(handler, max_attempts=3):
    transport = httpx.MockTransport(handler)
    return PodcastIndexClient(
        CONFIG,
        transport=transport,
        clock=lambda: FIXED_NOW,
        max_attempts=max_attempts,
        retry_wait_seconds=0,
        min_interval_seconds=0,
    )


def test_auth_headers_sign_key_secret_and_date_with_sha1():
    headers = build_auth_headers(CONFIG, now_epoch=FIXED_NOW)

    assert headers["Authorization"] == hashlib.sha1(b"ks1700000000").hexdigest()  # noqa: S324
    assert headers["X-Auth-Key"] == "k"
    assert headers["X-Auth-Date"] == "1700000000"


def test_auth_headers_send_the_configured_user_agent_unchanged():
    headers = build_auth_headers(CONFIG, now_epoch=FIXED_NOW)

    assert headers["User-Agent"] == "DataDiscoveryDemo/test"


def test_auth_headers_never_include_the_secret_itself():
    headers = build_auth_headers(CONFIG, now_epoch=FIXED_NOW)

    assert "s" not in headers.values()


def test_auth_headers_return_a_new_dict_each_call():
    first = build_auth_headers(CONFIG, now_epoch=FIXED_NOW)
    first["X-Auth-Key"] = "tampered"

    assert build_auth_headers(CONFIG, now_epoch=FIXED_NOW)["X-Auth-Key"] == "k"


def test_get_returns_json_and_sends_auth_headers_and_params():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        seen["ua"] = request.headers["User-Agent"]
        return httpx.Response(200, json={"status": "true", "count": 1})

    with make_client(handler) as client:
        body = client.get("/recent/newfeeds", {"max": 10})

    assert body == {"status": "true", "count": 1}
    assert seen == {
        "path": "/api/1.0/recent/newfeeds",
        "params": {"max": "10"},
        "ua": "DataDiscoveryDemo/test",
    }


@pytest.mark.parametrize("status", [401, 403])
def test_access_denied_is_raised_once_without_retry(status):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status, text="denied")

    with make_client(handler) as client, pytest.raises(SourceAccessDenied) as excinfo:
        client.get("/recent/newfeeds")

    assert len(calls) == 1
    assert excinfo.value.status_code == status


def test_rate_limit_is_not_retried_and_reports_retry_after():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "120"})

    with make_client(handler) as client, pytest.raises(SourceAccessDenied) as excinfo:
        client.get("/recent/newfeeds")

    assert len(calls) == 1
    assert excinfo.value.status_code == 429
    assert excinfo.value.retry_after == "120"


def test_server_errors_are_retried_until_success():
    responses = iter([httpx.Response(503), httpx.Response(502), httpx.Response(200, json={})])
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return next(responses)

    with make_client(handler) as client:
        assert client.get("/recent/feeds") == {}

    assert len(calls) == 3


def test_network_errors_give_up_after_max_attempts():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ConnectTimeout("timed out", request=request)

    with make_client(handler, max_attempts=3) as client, pytest.raises(SourceUnavailable):
        client.get("/recent/feeds")

    assert len(calls) == 3


def test_persistent_server_error_raises_source_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    with make_client(handler, max_attempts=2) as client, pytest.raises(SourceUnavailable):
        client.get("/recent/feeds")


def test_other_client_errors_raise_immediately():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(400, text="bad request")

    with make_client(handler) as client, pytest.raises(httpx.HTTPStatusError):
        client.get("/podcasts/byitunesid", {"id": "x"})

    assert len(calls) == 1


def test_get_does_not_mutate_caller_params():
    params = {"max": 5}

    with make_client(lambda request: httpx.Response(200, json={})) as client:
        client.get("/recent/feeds", params)

    assert params == {"max": 5}
