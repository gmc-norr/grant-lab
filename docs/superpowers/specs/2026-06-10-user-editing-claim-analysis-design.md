# User editing + claim analysis of user-added text — design

**Date:** 2026-06-10
**Status:** Approved (brainstorm with owner)
**Builds on:** `2026-04-18-call-manager-proposal-assist-design.md` (workspaces, turn queue, git-backed artifact), the citations system (slices 1–4: markers, deterministic validation, semantic audit, bibliography).

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
| 1 | Division of labor | **User edits, agent reviews.** User saves land immediately as commits; the system responds with annotations (analysis, suggestions), never silent rewrites. The agent does not overwrite user prose unprompted. |
| 2 | Editing granularity | **Section-level.** Rendered artifact pane stays; each `##` section gets an Edit button that swaps to a textarea (HTMX). No whole-document editor, no block-level inline editing. |
| 3 | Analysis trigger | **Two-tier.** Tier 1: deterministic validation, synchronous on every save, free. Tier 2: semantic LLM analysis, auto-enqueued on save, debounced (one pending job per workspace+section) and diff-scoped (only new/changed sentences are analyzed). |
| 4 | Suggestion source | **Workspace library only.** Candidates come from references uploaded to the workspace (extracted, chunked text → verifiable evidence quotes). Claims with no match are flagged honestly. No PubMed lookup in this slice (cheap escape hatch later: link to a pre-filled PubMed search). |
| 5 | Concurrency | **Lock during agent turns.** Section editing is disabled while a turn is queued/running. A `base_commit_sha` precondition on save additionally rejects stale forms — but no merge UX; a stale save just asks the user to reopen the section. |
| 6 | Who can edit | **Owner only** (admins per existing owner-or-admin policy). Viewers remain read-only. Shared editing is out of scope. |

## 1. Section editing

### Sectionizer

A deterministic parser splits proposal.md on `##` headings (ATX level-2 only;
fenced code blocks are respected — a `##` inside a fence is not a boundary):

- Content before the first `##` (title, abstract) is section 0 ("preamble").
- Each section = heading line + body until the next `##` or EOF.
- **Round-trip guarantee:** `join(split(text)) == text` byte-identical. This is
  the core invariant; saving section *n* replaces exactly that byte range.

Section identity for the UI is `(index, sha256(section_text))`. The SHA, not
the index alone, backs the stale check — heading renames or reordering by the
agent invalidate open edit forms.

### Routes

All under the existing workspaces router, owner-or-admin, CSRF-protected,
404 on soft-deleted workspaces.

- `GET /workspaces/{id}/sections/{n}/edit` → HTMX fragment: textarea with the
  raw markdown of section *n*, hidden fields `base_commit_sha` and
  `base_section_sha`, Save/Cancel buttons. Returns 409 (with a rendered notice
  fragment) if a turn is queued or running.
- `POST /workspaces/{id}/sections/{n}` → save. Guards, in order:
  1. **Turn lock:** any TurnJob for this workspace in status queued/running →
     409 "assistant is working — try again when the turn finishes".
  2. **Stale form:** if HEAD ≠ `base_commit_sha`, re-split HEAD's proposal.md;
     if the section at *n* no longer matches `base_section_sha` → 409 "this
     section changed underneath you — reopen it". (If HEAD moved but the
     section is byte-identical, the save proceeds.)
  3. Reassemble the document with the replaced section; commit via the
     existing `write_and_commit_files` (atomic, rollback on failure). Commit
     message: `user edit: <heading> (user:<id>)`.
  4. Write a `WorkspaceEvent` (`user_edit`, payload: section heading, commit
     SHA, byte delta).
  5. Run Tier-1 validation (below) and upsert the Tier-2 job.
  6. Respond with the re-rendered section fragment + validation results +
     annotation strip.
- `GET /workspaces/{id}/sections/{n}/cancel` → re-rendered read-only fragment.

### Turn-lock UX

The workspace page already streams turn status via SSE. Edit buttons disable
(with an "assistant is working…" hint) when a turn starts and re-enable when it
completes, mirroring the server-side guard. The server-side guard is the
authority; the UI state is convenience.

Symmetrically, an open edit form does **not** block agent turns — only saves
matter, and the stale check catches the race.

## 2. Tier 1 — deterministic validation (sync, free)

On every accepted save, run the existing `validate_citations_deterministic`
against the new document: unknown citekeys, malformed/residual markers,
evidence-quote drift. Results render inline in the save response (same visual
language as the citations panel). No LLM, no job, no cap.

## 3. Tier 2 — semantic claim analysis (async, LLM, diff-scoped)

### Job queue

New table `claim_analysis_jobs`, following the `ranking_jobs` / `turn_jobs`
pattern:

- Columns: id, workspace_id, section_index, section_heading (display only;
  empty string for the preamble), section_sha, commit_sha, status
  (queued/running/done/failed), attempts, last_error, queued_at, updated_at.
- **Debounce:** partial unique index on (workspace_id, section_index) WHERE
  status='queued' — index, not heading, so duplicate headings can't coalesce
  two different sections. A save while a job is queued updates that row's
  section_sha/commit_sha in place (coalescing); the job always analyzes the
  exact content addressed by its (commit_sha, section_sha), so index drift
  from intervening agent restructures cannot point it at the wrong text. A save while a job is
  *running* enqueues a fresh queued row (the running job's results may be
  partially stale; the new job supersedes them on completion).
- Drained by a scheduler tick (interval, same conditional as other
  proposal-assist jobs: DRAFTING_ENABLED + ANTHROPIC_API_KEY). Stuck-job
  reaper per house pattern.

### Per-job pipeline

1. Load the section text at `commit_sha`; extract sentences with the existing
   `claims.py` sentence iterator.
2. SHA each sentence (existing claim-SHA scheme). Skip sentences whose SHA has
   a cached verdict/suggestion for this workspace — **only new or changed
   sentences cost tokens.**
3. For each remaining sentence:
   - **Has `[@citekey]` marker(s):** run the existing semantic audit
     (claim vs reference chunks → supported/unsupported verdict). Reuses the
     verdict cache.
   - **No marker:** one LLM classification — "does this sentence assert a
     checkable factual claim that needs support?" (definitions, signposting,
     and the proposal's own aims/plans are *not* claims). If yes:
     - **Candidate retrieval:** embedding similarity between the claim
       sentence and reference chunks (top-k, k=3), workspace library only.
     - **Per-candidate verdict:** LLM judges supports / does not support, and
       returns an evidence quote. The quote is verified verbatim
       (whitespace-tolerant) against the chunk text — unverifiable quote ⇒
       candidate discarded (fail closed, same rule as the agent-side audit).
     - Outcome: best supporting candidate (suggestion), or "no support in
       library" (flag).
4. Persist results to `user_claim_suggestions`; mark job done.

All LLM calls go through the existing cap-checked citation service and token
ledger (same daily caps, same admin visibility in /admin/drafting).

### Reference chunk embeddings (new infra)

Reference chunks are not embedded today. Add embeddings at text-extraction
time (Phase 2): after chunking, embed each chunk with the local Ollama embed
client (same model/dim discipline as calls/profiles — store embedding_model,
treat mismatches as stale). Backfill job for existing references. Local-only,
no API cost. If an embedding is missing at analysis time, fall back to
analyzing against all chunks of the *cited* reference (audit path) and skip
candidate retrieval for uncited claims with a logged warning — never block the
job on missing embeddings.

### Suggestions storage

New table `user_claim_suggestions`:

- id, workspace_id, section_heading, claim_sha, claim_text, commit_sha,
  kind (`uncited_supported` / `uncited_no_match` / `cited_unsupported`),
  reference_id (nullable), evidence_quote (nullable), chunk_index (nullable),
  verdict_json, status (`open` / `inserted` / `dismissed`), dismissed_reason,
  resolved_by_user_id, created_at, resolved_at.
- Unique on (workspace_id, claim_sha, kind): re-analysis refreshes rather than
  duplicates; resolved rows are not reopened by re-analysis of an unchanged
  sentence.

## 4. Suggestions UI

An **annotation strip** renders under each section in the artifact pane
(collapsed to a count badge when empty/clean), refreshed when a job completes
(SSE if a turn stream is already open; otherwise HTMX poll on the strip while
a job is pending):

- **Uncited claim, candidate found:** claim sentence, formatted reference
  (existing CSL formatter), evidence quote, **Insert citation** button.
  Insert = server-side: append ` [@citekey]` before the sentence's terminal
  punctuation, commit atomically (same path as a section save, including
  Tier-1 revalidation), mark suggestion `inserted`, audit event. Idempotent:
  if the marker is already present at HEAD, just resolve the row.
- **Uncited claim, no match:** "Nothing in your library supports this." +
  **Dismiss** with required short reason ("common knowledge", "will add ref
  later", …) — waiver-style, audit-logged, mirroring the metadata-waiver
  pattern.
- **Cited but unsupported:** warning in the same visual language as the
  citations evidence ledger, linking to `/workspaces/{id}/citations`.
- Failed job: the strip shows the failure reason (cap exceeded vs transport
  error) and a **Retry** button (re-enqueues).

Insert/dismiss require owner-or-admin and the turn lock (same guards as a
section save).

## 5. Agent interplay

- The drafting agent system prompt gains one rule: do not rewrite or delete
  user-authored passages unless the user's chat message asks for it. (User
  edits are identifiable from `user edit:` commits in artifact history, which
  the agent can already read.)
- Agent turns read HEAD at turn start (existing behavior) — they naturally see
  user edits.
- Suggestions are advisory; the agent does not act on open suggestions
  automatically.

## 6. Error handling

- **Git commit failure on save/insert:** existing `write_and_commit_files`
  rollback; user sees an error banner; nothing half-written.
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
  (HEAD moved, section unchanged); owner-only enforcement; CSRF.
- Debounce: save-while-queued coalesces (row count stays 1, SHAs updated);
  save-while-running enqueues a second job.
- Diff-scoping: unchanged sentences produce zero LLM calls (assert on fake
  client call count).
- Pipeline: marker→audit path, claim→candidate→verdict path, unverifiable
  quote discarded, no-match flag, missing-embedding fallback.
- Insert: marker placement before terminal punctuation; idempotency; Tier-1
  revalidation runs.
- Caps: analysis blocked at cap with correct failure surface.
- Migrations: new tables round-trip per existing alembic test pattern.

## Out of scope (this slice)

- PubMed / external reference suggestions (future: "search PubMed" link
  pre-filled with the claim text — navigation only, no auto-insert).
- Shared editing for viewers; concurrent-edit merge UX.
- Whole-document or paragraph-level editing.
- Agent auto-acting on open suggestions.
- UI for tuning k / thresholds in candidate retrieval.
