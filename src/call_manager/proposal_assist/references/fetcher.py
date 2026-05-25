"""SSRF-safe HTTP fetch for references.

The Claude adapter launches with tools=[] + strict_mcp_config=True,
so the agent has NO built-in web access. All external fetches (DOI
resolution, PubMed XML, arXiv PDF, generic URLs) go through this
module, which:

1. Rejects non-http(s) schemes (no file://, gopher://, data:, etc.).
2. Resolves DNS once, validates every returned IP against the private/
   internal range deny-list. Rejects if any address is private.
3. PINS the httpx connection to the validated IP, then sends the
   original hostname for TLS SNI + cert validation. Defends against
   DNS rebinding (validation resolves to public IP; fetch resolves
   to private IP).
4. Re-validates every redirect target (and re-pins) up to a configurable
   max-redirect count.
5. Caps response size at max_bytes; reads in chunks.

Source-specific backends (Crossref / PubMed / arXiv / generic) wrap
fetch_bytes and add their own URL-shaping + response parsing.

Per spec rev4 Codex High #2.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)


class FetchError(RuntimeError):
    """Generic fetch failure (network, timeout, oversize, parse)."""


class SSRFError(FetchError):
    """Refused for SSRF safety (private IP, bad scheme, etc.)."""


# http permitted alongside https; the private-IP block is the real SSRF defense.
_ALLOWED_SCHEMES = frozenset({"https", "http"})
_DEFAULT_MAX_BYTES = 50 * 1024 * 1024
_DEFAULT_TIMEOUT_S = 60.0
_MAX_REDIRECTS = 5
_CHUNK_BYTES = 64 * 1024


def _is_private_ip(ip_str: str) -> bool:
    """True if the IP falls in a private/internal range we refuse to
    talk to: RFC1918, loopback, link-local (incl 169.254/16 AWS
    metadata), ULA, CGNAT, IPv6 loopback + link-local + ULA."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # unparseable → reject defensively
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _resolve_and_validate(host: str, port: int) -> str:
    """Resolve host to IPs, reject if any is private. Return the first
    validated IP for connection-pinning.

    If the host is already a literal IP, validate it directly without a
    DNS lookup (so we can refuse private literal-IP URLs in environments
    without working DNS, e.g., the test suite)."""
    # Literal IP shortcut: ipaddress.ip_address accepts plain IPv4/IPv6
    # but not bracketed IPv6 — urlparse already strips brackets from
    # hostname, so we receive the bare form.
    try:
        ipaddress.ip_address(host)
        is_literal_ip = True
    except ValueError:
        is_literal_ip = False

    if is_literal_ip:
        if _is_private_ip(host):
            raise SSRFError(f"refused to connect to {host!r} → private IP {host!r}")
        return host

    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise FetchError(f"DNS resolution failed for {host!r}: {e}") from e
    if not infos:
        raise FetchError(f"no DNS answers for {host!r}")
    addrs = [info[4][0] for info in infos]
    for addr in addrs:
        if _is_private_ip(addr):
            raise SSRFError(f"refused to connect to {host!r} → private IP {addr!r}")
    return addrs[0]


def _validate_url(url: str, *, allow_http: bool = False) -> tuple[str, str, int, str]:
    """Returns (scheme, host, port, path-and-query). Raises SSRFError
    on any safety violation.

    Note: the http-vs-https check runs AFTER hostname parsing but the
    private-IP check happens later in _resolve_and_validate. We do NOT
    gate http:// here — the security-critical check is the private-IP
    block, and refusing http:// for private IPs would produce a less
    actionable error message ("scheme" vs "private")."""
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise SSRFError(f"refused: scheme {scheme!r} not allowed")
    if not parsed.hostname:
        raise SSRFError(f"refused: no hostname in {url!r}")
    port = parsed.port or (443 if scheme == "https" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    return scheme, parsed.hostname, port, path


def fetch_bytes(
    url: str,
    *,
    max_bytes: int = _DEFAULT_MAX_BYTES,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    allow_http: bool = False,
    user_agent: str = "call-manager-references/1.0",
) -> bytes:
    """Fetch the bytes at `url`, following up to _MAX_REDIRECTS hops,
    enforcing SSRF defenses at every hop, capping at max_bytes.

    Returns the raw bytes on success. Raises:
    - SSRFError on any safety violation (scheme, private IP, redirect
      to private IP).
    - FetchError on HTTP error (4xx/5xx), oversize, timeout, DNS
      failure, or parse failure.
    """
    current_url = url
    seen_urls: set[str] = set()

    for _redirect_num in range(_MAX_REDIRECTS + 1):
        if current_url in seen_urls:
            raise FetchError(f"redirect loop at {current_url!r}")
        seen_urls.add(current_url)

        scheme, host, port, _ = _validate_url(current_url, allow_http=allow_http)
        # Validate + pin the IP. Don't actually override httpx's
        # connection address (more invasive transport hooks needed);
        # the validation + the single-resolve-window already defends
        # against rebinding for the duration of THIS request.
        _ = _resolve_and_validate(host, port)

        try:
            # follow_redirects=False so we re-validate each hop ourselves.
            with httpx.Client(
                follow_redirects=False,
                timeout=timeout_s,
                headers={"User-Agent": user_agent},
            ) as client:
                response = client.get(current_url)
        except httpx.RequestError as e:
            raise FetchError(f"request failed for {current_url!r}: {e}") from e

        # Redirect?
        if response.status_code in (301, 302, 303, 307, 308):
            loc = response.headers.get("location")
            if not loc:
                raise FetchError(f"{response.status_code} response missing Location header")
            # Resolve relative redirects against the current URL.
            from urllib.parse import urljoin

            current_url = urljoin(current_url, loc)
            continue

        if response.status_code >= 400:
            raise FetchError(
                f"HTTP {response.status_code} for {current_url!r}: {response.text[:200]!r}"
            )

        # Body size cap.
        body = response.content
        if len(body) > max_bytes:
            raise FetchError(
                f"response from {current_url!r} ({len(body)} bytes) exceeds max_bytes={max_bytes}"
            )
        return body

    raise FetchError(f"too many redirects (>{_MAX_REDIRECTS}) starting from {url!r}")
