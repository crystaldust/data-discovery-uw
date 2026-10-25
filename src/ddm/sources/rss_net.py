"""Outbound address policy for feed fetching: public internet addresses only.

Feed URLs and redirect targets come from untrusted data, so every hop is checked before a
request (for a clear failure reason) and again at connect time. The connect-time check
resolves the name itself and connects to the address it vetted, so a DNS answer that
changes between the two lookups (DNS rebinding) cannot reach a private address.
"""

import ipaddress
import socket
from collections.abc import Callable, Iterable

import httpcore
import httpx

# is_global still accepts these: IPv4-compatible and NAT64 forms can embed private IPv4,
# 6to4 can tunnel to anything, site-local is deprecated but routable inside a network.
BLOCKED_NETWORKS = tuple(
    ipaddress.ip_network(net)
    for net in (
        "::/96",
        "64:ff9b::/96",
        "64:ff9b:1::/48",
        "fec0::/10",
        "2002::/16",
        "100.64.0.0/10",
    )
)

Resolver = Callable[[str], Iterable[str]]


class AddressRefused(Exception):
    """The host cannot be fetched; `reason` is dns_error or non_public_address."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def resolve_host(host: str) -> list[str]:
    return [info[4][0] for info in socket.getaddrinfo(host, None)]


def is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def is_public_address(text: str) -> bool:
    try:
        address = ipaddress.ip_address(text.split("%", 1)[0])
    except ValueError:
        return False
    if address.version == 6 and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    blocked = any(address in net for net in BLOCKED_NETWORKS if net.version == address.version)
    return address.is_global and not blocked


def public_addresses(host: str, resolve: Resolver) -> list[str]:
    """All addresses of `host`, or AddressRefused unless every one of them is public."""
    try:
        addresses = [host] if is_ip_literal(host) else list(resolve(host))
    except (OSError, UnicodeError, ValueError) as exc:
        # UnicodeError: getaddrinfo rejects names such as 'a..b' or 64-character labels.
        raise AddressRefused("dns_error") from exc
    if not addresses:
        raise AddressRefused("dns_error")
    if not all(is_public_address(a) for a in addresses):
        raise AddressRefused("non_public_address")
    return [a.split("%", 1)[0] for a in addresses]


class PublicOnlyBackend(httpcore.SyncBackend):
    """Connects to an address it resolved and vetted itself; TLS still verifies the name."""

    def __init__(self, resolve: Resolver = resolve_host):
        self._resolve = resolve

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        try:
            addresses = public_addresses(host, self._resolve)
        except AddressRefused as exc:
            raise httpcore.ConnectError(exc.reason) from exc
        # Try each vetted address in turn, as the resolver order suggests (dual-stack hosts).
        for address in addresses[:-1]:
            try:
                return super().connect_tcp(address, port, timeout, local_address, socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout):
                continue
        return super().connect_tcp(addresses[-1], port, timeout, local_address, socket_options)


def public_only_transport(resolve: Resolver = resolve_host) -> httpx.HTTPTransport:
    """An httpx transport that ignores proxy settings and connects only to public addresses."""
    transport = httpx.HTTPTransport(trust_env=False)
    pool = getattr(transport, "_pool", None)
    # httpx exposes no public hook for the network backend; fail loudly if that changes.
    if not isinstance(pool, httpcore.ConnectionPool) or not hasattr(pool, "_network_backend"):
        raise RuntimeError("cannot install the public-only network backend on this httpx")
    pool._network_backend = PublicOnlyBackend(resolve)
    return transport
