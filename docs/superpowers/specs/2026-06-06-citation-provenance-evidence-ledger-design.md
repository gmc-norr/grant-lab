# Citation Provenance & Evidence Ledger — Design Spec

**Status:** Approved for implementation (gate-independent). **Revision 3** (incorporates two Codex spec-review passes, 2026-06-06).
**Created:** 2026-06-06
**Layers on top of:** [`2026-05-25-references-corpus-design.md`](2026-05-25-references-corpus-design.md) (reference ingestion, chunking, semantic search) and the base proposal-assist spec [`2026-04-18-call-manager-proposal-assist-design.md`](2026-04-18-call-manager-proposal-assist-design.md).
**Motivation:** External (Codex) review, 2026-06-06 — "references are searchable but claims are not auditable; the proposal cannot reliably answer *which source supports this sentence?*". Captured as follow-up #2 in [`2026-05-19-proposal-assist-phased-workflow-design.md`](2026-05-19-proposal-assist-phased-workflow-design.md).

**Relationship to the phased-workflow gate:** This work is **gate-independent**. The phased-workflow Implementation gate concerns the *ranking* foundations (golden set, embedding threshold calibration, section-aware extractor) because the phase FSM consumes the ranking cosine and section extracts. Citation provenance operates on the **reference corpus** (uploaded papers + fetched sources), which does not depend on ranking calibration, so it can be built now. The one coupling — the citation *audit* tools target a drafting flow that is not yet phase-enforced — is an accepted, known rework cost (see "Phase/gate fit").

---

## Purpose

Make every important proposal claim traceable to evidence that cannot silently change. Today the implemented proposal-assist is a versioned chat editor with workspace-scoped semantic search: the agent can *find* reference text but nothing records *which source backs which sentence*, reference bibliographic metadata is discarded at ingest (`references.title` is left NULL), and fetched reference text is injected without citation-ready locators. This spec adds:

1. **CSL-JSON bibliographic metadata** captured per reference, with an owner approve/correct gate (LLM extraction hallucinates) and a fail-closed export gate.
2. **A git-tracked, content-frozen evidence ledger** (`citations.json`) mapping each marker occurrence → the exact chunk + quote that backs a specific claim, with content hashes so drift is detectable at any `draft_sha`.
3. **Citation tools** (`insert_citation`, `remove_citation`, `update_citation`, `rename_citekey`) that atomically keep `proposal.md` and the ledger consistent.
4. **A separate structured audit service** behind `validate_citations` and `list_unsupported_claims` — its own model/prompts, token ledger + caps, timeouts, and a verdict cache — so semantic checks don't run as nested calls inside the streaming drafting agent or bypass token accounting.
5. **citeproc-based bibliography** rendering from approved metadata, plus a composition/export step.

Success criteria: given a finished draft, the system can (a) list every claim and the exact frozen quote/chunk that backs it, (b) flag claims whose cited chunk no longer matches or does not support the claim (source drift / unsupported), (c) flag factual claims with no citation, and (d) render a correct bibliography, refusing to emit one built on unverified metadata.

---

## Architecture overview

**Inherits, unchanged:** per-workspace git repo + `proposal.md`, the reference corpus (`references` + `reference_chunks`), workspace-scoped embedding search, the drafting agent + MCP tool surface, the owner/viewer permission model, the existing Phase-0 review-gate pattern (reused for metadata approval), the structured LLM client pattern (`ranking/anthropic_client.py`, `ranking/ollama_client.py`), and the daily-token-cap primitives (`turns/caps.py`).

**Adds:**
- CSL-JSON metadata (canonical + raw extracted) + extraction step + owner approval gate on `references`.
- `citations.json`, a git-tracked, content-hashed evidence ledger in the workspace repo beside `proposal.md`.
- Citation/lifecycle MCP tools that mutate `proposal.md` + `citations.json` **atomically in a single git commit**.
- A **citation audit service** (`proposal_assist/citations/audit.py`) — a structured-completion client (NOT the drafting SDK) with explicit prompts, a generic `citation_llm_usage` ledger + per-workspace/global caps + timeouts, and a DB-backed verdict cache. `validate_citations` / `list_unsupported_claims` are thin tools over it.
- `bibliography.md` rendered via citeproc from a stored workspace citation-style setting; a composition step to fold it into a single submission document.
- Two extra fields in `search_references` / `read_reference_section` output.

**Single source of truth:** canonical bibliographic metadata lives only on the `references` row (`csl_json`). The ledger stores `reference_id` + `citekey` + chunk locator + content hashes + the relied-on quote — never title/authors/year. `build_bibliography` renders from `references` at build time; each ledger row also keeps a `citation_snapshot` so a frozen review reflects what was cited even if metadata is later corrected.

---

## Data model

### 1. `references` — metadata columns

| Column | Type | Notes |
|---|---|---|
| `csl_json` | JSONB, nullable | **Canonical** metadata in CSL-JSON shape (see below). Holds canonical metadata for **both** `verified_external` (fetched) and owner-`approved` references; `metadata_status` carries the trust level. |
| `csl_json_extracted` | JSONB, nullable | **Raw** machine-extracted metadata before owner correction. Kept for provenance; per-field provenance = diff(`csl_json`, `csl_json_extracted`). |
| `citekey` | text, nullable | **Derived searchable cache** of the git/ledger-canonical citekey (see Atomicity). Unique per workspace (partial unique index where not null). Reconciliation can rebuild it from the ledger. |
| `metadata_status` | text | `pending` / `extracted` / `verified_external` / `approved` / `failed`. CHECK-constrained. |
| `metadata_source` | text | Coarse origin: `fetcher` / `embedded` / `heuristic` / `llm` / `manual`. |
| `original_sha256` | text, nullable | SHA-256 of the original uploaded/fetched bytes, hashed once at ingest. Snapshotted into each ledger row so validation can tell "source replaced" from "re-indexed" (see Freezing). |

`title` (existing column) is kept as a denormalized convenience for the search filter that already uses it, derived from `csl_json.title`. `source_uri` (existing) holds the URL.

**CSL-JSON shape** (subset of the [CSL-JSON schema](https://citeproc-js.readthedocs.io)), enough for citeproc to render any style:
```json
{
  "type": "article-journal",                 // article-journal | paper-conference | book | chapter | report | dataset ...
  "title": "…",
  "author": [{ "family": "Smith", "given": "J." }, { "literal": "WHO Collaborating Group" }],
  "issued": { "date-parts": [[2021]] },
  "container-title": "Nature Medicine",
  "volume": "27", "issue": "4", "page": "511-520",
  "publisher": "…",
  "DOI": "10.1038/…", "PMID": "34000000", "arxiv": "2101.00001",
  "URL": "https://…", "accessed": { "date-parts": [[2026, 6, 6]] }
}
```

**Migration:** add columns; existing rows get `metadata_status='pending'`. No data backfill required.

### 2. `citation_audit_verdicts` — semantic-verdict cache (DB)

| Column | Type | Notes |
|---|---|---|
| `id` | bigint PK | |
| `workspace_id` | uuid FK (CASCADE) | |
| `cache_key` | text, indexed | `sha256(claim_hash · chunk_content_hash · evidence_quote_hash · stance · validator_version)`. |
| `verdict` | text | `supports` / `contradicts` / `unsupported` (for `supports`/`contradicts` stances); `relevant` / `irrelevant` for `background`. |
| `rationale` | text | Short model rationale. |
| `validator_version` | text | Bump to invalidate the whole cache when prompts/model change. |
| `created_at` | timestamptz | |

Keyed by content hashes including the relied-on quote, so changing the quote within an otherwise-unchanged chunk correctly invalidates the verdict; re-running `validate_citations` over an unchanged draft is free.

### 2b. `citation_llm_usage` — complete LLM usage ledger (DB)

The verdict cache only records *successful, cacheable* verdicts, so it cannot account for failed/timed-out calls, `list_unsupported_claims`, metadata-extraction calls, or any call that produces no cacheable verdict. A separate append-only usage ledger records **every** LLM call the citation subsystem makes, and is the basis for caps.

| Column | Type | Notes |
|---|---|---|
| `id` | bigint PK | |
| `workspace_id` | uuid FK (CASCADE) | |
| `op` | text | `metadata_extract` / `validate_citation` / `list_unsupported` / … |
| `status` | text | `ok` / `failed` / `timeout`. |
| `tokens_in`, `tokens_out` | int | 0 on failures with no usage returned. |
| `model` | text | |
| `created_at` | timestamptz, indexed | Stockholm-day boundary for cap windows. |

Both a **per-workspace daily** cap and a **global daily** cap apply (parallel to the drafting `turns/caps.py` ceilings, but a distinct ledger so audit spend never silently rides on — or is hidden from — the drafting budget).

### 3. `citations.json` (git-tracked, per workspace repo) — schema **v2**

Created lazily on first `insert_citation`; absent/empty means "no citations." Versioned and committed *together with* `proposal.md`, so the ledger and the prose are always consistent at every commit, and both freeze at any `draft_sha`.

```json
{
  "version": 2,
  "evidence": [
    {
      "id": "ev-<12 hex>",                 // STABLE, per marker occurrence
      "citekey": "smith2021",
      "reference_id": "<uuid>",
      "locator": { "chunk_index": 12, "section_path": "Page 4" },
      "source_content_hash": "sha256:…",   // snapshot of references.original_sha256 → "source replaced"
      "chunk_content_hash": "sha256:…",    // detects chunk re-index/edit/delete → "re-indexed"
      "evidence_quote": "the exact span relied on, copied verbatim from the chunk",
      "evidence_quote_offsets": [120, 218],// char offsets in the chunk when the quote is uniquely locatable (else null)
      "stance": "supports",                // supports | contradicts | background
      "confidence": "high",                // high | medium | low (agent self-assessment)
      "claim_context": { "text": "<claim sentence snapshot>", "hash": "sha256:…" },
      "citation_snapshot": "<rendered short citation at insert time>",
      "created_at": "<iso8601>", "updated_at": "<iso8601>"
    }
  ]
}
```

The ledger is **per-marker-occurrence**: each marker in the prose carries its own `id`, so removing one of several `[@smith2021]` uses unambiguously orphans exactly one row. One reference still backs many claims via multiple rows sharing a `citekey`.

---

## Citation markers, keys, and atomicity

**Markers:** pandoc-style `[@citekey]` immediately followed by an HTML comment carrying the evidence id:
```
…reduces recurrence by 30% [@smith2021]<!-- ev:ev-a13f2c91e0b4 -->.
```
The `[@citekey]` renders normally via citeproc/pandoc; the comment is invisible in rendered output but ties this exact occurrence to its ledger row (resolves Codex H1).

**Atomic writes:** every citation tool stages and commits **`proposal.md` + `citations.json` in one git commit**. There is no window where the prose and ledger disagree, and no reliance on a second `edit_proposal` call (resolves the inconsistency hole). Tools locate the insertion point via a caller-supplied **anchor** (a substring that must occur exactly once in `proposal.md`); ambiguous/absent anchors are rejected with no write.

**Git/DB recovery contract** (git and Postgres can't share one transaction):
- The **git ledger is canonical** for the citekey↔reference binding; `references.citekey` is a *derived searchable cache*.
- Mutations hold the **per-workspace Postgres advisory lock** (`drafting/locking.py`, `workspace_lock_key`) for the whole operation, serializing concurrent citation writes.
- **Write order:** commit git (proposal.md + citations.json) **first**, then update the `references.citekey` cache. If the DB update fails after the git commit, the system is still correct (git is canonical); a **reconciliation** pass (run on workspace load and after a failed write) rebuilds `references.citekey` from the ledger. The reverse failure (git commit fails) leaves the DB untouched — no row was written.
- `rename_citekey` follows the same order: rewrite markers + ledger + commit, then refresh the cache; reconciliation repairs a partial failure.

**Citekey scheme:** prefer structured CSL `author[0].family` + `issued` year → `smith2021`, lowercased, non-alphanumerics stripped; workspace collisions get letter suffixes in assignment order. **Fallback** (metadata not yet approved/parseable): `ref-<12 hex>` derived from a hash, with unique-index retry on the rare collision (free-text first-author parsing is a fallback path only, never the primary one). **No auto-rewrite:** assigned keys are never silently changed; `rename_citekey` is the deliberate action.

---

## Metadata extraction & approval

Runs after text extraction at ingest.

- **Fetched refs** (Crossref / PubMed / arXiv): **the current fetchers return only identifiers** — Crossref `{doi, title}`, PubMed `{pmid, pmc_id}`, arXiv `{arxiv_id}` ([`fetcher.py`](../../../src/call_manager/proposal_assist/references/fetcher.py)). Slice 1 must **extend** them to pull a full record (Crossref full work metadata, PubMed efetch summary, arXiv Atom API) and map it into CSL-JSON, written to canonical `csl_json`. `metadata_source='fetcher'`, `metadata_status='verified_external'` — trusted external source, **not** owner-`approved` (records can be incomplete; the owner can promote to `approved` after a glance). No LLM. Any field the external record doesn't provide is left for owner completion.
- **Uploaded refs:** (1) PDF embedded info dict + first-page heuristics; (2) one cheap, low-temperature **structured** LLM call (via the audit/structured client, with untrusted-reference framing) extracting CSL-JSON fields from the first few-thousand chars; (3) merge per field (embedded > LLM) into `csl_json_extracted`. `metadata_source='heuristic'|'llm'`, `metadata_status='extracted'`.

**Owner approval/correction gate** (mirrors the Phase-0 rubric-review gate): a per-reference metadata panel shows the extracted CSL fields; the owner edits any and approves → `csl_json` (canonical) set, `metadata_status='approved'`. Drafting/`insert_citation` is allowed against `extracted`/`pending` metadata (don't block writing), but output is gated — see below.

---

## Tool surface

MCP tools added to the drafting agent's allowlist. **Deterministic checks are in-process Python** (free, no LLM). **Semantic checks go through the audit service**, never as nested calls inside the streaming drafting agent.

### Mutation tools (atomic: one git commit over `proposal.md` + `citations.json`)
- **`insert_citation(reference_id, anchor, chunk_index, evidence_quote, stance="supports", confidence=None)`** — resolves/assigns the `citekey`; inserts `[@citekey]<!-- ev:<id> -->` after the unique `anchor`; appends the ledger row with `source_content_hash`, `chunk_content_hash`, `evidence_quote`, `claim_context` (snapshot+hash of the anchor sentence), and `citation_snapshot`. **Quote verification:** `evidence_quote` must be an exact normalized-substring match of the selected chunk (whitespace-normalized); the tool records `evidence_quote_offsets` when the match is unique and **rejects** a quote that is absent or ambiguous (>1 match) — the agent can't invent or alter the relied-on text. Idempotent: an identical `(reference_id, anchor, chunk_index, evidence_quote)` returns the existing `ev` id without a second marker.
- **`remove_citation(evidence_id)`** — removes that marker occurrence and its ledger row.
- **`update_citation(evidence_id, chunk_index?|stance?|confidence?|evidence_quote?|reaccept_claim?)`** — updates the row (re-pins chunk/quote with the same quote-verification as insert, refreshes hashes); `reaccept_claim=true` re-snapshots the *live* claim around the marker (clearing a `claim_drift` flag — see validate). Marker text changes only on a citekey change (not here).
- **`rename_citekey(old, new)`** — rewrites all `[@old]` markers, the affected ledger rows, and `references.citekey`; rejects a `new` already in use.

### Audit tools (deterministic + audit service)
- **`validate_citations()`** → issue list `{type, severity, citekey, evidence_id, location, detail}`:
  - *Deterministic:* every marker has a matching ledger row and vice-versa (by `ev` id); each row's `reference_id`/`chunk_index` exist; **`source_content_hash` matches `references.original_sha256`** (else `source_replaced`) and **`chunk_content_hash` matches the live chunk** (else `index_drift`); the **live claim** extracted around each marker (the sentence containing it) is re-hashed and compared to `claim_context.hash` (else `claim_drift` — the prose changed under the marker); `evidence_quote` still occurs in the live chunk; each cited reference's metadata is `approved`/`verified_external` (else warning); citekey-upgrade notes.
  - *Semantic (audit service):* for each row, judge the **live claim** (not the stale snapshot) against `evidence_quote`/chunk → `supports`/`contradicts`/`unsupported` for those stances; for `stance=background`, judge `relevant`/`irrelevant` instead (the support/contradict set is not meaningful for background context). A `claim_drift` row is re-judged and its old verdict not reused; clearing the drift requires `update_citation(..., reaccept_claim=true)`.
- **`list_unsupported_claims()`** → via the audit service: identify factual/empirical claims lacking a nearby marker, with location + a suggested `search_references` query. Heuristic — "candidates," not exhaustive.

### Bibliography & export
- **`build_bibliography(style=None)`** — collects citekeys actually marked in `proposal.md`, renders via **citeproc** using the stored workspace citation style (default Vancouver; `style` arg overrides), and writes `bibliography.md`. **Fail-closed:** refuses to build if any cited reference's metadata is not `approved`/`verified_external`, unless the owner has recorded an explicit per-reference **waiver**. Never appended into `proposal.md`.
- **Composition/export step** — a separate action that assembles `proposal.md` + `bibliography.md` (rendered citations) into a single submission document when a funder requires one file. Keeps `proposal.md` free of generated, churn-prone reference lists.

**Reference lifecycle policy:** a reference that has **never** been cited can be hard-deleted. Once a reference has **ever** been cited, deletion is a **soft-delete / tombstone**: the row, bytes, chunks, and metadata are retained (so historical git commits still resolve their citations) and the reference is hidden from new search/citation. Re-indexing chunks is caught by `chunk_content_hash` (`index_drift`) and source replacement by `source_content_hash` (`source_replaced`) in `validate_citations`. The audit tools **report, never auto-edit** the draft.

---

## Audit service

`proposal_assist/citations/audit.py` — a structured-completion service, not an agent tool's nested call:
- **Client:** reuses the `ranking/anthropic_client.py` / `ollama_client.py` structured (prompt-in/JSON-out) pattern with explicit model + prompts. Not the streaming drafting SDK. This shared structured-completion client is built in **Slice 1** (metadata extraction needs it too).
- **Cost:** every call — `metadata_extract`, `validate_citation`, `list_unsupported`, including failures/timeouts — is recorded in the `citation_llm_usage` ledger (§2b), enforced against **per-workspace and global daily caps** distinct from the drafting turn caps it would otherwise bypass. The verdict cache (§2) is separate and holds only successful verdicts.
- **Resilience:** per-call timeout + bounded concurrency; on failure, `validate_citations` returns deterministic results plus an `audit_unavailable` status rather than failing the whole call.
- **Caching:** verdict cache keyed by `(claim hash, chunk_content_hash, evidence_quote_hash, stance, validator_version)`; bumping `validator_version` invalidates en masse.
- **Safety:** chunk text fed to the judge is wrapped as untrusted reference content (reuses the `<untrusted-reference>` framing + standing safety instruction).

---

## Retrieval surfacing

`search_references` currently drops the chunk locator. Add `chunk_index` to `search_references` and `read_reference_section` output (so the agent can pass an exact `chunk_index` to `insert_citation`), and include `citekey` when the reference has one. Underlying data already exists on `ReferenceChunk`; this is an output-shape change in `tools/references.py`.

---

## Phase/gate fit (accepted rework)

The tools join the always-available drafting allowlist now. When the gated phased workflow is built, they move to Draft/Review phase gates; metadata approval stays a per-reference gate. The audit tools target a not-yet-phase-enforced drafting flow — accepted known rework. The audit *service* is phase-agnostic and reusable directly by the future review runner.

---

## Testing strategy

- **Metadata:** extended Crossref/PubMed/arXiv fetchers map full records → CSL-JSON; embedded/heuristic parse; structured LLM extraction via fake adapter; per-field merge (`csl_json_extracted` vs canonical); status transitions incl. `verified_external` (fetched) vs `approved` (owner); `failed` path.
- **Citekey:** structured-family generation, collisions/suffixes, `ref-<12hex>`+retry fallback, no-auto-rewrite, reuse on re-cite.
- **Atomicity & recovery:** `insert_citation` commits `proposal.md`+`citations.json` together under the workspace advisory lock; anchor not-unique/not-found → no write; idempotent duplicate insert; DB-cache update failing after the git commit leaves a correct git state and reconciliation rebuilds `references.citekey`; `remove`/`update`/`rename_citekey` round-trips keep prose, ledger, and cache consistent.
- **Quote verification:** `insert/update` reject an `evidence_quote` absent from the chunk or matching >1 location; offsets recorded on a unique match.
- **Freezing/drift:** `chunk_content_hash` mismatch → `index_drift`; `source_content_hash` vs `references.original_sha256` mismatch → `source_replaced`; live-claim hash mismatch → `claim_drift` (cleared only by `reaccept_claim`); `evidence_quote`/offsets + `citation_snapshot` recorded.
- **`validate_citations`:** each deterministic check (orphan marker/row by `ev` id, missing chunk, `index_drift`, `source_replaced`, `claim_drift`, missing quote, unapproved warning) + semantic verdict over the **live** claim via fake audit service; `claim_drift` row re-judged (no stale-verdict reuse); cache hit avoids a second call; `audit_unavailable` degradation.
- **`list_unsupported_claims`:** via fake audit service; recorded in the usage ledger.
- **Audit service & usage ledger:** every call (ok/failed/timeout) recorded in `citation_llm_usage`; per-workspace + global cap enforcement; timeout/failure degradation; verdict cache key includes `evidence_quote_hash` + `validator_version`; `background` stance → `relevant`/`irrelevant`.
- **Reference lifecycle:** uncited reference hard-deletes; once-cited reference tombstones (chunks/metadata retained, hidden from new use, historical commit citations still resolve).
- **Bibliography/export:** citeproc rendering for Vancouver + author-year from CSL-JSON; **fail-closed** on unapproved metadata; waiver path; stored workspace style; renders only marker-present keys; composition step assembles one document.
- **Retrieval:** `search_references`/`read_reference_section` include `chunk_index` (+ `citekey` when set).

---

## Out of scope (separate specs)

| Feature | Reason |
|---|---|
| Hybrid/lexical retrieval, reranking, per-reference diversity, dedup (Codex item 4) | Distinct retrieval-quality effort; this spec is provenance, not recall. |
| Cross-workspace / curated shared reference library | Separate corpus + access-control design. |
| Automatic retrieval before evidence-backed drafting | Belongs with the gated Draft-phase behavior. |
| Real phase-gating of the citation tools | Part of the gated phased workflow. |
| PDF section-structure recovery (beyond page-based `section_path`) | Separate extractor improvement (Codex item 5). |
| Full multi-style CSL library management / custom funder styles | v1 ships a small fixed style set (Vancouver default, author-year); custom CSL upload later. |

---

## Suggested implementation slices (for the plan)

1. **Shared infra + metadata foundation** — the **shared structured-completion client**, the generic `citation_llm_usage` ledger, and per-workspace/global caps (all needed by metadata extraction now and the audit service later); `references` CSL columns + `original_sha256` + migration; **extended fetchers** (Crossref/PubMed/arXiv → full CSL) + heuristic + structured-LLM extraction; owner approval panel/route; `verified_external` vs `approved`.
2. **Ledger + mutation tools** — `citations.json` v2 with content + source + quote hashes, markers w/ `ev` ids, atomic `insert/remove/update/rename_citekey` (anchor-based, quote-verified, git/DB recovery contract), reference tombstoning, retrieval surfacing.
3. **Audit prompts + audit tools** — citation-specific audit prompts + verdict cache on top of Slice 1's client/ledger/caps; `validate_citations` (deterministic incl. claim/source/index drift + semantic), `list_unsupported_claims`.
4. **Bibliography/export** — citeproc dependency, workspace style setting, `build_bibliography` fail-closed + waiver, composition/export step.

---

## Decision log

| # | Decision | Notes |
|---|---|---|
| D1 | Scope = full evidence ledger **including** audit tools | Accepted rework when phase-gating lands. |
| D2 | Uploaded-ref metadata = heuristic + structured-LLM extraction with a **mandatory owner approve/correct gate** | Fetched metadata → `verified_external` (not owner-`approved`); can be incomplete. |
| D3 | Approach 1 — **git-artifact ledger + inline markers** | Chosen over a relational DB-table ledger and a git+DB hybrid: matches the codebase's git-artifact/audit-trail design, freezes at `draft_sha`, single source of truth, no DB↔git sync burden. |
| D4 | Ledger is **per-marker-occurrence** with stable `ev` ids; canonical metadata stays in the DB | Removing one of several same-key markers orphans exactly one row (Codex H1). |
| D5 | Citekeys from **structured CSL** family+year; `ref-<12hex>`+retry fallback; **never auto-rewritten** | Free-text author parsing is fallback only; 12 hex (not 4) + unique-index retry (Codex Q3). |
| D6 | Audit tools **report, never auto-edit**; `build_bibliography` renders from in-text markers only | |
| D7 | **Atomic** citation mutations: one git commit over `proposal.md`+`citations.json`; **anchor-based** insertion | Closes the non-atomic insert hole (Codex H1). |
| D8 | **Content-frozen** ledger: `source_content_hash` + `chunk_content_hash` + `evidence_quote`(+offsets) + `claim_context.hash` + `citation_snapshot` | `draft_sha` freeze is now real (Codex H2). **Rev 3:** reinstated the whole-source hash (`references.original_sha256`, snapshotted per row) — it distinguishes "source replaced" (`source_replaced`) from "re-indexed" (`index_drift`), which the chunk hash alone can't. |
| D9 | **Separate structured audit service** (not the drafting SDK) with own token ledger + caps, timeouts, content-keyed verdict cache, untrusted-reference framing | Nested LLM calls in a streaming agent + bypassed token accounting were the core flaw (Codex H3). |
| D10 | **CSL-JSON** canonical metadata + **citeproc** rendering; store raw extracted blob alongside canonical | Free-text authors+venue can't render Vancouver/author-year reliably (Codex M1). Per-field provenance = raw-vs-canonical diff. |
| D11 | Output **fail-closed** on unapproved metadata unless explicit owner waiver | The warn-only "gate" wasn't a gate (Codex M2). |
| D12 | Lifecycle tools (`remove`/`update`/`rename_citekey`), idempotent insert, reference delete/re-index policy | Filled the missing-correction gap (Codex M3). |
| D13 | `bibliography.md` is the canonical generated artifact; a separate composition step folds it into a single submission doc | Never append generated references into `proposal.md` (Codex Q1). Citation style is a stored workspace setting (Codex Q2). |
| D14 | `validate_citations` re-extracts the **live claim** per marker and flags `claim_drift`; semantic judging uses the live claim; re-accept via `update_citation(reaccept_claim=true)` | A generic `edit_proposal` can change a claim under an intact marker; the stale snapshot must not keep validating (Rev-3 H1). |
| D15 | `evidence_quote` must be an exact normalized substring of the chunk; store offsets when unique; reject absent/ambiguous | Caller-supplied quotes were unverified — the agent could invent them (Rev-3 H2). |
| D16 | Git/ledger **canonical** for citekey; `references.citekey` is a derived cache; advisory-locked, git-first write order + reconciliation | Git and Postgres can't be one transaction; defines recovery after partial failure (Rev-3 H3). |
| D17 | Separate append-only `citation_llm_usage` ledger records **all** calls (incl. failures, `list_unsupported`, metadata extraction); per-workspace + global caps | The verdict cache only held successful verdicts, so it couldn't be the usage ledger (Rev-3 H4). |
| D18 | **Soft-delete/tombstone** a reference once it has ever been cited (retain bytes/chunks/metadata); uncited refs hard-delete | Deletion also breaks citations in historical commits (Rev-3 M1). |
| D19 | Add `evidence_quote_hash` to the verdict cache key; `background` stance judged `relevant`/`irrelevant`, not supports/contradicts | Quote change within an unchanged chunk must invalidate; background isn't a support/contradict question (Rev-3 M2). |
| D20 | **Extend** Crossref/PubMed/arXiv fetchers to return full CSL metadata in Slice 1; `verified_external` metadata lives in canonical `csl_json` | Current fetchers return only IDs/title (Rev-3 M3). |
| D21 | Move the shared structured-completion client + usage ledger + caps into **Slice 1** | Slice 1 metadata extraction needs the client that was originally placed in Slice 3 (Rev-3 slices). |

---

## Cross-references

- References corpus (ingestion, chunking, search): [`2026-05-25-references-corpus-design.md`](2026-05-25-references-corpus-design.md)
- Phased workflow (gated; this is follow-up #2 there): [`2026-05-19-proposal-assist-phased-workflow-design.md`](2026-05-19-proposal-assist-phased-workflow-design.md)
- Base proposal-assist spec: [`2026-04-18-call-manager-proposal-assist-design.md`](2026-04-18-call-manager-proposal-assist-design.md)
- Structured LLM client pattern reused by the audit service: `src/call_manager/ranking/anthropic_client.py`, `src/call_manager/ranking/ollama_client.py`
