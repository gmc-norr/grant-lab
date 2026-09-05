"""Shared text-extraction helpers for source adapters.

Two best-effort, logged-on-failure helpers that adapters call when their
source API doesn't carry the call body:

- fetch_html_main_content(url) — Trafilatura main-content extraction
- fetch_pdf_text(url) — PyMuPDF text extraction

Both helpers use httpx.Client with a `transport` keyword (mirrors
OllamaClient/OllamaEmbedClient) so tests inject MockTransport.

Adapters call these EXPLICITLY — there is no auto-scrape layer in the
orchestrator. See `sources/__init__.py` for the adapter-authoring
decision tree.
"""

from __future__ import annotations

import logging

import httpx
import pymupdf
import trafilatura

from grant_lab.sources.types import (
    is_safe_fetch_url,
    pinned_transport_for,
)

_logger = logging.getLogger(__name__)

# ~200k chars ≈ ~50k tokens; more than enough for a call summary.
_MAX_EXTRACTED_TEXT_CHARS = 200_000


def _resolve_transport(
    url: str, transport: httpx.BaseTransport | None
) -> httpx.BaseTransport | None:
    """Return the httpx transport to use for fetching `url`, or None if the URL
    is unsafe (caller must abort).

    - Tests inject a `transport` (MockTransport, which never opens a socket):
      we keep the existing `is_safe_fetch_url` guard and use it verbatim.
    - Production passes no transport: we resolve+validate the host ONCE and
      return an IP-pinned transport so httpx connects to the validated address
      instead of re-resolving the hostname at connect time. This closes the
      DNS-rebinding window that `is_safe_fetch_url` alone left open (validate
      then connect race on the same hostname).
    """
    if transport is not None:
        return transport if is_safe_fetch_url(url) else None
    return pinned_transport_for(url)


def _reject_redirect(url: str, resp: httpx.Response, *, event: str) -> None:
    """Log and refuse a redirect response on a validated-URL fetch.

    We never follow redirects (follow_redirects=False) on these streams:
    is_safe_fetch_url validated the original host, but a 3xx Location could
    point at an internal/metadata address that was never checked. We
    re-validate the Location purely for the log line, then return None — a
    redirect always aborts the fetch. Returns None so callers can
    `return _reject_redirect(...)`.
    """
    location = resp.headers.get("location", "")
    location_safe = bool(location) and is_safe_fetch_url(location)
    _logger.warning(
        f"extraction.{event}",
        extra={
            "event": event,
            "url": url,
            "error": (
                f"refused redirect ({resp.status_code}) to {location!r} "
                f"(target_safe={location_safe})"
            ),
        },
    )
    return None


DEFAULT_HTML_TIMEOUT = 10.0
DEFAULT_PDF_TIMEOUT = 30.0
DEFAULT_HTML_MAX_BYTES = 10 * 1024 * 1024  # 10 MB hard ceiling for HTML pages
DEFAULT_PDF_MAX_BYTES = 50 * 1024 * 1024  # 50 MB hard ceiling


def fetch_html_main_content(
    url: str,
    *,
    timeout: float = DEFAULT_HTML_TIMEOUT,
    max_bytes: int = DEFAULT_HTML_MAX_BYTES,
    transport: httpx.BaseTransport | None = None,
) -> str | None:
    """Fetch URL and extract main content via Trafilatura.

    Returns None on HTTP error, fetch timeout, an unsafe/SSRF target, an
    oversized body, or if Trafilatura can't identify a main content block.
    Logs `extraction.html_failed` with the URL and the underlying error.
    """
    # SSRF guard: the URL comes from upstream JSON. Refuse to fetch anything
    # that resolves to a private/loopback/link-local/metadata address before
    # opening a connection. In production this also IP-PINS the connection to
    # the validated address (see _resolve_transport), closing DNS-rebinding.
    fetch_transport = _resolve_transport(url, transport)
    if fetch_transport is None:
        _logger.warning(
            "extraction.html_failed",
            extra={"event": "html_failed", "url": url, "error": "unsafe_url"},
        )
        return None
    buf = bytearray()
    try:
        with (
            # follow_redirects=False is a second SSRF mitigation on top of the
            # IP-pin: we validated the ORIGINAL host, but a 3xx could redirect
            # to an internal/metadata target that was never checked. We refuse
            # to follow redirects at all; if upstream returns a 3xx we
            # re-validate its Location (for the log) and bail.
            httpx.Client(
                transport=fetch_transport, timeout=timeout, follow_redirects=False
            ) as client,
            client.stream("GET", url) as resp,
        ):
            if resp.is_redirect:
                return _reject_redirect(url, resp, event="html_failed")
            resp.raise_for_status()
            for chunk in resp.iter_bytes():
                buf.extend(chunk)
                if len(buf) > max_bytes:
                    _logger.warning(
                        "extraction.html_failed",
                        extra={
                            "event": "html_failed",
                            "url": url,
                            "error": f"oversized: streamed > {max_bytes} bytes",
                        },
                    )
                    return None
            # Decode using the charset httpx inferred from headers/content,
            # falling back to utf-8 with replacement so a bad byte can't crash
            # the parse (Trafilatura tolerates imperfect text).
            encoding = resp.encoding or "utf-8"
            html = bytes(buf).decode(encoding, errors="replace")
    except (httpx.HTTPError, ValueError, LookupError) as e:
        _logger.warning(
            "extraction.html_failed",
            extra={"event": "html_failed", "url": url, "error": str(e)},
        )
        return None

    # Codex Plan F fix: Trafilatura/lxml can raise a variety of exceptions
    # on malformed HTML (RuntimeError, lxml.etree.* errors, encoding errors,
    # etc). Catch broadly here — we never want a single bad page to abort
    # the whole ingest run; degrade to None like every other failure path.
    try:
        extracted = trafilatura.extract(html)  # returns str | None
    except Exception as e:  # noqa: BLE001 — Trafilatura/lxml raise many types
        _logger.warning(
            "extraction.html_failed",
            extra={"event": "html_failed", "url": url, "error": str(e)},
        )
        return None
    if extracted and len(extracted) > _MAX_EXTRACTED_TEXT_CHARS:
        extracted = extracted[:_MAX_EXTRACTED_TEXT_CHARS] + "\n[extracted text truncated]"
    if not extracted or not extracted.strip():
        _logger.warning(
            "extraction.html_failed",
            extra={"event": "html_failed", "url": url, "error": "empty_extraction"},
        )
        return None
    return extracted


def fetch_pdf_text(
    url: str,
    *,
    timeout: float = DEFAULT_PDF_TIMEOUT,
    max_bytes: int = DEFAULT_PDF_MAX_BYTES,
    transport: httpx.BaseTransport | None = None,
) -> str | None:
    """Fetch a PDF URL and extract its text via PyMuPDF.

    Caps download at `max_bytes` (50 MB default — a hard ceiling against
    bombs). The cap is enforced by STREAMING the response and aborting
    once the running byte count exceeds the limit — we never materialize
    a payload larger than `max_bytes` in memory.

    Returns None on HTTP error, an unsafe/SSRF target, oversized payload,
    PDF parse failure, or empty extraction. Logs `extraction.pdf_failed`.
    """
    fetch_transport = _resolve_transport(url, transport)
    if fetch_transport is None:
        _logger.warning(
            "extraction.pdf_failed",
            extra={"event": "pdf_failed", "url": url, "error": "unsafe_url"},
        )
        return None
    buf = bytearray()
    try:
        with (
            # follow_redirects=False — same SSRF mitigation as the HTML path
            # (IP-pinned connection plus no-redirect-following). See
            # fetch_html_main_content for the rationale.
            httpx.Client(
                transport=fetch_transport, timeout=timeout, follow_redirects=False
            ) as client,
            client.stream("GET", url) as resp,
        ):
            if resp.is_redirect:
                return _reject_redirect(url, resp, event="pdf_failed")
            resp.raise_for_status()
            for chunk in resp.iter_bytes():
                buf.extend(chunk)
                if len(buf) > max_bytes:
                    _logger.warning(
                        "extraction.pdf_failed",
                        extra={
                            "event": "pdf_failed",
                            "url": url,
                            "error": f"oversized: streamed > {max_bytes} bytes",
                        },
                    )
                    return None
    except (httpx.HTTPError, ValueError) as e:
        _logger.warning(
            "extraction.pdf_failed",
            extra={"event": "pdf_failed", "url": url, "error": str(e)},
        )
        return None

    payload = bytes(buf)

    try:
        with pymupdf.open(stream=payload, filetype="pdf") as doc:
            chunks = [page.get_text() for page in doc]
    except Exception as e:  # noqa: BLE001 — PyMuPDF can raise various errors on corrupt input
        _logger.warning(
            "extraction.pdf_failed",
            extra={"event": "pdf_failed", "url": url, "error": str(e)},
        )
        return None

    text = "\n".join(c for c in chunks if c).strip()
    if not text:
        _logger.warning(
            "extraction.pdf_failed",
            extra={"event": "pdf_failed", "url": url, "error": "empty_extraction"},
        )
        return None
    return text
