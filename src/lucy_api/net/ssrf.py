"""Refuse URLs that would turn Lucy into a proxy against the family or the machine.

Lucy shares a host with eight services on loopback. A hostile MCP `resource_metadata`
or server URL of `http://127.0.0.1:8001` is not "a website"; it is an internal request
made with Lucy's process identity. The same is true of RFC 1918 space, link-local
metadata endpoints, and IPv6 unique-local addresses.

Address classification uses :mod:`ipaddress`, not a hand-rolled CIDR list, so the
decision tracks the language's view of global unicast. HTTPS is required: cleartext
to a "public" host is still a credential in a log. Every redirect hop must be passed
through this module again; following Location unchecked is how an allowlisted host
becomes 169.254.169.254.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

from lucy_api.core.errors import LucyError

REFUSED = "This address is not allowed for outbound requests."


def blocked() -> LucyError:
    """One response for every refused destination, so probing does not map the LAN."""
    return LucyError("ssrf-blocked", REFUSED, 403)


def assert_public_https(url: str) -> str:
    """Return ``url`` if it is HTTPS to a globally routed address; otherwise refuse.

    The returned string is the input, not a rewritten form: callers pass it to an
    HTTP client that must then re-check every redirect hop with this same function.
    """
    parts = urlsplit(url)
    if parts.scheme.lower() != "https":
        raise blocked()
    if parts.username is not None or parts.password is not None:
        raise blocked()
    host = parts.hostname
    if not host:
        raise blocked()
    port = parts.port if parts.port is not None else 443
    for address in _resolve(host, port):
        if not _is_global(address):
            raise blocked()
    return url


def _resolve(host: str, port: int) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
    try:
        parsed = ipaddress.ip_address(host)
    except ValueError:
        parsed = None
    if parsed is not None:
        return (parsed,)
    try:
        records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise blocked() from exc
    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    seen: set[str] = set()
    for _family, _kind, _proto, _name, sockaddr in records:
        raw = sockaddr[0]
        address = ipaddress.ip_address(raw)
        key = str(address)
        if key in seen:
            continue
        seen.add(key)
        addresses.append(address)
    if not addresses:
        raise blocked()
    return tuple(addresses)


def _is_global(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        return bool(mapped.is_global)
    return bool(address.is_global)


__all__ = ["REFUSED", "assert_public_https", "blocked"]
