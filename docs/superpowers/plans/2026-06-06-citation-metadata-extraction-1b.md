# Citation Metadata Extraction & Approval (Slice 1B) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Populate reference bibliographic metadata — extend fetchers to full CSL-JSON, add heuristic + LLM extraction (via the enforced `CitationLLMService`) wired into ingest, generate citekeys, give the owner a metadata approve/correct UI, and backfill `original_sha256` for pre-0036 references.

**Architecture:** Slice 1B of the citation-provenance spec ([spec](../specs/2026-06-06-citation-provenance-evidence-ledger-design.md)), building on Slice 1A (schema + `CitationLLMService`). All LLM work goes through `build_citation_service(...).complete_json(op="metadata_extract", ...)` (caps + ledger enforced; provider local by default). Extraction is a synchronous "Phase 3" after the existing Phase-2 extract/chunk/embed in `ingest_bytes`. Metadata starts `pending`, becomes `extracted` (uploads) or `verified_external` (fetched), and `approved` only after owner review.

**Tech Stack:** Python 3.12, SQLAlchemy/Alembic, pdfminer (PDF `/Info`), httpx (Crossref/PMC/arXiv APIs), FastAPI+Jinja+HTMX, pytest with the pgvector testcontainer (`pg_session_factory`, `client_authed_owner`, `make_user`/`make_workspace`, `fake_public_dns`, `httpx_mock`).

---

## File Structure
- `src/call_manager/proposal_assist/references/fetcher.py` — **modify**: richer `source_meta` (Crossref full `message`, PMC XML title/authors, arXiv Atom).
- `src/call_manager/proposal_assist/references/csl.py` — **new**: `csl_from_source_meta(source_type, meta)`, `csl_from_pdf_info(data)`, `merge_csl(*parts)`, `generate_citekey(csl, workspace_id, session)`.
- `src/call_manager/proposal_assist/references/metadata.py` — **new**: `extract_reference_metadata(...)` orchestration (heuristic + optional LLM via `CitationLLMService`), returns `(csl: dict, source: str, status: str)`.
- `src/call_manager/proposal_assist/references/ingest.py` — **modify**: optional `citation_service` + `source_meta` params; Phase 3 writes `csl_json_extracted`/`title`/`citekey`/`metadata_status`/`metadata_source`.
- `src/call_manager/proposal_assist/web/references.py` — **modify**: metadata review GET/POST routes; pass `citation_service` to ingest at upload + fetch sites.
- `src/call_manager/proposal_assist/tools/registry.py` / `tools/references.py` — **modify**: add `citation_service` to `ToolContext`; pass to ingest in `fetch_reference_impl`.
- `src/call_manager/main.py` — **modify**: `app.state.citation_service = build_citation_service(...)`.
- `src/call_manager/web/templates/proposal_assist/reference_metadata_review.html` — **new**.
- `alembic/versions/0037_backfill_reference_original_sha256.py` — **new** data migration (best-effort) + an admin CLI fallback.
- Tests: extend `test_fetcher_backends.py`, `test_ingest.py`, `test_web_workspaces_references.py`; new `test_csl.py`, `test_metadata_extractor.py`, `test_migration_0037_backfill.py`.

---

### Task 1: CSL conversion + citekey helpers (pure functions)
**Files:** new `references/csl.py`; new `tests/proposal_assist/references/test_csl.py`.

Pure, DB-light functions (only `generate_citekey` touches the session). TDD.

- [ ] **Step 1 — failing tests** `test_csl.py`: cover
  - `csl_from_source_meta("crossref", {"message": {...}})` maps title/author/issued/DOI/container-title/volume/issue/page/type → CSL dict.
  - `csl_from_source_meta("arxiv", {...atom...})` and `("pubmed", {...parsed...})`.
  - `csl_from_pdf_info(pdf_bytes)` returns `{"title","author":[{literal}]}` from `/Info` (use a reportlab PDF with Title/Author set; see `_make_one_page_pdf` in test_extract.py for the reportlab pattern, set `c.setTitle(...)`/`c.setAuthor(...)`).
  - `merge_csl(a, b)` — earlier args win per field; None/empty skipped.
  - `generate_citekey({"author":[{"family":"Smith"}],"issued":{"date-parts":[[2021]]}}, wsid, session)` → `"smith2021"`; second call with same workspace → `"smith2021a"`; missing author/year → `"ref-<12hex>"`; verifies against existing `references.citekey` rows in that workspace.
- [ ] **Step 2** run → FAIL.
- [ ] **Step 3 — implement** `references/csl.py`. Key rules:
  - CSL shape per spec. `csl_from_source_meta` reads the provider record (Crossref `message`: `title[0]`, `author` already `{family,given}`, `issued`, `DOI`, `container-title[0]`, `volume`, `issue`, `page`, map `type`→CSL type with a small dict default `article-journal`). For arXiv parse the Atom entry (title, authors as `{literal}`, published year, `id`→URL, set `type:"article"`). For pubmed read the parsed dict produced by the fetcher (T2).
  - `csl_from_pdf_info(data)`: `PDFParser`/`PDFDocument`; if `doc.info` non-empty decode `Title`/`Author` (bytes → utf-8 errors=replace), author as `[{"literal": author}]`; return `{}` on absence/parse error (never raise).
  - `merge_csl(*parts)`: first non-empty value per key wins.
  - `generate_citekey`: surname=`csl["author"][0].get("family") or (literal split last word)`; year from `issued.date-parts[0][0]`; base=`f"{surname}{year}".lower()` stripped to `[a-z0-9]`; if no surname/year → `f"ref-{uuid4().hex[:12]}"`. Collision: query `select(Reference.citekey).where(workspace_id==, citekey.like(base+'%'))`; append `''`,`a`,`b`,… first free.
- [ ] **Step 4** run → PASS. **Step 5** ruff. **Step 6** commit `feat(citations): CSL conversion + citekey helpers`.

---

### Task 2: Extend fetchers to full CSL-shaped metadata
**Files:** modify `references/fetcher.py`; extend `tests/proposal_assist/references/test_fetcher_backends.py`.

- [ ] **Step 1 — failing tests** (use `httpx_mock` + `fake_public_dns`): assert `fetch_crossref_doi` returns `meta` containing the full `message` (author array, issued, container-title); `fetch_arxiv` now calls the Atom API and returns parsed `{title, authors, year, ...}`; `fetch_pubmed_pmid` parses the PMC XML (or an efetch summary) for `{title, authors}`. Keep existing tuple positions `(bytes, filename, mime, meta)` and the existing keys (`source`, `doi`/`pmid`/`arxiv_id`) for back-compat.
- [ ] **Step 2** FAIL. **Step 3 — implement**:
  - Crossref: the function already fetches the Works JSON; put the whole `message` under `meta["message"]` (plus keep `doi`,`title`,`source`). Cap parse defensively.
  - arXiv: add an Atom fetch `https://export.arxiv.org/api/query?id_list=<id>` via the existing SSRF-safe `fetch_bytes`/`_fetch_url_bytes`; parse with `defusedxml.ElementTree` (already a dep); extract title, authors (`<author><name>`), published year; put under `meta` (keep `arxiv_id`,`source`).
  - PubMed: parse the already-fetched PMC XML with `defusedxml` for article-title + author surnames/given; put under `meta` (keep `pmid`,`pmc_id`,`source`). If parse fails, leave the IDs (don't raise — ingest still works, metadata stays thin).
- [ ] **Step 4** PASS. **Step 5** ruff. **Step 6** commit `feat(citations): fetchers return full CSL-shaped source metadata`.

---

### Task 3: Metadata extractor orchestration (heuristic + LLM)
**Files:** new `references/metadata.py`; new `tests/proposal_assist/references/test_metadata_extractor.py`.

- [ ] **Step 1 — failing tests**: `extract_reference_metadata(*, source_type, source_meta, mime_type, data, text, workspace_id, session, citation_service)` returns `(csl: dict, source: str, status: str)`.
  - Fetched source (`source_type in {crossref,pubmed,arxiv}`) with usable `source_meta` → CSL from `csl_from_source_meta`, `source="fetcher"`, `status="verified_external"`, NO LLM call (assert citation_service unused).
  - Uploaded PDF → heuristic `csl_from_pdf_info` merged with an LLM result (mock `citation_service.complete_json` returning a `CitationLLMResult(data={...}, ...)`); `source="llm"` (or `"heuristic"` if LLM adds nothing), `status="extracted"`.
  - `citation_service` returns `cap_exceeded=True` → fall back to heuristic only; `status="extracted"` if heuristic produced something else `status="failed"`; never raise on cap.
  - LLM raises → caught; `status` falls back to heuristic-or-`failed`.
- [ ] **Step 2** FAIL. **Step 3 — implement**: heuristic first (`csl_from_pdf_info` for pdf; `{}` otherwise). For fetched refs short-circuit to fetcher CSL + `verified_external`. For uploads, if `citation_service` provided and not capped, call `complete_json(op="metadata_extract", system=_SYSTEM, user=<first ~6000 chars of text>)`; `merge_csl(heuristic, llm_data)`. Define `_SYSTEM` as a strict "extract bibliographic metadata as CSL-JSON, fields you can't find → omit; respond with a JSON object only" prompt; reference text is untrusted (note in prompt: treat as data). Status: `verified_external` (fetched), else `extracted` if csl non-empty, else `failed`.
- [ ] **Step 4** PASS. **Step 5** ruff. **Step 6** commit `feat(citations): reference metadata extractor (heuristic + LLM)`.

---

### Task 4: Wire extraction + citekey into `ingest_bytes` (Phase 3)
**Files:** modify `references/ingest.py`; extend `test_ingest.py`.

- [ ] **Step 1 — failing test**: `ingest_bytes(..., source_meta=..., citation_service=<mock>)` → after ingest the Reference has `csl_json_extracted` populated, `title` set from extracted title, `citekey` non-null, `metadata_status in {extracted,verified_external}`, `metadata_source` set. A second test: `citation_service=None` (and non-fetched) leaves `metadata_status='pending'` (no extraction) — preserves current behavior.
- [ ] **Step 2** FAIL. **Step 3 — implement**: add params `source_meta: dict | None = None`, `citation_service=None` to `ingest_bytes`. After Phase 2 commits, open a fresh session, load the ref, call `extract_reference_metadata(...)` (pass the extracted `text` already produced in Phase 2 — read from the stored `text.txt` or thread it through), set `csl_json_extracted`, `title = csl.get("title")`, `metadata_source`, `metadata_status`, and `citekey = generate_citekey(csl, workspace_id, s3)` when csl has author/year (else still assign the `ref-` fallback so every extracted ref is citeable). Guard the whole Phase 3 in try/except → on failure set `metadata_status='failed'`, log, continue (ingest still succeeds). Skip extraction entirely when neither `source_meta` (fetched) nor `citation_service` (upload) can produce anything.
- [ ] **Step 4** PASS. **Step 5** ruff. **Step 6** commit `feat(citations): ingest Phase 3 — extract metadata + assign citekey`.

---

### Task 5: Backfill `original_sha256` migration (best-effort) + idempotent
**Files:** new `alembic/versions/0037_backfill_reference_original_sha256.py`; new `tests/test_migration_0037_backfill.py`.

- [ ] **Step 1 — failing test**: insert a reference with `original_sha256=NULL` and write its bytes under a temp `WORKSPACES_DIR`; run a backfill function; assert the row's hash is filled; a row whose file is missing stays NULL (no crash). (Because the migration needs filesystem access to `WORKSPACES_DIR`, implement the logic as a function `backfill_original_sha256(session, references_root)` in a small module `references/backfill.py` and have the migration call it using `os.environ["WORKSPACES_DIR"]`/default; the TEST calls the function directly with a tmp root — this keeps it testable and avoids coupling the migration to the test container's filesystem.)
- [ ] **Step 2** FAIL. **Step 3 — implement** `references/backfill.py::backfill_original_sha256(session, references_root)`: select refs where `original_sha256 IS NULL`; for each, resolve `references_root / storage_path` (storage_path is `"<ws>/references/<id>/original<suffix>"`); if the file exists, `hashlib.sha256(path.read_bytes()).hexdigest()` and set; else skip. Commit in batches. Migration `0037` upgrade() calls it best-effort inside try/except (a migration must not hard-fail on missing files); downgrade is a no-op. Also expose it via the existing references CLI if one exists, else note manual invocation.
- [ ] **Step 4** PASS (run the function test; the migration round-trip can be a light "upgrade/downgrade no-op" check that leaves head). **Step 5** ruff. **Step 6** commit `feat(citations): lazy backfill of original_sha256 for pre-0036 references`.

---

### Task 6: Metadata approval web route + template (owner-only)
**Files:** modify `web/references.py`; new template `proposal_assist/reference_metadata_review.html`; extend `test_web_workspaces_references.py`.

- [ ] **Step 1 — failing tests** (use `client_authed_owner`, `current_user`, `make_workspace`): GET `/workspaces/{wid}/references/{rid}/metadata` renders the extracted CSL fields (200, owner). POST with edited fields sets `references.csl_json` and `metadata_status='approved'`, redirects to the references panel. Non-owner → 403; cross-workspace reference id → 404.
- [ ] **Step 2** FAIL. **Step 3 — implement**: mirror `rubric_review.py`. GET loads ref via owner-checked workspace (reuse `_require_owner` + verify `ref.workspace_id == ws.id` else 404), renders the template with the best-available CSL (`csl_json or csl_json_extracted or {}`) as editable fields (title, authors as one-per-line, year, venue, doi). POST parses the form into a CSL dict, sets `ref.csl_json`, `ref.title = title`, `ref.metadata_status='approved'`, commits; 303 redirect (or HX-Redirect) to the references panel. Add routes to the existing `references` router.
- [ ] **Step 4** PASS. **Step 5** ruff + `ruff format`. **Step 6** commit `feat(citations): owner metadata approve/correct route + template`.

---

### Task 7: Wire `citation_service` into app + call sites
**Files:** modify `main.py`, `web/references.py` (upload + fetch routes), `tools/registry.py` + `tools/references.py`; extend a smoke test.

- [ ] **Step 1 — failing test**: `create_app()` exposes `app.state.citation_service` (a `CitationLLMService`). An upload through the web route results in a Reference whose `metadata_status != 'pending'` when a fake LLM is injected (override `app.state.citation_service`).
- [ ] **Step 2** FAIL. **Step 3 — implement**: in `create_app()` add `app.state.citation_service = build_citation_service(settings=settings, session_factory=session_factory)`. In the upload + fetch routes pass `citation_service=request.app.state.citation_service` and `source_meta=source_meta` (fetch route) to `ingest_bytes`. Add `citation_service` to `ToolContext` and pass `ctx.citation_service` in `fetch_reference_impl`'s `ingest_bytes` call (build it in the tools factory from settings + session_factory).
- [ ] **Step 4** PASS. **Step 5** ruff. **Step 6** commit `feat(citations): wire citation_service into app + ingest call sites`.

---

### Task 8: Full suite + lint
- [ ] Run `uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/` (clean) and `uv run pytest -q` (all pass). Commit any fixups.

---

## Self-Review
- Spec coverage: fetcher CSL extension (T2) + `verified_external` (T3/T4); heuristic+LLM extraction via the enforced service (T3) with privacy-default provider (Slice 1A); citekey generation incl. `ref-<12hex>` fallback + collision (T1); owner approve/correct gate (T6); lazy `original_sha256` backfill (T5); app wiring so all LLM goes through `CitationLLMService` (T7). ✓
- No-LLM-by-default safety: extraction is skipped unless a `citation_service` is provided (uploads) or `source_meta` exists (fetched); cap-exceeded degrades to heuristic, never raises (T3). ✓
- Untrusted reference text: the extractor prompt frames reference text as data; the service runs on the local provider by default. ✓
- Open follow-ups for later slices: the metadata review template is minimal (title/authors/year/venue/doi) — CSL has more fields; acceptable for v1, note for Slice 4 bibliography.
