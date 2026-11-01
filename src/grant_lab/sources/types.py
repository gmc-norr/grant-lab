from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit

import httpx

from grant_lab.net_safety import (
    BlockedAddress,
    ResolutionFailed,
    pinned_transport,
    resolve_public_ip,
)

_logger = logging.getLogger(__name__)

# Transient HTTP statuses worth a retry (server-side or gateway hiccups).
_RETRYABLE_STATUS = frozenset({500, 502, 503, 504})


def request_with_retry(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    attempts: int = 3,
    backoff_base: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
    **kwargs,
) -> httpx.Response:
    """Issue an HTTP request, retrying transient failures with exponential
    backoff. Retries on httpx.TransportError (network/timeout) and on 5xx
    gateway statuses; returns the response otherwise (incl. 4xx, which the
    caller's raise_for_status handles). The final attempt's error/response is
    surfaced unchanged so existing error handling still applies.

    Without this, a single transient 5xx on the source's list/page fetch
    fails the entire ingestion run for one tick.
    """
    last_exc: httpx.TransportError | None = None
    for attempt in range(1, attempts + 1):
        try:
            resp = client.request(method, url, **kwargs)
        except httpx.TransportError as e:
            last_exc = e
            if attempt == attempts:
                raise
        else:
            if resp.status_code in _RETRYABLE_STATUS and attempt < attempts:
                _logger.warning(
                    "sources.request_retry",
                    extra={
                        "event": "request_retry",
                        "url": url,
                        "status": resp.status_code,
                        "attempt": attempt,
                    },
                )
            else:
                return resp
        # Exponential backoff before the next attempt (0.5s, 1s, 2s, ...).
        sleep(backoff_base * (2 ** (attempt - 1)))
    # Unreachable: the loop either returns or re-raises on the last attempt.
    assert last_exc is not None
    raise last_exc


class AdapterError(Exception):
    """Adapter could not complete a fetch — orchestrator should treat as a
    full-run failure (do NOT advance last_fetched_at, increment failure count)."""


def is_safe_url(url: str) -> bool:
    """Return True iff the URL uses an http(s) scheme.

    Adapters use this to drop records whose URL would render as a
    `javascript:` (or `data:`, etc.) link in the dashboard — those would
    execute on click as stored XSS. Validating at ingest keeps stored links
    safe independently of the renderer; output escaping does not strip
    dangerous URL schemes from `href=...`.
    """
    lower = url.lower()
    return lower.startswith(("http://", "https://"))


def resolve_pinned_ip(url: str) -> str | None:
    """Resolve `url`'s host ONCE and return a validated public IP to connect to,
    or None if the URL is unsafe/unresolvable.

    This is the IP-pinning counterpart to `is_safe_fetch_url`: instead of just
    answering "is this safe?", it returns the exact address the caller should
    pin the TCP connection to. Pinning closes the DNS-rebinding window where a
    low-TTL hostname passes `is_safe_fetch_url`'s resolve and then flips to an
    internal address at httpx's connect-time re-resolve.

    The address checks and the resolve-every-answer rule live in
    `grant_lab.net_safety`, shared with the reference fetcher — one copy, so
    the two cannot drift into the same gap again (review 2026-10-26 A7). None,
    rather than an exception, because the adapters treat an unsafe URL as a
    record to drop rather than a run to fail.
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
        return resolve_public_ip(host, None)
    except (BlockedAddress, ResolutionFailed):
        return None


def is_safe_fetch_url(url: str) -> bool:
    """Stronger check for URLs we are about to *fetch* (not just store).

    Returns False on a malformed URL, missing host, unsafe scheme, private IP,
    or DNS failure.

    Defined in terms of `resolve_pinned_ip` so both functions share a single
    DNS resolution and a single source of truth for the private-IP check.
    """
    return resolve_pinned_ip(url) is not None


def _resolve_and_validate_public_ip(host: str, port: int | None) -> str:
    """`net_safety.resolve_public_ip` in the adapters' error domain: anything
    refused or unresolvable is an `AdapterError`, which the orchestrator treats
    as a full-run failure.

    The on-miss path for the pinned backend: a connect for a host that was
    never pre-pinned must NOT fall through to connecting the raw,
    re-resolvable hostname — that would reopen the DNS-rebinding window the
    pin map exists to close.

    `from e`, not `from None`: an ingest-run failure's traceback keeps which
    check fired and, for DNS, the resolver's own `socket.gaierror`. These are
    operator-facing errors from a scheduled run, so the chain is the only
    record there will be (review 2026-10-26 Minor 4).
    """
    try:
        return resolve_public_ip(host, port)
    except (BlockedAddress, ResolutionFailed) as e:
        raise AdapterError(str(e)) from e


def pinned_transport_for(url: str) -> httpx.HTTPTransport | None:
    """Resolve+validate `url`'s host and return an HTTPTransport pinned to that
    IP, or None if the URL is unsafe. Use for production fetches so the
    connection cannot be rebound to an internal address after validation."""
    ip = resolve_pinned_ip(url)
    if ip is None:
        return None
    host = urlsplit(url).hostname
    return pinned_transport({host: ip}, resolve=_resolve_and_validate_public_ip)


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
    # The source says this call can no longer be applied for: its deadline has
    # passed, or the EU portal republished it as "-CANCELLED". The orchestrator
    # uses such a call ONLY to update a row it already holds that is still open,
    # never to create one, and does no embedding or ranking for it. Adapters used
    # to drop these, so a deadline the source moved into the past never reached
    # our row and the call stayed listed as open (outside review 2026-10-29, F5).
    closed: bool = False


@runtime_checkable
class SourceAdapter(Protocol):
    """Each source is one adapter. Implementations live in `sources/<slug>.py`.

    `fetch_new_calls(since)` must yield NormalizedCalls — generator, not list,
    so the orchestrator can stream-upsert without holding everything in memory.
    `since` is the DB's `sources.last_fetched_at`; pass `None`-or-very-old date
    for first-ever fetch.
    """

    slug: str

    def fetch_new_calls(self, since: datetime) -> Iterator[NormalizedCall]:
        """Yield NormalizedCalls modified since `since`."""
        ...
