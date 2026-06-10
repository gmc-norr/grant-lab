# User editing + claim analysis of user-added text — design

**Date:** 2026-06-10 (rev 3 — incorporates two Codex review rounds, all findings verified against code)
**Status:** Approved (brainstorm with owner); rev 3 pending owner re-review
**Builds on:** `2026-04-18-call-manager-proposal-assist-design.md` (workspaces, turn queue, git-backed artifact), the citations system (slices 1–4: markers, evidence ledger, deterministic validation, semantic audit, bibliography).

## Problem

Today proposal.md is agent-only. The user converses in chat; only the drafting
agent edits the artifact (`edit_proposal` / `replace_proposal` tools, atomic git
commits). A researcher who wants to write or fix a paragraph themselves has no
path to do it. And the citation machinery (claim extraction, evidence audit)
only runs around agent-authored text.

This design adds:

1. **Direct section-level editing** of proposal.md by the workspace owner.
2. **Automatic claim analysis of user-added text** — detecting factual claims,
   auditing existing citations, and suggesting references from the workspace
   library with verifiable evidence quotes.

## Decisions (resolved in brainstorm)

| # | Question | Decision |
|---|----------|----------|
| 1 | Division of labor | **User edits, agent reviews.** User saves land immediately as commits; the system responds with annotations (analysis, suggestions), never silent rewrites. The agent is instructed not to overwrite user prose unprompted (best-effort; see §5). |
| 2 | Editing granularity | **Section-level.** Rendered artifact pane stays; each `##` section gets an Edit button that swaps to a textarea (HTMX). No whole-document editor, no block-level inline editing. |
| 3 | Analysis trigger | **Two-tier.** Tier 1: deterministic validation, synchronous on every save, free. Tier 2: semantic LLM analysis, auto-enqueued on save, content-debounced and diff-scoped (only new/changed sentences cost LLM calls). |
| 4 | Suggestion source | **Workspace library only.** Candidates come from references uploaded to the workspace (extracted, chunked, **already embedded at ingest** — `ReferenceChunk.embedding`). Claims with no match are flagged honestly. No PubMed lookup in this slice (cheap escape hatch later: link to a pre-filled PubMed search). |
| 5 | Concurrency | **Lock during agent turns** (product policy) + **per-workspace commit mutex** (correctness guarantee). See §1. A `base_commit_sha`/`base_section_sha` precondition rejects stale forms — no merge UX; a stale save asks the user to reopen the section. |
| 6 | Who can edit | **Owner only**, matching the existing `_require_owner` policy (which does *not* grant admins access — admins are not workspace editors). Viewers remain read-only. Shared editing is out of scope. |

## 1. Section editing

### Sectionizer

A deterministic parser splits proposal.md on `##` headings (ATX level-2 only;
fenced code blocks are respected — a `##` inside a fence is not a boundary):

- Content before the first `##` (title, abstract) is section 0 ("preamble",
  heading = empty string).
- Each section = heading line + body until the next `##` or EOF.
- **Round-trip guarantee:** `join(split(text)) == text` byte-identical. This is
  the core invariant; saving section *n* replaces exactly that byte range.

Section identity for the UI is `(index, sha256(section_text))`. The SHA, not
the index alone, backs the stale check — heading renames or reordering by the
agent invalidate open edit forms.

### Write serialization (two layers)

Verified constraint: the drafting agent's advisory lock
(`controller.py` launch sequence) is **session-scoped and held for the
subprocess's entire lifetime, including idle** (swept after ~30 min). User
saves therefore cannot take that lock, and a "no queued/running turn" query
before commit is check-then-act — racy on its own.

Two layers, with distinct jobs:

1. **Commit mutex (correctness, enforced).** A per-workspace
   transaction-scoped Postgres advisory lock (`pg_advisory_xact_lock`, new key
   class distinct from the subprocess lock) acquired by **every** path that
   mutates the workspace git repo: the agent's mutation tools
   (`edit_proposal`, `replace_proposal`, `insert_citation`, `remove_citation`,
   `update_citation`, `rename_citekey` — all run in-process as SDK MCP tools,
   so they can share it) and the new user save/insert routes. Inside the
   mutex: re-read HEAD, apply preconditions, write, commit. Commits can no
   longer interleave; preconditions are evaluated against a HEAD that cannot
   move underneath them.
2. **Turn lock (policy, made sound).** User save/insert routes, after
   acquiring the commit mutex, reject with 409 if any TurnJob for the
   workspace is queued or running ("assistant is working — try again when the
   turn finishes"). For this check to be authoritative rather than
   check-then-act, **turn enqueue participates in the mutex**:
   `submit_user_turn` acquires the same per-workspace commit mutex for its
   (already short) enqueue transaction. Either the enqueue lands first and
   the user save sees the queued turn (409), or the save lands first and the
   turn starts strictly after the user's commit.
3. **`replace_proposal` read precondition (enforced).** Verified:
   `replace_proposal_impl` writes model-supplied content with no read of
   HEAD — an in-flight replace generated before a user save would silently
   drop the user's text from HEAD (recoverable in history, but a lost
   update). The tool gains a precondition: the handle tracks the agent's
   *view of HEAD* — updated by `read_proposal` AND by each of its own
   successful mutations (the tools already return the new commit SHA), so
   multi-edit turns keep working without re-reads. `replace_proposal` (and
   `edit_proposal`, for uniformity) fails with "proposal changed since you
   read it — re-read" unless that SHA equals current HEAD inside the mutex.
   A blind replace becomes a tool error, not a lost update. The agent can still rewrite
   user prose *after reading it* — that remains the best-effort policy of
   §5 — but it can never overwrite text it has not seen.

**Race tests are required** (see §7): concurrent user-save vs agent-tool-write
must produce two clean sequential commits; concurrent user-save vs turn
enqueue must end in exactly one of the two serialized orders; a replace
generated against a stale read must fail, never drop a user commit from HEAD.

### Routes

All under the existing workspaces router, **owner-only** (`_require_owner`),
CSRF-protected, 404 on soft-deleted workspaces.

- `GET /workspaces/{id}/sections/{n}/edit` → HTMX fragment: textarea with the
  raw markdown of section *n*, hidden fields `base_commit_sha` and
  `base_section_sha`, Save/Cancel buttons. Returns 409 (with a rendered notice
  fragment) if a turn is queued or running.
- `POST /workspaces/{id}/sections/{n}` → save. Inside the commit mutex, in
  order:
  1. **Turn lock:** queued/running TurnJob → 409.
  2. **Stale form:** if HEAD ≠ `base_commit_sha`, re-split HEAD's proposal.md;
     if the section at *n* no longer matches `base_section_sha` → 409 "this
     section changed underneath you — reopen it". (If HEAD moved but the
     section is byte-identical, the save proceeds.)
  3. Reassemble the document with the replaced section; commit via the
     existing `write_and_commit_files` (atomic, rollback on failure). Commit
     message: `user edit: <heading> (user:<id>)`.
  4. **Provenance:** write an `ArtifactVersion` row with `created_by="user"`
     (the check constraint already includes `'user'`). Without this, the
     reconciliation path would later label user commits `reconciled`.
  5. Write a `WorkspaceEvent` (`user_edit`, payload: section heading, commit
     SHA, byte delta).
  6. Run Tier-1 validation (below) and enqueue the Tier-2 job.
  7. Respond with the re-rendered section fragment + validation results +
     annotation strip.
- `GET /workspaces/{id}/sections/{n}/cancel` → re-rendered read-only fragment.

### Turn-lock UX

Verified: the existing SSE stream emits rendered chat rows and a terminal
`done` event only — there are **no** turn-start/turn-status events to hook
Edit buttons to. So:

- Client-side (best-effort hint): the composer's submit handler disables Edit
  buttons; the existing `done` SSE event re-enables them. On page load,
  buttons render disabled if a turn is queued/running.
- Server-side (authority): the 409 guards above. A user who races the hint
  gets a clear error, not a corrupt state.

Symmetrically, an open edit form does **not** block agent turns — only saves
matter, and the stale check catches the race.

## 2. Tier 1 — deterministic validation (sync, free)

On every accepted save:

1. Run the existing `validate_citations_deterministic` against the new
   document + ledger: orphan markers (marker without ledger row), orphan rows
   (ledger row whose marker was deleted — catches users deleting citations in
   the textarea), evidence drift.
2. **New, small check:** scan for *bare* citekey markers — `[@key]` without
   the `<!-- ev:... -->` comment. Verified: `find_markers` only matches the
   full canonical form, so existing validation is blind to bare markers a
   user might hand-type, and they would later block composed export. The scan
   is a regex over the saved text; hits render as errors with a hint ("use
   the Insert citation button — hand-typed citations aren't linked to
   evidence").

Results render inline in the save response (same visual language as the
citations panel). No LLM, no job, no cap.

## 3. Tier 2 — semantic claim analysis (async, LLM, diff-scoped)

### Job queue

New table `claim_analysis_jobs`, following the `ranking_jobs` / `turn_jobs`
pattern — **append-only, content-addressed** (no in-place coalescing; a
queued row is never retargeted, which would risk pointing a job at an
unrelated section after `##` boundaries shift):

- Columns: id, workspace_id, section_index (display/ordering hint only),
  section_heading (display only), section_sha, commit_sha, status
  (queued/running/done/failed/superseded), attempts, last_error, queued_at,
  updated_at.
- **Enqueue dedup:** skip insert if a queued job already exists for
  (workspace_id, section_sha) — content-keyed, immune to index shifts and
  duplicate headings.
- **Supersession at drain time:** before running a job, re-split HEAD's
  proposal.md; if `section_sha` no longer appears among HEAD's section SHAs,
  mark the job `superseded` (the content was edited again — a newer job
  covers its successor). Jobs therefore only ever analyze content that is
  still live at HEAD.
- Drained by a scheduler tick. **Gating:** the tick is registered when the
  citation LLM service is configured (same condition as the citation-audit
  service — provider may be `ollama` (default, local) or `anthropic`), *not*
  on `ANTHROPIC_API_KEY`. Stuck-job reaper per house pattern.

### Per-job pipeline

1. Resolve the section's live text at HEAD (guaranteed identical to
   `section_sha` by the supersession check); extract sentences with the
   existing `claims.py` sentence iterator, keeping each sentence's ordinal
   and character offset within the section.
2. **Determine citation state per occurrence first.** Verified: the sentence
   iterator strips canonical markers from `sentence_text`, so a cited and an
   uncited occurrence of the same prose share a claim_sha — branching on the
   cache before checking markers would let a cached "uncited" outcome
   suppress the audit of a cited occurrence. So: markers are detected from
   the sentence's span in the *original* text, and the branch below is
   chosen before any cache lookup.
3. For each sentence occurrence:
   - **Cited (has canonical marker(s)):** always the evidence-ledger audit
     path — the existing semantic audit (claim vs the cited reference's
     chunks → supported/unsupported verdict per evidence id). Caching is the
     existing per-verdict cache; `claim_classifications` is never consulted
     for cited occurrences.
   - **Uncited:** look up the **classification cache** (below) by claim_sha +
     prompt_version — a hit costs zero tokens. On miss, one LLM
     classification — "does this sentence assert a checkable factual claim
     that needs support?" (definitions, signposting, and the proposal's own
     aims/plans are *not* claims). Cache the result either way. If a claim:
     - **Candidate retrieval:** embedding similarity between the claim
       sentence and the workspace's `ReferenceChunk` embeddings (top-k, k=3)
       — this **reuses the existing chunk embeddings and semantic-search
       machinery** (populated at Phase-2 ingest; existing model-drift
       handling applies). No new embedding infrastructure.
     - **Per-candidate verdict:** LLM judges supports / does not support, and
       returns an evidence quote. The quote is verified verbatim
       (whitespace-tolerant) against the chunk text — unverifiable quote ⇒
       candidate discarded (fail closed, same rule as the agent-side audit).
     - Outcome: best supporting candidate (suggestion), or "no support in
       library" (flag).
4. Persist results (content cache + occurrence annotations, below); mark job
   done.

All LLM calls go through the existing cap-checked citation service and token
ledger (same daily caps, same admin visibility in /admin/drafting).

### Storage: content cache vs occurrence annotations

Two concerns, two tables (a single content-keyed table conflates them —
identical sentences in different sections would share one row, one dismissal
would suppress every occurrence, and Insert couldn't target repeated text):

**`claim_classifications` (reusable content-level cache).** Keyed by
(workspace_id, claim_sha, prompt_version). Stores: is_claim (bool), and for
claims the retrieval/verdict outcome (best reference_id, chunk_index,
evidence_quote, verdict_json) or no_match. Re-analysis of an unchanged
sentence is a cache hit — zero LLM calls. Rows are invalidated by
prompt_version bumps.

**Library-change invalidation (both directions):**

- *Library grew* — a reference's text ingest completes: mark `no_match`
  retrieval outcomes for the workspace stale and enqueue analysis jobs for
  the sections at HEAD containing open `uncited_no_match` annotations.
  Re-runs are retrieval + verdict only (classification is still cached), so
  the cost is proportional to previously-unmatched claims — this is what
  makes "upload the missing PDF" actually resolve the flag.
- *Library shrank or changed* — a reference is tombstoned/deleted, its
  chunks are re-ingested, or chunk embeddings are refreshed under a new
  model: mark `supported` retrieval outcomes that point at the affected
  reference stale, mark open `uncited_supported` annotations citing it
  stale, and enqueue jobs for their sections. Otherwise the strip would
  keep offering Insert for evidence that no longer exists. (The `is_claim`
  classification itself is never invalidated by library changes — only the
  retrieval/verdict outcome attached to it.)

**`user_claim_annotations` (occurrence-addressed, user-facing).** One row per
*occurrence* of a finding:

- id, workspace_id, section_sha (snapshot the annotation was computed
  against), **section_occurrence** (0-based ordinal among sections sharing
  this section_sha at analysis time — byte-identical duplicate sections get
  separate rows; the job is content-keyed, so its processor fans out one
  annotation set per duplicate at HEAD), section_heading (display),
  sentence_ordinal, char_offset, claim_sha, claim_text, kind
  (`uncited_supported` / `uncited_no_match` / `cited_unsupported`),
  **evidence_id** (nullable; set — and required — for `cited_unsupported`,
  so a sentence carrying several bad citations gets one row per bad
  evidence marker), reference_id (nullable), evidence_quote (nullable),
  chunk_index (nullable), status (`open` / `inserted` / `dismissed` /
  `stale`), dismissed_reason, resolved_by_user_id, created_at, resolved_at.
- Unique on (workspace_id, section_sha, section_occurrence,
  sentence_ordinal, kind, evidence_id) — with evidence_id taking a sentinel
  empty value for uncited kinds so the composite stays enforceable. A new
  analysis of a *changed* section produces fresh rows against the new
  section_sha; rows for section SHAs no longer present at HEAD are marked
  `stale` (kept for audit, hidden from the strip). Dismissals are
  occurrence-scoped by construction.

**Reconcile tick (self-healing).** A periodic job (mirroring the ranking
reconciliation pattern): for each drafting-enabled workspace, split HEAD and
enqueue a job for any section whose section_sha has no job row in
queued/running/done. This heals enqueues lost to the DB-failure window in §6
and any supersession edge cases — analysis coverage converges to HEAD without
manual intervention.

## 4. Suggestions UI

An **annotation strip** renders under each section in the artifact pane
(collapsed to a count badge when empty/clean), refreshed by HTMX polling on
the strip while a job for that section is pending (the existing SSE stream
carries chat turns, not analysis events — polling is the no-new-plumbing
fit for the HTMX stack):

- **Uncited claim, candidate found:** claim sentence, formatted reference
  (existing CSL formatter), evidence quote, **Insert citation** button.
- **Uncited claim, no match:** "Nothing in your library supports this." +
  **Dismiss** with required short reason ("common knowledge", "will add ref
  later", …) — waiver-style, audit-logged, mirroring the metadata-waiver
  pattern.
- **Cited but unsupported:** warning in the same visual language as the
  citations evidence ledger, linking to `/workspaces/{id}/citations`.
- Failed job: the strip shows the failure reason (cap exceeded vs transport
  error) and a **Retry** button (re-enqueues).

### Insert citation — canonical path only

Insert does **not** splice a bare `[@citekey]` into the text. Verified: the
canonical citation is `[@citekey]<!-- ev:id -->` bound to an evidence row in
citations.json; bare markers are invisible to `find_markers` (and thus to
deterministic validation) and block composed export. Insert therefore goes
through the same code path as the agent's `insert_citation` tool:

1. Acquire the commit mutex; apply the turn-lock and stale checks (the
   annotation's `section_sha` must still be present at HEAD, else 409
   "the text changed — re-analysis is pending").
2. Create the evidence ledger row (claim text, reference, chunk, verified
   quote — all already in the annotation) and build the full marker.
3. Anchor placement uses the existing `insert_marker` semantics. The anchor
   is the claim sentence; if the sentence text occurs more than once in the
   document, the anchor is extended with preceding context until unique
   (bounded attempts). If no unique anchor can be constructed, Insert fails
   with a clear message ("repeated text — cite it via chat or edit the
   sentence") rather than guessing. `AnchorError` is surfaced, never
   swallowed.
4. Commit proposal.md + citations.json atomically (existing
   `write_and_commit_files` multi-file commit), `ArtifactVersion`
   `created_by="user"`, audit event, annotation → `inserted`.
5. **Occurrence-aware idempotency (explicit refactor).** Verified: the
   existing insertion path's `_find_idempotent_row` treats *any* live
   citation with the same (reference, chunk, quote) as already-inserted,
   anywhere in the document. That is wrong for occurrences: citing the same
   evidence for the same sentence appearing twice must produce two markers.
   The shared insertion core is refactored to accept an occurrence binding:
   the user path is idempotent only if *this annotation* already has its own
   ev_id live at HEAD (annotation row already `inserted` → no-op). The agent
   tool keeps its current document-level semantics — no behavior change on
   that surface.

Insert/dismiss require owner (same as section saves).

## 5. Agent interplay

- The drafting agent system prompt gains one rule: do not rewrite or delete
  user-authored passages unless the user's chat message asks for it. User
  edits are identifiable from `user edit:` commits and
  `ArtifactVersion.created_by='user'` rows.
- **This protection is best-effort, not enforced.** The agent's
  `replace_proposal` tool can overwrite the whole document; nothing
  mechanical stops it from clobbering user prose. Accepted for this slice
  because (a) every user save is a git commit, so nothing is ever
  unrecoverable (history + diff view already exist), and (b) an enforcement
  layer (e.g. a tool-side diff guard over user-authored spans) is real
  complexity with unclear precision. Revisit if clobbering is observed in
  practice.
- Agent turns read HEAD per tool call (existing behavior) — they naturally
  see user edits.
- Suggestions are advisory; the agent does not act on open annotations
  automatically.

## 6. Error handling

- **Git commit failure on save/insert:** existing `write_and_commit_files`
  rollback; user sees an error banner; nothing half-written.
- **DB failure *after* a successful git commit:** the save's DB writes
  (ArtifactVersion, WorkspaceEvent, job enqueue) happen in one transaction
  after the commit; if it fails, the user gets a 500 banner ("saved, but
  bookkeeping failed — reload"), and the text is safe at HEAD. Recovery is
  defined, not hoped for: the existing `reconcile_workspace_git_drift`
  backfills the missing ArtifactVersion (as `created_by='reconciled'` —
  provenance degraded, logged loudly), and the claim-analysis **reconcile
  tick** (§3) re-enqueues the missed analysis job. The WorkspaceEvent is the
  one accepted loss (audit gap, logged).
- **LLM failure / cap exceeded:** job → failed with last_error; strip shows
  reason + Retry. Cap-exceeded messaging matches the chat-turn cap UX.
- **Ledger write failure after a successful LLM call:** logged loudly, not
  fatal (existing citation-service behavior).
- **Sectionizer edge cases:** documents with no `##` headings are a single
  section 0 — everything still works, just coarser.

## 7. Testing

House style: pytest + testcontainers Postgres, fake LLM adapter / MockTransport,
no browser tests.

- Sectionizer: round-trip byte-identity (property-style over fixture docs,
  including fenced code blocks containing `##`, no-heading docs, trailing
  newline cases).
- Save guards: turn-lock 409; stale-form 409 (section changed) vs allowed save
  (HEAD moved, section unchanged); owner-only enforcement (viewer → 403,
  admin non-owner → 403); CSRF.
- **Write serialization:** concurrent user save + agent tool write under the
  commit mutex → two sequential commits, both contents present in history,
  no git corruption; mutex covers all agent mutation tools. User save vs
  turn enqueue → exactly one serialized order (409 or turn-after-commit).
  `replace_proposal` against a stale read → tool error, user commit still at
  HEAD; after re-read it succeeds.
- Queue: content-keyed enqueue dedup; supersession (job whose section_sha is
  gone at HEAD → superseded, never analyzed); index shift from a user adding
  a `##` mid-edit does not retarget any job; reconcile tick enqueues for a
  HEAD section with no job row.
- Diff-scoping: unchanged sentences produce zero LLM calls (assert on fake
  client call count); "not a claim" results are cached and not reclassified
  on the next save; prompt_version bump invalidates.
- **Cited vs cache:** the same prose cited in one place and uncited in
  another → the cited occurrence gets a semantic-audit verdict even when the
  uncited occurrence has a cached classification (cache never short-circuits
  the audit path).
- Library-change reanalysis: open `uncited_no_match` + new reference text
  ingested → job enqueued, retrieval re-runs, annotation resolves or stays
  flagged. Tombstoning a suggested reference → its `uncited_supported`
  annotations and cached outcomes go stale; re-analysis no longer offers it.
- Occurrence addressing: identical sentence in two sections → two
  annotations; dismissing one leaves the other open. **Byte-identical
  duplicate sections** → one content-keyed job, annotation sets fanned out
  per section_occurrence; dismiss in one duplicate leaves the other open.
  A sentence with two failing citations → two `cited_unsupported` rows
  (per evidence_id).
- Insert: canonical marker + evidence row committed atomically; bare-marker
  scan in Tier-1 catches hand-typed `[@key]`; ambiguous anchor → clean
  failure, no write; occurrence-aware idempotency (same evidence live
  elsewhere does NOT no-op this occurrence; this annotation already
  inserted DOES); agent-tool idempotency semantics unchanged; Tier-1
  revalidation runs post-insert.
- DB-failure-after-commit recovery: simulated DB error post-commit →
  reconciliation backfills ArtifactVersion, reconcile tick re-enqueues
  analysis.
- Caps: analysis blocked at cap with correct failure surface.
- Migrations: new tables round-trip per existing alembic test pattern.

## Out of scope (this slice)

- PubMed / external reference suggestions (future: "search PubMed" link
  pre-filled with the claim text — navigation only, no auto-insert).
- Shared editing for viewers; concurrent-edit merge UX.
- Whole-document or paragraph-level editing.
- Mechanical enforcement of "agent must not clobber user prose" (best-effort
  prompt rule + git recoverability in this slice).
- Agent auto-acting on open annotations.
- UI for tuning k / thresholds in candidate retrieval.
- New SSE event types for analysis progress (HTMX polling on the strip).
