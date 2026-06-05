from __future__ import annotations

import ipaddress
import socket
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit


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

    def fetch_new_calls(self, since: datetime) -> Iterator[NormalizedCall]: ...
