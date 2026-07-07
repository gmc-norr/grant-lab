# Proposal Workspace — Product Review & Feature Direction

**Date:** 2026-07-07
**Scope:** Product/workflow review of the proposal-assist workspace (the cockpit) and its trajectory — *not* a code or security review. How do teams develop proposals collaboratively in this tool, what's missing, does the workflow make sense, and how do we stay powerful **and** simple while delivering high-quality submissions?
**Status of the platform at review time:** full React SPA; collab editing foundation merged through 3b-5d.D1b (flag still OFF; D2 relay-spoof filter → D3 analysis re-fire → D4 THE FLIP remain).

---

## 1. What we've built (the loop as it stands)

A researcher finds a funded call on the dashboard → opens a workspace → **setup** (rubric, spec) → **active**: co-edit a draft (Tiptap/Yjs, about to go live), select text → **Ask / Edit / Cite**, the agent **proposes** changes into an Inbox, humans accept, background analyzers (coherence keeper, evaluator scorecard, uncited-claims, citation audit, mock panel) continuously surface quality signals, everything is versioned in git, and export is gated on approved references.

Three choices are genuinely right and worth defending:

1. **Evidence-first citations** — chunk-level evidence, audit, export gating. Word/Docs/Overleaf do nothing here. For a hospital, defensible claims *are* the product.
2. **Propose-only agent + human accept** — the trust architecture. In this domain it is a feature, not a limitation.
3. **Continuous background review** instead of end-stage review — the *machinery* of quality is built.

## 2. The core tension: an engineer's IDE for people who write documents

Surfaces today: 4 artifact tabs (Draft/Spec/Scorecard/Panel review) + 6 bottom tabs (Inbox/Problems/Activity/Library/Citations/Claims) + chat + outline + versions + members. Individually justified; together they present as a flat cockpit that assumes the user already knows the workflow. A clinician co-writing an EU application at 21:00 does not.

The fix is **not more features — it's re-presenting existing ones as a journey**:

- A **narrative spine**: the workspace always answers *"where are we, and what's next?"* — "Methods has 3 uncited claims · 2 proposals await review · evaluator is stale since your last big edit · 41 days to deadline." Every signal exists; none is composed into guidance.
- `current_phase {setup, active}` is too coarse. Even a soft, non-enforced phase model (**Understand → Specify → Draft → Harden → Submit**) that changes which panels are *prominent* would transform legibility without touching write paths.
- The paused **Spec→Plan→Draft→Review brainstorm** (stopped mid-Q7) was circling exactly this; resume it after the flip as the umbrella for this work.

## 3. Suggestions live in a list; writers think in margins

The Inbox is code review. It works, but it's detached from the text. Writers understand one idiom: *a suggestion appears in the margin, at the spot, and I accept or reject it there.* Proposals already carry `section_index` + anchors; post-flip, Tiptap can render pending ProposedChanges as decorations.

**Inline/margin rendering of proposals is the single highest-leverage UX change on the roadmap.** It also collapses the tab problem: Inbox, Problems, and Claims largely stop being destinations and become annotations where they apply.

Directly adjacent, the most-missed everyday feature: **there are no human↔human comments.** We built presence and cursors (co-*typing*) but not co-*authoring*: margin comments/threads, @mentions, "please review this section," section ownership/status (outline → drafting → needs-evidence → polished → frozen). Grant collaboration is division of labor over sections; the tool has no vocabulary for it. Section status can ride on the existing section-map/provenance infra.

Also: the permission matrix (many owner-only actions, incl. proposal accept) was designed *pre*-collaboration. Once co-editing is live, "a co-I can edit the text directly but can't accept a suggestion in their own section" will feel arbitrary. Re-review the matrix deliberately at D4 time.

## 4. The funder's structure is the real structure — and we don't model it

A proposal today is preamble + freeform H2 sections. Funders *mandate* structure: named sections, page/character limits, required annexes, published evaluation criteria.

1. **The parked ProposalTemplate v1 spec is the most valuable parked item in the backlog.** Scaffold-from-template + validate-against-template turns "a markdown doc" into "an application."
2. **Length budgets are the one evaluator that rejects without reading.** Over the page limit = desk rejection. Per-section character/page budgets with live feedback are simple, legible, and directly protect outcomes.
3. **Rubric fidelity is upstream of everything.** The evaluator scores against hand-typed criteria; if they don't mirror the funder's published criteria (often in a separate "guide for evaluators" document), the whole quality loop optimizes the wrong target.

## 5. The call→workspace bridge is a doorway; it should be a pipeline

The product's compounding advantage over generic writing tools is that *it already knows the call*: text, deadline, programme, ranked fit against the group's profile. The workspace inherits almost none of it.

Bridge features (cheap, high value):
- **Create-workspace-from-call carries call text + deadline in.**
- **Agent drafts the rubric from the funder's criteria** (incl. evaluator-guide documents) for owner approval.
- **Deadline countdown + milestone nudges.**
- **Compliance check** ("required section X missing; limit exceeded in B") as another Problems signal.
- **Call-amendment watch**: the monitor half already polls sources; a workspace should subscribe to *its* call and diff-alert on changes (deadline moved, criteria updated, FAQ published). This is where the two halves of the product multiply instead of adding.

## 6. Close the quality loop: from dashboard to coach

Every analyzer today ends at a *read-only* signal. The revision loop is manual: see problem → find text → select → Edit → phrase the ask. Each step sheds users.

- **Problem → "Fix with AI"**: one click pre-seeds a turn with the problem's context; result arrives as a normal proposal. Trust model fully preserved; friction collapses.
- **Scorecard band → "what would raise this?"** → targeted proposals against that criterion.
- **Mock-panel findings → the same.** Reviews you can't act on become reviews you stop running.
- **Section drafting from spec**: "draft this section from the spec bullet + these references." The blank-page moment is the biggest time sink and is currently unassisted. As a proposal, of course.

Two upstream quality investments to keep honest: the **references RAG revamp** (deferred; retrieval quality bounds citation quality) and **rubric fidelity** (§4–5).

## 7. The last mile: export

Markdown + bibliography export is a scratchpad's ending. Funders want DOCX/PDF in their template. If the final 48 hours happen in Word via copy-paste, provenance, citation integrity, and limit-checking all evaporate at the exact moment they matter most. **Styled DOCX export is the difference between "system of record" and "drafting aid."** Budget/annexes can stay explicitly out of scope — but the narrative document must leave the tool submission-shaped.

## 8. Trajectory: recommended sequencing

Current roadmap after the flip: #2 citations-as-node → #3 structure ops → #4 autonomous agent writes → #5 branching/reconciliation.

**Finish D2→D4 first — no debate.** The flip is load-bearing. Then:

| Tier | What | Why |
|---|---|---|
| **1 — Legibility & trust** | Margin comments (human) · inline proposals in the editor · deadline + narrative spine | Adoption is decided here |
| **2 — The funder's reality** | Unpark ProposalTemplate (scaffold+validate+limits) · rubric-from-call · compliance signal · DOCX export | Directly protects outcomes |
| **3 — Quality loop** | Problem/Scorecard/Panel → one-click fix proposals · section-draft-from-spec · RAG revamp | Turns dashboards into a coach |
| **4 — Power** | Structure ops (#3, needed by templates anyway) · citations-as-node (#2) · cross-workspace reuse · per-section alternatives + compare | Compounding, after 1–3 |

**Deliberately keep parked:**
- **Autonomous agent writes (#4).** Propose-only is the trust story in a hospital. Revisit only for narrow mechanical classes (formatting, reference hygiene) — never narrative content.
- **Full branching/reconciliation (#5).** Writers want "try a different angle for Aims 2" and "restore yesterday" — per-section alternatives + history deliver ~80% without exporting a git mental model to clinicians. Scope-check hard before building.

## 9. Extended feature catalog — helping users *develop* proposals

Beyond the review's tiers: features mined from how hospital teams actually win grants. Grouped by when they help. Each with a leverage/cost read. The unifying test: **does it exploit something only this tool has** (the call corpus, the profiles, the evidence ledger, the provenance) — if generic ChatGPT-in-a-doc could do it equally well, it's low priority.

### 9a. Before drafting — deciding *what* to propose

- **Fit memo** (high leverage / low cost). Agent reads spec vs the call text and flags misalignment early: "the call emphasizes implementation; your spec is discovery-heavy." Kills doomed proposals in week 1 instead of week 8, or redirects them. Reuses the ranking machinery's understanding of fit.
- **Funded-project scan** (high leverage / medium cost, differentiating). SweCRIS and CORDIS are public databases of *funded* projects. Retrieve similar funded projects for this funder/programme; agent summarizes what won, typical budgets, consortium shapes. No generic tool has this; we already do source aggregation.
- **Aims workbench** (medium/medium). The intellectual core of a grant is 2–4 aims with hypotheses and success criteria. A structured aims editor (in Spec) + agent stress-testing ("is aim 2 dependent on aim 1 succeeding? Reviewers penalize that") formalizes the step everyone does badly in freeform text.
- **Team & track-record section from profiles** (medium/low). Funders score the team. Group/researcher profiles already live in this tool — auto-draft the "consortium/team" section from them.

### 9b. While drafting — writing better, faster

- **Figure support** (high/medium). Proposals live and die by Figure 1 (the conceptual overview). Media attachments exist; promote them to first-class draft figures: placement in sections, caption drafting, and an analyzer nudge "this Methods section would benefit from a figure." Even without generating images, managing them well beats every competitor's nothing.
- **Abstract/summary auto-sync** (high/low — nearly free). The abstract must reflect the final draft; it never does. The coherence keeper already compares SPEC↔DRAFT; abstract↔draft is the *same machinery* pointed at a section. "Your summary no longer mentions the primary endpoint" as a Problems signal.
- **Lay summary / Swedish summary assist** (high/low, locally differentiating). Vinnova and Swedish foundations require a *populärvetenskaplig beskrivning*; EU calls want lay abstracts for mixed panels. Agent-drafted, kept in sync like the abstract. Concrete weekly pain for Swedish hospital researchers.
- **Claim–evidence matrix** (medium/low). Claims tab + citations ledger already exist; the synthesis view is a matrix: claims × citations × strength, sortable by "load-bearing but weakly evidenced." This is the evidence story made visible — our differentiator, surfaced.
- **Voice harmonization** (medium/low). The classic multi-author failure: stitched-together tone. An on-demand agent pass "harmonize voice across sections" emitting normal proposals. Uniquely valuable *because* we're collaborative.
- **Reverse outline** (medium/low). Agent maintains a live one-line-per-paragraph "what this paragraph actually does" outline; catches drift and redundancy across co-authors. Fits the existing sidebar outline surface.
- **Mechanical consistency checker** (low/very low). Acronyms defined once, terminology consistency, tense consistency. Deterministic, cheap, always-on Problems signals.

### 9c. Around the team — process and people

- **Internal review round** (high/medium). How hospitals actually work: before submission, a colleague or the grants office reads it. Feature: invite a **comment-only reviewer role** with a deadline; collect structured feedback; agent triages feedback into proposals. Rides on comments (§3) + roles.
- **Submission checklist** (medium/low). One generic checklist feature covers many realities: letters of support to chase, annexes, signatures, ethics approvals. Items with owner + status; deadline-aware nudges. Deliberately *not* a project-management suite — one list.
- **Milestone plan + notifications** (medium/low). Work backwards from deadline (internal review by X, grants office by Y). The weekly-digest infrastructure already exists on the monitor side; a workspace digest ("2 proposals await you; deadline in 12 days; scorecard dropped on Impact") is cheap connective tissue.
- **Ethics/regulatory nudges** (medium/low, hospital-specific). "Your methods mention patient data; no ethics/DPIA section found." Domain-aware compliance signals build exactly the trust a hospital tool needs. (Related in-house context: the variant_interpret IVDR work shows the appetite for regulatory awareness.)

### 9d. The long game — the flywheel

- **Resubmission loop** (high/medium — strategic). Most grants are won on the second attempt. Track outcome (funded/rejected + reviewer comments); import the critique; agent maps critiques to sections and generates a revision plan as proposals. This closes the product's flywheel: calls → proposals → outcomes → better proposals. Nothing else in the market does this well.
- **Boilerplate & DMP library** (high/low). Hospitals write the same data-management plans, facility descriptions, and ethics statements twenty times a year. A small, curated, cross-workspace snippet library (+ DMP generator from a questionnaire) relieves real weekly pain and needs no new deep machinery.
- **Cross-workspace reuse** (medium/medium). "Pull the Aims from last year's application" — workspace-to-workspace import with provenance. Prerequisite for the resubmission loop.
- **Outcome data feeding profiles/ranking** (medium/low, later). "We win implementation-type calls" should eventually inform the ranker. Log outcomes now; use them later.
- **Institutional proposal library** (high value / high governance-care). Opt-in, permissioned library of past successful proposals as RAG reference material. Enormous value; do it only with explicit governance design.

### 9e. Onboarding & simplicity

- **Sample workspace** (high/low). A pre-populated demo workspace (fictional call, half-written draft, pending proposals, a problems list) teaches the whole loop in ten minutes. First-run experience currently: empty IDE.
- **Teaching empty states** (high/very low). "No references yet — upload the 5 papers your aims rest on; I'll index them for citation." Every empty tab is a free tutorial slot.
- **"Explain this screen"** (low/low). The agent is right there; let it answer "what is the Scorecard?" contextually.

### Champion picks from this catalog

If I could add only five beyond the review's Tier 1–3: **① fit memo + rubric-from-call** (quality at the root), **② abstract-sync + lay-summary** (near-free reuse of the coherence keeper, locally differentiating), **③ resubmission loop** (the strategic flywheel), **④ boilerplate/DMP library** (weekly pain, low cost), **⑤ funded-project scan** (a research feature nobody else has).

## 10. North star

> **From the moment a call is ranked to the moment a submission-shaped document leaves the tool, the workspace always knows the funder's rules, always shows the team what to do next, and every AI contribution arrives as an evidence-backed suggestion in the margin.**

Everything above is a projection of that sentence; none of it requires new foundational machinery — rooms, provenance, anchors, analyzers, and the propose-only kernel are precisely what the last months built.

## 11. Open items to verify (asserted from memory, not re-checked)

1. Exactly what call context carries into workspace creation today.
2. The precise owner-only action matrix (esp. proposal accept) — re-review at D4.
3. Current export formats (believed: markdown raw + composed only).
