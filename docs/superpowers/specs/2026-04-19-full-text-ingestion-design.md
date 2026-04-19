# Full-text ingestion (design)

> **Status:** design draft, 2026-04-19. Not implemented. The companion implementation plan will follow once this spec is reviewed.
>
> **Companion specs:**
> - [2026-04-14-call-monitor-design.md](./2026-04-14-call-monitor-design.md) — original system design
> - [2026-04-17-ranking-v2-criterion-design.md](./2026-04-17-ranking-v2-criterion-design.md) — ranking pipeline that consumes the call body

## 1. Problem statement

Both source adapters (`vinnova.py`, `eu_portal.py`) construct `NormalizedCall` without setting `full_text`. The schema has the column ([db/models.py](../../../src/call_monitor/db/models.py)) and the rest of the pipeline references it — but inconsistently:

- `embeddings/similarity.py:build_call_text` reads `title + summary + full_text[:8000]` for the BGE-M3 input — concatenated. ✅
- `ranking/rank_pair.py:_build_verification_text` uses untruncated `title + summary + full_text` for quoted-evidence verification — concatenated. ✅
- `ranking/prompt.py` (line 96): `text_source = call.summary OR getattr(call, "full_text", None) OR ""` — **picks one, doesn't concatenate**. ❌

The prompt's OR-pick means that even after Plan F populates `full_text`, the LLM rubric will keep seeing only `summary` (because summary is non-empty for all Vinnova calls today). The Vinnova split proposed in §5.1 would actively REGRESS the LLM's view: shrinking `summary` to just `Beskrivning` would mean the LLM rubric sees only the 40-70 char headline-like description, ignoring the body text now living in `full_text`.

**This is not a pure data-only change.** Plan F also has to fix the prompt to actually concatenate `summary + full_text`, with a `PROMPT_VERSION` bump for audit / A-B-comparability.

Effects of today's gap:

- **Vinnova**: tolerable — the adapter currently shoehorns `WebTextLista[*].TextSv` (the substantive call prose) into `summary`, so the rubric LLM still sees real content. The cost is structural: there's no clean separation between "abstract" and "body" for this source.
- **EU Funding & Tenders Portal**: a real shortcoming — the adapter only reads the API's `summary` field, which is typically a 1-2 sentence abstract. The rubric LLM scores against the abstract, not the actual call.
- **Future sources**: every adapter author has to figure out the same problem. There's no shared toolkit, no documented pattern, no per-source decision tree.

## 2. Design intent (one-line)

**Each adapter populates `NormalizedCall.full_text` from its source's most authoritative API endpoint when available, falling back to a shared HTML/PDF scraping toolkit when no API exists. Scraping is best-effort and explicit per adapter — never automatic.**

## 3. Architecture decisions (FIX-NOW; push back if you disagree)

### 3.1 API-first, scrape-fallback, per-adapter explicit choice

- **Order of preference**: source API → source detail endpoint → HTML scrape → PDF scrape → None.
- **Decision lives in the adapter**, not the orchestrator. Each adapter knows whether its API has the body, where to look if not, and which fallback (HTML or PDF) is appropriate. The orchestrator does not auto-scrape any source's `full_text` if the adapter left it None.
- **Why**: auto-scraping every source that "happened to leave full_text empty" creates noisy failures (sources that legitimately have no body) and hides per-source intent (which URL to scrape — the call page? a topic-detail PDF? a separate landing page?). The adapter is the only place that knows.

### 3.2 Synchronous fetching inside the adapter's `fetch()` loop

- **Where the work happens**: inside `adapter.fetch()`, after the per-call API enrichment. The adapter returns a fully-populated `NormalizedCall` (with `full_text` set or None) to the orchestrator. The orchestrator's existing 3-step transaction split (upsert → embed → enqueue) is unchanged.
- **Why not orchestrator step 2?** Orchestrator step 2 is currently embedding-refresh — best-effort, per-row, log-on-fail. Adding a parallel "scrape-refresh" there would force a `BodySource` enum on `NormalizedCall` to express "where to scrape from" generically. That's premature abstraction with N=2 sources. Scraping is the adapter's responsibility.
- **Failure semantics**: scrape failure inside the adapter is logged and the field is left None. Ranking still runs (degraded — embedding gate becomes UNKNOWN, LLM falls back to title+summary). Same graceful-degradation pattern as embedding-refresh.

### 3.3 PyMuPDF for PDF, Trafilatura for HTML

- **PDF: PyMuPDF (`pymupdf`)**. AGPL-3.0 — accepted (internal hospital tool, no external distribution). Best-of-breed quality on funding-call PDFs (typically text-heavy, sometimes table-rich, occasionally OCR'd). Faster than alternatives (`pypdf`, `pdfplumber`) and significantly more robust on complex layouts.
- **HTML: Trafilatura**. Apache-2.0. Designed for content extraction (boilerplate stripping, main-content detection). Pulls in `lxml`, `htmldate`. Alternatives considered: `readability-lxml` (older, less robust), raw `beautifulsoup4` + heuristics (worse on modern pages).
- **No DOCX support yet**. Adding `python-docx` is one line + helper if a future Swedish charity funder needs it. Defer until requested.

### 3.4 The `NormalizedCall.full_text` contract

- `full_text: str | None = None` — **already exists in `sources/types.py:42`**. No new field; just adopt it.
- Adapters set it however they can (inline, helper-fetched, helper-extracted, or None).
- Adapters are NOT required to populate it (graceful degradation is built in).

### 3.5 Shared toolkit module: `src/call_monitor/sources/extraction.py`

Two helpers, both best-effort, logged-on-failure, return `str | None`. Signatures mirror `OllamaClient`/`OllamaEmbedClient` (`transport=` kw-only) so tests inject `MockTransport` the same way:

```python
def fetch_html_main_content(
    url: str,
    *,
    timeout: float = 10.0,
    transport: httpx.BaseTransport | None = None,
) -> str | None:
    """Fetch URL and extract main content via Trafilatura.

    Returns None on HTTP error, fetch timeout, or if Trafilatura
    can't identify a main content block. Logs `extraction.html_failed`
    with the URL and the underlying error.
    """

def fetch_pdf_text(
    url: str,
    *,
    timeout: float = 30.0,
    max_bytes: int = 50_000_000,
    transport: httpx.BaseTransport | None = None,
) -> str | None:
    """Fetch a PDF URL and extract its text via PyMuPDF.

    Caps download at `max_bytes` (50 MB default — a hard ceiling
    against bombs). Returns None on HTTP error, oversized payload,
    PDF parse failure, or empty extraction. Logs `extraction.pdf_failed`.
    """
```

Both helpers internally do `httpx.Client(transport=transport, timeout=timeout)`. Adapters either pass `transport=None` (production: real network) or accept it as a constructor kwarg of their own to support test injection — same convention as the LLM clients.

### 3.6 Per-adapter authoring pattern (the documented contract)

To be added to `src/call_monitor/sources/__init__.py` as a docstring:

> **Populating `NormalizedCall.full_text` — preferred order:**
>
> 1. **Use the source's API** if it returns the call body. Fastest, most stable, structured.
> 2. **Fetch the source's call-detail endpoint** if it's a separate API call.
> 3. **Scrape the call's HTML page** via `extraction.fetch_html_main_content(url)` only when the API is insufficient. Document why in the adapter docstring.
> 4. **Fetch a linked PDF** via `extraction.fetch_pdf_text(url)` when the body is in an attachment.
> 5. **Return None** if none of the above apply. Ranking still works (degraded — embedding gate becomes UNKNOWN, LLM falls back to title+summary).

### 3.7 LLM prompt: concatenate `summary + full_text`, bump truncation, bump PROMPT_VERSION

**The current prompt body (`prompt.py:96-100`) picks ONE of `summary` / `full_text`:**

```python
text_source = (
    call.summary
    or getattr(call, "full_text", None)
    or ""
)
```

This was correct when `full_text` was always None (early development) — `summary` carried everything. After Plan F, `summary` becomes a brief abstract and `full_text` becomes the body — the OR-pick would then drop the body entirely.

**Change**: concatenate, then truncate.

```python
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

**Truncation**: bump `CALL_TEXT_LIMIT` from `4000` → `16000`. Gemma 4 E4B context window is 128K tokens — 4 KB was defensive when full_text was empty. 16 KB is ~4 K tokens; well within budget alongside the system prompt + rubric instructions + JSON response.

**Embedding truncation**: stays at 8000 chars (BGE-M3's 8192-token hard limit).

**`PROMPT_VERSION` bump**: `"v2.0"` → `"v3.0"`. The rubric/parser shape doesn't change, but **what reaches the LLM** does. Bumping is the documented mechanism for this exact case (see `prompt.py:7` — version stamp goes onto every `CallRanking` row for audit and A/B comparison). Without the bump, "all rows ranked under v2.0" silently includes both old (summary-only) and new (summary+full_text) results, with no way to tell them apart.

### 3.8 Refresh policy — re-scrape on every ingest tick (with preservation on failure)

- Today: `upsert_call` (`ingestion/upsert.py:34-66`) always UPDATEs on conflict, replacing every column unconditionally. `summary` is refreshed every tick.
- With Plan F, scraping joins the upsert payload — but with one critical change: **`full_text` is preserved (not clobbered) when the new payload's value is None**.
  - **Why**: a transient HTTP timeout, a Trafilatura empty-extraction, a PDF parse failure all return None. Without preservation, a single transient failure on one ingest tick erases previously-captured body text from the DB row. Next ingest may or may not recover it.
  - **How**: in `upsert_call`'s `update_cols` dict, replace `full_text: stmt.excluded.full_text` with `full_text: func.coalesce(stmt.excluded.full_text, Call.full_text)`. SQL-level COALESCE: keep the existing value if the new one is NULL.
  - **Trade-off**: we can never re-NULL `full_text` via normal ingest. If a source removes a body and we want to reflect that, an admin operation is needed. Acceptable: the "body got removed" case is rare and surfaces during operator review; "transient failure clobbers good data" is common and silent.
- The same preservation applies symmetrically to other "best-effort enriched" fields if/when added later (e.g., a future `pdf_attachment_text`).
- Cost analysis: Vinnova has ~50 active programmes, no scraping. EU portal — assuming we end up scraping ~30 calls/day — adds ~150 ms × 30 = ~4.5 s of extra HTTP per daily ingest. Negligible at this scale.
- If costs grow (more sources, more calls/day), add a `full_text_fetched_at` column and skip when fresh. Defer until measured.

### 3.9 Test strategy

- **Helper tests**: unit-test `fetch_html_main_content` and `fetch_pdf_text` with `MockTransport` returning canned HTML strings / PDF bytes. Include 2-3 small fixture PDFs in `tests/fixtures/` (a few KB each, hand-crafted with `pymupdf` or a real funding-call PDF saved at fixture time).
- **Adapter tests**: extend existing `tests/test_sources_*.py` with assertions that `full_text` is populated correctly per adapter — Vinnova should have content from `WebTextLista`, EU portal should have content from either the richer API field or the HTML scrape.
- **Integration**: extend the existing `tests/test_ranking_e2e.py` to seed a call with a non-empty `full_text` and assert the LLM prompt contains a chunk from it.

## 4. Schema changes

**None.** `Call.full_text` already exists (`db/models.py` — added in an earlier plan, never wired up). Plan F just starts populating it.

## 5. Per-source updates

### 5.1 Vinnova (refactor — no scraping)

Today (`vinnova.py:34-54`): `summary` is `Beskrivning + WebTextLista[*].TextSv + BeskrivningEngelska` concatenated. After Plan F:

- `summary` = `Beskrivning` (and `BeskrivningEngelska` if present and distinct) — the brief description, ≤200 chars typically.
- `full_text` = `\n\n`.join(`WebTextLista[*].TextSv`) — the substantive prose paragraphs.
- **No new HTTP requests.** Pure organizational change.
- Adapter docstring updated to note: "API returns full content in `WebTextLista`; no scraping needed."

### 5.2 EU portal (investigate then update)

**Investigation step** (part of the plan, before code):
- Capture a raw `item` from the EU portal search API in dev (one curl + jq).
- Inventory all string-valued fields. Likely candidates beyond `summary`: `description`, `topicDescription`, `objectives`, `expectedImpact`, `additionalInfo`, `callConditions`, `eligibilityConditions`, `topicConditions`, etc.
- Decide: do these collectively give us enough body? Or do we still need to scrape the topic-detail HTML page?

**Outcome A (rich API fields exist)**:
- `summary` = `item.get("summary")` (unchanged — abstract).
- `full_text` = `\n\n`.join of the relevant rich fields, in a stable order.
- No scraping.

**Outcome B (API is genuinely thin)**:
- `summary` = `item.get("summary")` (unchanged).
- `full_text` = `extraction.fetch_html_main_content(url)`. Falls back to None on failure.
- Adapter docstring documents the fallback and why.

The investigation step's outcome is captured in the plan, not pre-decided here.

### 5.3 Future adapters

Adapter authoring becomes a 5-minute decision per source:

```python
class CancerfondenAdapter:
    """Cancerfonden has no public API; calls live as HTML pages on cancerfonden.se."""

    def fetch(self):
        # ... fetch the call list however the source allows ...
        for call_meta in self._fetch_call_list():
            full_text = fetch_html_main_content(call_meta["url"])
            yield NormalizedCall(
                external_id=call_meta["id"],
                url=call_meta["url"],
                title=call_meta["title"],
                summary=call_meta.get("abstract"),
                full_text=full_text,   # may be None on scrape failure
                # ...
            )
```

```python
class WellcomeAdapter:
    """Wellcome has an API for metadata, but the substantive call body
    is a PDF attachment on most awards."""

    def fetch(self):
        for item in self._fetch_api():
            pdf_url = item.get("attachments", [{}])[0].get("url")
            full_text = fetch_pdf_text(pdf_url) if pdf_url else None
            yield NormalizedCall(
                external_id=item["id"],
                url=item["html_url"],
                title=item["title"],
                summary=item.get("abstract"),
                full_text=full_text,
                # ...
            )
```

## 6. Pipeline changes downstream

Once `full_text` is populated, two layers light up unchanged, and one needs a code change:

1. **Embeddings (no code change)**: `build_call_text` already concatenates `title + summary + full_text[:8000]`. With `full_text` non-empty, BGE-M3 sees richer input → embeddings become more discriminative on borderline cases.
2. **Quote verification (no code change)**: `_build_verification_text` already uses untruncated `title + summary + full_text`. With it populated, quotes from past the 4/8/16 KB truncation window are validated correctly.
3. **LLM rubric prompt (CODE CHANGE — see §3.7)**: `prompt.py:96-100` currently does `summary OR full_text` (picks one). Plan F changes to `summary + full_text` concatenated, bumps `CALL_TEXT_LIMIT` from 4000 → 16000, and bumps `PROMPT_VERSION` from `"v2.0"` → `"v3.0"`. Without this change, populating `full_text` would silently NOT improve LLM judgments — the LLM would still see only `summary`.

## 7. Failure modes

| Failure | Effect on adapter | Effect on ranking | Effect on existing DB row |
|---|---|---|---|
| Source API returns no body field | Adapter falls back to scrape (its choice) | None (still has summary) | n/a |
| Scrape HTTP timeout (transient) | Helper logs `extraction.html_failed`, returns None | full_text reverts to None for THIS in-memory `NormalizedCall` — degraded ranking on this tick if it's a fresh insert | **Preserved** by `COALESCE(NEW, OLD)` in upsert (§3.8) — existing good `full_text` is NOT clobbered |
| PDF too large (> 50 MB cap) | Helper logs `extraction.pdf_oversized`, returns None | Same | Same — preserved |
| PyMuPDF parse error (corrupt PDF) | Helper logs, returns None | Same | Same — preserved |
| Trafilatura returns empty (no main content detected) | Returns None | Same | Same — preserved |
| Source's HTML structure changes (boilerplate stripping fails) | Trafilatura may return non-empty junk → not None → COALESCE writes the junk → low-quality embedding → low cosine | Cosine likely below threshold → embedding gate FAILs → score=0. The most pernicious failure mode (silent quality drop). Mitigated by golden-set τ tracking + the `embed_similarity` column the operator can spot-check. | **Overwritten** with junk — preservation only kicks in for None, not for "non-None but bad" |
| Source legitimately removes a call's body | Adapter writes None (no body to fetch) | Embedding gate UNKNOWN | **Preserved** — the COALESCE pattern can't distinguish "removed" from "transient failure". An operator can clear it manually if needed. Documented trade-off (§3.8). |

## 8. Performance expectations

| Source | Today's ingest cost | After Plan F | Delta |
|---|---|---|---|
| Vinnova (~50 programmes, two-stage API) | ~30 s (2 API calls × 50 + parsing) | ~30 s | 0 (no new HTTP) |
| EU portal (~30 new calls/day, single API) | ~5 s | ~5 s + 30 × ~150 ms HTTP if scraping = ~10 s | ~+5 s if scraping; 0 if API has rich fields |
| LLM rubric per pair | ~10 s on 2080 Ti | ~10-12 s with 16 KB body | +2 s typical (Gemma's serial token-decode time scales with input length) |
| Embedding per call | ~50 ms | ~50-80 ms with longer text | +30 ms (BGE-M3 still bounded at 8 KB) |
| Weekly digest ranking workload (15 profiles × 35 new calls × 10 s) | ~88 min | ~100 min | +12 min/week — well within budget |

## 9. Deploy considerations

After Plan F lands, three categories of existing data are stale:

1. **Existing `Call.full_text` is NULL** for all historical rows — adapters never populated it.
   - Vinnova: the adapter does **modified-since** fetching (`vinnova.py:91`, the orchestrator advances `Source.last_fetched_at` on every successful run). Old rows are NOT re-fetched on the next ingest tick — the API will return only programmes modified after the last fetch.
   - **Fix**: reset `Source.last_fetched_at` to NULL for any source whose adapter changed. The orchestrator falls back to epoch when the cursor is NULL (see `orchestrator.py`), guaranteeing every historical row is re-fetched regardless of when it first appeared. One SQL: `UPDATE sources SET last_fetched_at = NULL WHERE slug IN ('vinnova', 'eu_portal')`. Cheap and idempotent. (A hardcoded date would silently miss rows older than that date.)
2. **Existing `CallRanking` rows are stale** — computed against (summary only) under the old prompt. After Plan F's prompt bump to `v3.0`, they should be re-ranked. Trigger `/admin/rerank` once **after** step 1's re-ingest has populated `full_text`. The re-rank enqueues every (call × profile) pair from the last 90 days under the new prompt.
3. **Existing embeddings are stale** — computed against shorter text. `EMBEDDING_MODEL` hasn't changed, so the daily refresh-missing tick won't pick them up.
   - **Add a `--force` flag to the backfill CLI** for one-time post-deploy use. Re-embeds all rows regardless of NULL/mismatched state. Cleaner than bumping `EMBEDDING_MODEL` alias.

The deploy procedure becomes (ORDER MATTERS — read carefully):

```bash
# 1. Schema (no schema deltas from Plan F itself, but always safe)
alembic upgrade head

# 2. Reset incremental-fetch cursors so the next ingest re-fetches the historical window.
#    Without this, Vinnova's modified-since query returns nothing and full_text stays NULL.
psql -c "UPDATE sources SET last_fetched_at = NULL WHERE slug IN ('vinnova', 'eu_portal')"

# 3. Trigger ingest (or wait for the next cron tick at 03:00 / 03:15 Europe/Stockholm).
#    This populates Call.full_text on every existing row via the new adapters.
#    The orchestrator's step 2 (embedding refresh) handles the new content automatically.
python -m call_monitor.ingestion.cli run --source vinnova   # if a CLI entry point exists; else wait for cron
python -m call_monitor.ingestion.cli run --source eu_portal

# 4. Force-re-embed all calls + profiles under the new richer text.
#    Without --force, the tick only re-embeds NULL/mismatched rows; existing ones
#    were already embedded under the shorter (summary-only) text.
python -m call_monitor.embeddings.cli backfill --force

# 5. Re-rank existing calls under the new prompt v3.0.
#    /admin/rerank enqueues all (active_profile × call from last 90 days).
#    Hit this from the admin UI after step 4 completes. The drain tick processes
#    the resulting jobs over the next ~hour at typical scale.

# 6. Restart the app.
systemctl restart call-monitor
```

The `--force` flag is part of Plan F. The ingest CLI may not exist today — if not, the plan should add one (`python -m call_monitor.ingestion.cli run --source <slug>`); else operators wait for the cron tick which is fine. `/admin/rerank` already exists.

## 10. Out of scope (deferred to follow-up specs)

- **Async / parallel scraping** — current per-source ingest is hourly and handles serial scraping cleanly. Async only matters at much higher source counts.
- **Source-specific scraper plugins** (e.g., a custom Trafilatura override per source domain) — adapter authors can add them inline if needed; no framework required.
- **DOCX support** (`python-docx`) — add only when a real source needs it.
- **Caching layer** (`full_text_fetched_at` + skip-if-fresh) — defer until ingest costs grow measurable.
- **OCR for scanned PDFs** — out of scope; would require Tesseract or a cloud OCR. The vast majority of funding PDFs are text-extractable.
- **Source-detail endpoint pre-fetching** for EU portal even if the search API gives enough — defer.
- **HTML/PDF content sanitization** beyond what Trafilatura/PyMuPDF do natively (e.g., HTML entity decoding, footer removal heuristics) — defer until problems surface.

## 11. Decision log

| Question | Decision | Why |
|---|---|---|
| Auto-scrape from orchestrator if adapter left full_text None? | No — adapter calls scraping helpers explicitly | Source-specific intent; auto-scrape would be noisy and over-aggressive |
| Where does fetching happen? | Inside `adapter.fetch()` | Adapter knows the source semantics; orchestrator stays source-agnostic |
| HTML extraction library? | Trafilatura (Apache-2.0) | Best-of-breed for content extraction; well-maintained |
| PDF extraction library? | PyMuPDF (`pymupdf`, AGPL-3.0) | AGPL accepted for internal use; significantly more robust than `pypdf`/`pdfplumber` on complex layouts |
| DOCX library? | None for now | Defer until a source needs it |
| `NormalizedCall.full_text` field? | Already exists in `sources/types.py:42` | No new field; just adopt it |
| `Call.full_text` clobber on transient failure? | **Preserve via `COALESCE(NEW, OLD)` in upsert** | Transient scrape failures must not erase previously-captured body. Trade-off: can't re-NULL via ingest path (operator action needed for removed bodies) |
| LLM prompt: which text fields? | **`summary + full_text` concatenated** (was: OR-pick) | Today's OR-pick would silently drop full_text for any call whose summary is non-empty (i.e., ALL of them) |
| `PROMPT_VERSION` bump? | **`v2.0` → `v3.0`** | Rubric/parser shape unchanged, but input text changes. Prompt-version stamp is the documented audit/A-B mechanism for exactly this case |
| Re-fetch policy? | Always re-fetch on ingest | Simple; cost is acceptable at current scale; revisit with measurement |
| LLM prompt truncation bump? | `CALL_TEXT_LIMIT` 4000 → 16000 | Gemma 4's context window easily handles it; bigger context only when there's body to put in it |
| Embedding truncation? | Stay at 8000 chars | BGE-M3 hard limit |
| Existing Call.full_text refresh after deploy? | **Reset `Source.last_fetched_at`** to force re-ingest | Vinnova is incremental — without resetting the cursor, old rows stay None forever |
| Existing CallRanking refresh? | `/admin/rerank` once post-deploy (after re-ingest populates full_text) | Operator action, not automated; ordering matters |
| Existing embedding refresh? | New `--force` flag on backfill CLI | Cleaner than bumping `EMBEDDING_MODEL` alias |
| New schema columns? | None | `Call.full_text` already exists |
| Helper signature for tests? | Accept `transport: httpx.BaseTransport \| None = None` keyword | Mirrors `OllamaClient` / `OllamaEmbedClient`; test injects `MockTransport` |

## 12. Open questions for the implementation plan

- **EU portal investigation outcome**: does the search API expose richer fields (`description`, `objectives`, etc.)? The plan needs to either (a) use them, or (b) fall back to HTML scrape. Investigation step should run BEFORE the EU portal adapter task.
- **Trafilatura quality on Vinnova/EU portal pages**: should be tested against 2-3 real call URLs in the plan's investigation step. If extraction returns boilerplate noise, the adapter may need a per-source CSS selector hint passed to Trafilatura.
- **PyMuPDF binary wheels**: PyMuPDF ships compiled wheels for common platforms. Confirm the deploy environment can install them (no `gcc`-from-source build needed). If the deploy is a slim Docker image, may need a `Dockerfile` package addition (`apt-get install ...`) — likely none, but verify.
- **Trafilatura's `lxml` dependency**: pulls in libxml2/libxslt at the OS level on some images. Same Docker check as above.
- **Test PDF fixtures**: how do we generate them reproducibly? Options: hand-craft tiny PDFs with PyMuPDF itself in a `conftest.py` fixture; commit pre-built tiny PDFs as binary fixtures; use a real (small) funding-call PDF. Recommend committing 1-2 small PDFs as binary fixtures + 1 generated-on-demand fixture.
- **Ingest CLI**: does an `ingestion.cli` entry point exist for force-running an ingest tick from the command line (per the deploy procedure §9)? If not, the plan should add one — useful both for the post-deploy refetch and for ad-hoc operator debugging. Cheap; ~30 LOC.
- **Re-NULL escape hatch**: the COALESCE upsert pattern (§3.8) means a body that legitimately disappeared from the source can never be re-NULL'd via normal ingest. Acceptable for the rare case, but should the plan add a small admin tool (`/admin/calls/<id>/clear-full-text`) or an SQL snippet in deploy notes? Defer unless an operator asks.
- **`PROMPT_VERSION` and historical comparison**: with `v2.0` rows existing in the DB and `v3.0` rows added going forward, does any read path filter or aggregate by `prompt_version`? (Eval/Kendall's-τ should — verify the eval module doesn't conflate them.)

## 13. After this plan ships

- Vinnova LLM ranking is unchanged in raw quality (same content reaches the LLM, just split across two fields properly).
- EU portal LLM ranking improves on calls where the body wasn't in the API summary — likely the majority of cases.
- New adapters write 5-10 lines for `full_text` population — no new framework to learn, just call one of two helpers.
- The "what does the model actually see?" question becomes answerable per row: `Call.full_text` either has content or has a documented reason for being None (logged in extraction failures).
