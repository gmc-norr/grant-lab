from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit

import httpx

# Predicate the orchestrator passes to `fetch_new_calls` so an adapter can skip
# expensive per-call body scraping for calls already ingested with a non-null
# `full_text`. `needs_body(identifier)` returns True when the body is missing
# and should be (re)fetched, False to skip. Adapters whose source carries the
# body inline (e.g. Vinnova) ignore it.
NeedsBody = Callable[[str], bool]


class AdapterError(Exception):
    """Adapter could not complete a fetch — orchestrator should treat as a
    full-run failure (do NOT advance last_fetched_at, increment failure count)."""


def is_safe_url(url: str) -> bool:
    """Return True iff the URL uses an http(s) scheme.

    Adapters use this to drop records whose URL would render as a
    `javascript:` (or `data:`, etc.) link in the dashboard — those would
    execute on click as stored XSS. Doing the check at ingest time is
    defense-in-depth: the dashboard template is also Jinja-autoescaped,
    but autoescape doesn't strip dangerous URL schemes from `href=...`.
    """
    lower = url.lower()
    return lower.startswith(("http://", "https://"))


def _ip_is_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Reject IPs that point inside the deployment's own network: loopback,
    private RFC1918, link-local (incl. 169.254.169.254 cloud metadata),
    multicast, reserved, and the unspecified address.
    """
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def is_safe_fetch_url(url: str) -> bool:
    """Stronger check for URLs we are about to *fetch* (not just store).

    On top of the scheme check, resolve the host and reject any address that
    falls in a private/loopback/link-local/metadata range — closing the SSRF
    hole where upstream JSON points `url` at an internal service or the cloud
    metadata endpoint (169.254.169.254). A literal IP host is checked
    directly; a name is resolved via DNS and every returned address must pass.

    Returns False on a malformed URL, missing host, or DNS failure — callers
    treat False as "do not fetch".
    """
    if not is_safe_url(url):
        return False
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return False
    if not host:
        return False

    # Literal IP host: check directly (no DNS).
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        return not _ip_is_blocked(ip)

    # Hostname: resolve and require EVERY resolved address to be public,
    # so a name with both a public and an internal A record can't slip
    # an internal target past us.
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    addrs = {info[4][0] for info in infos}
    if not addrs:
        return False
    for addr in addrs:
        try:
            if _ip_is_blocked(ipaddress.ip_address(addr)):
                return False
        except ValueError:
            return False
    return True


def resolve_pinned_ip(url: str) -> str | None:
    """Resolve `url`'s host ONCE and return a validated public IP to connect to,
    or None if the URL is unsafe/unresolvable.

    This is the IP-pinning counterpart to `is_safe_fetch_url`: instead of just
    answering "is this safe?", it returns the exact address the caller should
    pin the TCP connection to. Pinning closes the DNS-rebinding window where a
    low-TTL hostname passes `is_safe_fetch_url`'s resolve and then flips to an
    internal address at httpx's connect-time re-resolve. A literal-IP host is
    returned as-is when public; a name is resolved and EVERY answer must be
    public (so a name with mixed public/internal records can't slip through),
    and the first answer is the pin target.
    """
    if not is_safe_url(url):
        return None
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return None
    if not host:
        return None

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        return None if _ip_is_blocked(ip) else host

    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return None
    addrs = [info[4][0] for info in infos]
    if not addrs:
        return None
    for addr in addrs:
        try:
            if _ip_is_blocked(ipaddress.ip_address(addr)):
                return None
        except ValueError:
            return None
    return addrs[0]


class _PinnedNetworkBackend:
    """Wraps httpcore's network backend so TCP connects target a pre-validated
    IP instead of a freshly-resolved hostname.

    DNS-rebinding SSRF defense: the caller resolves the host ONCE to a public IP
    (via `resolve_pinned_ip`), then this backend forces the socket connect to
    that literal IP. TLS SNI and certificate validation are unaffected —
    httpcore derives those from the connection ORIGIN (the request URL's
    hostname), not from the `host` argument to `connect_tcp` — so the Host
    header and cert hostname stay correct while the socket targets the pinned
    IP. No URL rewriting, so request mocking and Host headers behave normally.
    """

    def __init__(self, inner: object, pin_map: dict[str, str]) -> None:
        self._inner = inner
        self._pin_map = pin_map

    def connect_tcp(self, host: str, port: int, *args: object, **kwargs: object) -> object:
        pinned = self._pin_map.get(host, host)
        return self._inner.connect_tcp(pinned, port, *args, **kwargs)

    def __getattr__(self, name: str) -> object:
        # Delegate connect_unix_socket / anything else to the real backend.
        return getattr(self._inner, name)


def pinned_transport(pin_map: dict[str, str]) -> httpx.HTTPTransport:
    """Build an HTTPTransport whose network backend pins the hostnames in
    `pin_map` to their validated IPs at socket-connect time."""
    transport = httpx.HTTPTransport()
    pool = transport._pool
    pool._network_backend = _PinnedNetworkBackend(pool._network_backend, pin_map)
    return transport


def pinned_transport_for(url: str) -> httpx.HTTPTransport | None:
    """Resolve+validate `url`'s host and return an HTTPTransport pinned to that
    IP, or None if the URL is unsafe. Use for production fetches so the
    connection cannot be rebound to an internal address after validation."""
    ip = resolve_pinned_ip(url)
    if ip is None:
        return None
    host = urlsplit(url).hostname
    return pinned_transport({host: ip})


@dataclass(slots=True)
class NormalizedCall:
    """Source-agnostic shape that the ingestion orchestrator persists.

    Adapters MUST normalize timestamps to UTC. Budget values pass through
    in the source's native unit (typically major: euros, kronor) — the
    dashboard renders them with thousand-separators, no normalization.
    `external_id` is the source's own stable identifier — uniqueness is
    per-source, enforced by the DB.
    """

    external_id: str
    url: str
    title: str
    summary: str | None = None
    full_text: str | None = None
    deadline: datetime | None = None
    budget_min: int | None = None
    budget_max: int | None = None
    currency: str | None = None
    programme: str | None = None
    keywords: list[str] = field(default_factory=list)
    published_at: datetime | None = None
    raw: dict = field(default_factory=dict)


@runtime_checkable
class SourceAdapter(Protocol):
    """Each source is one adapter. Implementations live in `sources/<slug>.py`.

    `fetch_new_calls(since)` must yield NormalizedCalls — generator, not list,
    so the orchestrator can stream-upsert without holding everything in memory.
    `since` is the DB's `sources.last_fetched_at`; pass `None`-or-very-old date
    for first-ever fetch.
    """

    slug: str

    def fetch_new_calls(
        self, since: datetime, *, needs_body: NeedsBody | None = None
    ) -> Iterator[NormalizedCall]:
        """Yield NormalizedCalls modified since `since`.

        `needs_body` is an optional predicate the orchestrator supplies so an
        adapter that scrapes a separate detail page for `full_text` can skip
        the scrape when the call is already ingested with a body. Adapters
        whose source carries the body inline ignore it.
        """
        ...
