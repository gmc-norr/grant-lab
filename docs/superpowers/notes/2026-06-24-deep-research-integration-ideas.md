# Future work: deep-research capability (deer-flow evaluation + idea capture)

**Status:** concept exploration only — NOT a design, NOT a plan. Captured to seed a
later brainstorm if/when we pursue it.
**Date:** 2026-06-24

## Context / why this note exists

Evaluated whether ByteDance's **deer-flow** (https://github.com/bytedance/deer-flow)
is worth integrating. Conclusion: **don't integrate the codebase; borrow the idea.**

- deer-flow is now a complete rewrite from v1 — a general "super agent harness" on
  **LangGraph + LangChain**, with its own Next.js UI, Postgres/Redis/Nginx, Docker
  Compose deploy, code sandboxes (Docker/K8s), IM channels, skills, memory, MCP. It
  is a *platform that wants to be the app*, tuned for long-context (100k+) hosted
  models. (The famous v1 was the narrower planner→researcher→coder→reporter deep-
  research framework.)
- Poor fit to embed because: (1) framework collision with our **Claude Agent SDK**
  cockpit (propose-only tools, git-backed sections, citations, coherence/evaluator/
  mock-panel); (2) it's a platform not a library, vs. our single FastAPI app +
  in-process APScheduler single-instance constraint; (3) local-LLM mismatch — our
  spine is the on-prem 2080 Ti (Gemma 4 E4B Q4), deer-flow wants big hosted models;
  (4) most of its surface (sandboxes, IM, PPT/podcast, code-exec) is irrelevant or a
  security/supply-chain liability in a hospital-internal tool.

**The valuable part is the capability, not the code.** Build a bespoke deep-research
engine on the stack we already run (Claude Agent SDK + pgvector + citation
substrate) and wire it to multiple consumers as thin adapters.

## The thesis

Every agent feature we have — ranking, coherence-keeper, evaluator/Scorecard,
mock-panel, Problems — operates on data we **already hold** (draft, spec, call text,
profiles). **None reaches outside to bring fresh external evidence in, with
provenance.** That is the single gap deep-research fills — genuinely additive, not
overlapping. The test for every idea below: *does it import outside-world knowledge
we can't get from our own DB, and land it as citations?*

## Idea menu (by pillar)

### (a) Ingestion & source-aggregation
- **Source discovery** — crawl the open web for funders/programmes/calls we don't
  yet poll → candidates for human approval. Attacks the "source coverage pending"
  backlog item. Most strategic, fuzziest success criteria.
- **Call enrichment at ingest** — raw calls are terse (title + deadline + PDF link);
  research-fill the sparse structured fields (budget, eligibility, TRL/scope,
  co-funding rules, historical award sizes, typical success rate). Pays off twice:
  feeds both ranking and the brief.
- **Amendment/deadline monitoring** — re-research a call near deadline for
  extensions, FAQ docs, clarifications. The missing engine under the specced-not-
  planned "B2 live-monitor."
- **Cross-source dedup/canonicalization** — same call across EU portal / Vinnova /
  aggregator → identity resolution.

### (b) Ranking
- **Richer match inputs (unglamorous big win)** — Gemma ranks only as well as the
  call text it sees; enriched calls give embeddings + LLM more to match against
  profiles → better precision, no ranker change.
- **Eligibility / adversarial "why this does NOT fit"** — borrow deep-research's
  adversarial-verify step as a skeptic pass to catch topically-perfect-but-
  ineligible calls (e.g. "requires an SME lead; hospital not eligible"). Real
  failure mode pure relevance can't see. Flag or down-rank.
- **Auto-refreshing research profiles** — research the org's/group's own recent
  publications + grants to keep profiles current instead of hand-maintained.

### (c) Deep-research feature proper (cockpit / proposal-assist)
- **Funder/call background brief** — cited brief feeding the Spec: funder
  priorities, strategic plan, recent awards, scoring-rubric reconstruction,
  evaluator tendencies. Strengthens both Spec and the evaluator's rubric snapshot.
- **Close the citation loop** — Problems already flags *uncited claims*; deep-
  research goes and **finds** the sources → "here are 3 candidate sources for X"
  into the CSL-JSON/pgvector store. Cleanest fit in the project: a gap we already
  detect, met by a capability that fills it.
- **Background/SOTA drafting + prior-art/overlap check** — real-literature
  citations; "is this basically already funded?" to dodge a classic rejection.
- **Ground the mock-panel in reality** — anchor reviewer personas in real
  evaluation reports / ESR feedback patterns for that funder.
- **Competitive landscape / white-space positioning.**

## Where to start (leverage order, if pursued)
1. **Call enrichment at ingest** — quiet, compounding; improves ranking + briefs +
   digest with one capability.
2. **Funder/call background brief** — most visible value, bounded input, reuses
   cockpit + citations.
3. **Close-the-citation-loop** — smallest surface, tightest fit, need already
   detected.

Source discovery (a) is most strategic but fuzziest → sequence after the engine is
proven on 1–2.

## Cross-cutting realities (caveats)
- **This is the one feature that egresses data.** Hospital-internal tool sending
  research queries to the open web + a hosted model must respect Anthropic-only /
  per-group governance. Decide up front what may leave the building.
- **Hosted, not the 2080 Ti.** Needs long context + strong reasoning → existing
  hosted Claude tier. Keep embeddings/dedup/gating local.
- **Cap + cache like remote-rank.** Calls are stable → cache briefs per-call;
  per-day budget cap mirrors the escalation pattern.
- **Everything lands as citations, never free text** — route through the existing
  provenance substrate so the audit story stays intact.

## What's worth borrowing from deer-flow (inspiration, not code)
- The **plan → research → verify → report** decomposition.
- The **human-edits-the-plan gate** before research runs — maps onto our propose-
  only / owner-approval culture.
- The **report/brief export** idea → a 1-page "why this funder, why now" blurb could
  enrich the **weekly email digest**, not just the cockpit.

## Open questions
- One shared research engine with per-consumer adapters, or is the brief the only
  consumer worth building? (Engine-first only pays off if ≥2 consumers land.)
- Governance/egress policy: which research queries are allowed to leave, and does
  per-group governance gate it the way cockpit AI does?
- Search/fetch substrate: hosted search API (Tavily-style) vs. WebSearch/WebFetch
  vs. funder-site-scoped crawling — and how to keep it inside policy.
- Reuse vs. rebuild: how much of the existing evaluator/coherence job scaffolding
  (job tables, scheduler drain/reaper ticks) does a research job clone, like the
  evaluator cloned coherence?
