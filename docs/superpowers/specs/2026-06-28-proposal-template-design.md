# Proposal Template (v1: scaffold + validate) — Design Spec

_Brainstormed 2026-06-28; revised after a Codex review pass. Implements the
`ProposalTemplate` primitive locked in
[2026-06-15-collaborative-proposal-workspace-design-direction.md](../notes/2026-06-15-collaborative-proposal-workspace-design-direction.md)
(§"Proposal template", §"New backend primitives required"). This is the first
cut; the reusable **org-level template library** and **LLM derive-from-call** are
explicitly deferred (§10)._

## 1. Goal

Let a workspace carry the **funder's required proposal structure** — an ordered
list of sections, each with guidance text, an optional length budget, and a
required/optional flag — then:

1. **Scaffold** the draft (`proposal.md`) from it at setup, and
2. **Softly validate** the live draft against it (missing required sections,
   over-budget sections), surfaced in the existing Problems panel, a live length
   counter, and to the agent.

This closes the gap documented in the design direction: today the funder's
*evaluation criteria* are modeled (the rubric snapshot), but the funder's
required *structure* is not — the draft is seeded empty and the spec gets a
generic hardcoded skeleton.

## 2. Background — current state (verified)

- **Draft seeded empty.** Phase 0's repo init writes three empty artifact files
  and a single `"phase0: initialize empty artifacts"` git commit; it writes **no
  `ArtifactVersion` row** ([phases/setup.py:267-276](../../../src/call_manager/proposal_assist/phases/setup.py#L267)).
- **Spec seeded with a fixed skeleton** on first edit (`SPEC_SKELETON`, 5 headings)
  ([artifacts.py:36-38](../../../src/call_manager/proposal_assist/artifacts.py#L36)).
  The spec is the team's internal statement-of-intent doc; funder-required
  structure (the draft) is a different thing → spec is **out of scope** here.
- **Activation is DB-only.** `POST /workspaces/{id}/activate` applies the rubric,
  sets `job.status="done"`, flips `ws.current_phase="active"`, writes a
  `workspace_activated` event, and commits — **no git write today**
  ([web/api/rubric.py:205-232](../../../src/call_manager/web/api/rubric.py#L205)).
  Gated on `job.status == "waiting_for_rubric_review"` (else 409).
- **Commit-mutex pattern to reuse.** `save_section` calls
  `acquire_commit_mutex(session, workspace.id)`, reads/writes/commits git, adds
  provenance rows, and lets the **caller's** `session.commit()` release the mutex
  (git commit precedes the DB commit; a DB failure after the git commit is
  repaired by reconciliation) ([editing/save.py:178-197](../../../src/call_manager/proposal_assist/editing/save.py#L178)).
- **Rubric snapshot pattern to mirror.** `Workspace.rubric_criteria` is a JSONB
  snapshot ([db/models.py:707](../../../src/call_manager/db/models.py#L707)) copied
  from `CallRubric` at Phase 0; `ArtifactVersion.created_by` allows
  `'system'` ([db/models.py:1074-1086](../../../src/call_manager/db/models.py#L1074)).
- **Setup UI / gate.** The React `WorkspaceSetup` is a single gate screen ending
  in a **"Confirm & activate"** button that calls the activate mutation
  ([frontend/src/cockpit/WorkspaceSetup.tsx:100-107](../../../frontend/src/cockpit/WorkspaceSetup.tsx#L100));
  it disappears once the workspace is active.
- **Existing over-length signal (collision risk).** `over_length_problems(sections,
  budget)` already flags every draft section over a **single global character
  budget** ([problems/builders.py:61-82](../../../src/call_manager/proposal_assist/problems/builders.py#L61)),
  called from [problems/aggregate.py:125](../../../src/call_manager/proposal_assist/problems/aggregate.py#L125)
  and rendered under the `over_length` category in
  [ProblemsPanel.tsx:10-23](../../../frontend/src/cockpit/ProblemsPanel.tsx#L10).
- **Section model & guards.** Draft = one markdown doc split by `##`
  ([editing/sectionizer.py](../../../src/call_manager/proposal_assist/editing/sectionizer.py));
  `save_section` enforces the structure guard + optimistic concurrency. The
  invariant `"".join(s.text for s in split_sections(doc)) == doc` must hold for
  scaffolded output.

## 3. Scope

**In scope (v1):**
- Per-workspace template snapshot (data model + migration).
- Default-seed the snapshot at workspace creation.
- Owner authoring UI in **`WorkspaceSetup` (setup phase) and the owner Settings
  surface (active phase)** — §5.4.
- Idempotent scaffolding of `proposal.md` headings, folded into the activation
  transaction (§5.3).
- Soft validation: missing-required + over-budget signals (Problems panel), live
  length counter (status bar), agent read access (§6).
- A minimal **"insert missing section"** affordance from the outline (§8).
- Read/write API for the template snapshot (§7).
- Audit events: `template_initialized` / `template_edited` / `template_scaffolded`.

**Out of scope (v1) — see §10.**

## 4. Data model

No new table in v1. The template is a **per-workspace snapshot**, mirroring
`rubric_criteria`.

- **Migration:** add JSONB column `Workspace.template_sections` (nullable, default
  `NULL`). New workspaces are seeded at creation (§5.1). The migration does **not**
  backfill existing rows; `NULL` is read everywhere as the **default skeleton**
  (all `required:false`, no budgets), so pre-existing workspaces behave as today
  (no spurious signals) until their owner edits the template.
- **Shape:** ordered list of entries:
  ```json
  {
    "id": "01J...",              // stable opaque id; React keys, reorder, audit, future org-templates
    "order": 0,
    "heading": "Aims",
    "guidance": "State the overall objectives and the gap addressed.",
    "required": true,
    "length_unit": "words",      // "words" | "characters" | null
    "length_max": 500            // int > 0, or null (no budget)
  }
  ```
- **Write-time validation (→ 400):**
  - `order` is a 0-based contiguous sequence; `id` is present and unique.
  - `heading` is non-empty after trim; contains **no control characters**
    (reject `\r`, `\n`, and other C0/C1 controls) and **no leading `#`**
    (the scaffolder/API owns the `## ` prefix).
  - `heading` is unique across entries after normalization (§6).
  - `length_unit` and `length_max` are both set or both null; `length_max > 0`.
- **`heading`** is stored WITHOUT the leading `## `.

## 5. Setup & scaffolding

### 5.1 Default seed
At workspace creation (alongside the rubric snapshot), seed `template_sections`
with a default skeleton: the 5 headings (Aims / Approach / Novelty & Significance
/ Expected Impact / Key Claims), each with a fresh `id`, `required:false`, no
budgets. Emit a `template_initialized` event (mirrors `rubric_initialized`).

> **Behavior change (acceptable — tool is not yet deployed):** because scaffolding
> renders the template (§5.3), a brand-new workspace's draft will start with these
> 5 empty headings instead of a fully empty `proposal.md`. Validation stays silent
> (all `required:false`, no budgets). If a truly empty starting draft is preferred,
> the default seed can instead be an empty section list — called out for the user
> to decide.

### 5.2 Authoring
The owner edits the template via `PUT /api/workspaces/{id}/template`. Editing
emits a `template_edited` event.

### 5.3 Scaffolding — folded into the activation transaction (idempotent)
Extend the existing `POST /workspaces/{id}/activate` handler. After the current
rubric-apply + phase-flip + `workspace_activated` event, and **before**
`session.commit()`:

```
acquire_commit_mutex(session, ws.id)                      # serialize vs any git writer
doc = (repo_dir / PROPOSAL_FILENAME).read_text()
if doc.strip() == "":                                      # idempotency guard (authoritative)
    new_doc = render_template_headings(ws.template_sections)   # "## H\n\n## H2\n\n…", empty bodies
    if new_doc:                                            # non-empty template
        sha = write_and_commit_files(repo_dir, {PROPOSAL_FILENAME: new_doc},
                                     f"template scaffold (user:{user.id})")
        session.add(ArtifactVersion(workspace_id=ws.id, commit_sha=sha,
                                    created_by="system", message="template scaffold"))
        session.add(WorkspaceEvent(workspace_id=ws.id, kind="template_scaffolded",
                                   actor_id=user.id,
                                   payload={"commit_sha": sha,
                                            "section_count": len(ws.template_sections or [])}))
session.commit()                                           # releases the mutex
```

- **Idempotency guard = `proposal.md` is whitespace-only at HEAD.** This is
  draft-specific and robust: the phase-0 init leaves it empty and writes no
  `ArtifactVersion` row, and any spec edits during setup don't touch it. (We do
  **not** use an "ArtifactVersion rows" check — that table has no artifact
  discriminator, so it can't distinguish draft from spec versions.)
- **Partial-failure retry.** If `session.commit()` fails after the git commit, the
  DB rolls back (phase stays `setup`, job stays `waiting_for_rubric_review`) but
  git holds the scaffold commit. The owner retries activate; the guard now sees a
  non-empty `proposal.md` → it **skips the git scaffold** and re-applies the
  rubric + phase flip + events, then commits. Net effect is idempotent. (A small
  reconciliation gap — a scaffold git commit with no `ArtifactVersion` row after a
  crash — is the same class the existing save path tolerates and reconciles.)
- **Mutex timeout** → surface as the existing busy response (429); owner retries.
- **Output property:** `new_doc` satisfies the `split_sections` join==doc
  invariant and every non-preamble section's first line is a valid `## ` heading
  (so `save_section`'s structure guard accepts later edits).
- Empty template (owner deleted all sections) → scaffold nothing (empty draft).

### 5.4 Editing the template after activation (active-phase home)
`WorkspaceSetup` is gone once active, so the same section-list editor component is
also mounted in the **owner Settings surface** (the existing owner-settings
GET/PATCH area). Editing there updates the snapshot and **re-runs validation only
— it never re-scaffolds or rewrites the draft** (no auto-clobber). Closing a
missing-required gap is done via the §8 insert affordance or normal section
editing. The `PUT` endpoint is the single write path for both setup and active.

## 6. Validation (soft, advisory — never blocks a save)

A pure computed read-model. Inputs: live draft sections (`split_sections`) + the
template snapshot.

- **Matching:** live section ↔ template entry by **normalized heading text** —
  trim, collapse internal whitespace, casefold. Reordering is ignored.
- **Duplicate live headings:** if two live sections normalize to the same heading,
  a `required` entry is satisfied by **any** match; the **budget check applies to
  each matching section independently** (each over-budget section is flagged
  separately).
- **Signal — missing required section:** a `required:true` entry whose normalized
  heading matches no live `##` section. New category `missing_required_section`.
- **Signal — over-budget section:** a live section matching a template entry that
  has a budget, whose **body length** exceeds `length_max` in `length_unit`.
  - **body** = the section text minus its `##` heading line, **with leading/
    trailing whitespace stripped** (an empty scaffolded section counts as **0**).
  - **words** = whitespace-delimited token count of the body.
  - **characters** = `len()` of the stripped body. Counting operates on the stored
    markdown (citation markers count as their source markdown); code fences are
    counted as-is in v1.
- **Unmatched live sections** (a `##` with no template entry) are **not** flagged —
  the template is a floor, not a ceiling.

### 6.1 Surfaces
- **Problems panel:**
  - **`missing_required_section`** — new category. Add to `CATEGORY_LABEL`
    ("Missing required sections") and `CATEGORY_ORDER` (placed first), and to the
    `ProblemRow["category"]` union in the problems API types. Target deep-links to
    the draft / outline.
  - **Over-budget** reuses the existing **`over_length`** category. **Collision
    rule:** for any section that matches a template entry **with a budget**, the
    template budget is **authoritative** and the global heuristic is **suppressed**
    for that section (no double-flag); sections **without** a template budget keep
    the existing global `over_length` check unchanged. The row `detail` carries the
    unit (e.g. `"612 words (budget 500)"`).
- **Live counter:** status-bar readout for the active section when it has a budget,
  e.g. `420 / 500 words`, with an over-budget visual state.
- **Agent-awareness:** expose the template snapshot through the **existing
  workspace/call context the agent already reads** (e.g. `get_call_details`, which
  already returns the rubric) rather than a separate opt-in tool. Read-only; the
  agent never mutates the template.

## 7. API surface (additive)

- `GET /api/workspaces/{id}/template` → `{ sections: [...] }` (members).
- `PUT /api/workspaces/{id}/template` — body `{ sections: [...] }`; **owner only**;
  enforces §4 write-time validation → 400; emits `template_edited`; usable during
  setup and active phase.
- Template-coverage signals are computed inside the existing Problems/health
  read-model (no new endpoint).
- Scaffolding folds into the existing `activate` handler (no new endpoint).
- Regenerate `schema.gen.ts` (`EMBEDDING_MODEL=bge-m3`) after schema/endpoint
  changes; follow the existing envelope/error conventions.

## 8. Frontend

- **Template editor component** (shared): ordered section-list — add / remove /
  drag-reorder (keyed by `id`); per-row heading, guidance, required toggle, unit
  select, max. Mounted in **(a) `WorkspaceSetup`** as a step before "Confirm &
  activate", and **(b) the owner Settings surface** for the active phase. Saves via
  `PUT`. Mantine; `.module.css` scoped under hashed classes; `tsc` is the gate.
- **Cockpit outline:** `required` badge on entries; a **ghost placeholder** for a
  missing required section, with a one-click **"Insert section"** action that
  appends/inserts the `## heading` (empty body) via the normal `save_section` path
  (typo-proof way to satisfy a required section without retyping the heading).
- **Per-section brief:** the guidance text shown as a read-only hint near the
  editor (never written into the draft body).
- **Status bar:** live length counter when the active section has a budget.
- **Problems panel:** render `missing_required_section` (new label/order) and the
  budget-aware `over_length` rows with section deep-links.

## 9. Testing

- **Data model / API:** §4 write-time validation (order contiguity, `id`
  uniqueness, heading control-char + leading-`#` rejection, normalized-heading
  uniqueness, unit/max coupling); owner-gating on `PUT`; default-seed +
  `template_initialized` on create; `template_edited` on `PUT`.
- **Scaffolding:** idempotency via the whitespace-only guard (no clobber when the
  draft is non-empty; re-running activate after a simulated partial failure does
  not double-write); join==doc invariant on output; structure-guard compatibility;
  empty-template → empty draft; `template_scaffolded` event + `system`
  `ArtifactVersion` row written.
- **Validation:** normalized matching (rename / reorder / case / whitespace);
  duplicate-live-heading (required satisfied once, each over-budget section flagged);
  word vs character counting incl. heading-line exclusion and empty-section→0;
  missing-required detection; unmatched section not flagged; **over-length
  collision** (budgeted section suppresses the global heuristic; non-budgeted
  section still uses it).
- **Setup/active flow:** author → activate → draft scaffolded; edit-after-activate
  re-validates without rewriting the draft; insert-missing-section affordance
  satisfies the signal.
- Cockpit `/api` tests live under `tests/proposal_assist/`. Regenerate OpenAPI
  types after schema/endpoint changes.

## 10. Deferred (named to keep v1 bounded)

- **Org-level reusable template library** keyed by funder/call-type + admin CRUD +
  per-workspace instantiation (v1's snapshot is the forward-compatible substrate —
  the library will populate the same column; `id` is already in place).
- **LLM derive-template-from-call/criteria** (pre-fills the same editor).
- **Whole-document length total** (v1 is per-section only).
- **Export / portal-field** budget enforcement (DOCX/per-field).
- **Restructuring the spec** from the template (spec keeps its own skeleton).

## 11. Resolved decisions

- v1 = **scaffold + validate**; org-level library deferred.
- Template source = **owner-authored, default-seeded**; LLM-derive is a follow-up.
- Template drives the **draft only**; spec keeps `SPEC_SKELETON`.
- Guidance lives as **snapshot metadata shown as UI hints**; scaffolded draft is
  **headings-only** (clean deliverable).
- Validation is **soft**, in the **Problems panel** + **live counter** +
  **agent-aware** (via existing context); matching by **normalized heading**;
  budgets **per-section**, unit **per-section choosable: words | characters**.
- Length counts the **section body excluding the heading line, whitespace-stripped**
  (empty section = 0).
- Editing the template after activation **re-validates but does not re-scaffold**.
- **Codex-review outcomes folded in:** active-phase editing home = owner Settings
  (§5.4); scaffolding atomicity = activation-transaction + commit-mutex with a
  `proposal.md` whitespace-only idempotency guard (§5.3); `over_length` collision
  resolved by per-section-budget-suppresses-global (§6.1); heading control-char
  rejection + duplicate-live-heading rule (§4, §6); char counts whitespace-trimmed
  (§6); per-section stable `id` (§4); audit events mirrored (§5); agent context via
  the existing tool (§6.1); a minimal **insert-missing-section** affordance pulled
  into v1 (§8).
