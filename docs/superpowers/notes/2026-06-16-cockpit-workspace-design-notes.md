# Workspace Cockpit (Sub-project 2) — Design Notes (IN PROGRESS)

_Brainstorm session 2026-06-16. **Working notes, not yet a spec.** Refines the
[2026-06-15 design-direction note](2026-06-15-collaborative-proposal-workspace-design-direction.md)
for sub-project 2 specifically. Resume point is at the bottom._

## Where this sits

- Continuation of the collaborative-proposal-workspace brainstorm. **Sub-project 1**
  (SPA foundation + dashboard port) is **done & merged**. This session is **sub-project 2:
  the proposal-workspace cockpit, ported to React**.
- **Already locked** (from the 2026-06-15 note, do not re-litigate): React (Vite + React
  Router Data Mode + TanStack Query) over the FastAPI **JSON + SSE** API; **section-level**
  granularity + git / claim→source provenance backbone (no char-CRDT); **AI = propose→accept**;
  FastAPI backend reused as-is; no Node SSR.

## Decisions made this session

1. **Support BOTH single-user and collaborative work.** The layout must be
   *collaboration-ready*, even though multi-user UX (presence, soft-locks, live updates)
   stays in sub-project 3.
2. **Dashboard + profiles are good enough for now** — design energy goes into the workspace.
   (Revisit the dashboard later.)
3. **Preserve the three things that worked today:** per-**section** editing with "ask AI to
   improve," a **whole-proposal chat**, and **claim → reference-source** viewing.
4. The workspace is **VSCode-inspired** (activity rail + sidebars + multiple panes) but lives
   **inside the shared app shell** (same top bar / global nav) so it feels like one product.
5. We **set aside the early "how deep to port" (A/B/C) question** in favour of designing for
   genuine usefulness first; scope depth gets revisited once layout/flow are settled.

## Conceptual core: Criteria → Spec → Draft

- **Criteria** — the funder's evaluation criteria. **Holistic** (apply to the proposal *and*
  the spec *as a whole* — NOT a per-criterion structural skeleton). Read-only, weighted,
  sourced. Two jobs: **(a) ambient context** the AI always has when suggesting/reasoning, and
  **(b) a continuous yardstick** the AI evaluates the whole proposal + spec against.
- **Spec** — the team's + AI's evolving **statement of intent/ideas**. *Central to the whole
  process.* Kept in **two-way agreement** with the draft.
- **Draft** — the proposal prose.

**The spec↔draft coherence loop** (the spine):
- **spec → draft:** edit the spec → AI *proposes* draft changes to realize it.
- **draft → spec:** add to the draft something not in the spec → AI *proposes* a spec update.
- Always **proposed, never auto-applied**; a human **accepts and owns** it (propose→accept).

**AI plays three criteria-aware roles:** (1) **ideation partner** (reasons *with* you about
what to put in spec/draft), (2) **coherence keeper** (spec↔draft reconciliation proposals),
(3) **continuous evaluator** (scores the whole proposal + spec against the criteria; flags gaps).

## propose→accept at three scopes (the AI interaction ladder)

Same approve/dismiss mechanic, three blast radii:

| Scope | Entry point | Best for |
|-------|-------------|----------|
| **Selection** | inline floating input anchored to the highlighted span (ChatGPT-Canvas-style); short scoped discussion; result renders as an inline approve/dismiss diff | line/paragraph polish ("rephrase," "tighten," "soften this claim") — the **workhorse of the convergent phase** |
| **Section** | per-section "ask AI to improve" (spec-aware) | reworking a whole section toward the spec |
| **Whole-doc** | shared chat in the right pane | ideation, structure, cross-section reasoning |

**Selection becomes the universal in-editor affordance:** select text →
{ discuss/rephrase with AI · cite from a source · comment } — folds the Canvas-style edit and
select-to-cite into one gesture.

## Flow: soft divergent → convergent, pivot = spec-lock

Not rigid phase gates — the "phase" is really the **spec's lock state**.

- **Early (divergent):** ideas, spec content, draft text, references/data all churn together;
  the spec↔draft loop runs **both ways**; AI mostly *partner*.
- **Spec-lock (the one real milestone):** team marks the spec "settled." This **flips the
  loop**: spec is now **authoritative and frozen**; work becomes **language/polish that
  doesn't change the spec**; drift now means *"pull the draft back to the spec,"* not "update
  the spec." Re-opening the spec requires an explicit **unlock** (deliberate, attributed).
- **Late (convergent):** prose refinement, citation tightening, criteria-gap closing against
  a stable target; AI mostly *evaluator/editor*.
- **Evaluation cadence:** default **debounced / on-save + idle**, plus an explicit "evaluate
  now" — **not** per-keystroke (the evaluator is the local Gemma model; cost). Can ramp near
  the deadline; make it a setting.

## Layout: VSCode-inspired region map

```
┌──┬────────────┬─────────────────────────────┬─────────────────┐
│A │ PRIMARY    │  EDITOR (center, stable)    │ SECONDARY       │
│C │ SIDEBAR    │  the proposal draft         │ SIDEBAR (right) │
│T │ (active    │   • sections, editable      │  • AI assistant │
│I │  view)     │   • inline citation markers │    whole-doc    │
│V │            │   • per-section "ask AI →"  │    chat,        │
│I │ Outline    │   • inline selection edit   │    attributed   │
│T │ References │     (select → discuss →     │  • Source viewer│
│Y │ Criteria   │      approve/dismiss diff)  │    (opens on a  │
│  │ Coverage   │   • AI edits inline:        │    citation     │
│  │ Versions   │       accept · reject       │    click)       │
│  │ Members    │   • tabs: §section / Spec / │                 │
│  │            │     Scorecard / Readiness / │                 │
│  │            │     a reference's full text │                 │
├──┴────────────┴─────────────────────────────┴─────────────────┤
│ PANEL (bottom): Review inbox (proposals) · Problems (spec↔draft │
│   drift, criteria gaps, uncited claims, over-length §, broken   │
│   citations) · Activity log                                     │
├────────────────────────────────────────────────────────────────┤
│ STATUS/TOP BAR: deadline · phase/lock state · §length budget ·  │
│   save state · presence avatars                                 │
└────────────────────────────────────────────────────────────────┘
```

- **Activity rail (far left):** Outline · References · Criteria · Coverage · Versions ·
  Members. Single-select, full-height.
- **Editor (center, stable):** the draft, per-section editing, inline citation markers,
  inline AI accept/reject suggestions, per-section + per-selection AI. Wide destinations open
  as **tabs**: **Spec**, **Scorecard**, **Readiness**, a reference's full text. Preserve
  caret/scroll across swaps.
- **Secondary sidebar (right):** the shared attributed chat **+** the citation source viewer
  (opens on a citation click).
- **Panel (bottom):** **review inbox** + **"Problems"** (reimagined for proposals) + activity.
- **Status/top bar:** deadline countdown, phase/lock state, section length budget, save, presence.

**Artifacts & homes:**
- **Spec** = its own editor **tab**, sectioned, git-tracked.
- **Criteria** = read-only **rubric view** (weights, subcriteria, source); the funder's
  guidance *document* lives in **References** (kind = criteria/guidance).
- **Coverage** = a continuous **criteria scorecard / readiness view** (per weighted criterion:
  how is the proposal doing right now + the gaps) — **NOT** a criterion×section matrix (criteria
  are holistic). This is the "mock-panel review," made continuous/ambient instead of one-shot.
- **References / data / files** = References + Files views; source text in the right-pane viewer.

**Deliberate deviations from VSCode:** document-centric, not file-centric ("files" = sections
of one proposal + reference sources); the bottom panel is *proposal problems / review inbox*,
not a terminal.

## Open / to resolve (RESUME HERE TOMORROW)

1. **NEXT STEP — visual layout mockups (2–3) in the companion**, to settle:
   - **Right pane** fork: chat + source-viewer as two tabs in one sidebar, **vs** chat on the
     right while a clicked citation "**peeks**" open as a center split (VSCode go-to-definition).
   - **Editor model:** one section at a time (opened from the Outline) **vs** whole-doc scroll;
     how the Spec / Draft / Scorecard tabs sit.
   - Whether the layout **visibly shifts** divergent → convergent (ideation/references forward
     early; editor + problems forward late).
   - The **inline select → discuss → approve** popover placement + how the inline diff renders.
2. **Inline-edit provenance/collab:** log the accepted change + its triggering prompt into the
   shared thread (recommended) vs ephemeral.
3. **Citation widget integrity (hardest technical risk):** atomic, non-typeable citation
   markers must round-trip **byte-identically** through rephrases/edits (property-test vs the
   `split_sections` join==doc invariant).
4. **`proposed_change` primitive + lifecycle** (pending→accepted|rejected|superseded|stale);
   prototype the diff on real markdown with citation-renumber noise suppressed (per design review).
5. **Turn-lock scope** (scope the agent write-block to its target section(s); read-only turns
   run concurrently) — backend precondition flagged by the design review.
6. **select-to-cite** (select in the source viewer → derive offsets/quote, fuzzy match +
   confirm) replacing the type-the-verbatim-quote flow.
7. **Editor library choice** (Lexical vs ProseMirror) — a11y story; markdown-as-truth + atomic
   citation widgets.
8. **Scope depth** (parity + de-risking backbone vs + high-value features) — revisit once
   layout/flow are settled.
9. **Reconcile** the in-flight phased Spec→Plan→Draft→Review redesign (paused mid-Q7) with this
   spec-lock flow model.

See the 2026-06-15 note's **"Sub-project 2 — design-review findings"** for the four
code-verified backend preconditions (workspace-wide turn freeze; `replace_proposal`
silent-clobber; brittle human citation flow; greenfield `proposed_change` surface) and the
prioritized missing-features list — all still in force as spec-level inputs.

## Then (per the brainstorming flow)

Settle layout via mockups → present design sections (approve each) → write the formal spec to
`docs/superpowers/specs/YYYY-MM-DD-cockpit-workspace-design.md` → invoke writing-plans.
