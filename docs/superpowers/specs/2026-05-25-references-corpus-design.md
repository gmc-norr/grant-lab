# References Corpus Subsystem — Design

**Status:** Sections 1–4 approved by user (storage/data model, ingestion pipeline, agent tool surface, web UI + cost cap integration). Codex independent review applied as rev2 (see "Revisions" below). Section 5 (slice plan) still pending — will fold back into this spec before an implementation plan is written.

## Revisions

**rev2 (2026-05-25, after Codex review of rev1):**

1. **PageIndex cost is now first-class** (Codex High). Added a `llm_usage` ledger table for non-turn LLM spend (outline generation today; compaction + future server-side LLM use later). `check_daily_token_cap` and `check_global_daily_token_cap` UNION chat_turns and llm_usage. rev1's claim that PageIndex "rides along" with the existing cap was wrong — caps.py sums only `chat_turns.tokens_in/out`, so a non-turn worker had no ledger row.
2. **Generic URL fetch is server-side, not Claude web-fetch** (Codex High). The Claude adapter launches the SDK with `tools=[]` + `strict_mcp_config=True` and an explicit 8-tool `allowed_tools` allowlist — Claude's built-in web_fetch is NOT available to the agent. rev2's `fetch_reference` dispatcher's generic-URL path uses server-side `httpx` with explicit SSRF protections (no private IP ranges, https-only allowed by default, redirect limits, response-size cap). The agent never makes a direct outbound HTTP call.
3. **`kind=data` rows insert with `outline_status='skipped'`** (Codex High). rev1's default of `'pending'` would have populated the admin backlog with rows that the worker explicitly excludes (`WHERE kind != 'data'`), causing a permanently-growing "pending" count. Backlog query also excludes `'skipped'` explicitly.
4. **Split `text_status` from `outline_status`** (Codex Medium). rev1 conflated text-extraction failures with PageIndex-outline failures under a single column; `navigate_reference` could not distinguish "extraction failed, no point retrying" from "outline still indexing". rev2 adds `text_status` + `text_error` for extraction and chunk+embed; `outline_status` + `outline_error` for PageIndex only. Search keys off `text_status`, navigation keys off `outline_status`.
5. **`submit_user_turn` accepts `reference_ids` and pre-counts text tokens** (Codex Medium). Mirrors the B7 pattern that pre-stamps `ChatTurn.tokens_in` with attachment-token estimates so the cap check at submit time reflects the real input cost. Without this, a 5-reference turn (~100k input tokens of injected text) bypasses the cap because submit time only knows about user-typed text.
6. **`reference_chunks.embedding_model` column added** (Codex Medium). Existing `call_section_extracts` and similar tables already store `embedding_model` so a future EMBEDDING_MODEL change doesn't silently mix incompatible vectors. The new table follows the same convention.
7. **`fetch_reference` uses an atomic UPDATE for the per-turn cap** (Codex Open Question). The ToolContext from B5 is per-workspace, not per-turn. The tool handler uses `UPDATE turn_jobs SET fetch_count = fetch_count + 1 WHERE workspace_id = ? AND status = 'running' AND fetch_count < 3 RETURNING fetch_count` — atomic, no race, no need to thread turn_job_id through ToolContext.

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
    -- Text extraction + chunk+embed status (rev2: split from outline status)
    text_status     TEXT NOT NULL DEFAULT 'pending'
                    CHECK (text_status IN ('pending','running','done','failed','skipped')),
    text_error      TEXT,
    text_indexed_at TIMESTAMPTZ,           -- non-null once chunks + embeddings written
    -- PageIndex outline status (separate so navigate_reference can distinguish
    -- "extraction failed" from "outline still indexing")
    outline_status  TEXT NOT NULL DEFAULT 'pending'
                    CHECK (outline_status IN ('pending','running','done','failed','skipped')),
    outline_error   TEXT,
    outline_indexed_at TIMESTAMPTZ,
    uploaded_by     INTEGER REFERENCES users(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_references_ws ON references(workspace_id);
-- Worker backlog index — picks rows that need either text indexing or outline.
-- Excludes 'done', 'failed', 'skipped'. `kind=data` inserts use
-- text_status='skipped' AND outline_status='skipped' so they never appear here.
CREATE INDEX ix_references_indexing_backlog ON references(workspace_id, created_at)
    WHERE text_status IN ('pending','running')
       OR outline_status IN ('pending','running');

CREATE TABLE reference_chunks (
    id              BIGSERIAL PRIMARY KEY,
    reference_id    UUID NOT NULL REFERENCES references(id) ON DELETE CASCADE,
    workspace_id    UUID NOT NULL,                -- denormalized for fast per-workspace search
    chunk_index     INTEGER NOT NULL,
    section_path    TEXT,                         -- e.g., "Methods > Statistical analysis"
    text            TEXT NOT NULL,
    embedding       vector(1024),                 -- mxbai-embed-large dim
    embedding_model TEXT NOT NULL,                -- rev2: model versioning per existing convention
                                                  -- (call_section_extracts and friends already do this)
    UNIQUE (reference_id, chunk_index)
);
CREATE INDEX ix_reference_chunks_ws_embedding ON reference_chunks
    USING hnsw (embedding vector_cosine_ops);

-- Non-turn LLM spend ledger (rev2). The B8 daily-cap query UNIONs chat_turns
-- with this table so PageIndex outline + future compaction calls + server-side
-- web fetches that go through Anthropic land on the cap. Without this,
-- non-turn callers spend tokens that are invisible to the cap.
CREATE TABLE llm_usage (
    id              BIGSERIAL PRIMARY KEY,
    workspace_id    UUID REFERENCES workspaces(id) ON DELETE CASCADE,  -- NULL allowed for
                                                                       -- global system jobs
    kind            TEXT NOT NULL CHECK (kind IN
                    ('outline','compaction','fetch_assist','other')),
    tokens_in       INTEGER NOT NULL DEFAULT 0,
    tokens_out      INTEGER NOT NULL DEFAULT 0,
    model           TEXT,
    related_id      UUID,                         -- e.g. reference_id for kind='outline'
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_llm_usage_ws_created ON llm_usage(workspace_id, created_at);

-- Additive changes to existing tables:
ALTER TABLE turn_jobs ADD COLUMN fetch_count INTEGER NOT NULL DEFAULT 0;
```

**Cap query update (Codex High #1):** `check_daily_token_cap(workspace_id)` and `check_global_daily_token_cap()` in `caps.py` change from summing only `chat_turns` to summing the UNION of `chat_turns.tokens_in/out` and `llm_usage.tokens_in/out` over today's Stockholm-day window. Two-row aggregate inside a single CTE; reuses the existing `_stockholm_day_start_utc()` helper.

Notes:
- `kind=data` rows insert with `text_status='skipped' AND outline_status='skipped'` so they never appear in any backlog query and are never picked by the worker (Codex High #3). Accessed via the `summarize_data` tool, which derives its output on-the-fly from the file on disk + the schema/sample stored in `text.txt`.
- `outline_status` lets `navigate_reference` return a clean "still indexing, retry in 30s" response. `text_status` is the parallel signal for `search_references` (Codex Medium #4).
- `reference_chunks.workspace_id` denormalized so per-workspace search is a single WHERE without a join.
- Table naming (`references` is a noisy word) — Codex didn't object; keep for v1. Singular `reference` remains an option for future renaming if it bites.

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

- `^10\.\d{4,9}/` → DOI → Crossref API → resolved publisher URL → fetch PDF (via the same SSRF-safe httpx as the generic URL path below)
- `^\d{6,9}$` → PMID → PubMed E-utilities → PMC full-text XML if open access; else fetch fails (manual upload required)
- `^\d{4}\.\d{4,5}$` → arXiv → arXiv API → PDF URL
- `^https?://` → **server-side httpx fetch with SSRF protections** (Codex High #2 — NOT Claude web-fetch; the SDK launches with `tools=[]` + `strict_mcp_config=True` and the agent has no built-in web access). The same fetcher is used regardless of caller (agent MCP tool, admin/UI button, dispatcher resolution of Crossref-resolved URLs).

**SSRF protections (single shared `fetcher.py` module):**

- Scheme allowlist: `https://` by default, `http://` only when `REFERENCES_ALLOW_HTTP=true` (env, off by default). All other schemes (`file://`, `gopher://`, `data:`, ...) rejected.
- DNS resolution + IP allowlist: resolve hostname, reject if any returned address falls in a private/internal range (RFC1918 10/8, 172.16/12, 192.168/16; loopback 127/8 + ::1; link-local 169.254/16 + fe80::/10; ULA fc00::/7; CGNAT 100.64/10). Re-checked on every redirect target.
- Redirect limit: max 5 redirects, each one validated by the scheme/IP rules before following.
- Response-size cap: same 50 MB ceiling as the upload path (configurable via `REFERENCES_MAX_BYTES`).
- Timeout: 15s connect, 60s total.
- Content-Type sniff: if the response is `text/html` we accept it (some publishers redirect HTML wrappers around PDFs and the agent may want the HTML), but `script` and `iframe` content is stripped during text extraction.

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

### Stage 1 — Text extract + section-aware chunk + embed (sync, ~1-5s per paper)

Section structure is detected first:
- PDFs: outline via PyMuPDF; if absent, headings via pdftotext layout (font-size heuristic).
- Markdown: H1/H2/H3 headers.
- docx: heading styles (`Heading 1`, `Heading 2`, ...).

One chunk per section. If any section exceeds 1500 chars, sub-chunk recursively with 100-char overlap. `section_path` populated as breadcrumb (`"Methods > Statistical analysis"`). Falls back to plain recursive 1000-char chunks when no structure is detectable.

Embeddings via `OllamaEmbedClient(model=settings.embedding_model)` — already in the stack from call ranking. Each row stores `embedding_model` so a future model change doesn't silently mix incompatible vectors (Codex Medium #6). Batch insert into `reference_chunks` then UPDATE `references.text_status='done'`, `text_indexed_at = now()` in one transaction. ~30-60 chunks × ~50ms per local embedding call → 1.5–5s total for a 20-page paper. Fits in the request budget.

On any failure (extractor crash, Ollama unreachable, chunk insert error): UPDATE `text_status='failed'`, `text_error=<message>`, leaving bytes on disk. The worker (Stage 2) retries text+embed when it sees `text_status='failed'` AND `text_error LIKE 'transient:%'` (Ollama-down style), or leaves the row for the user to retry manually otherwise.

### Stage 2 — Async PageIndex outline + text-stage retry (~10-30s per paper)

New scheduler tick `_reference_indexing_tick`, mirroring B8's `_phase0_tick` + `_compaction_tick`:

- Interval `REFERENCE_INDEXING_TICK_SECONDS=30` (env, default 30s).
- **Gated by `DRAFTING_ENABLED`** — kill switch stops it (it spends Anthropic tokens via PageIndex; Stage-1 retries don't, but a single gate keeps the operational story simple).
- Two responsibilities per tick:
  1. **Stage-1 retry:** picks rows with `text_status='failed' AND text_error LIKE 'transient:%'`, runs the Stage-1 pipeline again. Local Ollama only — no Anthropic cost. Cap-irrelevant.
  2. **PageIndex outline:** picks up to N=5 rows with `outline_status='pending' AND kind != 'data' AND text_status='done'` (must have text first). For each: flip outline_status to `'running'`, invoke PageIndex against `original.<ext>`, write `outline.json`, flip to `'done'`, write the LLM usage to `llm_usage(kind='outline', related_id=ref_id, tokens_in/out=...)`. On exception: flip to `'failed'`, store error in `outline_error`.

**Cost accounting (Codex High #1, fixed):** PageIndex calls Anthropic. The worker writes a `llm_usage` row immediately on completion with the actual `tokens_in/tokens_out` returned by the SDK (same parsing as `claude_adapter`'s post-b8-residuals-2 fix). The B8 cap query unions chat_turns and llm_usage, so the outline cost counts.

**Cap-exhausted behavior:** the worker calls `check_global_daily_token_cap` BEFORE each PageIndex invocation. If over, leaves row in `'pending'` and logs (no infinite retry storm; no charge incurred for a request not made). Next day's first tick picks it up.

### Error handling summary

| Stage | Failure | Behavior |
|---|---|---|
| Upload validate | bad MIME / oversize | 400 with `_html.escape`-d message, no row, no file |
| Upload commit | DB error post-write | Rollback row, unlink file (B7 pattern) |
| Fetch | paywall / 404 / timeout | Tool error to agent; HTTP 502 to UI. No row inserted. |
| Text extract (Stage 1) | pdftotext crash / corrupted PDF | Row exists, `text_status='failed'`, `text_error=<exception>`. UI surfaces "extraction failed — re-upload or convert to txt". `search_references` won't find it; `navigate_reference` reports the text failure clearly. |
| Chunk + embed (Stage 1) | Ollama transient down | Row exists, `text_status='failed'`, `text_error='transient: <message>'`. The Stage-2 worker tick retries Stage 1 on next pass. `search_references` returns "still indexing". |
| PageIndex outline (Stage 2) | Anthropic 5xx / timeout / parse error | Row → `outline_status='failed'`, `outline_error=<message>`. `navigate_reference` returns "outline failed: <error>". User can retry from UI. `search_references` still works because Stage 1 succeeded. |
| Cap exhausted | global cap hit during Stage 2 | Row stays `outline_status='pending'`; tick logs + skips. No Anthropic call made. Next day's first tick retries. |
| Mid-turn 4th fetch | atomic UPDATE returns 0 rows (fetch_count already 3) | Tool returns error ("ask user to upload"); agent surfaces in response. Cap is per-turn, not per-workspace-per-day, so the next turn's fetches start fresh. |

---

## Section 3 — Agent MCP tool surface

Six new tools added to the existing MCP registry from B5 (which launches the SDK with `tools=[]` + `strict_mcp_config=True` + explicit `allowed_tools` list — the security boundary). All scoped to the agent's workspace.

### `list_references(kind=?, q=?, limit=50)`

Browse-only metadata listing. Returns `[{id, kind, title, filename, outline_status, created_at}, ...]`. Filter by kind and title substring. Token-cheap. Used at start of a turn to orient.

### `search_references(query, top_k=8, kind=?)`

Semantic search over `reference_chunks`. Embed `query` with the current `settings.embedding_model` (filter chunks WHERE `embedding_model = ?` so model-version drift is invisible — incompatible vectors are simply not searched). HNSW lookup with `workspace_id` filter. Returns `[{reference_id, title, section_path, snippet, score}, ...]`. Default `top_k=8`, hard cap 25. Skips references with `text_status != 'done'` (they're either pending, failed, or skipped — none searchable).

### `navigate_reference(reference_id, section_path=None)`

PageIndex outline traversal. With no `section_path`: returns the outline JSON. With `section_path`: returns the full text of that section. Status handling (Codex Medium #4):
- `outline_status='done'` → normal return
- `outline_status IN ('pending','running')` → `{"status": "indexing", "retry_after_seconds": 30}`
- `outline_status='failed'` → `{"status": "failed", "error": outline_error, "fallback": "use read_reference(...)"}` (distinct from indexing so the agent stops polling)
- `outline_status='skipped'` (data kind) → `{"status": "skipped", "kind": "data", "fallback": "use summarize_data(...)"}`

Tool docstring explicitly warns against retry-storming when status is `indexing`.

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

Agent-side mid-turn fetch. Uses the same dispatcher as the HTTP route. **Per-turn cap = 3**, enforced atomically (Codex Open Question resolution):

```sql
UPDATE turn_jobs
   SET fetch_count = fetch_count + 1
 WHERE workspace_id = :workspace_id
   AND status = 'running'
   AND fetch_count < 3
RETURNING fetch_count;
```

The MCP handler runs this BEFORE the fetch. If `RETURNING` is empty (no running turn OR cap reached) → return error to the agent. No race possible; the partial unique index on `turn_jobs(workspace_id) WHERE status='running'` (from B6) guarantees at most one running turn per workspace, so the UPDATE targets exactly one row.

If the dispatcher's generic-URL path lands on Claude's web fetch indirectly (it doesn't in v1; rev2 settled this as server-side httpx) and incurs Anthropic spend, that spend lands in `llm_usage(kind='fetch_assist')`. v1 has no such spend — the fetcher uses local httpx — so no `llm_usage` row is written from this path.

Returns the new `Reference` row partial once bytes are on disk + Stage 1 complete. PageIndex outline queued async — agent can `search_references` immediately, `navigate_reference` returns "indexing" until the worker catches up.

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

**`submit_user_turn` extension for pre-enqueue accounting (Codex Medium #5):**

B7's cap is enforced BEFORE the turn is queued by pre-stamping `ChatTurn.tokens_in` from attachment-token estimates. The same pattern is required for reference inclusions or a 5-reference turn (~100k input tokens of injected text) bypasses the cap.

`submit_user_turn`'s signature grows `reference_ids: list[uuid.UUID] = []` alongside the existing `attachment_ids`. Inside the FOR UPDATE row lock and BEFORE the cap check:

1. Resolve each `reference_id` to its `text_status` and approximate text length. For included refs, the truncation budget per ref is `max_chars_per_ref` (currently 20_000, matching `read_reference`).
2. `reference_tokens_estimate = sum(min(ref.text_chars_total, max_chars_per_ref) // 4 for ref in resolved)`.
3. Add to `chat_turn.tokens_in` alongside `attachment_tokens_estimate + len(user_text) // 4`. The combined sum reflects the real input cost the cap should see.
4. If `text_status != 'done'` for any included ref: reject the turn with a clear `ValueError("reference X is still indexing; please wait")`. Submitting against a still-indexing ref would inject empty content, which the agent would notice but the cap wouldn't.

The combined estimate flows into the existing `ChatTurn.tokens_in` field. B7-residuals-2's "NULL on finalize" rule (so the SDK-true count overrides the pre-stamp after the turn completes) still applies — the pre-stamp's only job is to make the cap check honest at submit time.

**Backward-compat change to B7's per-turn cap:** the existing 5-per-turn limit on image attachments becomes a **combined** 5-per-turn limit on attachments + reference inclusions (not 5 of each). Rationale: both consume the same kind of per-turn input-token budget, and the agent can already trade off ("attach this image OR pull this reference"); doubling the surface to 10 would weaken the cost guard B7 established. The error message and the over-cap test from B7 need to be updated to reflect this. `MAX_ATTACHMENTS_PER_TURN` constant is renamed to `MAX_TURN_INCLUSIONS = 5` and the check becomes `len(attachment_ids) + len(reference_ids) > 5`.

### Admin panel additions

`/admin/drafting` (B8) gets two new sections at the bottom:

- **Reference indexing backlog:** count of `references` rows WHERE `text_status IN ('pending','running')` OR `outline_status IN ('pending','running')` across all workspaces, with the oldest pending timestamp. Stale (>1h) entries highlighted — signal that the indexing worker is stuck. Two sub-counts surfaced (text backlog, outline backlog) since they fail for different reasons. `kind=data` rows are excluded (they're `'skipped'`).
- **Mid-turn fetch frequency:** count of `turn_jobs.fetch_count > 0` rows from today. Helps spot heavy-fetch agents.

Both read-only — no remediation UI in v1.

### Cost cap integration

- **No new cap surface, but `caps.py` queries change** (Codex High #1). The B8 daily-cap queries (`check_daily_token_cap`, `check_global_daily_token_cap`) currently sum only `chat_turns.tokens_in/out`. rev2 changes them to sum `chat_turns ∪ llm_usage` for the same Stockholm-day window. PageIndex outline tokens written by the Stage-2 worker land in `llm_usage(kind='outline')` and become visible to both caps.
- **Reference inclusions pre-counted** at submit time (Codex Medium #5; see Section 4 `submit_user_turn` extension above). A 5-reference turn injecting ~100k chars of reference text contributes its `len // 4` estimate to `ChatTurn.tokens_in` BEFORE the cap check.
- **Per-turn fetch cap = 3.** Bounded atomically by the `UPDATE ... RETURNING fetch_count` pattern in Section 3.
- **Outline worker honors global cap** before each PageIndex call (skip + log if exceeded — no Anthropic call, no `llm_usage` row).
- **Embedding cost = 0 external.** Local Ollama. Intentionally not counted (the cap protects external spend).
- **Generic URL fetch cost = 0 external** in v1 (server-side httpx, Codex High #2). If a future variant routes fetches through an Anthropic-backed path, those tokens land in `llm_usage(kind='fetch_assist')` and become cap-visible.

---

## Open questions / pending review

1. **Section 5 — slice plan** is not yet drafted. The build order is provisionally 4–5 slices: (1) `Reference` model + manual upload + storage, (2) ingestion pipeline (text extraction + chunk/embed + pgvector reuse), (3) PageIndex async indexing, (4) auto-fetch sources (Crossref + PubMed + arXiv + Web), (5) agent MCP tools + web UI + chat composer integration. Final ordering depends on dependency analysis during plan writing.

2. **Codex independent review applied as rev2.** Seven findings (3 High + 3 Medium + 1 Open Question) all addressed inline; see the "Revisions" section at the top of this spec for the per-fix summary and where in the design each landed. Items NOT raised by Codex that remain genuine open areas:
   - Cost realism: hundreds of PageIndex calls/day across workspaces. Should we cap outline generation per-workspace per-day on top of the existing daily token cap? Skip outline for some kinds (e.g., kind=analysis since typically small)? Slice-plan decision.
   - Backup strategy for `reference_chunks` as embeddings accumulate (potentially hundreds of MB per workspace after a few months of use). pg_dump weight + restore time considerations. Belongs in an ops doc, not a code slice.
   - Retention policy for failed-extract rows. Currently they stay forever; admin would have to delete manually. Backlog feature.
   - Alert hooks when outline backlog exceeds threshold. B8 set the pattern but didn't ship alerting; this slice doesn't either. Backlog.

3. **`references` table name** — Codex didn't object. Keeping for v1.

4. **PageIndex install path** — design assumes PageIndex is installable as a Python dep + LLM-backed CLI. Concrete install + setup steps belong in the slice plan.

5. **Backward-compat for existing workspaces.** All existing workspaces predate this subsystem; the migration adds the new tables (`references`, `reference_chunks`, `llm_usage`) and the new `turn_jobs.fetch_count` column (NOT NULL default 0 — additive, no backfill needed).
