# Cockpit SP2a-4b — Select-to-Cite (Write Path) Design

**Status:** approved 2026-06-18 (brainstorming). Implementation plan to follow.

**Parent design:** `docs/superpowers/specs/2026-06-17-cockpit-workspace-design.md` §7.1 (Citations & provenance / select-to-cite) and §7.2 (right pane). This doc resolves the *implementation* forks that parent spec left open and is the design-of-record for the SP2a-4b slice.

**Builds on:** SP2a-4a (merged, master `12b0a6b`) — the citation peek + `GET /api/workspaces/{id}/citations/{ev_id}/source` read endpoint + the `SourceViewer` component + the cockpit 3-region layout.

---

## 1. Goal

A workspace **owner** can cite a source directly from the editor: select the claim span → pick a reference → select the supporting span in that reference → commit. The citation is a **direct git commit** (the human is the actor and owns it) — only *AI* citations are proposals. One marker `[@key]<!-- ev:id -->` is inserted after the selected claim; the evidence ledger gains a verified row. This is the **claim-first** primary flow from §7.1; the source-first "cite at cursor" shortcut and the discuss/comment toolbar actions are explicitly out of scope (§7).

## 2. Architecture & key decisions

- **Reuse the agent core, human as actor.** `ToolContext` (`proposal_assist/tools/registry.py`) is a plain dataclass. A new **async** endpoint constructs a minimal `ToolContext(workspace_id, repo_dir, session_factory, settings)` and `await insert_citation_impl(ctx, ...)` (`tools/citations.py`). This reuses the exact verification (anchor uniqueness via `insert_marker`; quote-in-chunk via `find_quote_offsets`), the atomic `proposal.md` + `citations.json` commit, and the per-workspace Postgres commit-mutex that already serializes against agent turns. **No core refactor; no DB migration.**
- **Anchor = the selected claim span.** `insert_marker` inserts the marker immediately after the anchor substring, which must occur **exactly once** in the whole proposal (occurrences overlapping existing markers are ignored). Therefore citing requires the section to be **saved** — the anchor must exist in on-disk `proposal.md`, not just the dirty editor buffer.
- **Quote = the literal selected substring of `chunk.text`.** The source-selection pane renders the stored `ReferenceChunk.text` verbatim; a DOM selection within a chunk yields `(chunk_index, evidence_quote = selected substring)`. The backend re-verifies `quote ∈ chunk.text`. No offset index, no `ReferenceChunk` migration (`models.py`).
- **Owner-gated write; member-gated reads.** Writing a citation mutates `proposal.md`, so the POST is owner-gated (consistent with `save_section`). The reference-list and reference-chunks reads are member-gated (consistent with the draft/peek reads).

## 3. Backend surface (new)

### 3.1 `POST /api/workspaces/{workspace_id}/citations` — owner-gated, async
Request:
```json
{ "reference_id": "<uuid>", "anchor": "<selected claim span text>",
  "chunk_index": 0, "evidence_quote": "<selected supporting span, or \"\">",
  "stance": "supports" }
```
- `stance` optional, default `"supports"` (`supports|contradicts|background`).
- `evidence_quote` may be `""` for the **quote-optional / chunk-level** cite ("cite whole section").

Handler: owner-gate → 404 precheck (proposal.md exists + git HEAD, mirroring the save route) → build minimal `ToolContext` → `await insert_citation_impl(ctx, reference_id=, anchor=, chunk_index=, evidence_quote=, stance=)` → map the result dict:

| `insert_citation_impl` result | HTTP |
|---|---|
| `{ev_id, citekey, commit_sha}` (incl. `idempotent`) | **200** `{ev_id, citekey, commit_sha}` |
| `{"error": <busy>}` (`CommitMutexTimeout`) | **429** + `Retry-After`, message echoed |
| `{"error": <anchor not found>}` | **409** `conflict` ("this section changed / save it first — reopen") |
| `{"error": <anchor ambiguous / quote not found / reference or chunk unavailable>}` | **422** `unprocessable` (human reason echoed so the composer can guide the fix) |
| `{"error": <git commit failed>}` | **500** |

(The exact error-string→status mapping is pinned in the plan against the literal strings `insert_citation_impl` returns; `422` is added to the `/api` `STATUS_CODES` map if not already present, so the envelope is never sanitized to `internal_error`.)

### 3.2 `GET /api/workspaces/{workspace_id}/references` — member-gated
Returns the workspace's references:
```json
[{ "id": "<uuid>", "citekey": "smith2021|null", "title": "…",
   "kind": "paper", "text_status": "done|pending|…" }]
```
`text_status` is the real `Reference.text_status` field (the extraction/indexing state); the composer uses it to label a reference whose source isn't indexed yet (it will have no selectable chunks). Title resolved as in `_citation_map` (`ref.title` → CSL title → filename). Tombstoned references excluded.

### 3.3 `GET /api/workspaces/{workspace_id}/references/{reference_id}/chunks` — member-gated
Ownership-guarded (`ref.workspace_id == workspace_id`, 404 otherwise). Returns the chunks to render and select within:
```json
{ "reference_id": "<uuid>", "title": "…",
  "chunks": [{ "chunk_index": 0, "section_path": "## Background|null", "text": "<verbatim chunk.text>" }] }
```
Chunks ordered by `chunk_index`. If the reference has no chunks (not yet indexed), `chunks: []` and the composer shows a "not indexed yet" notice.

## 4. Frontend surface (new)

- **Selection bridge:** add `onSelectionChange?(sel: {from:number; to:number; text:string} | null)` to `CodeMirrorEditor`. Fire it from the existing `updateListener` when the selection is non-empty AND the editor is editable (skip `Transaction.remote` echoes). Cockpit stores the current selection for the active section.
- **Cite affordance:** when a non-empty selection exists in an editable section that is **not dirty**, surface a "Cite selection" control that opens the citation composer in the right pane. Disabled (with hint "Save this section first") while the section is dirty. Toolbar is **Cite-only** in this slice.
- **Citation composer** (right pane, reuses the SourceViewer read style):
  1. `useReferences(id)` → pick a reference.
  2. `useReferenceChunks(id, refId)` → render the chunks verbatim; the user selects the supporting span → `(chunk_index, evidence_quote)`, **or** clicks "Cite whole section" on a chunk → `evidence_quote=""`.
  3. Confirm chip shows the claim (anchor), chosen reference, and the evidence quote → "Add citation".
  4. `useAddCitation(id)` POSTs `{reference_id, anchor, chunk_index, evidence_quote, stance}`; on success invalidate `['draft', id]` so the new pill renders, then clear the composer + selection.
- **Hooks** live in `frontend/src/api/citations.ts` (alongside `useCitationSource`).

## 5. Error handling & preconditions

- **Dirty section** → Cite disabled client-side (anchor must be on disk).
- **Anchor not found (409)** → "This section changed or isn't saved — reopen/save and retry." **Anchor ambiguous / quote-not-in-chunk / reference-or-chunk gone (422)** → show the reason; **preserve** the selection + composer so the user can pick a longer claim span or a different supporting span. **Busy (429)** → "workspace is busy, retry." **500/network** → generic error, composer preserved.
- The write is idempotent at the `insert_citation_impl` layer (same ref+chunk+quote with a live marker is a no-op returning the existing ev).

## 6. Testing

**Backend** (mirror `tests/test_api_workspaces.py` + the citations-insert seeding in `tests/proposal_assist/tools/test_citations_insert.py`): POST happy path (marker + ledger committed, returns ev/citekey/sha); owner-gate (member → 403); anchor-not-found → 409; ambiguous-anchor → 422; quote-not-in-chunk → 422; quote-optional (`evidence_quote=""`) happy path; busy → 429; idempotent re-POST; cross-workspace reference → 404; `GET .../references` (list, member-gated, excludes tombstoned); `GET .../references/{id}/chunks` (ordered chunks, ownership-guarded 404, empty-when-unindexed).

**Frontend** (mirror `Cockpit.test.tsx` / `SourceViewer.test.tsx`, spying on `clientMod.api`): `onSelectionChange` fires on selection; Cite gated on dirty; composer pick-reference → chunks render → select-span → confirm → POST with the right body; quote-optional path; error surfacing preserves the composer; pill appears after success (draft invalidation).

## 7. Out of scope (later slices)

Source-first "cite at cursor" shortcut; the discuss/comment floating-toolbar actions; the AI propose→accept citation-review path (the proposed_change flow already exists from SP2a-2); inline citation-impact/`claim_drift` annotations.
