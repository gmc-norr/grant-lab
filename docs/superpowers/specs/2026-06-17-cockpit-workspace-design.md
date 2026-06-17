# Workspace Cockpit (Sub-project 2) — Design Spec

_Status: design spec, ready for implementation planning. Supersedes the working notes
in [2026-06-16-cockpit-workspace-design-notes.md](../notes/2026-06-16-cockpit-workspace-design-notes.md)
and the design-direction in [2026-06-15-collaborative-proposal-workspace-design-direction.md](../notes/2026-06-15-collaborative-proposal-workspace-design-direction.md).
Reconciles the paused phased-workflow brainstorm
([2026-04-18 proposal-assist design](2026-04-18-call-manager-proposal-assist-design.md))._

## 1. Context & scope

Sub-project 2 ports the proposal-assist **workspace** to the React SPA as a rich,
VSCode-inspired **cockpit** where a research team co-writes a funding proposal with a shared
AI agent — every claim traceable to a source and a person. It builds on **sub-project 1**
(SPA foundation + dashboard port), which is done and merged: React + Vite + React Router (Data
Mode) + TanStack Query over the FastAPI **JSON + SSE** API.

**Locked priors (do not re-litigate):** React over the JSON+SSE API; **section-level**
granularity + git / claim→source provenance backbone (no char-CRDT); **AI = propose→accept**;
FastAPI backend reused as-is; no Node SSR; dashboard + profiles left as-is for now; the layout
must be **collaboration-ready** even though multi-user UX (presence, live updates, soft-locks)
is **sub-project 3**.

**Build sequencing (decided this design):** the cockpit ships in two internal phases, with
spine-aware primitives designed from the start so the second phase needs no rework.

- **SP2a — foundation + parity + propose→accept.** The buildable-now core. Specified here in
  implementation depth.
- **SP2b — the spine.** The Spec artifact + the spec↔draft coherence loop + the continuous
  evaluator/Scorecard, layered on the proven SP2a foundation. Specified here at design depth;
  implementation detail defers to its own plan.

The **editor round-trip spike** (§12) is a **pre-plan de-risking gate** that must pass before the
SP2a plan commits to Lexical.

## 2. Conceptual model: Criteria → Spec → Draft

- **Criteria** — the funder's evaluation criteria. **Holistic** (apply to the proposal and the
  spec *as a whole*, not a per-criterion structural skeleton). Read-only, weighted, sourced.
  Sourced from `CallRubric` (`{name, description, weight, scale_max, subcriteria}` + funder
  notes), snapshotted to the workspace. Two jobs: **ambient context** the AI always has, and a
  **continuous yardstick** the AI evaluates the proposal + spec against.
- **Spec** — the team's + AI's evolving **statement of intent** (§8). Central; kept in **two-way
  agreement** with the draft.
- **Draft** — the proposal prose (`proposal.md`).

**The spec↔draft coherence loop (the spine):** editing the spec makes the AI *propose* draft
changes to realize it; adding to the draft something not in the spec makes the AI *propose* a
spec update. Always **proposed, never auto-applied**; a human **accepts and owns** it.

**AI plays three criteria-aware roles:** (1) **ideation partner**, (2) **coherence keeper**
(spec↔draft reconciliation proposals), (3) **continuous evaluator** (scores the whole proposal
+ spec against the criteria; flags gaps).

## 3. Lifecycle: divergent → convergent (reconciles the phased redesign)

The April "phased Spec→Plan→Draft→Review" redesign (four rigid forward-gated phases) is
**reconciled into a soft two-mode flow with two deliberate milestones.** All capabilities the
phased redesign produced are **kept**; only the rigid sequencing/gating UX is retired.

- **Phase 0 (setup)** — unchanged: context load, rubric review, section extraction
  (`section_extracts`), `workspace_setup_jobs`.
- **Divergent (spec unlocked)** — Criteria + Spec + Draft churn together; the coherence loop
  runs both ways; AI mostly **partner**. Absorbs the old Spec + Plan + early-Draft phases.
- **Spec-lock** — the team marks the spec settled. The **one deliberate, attributed milestone**
  that flips the loop: the spec becomes authoritative and frozen; work becomes language/polish
  that doesn't change the spec; drift now means "pull the draft back to the spec." Re-opening
  requires an explicit, attributed **unlock**. Reuses the existing `phase_approvals` mechanic
  for lock/unlock attribution.
- **Convergent (spec locked)** — prose refinement + criteria-gap closing against a stable
  target; AI mostly **evaluator/editor**. Absorbs the old Draft-polish + Review phases.
- **Submission-readiness** — the **second and final** milestone: an advisory, owner-confirmed
  pre-flight check (deadline met · per-section length budgets satisfied · no uncited claims ·
  high-weight criteria covered · required sections present), surfaced via the **Readiness** view.

**Retired:** the four-value phase machine and the separate gated **Plan** phase. **Kept:**
`CallRubric`, the section extractor, `workspace_setup_jobs`, and the ~6-Opus persona review
fan-out (now an on-demand action — §10).

**Schema migration (explicit — do not hand-wave "reuse `phase_approvals`"):**
- `Workspace.current_phase` CHECK constraint (`models.py:591`) changes from
  `{setup, spec, plan, draft, review}` to `{setup, active}`; a data migration maps existing
  `spec|plan|draft|review → active` (`setup` unchanged).
- Add `Workspace.spec_locked_at` (nullable `timestamptz`) + `Workspace.spec_locked_by` (nullable
  FK `users.id`) for the current lock state.
- New table **`spec_lock_events`** `(id, workspace_id, action ∈ {lock, unlock}, actor_id,
  artifact_commit_sha, created_at)` — the deliberate, attributed lock/unlock audit trail.
- The existing **`phase_approvals`** table (`models.py:1062`) still encodes the old
  `{spec, plan, draft, review}` lifecycle and does **not** fit the two-milestone model. **Retire
  it**, or repurpose it *solely* for the **submission-readiness** confirmation
  (`Workspace.submitted_at` + a `submission_approvals` row) — decided in the SP2b/submission plan.
  Do **not** overload `phase_approvals.phase`.

## 4. Agent harness — extend the existing Claude Agent SDK

The drafting agent **already runs on the Claude Agent SDK** (`ClaudeSDKClient` /
`ClaudeAgentOptions`), model **Opus 4.7** (`proposal_assist_llm_model`), an **in-process MCP
server** of 23 `@tool`s (`tools/registry.py`, `create_sdk_mcp_server`), native session resume
(`last_sdk_session_id` → `resume`), token caps (`turns/caps.py`), and the `claude_code`
system-prompt preset. **SP2 extends this harness; it does not build a new one.**

- **Provider & models:** SP2 uses the **configured Claude Agent SDK provider** — the **Claude
  model family only**, routable through **Anthropic direct, AWS Bedrock, or GCP Vertex** (the
  routing target is the per-group governance lever, below). The cockpit's "hard" AI (coherence,
  evaluation, ideation) runs on a frontier Claude (e.g. **Opus 4.8**) as a config knob; local
  **Gemma stays for ranking/embeddings/citation-audit only.** Proposal + reference content
  already egresses to a hosted Claude endpoint today. **Non-Claude providers (e.g. GPT-5.5) are
  out of scope for SP2** — the Claude Agent SDK is Anthropic-specific, so supporting them means a
  second agent harness (a separate adapter/project), not a config change.
- **Governance (Q3, cross-cutting):** enterprise / zero-retention agreements are **delegated to
  individual research groups**, not centrally enforced. The primary lever is the **routing
  target** — the same Claude model reached via Anthropic direct **or** AWS Bedrock **or** GCP
  Vertex — so a group uses whichever cloud it holds a BAA/agreement with, **without** changing
  harness or model family. The code uses one central endpoint today; **per-group credentials +
  routing + agreement tracking** is net-new (a later SP2 plan). A **true second provider**
  (non-Claude, requiring a separate agent adapter) is a **future additive project**, built only
  if a group's only agreement is exclusively non-Claude-routable and that becomes a hard
  requirement.

### 4.1 Propose→accept mechanism (B)

The agent's **mutating tools are removed from the allowlist.** It is given `propose_*` tools
(`propose_edit`, `propose_citation`) that write **`proposed_change` rows** (§5). **Accept** is a
separate human-triggered endpoint that **replays the existing mutation function** as the named
approver. `permission_mode="bypassPermissions"` is dropped. The **read-only tool surface is
untouched** (`read_proposal`, `search_references`, `navigate_reference`, `list_citations`,
`validate_citations`, etc.). This makes "the AI cannot mutate directly" a **structural**
guarantee, not a behavioral one.

### 4.2 Sessions & context (the new orchestration layer)

- **One interactive shared session** — ideation + propose-edit; one turn at a time per
  workspace; attributed thread (already true).
- **Separate concurrent background sessions** — the **evaluator** and **coherence-keeper**;
  **non-mutating with respect to draft/spec text** (they emit `proposed_change` / scorecard /
  Problems rows but never commit to `proposal.md` / `proposal-spec.md`), holistic, debounced
  (on-save + idle, plus explicit "evaluate now"); their output lands in the inbox and panels —
  **never the chat thread.** This honors the locked "one shared thread, one turn at a time"
  while moving non-mutating holistic work off-thread.
- **New infrastructure = orchestration, not storage:** a **context-assembly module** that
  builds role-specific prompts (full criteria + full spec + full draft + cited evidence for the
  holistic roles); **Anthropic prompt-caching** of the stable criteria+spec prefix (cache tokens
  already tracked in `caps.py`); **cited evidence pulled deterministically via the `citations.json`
  ledger** rather than re-searching; and the **Spec as durable externalized memory** (§8),
  reducing reliance on conversation compaction. **No new RAG/datastore** — the existing
  chunk+embed+vector-search suffices at single-proposal scale.

## 5. The `proposed_change` primitive (the propose→accept backbone)

A `proposed_change` is a **deferred, reviewable tool-call**: it captures *which existing
mutation function to run, with what args, against what base.*

**Row shape (new table):**
- `target`: `{artifact: draft|spec, section_index, base_commit_sha, base_section_sha}`
- `payload`: the **whole proposed new section text** (prose), **or** citation args
  (`reference_id, chunk_index, evidence_quote, anchor, stance, confidence`)
- `kind`: `prose_edit | citation_insert | citation_remove | …`
- `status`: `pending | accepted | rejected | superseded | stale`
- provenance: **`origin_kind`** (`interactive_turn | coherence_session | evaluator_session |
  human`), **`origin_session_id` / `origin_job_id`** (the emitting background session/job),
  **`triggering_turn_id` (nullable — set only for `interactive_turn`)**, `triggering_prompt`,
  `created_by`, `resolved_by` (approver), `resolved_at`, `resulting_commit_sha`

**Granularity = (A) per-section full-text.** A selection-scope edit is a section proposal whose
new text differs only in the span; the diff is computed **at render time** vs the live section
(with citation-renumber noise suppressed in the presentation layer).

**Accept = replay the existing mutation function as approver**, under the per-workspace commit
mutex:
- prose → `save_section(new_text, base_commit_sha, base_section_sha)` (`editing/save.py`)
- citation → `insert_citation_impl(reference_id, chunk_index, evidence_quote, anchor, …)`
  (`tools/citations.py`)

**Lifecycle transitions are sha-driven.** `stale` = the targeted section's `base_section_sha`
no longer matches the live section (reuses the existing `StaleSectionError`). When any commit
lands on a section, pending proposals targeting it with a mismatched base auto-flip to `stale`.
`superseded` = the same agent turn re-proposed the same target. **Citations are verified at both
propose-time** (so an unverifiable citation never becomes a pending proposal) **and accept-time**
(authoritative, under the mutex). **Concurrent same-section proposals serialize via staleness**
(the second goes stale once the first is accepted — the correct conservative behavior; never
silently merge two AI rewrites of one section).

## 6. Concurrency — delete the workspace-wide turn-freeze

Because mechanism (B) means the agent **never commits to `proposal.md`** (it only writes
`proposed_change` rows), the original reason for the **workspace-wide `_turn_in_flight` block**
on `save_section` (`editing/save.py:68-83,163-166`) is gone. **Delete it entirely** (do **not**
build the design-review's per-section-lock machinery — (B) made it obsolete).

Safety now rests on two existing mechanisms: the **commit mutex** (`drafting/locking.py:144`,
serializes the actual git writes — user save vs accept-replay) and **`base_section_sha`
staleness** (handles every logical conflict). This is a strictly-better trade: a *precise*
rejection replaces a *coarse* lock, with the same safety (no lost updates).

**Required client behavior** (the cost we accept): a save that 409s on a stale base must
**never drop the user's buffer** → queue+replay or conflict-prompt. **Born-stale proposals**
(a human edits a section the agent was mid-proposing) are surfaced as `stale`, not prevented;
SP3 section soft-locks reduce their frequency later.

## 7. Editor & the propose→accept review surface

- **Markdown-source editing on `CodeMirror-6`, NOT WYSIWYG.** The §12 round-trip spike RAN
  (2026-06-17) and the WYSIWYG path did NOT clear the gate: a Lexical/`@lexical/markdown`
  round-trip canonicalizes bytes on first import — corrupting snake_case headings
  (`## Work_Package_1` → `## Work\_Package\_1`), LaTeX/Windows paths, promoting fake citations
  out of non-backtick code blocks, and **churning 100% of section SHAs** (the byte-exact
  `sha256(section.text)` the propose/accept + staleness backbone keys on). That SHA-churn is
  architectural — it afflicts ProseMirror equally — so we take the spec's own **CodeMirror-6
  source** escape hatch. The editor edits the **raw `proposal.md` bytes**, so section bytes are
  stable **by construction** and the SP2a-1/SP2a-2 staleness backbone is untouched (no write-time
  canonicalization, no staleness re-key, no migration). See
  `docs/superpowers/notes/2026-06-17-editor-round-trip-spike-findings.md`. (Trade-off: rich
  markdown *source* + citation pills, not a Word-like surface — judged the right UX for a
  LaTeX-literate scientific authoring audience, and it keeps the AI's byte-level propose→accept
  diffs honest. Revisit WYSIWYG only if PIs demand it, paying for a byte-faithful serializer +
  staleness re-architecture deliberately.)
- **Server stays authoritative** for markdown render (pandoc preview) + citation numbering; the
  editor shows the source plus a live read-only render, and the server remains the render of record.
- **Citation markers are atomic, non-typeable widgets** (`[@key]<!-- ev:id -->`) rendered as
  **CodeMirror atomic-range decorations** (`Decoration.replace` + `EditorView.atomicRanges`) over
  the underlying marker text — the bytes stay in the document; the widget is only the rendering,
  so there is no node-serialization layer that could corrupt them.

**Editor model:** **section-at-a-time** editing (one section opened from the Outline) **plus a
read-only "Full draft" tab** for continuous reading. This maps 1:1 to per-section
save/budget/diff; with CodeMirror source editing there is no round-trip to contain, but
section-scoping still bounds each save + `base_section_sha` to exactly the edited section. Center
tabs: `§ <section>` (editable) · `Full draft` (read-only) · `Spec` · `Scorecard` · `Readiness` ·
a reference's full text. Preserve caret/scroll across swaps.

**Inline AI edits render as:** **inline track-changes** by default (struck old + highlighted
new, in the flow, one-gesture accept/dismiss); **auto-switch to an anchored diff card** for
large (multi-sentence/paragraph) changes. **Every** prose accept affordance carries the
**citation-impact line** (`✓ [n] still supports` / `⚠ claim drifted — [n] may no longer
support this`) — the `claim_drift` guard, never hidden behind a hover. Accepting a selection-
scope edit **logs the triggering prompt + the accepted edit to the shared thread** (provenance,
not ephemeral). Auto-apply-with-undo is reserved for **mechanical citation operations only**.

**Selection = the universal in-editor affordance:** select text → `{ discuss/rephrase with AI ·
cite from a source · comment }` (a floating toolbar; the "discuss" input is anchored to the
span — ChatGPT-Canvas style). The async gap is handled by a **pending-edit decoration** that
re-anchors the proposal; if the underlying text changed, the proposal flips `stale`.

### 7.1 Citations & provenance (select-to-cite, #6)

Replace the type-the-verbatim-quote flow:

- **Claim-first** (primary): select the claim span in the draft → "cite from a source" → select
  the supporting span in the **source viewer** → the system **derives** `chunk_index` + quote +
  offsets, **fuzzy-matches** against the canonical chunk, and shows a **confirm** chip. Plus a
  **source-first** shortcut ("cite at cursor").
- **Human select-to-cite is a DIRECT commit** (the human is the actor and owns it); **only AI
  citations are proposals.**
- **Quote granularity:** the selected span is the precise `evidence_quote`, with a **chunk-level
  fallback** ("quote optional").
- **Offsets are chunk-local and computed client-side:** the source viewer renders the stored
  `ReferenceChunk.text`; a DOM selection yields offsets **relative to that chunk** —
  `(chunk_index, local_start, local_end)` — then **fuzzy-matched against the stored `chunk.text`**
  to confirm. **No new source-offset index or `ReferenceChunk` migration is required**
  (`ReferenceChunk` keeps `chunk_index`, `section_path`, `text`, embeddings as-is — `models.py:1395`).

### 7.2 Right pane & citation peek (layout #1a)

The **shared assistant is always visible** in the right pane. Clicking a citation **peeks** the
source open as a **center split** (VSCode go-to-definition), beside the claim; a **⇱ pin**
promotes the peeked source into the right pane as a tab for sustained reading.

## 8. The Spec artifact (SP2b)

A **markdown statement-of-intent** with a light conventional skeleton — `## Aims` ·
`## Approach` · `## Novelty & Significance` · `## Expected Impact` · `## Key Claims` — living in
the existing `proposal-spec.md` (today an empty placeholder). It runs on the **same machinery
generalized to be artifact-aware**: today `save_section`, history, and versioning are hardcoded
to `proposal.md` (`PROPOSAL_FILENAME` in `tools/proposal.py:47`; `save.py:134`; `tools/history.py:7`
intentionally excludes sibling artifacts), so an **explicit SP2b precondition** is to parameterize
the save / sectionizer / history / versioning layer over the target artifact (`proposal.md` |
`proposal-spec.md`). Once artifact-aware, the Spec uses the sectionizer (`##` splits, join==doc
invariant), `save_section` + `base_section_sha`, `ArtifactVersion`, and `proposed_change` with
`target=spec`. The skeleton may become template-driven later. `## Key Claims` is the hook for the coherence loop and
`claim_drift` — the claims the draft must keep evidenced.

**Coherence is reasoned holistically** by the background coherence-keeper (whole-spec ↔
whole-draft via the context-assembly module), **not** via a rigid section-to-section map.

(`proposal-plan.md` is **retired** — the Spec absorbs the Plan role.)

## 9. Layout (region map)

```
┌──┬────────────┬─────────────────────────────┬─────────────────┐
│A │ PRIMARY    │  EDITOR (center, stable)    │ SECONDARY       │
│C │ SIDEBAR    │  §section (editable)        │ SIDEBAR (right) │
│T │ (active    │  · Full draft (read-only)   │  • Shared       │
│I │  view)     │  · inline citation widgets  │    assistant    │
│V │            │  · inline track-changes     │    (always on,  │
│I │ Outline    │  · citation peek (center    │    attributed)  │
│T │ References │    split, ⇱ pin to right)   │  • pinned source│
│Y │ Criteria   │  · tabs: Spec/Scorecard/    │    (on pin)     │
│  │ Coverage   │    Readiness/reference text │                 │
│  │ Versions   │                             │                 │
│  │ Members    │                             │                 │
├──┴────────────┴─────────────────────────────┴─────────────────┤
│ PANEL (bottom): Review inbox · Problems (drift, criteria gaps,  │
│   uncited claims, over-length §, broken citations) · Activity   │
├────────────────────────────────────────────────────────────────┤
│ STATUS/TOP BAR: deadline · spec-lock state · §length budget ·   │
│   save state · presence avatars                                 │
└────────────────────────────────────────────────────────────────┘
```

**Divergent → convergent shift (#1c):** **stable geometry** always (honors "stable center,
preserve caret"); the spec-lock state drives **emphasis only** — default rail view
(References/Criteria forward early → Coverage/Readiness late), the lock chip, badge counts, and
the assistant's role label (Partner → Evaluator). At the spec-lock milestone, offer a
**one-time, dismissable "Enter convergent mode"** that re-defaults those emphases forward. No
silent geometry changes.

**Deliberate deviations from VSCode:** document-centric, not file-centric; the bottom panel is
*proposal problems / review inbox*, not a terminal.

## 10. Coverage / Scorecard (SP2b)

A **per-weighted-criterion scorecard — NOT a criterion×section matrix** (criteria are holistic;
this overrides the 2026-06-15 design-review's matrix suggestion). Each funder criterion shows a
**qualitative status band — Strong / Adequate / At-risk / Gap** — plus the specific gap and a
**deep-link to act**, ordered/weighted by the criterion's weight, with an **overall readiness
roll-up**. Optional subcriteria expansion. **Qualitative bands, not numeric scores** (avoids LLM
false precision). Produced by the **background evaluator session** (debounced) — the
"mock-panel made continuous." The ~6-Opus persona fan-out (fit-check + four personas +
consolidation, `review_personas.yaml`) survives as an explicit **"run full mock-panel review
now"** action that writes a deeper one-shot review (`fit-check.md` / `rubric-review.md`).

## 11. Build decomposition

**SP2a (foundation; the §12 editor round-trip spike RAN 2026-06-17 → CodeMirror-6 source, see §7/§12):**
1. Backend: `proposed_change` table + lifecycle; `propose_*` tools; the **accept** endpoint
   (replay-as-approver); remove mutating tools from the allowlist; drop `bypassPermissions`. **[DONE — SP2a-1 + SP2a-2 merged to master 2026-06-17]**
2. **Delete the workspace-wide turn-freeze**; client conflict-handler (never drop buffer). **[turn-freeze deletion DONE in SP2a-1; client conflict-handler is frontend, pending]**
3. **CodeMirror-6 source editor** with atomic citation-marker widgets (range decorations); section-at-a-time + Full-draft tab. (Lexical/WYSIWYG rejected by the §12 spike — see §7.) **← this slice (SP2a-3)**
4. select-to-cite + chunk-offset-mapped source viewer; citation peek + pin.
5. Port the three proven things (per-section edit + ask AI, whole-doc chat, claim→source) — AI
   now lands as proposals.
6. Review inbox + inline diff rendering (track-changes/card + citation-impact); activity/Problems.

**SP2b (spine; on the proven SP2a foundation, primitives already `target`-aware):**
7. Generalize the save / sectionizer / history / versioning layer to be **artifact-aware**
   (`proposal.md` | `proposal-spec.md`), then add the Spec artifact as a first-class tab.
8. Coherence-keeper + continuous evaluator background sessions (context-assembly +
   prompt-caching).
9. Coverage/Scorecard + on-demand mock-panel review; Readiness + submission-readiness check.

## 12. Testing & risks

- **#1 risk — the editor round-trip — RESOLVED (2026-06-17): CodeMirror-6 source.** The pre-plan
  spike (branch `spike/editor-round-trip`; 5-agent adversarial verification) found the Lexical
  WYSIWYG round-trip does **not** hold the `split_sections`/section-SHA invariants: a property
  generator restricted to `[A-Za-z0-9]` masked first-import byte-canonicalization that churns
  every section SHA, plus snake_case-heading/backslash escaping and fake-citation promotion from
  non-backtick code blocks — all cross-checked against the real Python `split_sections`. The
  SHA-churn is architectural (any WYSIWYG-markdown editor canonicalizes on import) and the backbone
  demands byte-exact section bytes, so we adopt **CodeMirror-6 source editing** — it edits the raw
  bytes, so the invariant holds **by construction** with no round-trip layer. Findings + the reusable
  property harness: `docs/superpowers/notes/2026-06-17-editor-round-trip-spike-findings.md`.
  Residual editor test obligations: atomic-widget decorations never alter document bytes; a no-edit
  open→save is a byte-exact no-op (section SHA unchanged); accessibility of the source editor + pills.
- `proposed_change` lifecycle tests (accept/reject/supersede/stale; replay-as-approver;
  double-verify citations).
- Concurrency/staleness tests (no lost updates without the freeze; born-stale handling; client
  buffer never dropped).
- **Accessibility** is a **release gate** (WCAG level to confirm with the user).
- **Open:** Q3 governance (per-group credentials/routing) is specced but unscheduled within SP2.

## 13. Out of scope (sub-project 3)

Multi-user presence, live cross-user updates, section soft-locks, and the live shared-thread
surfacing. The SP2 layout and the `proposed_change` / staleness substrate are
**collaboration-ready** so SP3 layers on without rework.
