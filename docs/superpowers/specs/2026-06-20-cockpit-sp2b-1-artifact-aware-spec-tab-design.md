# Cockpit SP2b-1 — Artifact-aware foundation + Spec tab — Design Spec

_Status: design spec, ready for implementation planning. First of the three SP2b slices
defined in [2026-06-17-cockpit-workspace-design.md](2026-06-17-cockpit-workspace-design.md) §11
(items 7/8/9). Builds on the merged SP2a foundation (propose/accept substrate, CodeMirror-6
source editor, citation peek, select-to-cite)._

## 1. Context & scope

SP2b is the cockpit "spine": the **Spec artifact** + the spec↔draft **coherence loop** + the
continuous **evaluator/Scorecard** ([cockpit design §2, §8, §10](2026-06-17-cockpit-workspace-design.md)).
It decomposes into three dependency-ordered slices (cockpit design §11 items 7/8/9). **This spec
covers only the first slice, SP2b-1**: the artifact-aware foundation plus the Spec as a
first-class **editable, citation-free tab**.

**In scope (SP2b-1):**
- A single artifact abstraction (`Artifact` enum + `filename_for`) consolidating the duplicated
  `PROPOSAL_FILENAME` constant.
- Generalize the **save** and **read-model** layer over the target artifact
  (`proposal.md` | `proposal-spec.md`).
- The **Spec artifact** as an editable tab in the cockpit: its own outline + section-at-a-time
  editing + a read-only Full-spec view, reusing the CM6 editor — but **citation-free**.
- A backend Spec **skeleton** + an idempotent **initialize** endpoint.
- Frontend: extract a reusable `<ArtifactSectionEditor>` from `Cockpit.tsx`; add the Spec tab.

**Explicitly deferred (later SP2b slices), do not build here:**
- The **lifecycle migration** — `Workspace.current_phase` `{setup,spec,plan,draft,review}` →
  `{setup,active}`, `spec_locked_at/by`, `spec_lock_events`, retire/repurpose `phase_approvals`
  (cockpit design §3). **SP2b-1 does not touch the phase machine or spec-lock.**
- **Agent interaction with the Spec** — `propose_*`/`accept` routing for `target=spec`
  (`proposals/accepting.py:75` still guards `target_artifact != "draft"`; `proposing.py`
  hardcodes `"draft"`), and the read-only tool surface reading the spec. → **SP2b-2** (coherence
  keeper).
- **Citations in the Spec** — the Spec is a statement of intent; evidence lives in the draft
  (cockpit design §2, §8). The citation machinery (`tools/citations.py`,
  `citations/human_cite.py`, `web/api/workspaces.py` citation routes, `analysis/insert.py`)
  stays **draft-only**.
- **`tools/history.py` artifact parameterization** — nothing reads spec history in SP2b-1
  (no Versions UI for either artifact yet; no agent-spec interaction). → SP2b-2 when consumed.
- **Coherence loop, Scorecard, Readiness** → SP2b-2 / SP2b-3.

**Locked priors (from the cockpit design, do not re-litigate):** CodeMirror-6 **source** editing
(not WYSIWYG); section-level granularity keyed on byte-exact `sha256(section.text)`; React over
the FastAPI JSON API; human edits are **direct commits** (only AI edits are proposals — and the
AI doesn't touch the spec until SP2b-2).

## 2. The artifact abstraction

Today `proposal.md` is hardcoded as `PROPOSAL_FILENAME`, **defined twice**
(`editing/save.py:33`, `tools/proposal.py:23`), and `ProposedChange.target_artifact` already
carries the string values `'draft' | 'spec'` (`models.py:958` CHECK; `985`).

**Create `src/call_manager/proposal_assist/artifacts.py`** as the single source of truth:

```python
from enum import StrEnum

class Artifact(StrEnum):
    DRAFT = "draft"   # values MUST match ProposedChange.target_artifact (models.py:958)
    SPEC = "spec"

_FILENAMES: dict[Artifact, str] = {
    Artifact.DRAFT: "proposal.md",
    Artifact.SPEC: "proposal-spec.md",
}

def filename_for(artifact: Artifact) -> str:
    return _FILENAMES[artifact]

PROPOSAL_FILENAME = filename_for(Artifact.DRAFT)   # back-compat re-export
SPEC_FILENAME = filename_for(Artifact.SPEC)
```

- Consolidate the two `PROPOSAL_FILENAME` definitions to **re-export from `artifacts.py`**
  (`editing/save.py` and `tools/proposal.py` import it from here; existing importers of
  `PROPOSAL_FILENAME` from those modules keep working).
- Generalized functions take `artifact: Artifact = Artifact.DRAFT` as a **defaulted keyword
  param**, so **every existing draft call site compiles unchanged** and the diff stays small.
- `StrEnum` (not a plain string) for type-safety + matching the existing CHECK values. No richer
  value-object: the only per-artifact behavior differences (skip citations/analysis for spec) are
  few enough to branch on explicitly.

## 3. Backend: `save_section` generalized over the artifact

`save_section` (`editing/save.py:159`) is the single owner-edit write path. Today, beyond writing
`PROPOSAL_FILENAME`, it runs two **draft-only** post-commit steps: **Tier-1 citation validation**
(`_tier1_issues` → bare-marker scan + `validate_citations_deterministic`, step 9, and the
no-change short-circuit) and **Tier-2 claim-analysis enqueue** (`enqueue_section_job`, step 10).

**Changes (one function, minimal diff):**
1. Add `artifact: Artifact = Artifact.DRAFT`; **normalize at entry** (`artifact =
   Artifact(artifact)`) so a future raw-`"draft"`-string caller still hits the draft pipeline
   rather than silently skipping Tier-1/Tier-2 (`StrEnum` makes `Artifact("draft") is
   Artifact.DRAFT`, so the identity gate below stays valid). Read/write/commit
   `filename_for(artifact)` instead of the literal `PROPOSAL_FILENAME`.
2. **Gate the two draft-only steps behind `if artifact is Artifact.DRAFT`** (both the post-commit
   block and the no-change short-circuit). For the Spec: `issues = []`, `job_enqueued = False` —
   no bare-marker check, no analysis job.
3. **Keep shared** (artifact-agnostic; the Spec needs them identically): the per-workspace commit
   mutex, the HEAD/`base_section_sha` staleness predicate (`save.py:180`), the H2 **structure
   guard** (`save.py` step 5b — a non-preamble section edit must start with `## `; the preamble
   must not), newline glue, the no-change short-circuit, the join-invariant reassembly, and the
   `ArtifactVersion` / `WorkspaceEvent` provenance.
4. **Tagging:** `ArtifactVersion.message` → `f"user {artifact} edit: {heading}"`; add
   `"artifact": str(artifact)` to the `WorkspaceEvent.payload`, keep `kind="user_edit"` (a
   separate kind would fragment Activity-feed consumers; the payload field labels spec-vs-draft).

The Spec save path is thus `save_section` **minus exactly the two draft-only pipeline steps** —
no parallel function, no duplicated stale/structure/conflict logic. `SaveResult` is unchanged
(spec returns `issues=[]`, `job_enqueued=False`).

The **sectionizer needs no change**: `split_sections(text)` (`editing/sectionizer.py`) is a pure,
artifact-agnostic function on text; "read generalization" is just `filename_for(artifact)` + the
same `split_sections`.

## 4. Backend: the Spec read / save / init API

Add **parallel `/spec` routes** (not `?artifact=` on `/draft`, not a generic `/artifacts/{kind}`
rename) so the shipped, Codex-reviewed draft endpoints stay byte-untouched. There are only ever
two artifacts in scope, so a generic HTTP layer is over-abstraction; the **service layer** is
where artifact-awareness lives. All in `src/call_manager/web/api/workspaces.py`, mirroring the
draft routes' conventions (`require_member`/`require_owner`, `request.app.state.{settings,
session_factory}`, the global `{error:{code,message}}` envelope).

**Response model** (distinct from `DraftOut` — no `citations` field):
```python
class SpecOut(BaseModel):
    head: str
    can_edit: bool
    sections: list[SectionOut]   # reuse SectionOut {index, heading, text, sha}
```

**`GET /workspaces/{id}/spec` → `SpecOut`** (member-gated). Reads `filename_for(SPEC)`, splits
into `SectionOut`s, returns `head`/`can_edit`/`sections`. No citation map. (`proposal-spec.md`
exists from setup — seeded empty — so an empty spec returns `sections` per `split_sections("")`,
i.e. a single empty preamble section; the frontend treats a **whitespace-only** spec as the empty
state. If the file is somehow absent, 404 — same as the draft.) Extract the shared
`split_sections → list[SectionOut]` builder used by both `get_draft` and this route.

**`POST /workspaces/{id}/spec/sections/{index}` → `SaveResultOut`** (owner-gated). Reuse the draft
request model **`SaveSectionIn`** verbatim — body `{content, base_commit_sha, base_section_sha}`
(the HTTP field is `content`; `content` is what the endpoint passes as the core's `new_text=`
kwarg — `workspaces.py:364`). Calls `save_section(artifact=Artifact.SPEC, …)`. Maps exactly like
`post_save_section` (`workspaces.py:370-384`): `StaleSectionError → 409 conflict`,
`SectionStructureError → 400 bad_request`, `IndexError → 404`,
`CommitMutexTimeout → 429 + Retry-After`, `GitCommitError → 500`.

**`POST /workspaces/{id}/spec` → `SpecOut`** (owner-gated) — **idempotent skeleton init.** Under
the commit mutex: read `filename_for(SPEC)`; if it is **empty (whitespace-only)**, write the
`SPEC_SKELETON` (§5) as a **single whole-doc commit** (`write_and_commit_files` + `ArtifactVersion`
`message="user spec init"` + a `WorkspaceEvent kind="user_edit"` with `artifact="spec"`), then
return the seeded `SpecOut`. If it **has any non-whitespace content**, no-op → return the existing
`SpecOut` (never clobber user content — including preamble-only text).

**Section count after seeding is 6, not 5.** `split_sections` always emits an `index=0` preamble
(`sectionizer.py:44`), and the `SPEC_SKELETON` leads with `## Aims`, so the result is the empty
preamble (index 0) + the five H2 sections (indices 1–5). `section.index === array position`
because `split_sections` enumerates `0..n` in order, so the extracted editor keeps the draft's
`sections[activeIndex]` indexing **unchanged** and renders the empty preamble exactly as the draft
does (`s.heading || "preamble"`, `Cockpit.tsx:180`) — **no preamble-hiding, no index refactor.**
Tests assert 6 sections.
This covers **new and existing** workspaces uniformly with no data migration, sidesteps the
preamble-structure-guard problem (whole-doc seed, not a per-section save of a `##`-leading
preamble), and is a POST (not a side-effecting GET). Concurrent double-init serializes on the
mutex + the only-if-empty check (the second sees content → no-op).

`setup.py` is **unchanged** (it keeps seeding an empty `proposal-spec.md` in the initial commit);
the init endpoint is the single seed mechanism.

## 5. The Spec skeleton

A backend constant (in `artifacts.py` or a small `proposal_assist/spec_skeleton.py`):

```
## Aims

## Approach

## Novelty & Significance

## Expected Impact

## Key Claims
```

A light conventional skeleton (cockpit design §8). `## Key Claims` is the future hook for the
coherence loop / `claim_drift` (SP2b-2), but in SP2b-1 it is just a heading. The skeleton may
become template-driven later; out of scope now.

## 6. History / versioning — minimal

- **No `ArtifactVersion.artifact` column.** `ArtifactVersion` is commit-level provenance (one row
  per commit; `models.py:910`); per-artifact history is git-derivable by filename filter, and the
  Activity feed distinguishes spec-vs-draft via the `WorkspaceEvent.payload.artifact` field (§3).
- **`tools/history.py` (`get_artifact_history`/`read_artifact_at`/`diff_artifact`) is NOT
  parameterized in this slice** — no consumer (no Versions UI for either artifact; no agent-spec
  interaction). The `artifacts.py` abstraction makes that a trivial filename-thread when SP2b-2's
  coherence keeper needs `diff_artifact(spec)`.
- A reconcile sanity check: the analysis/reconcile tick keys on `proposal.md`; a now-non-empty
  `proposal-spec.md` must not perturb it.

## 7. Concurrency

No new mechanism. Both artifacts live in **one git repo per workspace**, and `save_section`
already takes the **per-workspace** commit mutex (`acquire_commit_mutex(session, workspace.id)`,
`drafting/locking.py:144`). The spec save reuses it:
- The mutex **must stay per-workspace, not per-artifact** — a per-artifact lock would let a draft
  commit and a spec commit hit the same git index concurrently and race. Serializing draft-vs-spec
  saves is correct and cheap.
- The staleness predicate handles **cross-artifact HEAD movement for free**: a draft-only commit
  advances repo HEAD but doesn't change the spec section's bytes, so
  `head != base_commit_sha AND live.sha != base_section_sha` stays false → the spec save still
  succeeds (identical to the cross-*section* case). `base_commit_sha` is the shared repo HEAD
  returned by `GET /spec`.

## 8. Frontend: the Spec tab

Per the cockpit layout (§9), `Spec` is a center **View** tab; selecting it shows the spec's own
outline + section-at-a-time editor + a read-only Full-spec view — the **same** section-editing
apparatus the draft uses, **minus** citations/peek/select-to-cite.

**Extract a reusable `<ArtifactSectionEditor>`** from `Cockpit.tsx` (the section-buffer state,
dirty tracking, the **409-safe save** that never drops the user's buffer — cockpit design §6 — the
outline + View tablist + the `CodeMirrorEditor` mount):
- The **draft** view composes the citation layer (pills, peek, pin, select-to-cite, composer) on
  top, via props/children.
- The **Spec** view passes a citations-disabled flag and renders the harness alone. **This is a
  real `CodeMirrorEditor` change, not just a prop the harness ignores:** today
  `CodeMirrorEditor.tsx:123` mounts the `citationPills(...)` extension **unconditionally**, so
  marker-like spec text (`[@key]<!-- ev:… -->`) would still become atomic pill widgets. Add a
  `citations?: boolean` prop (default `true`, preserving every current draft mount) that gates
  whether the `citationPills` extension — and the `onSelectionChange`/peek wiring — are included.
  The Spec mounts with `citations={false}`, so it renders **raw, fully editable** markdown text.
- The extraction is **behavior-preserving for the draft**: `Cockpit.test.tsx` and the editor/
  citation tests **stay green** — that green suite is the load-bearing regression guard. This also
  improves an over-large file (the brainstorming "improve the code you're working in" principle).

**Hooks** (in `frontend/src/api/editor.ts`, alongside `useDraft`/`useSaveSection`):
`useSpec(id)` (GET `/spec`), `useSaveSpecSection(id)` (POST `/spec/sections/{index}`),
`useInitSpec(id)` (POST `/spec`). `useSaveSpecSection`/`useInitSpec` invalidate `["spec", id]`.

**Empty-state:** when the Spec tab opens a **whitespace-only** spec, show a **"Start spec from
skeleton"** affordance → `useInitSpec` → the editor shows the seeded sections. Explicit user
action, not a silent commit-on-tab-open. (Same whitespace-only test the init endpoint uses, so
the CTA and the seed agree.)

**Schema:** regen `frontend/src/api/schema.gen.ts` for the three `/spec` routes
(`EMBEDDING_MODEL=bge-m3 pnpm gen:api`).

## 9. Error handling & status mapping

The `/spec` routes reuse the draft routes' envelope + status conventions exactly:
`403 forbidden` (non-owner save / non-member read), `404 not_found` (absent file / section out of
range), `409 conflict` (`StaleSectionError`), `400 bad_request` (`SectionStructureError` — matches
the shipped draft route `workspaces.py:376-384`, **not** 422),
`429 too_many_requests` (`CommitMutexTimeout`, + `Retry-After`), `500 internal_error`
(`GitCommitError`, sanitized). The client's 409 handler **must not drop the user's buffer** (queue/
re-prompt) — inherited from the shared `<ArtifactSectionEditor>`.

## 10. Testing

**Backend:**
- `filename_for` mapping; `PROPOSAL_FILENAME` re-export unchanged.
- `save_section(artifact=SPEC)`: commits `proposal-spec.md`; `issues=[]`, `job_enqueued=False`
  (no bare-marker check, no analysis enqueue); structure guard still applies (spec section must
  keep `## `; preamble must not start with `## `); no-change short-circuit.
- **Cross-artifact staleness:** a draft commit moves HEAD but the spec section is unchanged →
  spec save **succeeds**; a real spec-section change with a stale base → `StaleSectionError`.
- **Draft regression:** `save_section` (default artifact) still validates citations + enqueues.
- `GET /spec`: member-gated; no `citations` field; correct `head`/`sections`; stranger → 403.
- `POST /spec/sections/{i}`: owner-gated; **409/400/429** mapping mirrors `post_save_section`
  (`SectionStructureError → 400`, not 422).
- `POST /spec` init: empty → seeds skeleton in one commit, returns `SpecOut` with **6 sections**
  (the empty `index=0` preamble + the 5 H2 sections); non-empty → idempotent no-op returns
  existing; member (non-owner) → 403; concurrent double-init → exactly one skeleton commit.
- Reconcile/analysis tick ignores `proposal-spec.md` content.

**Frontend:**
- **Draft tests stay green** (the `<ArtifactSectionEditor>` extraction is behavior-preserving) —
  the primary regression guard.
- `useSpec` / `useSaveSpecSection` / `useInitSpec` hooks (success + the 409-invalidation idiom).
- **`CodeMirrorEditor citations={false}`**: marker-like text (`[@k]<!-- ev:… -->`) renders as
  **raw editable characters** — assert **no `.cm-citation-pill`** widget and no peek/selection
  wiring; the default `citations` mount (draft) still shows pills (existing tests stay green).
- Spec tab: empty spec → "Start spec from skeleton" → `useInitSpec` → editor shows skeleton
  sections (outline lists the empty `preamble` item + 5 H2s, matching the draft's preamble
  rendering); edit a spec section → save; dirty/staleness behavior (reused harness).
- `schema.gen.ts` no-drift after regen.

**Accessibility** of the Spec tab/outline is a release-gate obligation (consistent with the
cockpit design §12), reusing the editor's existing a11y.

## 11. Out of scope (later slices)

SP2b-2: lifecycle migration + spec-lock; the coherence-keeper + evaluator background sessions;
`propose_*`/`accept` for `target=spec`; `tools/history.py` artifact parameterization; the
agent reading the spec. SP2b-3: Coverage/Scorecard, on-demand mock-panel, Readiness/submission
check. Citations in the Spec; `proposal-plan.md` formal retirement; a per-artifact Versions UI.

## 12. Build decomposition (for the plan)

A natural task order: (1) `artifacts.py` + consolidate the constant; (2) generalize `save_section`
(incl. entry normalization) + tests; (3) the shared `SectionOut` builder + `GET /spec`;
(4) `POST /spec/sections/{i}`; (5) `SPEC_SKELETON` + `POST /spec` init; (6) regen schema;
(7) add the `CodeMirrorEditor citations` prop (default `true`, draft stays green) + extract
`<ArtifactSectionEditor>`; (8) Spec hooks + Spec tab (`citations={false}`) + empty-state CTA;
(9) full verification.
