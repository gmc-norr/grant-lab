# Full-text ingestion (Plan F)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Spec:** [2026-04-19-full-text-ingestion-design.md](../specs/2026-04-19-full-text-ingestion-design.md)
**Depends on:** Plan E merged. `master` HEAD `bf07d1b` or later. 430 tests passing baseline.

**Goal:** Populate `Call.full_text` for both source adapters (Vinnova: refactor; EU portal: investigate then API/scrape). Wire it through to the LLM rubric (currently OR-picks `summary` or `full_text` — change to concatenate, bump `PROMPT_VERSION`). Build a shared HTML/PDF extraction toolkit so future adapters can populate `full_text` in 5 lines. Preserve good `full_text` against transient scrape failures via COALESCE in upsert.

**Architecture:** API-first per source, scrape-fallback only when API is insufficient. Adapter decides per source — never auto-scrape from the orchestrator. Two new dependencies: PyMuPDF (PDF) and Trafilatura (HTML). One new shared module: `sources/extraction.py`. Two existing files get prompt-version-bumping changes (`prompt.py`, `upsert.py`). Two new CLIs: `ingestion.cli run --source <slug>` for forced re-ingest, `--force` flag on the existing backfill CLI for forced re-embed.

**Tech Stack:** Python 3.12 · FastAPI + Jinja2 + HTMX · PostgreSQL 16 + pgvector + SQLAlchemy 2.x + Alembic · `pymupdf` (NEW, AGPL-3.0) · `trafilatura` (NEW, Apache-2.0) · `pytest` + `testcontainers[postgres]` · `httpx.MockTransport` for HTML/PDF stubbing

---

## Design decisions (fix-now; push back if you disagree)

All decisions are committed in the spec. Plan F implements them. Briefly:

- Adapter calls scraping helpers explicitly when its API is insufficient; orchestrator does NOT auto-scrape
- Synchronous fetching inside `adapter.fetch()` (preserves the 3-step orchestrator transaction split)
- PyMuPDF for PDF (AGPL accepted for internal use), Trafilatura for HTML
- `NormalizedCall.full_text: str | None` — already exists in `sources/types.py:42`, just adopt it
- `Call.full_text` preserved on upsert via `COALESCE(NEW, OLD)` so transient scrape failures don't clobber good data
- Prompt: change `summary OR full_text` → `summary + full_text` concatenated, bump `CALL_TEXT_LIMIT` 4000 → 16000, bump `PROMPT_VERSION` `"v2.0"` → `"v3.0"`
- New `python -m call_monitor.ingestion.cli run --source <slug>` for force-ingest (deploy procedure needs this)
- New `--force` flag on `python -m call_monitor.embeddings.cli backfill` for re-embed-all
- Document the adapter-authoring pattern in `sources/__init__.py`
- No schema changes (Call.full_text column exists; other prerequisites in place)

---

## File structure created or modified by this plan

```
NEW:
  src/call_monitor/sources/extraction.py            # fetch_html_main_content + fetch_pdf_text helpers
  src/call_monitor/ingestion/cli.py                 # python -m call_monitor.ingestion.cli run --source <slug>
  tests/test_sources_extraction.py                  # NEW
  tests/test_ingestion_cli.py                       # NEW
  tests/fixtures/sample_call.pdf                    # small generated PDF for tests

MODIFIED:
  pyproject.toml                                    # add pymupdf, trafilatura
  src/call_monitor/sources/__init__.py              # adapter-authoring docstring (the decision tree)
  src/call_monitor/sources/vinnova.py               # split Beskrivning → summary; WebTextLista → full_text
  src/call_monitor/sources/eu_portal.py             # use richer API field if present (Task 5 investigation), else HTML scrape
  src/call_monitor/ingestion/upsert.py              # COALESCE preservation for full_text
  src/call_monitor/ranking/prompt.py                # concat summary+full_text, CALL_TEXT_LIMIT 4000→16000, PROMPT_VERSION v2.0→v3.0
  src/call_monitor/embeddings/cli.py                # add --force flag
  src/call_monitor/embeddings/refresh.py            # extend refresh_missing_embeddings_tick(force=False)

MODIFIED tests:
  tests/test_sources_vinnova.py                     # extend: assert summary brief + full_text rich
  tests/test_sources_eu_portal.py                   # extend: assert full_text populated
  tests/test_ingestion_orchestrator.py              # extend: confirm preserve-on-None upsert
  tests/test_ranking_prompt.py                      # extend: summary+full_text concat assertions; PROMPT_VERSION assertion
  tests/test_embeddings_cli.py                      # extend: --force flag
  tests/test_embeddings_refresh.py                  # extend: force=True path
```

No new Alembic migration. No schema changes.

---

## Task 1: Add `pymupdf` + `trafilatura` dependencies

**Files:**
- Modify: `pyproject.toml`

This is a pure dependency-add task. Verify the environment can install both, then commit.

- [ ] **Step 1: Add to `pyproject.toml` `[project.dependencies]`**

```toml
"pymupdf>=1.25.0",
"trafilatura>=2.0.0",
```

(Use the latest stable major versions. Verify against PyPI before committing — these are illustrative.)

- [ ] **Step 2: `uv sync`**

Verify both wheels install cleanly. PyMuPDF ships precompiled wheels for x86_64 Linux. Trafilatura pulls in `lxml`, `htmldate`, `justext`, `dateparser` — confirm none fail to build from source.

- [ ] **Step 3: Smoke-import**

```bash
uv run python -c "import pymupdf; import trafilatura; print('OK')"
```

Expected: `OK`. Any ImportError → stop, fix env, don't commit.

- [ ] **Step 4: Verify and commit**

```bash
uv run pytest -q     # baseline still 430 — adding deps shouldn't break anything
uv run ruff check .
git add pyproject.toml uv.lock
git commit -m "chore: add pymupdf + trafilatura for full-text extraction"
```

Expected: 430 passing.

---

## Task 2: Build `sources/extraction.py` — HTML + PDF helpers

**Files:**
- Create: `src/call_monitor/sources/extraction.py`
- Create: `tests/test_sources_extraction.py`
- Create: `tests/fixtures/sample_call.pdf`

Two helpers, both best-effort, logged-on-failure, return `str | None`. Signatures match the spec §3.5 exactly.

- [ ] **Step 1: Generate a small test PDF fixture**

Use a one-off Python script (run locally; commit the output bytes):

```python
# scripts/generate_sample_pdf.py — NOT committed; run once.
import pymupdf
doc = pymupdf.open()
page = doc.new_page()
page.insert_text(
    (50, 72),
    "Sample Call: Cancer Biomarker Discovery Funding 2026\n\n"
    "This call funds research into novel biomarkers for early-stage\n"
    "cancer detection, with a focus on liquid biopsy approaches.\n"
    "Eligible: clinical research groups at EU/EEA institutions.\n"
    "Budget: 500K EUR per project, 24 months max.\n",
)
doc.save("tests/fixtures/sample_call.pdf")
doc.close()
```

Run once, commit the resulting PDF binary. ~2-4 KB.

- [ ] **Step 2: Test `tests/test_sources_extraction.py`**

```python
from __future__ import annotations

from pathlib import Path

import httpx
import pytest

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
```

- [ ] **Step 3: Run test to verify it fails**

```bash
uv run pytest tests/test_sources_extraction.py -v
```

Expected: ImportError (`call_monitor.sources.extraction` doesn't exist yet).

- [ ] **Step 4: Implement `src/call_monitor/sources/extraction.py`**

```python
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

    extracted = trafilatura.extract(html)  # returns str | None
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
        with httpx.Client(transport=transport, timeout=timeout) as client:
            with client.stream("GET", url) as resp:
                resp.raise_for_status()
                for chunk in resp.iter_bytes():
                    buf.extend(chunk)
                    if len(buf) > max_bytes:
                        _logger.warning(
                            "extraction.pdf_failed",
                            extra={
                                "event": "pdf_failed", "url": url,
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
```

- [ ] **Step 5: Verify and commit**

```bash
uv run pytest tests/test_sources_extraction.py -v
uv run pytest -q
uv run ruff check .
git add src/call_monitor/sources/extraction.py tests/test_sources_extraction.py tests/fixtures/sample_call.pdf
git commit -m "feat: sources/extraction.py — HTML + PDF helpers (Trafilatura + PyMuPDF)"
```

Expected: 8 new tests pass, suite total 438 (430 + 8).

---

## Task 3: COALESCE preservation in `upsert_call`

**Files:**
- Modify: `src/call_monitor/ingestion/upsert.py`
- Modify: `tests/test_ingestion_orchestrator.py` (or wherever `upsert_call` is tested directly)

When a transient scrape failure causes the adapter to return `full_text=None` for a call that previously had body text, the upsert must NOT erase the existing value.

- [ ] **Step 1: Failing test**

In `tests/test_ingestion_orchestrator.py` (or a more focused unit test if one exists), append:

```python
def test_upsert_preserves_full_text_when_new_payload_is_none(pg_session_factory):
    """Codex H3 preservation: a transient scrape failure (full_text=None in
    the new payload) must NOT overwrite previously-captured body text on
    the existing row."""
    from datetime import UTC, datetime
    from sqlalchemy import select

    from call_monitor.db.models import Call, Source
    from call_monitor.ingestion.upsert import upsert_call
    from call_monitor.sources.types import NormalizedCall

    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        # Initial insert with rich full_text
        nc1 = NormalizedCall(
            external_id="preserve-1",
            url="https://example.test/p1",
            title="P1",
            summary="brief",
            full_text="This is the rich body of the call.",
            deadline=None, budget_min=None, budget_max=None, currency=None,
            programme=None, keywords=[], published_at=datetime.now(UTC),
            raw={},
        )
        upsert_call(s, src.id, nc1)
        s.commit()

        # Re-upsert with full_text=None (simulates scrape failure on next ingest)
        nc2 = NormalizedCall(
            external_id="preserve-1",
            url="https://example.test/p1",
            title="P1 updated title",
            summary="still brief",
            full_text=None,  # transient failure
            deadline=None, budget_min=None, budget_max=None, currency=None,
            programme=None, keywords=[], published_at=datetime.now(UTC),
            raw={},
        )
        upsert_call(s, src.id, nc2)
        s.commit()

        # Existing body MUST be preserved.
        c = s.execute(
            select(Call).where(Call.source_id == src.id, Call.external_id == "preserve-1")
        ).scalar_one()
        assert c.full_text == "This is the rich body of the call."
        # Other fields DID update.
        assert c.title == "P1 updated title"
        assert c.summary == "still brief"


def test_upsert_overwrites_full_text_when_new_payload_is_non_none(pg_session_factory):
    """The preservation only kicks in for None — a non-None new value
    (including a deliberate 'body changed' update) DOES overwrite."""
    from datetime import UTC, datetime
    from sqlalchemy import select
    from call_monitor.db.models import Call, Source
    from call_monitor.ingestion.upsert import upsert_call
    from call_monitor.sources.types import NormalizedCall

    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        upsert_call(s, src.id, NormalizedCall(
            external_id="overwrite-1", url="https://example.test/o1",
            title="T", summary=None, full_text="OLD body",
            deadline=None, budget_min=None, budget_max=None, currency=None,
            programme=None, keywords=[], published_at=datetime.now(UTC), raw={},
        ))
        s.commit()
        upsert_call(s, src.id, NormalizedCall(
            external_id="overwrite-1", url="https://example.test/o1",
            title="T", summary=None, full_text="NEW body",
            deadline=None, budget_min=None, budget_max=None, currency=None,
            programme=None, keywords=[], published_at=datetime.now(UTC), raw={},
        ))
        s.commit()
        c = s.execute(
            select(Call).where(Call.source_id == src.id, Call.external_id == "overwrite-1")
        ).scalar_one()
        assert c.full_text == "NEW body"
```

- [ ] **Step 2: Update `src/call_monitor/ingestion/upsert.py`**

In the `update_cols` dict construction, replace the bulk dict-comprehension with explicit handling of `full_text`:

```python
update_cols = {
    c: stmt.excluded[c] for c in payload if c not in {"source_id", "external_id"}
}
# Preserve existing full_text on transient scrape failures (None in new payload).
# Same intent as worker.py's embed_similarity preservation: don't clobber good
# diagnostic/content data with NULL just because one fetch went bad.
update_cols["full_text"] = func.coalesce(stmt.excluded.full_text, Call.full_text)
update_cols["fetched_at"] = func.now()
```

- [ ] **Step 3: Verify and commit**

```bash
uv run pytest tests/test_ingestion_orchestrator.py -v
uv run pytest -q
uv run ruff check .
git add src/call_monitor/ingestion/upsert.py tests/test_ingestion_orchestrator.py
git commit -m "fix: upsert preserves full_text on transient scrape failure (COALESCE)"
```

Expected: 2 new tests pass, suite total 440.

---

## Task 4: Refactor Vinnova adapter — split summary / full_text

**Files:**
- Modify: `src/call_monitor/sources/vinnova.py`
- Modify: `tests/test_sources_vinnova.py`

Today: `summary` is `Beskrivning + WebTextLista[*].TextSv + BeskrivningEngelska` concatenated. After Task 4: `summary` is just `Beskrivning` (and English variant if distinct), `full_text` is the WebTextLista concatenation. **Same payload, different field assignment** — no new HTTP requests.

- [ ] **Step 1: Failing test — extend `tests/test_sources_vinnova.py`**

Find an existing `test_*` that constructs an item with `Beskrivning` + `WebTextLista` and asserts on the resulting `summary`. Update its assertions:

```python
def test_vinnova_adapter_splits_summary_and_full_text():
    """After Plan F: Beskrivning → summary; WebTextLista[*].TextSv → full_text."""
    item = {
        "Diarienummer": "TEST-123",
        "Titel": "Test programme",
        "Beskrivning": "A brief description.",
        "BeskrivningEngelska": "A brief description in English.",
        "WebTextLista": [
            {"TextSv": "First substantive paragraph about the call."},
            {"TextSv": "Second paragraph with more detail and eligibility."},
        ],
        "Stangningsdatum": "2026-12-31",
        "Oppningsdatum": "2026-04-01",
        "Publik": 1, "Webbsida": 1,
        "LankLista": [{"URL": "https://vinnova.se/test"}],
    }

    # Adapter logic — call _normalize_round or whatever the entry point is.
    # If the test pattern uses a full adapter.fetch() with a mocked client,
    # adapt accordingly.
    nc = _normalize_round_for_test(item)  # adapt to actual helper

    # summary now contains ONLY the brief Beskrivning (and English variant)
    assert "A brief description." in nc.summary
    assert "First substantive paragraph" not in nc.summary

    # full_text contains the WebTextLista paragraphs
    assert nc.full_text is not None
    assert "First substantive paragraph about the call." in nc.full_text
    assert "Second paragraph with more detail" in nc.full_text


def test_vinnova_adapter_full_text_none_when_no_webtextlista():
    """If the source provides no WebTextLista, full_text is None (not empty string)."""
    item = {
        "Diarienummer": "TEST-456",
        "Titel": "Test programme",
        "Beskrivning": "Just a description.",
        "WebTextLista": [],  # or missing entirely
        "Stangningsdatum": "2026-12-31",
        "Oppningsdatum": "2026-04-01",
        "Publik": 1, "Webbsida": 1,
        "LankLista": [{"URL": "https://vinnova.se/test"}],
    }
    nc = _normalize_round_for_test(item)
    assert nc.summary == "Just a description."
    assert nc.full_text is None
```

(If existing tests use the full `VinnovaAdapter.fetch()` flow with a mocked HTTP client, adapt accordingly. The assertion intent is what matters: summary is brief, full_text has the body.)

- [ ] **Step 2: Update `src/call_monitor/sources/vinnova.py`**

Refactor the existing `_build_summary` helper, OR introduce a sibling `_build_full_text` helper:

```python
def _build_summary(item: dict) -> str | None:
    """The brief description fields. Body text lives in full_text (Plan F).

    Vinnova exposes Beskrivning (40-70 chars) and BeskrivningEngelska
    as the call's headline-level descriptions. The substantive body
    paragraphs are in WebTextLista — those go into full_text.
    """
    parts: list[str] = []
    beskr = item.get("Beskrivning")
    if isinstance(beskr, str) and beskr.strip():
        parts.append(beskr.strip())
    eng = item.get("BeskrivningEngelska")
    if isinstance(eng, str) and eng.strip() and eng.strip() != (beskr or "").strip():
        parts.append(eng.strip())
    return "\n\n".join(parts) if parts else None


def _build_full_text(item: dict) -> str | None:
    """The substantive call body — WebTextLista[*].TextSv concatenated.

    Vinnova returns the full call prose under WebTextLista; each entry
    is one section of the call. Joining them with blank-line separators
    preserves section boundaries for both the embedding and the LLM rubric.

    Returns None when the source has no WebTextLista entries (rare —
    most active programmes carry at least one).
    """
    parts: list[str] = []
    for entry in item.get("WebTextLista") or []:
        if not isinstance(entry, dict):
            continue
        text = entry.get("TextSv")
        if isinstance(text, str) and text.strip():
            parts.append(text.strip())
    return "\n\n".join(parts) if parts else None
```

In the `_normalize_round` (or equivalent) function, set both:

```python
return NormalizedCall(
    external_id=str(external_id),
    url=url,
    title=str(title),
    summary=_build_summary(item),
    full_text=_build_full_text(item),    # NEW
    deadline=deadline,
    # ... other fields unchanged ...
    raw=item,
)
```

Update the docstring on `VinnovaAdapter` to note: "API returns full call body in `WebTextLista[*].TextSv`; populated into `full_text`. No scraping needed."

- [ ] **Step 3: Verify and commit**

```bash
uv run pytest tests/test_sources_vinnova.py -v
uv run pytest -q
uv run ruff check .
git add src/call_monitor/sources/vinnova.py tests/test_sources_vinnova.py
git commit -m "refactor: vinnova adapter — split Beskrivning → summary, WebTextLista → full_text"
```

Expected: 2 new tests pass + existing Vinnova tests adapted, suite total ~442.

---

## Task 5: EU portal — investigate API, then update adapter

**Files:**
- Modify: `src/call_monitor/sources/eu_portal.py`
- Modify: `tests/test_sources_eu_portal.py`
- POSSIBLY: nothing more if a richer API field exists

This task has an investigation step that gates the implementation. The plan supports both outcomes.

### Step 1: INVESTIGATE the EU portal API

Capture a real EU portal item from the search API in dev and inventory the available fields:

```bash
# Find a recent EU portal call URL or topic ID, hit the search API directly:
curl -s 'https://ec.europa.eu/info/funding-tenders/opportunities/data/topicDetails/<topic_id>.json' \
    | jq 'keys'
# OR via the search API the adapter already uses, capture one full item:
curl -s '...' | jq '.results[0]'
```

(The exact URL pattern depends on what `eu_portal.py` already calls. Read the adapter to find the endpoint, then probe the same endpoint manually with `curl + jq`.)

Document the findings inline in `eu_portal.py` as a comment block. Specifically:

- List every string-valued field in the response.
- Identify candidates for body content beyond `summary`: `description`, `topicDescription`, `objectives`, `expectedImpact`, `additionalInfo`, `callConditions`, `eligibilityConditions`, `topicConditions`, etc.
- Decide: do these collectively give us a meaningful body? Or is the API genuinely thin?

### Step 2: Implement based on the investigation outcome

**Outcome A — rich API fields exist:**

```python
def _build_full_text(item: dict) -> str | None:
    """EU portal exposes the call body across multiple fields. Concatenate
    the relevant rich ones in a stable order.

    Per investigation 2026-04-19: the search API returns <list the fields you found>.
    """
    parts: list[str] = []
    for field in ("description", "objectives", "expectedImpact", "additionalInfo"):
        v = item.get(field)
        if isinstance(v, str) and v.strip():
            parts.append(v.strip())
    return "\n\n".join(parts) if parts else None
```

In `_normalize`:
```python
return NormalizedCall(
    # ... existing fields ...
    summary=item.get("summary"),
    full_text=_build_full_text(item),   # NEW
    # ...
)
```

**Outcome B — API is genuinely thin:**

The adapter must scrape the call's HTML page. Extend `EUPortalAdapter.__init__` to accept an optional `extraction_transport` kwarg for tests, then in `_normalize`:

```python
from call_monitor.sources.extraction import fetch_html_main_content

# Inside _normalize, after constructing summary/title/url:
full_text = fetch_html_main_content(url, transport=self._extraction_transport)
return NormalizedCall(
    # ... existing fields ...
    summary=item.get("summary"),
    full_text=full_text,
    # ...
)
```

Add a docstring on the adapter: `"EU portal's search API returns only a brief summary; full call body is on the topic-detail HTML page. We scrape via fetch_html_main_content."`

### Step 3: Tests

Either outcome:

```python
def test_eu_portal_adapter_populates_full_text():
    """After Plan F: full_text is populated from <API field | HTML scrape>."""
    # If outcome A: pass an item with the rich field set, assert full_text contains it.
    # If outcome B: use httpx.MockTransport to return a canned HTML page,
    #               pass it to the adapter via extraction_transport,
    #               assert full_text is the extracted main content.


def test_eu_portal_adapter_full_text_none_on_failure():
    """If outcome A: missing fields → None. If outcome B: scrape failure → None."""
    # Cover the failure path explicitly.
```

### Step 4: Verify and commit

```bash
uv run pytest tests/test_sources_eu_portal.py -v
uv run pytest -q
uv run ruff check .
git add src/call_monitor/sources/eu_portal.py tests/test_sources_eu_portal.py
git commit -m "feat: eu portal adapter populates full_text (<API outcome A | HTML scrape outcome B>)"
```

Expected: 2 new tests pass, suite total ~444. The commit message should reflect the actual outcome chosen.

---

## Task 6: Prompt change — concatenate, bump truncation, bump PROMPT_VERSION

**Files:**
- Modify: `src/call_monitor/ranking/prompt.py`
- Modify: `tests/test_ranking_prompt.py`

Three coupled changes (per spec §3.7) that have to land together.

- [ ] **Step 1: Failing tests — extend `tests/test_ranking_prompt.py`**

```python
def test_build_messages_concatenates_summary_and_full_text():
    """Plan F prompt change: summary AND full_text both reach the LLM,
    not just one or the other (was: OR-pick)."""
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from call_monitor.ranking.prompt import build_messages

    profile = SimpleNamespace(
        name="Cancer Research", description="cancer biomarkers",
        kind=SimpleNamespace(value="group"),
        structured={"areas": [], "hard_exclusions": []},
    )
    call = SimpleNamespace(
        title="A relevant call",
        summary="Brief abstract.",
        full_text="Substantive body paragraph 1.\n\nSubstantive body paragraph 2.",
        deadline=datetime.now(UTC) + timedelta(days=30),
        budget_min=None, budget_max=None, currency=None, programme=None,
    )
    messages = build_messages(profile=profile, call=call)
    user_text = messages[1]["content"]
    # Both reach the LLM
    assert "Brief abstract." in user_text
    assert "Substantive body paragraph 1." in user_text
    assert "Substantive body paragraph 2." in user_text


def test_build_messages_uses_summary_only_when_full_text_is_none():
    """Backwards-compat: when full_text is None, prompt still uses summary alone."""
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from call_monitor.ranking.prompt import build_messages

    profile = SimpleNamespace(
        name="P", description="d", kind=SimpleNamespace(value="group"),
        structured={"areas": [], "hard_exclusions": []},
    )
    call = SimpleNamespace(
        title="t", summary="only summary here", full_text=None,
        deadline=datetime.now(UTC) + timedelta(days=30),
        budget_min=None, budget_max=None, currency=None, programme=None,
    )
    messages = build_messages(profile=profile, call=call)
    assert "only summary here" in messages[1]["content"]


def test_build_messages_uses_full_text_only_when_summary_is_none():
    """And vice versa — full_text alone when summary is None."""
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from call_monitor.ranking.prompt import build_messages

    profile = SimpleNamespace(
        name="P", description="d", kind=SimpleNamespace(value="group"),
        structured={"areas": [], "hard_exclusions": []},
    )
    call = SimpleNamespace(
        title="t", summary=None, full_text="only the body",
        deadline=datetime.now(UTC) + timedelta(days=30),
        budget_min=None, budget_max=None, currency=None, programme=None,
    )
    messages = build_messages(profile=profile, call=call)
    assert "only the body" in messages[1]["content"]


def test_build_messages_truncates_at_call_text_limit():
    """CALL_TEXT_LIMIT bumped to 16000 — verify the cap.

    Note: the cap is on the COMBINED summary+full_text after concat. To make
    the assertion crisp, we leave summary=None so the entire budget is
    available for the body of x's.
    """
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from call_monitor.ranking.prompt import build_messages, CALL_TEXT_LIMIT

    assert CALL_TEXT_LIMIT == 16000

    profile = SimpleNamespace(
        name="P", description="d", kind=SimpleNamespace(value="group"),
        structured={"areas": [], "hard_exclusions": []},
    )
    huge_full_text = "x" * 30_000
    call = SimpleNamespace(
        title="t", summary=None, full_text=huge_full_text,
        deadline=datetime.now(UTC) + timedelta(days=30),
        budget_min=None, budget_max=None, currency=None, programme=None,
    )
    messages = build_messages(profile=profile, call=call)
    user_text = messages[1]["content"]
    # The body section should be exactly CALL_TEXT_LIMIT chars of x.
    assert "x" * CALL_TEXT_LIMIT in user_text
    assert "x" * (CALL_TEXT_LIMIT + 1) not in user_text


def test_build_messages_truncates_combined_summary_plus_full_text():
    """When BOTH summary and full_text are provided, the truncation cap
    applies to their CONCATENATION (summary + '\\n\\n' + full_text), not
    to full_text alone. The available x-budget is reduced by the summary
    + separator length."""
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from call_monitor.ranking.prompt import build_messages, CALL_TEXT_LIMIT

    profile = SimpleNamespace(
        name="P", description="d", kind=SimpleNamespace(value="group"),
        structured={"areas": [], "hard_exclusions": []},
    )
    summary = "Short abstract."
    huge_full_text = "x" * 30_000
    call = SimpleNamespace(
        title="t", summary=summary, full_text=huge_full_text,
        deadline=datetime.now(UTC) + timedelta(days=30),
        budget_min=None, budget_max=None, currency=None, programme=None,
    )
    messages = build_messages(profile=profile, call=call)
    user_text = messages[1]["content"]
    # Summary appears verbatim
    assert summary in user_text
    # Body x-run is shorter than CALL_TEXT_LIMIT by exactly len(summary) + 2
    # (the "\n\n" separator). Verify the budget accounting.
    expected_body_len = CALL_TEXT_LIMIT - len(summary) - 2
    assert "x" * expected_body_len in user_text
    assert "x" * (expected_body_len + 1) not in user_text


def test_prompt_version_bumped_to_v3():
    """The prompt's input shape changed — bump PROMPT_VERSION for audit/A-B."""
    from call_monitor.ranking.prompt import PROMPT_VERSION
    assert PROMPT_VERSION == "v3.0"
```

- [ ] **Step 2: Update `src/call_monitor/ranking/prompt.py`**

Three changes in one commit:

1. `PROMPT_VERSION = "v3.0"` (was `"v2.0"`)
2. `CALL_TEXT_LIMIT = 16000` (was `4000`)
3. Replace the `text_source` construction at lines ~96-100:

```python
# Before:
text_source = (
    call.summary
    or getattr(call, "full_text", None)
    or ""
)

# After:
parts: list[str] = []
if call.summary:
    parts.append(call.summary)
full_text = getattr(call, "full_text", None)
if full_text:
    parts.append(full_text)
text_source = "\n\n".join(parts) if parts else "(no text)"
if len(text_source) > CALL_TEXT_LIMIT:
    text_source = text_source[:CALL_TEXT_LIMIT]
```

- [ ] **Step 3: Verify and commit**

```bash
uv run pytest tests/test_ranking_prompt.py -v
uv run pytest -q
uv run ruff check .
git add src/call_monitor/ranking/prompt.py tests/test_ranking_prompt.py
git commit -m "feat: prompt concatenates summary+full_text; CALL_TEXT_LIMIT 4000→16000; PROMPT_VERSION v3.0"
```

Expected: 5 new tests pass + any existing prompt tests adapted, suite total ~449.

**Note:** any existing test that asserts `PROMPT_VERSION == "v2.0"` needs to be updated. Same for any test that asserts `CALL_TEXT_LIMIT == 4000`. Same for tests asserting on the old text_source shape. Audit `tests/test_ranking_prompt.py` and `tests/test_ranking_worker.py` (the worker writes prompt_version to CallRanking rows).

---

## Task 7: `--force` flag on backfill CLI

**Files:**
- Modify: `src/call_monitor/embeddings/cli.py`
- Modify: `src/call_monitor/embeddings/refresh.py`
- Modify: `tests/test_embeddings_cli.py`
- Modify: `tests/test_embeddings_refresh.py`

The deploy procedure needs a way to re-embed every row, not just NULL/mismatched ones. Today the tick query filters by `embedding IS NULL OR embedding_model != current_model`. Add a `force=False` parameter that drops that filter when True.

- [ ] **Step 1: Failing tests**

In `tests/test_embeddings_refresh.py`:

```python
def test_refresh_missing_tick_force_true_re_embeds_already_embedded_rows(pg_session_factory):
    """force=True ignores the NULL/mismatched filter — re-embeds everything."""
    with pg_session_factory() as s:
        cid_already = _seed_call(s, title="Already")
        s.flush()
        s.get(Call, cid_already).embedding = [0.5] * 1024
        s.get(Call, cid_already).embedding_model = "bge-m3"  # current model, not stale
        s.commit()

    client = _fake_client(vec=[0.9] * 1024)
    summary = refresh_missing_embeddings_tick(
        session_factory=pg_session_factory, embed_client=client, batch_size=200,
        force=True,
    )
    # The already-embedded row IS re-embedded under force=True
    assert summary["calls_embedded"] == 1
    with pg_session_factory() as s:
        c = s.get(Call, cid_already)
        # Verify the value was overwritten with the new vector
        # (pgvector returns numpy; convert to list for comparison)
        assert list(c.embedding) == [0.9] * 1024


def test_refresh_missing_tick_force_false_default_skips_already_embedded(pg_session_factory):
    """Default behavior unchanged — force=False keeps the NULL/mismatched filter."""
    with pg_session_factory() as s:
        cid_already = _seed_call(s, title="StillAlready")
        s.flush()
        s.get(Call, cid_already).embedding = [0.5] * 1024
        s.get(Call, cid_already).embedding_model = "bge-m3"
        s.commit()

    client = _fake_client(vec=[0.9] * 1024)
    client.embed.side_effect = AssertionError("should not be called for already-embedded row")
    summary = refresh_missing_embeddings_tick(
        session_factory=pg_session_factory, embed_client=client, batch_size=200,
    )
    assert summary["calls_embedded"] == 0
```

In `tests/test_embeddings_cli.py`:

```python
def test_cli_backfill_force_flag_re_embeds_already_embedded_rows(pg_session_factory):
    """`backfill --force` re-embeds everything regardless of NULL/mismatched."""
    from unittest.mock import MagicMock, patch
    from sqlalchemy import select
    from datetime import UTC, datetime
    from call_monitor.db.models import Call, Source

    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        s.add(Call(
            source_id=src.id, external_id="force-1", url="https://x", title="T",
            published_at=datetime.now(UTC),
            embedding=[0.1] * 1024, embedding_model="bge-m3",
        ))
        s.commit()

    fake = MagicMock()
    fake.model = "bge-m3"
    fake.embed.return_value = [0.7] * 1024

    with patch("call_monitor.embeddings.cli._build_embed_client", return_value=fake):
        from call_monitor.embeddings.cli import backfill_main
        exit_code = backfill_main(
            argv=["backfill", "--force"],
            session_factory=pg_session_factory,
        )
    assert exit_code == 0
    assert fake.embed.call_count >= 1  # the already-embedded row was re-embedded
```

- [ ] **Step 2: Update `src/call_monitor/embeddings/refresh.py`**

Add `force: bool = False` to `refresh_missing_embeddings_tick`. When True, drop the NULL/mismatched filter:

```python
def refresh_missing_embeddings_tick(
    *,
    session_factory,
    embed_client: "OllamaEmbedClient",
    batch_size: int = 200,
    force: bool = False,
) -> dict[str, int]:
    """... existing docstring ...

    `force=True` (Plan F) re-embeds every row regardless of NULL/mismatched
    state. Used by the backfill CLI's `--force` flag for one-time post-deploy
    refreshes (e.g., after the prompt or text-extraction layer changes the
    content the embeddings were computed against).
    """
    current_model = embed_client.model
    summary = {"calls_embedded": 0, "calls_failed": 0,
               "profiles_embedded": 0, "profiles_failed": 0}

    # Build the WHERE clause based on force.
    def _call_filter():
        if force:
            return None  # no filter
        return or_(Call.embedding.is_(None), Call.embedding_model != current_model)

    def _profile_filter():
        if force:
            # Still respect the "blank-description profiles are not selectable" rule
            return func.length(func.trim(Profile.description)) > 0
        return and_(
            or_(Profile.embedding.is_(None), Profile.embedding_model != current_model),
            func.length(func.trim(Profile.description)) > 0,
        )

    with session_factory() as session:
        q = select(Call.id)
        cf = _call_filter()
        if cf is not None:
            q = q.where(cf)
        q = q.where(func.length(func.trim(Call.title)) > 0).limit(batch_size)
        call_ids = session.execute(q).scalars().all()
    # ... rest of the function unchanged: per-row refresh, accumulate counters ...
```

(Pseudo-code: adapt to the actual structure of `refresh_missing_embeddings_tick`. The key invariant: when `force=False`, behavior is byte-identical to today; when `force=True`, the NULL/mismatched filter is dropped but the blank-text guard stays.)

- [ ] **Step 3: Update `src/call_monitor/embeddings/cli.py`**

Add `--force` to the argparse:

```python
p_backfill = sub.add_parser("backfill", help="Embed all NULL or model-mismatched rows.")
p_backfill.add_argument("--batch-size", type=int, default=200)
p_backfill.add_argument(
    "--force",
    action="store_true",
    help="Re-embed every row regardless of NULL/mismatched state. Use after "
         "deploys that change the text being embedded (e.g., Plan F's full_text rollout).",
)
```

Pass it through to `refresh_missing_embeddings_tick(force=args.force)`.

- [ ] **Step 4: Verify and commit**

```bash
uv run pytest tests/test_embeddings_cli.py tests/test_embeddings_refresh.py -v
uv run pytest -q
uv run ruff check .
git add src/call_monitor/embeddings/cli.py src/call_monitor/embeddings/refresh.py tests/test_embeddings_cli.py tests/test_embeddings_refresh.py
git commit -m "feat: backfill CLI --force flag (re-embed everything; deploy after content changes)"
```

Expected: 3 new tests pass, suite total ~452.

---

## Task 8: New `ingestion.cli` for forced ingest

**Files:**
- Create: `src/call_monitor/ingestion/cli.py`
- Create: `tests/test_ingestion_cli.py`

The deploy procedure (spec §9 step 3) needs a way to trigger an ingest tick from the command line. Currently ingestion only runs from APScheduler. Add a tiny CLI.

- [ ] **Step 1: Failing test — `tests/test_ingestion_cli.py`**

```python
"""Tests for the ingestion CLI: `python -m call_monitor.ingestion.cli run --source <slug>`."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from call_monitor.ingestion.cli import ingest_main


def test_cli_invokes_orchestrator_for_named_source(pg_session_factory):
    """`run --source vinnova` calls the orchestrator with the Vinnova adapter."""
    fake_run = MagicMock(return_value=None)
    with patch("call_monitor.ingestion.cli._run_for_source", fake_run):
        exit_code = ingest_main(
            argv=["run", "--source", "vinnova"],
            session_factory=pg_session_factory,
        )
    assert exit_code == 0
    fake_run.assert_called_once()
    args, kwargs = fake_run.call_args
    # Slug propagates
    assert kwargs.get("slug") == "vinnova" or "vinnova" in args


def test_cli_unknown_source_returns_nonzero(pg_session_factory):
    """`run --source garbage` exits 1 with a clear error."""
    exit_code = ingest_main(
        argv=["run", "--source", "garbage"],
        session_factory=pg_session_factory,
    )
    assert exit_code == 1


def test_cli_no_source_lists_available_sources(pg_session_factory, capsys):
    """`run --list-sources` (or no source) prints registered sources."""
    exit_code = ingest_main(
        argv=["list-sources"],
        session_factory=pg_session_factory,
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "vinnova" in captured.out
    assert "eu_portal" in captured.out


def test_cli_returns_nonzero_when_orchestrator_reports_error(
    pg_session_factory, capsys,
):
    """Codex H2 regression: when run_ingestion returns an IngestionResult
    with .error set (adapter HTTP failure, DB error during enqueue, etc.),
    the CLI must exit 1 — deploy scripts chain on `&&` and would otherwise
    proceed past a real failure."""
    from unittest.mock import MagicMock

    fake_failed_result = MagicMock(error="adapter timed out", inserted=0, updated=0)
    with patch("call_monitor.ingestion.cli._run_for_source",
               return_value=fake_failed_result):
        exit_code = ingest_main(
            argv=["run", "--source", "vinnova"],
            session_factory=pg_session_factory,
        )
    assert exit_code == 1
    captured = capsys.readouterr()
    assert "adapter timed out" in captured.err
```

- [ ] **Step 2: Implement `src/call_monitor/ingestion/cli.py`**

```python
"""Ingestion CLI — `python -m call_monitor.ingestion.cli run --source <slug>`.

Used by the deploy procedure (spec 2026-04-19) to force an ingest tick
without waiting for the next APScheduler cron. Operators run this after
resetting `Source.last_fetched_at` to repopulate `Call.full_text` on
historical rows.

    # Trigger one-shot ingest of a single source:
    python -m call_monitor.ingestion.cli run --source vinnova

    # List available sources:
    python -m call_monitor.ingestion.cli list-sources
"""
from __future__ import annotations

import argparse
import logging
import sys

from call_monitor.config import Settings
from call_monitor.db.session import make_engine, make_session_factory
from call_monitor.ingestion.alerts import ConsecutiveFailureTracker
from call_monitor.ingestion.orchestrator import run_ingestion
from call_monitor.ranking.queue import enqueue_for_pair  # if needed
from call_monitor.sources.eu_portal import EUPortalAdapter
from call_monitor.sources.vinnova import VinnovaAdapter
from call_monitor.embeddings.client import OllamaEmbedClient

_logger = logging.getLogger(__name__)


def _build_adapter(slug: str, settings: Settings):
    if slug == "vinnova":
        return VinnovaAdapter(base_url=settings.vinnova_base_url)
    if slug == "eu_portal":
        return EUPortalAdapter(base_url=settings.eu_portal_base_url)
    return None


def _build_embed_client(settings: Settings) -> OllamaEmbedClient:
    return OllamaEmbedClient(
        base_url=settings.ollama_url,
        model=settings.embedding_model,
    )


def _run_for_source(*, slug: str, session_factory, settings: Settings):
    """Returns the orchestrator's IngestionResult so the caller can inspect
    `result.error` and propagate failure to the shell exit code."""
    adapter = _build_adapter(slug, settings)
    if adapter is None:
        raise ValueError(f"unknown source: {slug}")
    embed_client = _build_embed_client(settings)
    alerter = ConsecutiveFailureTracker(...)  # adapt to actual constructor
    return run_ingestion(
        adapter=adapter,
        session_factory=session_factory,
        embed_client=embed_client,
        alerter=alerter,
    )


def ingest_main(argv: list[str] | None = None, *, session_factory=None) -> int:
    """Entry point. Returns:
    - 0 on full success (orchestrator's IngestionResult.error is None)
    - 1 on usage error, unknown source, or orchestrator-reported failure

    The exit code is load-bearing: deploy scripts (per spec §9) chain
    `python -m call_monitor.ingestion.cli run --source X && next-step`.
    A silent zero on a real ingest failure would break that chain.
    """
    parser = argparse.ArgumentParser(prog="call_monitor.ingestion.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="Force a one-shot ingest tick for a source.")
    p_run.add_argument("--source", required=True, help="Source slug (e.g., vinnova).")

    sub.add_parser("list-sources", help="List registered source slugs.")

    args = parser.parse_args(argv)

    if args.cmd == "list-sources":
        # Hardcoded list; adapt if adapters get registered dynamically later.
        for slug in ("vinnova", "eu_portal"):
            print(slug)
        return 0

    if args.cmd == "run":
        if session_factory is None:  # pragma: no cover — production path
            settings = Settings()
            engine = make_engine(settings.database_url)
            session_factory = make_session_factory(engine)
        else:
            settings = Settings()  # tests inject session_factory but still need settings

        try:
            result = _run_for_source(
                slug=args.source, session_factory=session_factory, settings=settings,
            )
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1

        # Orchestrator reports operational failures via IngestionResult.error
        # (e.g., adapter HTTP error, DB error during enqueue). Those are
        # ALSO failures from the deploy script's perspective.
        if result is not None and getattr(result, "error", None):
            print(f"ingest failed: {result.error}", file=sys.stderr)
            return 1

        # Optional: surface counts so the operator sees what was done
        if result is not None:
            inserted = getattr(result, "inserted", None)
            updated = getattr(result, "updated", None)
            if inserted is not None and updated is not None:
                print(f"ingest ok: {inserted} new, {updated} updated")

        return 0

    parser.error(f"unknown command: {args.cmd}")


def main() -> None:  # pragma: no cover
    sys.exit(ingest_main(sys.argv[1:]))


if __name__ == "__main__":  # pragma: no cover
    main()
```

(Adapt to the actual orchestrator entry-point signature and Settings/adapter construction patterns. The exact factory functions may differ from what's sketched here.)

- [ ] **Step 3: Verify and commit**

```bash
uv run pytest tests/test_ingestion_cli.py -v
uv run python -m call_monitor.ingestion.cli list-sources   # smoke test — should print 2 lines
uv run pytest -q
uv run ruff check .
git add src/call_monitor/ingestion/cli.py tests/test_ingestion_cli.py
git commit -m "feat: ingestion CLI — force ingest of a single source on demand"
```

Expected: 3 new tests pass, suite total ~455.

---

## Task 9: Document the adapter-authoring contract

**Files:**
- Modify: `src/call_monitor/sources/__init__.py`

A docstring update in one file. The decision tree from the spec §3.6 lives here so future adapter authors find it.

- [ ] **Step 1: Update `src/call_monitor/sources/__init__.py`**

Replace the file's current contents (likely empty or a one-liner) with:

```python
"""Source adapters for funding-call ingestion.

Each adapter normalizes one funding-body's API into `NormalizedCall`
([sources/types.py](./types.py)) and yields them via `fetch_new_calls(since)`.

## Populating `NormalizedCall.full_text` — preferred order

When writing a new adapter, populate `full_text` in this order of preference:

1. **Use the source's API** if it returns the call body (e.g., Vinnova's
   `WebTextLista[*].TextSv`). Fastest, most stable, structured.

2. **Fetch the source's call-detail endpoint** if it's a separate API call
   (some funding bodies have list+detail endpoints; the body is in detail).

3. **Scrape the call's HTML page** via
   `extraction.fetch_html_main_content(url, transport=...)` only when the API
   is insufficient. Document why in your adapter's docstring. Pass through a
   `transport` kwarg from your adapter constructor so tests can inject
   `httpx.MockTransport`.

4. **Fetch a linked PDF attachment** via
   `extraction.fetch_pdf_text(url, transport=...)` when the body lives in a
   PDF.

5. **Return None** if none of the above apply. Ranking still works
   (degraded — the embedding gate becomes UNKNOWN, the LLM falls back to
   title + summary alone).

The orchestrator does NOT auto-scrape — adapters call the helpers explicitly.
This keeps source-specific intent inside the source's adapter and the
orchestrator stays source-agnostic.

## Failure handling

`fetch_html_main_content` and `fetch_pdf_text` are best-effort: they log
on failure and return None. Adapters do not need to wrap them in try/except
(the helpers don't raise on HTTP/parse errors).

The upsert layer preserves `full_text` against transient None — see
`ingestion/upsert.py`. Adapters can return `full_text=None` on a failed
scrape without erasing previously-captured body text on existing rows.

## Ranking pipeline impact

Once `full_text` is populated, three downstream layers use it automatically:

- `embeddings.similarity.build_call_text` includes `full_text[:8000]` in BGE-M3 input.
- `ranking.prompt.build_messages` includes `summary + full_text[:CALL_TEXT_LIMIT]` (16000) in the LLM rubric body.
- `ranking.rank_pair._build_verification_text` uses untruncated `full_text` for quoted-evidence verification.

There is no schema migration required for new adapters — `Call.full_text`
already exists.
"""
```

- [ ] **Step 2: Verify and commit**

```bash
uv run pytest -q     # no behavior change; just docstring
uv run ruff check .
git add src/call_monitor/sources/__init__.py
git commit -m "docs: adapter-authoring contract for full_text in sources/__init__.py"
```

Expected: 455 still passing.

---

## Verification at end of plan

1. `uv run ruff check .` → clean
2. `uv run pytest -q` → all green (~455 tests)
3. `DATABASE_URL=... uv run alembic check` → "No new upgrade operations detected" (no schema changes)
4. **Manual smoke** (with Ollama running with `gemma4:e4b-it-q4_K_M` and `bge-m3` pulled):
   - `python -m call_monitor.ingestion.cli list-sources` → prints `vinnova` + `eu_portal`
   - `python -m call_monitor.ingestion.cli run --source vinnova` → fetches a few programmes, populates `Call.full_text` for them
   - Open `psql` and verify a recent Vinnova `Call.full_text` is non-NULL and contains a multi-paragraph body (the `WebTextLista` content)
   - Same for EU portal: `python -m call_monitor.ingestion.cli run --source eu_portal` → verify `full_text` non-NULL
   - `python -m call_monitor.embeddings.cli backfill --force` → re-embeds everything, exits 0
   - Hit `/admin/rerank` once → drains a queue of fresh ranking jobs under `prompt_version="v3.0"`
   - Open `/calls?profile=<id>` and `/calls/<id>?profile=<id>` to confirm rankings render normally
   - Check `/admin` embedding-coverage badge → reads "calls embedded: N/N"

5. **Code metrics — before/after Plan F**:
   ```bash
   find src/call_monitor -name '*.py' | xargs wc -l | tail -1
   find tests -name '*.py' | xargs wc -l | tail -1
   ```
   Expected: production +200 LOC (extraction.py + cli.py + small adapter changes), tests +400 LOC.

## Deploy procedure (post-merge)

After Plan F merges to master, the deploy sequence (per spec §9) is:

```bash
# 1. Schema (no deltas from Plan F itself, but always safe):
alembic upgrade head

# 2. Reset incremental-fetch cursors so the next ingest re-fetches the
#    historical window. NULL (not a hardcoded date) is correct: the
#    orchestrator falls back to epoch when last_fetched_at IS NULL — see
#    orchestrator.py — guaranteeing every historical row is re-fetched
#    regardless of when it first appeared.
psql -c "UPDATE sources SET last_fetched_at = NULL WHERE slug IN ('vinnova', 'eu_portal')"

# 3. Trigger ingest for both sources to populate full_text on existing rows.
#    The CLI exits non-zero on adapter/DB failure (see Task 8), so chaining
#    with && is safe — a failed Vinnova refetch won't silently proceed to EU.
python -m call_monitor.ingestion.cli run --source vinnova && \
python -m call_monitor.ingestion.cli run --source eu_portal

# 4. Re-embed all calls + profiles under the now-richer text.
python -m call_monitor.embeddings.cli backfill --force

# 5. Re-rank existing calls under prompt v3.0. Hit /admin/rerank from the
#    admin UI after step 4 completes. The drain tick processes the queue
#    over the next ~hour at typical scale.

# 6. Restart the app.
systemctl restart call-monitor
```

The `/admin` embedding-coverage badge surfaces a quick post-deploy sanity check.

## Out of scope (deferred to follow-up plans)

Per spec §10:
- DOCX support (`python-docx`)
- Async / parallel scraping
- Source-specific Trafilatura overrides
- Caching (`full_text_fetched_at` skip-if-fresh)
- OCR for scanned PDFs
- Source-detail endpoint pre-fetching when the search API is sufficient
- Custom HTML/PDF sanitization
- Admin tool for re-NULL'ing `full_text`

## After this plan ships

- Vinnova and EU portal adapters populate `full_text` from their respective best sources (API or scrape).
- LLM rubric sees `summary + full_text` (concatenated, truncated to 16 KB) — actual call content reaches the ranker.
- Future adapters write 5-10 lines for `full_text` population — clear pattern documented in `sources/__init__.py`.
- Operator can spot which calls have body content (`Call.full_text IS NOT NULL`) and which don't.
- `prompt_version` correctly distinguishes v2.0 (summary-only era) from v3.0 (summary+full_text era) for audit.
