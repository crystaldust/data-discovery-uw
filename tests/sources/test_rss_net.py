import httpcore
import pytest

from ddm.sources import rss_net
from ddm.sources.rss_net import (
    AddressRefused,
    PublicOnlyBackend,
    is_public_address,
    public_addresses,
    public_only_transport,
)


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "100.64.0.1",
        "0.0.0.0",  # noqa: S104 - an address under test, not a bind
        "::1",
        "fe80::1%en0",
        "fc00::1",
        "fec0::1",
        "::ffff:127.0.0.1",
        "::127.0.0.1",
        "64:ff9b::7f00:1",
        "2002:7f00:1::1",
        "not-an-ip",
    ],
)
def test_private_reserved_and_tunnelled_addresses_are_not_public(address):
    assert is_public_address(address) is False


@pytest.mark.parametrize("address", ["93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"])
def test_global_addresses_are_public(address):
    assert is_public_address(address) is True


def test_every_resolved_address_must_be_public():
    with pytest.raises(AddressRefused) as excinfo:
        public_addresses("mixed.example.com", lambda host: ["93.184.216.34", "10.0.0.1"])

    assert excinfo.value.reason == "non_public_address"


def test_literal_addresses_are_checked_without_resolving():
    def resolve(host):
        raise AssertionError("must not resolve")

    assert public_addresses("93.184.216.34", resolve) == ["93.184.216.34"]


@pytest.mark.parametrize("error", [OSError("nx"), UnicodeError("label"), ValueError("bad")])
def test_resolver_errors_are_dns_errors(error):
    def resolve(host):
        raise error

    with pytest.raises(AddressRefused) as excinfo:
        public_addresses("x.example.com", resolve)

    assert excinfo.value.reason == "dns_error"


def test_no_addresses_is_a_dns_error():
    with pytest.raises(AddressRefused, match="dns_error"):
        public_addresses("x.example.com", lambda host: [])


def test_backend_refuses_to_connect_when_the_name_now_resolves_privately(monkeypatch):
    def connect(*args, **kwargs):
        raise AssertionError("must not connect")

    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", connect)
    backend = PublicOnlyBackend(lambda host: ["169.254.169.254"])

    with pytest.raises(httpcore.ConnectError, match="non_public_address"):
        backend.connect_tcp("rebind.example.com", 443)


def test_backend_connects_to_the_address_it_vetted(monkeypatch):
    seen = []

    def connect(self, host, port, timeout=None, local_address=None, socket_options=None):
        seen.append((host, port))
        return "stream"

    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", connect)
    backend = PublicOnlyBackend(lambda host: ["93.184.216.34"])

    assert backend.connect_tcp("feeds.example.com", 443) == "stream"
    assert seen == [("93.184.216.34", 443)]


def test_transport_uses_the_public_only_backend_and_ignores_proxies(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example.net:3128")

    transport = public_only_transport(lambda host: ["93.184.216.34"])

    assert isinstance(transport._pool, httpcore.ConnectionPool)
    assert isinstance(transport._pool._network_backend, PublicOnlyBackend)


def test_transport_refuses_to_start_if_httpx_internals_changed(monkeypatch):
    class Changed:
        def __init__(self, **kwargs):
            self._pool = object()

    monkeypatch.setattr(rss_net.httpx, "HTTPTransport", Changed)

    with pytest.raises(RuntimeError, match="network backend"):
        public_only_transport()


def test_backend_falls_back_to_the_next_vetted_address(monkeypatch):
    seen = []

    def connect(self, host, port, timeout=None, local_address=None, socket_options=None):
        seen.append(host)
        if host == "2606:2800:220:1:248:1893:25c8:1946":
            raise httpcore.ConnectError("unreachable")
        return "stream"

    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", connect)
    backend = PublicOnlyBackend(
        lambda host: ["2606:2800:220:1:248:1893:25c8:1946", "93.184.216.34"]
    )

    assert backend.connect_tcp("feeds.example.com", 443) == "stream"
    assert seen == ["2606:2800:220:1:248:1893:25c8:1946", "93.184.216.34"]
