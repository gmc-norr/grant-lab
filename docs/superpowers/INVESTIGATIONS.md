# Prior investigations — read this before measuring anything

31 documents in `notes/` and `reviews/` record work already done: spikes with verdicts,
multi-agent reviews with verified defect tallies, and outside opinions. They are indexed
here **by the question they answer**, because they are named by date and slice, which is
useless when you are trying to find out whether someone has already looked at something.

This index exists because a session spent an afternoon re-deriving
`2026-06-17-editor-round-trip-spike-findings.md` from scratch, and separately re-derived
the "git is a side effect outside the Postgres transaction" finding that
`2026-08-25-grok-fence-elimination-architecture-review.md` had already recorded.

Completeness is pinned by `tests/architecture/test_investigations_index.py`: every file in
`notes/` and `reviews/` must appear here, and every entry must name a real file.

---

## Storage, the editor, and the git ↔ CRDT boundary

**[notes/2026-06-17-editor-round-trip-spike-findings.md](notes/2026-06-17-editor-round-trip-spike-findings.md)**
*Can a WYSIWYG editor round-trip our markdown without breaking the backbone?*
**GATE NOT CLEARED** for Lexical. Five confirmed breaks; the fifth is architectural:
a rich editor canonicalizes on import, and the propose/accept backbone keys staleness on
byte-exact `sha256(section.text)`. First import→export changed **3/3 section SHAs**.
Explicitly says Break 5 **applies to ANY WYSIWYG editor, including ProseMirror** — which is
what Tiptap is. Recommended CodeMirror-6 source (taken in SP2a-3). The mitigation that
later shipped is `collab/canonical.py`, whose docstring already declares *"the durable Yjs
update log is the canonical committed state"*.

**[notes/2026-06-30-bridge-spike-findings.md](notes/2026-06-30-bridge-spike-findings.md)**
*Can Python read a y-prosemirror document faithfully?*
**GO.** Pure-Python `pycrdt` reads a real `y-prosemirror`-encoded document byte-identically,
16/16 fidelity and 16/16 round-trip. This is why the CRDT→markdown direction is trustworthy
and the markdown→CRDT direction is not.

**[notes/2026-07-03-collab-slice3b1-writepath-findings.md](notes/2026-07-03-collab-slice3b1-writepath-findings.md)**
*Does incremental fragment application survive the likely failure modes?*
Five incremental `apply_pm_section_to_fragment` fixtures added to the Python↔y-prosemirror
write-path harness. Relevant to the delete-then-reinsert duplication.

## The epoch fence — including whether it can be removed

**[reviews/2026-08-25-grok-fence-elimination-architecture-review.md](reviews/2026-08-25-grok-fence-elimination-architecture-review.md)**
*Is there an architecture where a stale writer is not reachable, at lower complexity than
the fence?* **Yes — stop reanimating the name.** The fence exists because restore reuses the
same UUID and the same `<workspaces_dir>/<id>/repo` path. If restore cloned a NEW workspace,
a stale writer could only name a tombstone, plain `soft_deleted_at` would suffice, and the
generation / FenceStamp / Primitive A–B / mint-site scanners could go. Also states plainly
that **git is a side effect outside the Postgres transaction**.

**[reviews/2026-08-25-sol-fence-elimination-architecture-review.md](reviews/2026-08-25-sol-fence-elimination-architecture-review.md)**
The same ask, answered independently at 615 lines: structural alternatives to the
generation fence for disable, restore and purge.

## Whole-project architecture reviews

**[reviews/2026-09-02-current-tree-project-review.md](reviews/2026-09-02-current-tree-project-review.md)**
*What is still true on `7bdc2bb3` after the morning's `fix/review-2026-09-02` merge?*
Independent re-read plus cheap probes. Does not replace the morning review of record.
**Fixed since `8f44a35e`:** `published_at` upsert, empty-blockquote brick, mark widening
on the markdown↔PM path, tracked `.orig` files, pip PYSEC-2026-3721.
**Still High:** stale Y.Doc merge (`doc_epoch` spec only), reference ingest exhaustion,
CJK `Content-Disposition` 500, forgeable ranking fence, register squat, sync work on
the asyncio loop, silent scheduler-lock loss, no git remote. Grade **B**.

**[reviews/2026-09-02-thorough-project-review.md](reviews/2026-09-02-thorough-project-review.md)**
*What does the whole project look like when every gate is mutated, every prior open
finding is re-verified, and the modules no reviewer had opened are read?* Eleven
worktree-isolated reviewers plus independent re-execution of the headline probes.
**16/16 mutations against the core security and fence gates were caught.** New: three
reproduced collab data-corruption paths (stale-doc merge after a branch switch, mark
mis-nesting, empty blockquote bricks the artifact), a `published_at` ingestion regression
from `8a297db1`, four member-reachable resource exhaustions in reference upload/fetch, and
an unattached safety net (no git remote, so CI has never run; README migration step fails
as written; `--workers 2` boots healthy). 43 prior open findings: 8 fixed, 3 partial, 1
refuted (Referrer-Policy comment is correct), 31 open. Appendices carry every reviewer's
full report with probe commands.

**[reviews/2026-09-01-security-and-performance-review.md](reviews/2026-09-01-security-and-performance-review.md)**
*Does the current tree hold up against OWASP Top 10, and where does non-LLM latency
come from (backend queries/scheduler, frontend rendering/polling)?* No High-severity,
currently-exploitable security issue found; auth/CSRF/CSWSH/fencing/SSRF all held up
under direct inspection. Independent verification refuted several performance severities
and recommendations. Confirmed work: Unicode-safe download filenames, then cursor-paginate
`GET /turns`; profile editor and scheduler costs before optimizing them.

**[reviews/2026-09-01-whole-project-review.md](reviews/2026-09-01-whole-project-review.md)**
*What remains after the August 29 remediation wave and the branch-identity work?*
Read-only current-tree review with measured backend/frontend validation. Finds an incomplete
`base_branch` rollout, two remaining branch-switch consistency gaps, blocking work on the
application loop, an unmonitored scheduler lock, a forgeable ranking delimiter, and SSE
revocation drift. Confirms the prior critical CRDT decoder issue is fixed.

**[reviews/2026-08-29-third-pass-expansion.md](reviews/2026-08-29-third-pass-expansion.md)**
**The review of record.** Supersedes the second pass's severity ranking. Start here.

**[reviews/2026-08-29-second-pass-architecture-review.md](reviews/2026-08-29-second-pass-architecture-review.md)**
Superseded by the third pass. Contains a confirmed production race.

**[reviews/2026-08-29-prioritized-architecture-review.md](reviews/2026-08-29-prioritized-architecture-review.md)**
The first pass. Full-project architecture and quality review.

**[reviews/2026-08-20-architecture-solid-review.md](reviews/2026-08-20-architecture-solid-review.md)**
*Does the architecture hold up under a SOLID lens?* **"This is a healthy architecture."**
14 agents, 82 findings, 6 high, 5 verified. Reviewers primed to find violations called
their subsystems among the cleanest they had reviewed.

**[reviews/2026-08-29-backend-latency-budget.md](reviews/2026-08-29-backend-latency-budget.md)**
*Where does backend latency actually go, and would Rust help?* Measured. **No Rust.**

**[notes/2026-06-27-thorough-code-review.md](notes/2026-06-27-thorough-code-review.md)**
76 agents, 19 areas. **176 findings → 132 verified defects** (0 Critical, 10 High,
29 Medium, 70 Low, 23 Info), 6 refuted, 38 product ideas.

## Product and UX

**[reviews/2026-06-11-product-review-backlog.md](reviews/2026-06-11-product-review-backlog.md)**
13-agent whole-product review. Every theme except the one already spec'd.

**[notes/2026-07-07-proposal-workspace-product-review.md](notes/2026-07-07-proposal-workspace-product-review.md)**
*How do teams actually develop proposals here, and what is missing?* Product/workflow
review of the cockpit. The source of the "do not export a git mental model to clinicians"
rule.

**[notes/2026-06-15-collaborative-proposal-workspace-design-direction.md](notes/2026-06-15-collaborative-proposal-workspace-design-direction.md)**
*Should this become a collaborative AI cockpit, and as an SPA?*
**makes_sense_with_conditions** — four planes must be co-resident; HTMX cannot do it.

**[notes/2026-06-16-cockpit-workspace-design-notes.md](notes/2026-06-16-cockpit-workspace-design-notes.md)**
Working notes refining the above for sub-project 2. Has a resume point at the bottom.

## Collab build record — one note per slice, what shipped and what was deferred

Consult these when a collab behaviour looks wrong and you need to know whether it was
deliberate. Each names its branch, its tasks, and its flag status at the time.

- **[notes/2026-06-30-collab-slice1-sync-server-notes.md](notes/2026-06-30-collab-slice1-sync-server-notes.md)** — the sync server
- **[notes/2026-07-01-collab-slice2a-notes.md](notes/2026-07-01-collab-slice2a-notes.md)** — git↔room bridge + checkpoint layer
- **[notes/2026-07-01-collab-slice2b1-notes.md](notes/2026-07-01-collab-slice2b1-notes.md)** — room-aware write kernel; accept goes through the room, not git
- **[notes/2026-07-02-collab-slice2b2-notes.md](notes/2026-07-02-collab-slice2b2-notes.md)** — citation-insert accept + spec-lock through the kernel
- **[notes/2026-07-02-collab-slice2c-notes.md](notes/2026-07-02-collab-slice2c-notes.md)** — mid-session permission sync
- **[notes/2026-07-04-collab-slice3b3-notes.md](notes/2026-07-04-collab-slice3b3-notes.md)** — live handshake probe, **GO**
- **[notes/2026-07-05-collab-e2e-derisk-findings.md](notes/2026-07-05-collab-e2e-derisk-findings.md)** — two-browser e2e, **GO**

## Ranking and matching quality

**[reviews/2026-09-03-matching-method-and-release-split-review.md](reviews/2026-09-03-matching-method-and-release-split-review.md)**
*Does the call↔profile matching method make sense, would a different model be better,
and should matching ship before proposal assist?* The pipeline is a sound five-stage
cascade — but **the golden set is empty** (`golden_set_last.json` is `n_fixture: 0`),
so nearly every tuning question is unanswerable, including the ones `config.py:153`
already defers to it. Remote-model cost is capped at ~$99/month, so the cheap-API
question is not worth a data-residency review; `claude-sonnet-5` is a free win over the
configured `claude-sonnet-4-6`. Carries a SQL query that measures whether the remote
escalation tier earns its keep **without** a golden set. Also records the two-release
split decision (companion spec: `specs/2026-09-03-call-match-release-1-design.md`).

**[notes/2026-09-03-matching-method-second-look.md](notes/2026-09-03-matching-method-second-look.md)**
*Do the matching-method review's findings hold against the code and the live DB, and
should the method change?* **Method stays** (user decision, principles only, no tuning).
Citations hold; two claims wrong: the history file has 1052+ rows, not four — **the test
suite appends to the live `data/golden_set_history.jsonl`** — and a 0.6B reranker is not
"nearly free" (**1036 MiB free with Gemma loaded; Ollama 0.21 has no rerank endpoint**).
F5 run live: 11 escalations, 5 flips ≥15, mostly downward. `claude-sonnet-5` verified safe
for the ranking client. Principle verdict: rubric not Likert (only `topic_fit` is bare),
the system must measure itself, no filter without an audit sample. Blind labelling helper
left in `data/golden_labelling/` (gitignored).

## Implementation maps

**[notes/2026-06-17-sp2a-2-harness-map.md](notes/2026-06-17-sp2a-2-harness-map.md)**
Workflow-generated map of the propose_* tools, allowlist swap and citation accept path.
Partly superseded by the generated [ARCHITECTURE.md](../ARCHITECTURE.md).

## Future work — captured, never started

**[notes/2026-06-14-references-ranking-rag-revamp.md](notes/2026-06-14-references-ranking-rag-revamp.md)**
Agenda only. Seeded by the embed-fragility bug (dense chunks rejected by the embedder).

**[notes/2026-06-24-deep-research-integration-ideas.md](notes/2026-06-24-deep-research-integration-ideas.md)**
Concept exploration: evaluated ByteDance deer-flow. Not a design, not a plan.
