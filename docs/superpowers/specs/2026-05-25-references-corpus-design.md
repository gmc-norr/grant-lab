# References Corpus Subsystem — Design

**Status:** Sections 1–4 approved by user (storage/data model, ingestion pipeline, agent tool surface, web UI + cost cap integration). Codex independent review applied as rev2 (see "Revisions" below). Section 5 (slice plan) still pending — will fold back into this spec before an implementation plan is written.

## Revisions

**rev4 (2026-05-25, after Codex review of rev3):** Seven findings, all addressed:

1. **Stage 1 failure contract made consistent** (Codex High #1). rev3 said "no row, no file" in the error table but "text_status='failed', bytes on disk" elsewhere — contradictory, and the admin "failures" panel only makes sense with the retain-on-failure model. rev4 commits to: **insert row + commit FIRST, then run Stage 1 in a separate transaction**. On Stage 1 failure, UPDATE `text_status='failed'`, `text_error=<msg>` — row + bytes persist for retry from the UI. The upload handler returns 200 in both cases (success or failure) with the row partial; the partial renders the failure state and a retry button. Pre-commit failures (MIME / size / disk write) still use the B7 atomic cleanup pattern (no row, no file).
2. **SSRF DNS TOCTOU closed** (Codex High #2). Custom httpx Transport that resolves the hostname once, validates every returned IP against the deny-ranges, then pins the connection to the validated IP. TLS still uses the original hostname for SNI + cert validation. Defends against DNS rebinding (where validation lookup returns public IP but the fetch lookup returns private).
3. **Raw bytes serve hardened** (Codex High #3). Unlike B7's images-only `Content-Disposition: inline`, references serve a wider MIME range that includes HTML/SVG/HTML-wrapped-PDFs. rev4 sets `Content-Disposition: attachment` always (force download) + `X-Content-Type-Options: nosniff` always. PDFs/images can be browsed-inline via separate (future) preview routes that go through bleach-sanitization; the raw route is download-only.
4. **User-confirmed table updated** (Codex Medium #4). Stale "Anthropic Web Fetch" row corrected to "Crossref / PubMed / arXiv / generic URL via server-side httpx".
5. **`text_chars_total` column added** (Codex Medium #5). Populated by Stage 1; used by `submit_user_turn`'s pre-count without a file-read on the hot submit path.
6. **Reference re-embed gap documented + CLI hook** (Codex Medium #6). Added a brief Open Questions entry plus a note that the existing `embeddings refresh` CLI tick will be extended to cover references. v1 ships with the column populated correctly at insert time; backfill after a future model change is a separate operator action.
7. **XLSX extraction path filled in** (Codex Low #7). Added `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet` with openpyxl schema + first 5 rows extraction.

**rev3 (2026-05-25, after user direction):** PageIndex deferred to v1.1; local RAG is the primary navigation path. The section-aware chunker already produces a usable structure — `navigate_reference` derives the outline from `SELECT DISTINCT section_path FROM reference_chunks` instead of calling an LLM. Removes ~30% of the rev2 complexity:

- No `_reference_indexing_tick` worker (Stage 2 was PageIndex + text-retry; both gone — text failures are surface-level in v1, user retries via UI).
- No `llm_usage` ledger table (no non-turn LLM spend exists in v1's references subsystem). The Codex High #1 motivation for the ledger was PageIndex; without it, B8's existing cap is sufficient. Compaction's missing accounting is a pre-existing issue separate from this subsystem and out of scope here.
- No cap-query change (`caps.py` keeps summing only chat_turns).
- New tool `read_reference_section(reference_id, section_path)` returns chunks concatenated for a section — provides the "read all of Methods" capability without an LLM-generated outline.
- `outline_status`, `outline_error`, `outline_indexed_at` columns kept as v1.1 scaffolding (default `'skipped'` on every v1 insert). v1.1 changes the default + wires the worker; no schema migration needed at that point.

PageIndex remains the chosen v1.1 enhancement when better outlines for weak-structure PDFs become a real pain point.

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
| Agent interface | v1: Browse + semantic search + chunk-derived outline navigation. v1.1: PageIndex-generated outlines layer on top of the same `navigate_reference` tool. |
| Auto-fetch sources | Crossref (DOI) + PubMed (PMID/PMC XML) + arXiv + generic URL via server-side httpx (rev2 — NOT Claude web-fetch; the adapter has no built-in tools) |
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
        └── outline.json           (v1.1 only — PageIndex output; absent in v1)
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
    text_chars_total INTEGER,              -- rev4: total extracted text length, populated by Stage 1.
                                            -- Used by submit_user_turn for cap pre-count without a
                                            -- file-read on the hot submit path. NULL when text_status
                                            -- != 'done'.
    -- PageIndex outline status — v1.1 scaffolding. v1 inserts always
    -- use 'skipped' because PageIndex is deferred (rev3). When v1.1
    -- lands, the default changes to 'pending' and the worker starts
    -- populating outline_indexed_at. Kept now so v1.1 doesn't need
    -- a schema migration.
    outline_status  TEXT NOT NULL DEFAULT 'skipped'
                    CHECK (outline_status IN ('pending','running','done','failed','skipped')),
    outline_error   TEXT,
    outline_indexed_at TIMESTAMPTZ,
    uploaded_by     INTEGER REFERENCES users(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_references_ws ON references(workspace_id);
-- Admin-panel backlog index — picks rows currently being indexed.
-- v1 only has text_status (outline always 'skipped'); v1.1 will use both.
-- Excludes 'done', 'failed', 'skipped'. `kind=data` rows use
-- text_status='skipped' so they never appear here.
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

-- Additive changes to existing tables:
ALTER TABLE turn_jobs ADD COLUMN fetch_count INTEGER NOT NULL DEFAULT 0;
```

**Cap query unchanged in v1.** rev2 proposed a `llm_usage` ledger + UNION-with-chat_turns cap query to capture non-turn Anthropic spend (PageIndex worker). rev3 has no non-turn LLM spend in v1's references subsystem (PageIndex deferred; the agent's `fetch_reference` uses server-side httpx, not Claude web-fetch). So `caps.py` keeps its existing chat_turns-only sum. When v1.1 lands PageIndex, the ledger + query change land with it as one slice.

Notes:
- `kind=data` rows insert with `text_status='skipped' AND outline_status='skipped'`. Accessed via the `summarize_data` tool, which derives its output on-the-fly from the file on disk + the schema/sample stored in `text.txt`.
- v1: all non-data inserts use `outline_status='skipped'` (rev3 — PageIndex deferred). v1.1 changes the default to `'pending'` and starts the worker.
- `text_status` is the signal for `search_references`. `outline_status` is v1.1 scaffolding for `navigate_reference`'s LLM-outline path.
- `reference_chunks.workspace_id` denormalized so per-workspace search is a single WHERE without a join.
- Table naming (`references` is a noisy word) — Codex didn't object; keep for v1. Singular `reference` remains an option for future renaming if it bites.

### Cross-workspace copy

`POST /workspaces/<dst>/references/copy_from?source_ref_id=<id>`. Server resolves the source ref (must be visible to caller as owner of source workspace), copies bytes to the new workspace's `references/<new_id>/`, inserts a fresh `references` row + duplicates chunks. No shared state — independent storage and indexing per workspace. Clean semantics.

---

## Section 2 — Ingestion pipeline

Two entry points (upload + fetch) feed one shared indexing pipeline. v1 has one stage (sync RAG); v1.1 adds Stage 2 (async PageIndex outline).

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
                      [text_status='done', searchable]

v1.1 only: queue PageIndex outline job → async worker → outline.json + outline_status='done'
```

### Upload entry point

`POST /workspaces/<id>/references` (multipart). Owner-only. Two-phase: atomic write+row (B7 pattern), then sync Stage 1 in a separate transaction:

**Phase 1 (atomic write + row, rollback on failure):**

1. Validate MIME against allowlist: pdf, md, txt, docx, rtf, csv, xlsx, ipynb.
2. Stream-read with 50 MB cap (papers are bigger than B7's 5 MB image cap; configurable env `REFERENCES_MAX_BYTES`).
3. Write atomically (temp + `os.replace`) to `references/<new_id>/original.<ext>`.
4. Insert `Reference` row with `text_status='pending'`, `outline_status='skipped'` (v1; v1.1 defaults outline to `'pending'`).
5. Commit. On commit failure → `storage.delete_one` + return 500. On any earlier failure → row never inserted + file never written (atomic cleanup mirrors B7).

**Phase 2 (sync Stage 1, retained-on-failure model — Codex High #1 resolution):**

6. UPDATE `text_status='running'`; run text-extract + chunk + embed.
7. On success: UPDATE `text_status='done'`, `text_indexed_at=now()`, `text_chars_total=<n>`, INSERT chunks. Same transaction.
8. **On failure: UPDATE `text_status='failed'`, `text_error=<message>`. Row + bytes persist** so the user can retry from the UI and the admin "failed references" panel reflects the failure. NO atomic rollback for Stage 1 failures.

Return 200 in both Stage 1 outcomes with the row partial (the partial renders the failure state with a retry button when `text_status='failed'`). Pre-Phase-1-commit failures still return 500 (no row to render).

**Why two phases:** Phase 1's atomicity is preserved (we never end with a row pointing at a missing file or vice versa). Phase 2 is sync but separate-transaction so the user gets a fast 200 acknowledging the upload landed; the cap-relevant work (embeddings) succeeds-or-marks-failed. Compare this to B7 attachments which combined everything in one tx because attachment indexing is a synchronous Pillow probe with negligible failure modes.

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
- DNS resolution + IP allowlist + IP pinning (rev4 — closes Codex High #2 DNS rebinding gap): resolve hostname **once** via a custom httpx Transport, reject if any returned address falls in a private/internal range (RFC1918 10/8, 172.16/12, 192.168/16; loopback 127/8 + ::1; link-local 169.254/16 + fe80::/10; ULA fc00::/7; CGNAT 100.64/10). **Pin** the connection to the validated IP — TLS verification continues against the original hostname (SNI + cert subject) but the socket connects to the explicit IP. Defends against DNS rebinding (validation lookup returns public IP; fetch lookup would otherwise return private). httpx supports this via a custom `AsyncHTTPTransport` with `local_address`/`socket_options` + a hostname-to-IP override map. Implementation lives in a single `fetcher.py` module shared by the dispatcher and any future fetch caller.
- Redirect limit: max 5 redirects, each one re-validated by the scheme/IP rules (and re-pinned) before following.
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
| `text/csv` | **Schema + sample only**: column names + first 5 rows. Stored as a small Markdown table in `text.txt`. |
| `application/vnd.ms-excel` (legacy .xls) | Same as CSV (via openpyxl). Schema + first 5 rows from sheet 0. Markdown table. |
| `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet` (.xlsx, rev4) | Same as CSV (via openpyxl). Schema + first 5 rows from sheet 0. Markdown table. |
| `application/x-ipynb+json` | markdown cells + first 200 chars of each code cell + text outputs; skip raw image cells |

Failures here mark the Reference with `outline_status='failed'` and surface a clear error to the user — bytes stay on disk for re-extraction after a fix.

### Stage 1 — Text extract + section-aware chunk + embed (sync, ~1-5s per paper)

Section structure is detected first:
- PDFs: outline via PyMuPDF; if absent, headings via pdftotext layout (font-size heuristic).
- Markdown: H1/H2/H3 headers.
- docx: heading styles (`Heading 1`, `Heading 2`, ...).

One chunk per section. If any section exceeds 1500 chars, sub-chunk recursively with 100-char overlap. `section_path` populated as breadcrumb (`"Methods > Statistical analysis"`). Falls back to plain recursive 1000-char chunks when no structure is detectable.

Embeddings via `OllamaEmbedClient(model=settings.embedding_model)` — already in the stack from call ranking. Each row stores `embedding_model` so a future model change doesn't silently mix incompatible vectors (rev2 Codex Medium #6). Batch insert into `reference_chunks` then UPDATE `references.text_status='done'`, `text_indexed_at = now()`, `text_chars_total = len(extracted_text)` in one transaction (rev4 closure of Codex Medium #5 — `text_chars_total` is what `submit_user_turn` reads for the cap pre-count, avoiding a file-read on the hot submit path). ~30-60 chunks × ~50ms per local embedding call → 1.5–5s total for a 20-page paper. Fits in the request budget.

On any failure (extractor crash, Ollama unreachable, chunk insert error): UPDATE `text_status='failed'`, `text_error=<message>`, leaving row + bytes on disk. The user retries from the UI (manual retry button on the failed row); v1 has no async retry. The admin panel "failed references" view surfaces the count.

### Stage 2 — Deferred to v1.1

rev3 ships Stage 1 only. Reasoning:

- Stage 1 (text extract + chunk + embed) runs synchronously inside the upload/fetch HTTP request, using local Ollama. Typical latency 1–5s for a 20-page paper; fits in the request budget. Ollama-down failure → upload returns 500, atomic ordering cleans up the file and row.
- Stage 2 (PageIndex outline) was the async/Anthropic-spending part. Deferred per rev3.
- No async worker tick added in v1. Failure handling is surface-level: the user retries via the UI, or `fetch_reference` returns a clear error.

When v1.1 lands PageIndex:
- New tick `_reference_indexing_tick` (mirrors B8 patterns: gated by `DRAFTING_ENABLED`, picks up to N=5 `outline_status='pending'` rows, honors global cap before each call).
- `llm_usage` ledger table added in the same slice; `caps.py` cap queries updated to UNION chat_turns + llm_usage.
- `navigate_reference` switches from the v1 chunk-derived outline (see Section 3) to the LLM-generated outline when available.

### Error handling summary

| Stage | Failure | Behavior |
|---|---|---|
| Upload validate (Phase 1) | bad MIME / oversize | 400 with `_html.escape`-d message, no row, no file |
| Phase 1 commit | DB error after file write | `storage.delete_one`, return 500 (B7 atomic pattern). |
| Text extract (Stage 1) | pdftotext crash / corrupted PDF | Upload returns 200 with the row in `text_status='failed'`. Row + bytes retained. User retries via UI; admin panel surfaces the failure. |
| Chunk + embed (Stage 1) | Ollama transient down | Same — `text_status='failed'`, `text_error='transient: <message>'`. UI retry available; admin can see the failure. |
| Fetch (any source) | publisher 404 / paywall / SSRF reject / timeout | Tool error to agent; HTTP 502 to UI. No row inserted (Phase 1 never reached). |
| Mid-turn 4th fetch | atomic UPDATE returns 0 rows (fetch_count already 3) | Tool returns error ("ask user to upload"); agent surfaces in response. Cap is per-turn, not per-workspace-per-day, so the next turn's fetches start fresh. |

---

## Section 3 — Agent MCP tool surface

Six new tools added to the existing MCP registry from B5 (which launches the SDK with `tools=[]` + `strict_mcp_config=True` + explicit `allowed_tools` list — the security boundary). All scoped to the agent's workspace.

### `list_references(kind=?, q=?, limit=50)`

Browse-only metadata listing. Returns `[{id, kind, title, filename, outline_status, created_at}, ...]`. Filter by kind and title substring. Token-cheap. Used at start of a turn to orient.

### `search_references(query, top_k=8, kind=?)`

Semantic search over `reference_chunks`. Embed `query` with the current `settings.embedding_model` (filter chunks WHERE `embedding_model = ?` so model-version drift is invisible — incompatible vectors are simply not searched). HNSW lookup with `workspace_id` filter. Returns `[{reference_id, title, section_path, snippet, score}, ...]`. Default `top_k=8`, hard cap 25. Skips references with `text_status != 'done'` (they're either pending, failed, or skipped — none searchable).

### `navigate_reference(reference_id)`

Returns the document's outline. v1 derives the outline from `SELECT DISTINCT section_path FROM reference_chunks WHERE reference_id = ? ORDER BY chunk_index`, formatted as a tree (parses `"Methods > Statistical analysis"` into nested nodes). No LLM call. Output:

```json
{
  "title": "...",
  "outline": [
    {"section": "Abstract", "children": []},
    {"section": "Methods", "children": [
        {"section": "Statistical analysis", "children": []},
        {"section": "Cohort selection", "children": []}
    ]},
    ...
  ]
}
```

Status handling:
- `text_status='done'` → return the chunk-derived outline above.
- `text_status IN ('pending','running')` → `{"status": "indexing"}` (Stage 1 in flight; in v1 this only happens for a brief moment during the synchronous upload, but listed for completeness).
- `text_status='failed'` → `{"status": "failed", "error": text_error}`.
- `text_status='skipped'` (kind=data) → `{"status": "skipped", "kind": "data", "fallback": "use summarize_data(...)"}`.

When v1.1 PageIndex lands: outline source becomes `outline.json` when `outline_status='done'`, falling back to the chunk-derived outline otherwise. Same tool contract.

### `read_reference_section(reference_id, section_path)`

Returns the concatenated text of all chunks under a given section path. Drilling deeper than a leaf returns 404; passing a non-leaf section_path returns everything underneath. Hard cap 20_000 chars (mirrors `read_reference`'s truncation).

Together with `navigate_reference` this gives the agent the "browse outline, read just Methods" workflow without needing the LLM-generated PageIndex.

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

The fetcher uses local httpx (rev2 settled this as server-side; v1 has no Anthropic-spending fetch path). No external cost; no `llm_usage` row.

Returns the new `Reference` row partial once bytes are on disk + Stage 1 complete (chunks indexed and searchable). v1.1 adds an async PageIndex queue; in v1 there's no Stage 2 to wait for.

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
- `GET /workspaces/<id>/references/<ref_id>/raw` — serve original bytes, auth-checked (owner+viewer). **Always `Content-Disposition: attachment; filename="<sanitized>"`** (force download, NEVER inline — unlike B7's images-only serve which uses inline; references include HTML/SVG/HTML-wrapped-PDFs that would XSS in the app origin if rendered inline). Plus `X-Content-Type-Options: nosniff` to prevent browser MIME-sniffing into HTML. PDFs/images can be browsed-inline via separate (v1.1+) preview routes that go through bleach-sanitization. Cache-Control: private, max-age=300 (per-user, no proxy share).

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

- **Reference indexing failures:** count of `references.text_status='failed'` rows across all workspaces, with the oldest failed timestamp + recent error sample. v1 has no async backlog (Stage 1 is synchronous at upload time), so this view is primarily a health signal — non-zero counts mean Ollama is having trouble or a publisher PDF is unparseable. v1.1 adds an outline-backlog row to this section.
- **Mid-turn fetch frequency:** count of `turn_jobs.fetch_count > 0` rows from today. Helps spot heavy-fetch agents.

Both read-only — no remediation UI in v1.

### Cost cap integration

- **No new cap surface in v1.** v1 has no non-turn Anthropic spend in this subsystem (PageIndex deferred to v1.1; fetcher is server-side httpx). The existing B8 cap queries already see all cost — agent-turn input/output tokens including the new reference-inclusion pre-counts.
- **Reference inclusions pre-counted at submit time** (Codex Medium #5; see Section 4 `submit_user_turn` extension above). A 5-reference turn injecting ~100k chars of reference text contributes its `len // 4` estimate to `ChatTurn.tokens_in` BEFORE the cap check.
- **Per-turn fetch cap = 3.** Bounded atomically by the `UPDATE ... RETURNING fetch_count` pattern in Section 3.
- **Embedding cost = 0 external.** Local Ollama. Intentionally not counted (the cap protects external spend).
- **Generic URL fetch cost = 0 external.** Server-side httpx (Codex High #2). When v1.1 lands PageIndex, the `llm_usage` ledger + cap-query change land in the same slice.

---

## Section 5 — Slice plan

The subsystem ships in 5 slices, each independently mergeable to master with the established pattern (plan → subagent-driven execution → 2-stage review → smoke → merge → Codex review pass → residuals). Total scope is ~2-3 weeks if all slices are pursued sequentially.

### Slice 1 — Foundation: model + storage + sync ingest + manual upload

**Goal:** users can upload a reference file via the web UI; bytes land on disk, text is extracted, chunks are embedded, the row is searchable. No agent integration yet.

**Files (new):**
- `alembic/versions/0031_references.py` — migration for `references`, `reference_chunks`, `ALTER turn_jobs ADD fetch_count`.
- `src/call_manager/db/models.py` — `Reference`, `ReferenceChunk` ORM models.
- `src/call_manager/proposal_assist/references/__init__.py`
- `src/call_manager/proposal_assist/references/storage.py` — filesystem layout helpers (mirrors `attachments/storage.py`).
- `src/call_manager/proposal_assist/references/extract.py` — text extractors per MIME (pdftotext via pdfminer/poppler subprocess, python-docx, striprtf, openpyxl, ipynb parsing).
- `src/call_manager/proposal_assist/references/chunker.py` — section-aware chunker.
- `src/call_manager/proposal_assist/references/ingest.py` — two-phase ingest entry point (atomic write+row then sync Stage 1).
- `src/call_manager/proposal_assist/web/references.py` — new router with `GET /workspaces/<id>/references` (panel, owner+viewer), `POST /workspaces/<id>/references` (multipart upload, owner-only), `GET /workspaces/<id>/references/<id>/raw` (serve bytes with `Content-Disposition: attachment` + `nosniff`).
- `src/call_manager/web/templates/proposal_assist/workspace_references.html` — list+upload panel.
- `src/call_manager/web/templates/proposal_assist/partials/reference_row.html` — per-row partial (HTMX swap target).
- `tests/proposal_assist/references/test_storage.py`
- `tests/proposal_assist/references/test_extract.py`
- `tests/proposal_assist/references/test_chunker.py`
- `tests/proposal_assist/references/test_ingest.py`
- `tests/proposal_assist/test_web_workspaces_references.py`

**Files (modified):**
- `src/call_manager/main.py` — register new router.
- `src/call_manager/web/templates/proposal_assist/workspace_main.html` — add "References" link to side nav.
- `pyproject.toml` — add `python-docx`, `striprtf`, `openpyxl`, optionally `pdfminer.six`.

**Smoke test:** upload a small PDF via the panel; verify row + bytes + chunks land; download via `/raw` returns the original bytes with `attachment` disposition.

### Slice 2 — Read-only MCP tools (browse + search + navigate)

**Goal:** agent can `list_references`, `search_references`, `navigate_reference`, `read_reference`, `read_reference_section`, `summarize_data`. Read-only — no mutation, no fetch.

**Files (new):**
- `src/call_manager/proposal_assist/references/mcp_tools.py` — 6 MCP tool handlers.
- `tests/proposal_assist/references/test_mcp_tools.py` — each tool tested with seeded chunks.

**Files (modified):**
- `src/call_manager/proposal_assist/tools/registry.py` — register the 6 new tools in the existing MCP registry.
- `src/call_manager/proposal_assist/drafting/claude_adapter.py` — add the 6 new tool names to the `allowed_tools` allowlist (the security boundary established in B5).

**Smoke test:** ask the agent in a workspace with 3+ uploaded papers "search for the methods section about X" — verify it picks the right tool, returns relevant snippets with section_path tagged.

### Slice 3 — Auto-fetch dispatcher + SSRF-safe httpx + `fetch_reference` MCP tool

**Goal:** agent can `fetch_reference("10.1056/NEJMms…")`, `fetch_reference("https://arxiv.org/abs/…")`, etc. Same dispatcher backs a `POST /references/fetch` UI button.

**Files (new):**
- `src/call_manager/proposal_assist/references/fetcher.py` — SSRF-safe httpx Transport with IP pinning + the 4 source-specific backends (Crossref, PubMed, arXiv, generic).
- `tests/proposal_assist/references/test_fetcher_ssrf.py` — explicit tests for each SSRF defense (private IP rejection, DNS rebinding sim, scheme allowlist, redirect validation, size cap).
- `tests/proposal_assist/references/test_fetcher_backends.py` — mock each source's API surface.

**Files (modified):**
- `src/call_manager/proposal_assist/web/references.py` — add `POST /references/fetch` form route.
- `src/call_manager/proposal_assist/references/mcp_tools.py` — add `fetch_reference` tool with the atomic `UPDATE turn_jobs SET fetch_count = fetch_count + 1 WHERE workspace_id = ? AND status = 'running' AND fetch_count < 3 RETURNING fetch_count` pattern.
- `src/call_manager/proposal_assist/drafting/claude_adapter.py` — add `fetch_reference` to `allowed_tools`.
- `pyproject.toml` — add `httpx` if not already direct dep (transitive via Claude SDK; promote to direct).

**Smoke test:** agent fetches a real arXiv paper mid-turn, then immediately `search_references` finds a chunk from it.

### Slice 4 — Chat composer integration + cap pre-counting

**Goal:** user can attach references to a turn via the composer; B8 cap reflects the real input cost.

**Files (modified):**
- `src/call_manager/proposal_assist/turns/submit.py` — extend `submit_user_turn` signature with `reference_ids: list[uuid.UUID] = []`. Pre-count via `text_chars_total // 4` (rejects if any included ref's `text_status != 'done'`). Rename `MAX_ATTACHMENTS_PER_TURN` → `MAX_TURN_INCLUSIONS = 5`; check becomes `len(attachment_ids) + len(reference_ids) > 5`.
- `src/call_manager/proposal_assist/attachments/resolver.py` — sibling resolver function `resolve_references()` that injects `<system>Reference: <title>\n\n<truncated text>...</system>` blocks into the user message at processor turn-time.
- `src/call_manager/proposal_assist/turns/processor.py` — call the new resolver alongside attachments resolver.
- `src/call_manager/web/templates/proposal_assist/workspace_main.html` — composer `<details>` gains reference checkbox picker (similar to image picker).
- `tests/proposal_assist/test_turns_submit.py` — add tests for `reference_ids` + pre-count + over-cap-combined rejection.
- `tests/proposal_assist/test_web_workspaces_turns.py` — add e2e test for posting a turn with references.

**Smoke test:** user submits a turn with 2 attached references; agent's response demonstrates it saw the reference content; verify cap counter on the row reflects the pre-count.

### Slice 5 — Operational polish: delete + copy + reembed CLI + admin panel

**Goal:** lifecycle complete — delete, copy across workspaces, re-embed after model change, admin visibility of failures.

**Files (modified/new):**
- `src/call_manager/proposal_assist/web/references.py` — add `POST /references/<id>/delete` (owner-only, row-first then file unlink, B7 pattern) and `POST /references/<id>/copy_to` (resolve source, copy bytes + INSERT new row + duplicate chunks).
- `src/call_manager/proposal_assist/references/cli.py` — new `python -m call_manager.proposal_assist.references.cli reembed --model <new>` command that re-extracts + re-chunks + re-embeds all `text_status='done'` references with `embedding_model != <new>`. Atomic per-reference (delete old chunks + insert new in one tx).
- `src/call_manager/web/routes/admin.py` — extend `_drafting_panel_context` to include reference-failure counts; render in the existing `admin_drafting.html` template (new section at the bottom).
- `tests/proposal_assist/references/test_delete_copy.py`
- `tests/proposal_assist/references/test_reembed_cli.py`
- `tests/test_web_admin_drafting_panel.py` — add test for the new section.

**Smoke test:** copy a reference from workspace A to B (verify independent row + file); change `EMBEDDING_MODEL` env, run reembed CLI, verify chunks updated; admin panel shows the failure count if any `text_status='failed'` rows exist.

### Slice dependency graph

```
1 (Foundation) ──┬─→ 2 (read-only MCP tools)
                 ├─→ 4 (composer integration)
                 └─→ 5 (operational polish)
3 (auto-fetch + fetch_reference tool) ─── depends on 1 + 2
```

Slice 2 and Slice 3 can land in either order after Slice 1 (3 can be sketched against the UI route alone; the MCP version benefits from 2's tool-registry plumbing being live but doesn't strictly require it). Recommended order is **1 → 2 → 3 → 4 → 5** because each slice's smoke test is more interesting when the prior agent-interaction surface is live.

### What's NOT in v1 (deferred to v1.1)

- PageIndex outline generation + the async indexing tick + the `llm_usage` ledger table + the `caps.py` cap-query UNION change. All land as one slice (call it `v1.1`) when better outlines for weak-structure PDFs become a real pain point.
- Inline-preview routes for PDFs/images (with bleach sanitization). v1 only has the `attachment`-disposition raw download.
- Promotion of references to a cross-workspace shared library. v1 is copy-on-add only.

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

5. **Backward-compat for existing workspaces.** All existing workspaces predate this subsystem; the migration adds the new tables (`references`, `reference_chunks`) and the new `turn_jobs.fetch_count` column (NOT NULL default 0 — additive, no backfill needed). v1.1 adds the `llm_usage` table and changes `references.outline_status` default — both also additive.

6. **PageIndex (v1.1)** lands as one slice when better outlines for weak-structure PDFs become a real pain point. The v1 schema scaffolding (`outline_status`, `outline_error`, `outline_indexed_at` columns) means no migration churn at that time — just default change + new worker + new ledger table + cap-query update.

7. **Reference re-embed after EMBEDDING_MODEL change** (Codex Medium #6). v1 ships with `embedding_model` stamped on each chunk and search filtering by current model — so a model change makes old chunks invisible (correct behavior — incompatible vectors shouldn't be mixed). What's missing is a re-embed path. Options for v1 → v1.1:
   - Operator runs a one-off `uv run python -m call_manager.references.cli reembed --model <new>` command — re-extracts text from `text.txt` (already on disk), re-chunks, re-embeds, replaces chunks atomically per reference.
   - Or: extend the existing `embeddings refresh` daily tick (`embeddings/cli.py`) to also walk references. Closer to the existing pattern but adds work to the daily tick.

   v1 ships the CLI option (lower-risk, opt-in per operator action). Belongs in the same slice as the reference ingestion pipeline so it gets tested at land time, not retrofit.
