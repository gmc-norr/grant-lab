# Collab Slice 3b-5a — Citation-insert parity (Ask / Edit / Cite toolbar in the collab editor)

**Date:** 2026-07-04
**Status:** Design (approved-in-shape; Codex amendments folded in)
**Builds on:** 3b-4 presence/cursors (merged `b6e60e3`). Part of the 3b-5 flip decomposition
(3b-5a parity → 3b-5b presence follow-ups → 3b-5c tail-bounding → **3b-5d the flip**, LAST).
**Feature flag stays OFF in prod.** This slice is inert with the flag off; it activates only
for collab-enabled artifacts, of which prod has none until 3b-5d.

---

## 1. Goal

Bring the CM6 select-to-cite / selection-assist experience to parity in the Tiptap collab
editor: a floating **Ask / Edit / Cite** selection toolbar that works while collaborative
editing is live. Today these actions are wired only to the CodeMirror-6 editor and validate
against **git** `proposal.md`; under collab the canonical document is the live Yjs room, so the
git-canonical paths mis-fire. This slice adds collab-canonical branches so the three actions
behave correctly against the room.

## 2. Decisions (locked)

- **Shape:** full **Ask / Edit / Cite** selection toolbar ported to the Tiptap collab editor
  (`CollabEditor`), reusing the existing Ask/Edit presets and `CitationComposer`.
- **Permission — Cite = any writer.** Citation insert under collab is gated on `can_write`
  (any member who may write the draft), **not** owner-only. The endpoint uses the **same
  authority as the WS write path** — `resolve_access(session, user=…, workspace_id=…,
  artifact="draft")` — so the UI's `can_write` and the backend gate agree.
- **Ask / Edit governance unchanged.** `ask` stays any-member; `modify` (Edit) and free-chat
  stay owner-only (agent turns consume the hosted-LLM budget — that governance is orthogonal
  to collab and not relaxed here). What changes for Ask/Edit is only the *selection-verification
  source* (git → room-canonical) so they stop false-rejecting under collab.
- **Server applies the Cite to the room** (approach A, approved). The backend applies the
  marker to the live Yjs room server-side and co-commits the ledger; the citation node then
  reaches every connected client (including the initiator) via the normal Yjs broadcast. The
  frontend does **not** insert the node itself. Server-authoritative, atomic ledger co-commit,
  identical to how the agent cites.

## 3. Non-goals

- No flag flip, no `enable_collab` call in `src/`. (That is 3b-5d.)
- No database migration, no schema change. All backend changes are additive.
- CM6 flag-off behavior stays **byte-for-byte** unchanged (hard gate). Every git-canonical path
  (`insert_citation_for_user` git branch, `build_selection_block` git branch, `get_draft` git
  read) is untouched when the artifact is not collab-enabled.
- Not relaxing Edit/free-chat to any-writer. Not touching spec-artifact citing (the spec is
  citation-free).

## 4. Architecture

Six coordinated pieces (A–F). A, B, C, D are backend; E is frontend; F is the shared authority.

### A. Read-model: room-canonical draft under collab (`get_draft`)

**Problem (verified).** [`get_draft`](../../../src/call_manager/web/api/workspaces.py#L249)
reads `raw = proposal.read_text()` (git) at line 268 and builds `sections = _sections_out(raw)`
and `citations = _citation_map(…, raw)` from it. It does **not** checkpoint-before-read under
collab (unlike the spec-save handlers, which call `checkpoint_artifact` before a save). So under
collab, `DraftOut.sections[i].sha` is the **git** section SHA, which lags the live room after any
uncheckpointed edit — i.e. during any active session. Consequences:

1. The toolbar's `base_section_sha` (read from `DraftOut`) would be git-canonical, but the
   kernel's staleness guard checks the **live room** section SHA → **false stale on every
   cite/edit** while a session has unflushed edits.
2. Independently: Outline, pills, the Full-draft tab, and the citation map would render **stale
   git content** while the live editor shows diverged room content — a correctness/UX bug.

**Fix.** Under collab (`is_collab_enabled(session, workspace_id, artifact="draft")`), compute the
read-model's `raw` from the **room**, read-only, without a git write or the commit mutex:

```python
raw = serialize_doc_to_markdown(build_doc(load_rows(session, workspace_id=workspace_id, artifact="draft")))
```

using the existing `checkpoint.serialize_doc_to_markdown` (`= pm_to_markdown(ydoc_to_pm_json(doc))`),
`hydrate.build_doc`, and `store.load_rows`. Pass this `raw` to **both** `_sections_out(raw)` and
`_citation_map(…, raw)` so sections **and** the citation map are room-canonical and mutually
consistent. Flag-off: unchanged git read.

- `head` stays `current_head(repo)` (git HEAD). Under collab the commit SHA is **not** the
  staleness gate (the kernel guards on section SHA), so a git-lagged `head` is harmless; the
  client still echoes it as `base_commit_sha`.
- **Cost:** `build_doc(load_rows(...))` on each collab `get_draft`. Bounded by 3b-1 compaction
  (the log is merged-first: ~1 snapshot row + a short tail), so this is O(recent tail), not O(all
  edits). Acceptable for the editor's primary read.
- Applies to the **draft** only in this slice. `get_spec` may get the same treatment for
  consistency, but the spec is citation-free and not part of the toolbar surface — **out of scope
  here** (leave `get_spec` git-canonical; note it as a 3b-5 follow-up so Spec Outline SHAs are
  addressed with the flip, not piecemeal).

### B. Selection verification: collab-canonical branch (`build_selection_block`) — Ask / Edit

**Problem (verified).**
[`build_selection_block`](../../../src/call_manager/proposal_assist/turns/selection.py#L41) reads
git `proposal.md`, splits sections, and marks the selection **verified** iff
`live.sha == base_section_sha`. A `modify` (Edit) that isn't verified raises
`SelectionConflictError` → 409 ([selection.py:71-72](../../../src/call_manager/proposal_assist/turns/selection.py#L71)).
Under collab, git lags the room, so a live Edit is **never** verified → **always rejected**. `ask`
degrades to `verified=False` (still allowed, just labeled) — functional but wrong-canonical.

**Fix.** Give `build_selection_block` a collab-canonical source for `sections`. When the draft
artifact is collab-enabled, derive the section text from the **room** (same
`serialize_doc_to_markdown(build_doc(load_rows(...)))` snapshot as A → `split_sections`) instead
of `proposal.read_text()`. The offset/anchor validation logic is otherwise unchanged. This makes
Edit verifiable under collab and Ask verified when the section is clean.

- Threading: `build_selection_block(*, repo_dir, sel)` currently takes only `repo_dir`. Add an
  optional collab source — cleanest is to pass the already-computed room `sections` (or a
  `session`+`workspace_id` so it can compute them) from the turns route
  ([turns.py:147](../../../src/call_manager/web/api/turns.py#L147)), which already holds the
  session. Keep the git path as the default (flag-off parity).
- **Governance unchanged:** the turns route still gates `ask`→member, `modify`→owner
  ([turns.py:103-108](../../../src/call_manager/web/api/turns.py#L103-L108)). Only the
  verification canonical changes. Owner is a writer, so owner-Edit now works under collab.

### C. Direct citation under collab: extract kernel helper + collab branch in `insert_citation_for_user`

**Problem (verified).**
[`insert_citation_for_user`](../../../src/call_manager/proposal_assist/citations/human_cite.py)
is **git-only**: it reads `proposal.md` from git, validates staleness against git HEAD + section
SHA + anchor occurrences, `insert_marker`s into the git doc, and commits `proposal.md` +
`citations.json` via `write_and_commit_files`. For a collab artifact the Yjs room is canonical,
so this git commit would be invisible to the live editor and later overwritten by a room
checkpoint. There is **no** `is_collab_enabled` branch today.

**Fix.**

1. **Extract a pc-free helper** from
   [`kernel_accept_citation`](../../../src/call_manager/proposal_assist/collab/kernel.py#L162) —
   the tail from "acquire mutex → load ledger → `compute_citation_insert` → section-scoped apply
   → `append_update` → `checkpoint_artifact(extra_files=ledger)` → `_write_citekey_cache`"
   ([kernel.py:207-277](../../../src/call_manager/proposal_assist/collab/kernel.py#L207-L277)).
   Parameterize by `(room_doc, workspace_id, section_index, base_section_sha, anchor,
   reference_id, chunk_index, evidence_quote, quote_start, quote_end, stance, confidence,
   actor)`. The pc-specific bits (`pc.status`/`resolved_by`/`resulting_commit_sha` flips) stay in
   `kernel_accept_citation`, which now calls the shared helper. This keeps the proven
   room-apply + atomic ledger co-commit + staleness-vs-live-section-SHA machinery in ONE place.

2. **Keep the direct-user protection.** The pc-based path trusts a section-validated propose
   step; the direct path has none. So the helper (for the direct path) adds the check
   `human_cite` has: **verify the anchor occurs in the selected live section**
   (`sections[section_index]`) **before** the whole-doc `insert_marker` runs — reject with the
   same user-safe stale/invalid error otherwise. (Prevents citing a whole-doc-unique anchor that
   the user selected in a different section, and gives a precise error when the anchor is
   section-ambiguous.)

3. **Add the collab branch to `insert_citation_for_user`.** When
   `is_collab_enabled(session, workspace_id, artifact="draft")`: acquire the room via
   `MANAGER.operate(...)` (the server-side room accessor used by the kernel accepts) and call the
   extracted helper with the writer as `actor`. Return `{ev_id, citekey, commit_sha}` exactly as
   the git path does. Flag-off: the existing git branch, unchanged.

4. **Result reaches clients via broadcast.** The helper's `append_update` + checkpoint emits a
   Yjs update; the room broadcasts it; every client (including the initiator) renders the new
   `Citation` node. No client-side node insertion.

### D. `compute_citation_insert`: exact-offset preservation

**Problem (verified).**
[`compute_citation_insert`](../../../src/call_manager/proposal_assist/tools/citations.py#L576)
re-derives the evidence-quote span via `find_quote_offsets(chunk.text, evidence_quote)`
([citations.py:634](../../../src/call_manager/proposal_assist/tools/citations.py#L634)) and
returns `{"error": …}` on `QuoteAmbiguous` (quote occurs multiple times in the chunk). The direct
human path (`insert_citation_for_user`) instead accepts exact `quote_start`/`quote_end` and
validates them. If the collab direct path routes through `compute_citation_insert` as-is, a user
who deliberately selected one occurrence of a repeated phrase in the reference viewer would be
**rejected** (ambiguous) where the git path **succeeds** — a parity regression.

**Fix.** Extend `compute_citation_insert` with **optional** `quote_start: int | None = None`,
`quote_end: int | None = None`. When both are provided, verify
`chunk.text[quote_start:quote_end] == evidence_quote` and use those offsets (deterministic
disambiguation) instead of `find_quote_offsets`; on mismatch return the existing
`{"error": "evidence quote not found in chunk"}`. When absent, behavior is exactly as today
(the kernel/agent callers pass nothing → unchanged). The collab direct helper passes the exact
offsets through. Flag-off git path untouched.

### E. Frontend: `TiptapSelectionToolbar`

A floating toolbar shown on a non-empty text selection inside `CollabEditor`, mirroring
[`SelectionToolbar`](../../../frontend/src/cockpit/SelectionToolbar.tsx)'s Ask / Edit / Cite,
reusing the same Ask/Edit presets and `CitationComposer`.

**Anchor derivation from the ProseMirror selection.**
- **anchor** = `editor.state.doc.textBetween(from, to, "\n\n", "")` (block separator `"\n\n"`,
  no leaf text) — matches the plain-text the backend anchor-matches.
- **section_index** = count of level-2 heading nodes (ProseMirror `heading`, `attrs.level === 2`)
  among top-level nodes **before** the block containing the selection start. Section 0 = the
  preamble before the first H2 (must match `split_sections`' convention). The backend's
  section+anchor+SHA checks are **authoritative** — a client miscount surfaces as a safe
  stale/anchor-not-found the user retries.
- **base_section_sha** = `DraftOut.sections[section_index].sha` from the (now room-canonical, per
  A) read-model.
- **base_commit_sha** = `DraftOut.head`.

**Selection constraints (first version, per Codex).**
- **Reject cross-section selections:** if the selection's `$from` and `$to` fall in different
  top-level sections, disable Cite/Edit (tooltip: "select within one section").
- **Reject heading-only selections** for Cite (a citation on a heading is not meaningful).
- No CM6-specific concepts port: **no `isDirty`/"Save section to cite" gate** (collab has no
  local dirty buffer — the doc is always live), and **no string-offset math** for Cite (the
  claim anchor is text-based; the backend matches by text + section + SHA).

**Transport-settled guard (per Codex).** Disable Cite and Edit when the collab provider is
**disconnected or not settled/synced** (e.g. `provider.connected && provider.synced`, and not
mid-inflight). Rationale: text just typed locally can race ahead of the server; acting before the
local doc has converged with the server produces a false stale / anchor-not-found. Ask *could*
stay enabled (it tolerates unverified), but gate all three uniformly for a predictable UI. The
kernel staleness guard remains the correctness backstop; this guard removes the avoidable
false-reject.

**Wiring.** Ask/Edit → build the existing `SelectionContext` (`request_type`,
`target_artifact:"draft"`, `section_index`, `base_commit_sha`, `base_section_sha`, `anchor`) and
call `useSubmitTurn` — but derive `from_offset`/`to_offset` **section-locally** from the anchor's
position in the canonical section markdown (best-effort; the anchor text is the primary signal
the backend verifies). Cite → open `CitationComposer` (reference picker + supporting-span
selection) → `useAddCitation` POST → backend (C) applies to the room. On success the pill appears
live via broadcast; the composer closes.

### F. Permissions (shared authority)

The citation-insert endpoint (the collab branch of `AddCitation`) gates on
`resolve_access(session, user=…, workspace_id=…, artifact="draft") → can_write`
([ws_auth.py:93](../../../src/call_manager/proposal_assist/collab/ws_auth.py#L93)) — the identical
gate the WS write path and `DraftOut.can_write` already use, so the toolbar's enabled/disabled
state and the backend's accept/reject never disagree. (Flag-off, the existing owner-only git path
is unchanged.) The frontend shows Cite only when `DraftOut.can_write`.

## 5. Data flow (Cite, end-to-end under collab)

1. User selects claim text in `CollabEditor`; toolbar appears (writer + settled + single-section).
2. Cite → `CitationComposer`: pick reference + select the supporting evidence span (exact
   `quote_start`/`quote_end`).
3. POST citation with `{section_index, base_commit_sha, base_section_sha, anchor, reference_id,
   chunk_index, evidence_quote, quote_start, quote_end, stance}`.
4. Endpoint: `resolve_access → can_write` gate (F). `insert_citation_for_user` collab branch (C):
   `MANAGER.operate` room → shared helper → staleness vs **live** section SHA → section-anchor
   pre-check → `compute_citation_insert` (with exact offsets, D) → section-scoped
   `apply_pm_section_to_fragment` → `append_update` → `checkpoint_artifact(extra_files=ledger)` →
   `_write_citekey_cache`. One transaction; a git failure rolls back both the room marker and the
   ledger row.
5. Room broadcasts the Yjs update → all clients render the `Citation` pill live. Response
   `{ev_id, citekey, commit_sha}` closes the composer.

Ask/Edit reuse the existing turn → agent → `kernel_accept_prose` path unchanged, except the
selection is now verified collab-canonically (B).

## 6. Flag-off parity (hard gate)

With the draft artifact **not** collab-enabled, every path above takes its existing git branch
**byte-for-byte**: `get_draft` git read (A), `build_selection_block` git verification (B),
`insert_citation_for_user` git commit (C), `compute_citation_insert` with offsets absent (D).
`compute_citation_insert`'s new optional params default to `None` → the kernel/agent callers are
unchanged. The parity gate asserts the CM6 select-to-cite + selection-assist suites are unaffected.

> Note on "parity": flag-off parity means the **git path is unchanged**, not that flag-on produces
> git-identical bytes — under collab the canonical is the room, so the doc bytes legitimately
> differ. The invariant is: collab OFF ⇒ today's behavior exactly; collab ON ⇒ correct against the
> room.

## 7. Testing

**Backend.**
- A: collab `get_draft` returns **room-canonical** section SHAs + citation map (diverge git from
  room, assert the read reflects the room); flag-off returns git (unchanged).
- B: `build_selection_block` collab branch verifies against the room (a live-but-uncheckpointed
  Edit is **verified**, not 409); flag-off git branch unchanged.
- C: extracted helper applies the marker to the room (not git-canonical); ledger co-commit atomic;
  staleness vs live section SHA; **section-anchor pre-check** rejects an anchor absent from the
  selected section; `can_write` (any writer) permitted, non-writer rejected; `kernel_accept_citation`
  still passes (helper extraction is behavior-preserving).
- D: `compute_citation_insert` with exact offsets disambiguates a repeated quote that the
  offset-less path rejects as ambiguous; offsets-absent path byte-identical to today.
- F: endpoint authority matches `resolve_access` (owner in setup/locked = allowed as owner;
  member in active draft = allowed; non-member = 403).
- Always run grouped dirs incl. `tests/test_api_spec.py` with `EMBEDDING_MODEL=bge-m3`,
  `-p no:cacheprovider`.

**Frontend.**
- `TiptapSelectionToolbar`: renders on selection; hidden when `!can_write`; Cite/Edit disabled
  when disconnected/unsettled and when the selection spans sections; anchor/section_index derived
  correctly from a PM selection; Cite opens `CitationComposer` and calls `useAddCitation`.
- `pnpm tsc --noEmit` clean; full `pnpm vitest run` green.

**Live e2e (de-risk).** A collab-on two-client check that a Cite from client A renders the pill on
client B via broadcast — **authored**, run if Docker is available, else flagged NOT-RUN in the
report (do not silently skip). Full flag-on e2e is 3b-5d's de-risk; here we prove the round-trip.

## 8. Constraints / process

- Worktree isolation (`.worktrees/collab-slice3b5a`, base current master), detached SDD,
  stop-before-merge. `git add` explicit paths only. Merge LOCAL only, no push.
- No migration; no schema change; no `enable_collab`/flag flip in `src/`.
- Pinned deps unchanged (no new frontend dep expected; if `CitationComposer` reuse needs none,
  add none). Any fresh `pnpm add` uses `--config.minimumReleaseAge=0`.
- Do NOT invoke `/code-review` or `/ultrareview`.
- Independent verify + adversarial review before merge (the 3-lens pass that caught the 3b-4 DoS
  earns its keep — pay attention to the extracted helper's error/rollback arms and the new
  read-path cost).

## 9. Deferred / follow-ups

- `get_spec` room-canonical read (Spec Outline SHAs) — fold into 3b-5 with the flip, not piecemeal.
- Ask/Edit `from_offset`/`to_offset` precision under collab is best-effort (anchor text is the
  primary signal). If the agent preamble ever depends on exact offsets, revisit.
- The 3 documented 3b-4 presence follow-ups remain **3b-5b** (not this slice).
