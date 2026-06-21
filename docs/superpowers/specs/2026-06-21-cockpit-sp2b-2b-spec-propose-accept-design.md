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
current behavior — a regression test guards the draft path. The agent's system
prompt gains one line: the spec tools exist; use them when the user's request is
about the spec rather than the draft.

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
exist, are **member-gated**, and already expose `target_artifact`. Changes are
mostly behavioral:

- **List** (`GET …/proposed-changes`): add an optional `?target_artifact=
  draft|spec` filter (omitted = all) so each panel fetches only its artifact's
  rows (§3). `refresh_staleness` (now both-artifact) runs before returning, so
  opening the Spec tab proactively flips stale spec rows.
- **Accept** (`POST …/{pc_id}/accept`): member-gated (unchanged). The service
  now handles spec via `save_section(artifact=SPEC)`. One new mapping:
  **`SpecLockedError → 409`** "spec is locked — unlock to accept", **proposal
  stays `pending`** (status unchanged, rollback) — retryable after unlock, not
  terminal. All existing mappings reused: `SectionStructureError→400`,
  `IndexError→404`, `CommitMutexTimeout→503`, `ProposedChangeNotPending→409`,
  `GitCommitError→500`.
- **Reject** (`POST …/{pc_id}/reject`): member-gated, artifact-agnostic already
  (flips status) — works for spec rows unchanged.

### Accept-behavior matrix for a spec proposal

| Situation | Outcome |
|---|---|
| Spec unlocked, base matches | commit → `accepted`, `resulting_commit_sha` set |
| Spec section edited since the agent read it | `StaleSectionError` → `stale`, resolver stamped |
| **Spec locked** | **409 "unlock to accept", stays `pending`** |
| No-op (proposed text == live) | `save_section` short-circuits → `accepted` |
| Workspace busy (mutex) | existing 503, stays `pending` |

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
  `GET …/proposed-changes?target_artifact=spec`. When none, renders nothing.
  When N>0, a compact "Pending proposals (N)" strip sits above the editor.
- Each proposal shows: the target section (heading), a **minimal client-side
  unified line diff** of the current section text → proposed `new_text` (the
  spec is citation-free, so no citation-renumber noise — a plain line diff
  suffices; richer word-diff deferred), light attribution ("proposed by the
  assistant" + timestamp), and **Accept / Reject**.
- **State-aware actions:** a `stale` row (flipped by `refresh_staleness`) shows
  as stale with Accept disabled, Reject offered. When the **spec is locked**
  (`SpecPanel` already has `locked` from `useSpec`), Accept is disabled with an
  "unlock to accept" hint — proactively mirroring the backend 409.

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
  member can accept a spec proposal, stranger → 403; locked-spec accept → 409 +
  message + pending.

### Frontend tests (Vitest + jsdom)
- `<ProposalReview>` renders nothing when empty, the strip+diff+actions when
  pending; accept invalidates **both** `["spec", id]` and `["proposed-changes",
  id, "spec"]`; a locked spec disables Accept with the hint; a stale row disables
  Accept and offers Reject; `SpecPanel`/`DraftPanel` mount `<ProposalReview>`
  with the right artifact.

### Risks (all structurally mitigated)
1. **Accept vs. concurrent human spec edit** → `save_section` staleness
   (`base_section_sha`) → `stale`. Proven on the draft.
2. **Accept vs. owner lock** → `save_section` refreshes `spec_locked_at` under
   the commit mutex (SP2b-2a) → `SpecLockedError` → 409, pending. No corruption.
3. **Double-accept** → `_load_scoped` `FOR UPDATE` serializes; loser sees a
   terminal status → 409. Proven (SP2a).
4. **Misleading diff when stale** — the minimal diff is *current vs proposed*; if
   the base moved it can look off, but the row is `stale` and Accept is disabled,
   so it can't be acted on. Accepted for the minimal UI.
5. **Agent proposes to the wrong artifact** — dedicated tool names + a
   system-prompt line reduce it; worst case it's a reviewable, rejectable pending
   row, never a direct commit.

---

## File-touch summary

| File | Change |
|------|--------|
| `proposal_assist/tools/proposal.py` | `read_spec_impl` / `propose_spec_edit_impl` via a shared artifact-parameterized core; draft impls unchanged |
| `proposal_assist/tools/registry.py` | register `read_spec` + `propose_spec_edit` (→ 21-tool allowlist); add `agent_view_spec_*` to `ToolContext`; one system-prompt line |
| `proposal_assist/proposals/proposing.py` | `propose_prose_edit` gains `target_artifact="draft"` param |
| `proposal_assist/proposals/accepting.py` | light up the spec path: replay `save_section(artifact=SPEC)`; re-raise `SpecLockedError` |
| `proposal_assist/proposals/staleness.py` | `refresh_staleness` sweeps both artifacts |
| `proposal_assist/web/proposals.py` | `?target_artifact` list filter; `SpecLockedError → 409` on accept |
| `frontend/src/api/proposals.ts` | `useProposedChanges` / `useAcceptProposal` / `useRejectProposal` |
| `frontend/src/cockpit/ProposalReview.tsx` | shared inline review component + minimal line diff |
| `frontend/src/cockpit/SpecPanel.tsx`, `DraftPanel.tsx` | mount `<ProposalReview artifact=…>` |
| `frontend/src/api/schema.gen.ts` | regen (new `?target_artifact` param) |
| tests | proposing/agent-tools/accept-matrix/reject/staleness/endpoints + frontend |
