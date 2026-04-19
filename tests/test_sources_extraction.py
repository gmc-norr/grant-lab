from __future__ import annotations

from pathlib import Path

import httpx

from call_monitor.sources.extraction import (
    fetch_html_main_content,
    fetch_pdf_text,
)

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


def _pdf_transport(payload: bytes, *, status: int = 200,
                   content_type: str = "application/pdf") -> httpx.MockTransport:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=payload, headers={"content-type": content_type})
    return httpx.MockTransport(handler)


def test_fetch_html_main_content_extracts_main_drops_chrome():
    text = fetch_html_main_content(
        "https://example.test/call",
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
        "https://example.test/missing",
        transport=_html_transport("not found", status=404),
    )
    assert text is None


def test_fetch_html_main_content_returns_none_on_empty_body():
    text = fetch_html_main_content(
        "https://example.test/empty",
        transport=_html_transport("<html><body></body></html>"),
    )
    # Trafilatura returns None for empty body
    assert text is None


def test_fetch_pdf_text_extracts_text_from_real_pdf():
    pdf_bytes = Path(__file__).parent.joinpath("fixtures/sample_call.pdf").read_bytes()
    text = fetch_pdf_text(
        "https://example.test/call.pdf",
        transport=_pdf_transport(pdf_bytes),
    )
    assert text is not None
    assert "Cancer Biomarker Discovery" in text
    assert "liquid biopsy" in text


def test_fetch_pdf_text_returns_none_on_404():
    text = fetch_pdf_text(
        "https://example.test/missing.pdf",
        transport=_pdf_transport(b"not found", status=404),
    )
    assert text is None


def test_fetch_pdf_text_returns_none_on_corrupt_pdf():
    text = fetch_pdf_text(
        "https://example.test/garbage.pdf",
        transport=_pdf_transport(b"not actually a pdf"),
    )
    assert text is None


def test_fetch_pdf_text_rejects_oversized_response():
    """Streaming-with-cap: the helper aborts as soon as the running byte
    count exceeds max_bytes — never materializes the full payload."""
    # 2 MB of zeros — much larger than max_bytes=1 MB; streaming should abort.
    huge = b"\x00" * (2 * 1024 * 1024)
    text = fetch_pdf_text(
        "https://example.test/huge.pdf",
        transport=_pdf_transport(huge),
        max_bytes=1 * 1024 * 1024,
    )
    assert text is None


def test_fetch_html_main_content_logs_warning_on_failure(caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger="call_monitor.sources.extraction"):
        fetch_html_main_content(
            "https://example.test/missing",
            transport=_html_transport("", status=500),
        )
    assert any("html_failed" in (r.getMessage() or "") or "html_failed" in r.message
               for r in caplog.records)


def test_fetch_pdf_text_logs_warning_on_failure(caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger="call_monitor.sources.extraction"):
        fetch_pdf_text(
            "https://example.test/missing.pdf",
            transport=_pdf_transport(b"", status=500),
        )
    assert any("pdf_failed" in (r.getMessage() or "") or "pdf_failed" in r.message
               for r in caplog.records)


def test_fetch_html_main_content_returns_none_on_extractor_exception(caplog):
    """Codex Plan F regression: a Trafilatura/lxml exception during
    extract() must NOT propagate — the helper must degrade to None like
    every other failure path. Otherwise a single pathological page would
    abort the whole ingest run."""
    import logging
    from unittest.mock import patch

    with (
        patch("trafilatura.extract", side_effect=RuntimeError("simulated parser error")),
        caplog.at_level(logging.WARNING, logger="call_monitor.sources.extraction"),
    ):
        text = fetch_html_main_content(
            "https://example.test/call",
            transport=_html_transport(_SAMPLE_HTML),
        )
    assert text is None
    assert any("html_failed" in (r.getMessage() or "") or "html_failed" in r.message
               for r in caplog.records)
