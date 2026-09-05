"""TDD tests for extract_reference_metadata (Slice 1B Task 3).

Written before the implementation (TDD red-first):
  src/grant_lab/proposal_assist/references/metadata.py

Covers:
- Fetched crossref with usable source_meta → verified_external; LLM not called.
- Uploaded PDF: heuristic title wins over LLM; LLM author merges in; source "llm".
- Cap exceeded: degrades to heuristic-only; status "failed" for plain text; no raise.
- LLM raises RuntimeError: caught; degrades gracefully; no raise.
- No citation_service + uploaded plain text → heuristic only.
"""

from __future__ import annotations

import io
import uuid

from reportlab.pdfgen.canvas import Canvas

from grant_lab.proposal_assist.citations import CitationLLMResult
from grant_lab.proposal_assist.references.metadata import extract_reference_metadata

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pdf(title: str | None = None, author: str | None = None) -> bytes:
    buf = io.BytesIO()
    c = Canvas(buf)
    if title is not None:
        c.setTitle(title)
    if author is not None:
        c.setAuthor(author)
    c.drawString(72, 720, "Sample reference text")
    c.showPage()
    c.save()
    return buf.getvalue()


class _StubCitationService:
    """Minimal stub that records calls and returns a preset result."""

    def __init__(self, result: CitationLLMResult):
        self._result = result
        self.calls: list[dict] = []

    def complete_json(self, *, workspace_id, op, system, user) -> CitationLLMResult:
        self.calls.append({"workspace_id": workspace_id, "op": op, "system": system, "user": user})
        return self._result


class _RaisingCitationService:
    """Stub whose complete_json raises RuntimeError."""

    def __init__(self):
        self.calls: list[dict] = []

    def complete_json(self, *, workspace_id, op, system, user) -> CitationLLMResult:
        self.calls.append({})
        raise RuntimeError("LLM unavailable")


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


CROSSREF_SOURCE_META = {
    "message": {
        "title": ["Effects of climate change on biodiversity"],
        "author": [{"family": "Smith", "given": "John A."}],
        "issued": {"date-parts": [[2021, 3, 15]]},
        "DOI": "10.1234/example.doi",
        "type": "journal-article",
    }
}


def test_fetched_crossref_uses_provider_metadata_and_skips_llm():
    """Fetched crossref → (csl, 'fetcher', 'verified_external'); LLM not called."""
    stub = _StubCitationService(
        CitationLLMResult(data={"title": "LLM Title"}, usage=None, model="m", cap_exceeded=False)
    )

    csl, source, status, _kind = extract_reference_metadata(
        source_type="crossref",
        source_meta=CROSSREF_SOURCE_META,
        mime_type="application/pdf",
        data=None,
        text="some text",
        workspace_id=uuid.uuid4(),
        citation_service=stub,
    )

    assert source == "fetcher"
    assert status == "verified_external"
    assert csl.get("title") == "Effects of climate change on biodiversity"
    # LLM must NOT have been called
    assert stub.calls == [], "citation_service.complete_json must not be called for fetched refs"


def test_uploaded_pdf_heuristic_title_wins_llm_author_merges():
    """Uploaded PDF: heuristic title beats LLM title; LLM-only author is merged in."""
    pdf_bytes = _make_pdf(title="PDF Title")  # no author in PDF /Info
    llm_result = CitationLLMResult(
        data={"title": "LLM Title", "author": [{"family": "Roe"}]},
        usage=None,
        model="m",
        cap_exceeded=False,
    )
    stub = _StubCitationService(llm_result)

    csl, source, status, _kind = extract_reference_metadata(
        source_type="uploaded",
        source_meta=None,
        mime_type="application/pdf",
        data=pdf_bytes,
        text="Abstract: This paper discusses things.",
        workspace_id=uuid.uuid4(),
        citation_service=stub,
    )

    # Heuristic title wins
    assert csl.get("title") == "PDF Title", f"Expected 'PDF Title', got {csl.get('title')!r}"
    # LLM-provided author merged in
    assert csl.get("author") == [{"family": "Roe"}]
    assert source == "llm"
    assert status == "extracted"
    # LLM was called exactly once
    assert len(stub.calls) == 1
    assert stub.calls[0]["op"] == "metadata_extract"


def test_cap_exceeded_degrades_to_heuristic_no_raise():
    """Cap exceeded → no LLM data; plain-text upload with no heuristic → status 'failed'."""
    cap_result = CitationLLMResult(
        data=None,
        usage=None,
        model=None,
        cap_exceeded=True,
        cap_scope="workspace",
    )
    stub = _StubCitationService(cap_result)

    csl, source, status, _kind = extract_reference_metadata(
        source_type="uploaded",
        source_meta=None,
        mime_type="text/plain",
        data=None,
        text="Some plain text reference",
        workspace_id=uuid.uuid4(),
        citation_service=stub,
    )

    # No raise
    # No LLM data → heuristic only
    assert source == "heuristic"
    assert status == "failed"
    assert csl == {}


def test_llm_raises_caught_degrades_gracefully_no_raise():
    """If complete_json raises, exception is caught; falls back to heuristic; never re-raises."""
    raising_stub = _RaisingCitationService()

    # No exception should propagate out
    csl, source, status, _kind = extract_reference_metadata(
        source_type="uploaded",
        source_meta=None,
        mime_type="text/plain",
        data=None,
        text="Reference text that would be passed to LLM",
        workspace_id=uuid.uuid4(),
        citation_service=raising_stub,
    )

    # Still called (attempt was made)
    assert len(raising_stub.calls) == 1
    # Degraded gracefully
    assert source == "heuristic"
    assert status == "failed"
    assert isinstance(csl, dict)


def test_no_citation_service_uploaded_plain_text_heuristic_only():
    """No citation_service and uploaded plain text → heuristic-only, no error."""
    csl, source, status, _kind = extract_reference_metadata(
        source_type="uploaded",
        source_meta=None,
        mime_type="text/plain",
        data=None,
        text="Plain text body",
        workspace_id=uuid.uuid4(),
        citation_service=None,
    )

    assert source == "heuristic"
    assert status == "failed"
    assert csl == {}


def test_llm_title_as_list_degrades():
    """CitationLLMResult with title as a list (non-str) must not crash; the
    extractor stores it as-is (the ingest layer separately guards str type).
    Source must be 'llm', status 'extracted'."""
    llm_result = CitationLLMResult(
        data={"title": ["Array Title"], "author": [{"family": "Roe"}]},
        usage=None,
        model="m",
        cap_exceeded=False,
    )
    stub = _StubCitationService(llm_result)

    csl, source, status, _kind = extract_reference_metadata(
        source_type="uploaded",
        source_meta=None,
        mime_type="text/plain",
        data=None,
        text="Some reference text",
        workspace_id=uuid.uuid4(),
        citation_service=stub,
    )

    # extractor must not crash and must store whatever the LLM returned
    assert csl.get("title") == ["Array Title"]
    assert source == "llm"
    assert status == "extracted"


def test_heuristic_only_when_no_text():
    """Uploaded PDF with text=None: LLM must not be called; result uses PDF
    heuristic (metadata_source='heuristic', title from /Info, status='extracted')."""
    pdf_bytes = _make_pdf(title="PDF Only")
    stub = _StubCitationService(
        CitationLLMResult(
            data={"title": "LLM Title"},
            usage=None,
            model="m",
            cap_exceeded=False,
        )
    )

    csl, source, status, _kind = extract_reference_metadata(
        source_type="uploaded",
        source_meta=None,
        mime_type="application/pdf",
        data=pdf_bytes,
        text=None,
        workspace_id=uuid.uuid4(),
        citation_service=stub,
    )

    # LLM must NOT have been called because text is None
    assert stub.calls == [], "LLM must not be called when text is None"
    assert source == "heuristic"
    assert status == "extracted"
    assert csl.get("title") == "PDF Only"


# ---------------------------------------------------------------------------
# Task 4: _kind sentinel tests
# ---------------------------------------------------------------------------


class _Result:
    def __init__(self, data):
        self.data = data
        self.cap_exceeded = False
        self.cap_scope = None


class _Svc:
    def __init__(self, data):
        self._data = data

    def complete_json(self, *, workspace_id, op, system, user):
        # Hand back a fresh copy each call: extract_reference_metadata pops
        # "_kind" off the returned dict, which would otherwise mutate this
        # stub's stored data and break a second call against the same stub.
        return _Result(dict(self._data))


def test_kind_popped_and_returned_not_in_csl():
    svc = _Svc({"title": "A Paper", "_kind": "data"})
    csl, source, status, kind = extract_reference_metadata(
        source_type="uploaded",
        source_meta=None,
        mime_type="application/pdf",
        data=b"%PDF-1.4",
        text="some text",
        workspace_id=uuid.uuid4(),
        citation_service=svc,
    )
    assert kind == "data"
    assert "_kind" not in csl
    assert csl.get("title") == "A Paper"
    assert status == "extracted"


def test_invalid_kind_returns_none():
    svc = _Svc({"title": "X", "_kind": "nonsense"})
    _csl, _src, _st, kind = extract_reference_metadata(
        source_type="uploaded",
        source_meta=None,
        mime_type="application/pdf",
        data=b"%PDF-1.4",
        text="t",
        workspace_id=uuid.uuid4(),
        citation_service=svc,
    )
    assert kind is None


def test_no_llm_returns_kind_none():
    _csl, _src, _st, kind = extract_reference_metadata(
        source_type="uploaded",
        source_meta=None,
        mime_type="text/plain",
        data=b"hello",
        text="t",
        workspace_id=uuid.uuid4(),
        citation_service=None,
    )
    assert kind is None
