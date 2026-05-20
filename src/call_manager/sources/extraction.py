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

_logger = logging.getLogger(__name__)

DEFAULT_HTML_TIMEOUT = 10.0
DEFAULT_PDF_TIMEOUT = 30.0
DEFAULT_PDF_MAX_BYTES = 50 * 1024 * 1024  # 50 MB hard ceiling


def fetch_html_main_content(
    url: str,
    *,
    timeout: float = DEFAULT_HTML_TIMEOUT,
    transport: httpx.BaseTransport | None = None,
) -> str | None:
    """Fetch URL and extract main content via Trafilatura.

    Returns None on HTTP error, fetch timeout, or if Trafilatura can't
    identify a main content block. Logs `extraction.html_failed` with
    the URL and the underlying error.
    """
    try:
        with httpx.Client(transport=transport, timeout=timeout) as client:
            resp = client.get(url)
            resp.raise_for_status()
            html = resp.text
    except (httpx.HTTPError, ValueError) as e:
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

    Returns None on HTTP error, oversized payload, PDF parse failure,
    or empty extraction. Logs `extraction.pdf_failed`.
    """
    buf = bytearray()
    try:
        with (
            httpx.Client(transport=transport, timeout=timeout) as client,
            client.stream("GET", url) as resp,
        ):
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
