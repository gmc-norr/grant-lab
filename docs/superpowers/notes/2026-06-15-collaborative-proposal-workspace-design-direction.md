# Collaborative AI Proposal Workspace — Design Direction

_Brainstorm 2026-06-15. Living doc; consolidates decisions before formal per-sub-project specs. Not yet a spec._

## Goal

Turn the proposal-assist workspace into a **collaborative, AI-assisted cockpit** where a research team co-writes a funding proposal with a shared AI agent, with all relevant info close at hand, and every claim traceable to a source and a person.

## Decomposition (build in order)

1. **Frontend foundation (single SPA)** — choose the framework; stand up the SPA (build tooling, routing, auth against the existing session/cookie, shared design-system port) over a FastAPI **JSON + event-stream API**. Port one simple page (e.g. dashboard) to validate the architecture + patterns.
2. **Workspace cockpit** — the rich surface (sections, references, criteria, coverage, agent chat) at single-user feature parity in the SPA.
3. **Collaborative writing** — multi-user writes, shared attributed agent thread, presence + soft-locks, live updates, propose→accept review surface — layered on the cockpit.
- **Plus (ongoing):** port the remaining simple pages (call feed, admin, login). May stay server-rendered temporarily *during* migration; **end-state is one framework**.

Each gets its own spec → plan → implementation.

## Decisions locked (this brainstorm)

- **Frontend:** **single unified framework** — the WHOLE frontend becomes one SPA over the FastAPI JSON+event API. (Evolved 2026-06-15: parked SPA → hybrid workspace-SPA → **full single-framework SPA**.) Rationale: tool is in active development, **never deployed** → no migration risk; long-term, a principled single stack beats polyglot. **FastAPI backend reused as-is** (exposed as JSON+event API); **no Node SSR layer** (keeps one backend; CSR SPA). New frontend **modeled on the existing pages + design system**; migration phased to the single target.
- **Two independent axes, do not conflate:** (1) client arch (SPA) vs (2) edit granularity. Keep **section-level** granularity + the git/per-section-commit/claim→source provenance backbone regardless. SPA does **not** force char-level CRDT.
- **AI role = propose → accept.** AI read-only work (search, analysis, chat drafting) is free/instant; every AI *mutation* of the proposal is a suggestion a member reviews and accepts. The accepting member owns the change + its provenance. (Google-Docs suggesting mode / PR review pattern.)
- **Collaboration model:** flat membership (every member writes; owner keeps admin: invite/remove, settings, delete, phase/rubric advancement). Section-level concurrency. Presence + **advisory** soft-locks ("Anna is editing Methods", TTL/heartbeat, no full roster). Optimistic concurrency on stale saves → conflict prompt.
- **Agent conversation:** one shared, attributed thread; agent runs one turn at a time per workspace (already true).
- **Evaluation criteria:** criteria come from **one or more authoritative sources** — the funder's official criteria/guidance document(s) become first-class **references** and authoritative inputs to the structured rubric, alongside (and overriding the thin) auto-extraction from call text. Criteria are synthesized/confirmed from the union, and **each criterion tracks which source(s) it came from**. Adds a **coverage checklist** (criteria↔sections, gap-flag by weight) and **section→criterion provenance**.
- **Proposal template:** new **ProposalTemplate** (ordered sections `{heading, guidance, length?, required?}`) that scaffolds `proposal.md` at setup. **Reusable org-level template library** keyed by funder/call-type; a workspace instantiates a **snapshot** (consistent with existing rubric/extract snapshotting). Start options: library template · derive from call/criteria doc · blank.

## What already exists (reuse — don't rebuild)

- **Membership:** `require_member` / `is_member` over `workspace_viewers`; references already member-writable ([_perms.py](../../../src/call_manager/proposal_assist/web/_perms.py)).
- **Multi-writer safety:** per-workspace commit mutex serializes all git mutations ([locking.py:144](../../../src/call_manager/proposal_assist/drafting/locking.py#L144)).
- **Optimistic concurrency:** `save_section` already takes `base_commit_sha`/`base_section_sha` → `StaleSectionError` ([save.py:179](../../../src/call_manager/proposal_assist/editing/save.py#L179)).
- **Attribution:** `ArtifactVersion.created_by`, `WorkspaceEvent.actor_id`, `user:{id}` commits, `TurnJob.submitted_by_user_id` (ChatTurn itself has none — attribute via the TurnJob join).
- **Structured rubric:** `CallRubric` = `[{name, description, weight, scale_max, subcriteria}]` + `funder_notes`, snapshotted to workspace, owner-editable review screen ([rubric_review.py](../../../src/call_manager/proposal_assist/web/rubric_review.py)). Agent reads it via `get_call_details()`.
- **Section structure guards:** `SectionStructureError` protects `##` boundaries; sections identified by index / heading / sha ([save.py](../../../src/call_manager/proposal_assist/editing/save.py), sectionizer).
- **Setup lifecycle:** phases `setup→spec→plan→draft→review`; owner rubric-review gate.

## New backend primitives required

- **Proposed-change (suggestion)** row: AI (and optionally risky human edits) write a pending change; **accept** runs the existing commit + ledger flow with a named approver.
- **Workspace event stream:** generalize the per-turn-job polling SSE ([stream_turn](../../../src/call_manager/proposal_assist/web/workspaces.py#L1028)) into a workspace-wide channel over `workspace_events` (turn started/finished, section saved, lock changed) so members see each other live without reload.
- **`section_locks`** table: `(workspace_id, section_index, user_id, acquired_at, heartbeat_at)` for advisory soft-locks; TTL by heartbeat.
- **Auth flip:** `require_owner` → `require_member` on `post_turn` and section edit/save; keep owner-only admin surfaces.
- **`evaluation_criteria` reference kind** (current kinds: paper/prior_proposal/analysis/data) + derive/confirm structured criteria from **one or more** such references (multiple authoritative sources; per-criterion source provenance).
- **Coverage model:** criterion ↔ section mapping; gap detection by weight.
- **ProposalTemplate** + org-level template library + per-workspace snapshot + `proposal.md` scaffolding from it; optional required-section validation.

## Cockpit (target UI)

```
┌──────────────┬──────────────────────────────────┬───────────────────┐
│ OUTLINE      │  <current section> (editable)     │  Shared assistant │
│  (from tmpl) │   inline citation markers,        │   (attributed     │
│ ─ References │   claim warnings,                 │    turns)         │
│   (+criteria │   AI suggestions as accept/reject │   click citation  │
│    doc)      │                                   │   → source +      │
│ ─ Criteria   │                                   │     evidence here │
│   (weighted) │                                   │  presence avatars │
│ ─ Coverage   │                                   │                   │
└──────────────┴──────────────────────────────────┴───────────────────┘
```

## Research findings (deep-research 2026-06-15 — 26 sources, 23/25 claims verified)

**Validated (our core choices are well-supported by shipped products + HCI canon):**
- **Propose→accept is the dominant shipped pattern** — Notion Suggested Edits (track-changes accept/reject); CHI'26 prototype ("agents never edit the text directly; they post results as comments" + Append/Replace + side-by-side diff + Approve); Liveblocks AI Copilots ("tagged like a teammate", not direct mutation).
- **AI mutates only via typed, server-side tool calls** — ElectricSQL agent-as-Yjs-peer: "doesn't manipulate the doc directly… a runtime translates tool calls into operations"; Liveblocks `defineAiTool` (execute + render + human-in-the-loop Confirmation). We already act through tools — keep them the **only** mutation path; their output now lands as a *proposal*.
- **No character-level CRDT needed at section granularity** — the one deployed CRDT editor used Yjs for human-human sync only and STILL routed AI through proposals; "CRDT auto-merge removes the need for coordination" was REFUTED 0-3.
- **Human-triggered, scoped agent beats autonomous** — CHI'26 N=30 / 14-team / 1-week: users preferred manual triggers; agent-initiated suggestions accepted less than user-initiated ("I hated the idea of it doing stuff when I wasn't there").
- **Don't run concurrent agents on the same region** — Stanford CooperBench: pairing top agents ~halves capability; agent-to-agent chat barely helps; documented clobbering despite a conflict warning. Validates single shared agent + section-level + soft-locks.
- **HCI canon maps 1:1** — Amershi 2019 (G8 easy dismiss, G9 easy correct, G11 expose why) + Horvitz 1999 (mixed-initiative; "do less but do it well"). = our accept/edit/reject + provenance.

**New / refined patterns to fold in:**
- **@mention / comment as the scoped agent entry point** — asking in a comment "constrains the response to one expected location." Let members @-mention the agent on a section/claim.
- **Diff + per-item accept/reject/edit; rationale + provenance on every AI action** (maps to claim→source + "why").
- **Large/long-running agent work → a side branch in the per-workspace git, surfaced as a reviewable proposal set (PR-like), reasoning logged, never auto-merged** (GitHub Copilot agent model). Natural fit for our git-per-workspace.
- **Agent governance:** agent *outputs* are team resources (matches our shared agent); but decide who may change the shared agent's *config/persona/tools* — teams guard agent config as "territory" (recommend owner-only, attributed).
- **Do NOT assume an auto-revise loop** — "agent auto-iterates on PR review comments" was REFUTED 1-2.

## Open decisions for sub-project 1 (not settled by research)

- ~~Realtime transport~~ → **DECIDED: SSE (server→client live updates) + HTTP POST (client actions)**. Fits our low-frequency, server-push event model; reuses existing SSE plumbing; rides the session cookie + CSP cleanly. WebSocket rejected as more than we need (only worth it for char-level live co-editing, which we're not doing).
- ~~JS framework~~ → **DECIDED: React** (Vite + **React Router (Data Mode)** + TanStack Query), client-rendered SPA over FastAPI; no Node SSR. (Chosen for deepest rich-text-editor + realtime ecosystem, AI-assistant fluency, longevity.)
- **Citation-level (vs section-level) accept/reject + source-span binding** — needs its own design pass (G11 provenance).
- **Reconcile** the in-flight phased Spec→Plan→Draft→Review redesign into the cockpit lifecycle UX.

_Caveat: the AI-as-proposer evidence leans heavily on one strong CHI'26 paper (arXiv 2509.11826) + vendor demos (ElectricSQL, Liveblocks); HCI guidelines (Horvitz, Amershi) are durable. Full report: workflow task w4qyrz085._

## Sub-project 2 (cockpit) — design-review findings (2026-06-15, multi-lens workflow)

**Verdict: makes_sense_with_conditions.** The SPA cockpit is the right call (4 planes — structure + evidence + weighted criteria + editable draft — must be co-resident; HTMX can't; keeping section-level git/provenance over char-CRDT de-risks the hard part). But the SPA exposes single-user backend assumptions that break under 3 writers + a shared agent. Four **code-verified preconditions** to treat as spec-level for sub-project 2:

1. **Workspace-wide turn freeze (biggest).** `save_section` blocks on a *workspace-wide* `_turn_in_flight` check ([editing/save.py:163-166,68-83](../../../src/call_manager/proposal_assist/editing/save.py#L163)) and only one turn runs per workspace — so a 60-90s Gemma turn for the PI freezes *everyone's* saves. Fix: scope the write-block to the agent's declared target section(s); read-only turns (search/analysis/coverage/mock-review/@mention) run concurrently with no lock; serialize only same-section mutating turns; never drop the user's buffer on 409 (client queue+replay).
2. **`replace_proposal` is a silent-clobber under concurrency** ([tools/proposal.py:148-177](../../../src/call_manager/proposal_assist/tools/proposal.py#L148)) — whole-file overwrite gated per-document not per-section. Forbid as a direct mutation; route through per-section diffs / PR-like side-branch.
3. **Human citation flow too brittle** — `insert_citation` needs an exact verbatim `evidence_quote`+`chunk_index` ([tools/citations.py:439-446](../../../src/call_manager/proposal_assist/tools/citations.py#L439)). Right for the LLM, punishing for a PI; provenance is the marquee feature → make it select-in-source-viewer → derive offsets, fuzzy match + confirm, quote optional.
4. **The `proposed_change` / diff-accept-reject surface is greenfield + hardest UI** (AI commits directly today). Spec the primitive + lifecycle (pending→accepted|rejected|superseded|stale) and prototype the diff on real markdown (citation-renumber noise suppressed) before building the rest.

Plus SPA conditions: server stays authoritative for markdown render + citation numbering; citation markers are **atomic non-typeable widgets** round-tripping byte-identically (property-test vs `split_sections` join==doc invariant — **the single biggest technical risk**); autosave survives 409/503/stale; accessibility is a procurement gate.

**Top missing features (prioritized):** (high) per-section **length budgets** w/ live counters + agent-awareness (funders desk-reject on limits; `length?` exists in schema but nothing reads it); **DOCX/per-portal-field export** (export is markdown-only — `workspaces.py:1374` defers pandoc; no portal takes markdown); **AI mock-panel review** (read-only, score draft vs weighted rubric — all inputs exist, no tool does it); **coverage promoted** from rail-stub to full-width criteria×sections matrix with gap→action deep-links; **human version-history/diff/restore UI** (git substrate exists, agent-only today); **deadline countdown + reminders + submission-readiness checklist** (`deadline` fields exist, unsurfaced). (medium) bulk reference import beyond the 3/turn cap; per-section ownership/assignment + status + seed-from-prior-proposal; structured **budget table** (system-computed totals + funder-cap checks); figures/tables as first-class objects.

**Key ergonomics:** scope agent lock per-section; **select-to-cite** (not type-the-quote) + `@`/`[[` citation autocomplete; **tier the accept queue** (auto-apply-with-undo for verified citation edits; batch-accept per section; individual review for prose); show **citation-impact (claim_drift)** inside every prose accept diff; typed agent **scope** (section|claim|criterion|doc) not free chat; presence/locks rendered **on the outline**; split AI output into ambient **comments** vs **proposed changes**; give each suggestion an **owner-of-record + the triggering ask + lifecycle**; **tier the SSE stream** (ambient vs actionable inbox) + "since last seen" changelog.

**IA:** stable center (editor never moves, preserve caret across swaps); left rail = single-select navigator (Outline/References/Criteria/Coverage, one at full height + status glyphs); **promote Coverage + a Readiness view to full-width destinations**; add a **Suggestions/review inbox** as a first-class destination; right pane = attributed thread + anchored comments (@mention-in-comment scopes the agent); top bar = deadline countdown + phase + high-weight-gaps chip; phase lifecycle visible and re-weights layout.

**Open questions to decide before sub-project 2:** editor contract (markdown truth + atomic citation widgets — biggest risk; pick a lib with a11y story, Lexical/ProseMirror); save granularity (whole-section vs per-block) under concurrency; turn-lock scope; citation accept grain (sub-section, no full lock?); `proposed_change` storage + staleness; phase gates soft-vs-hard (recommend soft for editing, owner-only + delegable for authoritative acts); accessibility WCAG level as a release gate; agent-config governance (owner-only); token-cap UX at deadline. _Full report: workflow task wtfaz7zuz._

## Next steps

1. Let the deep-research land; fold findings in here.
2. Sub-project 1 brainstorm → spec (SPA foundation, incl. template/criteria/coverage data surfaces).
3. Sub-project 2 spec (collaborative writing) on top.
