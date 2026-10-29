from types import SimpleNamespace

from ddm.web.auth import FailureLimiter, client_address
from ddm.web.headers import docs_csp


def test_the_limiter_counts_per_client_only():
    limiter = FailureLimiter(window=100, per_client=2)

    for client in ("a", "a", "b", "c", "d"):
        limiter.record(client)

    assert limiter.blocked("a")
    assert not limiter.blocked("b") and not limiter.blocked("e")


def test_old_failures_leave_the_window(monkeypatch):
    now = [0.0]
    monkeypatch.setattr("ddm.web.auth.time.monotonic", lambda: now[0])
    limiter = FailureLimiter(window=10, per_client=1)
    limiter.record("a")

    now[0] = 11.0

    assert not limiter.blocked("a")


def request(headers, host="10.0.0.1"):
    return SimpleNamespace(headers=headers, client=SimpleNamespace(host=host) if host else None)


def test_client_address_prefers_the_first_forwarded_address():
    assert client_address(request({"x-forwarded-for": "203.0.113.5, 10.0.0.2"})) == "203.0.113.5"
    assert client_address(request({})) == "10.0.0.1"
    assert client_address(request({}, host=None)) == "unknown"


def test_docs_csp_hashes_only_inline_scripts():
    policy = docs_csp("<script src='/x.js'></script><script>run()</script>")

    assert policy.count("'sha256-") == 1  # only the inline script, not the src one
    assert "unsafe-inline' https" not in policy
