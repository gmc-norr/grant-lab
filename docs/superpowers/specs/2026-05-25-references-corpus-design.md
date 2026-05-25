# References Corpus Subsystem — Design

**Status:** Sections 1–4 approved by user (storage/data model, ingestion pipeline, agent tool surface, web UI + cost cap integration). Section 5 (slice plan) and Codex independent review still pending — both will fold back into this spec before an implementation plan is written.

**Goal:** Per-workspace reference corpus the proposal-assist drafting agent can browse, semantically search, and navigate into. Supports four content types (external papers, prior grant proposals, internal analyses, data files), two ingestion paths (manual upload + auto-fetch from DOI/PubMed/arXiv/web), and copy-on-add cross-workspace sharing.

**Scope:** This spec replaces / supersedes any earlier informal references-handling assumptions in the proposal-assist arc. It is layered on top of the merged proposal-assist subsystem (B3–B8 + residuals). v1 scale target: hundreds of references per workspace.

---

## Background — patterns this subsystem builds on

The new code must be consistent with patterns landed across the proposal-assist arc.

**B5 — per-workspace git repo:** each workspace owns `var/workspaces/<workspace_id>/repo/` containing `proposal.md`, `proposal-spec.md`, `proposal-plan.md`. The Claude SDK subprocess runs with `cwd=repo_dir`.

**B7 — image attachments + adapter contract:** filesystem layout `var/workspaces/<workspace_id>/attachments/<attachment_uuid>.<ext>`. The Claude adapter (`drafting/claude_adapter.py`) stays opaque about the DB and filesystem — `AgentHandle.state: dict[str, Any]` is private to the adapter, no public fields are added by feature code. A separate `attachments/resolver.py` (called by `turns/processor.py` BEFORE invoking the adapter) does DB lookups + disk reads + content-block construction. The adapter receives already-API-shape content via the SDK's `AsyncIterable[dict]` query path. This is a load-bearing pattern from the post-B7 Codex review and the new subsystem MUST honor it.

**B7 — file panel patterns:** `/workspaces/<id>/files` GET (owner+viewer), POST upload (owner-only, atomic ordering = tmp+fsync+os.replace, INSERT, commit, on-failure cleanup), GET serve bytes (owner+viewer, cache-private, sanitized Content-Disposition), POST delete (owner-only, row-first then file). MIME validated via content_type + Pillow probe (defense-in-depth).

**B7 — submit_user_turn:** holds a `SELECT ... FOR UPDATE` row lock on the workspace BEFORE the cap check (B6-residuals-2 fix for cap TOCTOU). NULL `tokens_estimate` triggers re-probe + persist on first use (never silent zero). All HTMLResponse error paths use `_html.escape` (B7-residuals fix).

**B8 — operational safety:** `DRAFTING_ENABLED=false` gates POST /turns (503), the drain tick, the Phase 0 tick, and the compaction tick. Global daily token cap (`GLOBAL_DAILY_TOKEN_CAP_INPUT/OUTPUT` defaults 5M/1M) sums across all workspaces; raised as `GlobalTokenCapExceeded` → 429 with `X-Token-Cap: global` header. Read-only `/admin/drafting` admin panel surfaces kill-switch state, global cap usage, per-workspace usage, last 50 failed/cancelled events.

**B8-residuals-2 — token counting:** the adapter sums `input_tokens + cache_creation_input_tokens + cache_read_input_tokens` from `ResultMessage.usage` (the authoritative final tally). The cap reflects real spend; an earlier bug under-counted by ~99%.

**Audit invariant:** every turn-lifecycle state-change writes a `WorkspaceEvent` row (`cloud_turn_sent` at submit, then exactly one of `cloud_turn_completed` / `cloud_turn_failed` / `cloud_turn_cancelled`). `actor_id` is populated wherever a user is identifiable.

---

## User-confirmed design choices (made during brainstorm)

| Question | Choice |
|---|---|
| Content types in v1 | All four: papers, prior proposals, analyses, data files |
| Cross-workspace sharing | Copy-on-add (snapshot). No shared library. |
| Agent interface | Browse + semantic search + PageIndex navigation. All three. |
| Auto-fetch sources | Crossref (DOI) + PubMed (PMID/PMC XML) + arXiv + Anthropic Web Fetch |
| Who can add refs | Both user (UI upload) and agent (mid-turn `fetch_reference`) |
| Embedding model | Local Ollama `mxbai-embed-large` (matches dev .env, avoids bge-m3 NaN bug) |
| Chunker | Section-aware (PDF outline / markdown headings / docx heading styles); fallback to recursive 1000-char with 100-char overlap |
| Mid-turn fetch cap | 3 per turn (counter on turn_jobs row) |

---

## Section 1 — Data model + storage layout

### Storage layout

```
var/workspaces/<workspace_id>/
├── repo/                          (B5 — proposal draft, git-backed)
├── attachments/                   (B7 — chat-attached images)
└── references/                    (NEW)
    └── <reference_id>/
        ├── original.<ext>         (uploaded/fetched bytes — pdf, docx, md, csv, ...)
        ├── text.txt               (extracted plain text — input for chunk+embed)
        └── outline.json           (PageIndex output, written when async job completes)
```

### Database schema

```sql
CREATE TABLE references (
    id              UUID PRIMARY KEY,
    workspace_id    UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL CHECK (kind IN ('paper','prior_proposal','analysis','data')),
    source_type     TEXT NOT NULL CHECK (source_type IN ('uploaded','crossref','pubmed','arxiv','web')),
    source_uri      TEXT,                  -- DOI / PMID / arXiv id / URL / NULL for uploads
    title           TEXT,                  -- pulled from metadata / first line / filename
    filename        TEXT NOT NULL,         -- sanitized original filename
    storage_path    TEXT NOT NULL,         -- relative path under workspace
    mime_type       TEXT NOT NULL,
    bytes_total     INTEGER,
    text_indexed_at TIMESTAMPTZ,           -- non-null once chunks + embeddings written
    outline_indexed_at TIMESTAMPTZ,        -- non-null once PageIndex outline written
    outline_status  TEXT NOT NULL DEFAULT 'pending'
                    CHECK (outline_status IN ('pending','running','done','failed','skipped')),
    outline_error   TEXT,
    uploaded_by     INTEGER REFERENCES users(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_references_ws ON references(workspace_id);
CREATE INDEX ix_references_outline_pending ON references(workspace_id)
    WHERE outline_status IN ('pending','running');

CREATE TABLE reference_chunks (
    id              BIGSERIAL PRIMARY KEY,
    reference_id    UUID NOT NULL REFERENCES references(id) ON DELETE CASCADE,
    workspace_id    UUID NOT NULL,                -- denormalized for fast per-workspace search
    chunk_index     INTEGER NOT NULL,
    section_path    TEXT,                         -- e.g., "Methods > Statistical analysis"
    text            TEXT NOT NULL,
    embedding       vector(1024),                 -- mxbai-embed-large dim
    UNIQUE (reference_id, chunk_index)
);
CREATE INDEX ix_reference_chunks_ws_embedding ON reference_chunks
    USING hnsw (embedding vector_cosine_ops);

-- Additive change to existing table:
ALTER TABLE turn_jobs ADD COLUMN fetch_count INTEGER NOT NULL DEFAULT 0;
```

Notes:
- `kind=data` rows have `text_indexed_at = outline_indexed_at = NULL` forever — accessed via a separate `summarize_data` tool, not via search.
- `outline_status` lets `navigate_reference` return a clean "still indexing, retry in 30s" response.
- `reference_chunks.workspace_id` denormalized so per-workspace search is a single WHERE without a join.
- Table naming (`references` is a noisy word) is flagged for Codex review.

### Cross-workspace copy

`POST /workspaces/<dst>/references/copy_from?source_ref_id=<id>`. Server resolves the source ref (must be visible to caller as owner of source workspace), copies bytes to the new workspace's `references/<new_id>/`, inserts a fresh `references` row + duplicates chunks. No shared state — independent storage and indexing per workspace. Clean semantics.

---

## Section 2 — Ingestion pipeline

Two entry points (upload + fetch) feed one shared indexing pipeline with two stages (sync RAG + async PageIndex).

```
[upload (multipart)] ──┐
                       ├──> [bytes on disk + Reference row]
[fetch dispatcher] ────┘             │
                                     ▼
                          [extract text (by mime)]
                                     │
                                     ▼
                       [chunk + embed (sync, ~1-5s)]
                                     │
                                     ▼
                        [PageIndex outline job queued]
                                     │
                                     ▼
                       [async worker (scheduler tick)]
                                     │
                                     ▼
                  [outline.json + outline_status='done']
```

### Upload entry point

`POST /workspaces/<id>/references` (multipart). Owner-only. Atomic ordering identical to B7 attachments:

1. Validate MIME against allowlist: pdf, md, txt, docx, rtf, csv, xlsx, ipynb.
2. Stream-read with 50 MB cap (papers are bigger than B7's 5 MB image cap; configurable env `REFERENCES_MAX_BYTES`).
3. Write atomically (temp + `os.replace`) to `references/<new_id>/original.<ext>`.
4. Insert `Reference` row with `outline_status='pending'`.
5. Commit; on commit failure → unlink file.
6. Synchronously run text-extraction + chunk + embed (Stage 1 below).
7. Return 200 with the new `Reference` row partial for HTMX swap.

### Auto-fetch dispatcher

Shared backend for the MCP tool + the HTTP route.

```python
fetch_reference(input: str) -> Reference
# input: DOI ("10.1056/NEJMms1411398"), PMID ("12345678"),
# arXiv id ("2401.12345"), or any https:// URL.
```

Detection by regex, in order:

- `^10\.\d{4,9}/` → DOI → Crossref API → resolved publisher URL → fetch PDF
- `^\d{6,9}$` → PMID → PubMed E-utilities → PMC full-text XML if open access; else fetch fails (manual upload required)
- `^\d{4}\.\d{4,5}$` → arXiv → arXiv API → PDF URL
- `^https?://` → generic HTTP fetch (Claude SDK's web-fetch tool when called from the agent path; plain `httpx` from the admin/UI path)

Each backend returns `(bytes, suggested_filename, source_metadata)`. The dispatcher writes via the same write_atomic helper, then runs the same indexing pipeline. Failed fetches return a clear error (no Reference row inserted) — the agent surfaces it as a tool error.

### Text extraction (by MIME)

| MIME / Kind | Extraction |
|---|---|
| `application/pdf` | `pdftotext -layout` (poppler-utils — new docker dep) or PyMuPDF for outline + text |
| `text/markdown`, `text/plain` | Copy as-is |
| `application/vnd.openxmlformats-officedocument.wordprocessingml.document` | `python-docx` → plain text + heading metadata |
| `application/rtf` | `striprtf` (small dep) |
| `text/csv`, `application/vnd.ms-excel` | **Schema + sample only**: column names + first 5 rows. Stored as a small Markdown table in `text.txt`. |
| `application/x-ipynb+json` | markdown cells + first 200 chars of each code cell + text outputs; skip raw image cells |

Failures here mark the Reference with `outline_status='failed'` and surface a clear error to the user — bytes stay on disk for re-extraction after a fix.

### Stage 1 — Section-aware chunk + embed (sync, ~1-5s per paper)

Section structure is detected first:
- PDFs: outline via PyMuPDF; if absent, headings via pdftotext layout (font-size heuristic).
- Markdown: H1/H2/H3 headers.
- docx: heading styles (`Heading 1`, `Heading 2`, ...).

One chunk per section. If any section exceeds 1500 chars, sub-chunk recursively with 100-char overlap. `section_path` populated as breadcrumb (`"Methods > Statistical analysis"`). Falls back to plain recursive 1000-char chunks when no structure is detectable.

Embeddings via `OllamaEmbedClient(model="mxbai-embed-large")` — already in the stack from call ranking. Batch insert into `reference_chunks` then UPDATE `references.text_indexed_at = now()` in one transaction. ~30-60 chunks × ~50ms per local embedding call → 1.5–5s total for a 20-page paper. Fits in the request budget.

### Stage 2 — Async PageIndex outline (~10-30s per paper)

New scheduler tick `_reference_outline_tick`, mirroring B8's `_phase0_tick` + `_compaction_tick`:

- Interval `REFERENCE_OUTLINE_TICK_SECONDS=30` (env, default 30s).
- **Gated by `DRAFTING_ENABLED`** — kill switch stops it (it spends Anthropic tokens).
- Picks up to N=5 rows with `outline_status='pending' AND kind != 'data'`.
- For each: flip to `'running'`, invoke PageIndex against `original.<ext>`, write `outline.json`, flip to `'done'`.
- On exception: flip to `'failed'`, store error in `outline_error`.

**Cost accounting:** PageIndex calls Anthropic. Token usage flows through the same `tokens_in/tokens_out` counting as agent turns (post-b8-residuals-2 fix). Hits the same per-workspace + global daily caps from B8.

**Cap-exhausted behavior:** the worker checks the global cap (`check_global_daily_token_cap`) before each PageIndex call. If over, leaves row in `'pending'` and logs (no infinite retry storm). Next day's first tick picks it up.

### Error handling summary

| Stage | Failure | Behavior |
|---|---|---|
| Upload validate | bad MIME / oversize | 400 with `_html.escape`-d message, no row, no file |
| Upload commit | DB error post-write | Rollback row, unlink file (B7 pattern) |
| Fetch | paywall / 404 / timeout | Tool error to agent; HTTP 502 to UI. No row inserted. |
| Text extract | pdftotext crash / corrupted PDF | Row exists, `outline_status='failed'`, surface in UI as "extraction failed — re-upload or convert to txt" |
| Chunk + embed | Ollama down at upload time | Row exists with `text_indexed_at=NULL`; the outline tick (which already loops through pending rows) also retries chunk+embed when it finds `text_indexed_at IS NULL` — a small extra responsibility on the same worker. Search returns "indexing in progress" for these rows. |
| PageIndex outline | Anthropic 5xx / timeout | Row → `'failed'` with error; user can retry from UI |
| Cap exhausted | global cap hit during outline tick | Row stays `'pending'`; tick logs + skips |
| Mid-turn 4th fetch | over the 3/turn cap | Tool returns error ("ask user to upload"); agent surfaces in response |

---

## Section 3 — Agent MCP tool surface

Six new tools added to the existing MCP registry from B5 (which launches the SDK with `tools=[]` + `strict_mcp_config=True` + explicit `allowed_tools` list — the security boundary). All scoped to the agent's workspace.

### `list_references(kind=?, q=?, limit=50)`

Browse-only metadata listing. Returns `[{id, kind, title, filename, outline_status, created_at}, ...]`. Filter by kind and title substring. Token-cheap. Used at start of a turn to orient.

### `search_references(query, top_k=8, kind=?)`

Semantic search over `reference_chunks`. Embed `query` with `mxbai-embed-large`, HNSW lookup with `workspace_id` filter. Returns `[{reference_id, title, section_path, snippet, score}, ...]`. Default `top_k=8`, hard cap 25.

### `navigate_reference(reference_id, section_path=None)`

PageIndex outline traversal. With no `section_path`: returns the outline JSON. With `section_path`: returns the full text of that section. If `outline_status != 'done'`: returns `{"status": "indexing", "retry_after_seconds": 30}`. Tool docstring explicitly warns against retry-storming.

### `read_reference(reference_id, max_chars=20000)`

Full extracted text. Truncates at 20,000 chars with a "use navigate_reference for the rest" note. For short refs or when navigation can't structure-pick.

### `summarize_data(reference_id)`

For `kind=data` rows only. Returns:

```json
{
  "filename": "patients_2023.csv",
  "schema": [{"column": "patient_id", "type": "int"}, ...],
  "row_count": 1247,
  "sample": "<first 5 rows as markdown table>"
}
```

The agent uses this to know what's in a dataset without loading rows. No SQL execution in v1 (sandboxing concerns) — that's a separate subsystem.

### `fetch_reference(input)`

Agent-side mid-turn fetch. Uses the same dispatcher as the HTTP route. **Per-turn cap = 3,** enforced via `turn_jobs.fetch_count` (incremented in the tool handler; raises sentinel error at 4th attempt). On cap-hit the tool returns an error telling the agent to surface "user, please upload these manually" in its response.

Returns the new `Reference` row partial once bytes are on disk + RAG chunks are indexed (Stage 1 complete). PageIndex outline queued async — agent can `search_references` immediately, `navigate_reference` returns "indexing" until the worker catches up.

---

## Section 4 — Web UI + cost cap integration

### Web UI

Mirrors the B7 files panel.

**`GET /workspaces/<id>/references`** — owner+viewer (viewers can read, like attachments). Renders `workspace_references.html`:

```
┌─────────────────────────────────────────────────────────────────┐
│ References for "Smoke Test B8"                                  │
├─────────────────────────────────────────────────────────────────┤
│ [📎 Upload PDF/Word/Markdown/CSV/Notebook] [+ Fetch by DOI/URL] │
├─────────────────────────────────────────────────────────────────┤
│ Filter: [all ▾]  [search title...]                              │
├─────────────────────────────────────────────────────────────────┤
│ □ paper      NEJM perspective on AI in pathology   ✓ indexed    │
│ □ paper      arXiv 2401.12345 — vision transformers ⏳ outline pending │
│ □ prior      2023 NIH R01 application              ✓ indexed    │
│ □ analysis   2024-Q3 cohort baseline analysis      ✓ indexed    │
│ □ data       patients_2023.csv (1247 rows)         ─ schema only│
│ □ paper      Smith et al 2020 (fetch failed: 404)  ✗ retry     │
└─────────────────────────────────────────────────────────────────┘
[Copy selected to another workspace ▾] [Delete selected]
```

**Routes:**
- `GET /workspaces/<id>/references` — list page (owner+viewer)
- `POST /workspaces/<id>/references` — multipart upload (owner-only)
- `POST /workspaces/<id>/references/fetch` — form with `input` field; calls the dispatcher (owner-only)
- `POST /workspaces/<id>/references/<ref_id>/delete` — row-first delete + idempotent unlink (owner-only)
- `POST /workspaces/<id>/references/<ref_id>/copy_to` — form with `target_workspace_id` (owner of both)
- `GET /workspaces/<id>/references/<ref_id>/raw` — serve original bytes, auth-checked, sanitized Content-Disposition (owner+viewer)

### Chat composer extension

The existing `workspace_main.html` composer's `<details>` block (currently shows attachable images) gains a second section for attachable references — a checkbox list of `kind=paper` and `kind=prior_proposal` rows. Selected refs land in `reference_ids[]` form field.

The processor's content resolver (B7's `attachments/resolver.py`) gains a sibling resolver for references that injects `<system>Reference: <title>\n\n<truncated text>...</system>` blocks into the user message.

**Backward-compat change to B7's per-turn cap:** the existing 5-per-turn limit on image attachments becomes a **combined** 5-per-turn limit on attachments + reference inclusions (not 5 of each). Rationale: both consume the same kind of per-turn input-token budget, and the agent can already trade off ("attach this image OR pull this reference"); doubling the surface to 10 would weaken the cost guard B7 established. The error message and the over-cap test from B7 need to be updated to reflect this.

### Admin panel additions

`/admin/drafting` (B8) gets two new sections at the bottom:

- **Reference indexing backlog:** count of `references.outline_status IN ('pending','running')` across all workspaces, with the oldest pending timestamp. Stale (>1h) entries highlighted — signal that the outline worker is stuck.
- **Mid-turn fetch frequency:** count of `turn_jobs.fetch_count > 0` rows from today. Helps spot heavy-fetch agents.

Both read-only — no remediation UI in v1.

### Cost cap integration

- **No new cap.** PageIndex outline tokens, agent-turn tokens with reference inclusions, and `fetch_reference` (which uses Claude web-fetch from the agent path) all flow through B8's per-workspace + global daily caps via the existing `tokens_in/tokens_out` accounting.
- **Per-turn fetch cap = 3.** Bounded directly by `turn_jobs.fetch_count`.
- **Outline worker honors global cap** before each PageIndex call (skip + log if exceeded).
- **Embedding cost = 0 external.** Local Ollama. Intentionally not counted (the cap protects external spend).

---

## Open questions / pending review

1. **Section 5 — slice plan** is not yet drafted. The build order is provisionally 4–5 slices: (1) `Reference` model + manual upload + storage, (2) ingestion pipeline (text extraction + chunk/embed + pgvector reuse), (3) PageIndex async indexing, (4) auto-fetch sources (Crossref + PubMed + arXiv + Web), (5) agent MCP tools + web UI + chat composer integration. Final ordering depends on dependency analysis during plan writing.

2. **Codex independent review pending.** Specific areas flagged for outside-eye review:
   - Cross-slice consistency with B5/B7/B8 patterns (AgentHandle opacity, row-first delete, atomic write+rollback, B8 cap integration).
   - Concurrency: mid-turn fetch then immediate search by the agent — is the "Stage 1 complete before fetch returns" guarantee robust? Outline tick + chat tick both honoring global cap — can they together overshoot since we don't pre-reserve tokens?
   - Security: agent-controlled `fetch_reference(input)` accepts arbitrary URLs (SSRF via `http://169.254.169.254/...`, resource exhaustion via huge PDFs behind small URLs, malformed PDF crashing the worker, snippets injected via HTML responses from generic URL fetch).
   - Storage/naming: `references` as a table name (most projects use singular `reference`); pg_dump weight as `reference_chunks` grows with embeddings.
   - Cost realism: hundreds of PageIndex calls/day across workspaces. Should we cap outline generation per-workspace per-day? Skip outline for some kinds (e.g., kind=analysis since typically small)?
   - B8-style ops safety: backup strategy for embedding-heavy table, retention policy for failed-extract rows, alert hooks when outline backlog exceeds threshold.

3. **`references` table name** — placeholder. May change to `reference` (singular) per common convention pending Codex feedback.

4. **PageIndex install path** — design assumes PageIndex is installable as a Python dep + LLM-backed CLI. Concrete install + setup steps belong in the slice plan.

5. **Backward-compat for existing workspaces.** All existing workspaces predate this subsystem; the migration adds the new tables and the new `turn_jobs.fetch_count` column (nullable-friendly default 0). No data backfill required.
