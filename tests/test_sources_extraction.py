from __future__ import annotations

import socket
from pathlib import Path

import httpx

from call_manager.sources.extraction import (
    _resolve_transport,
    fetch_html_main_content,
    fetch_pdf_text,
)
from call_manager.sources.types import _PinnedNetworkBackend


def _fake_getaddrinfo(*ips):
    def _inner(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in ips]

    return _inner


def test_resolve_transport_injected_safe_url_passes_through():
    mock = httpx.MockTransport(lambda req: httpx.Response(200))
    # Injected transport (test seam): safe url returns the same transport, no pin.
    assert _resolve_transport("https://example.com/x", mock) is mock


def test_resolve_transport_injected_unsafe_url_returns_none():
    mock = httpx.MockTransport(lambda req: httpx.Response(200))
    assert _resolve_transport("http://127.0.0.1/", mock) is None


def test_resolve_transport_production_pins_safe_url(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("93.184.216.34"))
    transport = _resolve_transport("https://example.com/x", None)
    assert transport is not None
    assert isinstance(transport._pool._network_backend, _PinnedNetworkBackend)
    transport.close()


def test_resolve_transport_production_rejects_rebind(monkeypatch):
    # Host that resolves to the cloud-metadata address is refused before connect.
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("169.254.169.254"))
    assert _resolve_transport("https://rebind.example/", None) is None


_SAMPLE_HTML = """\
<html>
<head><title>Sample Funding Call</title></head>
<body>
  <nav>Home | Calls | About</nav>
  <main>
    <h1>Cancer Biomarker Discovery Funding 2026</h1>
    <p>This call funds research into novel biomarkers for early-stage
       cancer detection, with a focus on liquid biopsy approaches.</p>
    <p>Eligible: clinical research groups at EU/EEA institutions.</p>
    <p>Budget: 500K EUR per project, 24 months max.</p>
  </main>
  <footer>© 2026 Funding Body</footer>
</body>
</html>
"""


def _html_transport(payload: str, *, status: int = 200) -> httpx.MockTransport:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=payload, headers={"content-type": "text/html"})

    return httpx.MockTransport(handler)


def _pdf_transport(
    payload: bytes, *, status: int = 200, content_type: str = "application/pdf"
) -> httpx.MockTransport:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=payload, headers={"content-type": content_type})

    return httpx.MockTransport(handler)


def test_fetch_html_main_content_extracts_main_drops_chrome():
    text = fetch_html_main_content(
        "https://example.com/call",
        transport=_html_transport(_SAMPLE_HTML),
    )
    assert text is not None
    # Main content present
    assert "Cancer Biomarker Discovery" in text
    assert "liquid biopsy" in text
    # Boilerplate stripped
    assert "Home | Calls | About" not in text
    assert "© 2026 Funding Body" not in text


def test_fetch_html_main_content_returns_none_on_404():
    text = fetch_html_main_content(
        "https://example.com/missing",
        transport=_html_transport("not found", status=404),
    )
    assert text is None


def test_fetch_html_main_content_returns_none_on_empty_body():
    text = fetch_html_main_content(
        "https://example.com/empty",
        transport=_html_transport("<html><body></body></html>"),
    )
    # Trafilatura returns None for empty body
    assert text is None


def test_fetch_pdf_text_extracts_text_from_real_pdf():
    pdf_bytes = Path(__file__).parent.joinpath("fixtures/sample_call.pdf").read_bytes()
    text = fetch_pdf_text(
        "https://example.com/call.pdf",
        transport=_pdf_transport(pdf_bytes),
    )
    assert text is not None
    assert "Cancer Biomarker Discovery" in text
    assert "liquid biopsy" in text


def test_fetch_pdf_text_returns_none_on_404():
    text = fetch_pdf_text(
        "https://example.com/missing.pdf",
        transport=_pdf_transport(b"not found", status=404),
    )
    assert text is None


def test_fetch_pdf_text_returns_none_on_corrupt_pdf():
    text = fetch_pdf_text(
        "https://example.com/garbage.pdf",
        transport=_pdf_transport(b"not actually a pdf"),
    )
    assert text is None


def test_fetch_pdf_text_rejects_oversized_response():
    """Streaming-with-cap: the helper aborts as soon as the running byte
    count exceeds max_bytes — never materializes the full payload."""
    # 2 MB of zeros — much larger than max_bytes=1 MB; streaming should abort.
    huge = b"\x00" * (2 * 1024 * 1024)
    text = fetch_pdf_text(
        "https://example.com/huge.pdf",
        transport=_pdf_transport(huge),
        max_bytes=1 * 1024 * 1024,
    )
    assert text is None


def test_fetch_html_main_content_logs_warning_on_failure(caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="call_manager.sources.extraction"):
        fetch_html_main_content(
            "https://example.com/missing",
            transport=_html_transport("", status=500),
        )
    assert any(
        "html_failed" in (r.getMessage() or "") or "html_failed" in r.message
        for r in caplog.records
    )


def test_fetch_pdf_text_logs_warning_on_failure(caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="call_manager.sources.extraction"):
        fetch_pdf_text(
            "https://example.com/missing.pdf",
            transport=_pdf_transport(b"", status=500),
        )
    assert any(
        "pdf_failed" in (r.getMessage() or "") or "pdf_failed" in r.message for r in caplog.records
    )


def test_fetch_html_rejects_loopback_url_without_fetching():
    """SSRF guard: a URL resolving to loopback must be refused before any
    connection is opened. The transport raises if it's ever invoked."""

    def boom(_req: httpx.Request) -> httpx.Response:
        raise AssertionError("transport must not be called for an unsafe URL")

    text = fetch_html_main_content(
        "https://127.0.0.1/internal",
        transport=httpx.MockTransport(boom),
    )
    assert text is None


def test_fetch_html_rejects_cloud_metadata_url():
    """SSRF guard: the cloud metadata endpoint (link-local 169.254.169.254)
    is the canonical SSRF target — must be refused."""

    def boom(_req: httpx.Request) -> httpx.Response:
        raise AssertionError("transport must not be called for metadata URL")

    text = fetch_html_main_content(
        "http://169.254.169.254/latest/meta-data/",
        transport=httpx.MockTransport(boom),
    )
    assert text is None


def test_fetch_pdf_rejects_private_ip_url():
    """SSRF guard applies to the PDF path too (it also fetches upstream URLs)."""

    def boom(_req: httpx.Request) -> httpx.Response:
        raise AssertionError("transport must not be called for an unsafe URL")

    text = fetch_pdf_text(
        "https://10.0.0.5/secret.pdf",
        transport=httpx.MockTransport(boom),
    )
    assert text is None


def test_fetch_html_rejects_oversized_body():
    """HTML path now caps body size like the PDF path — a huge page is
    aborted mid-stream and yields None."""
    huge = "<html><body>" + ("x" * (2 * 1024 * 1024)) + "</body></html>"
    text = fetch_html_main_content(
        "https://example.com/huge",
        transport=_html_transport(huge),
        max_bytes=1 * 1024 * 1024,
    )
    assert text is None


def test_fetch_html_does_not_follow_redirects():
    """SSRF DNS-rebinding mitigation: a redirect (e.g. 302 → internal target)
    must NOT be followed. is_safe_fetch_url validated the ORIGINAL host; an
    auto-followed redirect could land on an internal address that was never
    checked. We treat a redirect response as a failure → None."""

    def handler(req: httpx.Request) -> httpx.Response:
        # Redirect the validated public URL to an internal target. If
        # follow_redirects were on, httpx would chase this to 169.254...
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"})

    text = fetch_html_main_content(
        "https://example.com/call",
        transport=httpx.MockTransport(handler),
    )
    assert text is None


def test_fetch_pdf_does_not_follow_redirects():
    """Same redirect SSRF mitigation on the PDF path."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(301, headers={"location": "http://10.0.0.5/secret.pdf"})

    text = fetch_pdf_text(
        "https://example.com/call.pdf",
        transport=httpx.MockTransport(handler),
    )
    assert text is None


def test_fetch_html_main_content_returns_none_on_extractor_exception(caplog):
    """Codex Plan F regression: a Trafilatura/lxml exception during
    extract() must NOT propagate — the helper must degrade to None like
    every other failure path. Otherwise a single pathological page would
    abort the whole ingest run."""
    import logging
    from unittest.mock import patch

    with (
        patch("trafilatura.extract", side_effect=RuntimeError("simulated parser error")),
        caplog.at_level(logging.WARNING, logger="call_manager.sources.extraction"),
    ):
        text = fetch_html_main_content(
            "https://example.com/call",
            transport=_html_transport(_SAMPLE_HTML),
        )
    assert text is None
    assert any(
        "html_failed" in (r.getMessage() or "") or "html_failed" in r.message
        for r in caplog.records
    )
