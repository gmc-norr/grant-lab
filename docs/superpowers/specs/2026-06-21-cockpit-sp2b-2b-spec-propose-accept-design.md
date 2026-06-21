# Cockpit SP2b-2b — Agent↔Spec propose/accept + inline review UI — Design

> Sub-project 2 (cockpit), slice SP2b-2b. The second of the three SP2b-2 slices:
> **2a** (two-mode lifecycle + spec lock, merged `d24ff87`) → **2b** (this doc) →
> **2c** (coherence-keeper background session). Built on the SP2a propose/accept
> substrate and the SP2b-1 artifact-aware `[Draft|Spec]` cockpit.

## Goal

Let the drafting agent **propose edits to the Spec artifact** (`proposal-spec.md`),
and let workspace members **review, accept, or reject** those proposals from
inside the cockpit — mirroring the draft propose/accept machinery SP2a already
built. Acceptance replays the proposal through the existing `save_section`
write path (so the SP2b-2a spec lock gates it automatically). Because SP2a
shipped the propose/accept *backend* but no review *UI* for either artifact,
this slice also builds the **first proposal-review surface**, artifact-aware, so
the stranded **draft** proposals light up at the same time.

## Why now

SP2b-1 made the Spec a real editable artifact; SP2b-2a made it lockable. The
remaining half of the cockpit's value proposition — the agent *proposing* and a
human *accepting* — exists in the backend (`proposals/{proposing,accepting,
staleness}.py`, `tools/proposal.py`, `proposal_assist/web/proposals.py`) but has
never been usable end-to-end because there is no review screen. This slice makes
the loop real for the spec (and, for free, the draft).

## Non-goals (scope fence)

- **No coherence-keeper / evaluator background sessions** — SP2b-2c / SP2b-3.
- **No spec citations** — the spec is citation-free; only `kind="prose_edit"`
  proposals target the spec. The `citation_insert` accept path stays draft-only.
- **No spec git-history agent tools** — `read_spec` + `propose_spec_edit`
  suffice; `get_artifact_history`/`read_artifact_at`/`diff_artifact` stay
  draft-only (YAGNI).
- **No cross-artifact inbox, bulk actions, or filters** in the UI — just the
  per-tab pending strip + per-proposal diff + accept/reject.
- **No change to direct spec *editing* gating** — typing into the spec stays
  owner-only (SP2b-2a). See the asymmetry note in §2.

---

## Section 1 — Backend: agent tools + propose/accept services

### Agent tools (dedicated, shared core)

Add two tools to the propose-only allowlist (19 → 21):

- **`read_spec`** — reads `proposal-spec.md`, snapshots the agent's view into
  **new parallel `ToolContext` fields** `agent_view_spec_head`,
  `agent_view_spec_sections` (`{section_index: sha}`), `agent_view_spec_proposal`
  (full text) — mirroring the existing draft snapshot fields. Same ordering
  invariant as `read_proposal` (resolve HEAD *before* reading content, so a
  concurrent save fails safe).
- **`propose_spec_edit(section_index, new_text)`** — records a pending
  `ProposedChange` with `target_artifact="spec"`, `kind="prose_edit"`, using the
  **read-time** spec snapshot (`agent_view_spec_head` +
  `agent_view_spec_sections[section_index]`) as the base. Never a live recompute
  — same anti-clobber rule as the draft (`proposing.py` module docstring): a
  human edit landed since the read makes accept go `stale`, not clobber.

Both are backed by an artifact-parameterized core
(`read_artifact_impl(ctx, artifact)` / `propose_artifact_edit_impl(ctx,
artifact, …)`). The existing `read_proposal` / `propose_edit` keep their exact
current behavior — a regression test guards the draft path.

**Two registration sites, both required (Codex High #1).** Registering the
tools in `tools/registry.py` makes them *exist*; it does NOT make them
*callable*. The runtime gate is the explicit `allowed_tools` list in
`drafting/claude_adapter.py` (~line 237) — `read_spec` and `propose_spec_edit`
**must be added there** or the agent can't invoke them. The agent's guidance to
*use* them lives in the same file's `system_prompt` preset `append`
(~line 225-229): extend that appended instruction (a new `_SPEC_TOOLS_INSTRUCTION`)
to say the spec tools exist and when to prefer them over the draft tools.

### Services parameterized over artifact

- **`proposing.py`** — `propose_prose_edit` gains `target_artifact: str =
  "draft"` (default preserves all current callers); the spec tool passes
  `"spec"`. `capture_base` is parameterized over the artifact filename only if a
  caller needs it (the agent path uses the read-time snapshot, not
  `capture_base`).
- **`accepting.py`** — light up the `if pc.target_artifact != "draft"` guard
  (currently `accepting.py:75`, `raise NotImplementedError("spec target is
  SP2b")`). For a **spec + `prose_edit`** proposal, replay
  `save_section(…, artifact=Artifact.SPEC)` (derive `Artifact(pc.target_artifact)`).
  `StaleSectionError → stale` (unchanged). **`SpecLockedError → re-raise`** so the
  endpoint maps it to 409 and the proposal stays `pending` (§2). Restructure the
  branch as: `if target_artifact == "spec"` → only `kind == "prose_edit"` is
  supported (spec is citation-free), any other spec kind raises
  `NotImplementedError`; the existing draft prose + `citation_insert` branches are
  untouched.
- **`staleness.py`** — `refresh_staleness` sweeps **both** artifacts: drop the
  `target_artifact == "draft"` filter; for each pending row, compare its
  `base_section_sha` against the live section sha of **its own** artifact's file
  (read each artifact file once, keyed by `filename_for(artifact)`), skipping an
  artifact whose file is absent (un-set-up workspace) as today.

This keeps the proven draft flow byte-for-byte while making every layer
artifact-aware through one parameter.

---

## Section 2 — Endpoints, gating, and accept behavior

The list/accept/reject routes in `proposal_assist/web/proposals.py` already
exist, are **member-gated**, and already expose `target_artifact`. Three
structural fixes are needed before the SPA can consume them, plus the behavioral
changes:

**Relocate the router under `/api` (Codex Medium #5).** Today the router is
mounted at root (`prefix="/workspaces"`, `main.py:530`), so its errors return
FastAPI's plain `{"detail": …}` — but the SPA's `unwrap` (`api/client.ts:21`)
only reads the `/api` envelope `{error:{code,message}}` (the envelope handler is
`api_request`-gated, `main.py:331/344`). So a 409 ("spec is locked…") would
reach the UI as a generic "request failed". Fix: mount these endpoints under
`/api` (full path `/api/workspaces/{id}/proposed-changes`) so they get the
envelope and `unwrap` surfaces the detail. **Safe — no consumer exists yet** (no
UI, the agent never calls them). Regenerate `schema.gen.ts` after the move.

**Expose the proposed prose text (Codex High #2).** The current `_serialize`
(`proposals.py:30`) omits `payload`, so the UI has no `new_text` to diff. Add
`"new_text": pc.payload.get("new_text") if pc.kind == "prose_edit" else None` to
the serialized row.

- **List** (`GET …/proposed-changes`): add an optional `?target_artifact=
  draft|spec` filter (omitted = all) so each panel fetches only its artifact's
  rows (§3). `refresh_staleness` (now both-artifact) runs before returning, so
  opening the Spec tab proactively flips stale spec rows. The list returns only
  `status == "pending"` rows (unchanged) — so a freshly-staled row simply drops
  out (this shapes the §3 stale handling).
- **Accept** (`POST …/{pc_id}/accept`): member-gated (unchanged). The service
  now handles spec via `save_section(artifact=SPEC)`. New/changed mappings:
  **`SpecLockedError → 409`** "spec is locked — unlock to accept", **proposal
  stays `pending`** (status unchanged, rollback) — retryable after unlock, not
  terminal; **`NotImplementedError → 400`** "unsupported proposal kind" so a
  DB-permitted-but-unsupported row (e.g. a `spec`+`citation_insert`, which the
  CHECK allows, `models.py:966`) returns a clean 4xx instead of a sanitized 500
  (Codex Medium #6); and **`CommitMutexTimeout → 429` + `Retry-After`** (was 503)
  — a busy workspace is retryable, and under the `/api` envelope a ≥500 is
  sanitized to "internal_error" (losing the message), so 429 both matches the
  SP2b-2a cockpit convention and survives the envelope. Existing reused:
  `SectionStructureError→400`, `IndexError→404`, `ProposedChangeNotPending→409`,
  `GitCommitError→500`. A `StaleSectionError` is **not** an endpoint error: the
  service catches it, flips the row to `stale`, and returns it 200 (see §3).
- **Reject** (`POST …/{pc_id}/reject`): member-gated, artifact-agnostic already
  (flips status) — works for spec rows unchanged. Note: `reject_proposed_change`
  only accepts `pending` rows (non-pending → `ProposedChangeNotPending`/409), so
  there is no "reject a stale row" path (shapes §3).

### Accept-behavior matrix for a spec proposal

| Situation | Outcome |
|---|---|
| Spec unlocked, base matches | commit → `accepted`, `resulting_commit_sha` set |
| Spec section edited since the agent read it | `StaleSectionError` → `stale`, resolver stamped |
| **Spec locked** | **409 "unlock to accept", stays `pending`** |
| No-op (proposed text == live) | `save_section` short-circuits → `accepted` |
| Workspace busy (mutex) | **429** + `Retry-After`, stays `pending` |

### Gating decision (member-gated) and the lock-as-control synthesis

Accept/reject of a spec proposal is **member-gated**, identical to the draft —
**not** owner-only. Owner-only would bottleneck collaboration. Instead, **the
owner's control lever is the spec lock**: a locked spec turns every accept into
a 409 (member or not) until the owner unlocks, enforced through the
`save_section` chokepoint built in SP2b-2a. No artifact-dependent permission
code is required.

**Resulting asymmetry (intentional, documented):** direct spec *typing* stays
owner-only (SP2b-2a's `post_save_spec_section`), but reviewing/accepting agent
spec proposals is collaborative and **audited** (`resolved_by`/`resolved_at`).
Unilateral edits = owner; reviewed, attributed proposals = members.

---

## Section 3 — Frontend: the inline review surface

**One shared component, `<ProposalReview artifact workspaceId>`**, rendered
inside both `SpecPanel` (artifact `"spec"`) and `DraftPanel` (`"draft"`).
Artifact-parameterized, so wiring it into both panels lights up **spec
proposals (the 2b goal) and the draft proposals stranded since SP2a** together.

**Behavior:**
- Fetches that artifact's pending rows via `useProposedChanges(id, artifact)` →
  `GET /api/workspaces/{id}/proposed-changes?target_artifact=spec`, then
  **renders `kind == "prose_edit"` rows only** (Codex High #3). Draft
  `citation_insert` proposals have an anchor/reference/evidence payload, not
  `new_text`, so the current-section→`new_text` diff can't render them; citation
  proposal review is **out of v1 scope** (deferred). The "Pending proposals (N)"
  count reflects the prose rows shown. The spec is citation-free, so it's always
  fully covered; for the draft, prose proposals light up and citation proposals
  stay unsurfaced for now.
- Each proposal shows: the target section (heading), a **minimal client-side
  unified line diff** of the current section text (from the artifact query's
  section at `section_index`) → the proposed `new_text` (now in the serialized
  row, §2); a plain line diff suffices (citation-free spec → no renumber noise;
  richer word-diff deferred); light attribution ("proposed by the assistant" +
  timestamp); and **Accept / Reject**.
- **Stale rows never render (Codex High #4).** The list returns only `pending`
  rows, and `refresh_staleness` (run on every list fetch) flips a no-longer-
  applicable proposal to `stale` — so it simply drops out of the list. There is
  no pre-emptive "stale, Accept-disabled, Reject" row (the backend wouldn't even
  return it, and `reject` rejects only `pending`). The one place staleness
  surfaces is **accept-time**: if the section moved between fetch and accept,
  `accept` returns **200 with `status == "stale"`** (the service flips it, not an
  error). The accept hook detects `result.status !== "accepted"` and shows a
  brief "this proposal went stale — reloading" notice, then invalidates (the row
  drops out on refetch).
- **Locked spec:** when `SpecPanel` already knows `locked` (from `useSpec`),
  Accept is disabled with an "unlock to accept" hint — proactively mirroring the
  backend 409 so the member doesn't hit the error path.

**Hooks** (new `frontend/src/api/proposals.ts`):
- `useProposedChanges(id, artifact)` — key `["proposed-changes", id, artifact]`.
- `useAcceptProposal(id, artifact)` — on success invalidates **both**
  `["proposed-changes", id, artifact]` and the artifact content query
  (`["spec", id]` / `["draft", id]`) so the editor refetches the new commit. A
  409 surfaces the backend detail and invalidates to refresh.
- `useRejectProposal(id, artifact)` — invalidates the proposals list.

**Placement:** inside SP2b-2a's `spec-shell` wrapper, the order becomes: lock
banner → lock control → **pending-proposals strip** → editor. `DraftPanel` gets
a matching light wrapper. Scope guard (YAGNI): no cross-artifact inbox, no bulk
accept, no filters.

---

## Section 4 — Testing strategy + risks

### Backend tests
- **Proposing:** `propose_prose_edit(target_artifact="spec")` writes a spec row;
  the `propose_spec_edit` tool creates a pending spec `ProposedChange` whose base
  is the read-time spec snapshot; `propose_spec_edit` before `read_spec` →
  "haven't read" error.
- **Agent tools:** `read_spec` populates `agent_view_spec_*`; the draft tools are
  unchanged (explicit regression assertion).
- **Accept (the §2 matrix):** unlocked+match → `accepted` + spec committed +
  `resulting_commit_sha`; section moved → `stale` + resolver stamped; **locked →
  409 + stays `pending`**; no-op → `accepted`; draft accept unchanged.
- **Reject:** spec row → `rejected`.
- **Staleness:** `refresh_staleness` flips a stale spec row and a stale draft row
  in one sweep, each against its own file; absent artifact file → no-op.
- **Endpoints:** `?target_artifact=spec` returns only spec rows (omitted → all);
  the serialized row carries `new_text` for a prose proposal; member can accept a
  spec proposal, stranger → 403; locked-spec accept → **409** + message + pending;
  an unsupported spec kind → **400** (not 500); busy → **429** + `Retry-After`;
  errors come back in the `/api` envelope (`{error:{code,message}}`) so the
  detail is present.

### Frontend tests (Vitest + jsdom)
- `<ProposalReview>` renders nothing when empty and the strip+diff+actions when
  pending; **only `prose_edit` rows render** (a draft `citation_insert` row is not
  shown); accept invalidates **both** `["spec", id]` and `["proposed-changes",
  id, "spec"]`; an accept that returns `status == "stale"` shows the went-stale
  notice and invalidates (no stale row is ever rendered up-front); a locked spec
  disables Accept with the hint; `SpecPanel`/`DraftPanel` mount `<ProposalReview>`
  with the right artifact.

### Risks (all structurally mitigated)
1. **Accept vs. concurrent human spec edit** → `save_section` staleness
   (`base_section_sha`) → `stale`. Proven on the draft.
2. **Accept vs. owner lock** → `save_section` refreshes `spec_locked_at` under
   the commit mutex (SP2b-2a) → `SpecLockedError` → 409, pending. No corruption.
3. **Double-accept** → `_load_scoped` `FOR UPDATE` serializes; loser sees a
   terminal status → 409. Proven (SP2a).
4. **Diff drift between fetch and accept** — the diff is *current vs proposed* at
   fetch time; if the section moves afterward the diff is briefly stale, but
   `accept` then returns `status == "stale"` (the service flips it) and the UI
   shows the went-stale notice + invalidates. Stale rows are never rendered
   up-front (the list returns only `pending`). Bounded and self-correcting.
5. **Agent proposes to the wrong artifact** — dedicated tool names + a
   system-prompt line reduce it; worst case it's a reviewable, rejectable pending
   row, never a direct commit.

---

## File-touch summary

| File | Change |
|------|--------|
| `proposal_assist/tools/proposal.py` | `read_spec_impl` / `propose_spec_edit_impl` via a shared artifact-parameterized core; draft impls unchanged |
| `proposal_assist/tools/registry.py` | register `read_spec` + `propose_spec_edit`; add `agent_view_spec_*` fields to `ToolContext` |
| `proposal_assist/drafting/claude_adapter.py` | **add `read_spec` + `propose_spec_edit` to `allowed_tools`** (~L237) — registration alone doesn't make them callable; **append `_SPEC_TOOLS_INSTRUCTION` to the `system_prompt` preset** (~L225) |
| `proposal_assist/proposals/proposing.py` | `propose_prose_edit` gains `target_artifact="draft"` param |
| `proposal_assist/proposals/accepting.py` | light up the spec path: `if target=="spec"` → only `prose_edit`, replay `save_section(artifact=SPEC)`; re-raise `SpecLockedError`; non-prose spec kind → `NotImplementedError` |
| `proposal_assist/proposals/staleness.py` | `refresh_staleness` sweeps both artifacts (per-file section-sha compare, keep `FOR UPDATE SKIP LOCKED`, absent file → no-op) |
| `proposal_assist/web/proposals.py` | `_serialize` adds `new_text` (prose only); `?target_artifact` list filter; accept maps `SpecLockedError→409`, `NotImplementedError→400`, `CommitMutexTimeout→429`+Retry-After |
| `main.py` | mount `proposals_router` under **`/api`** (L530) so it gets the error envelope |
| `frontend/src/api/proposals.ts` | `useProposedChanges` / `useAcceptProposal` (handles 200-stale return) / `useRejectProposal` |
| `frontend/src/cockpit/ProposalReview.tsx` | shared inline review component, **prose-only**, minimal line diff |
| `frontend/src/cockpit/SpecPanel.tsx`, `DraftPanel.tsx` | mount `<ProposalReview artifact=…>` |
| `frontend/src/api/schema.gen.ts` | regen (new `/api/.../proposed-changes` paths, `?target_artifact`, `new_text` field) |
| tests | proposing/agent-tools/accept-matrix/reject/staleness/endpoints (+ NotImplementedError→400, busy→429, /api envelope) + frontend (prose-only, stale-on-accept, locked-disable) |
