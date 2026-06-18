# Cockpit SP2a-4b — Select-to-Cite (Write Path) Design

**Status:** approved 2026-06-18 (brainstorming); revised after Codex review (v2). Implementation plan to follow.

**Parent design:** `docs/superpowers/specs/2026-06-17-cockpit-workspace-design.md` §7.1 (Citations & provenance / select-to-cite) and §7.2 (right pane). This doc resolves the *implementation* forks that parent spec left open and is the design-of-record for the SP2a-4b slice.

**Builds on:** SP2a-4a (merged, master `12b0a6b`) — the citation peek + `GET /api/workspaces/{id}/citations/{ev_id}/source` read endpoint + the `SourceViewer` component + the cockpit 3-region layout.

---

## 1. Goal

A workspace **owner** can cite a source directly from the editor: select the claim span → pick a reference → select the supporting span in that reference → commit. The citation is a **direct git commit** (the human is the actor and owns it) — only *AI* citations are proposals. One marker `[@key]<!-- ev:id -->` is inserted after the selected claim; the evidence ledger gains a verified row. This is the **claim-first** primary flow from §7.1; the source-first "cite at cursor" shortcut and the discuss/comment toolbar actions are out of scope (§7).

## 2. Architecture & key decisions

- **A new SYNC core `insert_citation_for_user`, not the agent wrapper.** The agent's `insert_citation_impl` is async, has no section anti-clobber check, uses `(reference_id, chunk_index, evidence_quote)`-only idempotency, and searches for the quote (losing the user's chosen occurrence) — all wrong for a human caller. Instead, add a **synchronous** core that mirrors the proven `insert_from_annotation` (`proposal_assist/analysis/insert.py`): it reuses the same low-level helpers — `acquire_commit_mutex`, `_turn_in_flight`, `insert_marker`, `build_evidence_row`, `write_and_commit_files`, `claim_sentence_around` — so it is DRY at the **helper** level while leaving the agent path untouched. The FastAPI endpoint is a **sync `def`** (FastAPI runs it in the threadpool), so the blocking DB + git work never stalls the event loop.
- **Anchor = the selected claim span; requires a saved section.** `insert_marker` inserts the marker immediately after the anchor substring, which must occur **exactly once** in the whole proposal (occurrences overlapping existing markers are ignored). The anchor must exist in on-disk `proposal.md`, so citing requires the section to be **saved** (clean buffer).
- **Section anti-clobber, mirroring `save_section`.** Saved-but-stale is still unsafe: a concurrent save/agent commit can change the claim context while the anchor substring stays unique (Codex High #1). The request therefore carries `section_index` + `base_commit_sha` + `base_section_sha`, and the core — **inside the commit mutex** — verifies HEAD and the target section's `sha256` are unchanged (the same staleness check as `editing/save.py`), raising a stale error otherwise.
- **The stale-check subsumes idempotency (Codex High #2).** A replayed or concurrent double-submit carries a now-stale `base_section_sha` (the first insert changed the section's bytes) → it fails the stale-check (409) instead of inserting a duplicate marker. A genuinely new citation of the *same evidence sentence for a different claim* uses a different anchor and a fresh base → it inserts correctly. So this endpoint deliberately uses **no** `(ref,chunk,quote)` idempotency rule.
- **Quote = the user's exact selected occurrence, by chunk-local offsets (Codex Medium #4; parent §7.1).** The source pane renders the stored `ReferenceChunk.text` **verbatim as a single text node** (`white-space: pre-wrap`), so a DOM selection yields chunk-local `(quote_start, quote_end)` that map 1:1 to the string. The request carries `chunk_index`, `quote_start`, `quote_end`, and the `evidence_quote` substring; the core verifies `normalize(chunk.text[quote_start:quote_end]) == normalize(evidence_quote)` and stores those offsets in the ledger. This honors a repeated span's actual occurrence instead of failing as ambiguous. No offset index, no `ReferenceChunk` migration.
- **Quote-optional / "cite whole section" is dropped from this slice (Codex Medium #3).** `find_quote_offsets` rejects empty quotes and the ledger/audit assume a quote, so chunk-level cites would need new core/ledger/audit semantics. This slice **requires a non-empty selected supporting span**; chunk-level citing is a future extension (§7).
- **Owner-gated write; member-gated reads.** The POST mutates `proposal.md` → owner-gated (like `save_section`). The reference-list and reference-chunks reads are member-gated (like the draft/peek reads).

## 3. Backend surface (new)

### 3.1 `POST /api/workspaces/{workspace_id}/citations` — owner-gated, **sync** endpoint
Request:
```json
{ "section_index": 1, "base_commit_sha": "<40-hex>", "base_section_sha": "<64-hex>",
  "anchor": "<selected claim span text>", "reference_id": "<uuid>",
  "chunk_index": 0, "evidence_quote": "<selected supporting substring of chunk.text>",
  "quote_start": 12, "quote_end": 48, "stance": "supports" }
```
- `stance` optional, default `"supports"` (`supports|contradicts|background`). No `confidence` (that is an agent self-assessment).
- `evidence_quote` MUST be non-empty (quote-optional dropped).

Handler: `require_owner` → 404 precheck (proposal.md exists + git HEAD, mirroring the save route) → call `insert_citation_for_user(...)` → the endpoint owns the transaction (single `session.commit()` / `rollback`, mirroring the save route), mapping the core's outcomes:

| Outcome | HTTP | code |
|---|---|---|
| success `{ev_id, citekey, commit_sha}` | **200** | — |
| commit-mutex timeout (busy) | **429** + `Retry-After` | `too_many_requests` |
| section stale (HEAD/section sha moved) OR an AI turn is in flight OR anchor not found | **409** | `conflict` |
| anchor ambiguous, or `evidence_quote` ≠ `chunk.text[quote_start:quote_end]`, or chunk_index invalid for the (owned) reference | **422** | `validation_error` |
| reference missing / foreign / tombstoned | **404** | `not_found` |
| git commit failed | **500** | `internal_error` |

(Codex Low #6: foreign/missing ref is **404** — not 422; a bad chunk on an *owned* ref is **422**. The `/api` envelope code for 422 is `validation_error` per `web/api/errors.py`; the plan documents that exactly and adds 422 to the router's `STATUS_CODES` responses map. Human error messages from the core are echoed in the envelope `message`.)

### 3.2 `GET /api/workspaces/{workspace_id}/references` — member-gated
```json
[{ "id": "<uuid>", "citekey": "smith2021|null", "title": "…",
   "kind": "paper", "text_status": "done|pending|…" }]
```
`text_status` is the real `Reference.text_status` field; the composer uses it to label a reference whose source isn't indexed yet (no selectable chunks). Title resolved as in `_citation_map` (`ref.title` → CSL title → filename). Tombstoned references excluded.

### 3.3 `GET /api/workspaces/{workspace_id}/references/{reference_id}/chunks` — member-gated
Ownership-guarded (`ref.workspace_id == workspace_id`, else 404). Returns the chunks to render and select within, ordered by `chunk_index`:
```json
{ "reference_id": "<uuid>", "title": "…",
  "chunks": [{ "chunk_index": 0, "section_path": "## Background|null", "text": "<verbatim chunk.text>" }] }
```
If the reference has no chunks (not indexed), `chunks: []` and the composer shows a "not indexed yet" notice.

## 4. Frontend surface (new)

- **Selection bridge:** add `onSelectionChange?(sel: {from:number; to:number; text:string} | null)` to `CodeMirrorEditor`, fired from the existing `updateListener` on a non-empty selection in an editable section (skip `Transaction.remote` echoes). Cockpit stores the active section's selection.
- **Cite affordance:** when a non-empty selection exists in an editable section that is **not dirty**, surface a "Cite selection" control opening the citation composer in the right pane. Disabled (hint "Save this section first") while the section is dirty. **Cite-only** toolbar in this slice.
- **Citation composer** (right pane, reuses the SourceViewer read style):
  1. `useReferences(id)` → pick a reference (skip/label those with `text_status != "done"`).
  2. `useReferenceChunks(id, refId)` → render each chunk's `text` **verbatim, single text node, `white-space: pre-wrap`** (so selection offsets align). The user selects the supporting span → capture `(chunk_index, quote_start, quote_end, evidence_quote)`.
  3. Confirm chip shows the claim (anchor), chosen reference, and the evidence quote → "Add citation".
  4. `useAddCitation(id)` POSTs `{section_index, base_commit_sha, base_section_sha, anchor, reference_id, chunk_index, evidence_quote, quote_start, quote_end, stance}` — `base_commit_sha`/`base_section_sha`/`section_index` come from the loaded draft for the active section. On success, invalidate `['draft', id]` so the new pill renders, then clear the composer + selection.
- **Hooks** live in `frontend/src/api/citations.ts` (alongside `useCitationSource`).

## 5. Error handling & preconditions

- **Dirty section** → Cite disabled client-side (anchor must be on disk).
- **Section stale / AI turn in flight (409)** → "The document changed (or an AI turn is running) — reopen and re-cite"; the draft is invalidated so the user re-selects against fresh text. **Anchor ambiguous / quote-mismatch / bad chunk (422)** → show the reason and **preserve** the composer so the user can pick a longer claim span or a different supporting span. **Reference gone (404)** → "that source is no longer available." **Busy (429)** → retry. **500/network** → generic error, composer preserved.
- A replayed/concurrent submit fails the stale-check (409), so no duplicate marker is created (idempotency via anti-clobber, §2).
- **Known limitation:** the 4a peek (`locate_evidence`) recomputes the highlight span live and may be imprecise for a quote that repeats inside one chunk; the ledger's stored offsets remain authoritative. Fixing the peek to prefer stored offsets is out of scope here.

## 6. Testing

**Backend** (mirror `tests/test_api_workspaces.py` + the citations-insert seeding in `tests/proposal_assist/tools/test_citations_insert.py`):
- POST happy path: marker inserted after the anchor + ledger row committed (offsets = `[quote_start, quote_end]`); returns `{ev_id, citekey, commit_sha}`.
- owner-gate: member → 403.
- section stale: a concurrent commit changes the section → 409; **double-submit** (same base shas) → second is 409, exactly one marker exists.
- AI turn in flight → 409.
- anchor not found → 409; anchor ambiguous (claim repeats) → 422.
- quote mismatch (`evidence_quote` ≠ `chunk.text[quote_start:quote_end]`) → 422; chunk_index invalid on an owned ref → 422.
- reference foreign/missing/tombstoned → 404.
- busy (mutex timeout) → 429 with `Retry-After`.
- repeated-span occurrence: two identical substrings in one chunk, citing each occurrence stores the correct distinct offsets (no false ambiguity).
- `GET .../references`: member-gated, excludes tombstoned, returns `text_status`.
- `GET .../references/{id}/chunks`: ordered chunks, ownership-guarded 404, empty list when unindexed.

**Frontend** (mirror `Cockpit.test.tsx` / `SourceViewer.test.tsx`, spying on `clientMod.api`): `onSelectionChange` fires on selection; Cite gated on dirty; composer pick-reference → chunks render → select-span → confirm → POST carries the right body (incl. base shas + offsets); 409/422 error surfacing preserves the composer; pill appears after success (draft invalidation).

## 7. Out of scope (later slices)

Quote-optional / chunk-level "cite whole section" (needs core/ledger/audit changes); source-first "cite at cursor" shortcut; the discuss/comment floating-toolbar actions; the AI propose→accept citation-review path (the `proposed_change` flow already exists from SP2a-2); making the 4a peek prefer the ledger's stored offsets; inline citation-impact / `claim_drift` annotations.
