"""The one place the application decides whether an address is safe to open a
socket to, and the one DNS-pinned transport.

Top-level and leaf, for the same reason as `errors.py`: `grant_lab/__init__.py`
is empty and this module imports nothing from the package, so its caller can
reach it without dragging a package into a process that does not want one.

One caller: `sources/types.py` — the ingestion adapters' own fetches. Refusals
surface as `AdapterError`, or as a `None` transport for the fail-closed adapters.

It refuses 100.64.0.0/10 (CGNAT) along with the private, loopback and
link-local ranges (review 2026-10-26, A7 "One SSRF module").

A refusal's detail names the hostname and the resolved address, which is
exactly what an internal-network map is made of (M-sec-1), so it is for the
LOG, never for a user.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Iterable

import httpcore
import httpx

#: One httpcore socket option tuple, as `connect_tcp` accepts them.
SocketOption = (
    tuple[int, int, int] | tuple[int, int, bytes | bytearray] | tuple[int, int, None, int]
)

#: Either address family, as `ipaddress.ip_address` returns it.
IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

#: What a pinned backend calls for a host nobody pre-pinned. It returns a
#: validated literal IP or raises the CALLER's error type — which is why the
#: backend takes it as an argument instead of hard-coding one exception.
#:
#: The port is `int`, not `int | None`: the only caller is
#: `PinnedNetworkBackend.connect_tcp`, whose `port` is always a concrete int.
#: Review 2026-10-26 Minor 5 — the alias used to say `int | None`, which made a
#: resolver typed `(host: str, port: int)` not a valid `Resolver` under strict
#: typing. Narrowing the alias is the half that is actually true at every call
#: site; `resolve_public_ip` keeps the wider `int | None` and stays assignable,
#: because a parameter may be wider than the alias requires.
Resolver = Callable[[str, int], str]


class BlockedAddress(Exception):
    """`host` resolved to an address we refuse to connect to.

    The message names the hostname AND the address, deliberately: that detail
    is what makes a refusal debuggable. It is for logs. Putting it on the wire
    turns the endpoint into an internal-hostname and IP oracle for anyone
    logged in (review 2026-10-26 M-sec-1).
    """


class ResolutionFailed(Exception):
    """DNS returned nothing usable for `host`.

    Separate from `BlockedAddress` because the callers answer differently: a
    refusal is the caller's fault (400), a resolution failure is not (502).
    """


def is_not_global(ip: IpAddress) -> bool:
    """Layer 1, and the primary rule: refuse anything the registry does not
    call globally reachable.

    This is the layer that catches 100.64.0.0/10 (carrier-grade NAT), which
    `ipaddress` reports as `is_private=False` — so none of the explicit checks
    below match it. Exploitable wherever CGNAT space is routable (Tailscale,
    some k8s setups).
    """
    return not ip.is_global


def is_explicitly_blocked(ip: IpAddress) -> bool:
    """Layer 2: the named internal ranges — RFC1918, loopback, link-local
    (incl. 169.254.169.254 cloud metadata), multicast, reserved, unspecified.

    Belt and braces, kept on purpose rather than folded into layer 1: it
    catches addresses `is_global` calls globally reachable, such as
    `64:ff9b::7f00:1` — the NAT64 form of 127.0.0.1, where `is_global` is True
    and `is_reserved` is what refuses it. Neither layer subsumes the other;
    tests/test_net_safety.py pins that both ways.
    """
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def is_private_ip(ip_str: str) -> bool:
    """True if we refuse to talk to this literal address.

    Fails closed: an address `ipaddress` cannot parse is not something we are
    going to connect to either.
    """
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # unparseable → reject defensively
    return is_not_global(ip) or is_explicitly_blocked(ip)


def resolve_public_ip(host: str, port: int | None) -> str:
    """Resolve `host` ONCE and return the literal IP a connection may be pinned
    to. Raises `BlockedAddress` or `ResolutionFailed`.

    A literal-IP host is validated directly, with no DNS lookup — so a private
    literal-IP URL is refused even where DNS does not work (the test suite).
    `ipaddress.ip_address` takes the bare form, which is what `urlparse`/
    `urlsplit` hand back for a bracketed IPv6 host.

    EVERY answer must be public, so a name with mixed public and internal
    records cannot slip through on answer ordering; the FIRST answer is the pin
    target. Resolving exactly once is the point: the pin closes the window
    between this validation and the socket connect, which is where a low-TTL
    rebind would otherwise land.
    """
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if is_private_ip(host):
            # One address, named once. The two-part form below exists to pair a
            # NAME with what it resolved to; there is no name here, and the
            # duplicate read as a bug in every log line (review 2026-10-26
            # Minor 6).
            raise BlockedAddress(f"refused to connect to private IP {host!r}")
        return host

    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise ResolutionFailed(f"DNS resolution failed for {host!r}: {e}") from e
    # A TCP sockaddr's first element is the address string; typeshed widens it
    # to `str | int` for the families that carry a numeric first field.
    addrs = [str(info[4][0]) for info in infos]
    if not addrs:
        raise ResolutionFailed(f"no DNS answers for {host!r}")
    for addr in addrs:
        if is_private_ip(addr):
            raise BlockedAddress(f"refused to connect to {host!r} → private IP {addr!r}")
    return addrs[0]


class PinnedNetworkBackend(httpcore.NetworkBackend):
    """Wraps httpcore's network backend so a TCP connect targets a
    pre-validated IP instead of a freshly-resolved hostname.

    The DNS-rebinding defense: the caller resolves the host once (via
    `resolve_public_ip`), then this forces the socket connect to that literal
    IP. TLS SNI and certificate validation are unaffected — httpcore derives
    those from the connection ORIGIN (the request URL's hostname), not from the
    ``host`` argument to ``connect_tcp`` — so the Host header and cert hostname
    stay correct while the socket targets the pinned IP. No URL rewriting, so
    request mocking and Host headers behave normally.

    Residual, unchanged by the merge: the pin map keys on the hostname string,
    which defeats classic time-of-check/time-of-use rebinding. It does NOT
    cover a server that legitimately resolves to several IPs where one rotates
    internal mid-session; we pin the first validated answer per hop, the
    standard SSRF-pinning posture.
    """

    def __init__(
        self, inner: httpcore.NetworkBackend, pin_map: dict[str, str], *, resolve: Resolver
    ) -> None:
        self._inner = inner
        self._pin_map = pin_map
        self._resolve = resolve

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[SocketOption] | None = None,
    ) -> httpcore.NetworkStream:
        pinned = self._pin_map.get(host)
        if pinned is None:
            # Defense in depth: a connect for a host we did not pre-pin must
            # NOT fall through to the raw (re-resolvable) hostname — that would
            # reopen the very window the pin map exists to close. Validate and
            # pin it on the fly; the caller's resolver raises on an internal
            # address.
            pinned = self._resolve(host, port)
        return self._inner.connect_tcp(
            pinned,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[SocketOption] | None = None,
    ) -> httpcore.NetworkStream:
        return self._inner.connect_unix_socket(path, timeout=timeout, socket_options=socket_options)

    def sleep(self, seconds: float) -> None:
        self._inner.sleep(seconds)


def pinned_transport(pin_map: dict[str, str], *, resolve: Resolver) -> httpx.HTTPTransport:
    """An `httpx.HTTPTransport` whose network backend pins the hostnames in
    `pin_map` to their validated IPs at socket-connect time.

    Reaches into `transport._pool._network_backend`, which is private to
    httpcore. That is the known cost of pinning without reimplementing the
    pool, and it is why the real-socket test exists: an httpx upgrade that
    renamed this would otherwise turn pinning off in silence.
    """
    transport = httpx.HTTPTransport()
    pool = transport._pool
    pool._network_backend = PinnedNetworkBackend(pool._network_backend, pin_map, resolve=resolve)
    return transport
