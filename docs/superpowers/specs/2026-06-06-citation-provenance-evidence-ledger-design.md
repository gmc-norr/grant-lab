# Citation Provenance & Evidence Ledger — Design Spec

**Status:** Approved for implementation (gate-independent).
**Created:** 2026-06-06
**Layers on top of:** [`2026-05-25-references-corpus-design.md`](2026-05-25-references-corpus-design.md) (reference ingestion, chunking, semantic search) and the base proposal-assist spec [`2026-04-18-call-manager-proposal-assist-design.md`](2026-04-18-call-manager-proposal-assist-design.md).
**Motivation:** External (Codex) review, 2026-06-06 — "references are searchable but claims are not auditable; the proposal cannot reliably answer *which source supports this sentence?*". Captured as follow-up #2 in [`2026-05-19-proposal-assist-phased-workflow-design.md`](2026-05-19-proposal-assist-phased-workflow-design.md).

**Relationship to the phased-workflow gate:** This work is **gate-independent**. The phased-workflow Implementation gate concerns the *ranking* foundations (golden set, embedding threshold calibration, section-aware extractor) because the phase FSM consumes the ranking cosine and section extracts. Citation provenance operates on the **reference corpus** (uploaded papers + fetched sources), which does not depend on ranking calibration, so it can be built now. The one coupling — the citation *audit* tools target a drafting flow that is not yet phase-enforced — is an accepted, known rework cost (see "Phase/gate fit").

---

## Purpose

Make every important proposal claim traceable to evidence. Today the implemented proposal-assist is a versioned chat editor with workspace-scoped semantic search: the agent can *find* reference text but nothing records *which source backs which sentence*, reference bibliographic metadata is discarded at ingest (`references.title` is left NULL), and fetched reference text is injected without citation-ready locators. This spec adds:

1. **Bibliographic metadata** captured per reference (title, authors, year, DOI, venue), with an owner approve/correct gate because the extraction is partly LLM-driven and LLMs hallucinate.
2. **A git-tracked evidence ledger** (`citations.json`) mapping claims → supporting/contradicting source chunks, with stable citation keys and pandoc-style inline markers in `proposal.md`.
3. **Four agent tools** — `insert_citation`, `validate_citations`, `list_unsupported_claims`, `build_bibliography` — that let the agent cite precisely and let the system audit whether cited sources actually support their claims and which claims lack evidence.

Success criteria: given a finished draft, the system can (a) list every claim and the exact chunk that backs it, (b) flag claims whose cited chunk does not actually support them, (c) flag factual claims with no citation, and (d) render a correct bibliography from approved metadata.

---

## Architecture overview

**Inherits, unchanged:** per-workspace git repo + `proposal.md`, the reference corpus (`references` + `reference_chunks`), workspace-scoped embedding search, the drafting agent + MCP tool surface, the owner/viewer permission model, and the existing Phase-0 review-gate pattern (reused for metadata approval).

**Adds:**
- Bibliographic columns + a metadata-extraction step + an owner approval gate on `references`.
- `citations.json`, a git-tracked artifact in the workspace repo beside `proposal.md`.
- Four MCP tools (deterministic checks in Python; semantic checks via the existing LLM adapter, so they are testable with the fake adapter).
- Two extra fields in `search_references` / `read_reference_section` output.

**Single source of truth:** bibliographic metadata lives only on the `references` row. `citations.json` stores `reference_id` + a `citekey` snapshot + chunk locator + claim + stance — never title/authors/year. `build_bibliography` joins to `references` at render time, so corrected metadata flows through without rewriting the ledger.

---

## Data model

### 1. Columns added to `references`

| Column | Type | Notes |
|---|---|---|
| `authors_json` | JSON (list of strings) | Author display strings, first author first. |
| `year` | int, nullable | Publication year. |
| `doi` | text, nullable | Normalized DOI. |
| `venue` | text, nullable | Journal / conference / publisher. |
| `citekey` | text, nullable | Unique per workspace. Assigned at metadata approval or first `insert_citation`. Stable once assigned. |
| `metadata_status` | text | `pending` / `extracted` / `approved` / `failed`. CHECK-constrained. |
| `metadata_source` | text | `fetcher` / `embedded` / `heuristic` / `llm` / `manual` — provenance/transparency. |

Title reuses the existing `title` column; URL reuses existing `source_uri`. A partial unique index enforces `citekey` uniqueness per `workspace_id` where `citekey IS NOT NULL`.

**Migration:** add columns; existing rows get `metadata_status='pending'` (the owner can trigger extraction or fill metadata manually). No data backfill required.

### 2. `citations.json` (git-tracked, per workspace repo)

Created lazily on first `insert_citation`; absent/empty means "no citations" (so existing workspaces need no backfill). Versioned with `proposal.md`, so it is diffable and frozen at any `draft_sha` for the future review runner.

```json
{
  "version": 1,
  "evidence": [
    {
      "id": "ev-<short>",
      "citekey": "smith2021",
      "reference_id": "<uuid>",
      "locator": { "chunk_index": 12, "section_path": "Page 4" },
      "stance": "supports",          // supports | contradicts | background
      "confidence": "high",          // high | medium | low (agent self-assessment)
      "claim": "<snapshot of the claim sentence this backs>",
      "created_at": "<iso8601>",
      "updated_at": "<iso8601>"
    }
  ]
}
```

The ledger is **claim-level, not reference-level**: one reference can back several distinct claims via multiple `evidence` rows sharing a `citekey`, each pinning the specific chunk that supports that specific claim. This per-row chunk pin is what makes `validate_citations` able to check "does this chunk actually support this sentence?".

---

## Citation markers & keys

**Markers:** pandoc-style `[@citekey]` inline in `proposal.md`, e.g. `…reduces recurrence by 30% [@smith2021].`. Human-readable and survives in the deliverable.

**Citekey scheme:** `{first-author-surname}{year}`, lowercased, non-alphanumerics stripped — e.g. `smith2021`. Workspace-scoped collisions get letter suffixes in assignment order (`smith2021`, `smith2021a`, …). When metadata is too thin (unapproved/unparseable), fall back to `ref-{short-id}` derived from the reference UUID (e.g. `ref-3a2f`) — stable and collision-free.

**No silent rewrites:** once assigned, a citekey is never auto-rewritten (that would break `[@…]` markers already in the draft). If approved metadata would yield a nicer key, `validate_citations` emits a low-severity "citekey could be upgraded" note; renaming is a deliberate action that updates both the markers and the ledger row.

---

## Metadata extraction pipeline

Runs after text extraction at ingest.

- **Fetched refs** (Crossref / PubMed / arXiv): use the structured metadata the fetcher already returns (currently discarded). `metadata_source='fetcher'`, `metadata_status='approved'` (authoritative; still owner-editable). No LLM.
- **Uploaded refs:**
  1. Read the PDF embedded info dict + simple first-page heuristics.
  2. One cheap, low-temperature LLM pass over the first few-thousand chars extracting `{title, authors, year, doi, venue}` as structured JSON.
  3. Merge per field (embedded > LLM).
  - Result: `metadata_source='heuristic'|'llm'`, `metadata_status='extracted'` — **not** approved.

**Owner approval/correction gate** (mirrors the Phase-0 rubric-review gate): a per-reference metadata panel shows extracted fields; the owner edits any and approves → `metadata_status='approved'`. Citing an unapproved reference is allowed but `validate_citations` and `build_bibliography` warn, so drafting isn't blocked yet unverified metadata can't silently reach the final bibliography.

---

## Tool surface

Four MCP tools added to the drafting agent's allowlist. Deterministic checks are plain Python; semantic checks use the existing LLM adapter.

### `insert_citation(reference_id, claim, chunk_index, stance="supports", confidence=None)`
Resolves/assigns the reference's `citekey`, appends an `evidence` row to `citations.json`, and commits it to the workspace repo. Returns the `[@citekey]` marker string (the agent places it in prose via `edit_proposal`) plus a note if the reference's metadata is unapproved. Pure ledger + git write; never edits prose. Reuses an existing citekey so re-citing the same paper yields the same marker.

### `validate_citations()`
Returns a structured issue list `{type, severity, citekey, location, detail}`:
- **Deterministic:** every `[@key]` marker in `proposal.md` has a ledger row and vice-versa (orphan markers / orphan rows); each row's `reference_id` exists and its `chunk_index` is still present; each cited reference's metadata is `approved` (else warning); citekey-upgrade notes.
- **Semantic (LLM):** for each evidence row, judge whether the cited chunk text supports the `claim` — `supports` / `contradicts` / `unsupported` — flagging mismatches.

### `list_unsupported_claims()`
LLM scans `proposal.md`, identifies factual/empirical claims (vs. framing/transition prose), and returns those lacking a nearby citation marker, each with location and a suggested `search_references` query. Inherently heuristic — "candidates to review," explicitly not exhaustive.

### `build_bibliography(style="numbered")`
Collects citekeys actually used as markers in `proposal.md`, orders them (by first appearance for `numbered`; alphabetical for `author-year`), joins `references` metadata, and writes a rendered References section to a git-tracked `bibliography.md`. Warns on any cited reference whose metadata isn't `approved`. Default `numbered` (Vancouver — standard in biomedical/Karolinska grants); `author-year` available.

**Two boundaries:** the audit tools (`validate_citations`, `list_unsupported_claims`) *report* — they never auto-edit the draft. `build_bibliography` renders from markers actually in the text, so a stale ledger row with no marker never pollutes the bibliography.

---

## Retrieval surfacing

The agent can only cite a chunk whose locator it can see. `search_references` currently returns `reference_id, title, section_path, score, text` — it drops the locator.

- Add `chunk_index` to `search_references` results and to `read_reference_section`, so the agent can pass an exact `chunk_index` to `insert_citation`.
- Include `citekey` in results when the reference already has one.

Underlying data already exists on `ReferenceChunk`; this is an output-shape change in `tools/references.py`.

---

## Phase/gate fit (accepted rework)

The four tools join the always-available drafting allowlist now. When the gated phased workflow is built, they will move to Draft/Review phase gates; metadata approval stays a per-reference gate (not a phase gate). The audit tools target a drafting flow that is not yet phase-enforced — accepted as known rework when the phases land. No behavior change is needed now beyond noting it so the future phase work absorbs these tools.

---

## Testing strategy

- **Metadata extraction:** embedded/heuristic parse; LLM extraction via the fake adapter (deterministic); per-field merge precedence; `failed` path.
- **Approval gate:** `extracted` → owner edit → `approved` transitions; correction persists; fetched refs land `approved`.
- **Citekey:** generation, workspace-scoped collisions + suffixes, `ref-<id>` fallback, no-auto-rewrite + upgrade note, reuse on re-cite.
- **Ledger:** `citations.json` round-trip; `insert_citation` appends + commits + reuses citekey; lazy creation.
- **`validate_citations`:** each deterministic check (orphan marker, orphan row, missing chunk, unapproved-metadata warning) and the semantic supports/contradicts/unsupported verdict via fake adapter.
- **`list_unsupported_claims`:** via fake adapter (claims with/without nearby markers).
- **`build_bibliography`:** numbered + author-year ordering, metadata join, unapproved warning, renders only marker-present keys.
- **Retrieval:** `search_references` / `read_reference_section` include `chunk_index` (+ `citekey` when set).

---

## Out of scope (separate specs)

| Feature | Reason |
|---|---|
| Hybrid/lexical retrieval, reranking, per-reference diversity, dedup (Codex item 4) | Distinct retrieval-quality effort; this spec is provenance, not recall. |
| Cross-workspace / curated shared reference library | Separate corpus + access-control design. |
| Automatic retrieval before evidence-backed drafting | Belongs with the gated Draft-phase behavior. |
| Real phase-gating of the citation tools | Part of the gated phased workflow. |
| PDF section-structure recovery (beyond page-based `section_path`) | Separate extractor improvement (Codex item 5). |

---

## Decision log

| # | Decision | Notes |
|---|---|---|
| D1 | Scope = full evidence ledger **including** audit tools (`validate_citations`, `list_unsupported_claims`), not just provenance plumbing | User chose this over a foundation-only cut, accepting rework when phase-gating lands. |
| D2 | Uploaded-ref metadata = heuristic + LLM extraction with a **mandatory owner approve/correct gate** | LLMs hallucinate; fetched (Crossref/PubMed/arXiv) metadata is authoritative and auto-`approved`. |
| D3 | Approach 1 — **git-artifact ledger + inline markers** | Chosen over a relational DB-table ledger and over a git-markers+DB-rows hybrid: it matches the codebase's git-artifact/audit-trail design, freezes at `draft_sha` for free, keeps a single source of truth, and avoids the DB↔git sync burden the hybrid carries. |
| D4 | Ledger is **claim-level** (per-chunk evidence rows), bib metadata stays in the DB | Enables "does this chunk support this sentence?"; corrected metadata flows without rewriting the ledger. |
| D5 | Citekeys human-meaningful (`smith2021`) with `ref-<id>` fallback; **never auto-rewritten** | Avoids breaking placed markers; upgrades are deliberate. |
| D6 | Audit tools **report, never auto-edit**; `build_bibliography` renders from in-text markers only | Agent/owner stays in control; stale rows can't pollute output. |

---

## Cross-references

- References corpus (ingestion, chunking, search): [`2026-05-25-references-corpus-design.md`](2026-05-25-references-corpus-design.md)
- Phased workflow (gated; this is follow-up #2 there): [`2026-05-19-proposal-assist-phased-workflow-design.md`](2026-05-19-proposal-assist-phased-workflow-design.md)
- Base proposal-assist spec: [`2026-04-18-call-manager-proposal-assist-design.md`](2026-04-18-call-manager-proposal-assist-design.md)
