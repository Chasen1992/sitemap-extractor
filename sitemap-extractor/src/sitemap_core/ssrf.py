"""SSRF guard.

`startUrls` is attacker-controlled input and every one of them becomes a DNS
lookup plus a fetch. Resolve first, then refuse private, link-local, loopback
and cloud-metadata ranges. A user pointing the Actor at a metadata endpoint
should be told no, not served credentials.

Conservative rule: if *any* resolved address is blocked, the whole URL is
blocked. That also closes the simple DNS-rebinding shape where one answer is
public and another is internal.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

ALLOWED_SCHEMES = frozenset({"http", "https"})

# Hosts that are metadata/management endpoints regardless of resolved IP.
BLOCKED_HOSTS = frozenset(
    {
        "metadata.google.internal",
        "metadata.goog",
        "instance-data",
        "metadata",
    }
)

_BLOCKED_NETWORKS = tuple(
    ipaddress.ip_network(n)
    for n in (
        # RFC1918
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        # RFC3927 link-local
        "169.254.0.0/16",
        # loopback / unspecified / reserved
        "127.0.0.0/8",
        "0.0.0.0/8",
        "100.64.0.0/10",  # RFC6598 carrier-grade NAT
        "192.0.0.0/24",
        "192.0.2.0/24",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
        # IPv6
        "::1/128",
        "::/128",
        "fc00::/7",
        "fe80::/10",
        "ff00::/8",
        "2001:db8::/32",
        "64:ff9b::/96",
    )
)


class SsrfBlocked(Exception):
    """The target resolves into a range we must not talk to."""

    def __init__(self, url: str, reason: str):
        super().__init__(f"{url}: {reason}")
        self.url = url
        self.reason = reason


class UnsupportedScheme(Exception):
    def __init__(self, url: str, scheme: str):
        super().__init__(f"{url}: scheme {scheme!r} is not http/https")
        self.url = url
        self.scheme = scheme


def _embedded_addresses(addr: ipaddress.IPv4Address | ipaddress.IPv6Address):
    """Yield the address plus any IPv4 address it carries inside it.

    ``::ffff:127.0.0.1`` and ``::ffff:169.254.169.254`` connect to the IPv4
    loopback and to the cloud metadata service on every mainstream OS, yet the
    IPv6 literal matches none of the blocked IPv4 networks — Python's
    ``IPv6Address in IPv4Network`` is always False. 6to4 (``2002::/16``) embeds
    an IPv4 address as well. The embedded form has to be checked separately or
    the guard is decorative.
    """
    yield addr
    for attribute in ("ipv4_mapped", "sixtofour"):
        embedded = getattr(addr, attribute, None)
        if embedded is not None:
            yield embedded


def is_blocked_ip(value: str, permit: tuple[str, ...] = ()) -> bool:
    """True if a literal address is in a refused range.

    ``permit`` lists CIDR ranges explicitly allowed to be reached. It exists
    only so the test corpus can run against a loopback fixture server;
    ``pipeline.extract`` never passes it, so production is always strict.
    """
    try:
        addr = ipaddress.ip_address(value)
    except ValueError:
        return False
    for candidate in _embedded_addresses(addr):
        if _is_permitted(candidate, permit):
            continue
        for net in _BLOCKED_NETWORKS:
            if candidate in net:
                return True
    return False


def _is_permitted(addr: ipaddress.IPv4Address | ipaddress.IPv6Address, permit: tuple[str, ...]) -> bool:
    for cidr in permit:
        try:
            if addr in ipaddress.ip_network(cidr, strict=False):
                return True
        except ValueError:
            continue
    return False


def _host_of(url: str) -> str:
    parts = urlsplit(url)
    host = parts.hostname
    if not host:
        raise SsrfBlocked(url, "no host in URL")
    return host.lower()


def check_url_syntax(url: str, permit: tuple[str, ...] = ()) -> None:
    """Scheme + literal-address check. No DNS, so this is cheap and sync-safe."""
    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UnsupportedScheme(url, scheme or "<missing>")
    host = _host_of(url)
    if host in BLOCKED_HOSTS:
        raise SsrfBlocked(url, f"host {host} is a metadata endpoint")
    if is_blocked_ip(host, permit):
        raise SsrfBlocked(url, f"host {host} is a private or reserved address")


def resolve_and_check(
    url: str, *, timeout: float = 10.0, permit: tuple[str, ...] = ()
) -> list[str]:
    """Resolve the host and refuse if any answer lands in a blocked range.

    Returns the list of resolved address strings so the caller can pin the
    connection to one of them.
    """
    check_url_syntax(url, permit)
    host = _host_of(url)
    try:
        old = socket.getdefaulttimeout()
        socket.setdefaulttimeout(timeout)
        try:
            infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
        finally:
            socket.setdefaulttimeout(old)
    except socket.gaierror as exc:
        raise _DnsFailure(url, str(exc)) from exc
    except OSError as exc:
        raise _DnsFailure(url, str(exc)) from exc

    addresses = sorted({info[4][0] for info in infos})
    if not addresses:
        raise _DnsFailure(url, "host resolved to no addresses")

    for addr in addresses:
        if is_blocked_ip(addr, permit):
            raise SsrfBlocked(url, f"resolved to blocked address {addr}")
    return addresses


class _DnsFailure(Exception):
    def __init__(self, url: str, reason: str):
        super().__init__(f"{url}: DNS failure: {reason}")
        self.url = url
        self.reason = reason
