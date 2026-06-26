# Cockpit "Claims" Tab — Claim-Analysis Migration (Design Spec)

**Date:** 2026-06-26
**Status:** Design approved; pending spec-review gate → writing-plans.
**Project:** Full-React migration → retire the legacy Jinja `/workspaces/{id}` workspace UI.
**Builds on:** Cockpit gap-closers merged at master `8a9c465`.
**Blocks / unblocks:** the 3rd and last Jinja-only feature gap. Once this lands, the G2
delete (spec `d9cca04`) can remove the Jinja annotation workflow too — completing the
"pure orphan-removal" premise for the whole `workspace_main` teardown.

---

## 1. Why this slice exists

G2 plan-prep found the cockpit was not actually feature-complete: three owner-only Jinja
routes had no React/`/api` equivalent. Two (soft-delete, turn-cancel) shipped in the gap-closers
slice. This migrates the **third and largest**: the **claim-analysis annotation workflow**.

**What claim-analysis is.** A background "citation-hygiene" analyzer. Per draft section, the
pipeline (scheduler ticks `drain_analysis_queue` / `reconcile_analysis_coverage`, gated on
`citation_service` + `analysis_embedder`) classifies sentences and writes `UserClaimAnnotation`
findings of **three kinds** (`ck_user_claim_annotations_kind`):

- **`uncited_supported`** — claim has no citation but a library reference supports it →
  **actionable: one-click Insert** (commits a citation marker via `insert_from_annotation`, the
  canonical select-to-cite write path) or Dismiss.
- **`uncited_no_match`** — uncited claim, no matching reference → informational; Dismiss only.
- **`cited_unsupported`** — claim is cited but the cited evidence doesn't support it → warning;
  Dismiss only.

Findings are occurrence-addressed (`section_sha` + `section_occurrence` + `sentence_ordinal` +
`char_offset`) and go **stale** when the section is edited (its sha leaves HEAD) or the library
changes (invalidation hooks). The only UI today is the Jinja per-section "annotation strip"
(`web/annotations.py` → `annotation_strip.html`, lazy-loaded + polled inside the doomed
`workspace_main` editor). There is **no React equivalent**, and **no `web/api/` module imports the
subsystem yet** — so this is a clean new `/api` surface.

This slice is **additive**: no DB schema change (tables, pipeline, scheduler ticks all exist), and
the Jinja annotation routes/template stay wired to the live Jinja editor until **G2** deletes them.

---

## 2. Goal & invariant

**Goal:** an owner can review and act on claim-analysis findings — Insert a suggested citation,
Dismiss with a reason, Reanalyze a section — entirely from the React cockpit, at parity with the
Jinja strip; a viewer sees the findings read-only.

**Invariant after this slice:**
1. The cockpit has a "Claims" bottom-panel tab driven by a typed `/api/workspaces/{id}/annotations`
   surface; the four Jinja behaviors (read strip, insert, dismiss, reanalyze) are reachable in React.
2. Insert/dismiss/reanalyze go through the **exact existing** write paths (`insert_from_annotation`,
   the guarded dismiss UPDATE + `user_claim_dismissed` audit, `enqueue_section_job`) — byte-for-byte
   the same behavior, including the post-insert re-enqueue and the audit event.
3. The Jinja annotation surface is unchanged and still works in the live Jinja editor (deleted in G2).
4. No DB schema change. No new dependency.

---

## 3. Architecture

### 3.1 Backend — new `src/call_manager/web/api/annotations.py` (router mounted under `/api`)

Self-contained on **kept** primitives only (the same the kept `web/api/workspaces.py` already
uses: `workspace_repo_dir`, `PROPOSAL_FILENAME`, `current_head`, `split_sections`); it does **not**
import the doomed Jinja `web/sections.py` / `web/workspaces.py` / `web/annotations.py` helpers.
Reuses the existing analysis services verbatim: `insert_from_annotation` (`analysis/insert.py`),
`enqueue_section_job` (`analysis/queue.py`), `_citation_snapshot` (`tools/citations.py`), and the
models.

**Shared-helper extraction (prerequisite — Task 0 of the plan).** The post-insert re-enqueue helper
`_enqueue_containing_section` currently lives **inside the doomed Jinja `web/annotations.py`**
([`web/annotations.py:72`](../../../src/call_manager/proposal_assist/web/annotations.py)). The
new `/api` needs it for insert parity, and importing it from the doomed module would re-create
exactly the coupling the teardown is removing. **Extract it to `analysis/queue.py`** (next to
`enqueue_section_job`, all kept) as `enqueue_containing_section(session, *, workspace_id,
workspaces_dir, commit_sha, ev_id) -> None` — behavior byte-identical. The new `/api` insert calls
it; **repoint the Jinja route at it too** (drop its inline copy now, so the route's existing tests
become a free regression net — the same extract-and-repoint pattern the gap-closers soft-delete
used). G2 then deletes only the Jinja route, not the helper.

**`GET /api/workspaces/{workspace_id}/annotations`** (member) — the read-model.
- Read the current `proposal.md` at HEAD (`repo = workspace_repo_dir(...)`,
  `raw = (repo/PROPOSAL_FILENAME).read_text()`, `head = current_head(repo)`), `split_sections(raw)`.
- **Always degrade to `{is_owner, sections: []}` — never 404/500 — on ANY pre-draft state:** no git
  repo / uninitialized repo (`current_head` raises) / no `proposal.md` / empty file. A setup-phase or
  brand-new workspace legitimately has no draft yet, and the Claims tab is always mounted. (This is
  the one deliberate behavior change from the Jinja `get_section_annotations`, which 404s — a strip
  fragment can 404, an always-present tab cannot.)
- For each live section compute its `occurrence` (count of earlier sections with the same sha),
  gather `status="open"` annotations matching `(workspace_id, section_sha, section_occurrence)`
  ordered by `sentence_ordinal, id`, resolve each `reference_id` → "Family Year" label
  (`_citation_snapshot`, workspace-scoped — a cross-workspace ref renders as the fallback), and
  attach the latest `ClaimAnalysisJob` state for that `section_sha` (`pending` if status in
  `(queued,running)`, `failed` + `last_error` if failed).
- **Returns ALL live sections** (so every section is reanalyzable, mirroring the per-section Jinja
  strip), each: `{index, heading, section_sha, occurrence, job:{status,error}, findings:[…]}`.
  Each finding: `{id, kind, claim_text, evidence_quote, ref_label, can_insert, sentence_ordinal}`
  where `can_insert = (kind == "uncited_supported")`. Top-level `is_owner`. Annotations whose
  `section_sha` is not at HEAD are omitted (stale — owned by the reconcile tick / invalidation).

**`POST .../annotations/{annotation_id}/insert`** (owner) — verbatim reuse of the Jinja body:
`_annotation_or_404` (IDOR: `annotation.workspace_id != ws.id` → 404) → capture `(section_sha,
section_occurrence)` before the service → `insert_from_annotation(...)` → `commit` → on a genuine
(non-idempotent) insert, the post-insert `enqueue_containing_section` re-enqueue (the extracted
shared helper). Error map:
`InsertConflict`→409 (detail = the message, e.g. turn-in-flight), `CommitMutexTimeout`→**429**
(`Retry-After`; `/api` sanitizes ≥500 so 503→429, per the Slice-B convention),
`GitCommitError`→500 ("insert failed — nothing was written"). Returns
`{ev_id, citekey, commit_sha}` or `{idempotent: true}`.

**`POST .../annotations/{annotation_id}/dismiss`** (owner, `{reason: str}` JSON body) —
`_annotation_or_404` → trimmed reason required (empty → 422) → guarded
`UPDATE … WHERE id=… AND status='open'` setting `status='dismissed'`, `dismissed_reason`,
`resolved_by_user_id`, `resolved_at` (race → 409 "no longer actionable") → `user_claim_dismissed`
`WorkspaceEvent` (payload `{annotation_id, reason}`) → commit. Returns `{dismissed: true}`.

**`POST .../sections/{n}/reanalyze`** (owner) — read the live sections (as above), 404 if `n` out of
range, `enqueue_section_job(section_index=n, section_heading, section_sha, commit_sha=head)` →
commit. Returns `{enqueued: bool}`.

All endpoints: session-factory context-manager pattern (mirror `web/api/turns.py`); `require_owner`
for writes, `require_member` for the read; binary/HTML responses become JSON.

### 3.2 Frontend — `ClaimsPanel.tsx` + the 6th bottom-panel tab

- `api/annotations.ts` hooks: `useAnnotations(id)` (read-model; `refetchInterval` ~3s **only while**
  some section's `job.status === "pending"`, else off), `useInsertAnnotation(id)`,
  `useDismissAnnotation(id)`, `useReanalyzeSection(id)`. Insert/dismiss/reanalyze invalidate
  `["annotations", id]`; insert additionally invalidates `["draft", id]` and the
  proposed-changes/citation queries (the doc changed).
- `ClaimsPanel`: renders each section (heading + job indicator: "analyzing…" / "analysis failed —
  Reanalyze") with its findings. Each finding: a **kind badge** (uncited-supported / uncited-no-match
  / cited-unsupported), the claim text, evidence quote (where present), and the ref label. Owner
  actions: **Insert** (only when `can_insert`), **Dismiss**, per-section **Reanalyze**. Viewer: the
  same list, read-only (no action buttons). Sections with no open findings render compactly (heading
  + "No open findings" + Reanalyze).
- **Dismiss reason:** clicking Dismiss expands a small inline required text field + a confirm button
  (cleaner and more controllable than `window.prompt`); empty stays disabled. On confirm → POST.
- Insert/dismiss error → a brief inline note on the finding (e.g. "Couldn't insert — a turn may be in
  progress; try again"); the read-model refetch reconciles state.
- Wire-up: extend the `BottomTab` union + `nav.ts`, add a `Claims {openCount}` tab button + body in
  `BottomPanel.tsx` (count = total open findings across sections), following the existing 5-tab
  pattern exactly.

---

## 4. Scope & boundaries

- **One slice** (1 read-model + 3 actions + 1 panel; cohesive — comparable to Slice F). **No DB
  schema change.**
- **Additive** (one extraction aside). The Jinja `web/annotations.py`, `partials/annotation_strip.html`,
  and the `section_view.html` strip wiring are **kept** (still rendered in the live Jinja editor).
  **G2 deletes them** once this lands — same narrow-teardown discipline as Slice F (don't delete Jinja
  that's still wired to live rendering). The one touch to the doomed module is the §3.1 extract-and-repoint
  of `enqueue_containing_section` (so the `/api` doesn't import doomed code); the Jinja insert route's
  behavior is unchanged.
- **Deferred (follow-up, not v1):** clicking a finding to jump the editor to the claim's
  `char_offset` (a Citations-`openPeek`-style deep-link). Adds editor-coordination complexity for
  marginal v1 value; the finding already shows the claim text.

---

## 5. Data flow / error handling

- **Staleness:** the read-model only surfaces open annotations that map to a live section at HEAD.
  Editing a section (sha leaves HEAD) drops its findings from the tab; the reconcile tick + the
  library-change invalidation hooks own the DB-level `stale`/`superseded` transitions. The tab is a
  pure view of current open findings — it never writes staleness itself.
- **Insert:** flips the annotation to `inserted`, commits a marker, and re-enqueues analysis for the
  now-changed section; the client refetches annotations + draft so the new marker and any new
  findings appear.
- **Concurrency:** insert is guarded by the commit mutex inside `insert_from_annotation` (busy→429);
  dismiss is a guarded `WHERE status='open'` UPDATE (race→409); both match the Jinja semantics.
- **Error envelope:** handlers raise `HTTPException`; the global handler produces the `/api` error
  envelope (same as the rest of `web/api`). 409/422/429 carry actionable detail; 500 is generic.

---

## 6. Verification gates

- `uv run python -c "import call_manager.main"` boots with the new router mounted.
- Backend suite green: read-model (section mapping + occurrence, job state, owner flag, stale
  omission, no-proposal empty), insert (success / idempotent / `InsertConflict`-409 / busy-429 /
  non-owner-403 / cross-workspace-404), dismiss (success + audit event / empty-422 / race-409 /
  non-owner-403), reanalyze (enqueue + idempotent dedup / out-of-range-404 / non-owner-403), and the
  read-model's **empty-on-pre-draft** path (no repo / no proposal → `sections: []`, 200) plus the
  duplicate-section occurrence math. `ruff` clean. The Jinja annotation tests stay green — the insert
  route is repointed to the extracted `enqueue_containing_section` (its tests are the extraction's
  regression net); the other Jinja annotation routes are untouched.
- Frontend `tsc` clean, `vitest` green (ClaimsPanel renders all 3 kinds, insert/dismiss/reanalyze
  paths, owner-vs-viewer gating, pending/failed indicator, the new tab + count), `build` clean.
- OpenAPI regen (`EMBEDDING_MODEL=bge-m3 bash scripts/gen_openapi_types.sh`): the new annotation
  paths added (additive); `routes.gen.ts` unchanged (no new React route — the panel is a bottom tab).

---

## 7. Risks / notes

- **Read-model section-mapping is the one piece of real logic.** Mitigation: it mirrors the proven
  Jinja `get_section_annotations` + `_strip_response` mapping (occurrence = count of earlier
  same-sha sections) and reuses the same `split_sections`; tests pin the occurrence math for
  duplicate sections and the stale-omission case.
- **Reusing `insert_from_annotation` verbatim** keeps the slice low-risk — the canonical write path,
  its conflict semantics, and the post-insert re-enqueue are unchanged; only the transport (JSON vs
  HTMX) and the auth-dep style (session-factory vs `_session_dep`) change.
- **`tools/citations._citation_snapshot`** is a private helper; importing it from `web/api` is the
  same coupling the Jinja `annotations.py` already has. Acceptable (kept module); if it later moves,
  one import updates.
- **Two coexisting UIs until G2:** the Jinja strip and the React Claims tab both read the same
  tables. They never conflict (the tab is additive; both use the same guarded write paths).
- **Perpetual "analyzing…" when the local analysis stack is unconfigured.** The scheduler ticks
  (`drain_analysis_queue`/`reconcile_analysis_coverage`) are gated on `citation_service` +
  `analysis_embedder`; if those are unset (local LLM/embedder down), `reanalyze` enqueues jobs that
  never drain and the tab's per-section indicator stays "analyzing…". This is **parity with the
  Jinja strip** (which polls forever in the same case) — accepted for v1; a future enhancement could
  surface analysis-availability and disable Reanalyze when the stack is off.

---

## 8. Out of scope

- Deleting the Jinja `annotations.py` / `annotation_strip.html` / the `section_view.html` strip
  wiring → **G2**.
- The editor `char_offset` jump-to-claim deep-link → deferred follow-up.
- Any change to the analysis pipeline, the classifier, the scheduler ticks, the invalidation hooks,
  or the annotation/job schema — this slice only re-exposes existing behavior through `/api` + React.
- The other migration sub-projects (G2 delete, calls-detail, profile/admin/auth).
