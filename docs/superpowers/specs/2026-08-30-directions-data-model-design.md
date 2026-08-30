# Directions: the data model

**Status:** decided, unbuilt.
**Supersedes** the data-model half of `2026-08-30-directions-design.md` (ebf9d226).
That document's product framing — Directions replace the never-used git branch layer;
auto-merge is impossible; reconciliation is a human-elected drafting turn — stands
unchanged. Its *shape* (fork the whole workspace) is replaced here.

## 0. How this was decided

A 23-agent workflow (`wf_520d9e00-5ed`): six code readers, four deliberately opposed
designs, twelve adversarial critiques over three lenses (future corners, invariants,
lived use), one synthesis. Scores: **Fork the Document 7.0**, Application/Direction
split 6.7, Rooted self-FK 5.7, three-tier Library 5.7. None scored fatal.

The prior working assumption — split `workspaces` into a project parent and direction
children — **lost**. Every claim below marked ✅ was independently re-verified against
the tree before adoption.

## 1. The shape

**A Direction is a named document-set INSIDE one workspace.** Not a second workspace.

One application keeps: one funding call, one profile, one rubric, one deadline, one
member list, one budget, one git repo, one commit mutex, one `lifecycle_generation`,
one retirement barrier.

### Why this and not the split

1. **Reconciliation is the payload, and only this shape makes it safe.** Blending two
   directions means reading both. Under every split design that is reading a live
   sibling's working tree from another repository with no lock available — their own
   "never hold two mutexes" rule forbids taking the source's mutex. All three rival
   designs were caught doing this independently, by their own reviewers. Here both
   sources are files at one HEAD under one mutex.
2. **Promotion is one UPDATE.** "This direction won, we're submitting it" moves zero
   bytes, zero references, zero members. Under a split it is not a deferred feature,
   it is unbuildable — the corpus, members, rubric, setup job and call live on the
   other row.
3. **Zero new access rules.** The five live "owner OR viewer row" implementations stay
   five. The shared-corpus leak the split had to guard against is not guarded — it is
   unconstructible.
4. **Zero new lock levels.** No second lockable row means the two-key AB-BA deadlock
   every rival must argue away cannot be written.
5. **`2026-05-19` already predicted the split's failure:** *"a branch fork would split
   phase approvals across branches with no defined merge semantics."* Forking never
   leaves the application, so phases never split. ✅ (recovered from git history)

## 2. Entities

### `workspaces` — UI noun **Application**
Table, primary key and 26-table FK surface unchanged. Keeps the git repo, the single
commit mutex, the single fence generation, `current_phase`, setup, rubric, call and
profile snapshots, `owner_id`, `workspace_viewers`, `soft_deleted_at`,
`lifecycle_generation`, the spend-cap overrides.
Gains two columns: `library_id` NOT NULL, `main_direction_id` NOT NULL DEFERRABLE.

### `directions` — UI noun **Direction**
New. Columns: `id`, `workspace_id` (CASCADE), `name`, `created_by` (provenance, never
authority), `created_at`, `archived_at`, `retire_epoch`, `forked_from_direction_id`,
`forked_at_sha`, `spec_locked_at`, `spec_locked_by`.
Constraints: `UNIQUE(workspace_id, id)`, `UNIQUE(workspace_id, name)`.

Owns **11 tables outright** — these DROP `workspace_id`: `yjs_updates`,
`collab_artifact_state`, `artifact_versions`, `chat_turns`, `chat_summaries`,
`proposed_changes`, `user_claim_annotations`, `claim_analysis_jobs`, and the three
per-document caches.

**Five job tables carry BOTH keys** with a composite FK against `UNIQUE(workspace_id,
id)`: `turn_jobs`, `review_runs`, `coherence_jobs`, `evaluator_jobs`,
`uncited_claims_jobs`. Dropping `workspace_id` from these would silently disarm
existing workspace-scoped admission guards. The composite FK makes "this job belongs
to a direction of a different application" unrepresentable in Postgres rather than
remembered by a developer.

On disk: `directions/<direction_id>/{proposal.md, proposal-spec.md, citations.json}`
inside the ONE repo.

### `libraries` — UI noun **Library**
New, thin, call-free: `id`, `name`, `owner_id`, `profile_id` (nullable), `created_at`,
`soft_deleted_at`. Owns `references`, `reference_chunks`, `attachments`,
`attachment_jobs` (`workspace_id` renamed to `library_id`).

**Created 1:1 with each application. Nothing changes semantically on day one.**
It has NO membership table, NO roles, NO mutex, NO generation. It is an ownership row,
not an access tier — `library_id` is resolved from the workspace the request is already
authorized against, so access stays DERIVED. That is what keeps the ~19 renamed IDOR
guards genuinely sufficient rather than ownership comparisons every member passes.

### Deleted, not classified
- **`phase_approvals`** — 0 rows, zero production readers, zero production writers, and
  a CHECK still naming a vocabulary migration 0052 removed. ✅ (`db/models.py:1356`;
  only test references exist.) Classifying a dead table is a category error.
- `workspaces.current_branch` and the ~2,070-line branch layer.
- The empty `proposal-plan.md` written into every repo but absent from the Artifact enum.

## 3. The five decisions

| # | Question | Answer |
|---|---|---|
| **D1** | May a direction be private from other members? | **No.** One owner, one member list; every direction is visible and editable by every member. |
| **D2** | Will material be reused across calls? | **Yes** — split `libraries` now, 1:1. |
| **D3** | May a non-owner member drive the drafting agent? | **Yes** — any member may submit drafting turns in any direction. |
| **D4** | How many directions per application? | **A setting, defaulting to 3.** Per-direction job slot yes; per-direction budget reservation no. |
| **D5** | What does "Main" mean? | **The document you intend to submit.** Promotion is one UPDATE. |

**D1 is the gate.** If it ever flips to Yes, this whole shape flips back to the
two-table split: directions owned separately + one shared corpus is a live
authorization hole, not a guarded one. It is not recoverable additively.

**D2's argument is timing, not value.** Splitting 1:1 today cannot produce a single
citekey collision. Merging two libraries later MUST resolve collisions by re-keying —
and a citekey is literal text (`[@smith2021]<!-- ev:… -->`) committed inside
`proposal.md` and mirrored in `citations.json`. A re-key rewrites prose already in git
history in every direction of every application. The `2026-05-25` references spec
answered this question "no shared library", specced
`POST /workspaces/<dst>/references/copy_from`, and **never built it** ✅ — so today a
group's second application has no path except re-uploading and re-embedding every PDF.
`Reference.kind` already allows `prior_proposal`; `Attachment.kind` already allows
`past_grant` and `preliminary_data` ✅ — the schema already names standing-library
material that cannot move anywhere.

**D3's evidence:** `turns.py:105-111` ✅ — *"A member can ONLY submit a selection ask —
modify and free-chat stay owner-only."* That is very likely why `workspace_viewers` has
never had a row. Note there is **no owner-transfer route anywhere in the codebase**, so
concentrating capability on one `owner_id` concentrates the risk of a departure.

**D4 was half-forced by the code.** Both enqueue helpers are *"create a queued job, or
return the existing active one"*, pre-read scoped by `workspace_id` alone ✅
(`evaluator/jobs.py:27-29`, `coherence/jobs.py:30`). Unchanged, direction B clicks
Evaluate and receives direction A's job: 200 OK, no error, a scorecard for the wrong
document. The base design's justification for serialising — "honest against one RTX
2080 Ti" — is **false**: review, coherence and evaluator all build a Claude SDK adapter
✅, i.e. hosted. The GPU serves ranking, embeddings and the citation audit. The real
constraint is money, and money is a budget, not a slot.

**The number is a SETTING, not a constant.** It follows the pattern this repo already
uses three times ✅ — a global default in `Settings` plus a nullable per-workspace
override column (`daily_token_cap_input` / `workspaces.daily_token_cap_in_override`;
same for output; `review_dollar_cap_default` / `review_dollar_cap_override`). So:

- `Settings.max_directions_per_application: int = 3`
- `workspaces.max_directions_override: int | None` (nullable, admin-settable)

Two reasons this matters beyond convenience. First, 3 is a guess about human behaviour on
a pre-launch instance, and the honest way to ship a guess is as a value someone can change
without a deploy. Second — and this is the architectural point — **the justification for
the number is not durable.** Today the constraint is hosted-model money. Cheap open-weight
models are improving fast, and this repo already runs Gemma 4 E4B and BGE-M3 locally on an
RTX 2080 Ti. CLAUDE.md states the ranking and extraction interfaces are *"deliberately
prompt-in/JSON-out and model-agnostic, so swapping a model is a config change, not a
rewrite"* — and `DRAFTING_ADAPTER` is already a setting. If review, coherence and evaluator
move to a local model, cost stops being the constraint and GPU concurrency becomes it
instead. The limit survives that swap **only if it is a knob whose reason can change while
its shape does not**. Nothing about the schema may encode "hosted models are expensive".

## 4. Where things live

### Access control — unchanged
Zero new rules, zero new predicates, zero new tables. `workspace_viewers` stays
`(workspace_id, user_id)`. D5-from-the-old-spec ("every corpus read re-checks live
access to the root") is not implemented more cheaply — it is **unnecessary**, because
there is no cross-application corpus access to check.

One addition, a resolver rather than a rule:
`require_direction(*, workspace_id, direction_id, user, session)` calls `require_member`,
then asserts the direction belongs to that workspace and is not archived. Downstream
every query scopes by `direction_id` alone.

Two defects to fix on the way, both found by review:
- The collab WebSocket handshake does NOT route through `web/_perms` — it has its own
  `resolve_access`. "This direction belongs to this workspace" must not become a sixth
  hand-written scope rule there; it becomes part of the fence predicate (§5).
- `RoomKey` is today a bare `tuple[uuid.UUID, str]` alias built as hand-written literals
  in five places ✅ (`collab/room.py:32`). Make it a **NamedTuple BEFORE widening it** —
  `refresh_permissions` fails silently on a key miss, so one missed literal turns
  permission revocation into a permanent no-op with no log line.

### Spec and phase lifecycle
- **`spec_lock_events` → DIRECTION-level**, with `spec_locked_at` / `spec_locked_by`
  moving onto `directions`. The lock IS the coherence keeper's divergent/convergent mode
  switch: unlocked, `propose_spec_edit` is in the allowlist; locked, that tool is
  structurally unreachable. "An unlocked spec may change the intent" is already this
  codebase's own definition of a direction. Per-direction locking lets one direction
  converge under a frozen spec while its sibling drifts — two modes already built and
  never once used. Lock and unlock start writing an event, which today they do not.
- **`current_phase` and setup → APPLICATION-level, unchanged.** One writer, advances
  once, never reverses, gated on a human approving the FUNDER's criteria; everything
  Phase 0 does is call-keyed and already call-cached. So `WorkspaceSetupJob`'s
  `UNIQUE(workspace_id)` stays true, a fork never re-enters setup, and never demands a
  second rubric review.
- Free consequence: one `rubric_updated_at` on one row means amending the funder criteria
  marks **every** direction's scorecard stale at once, and two scorecards are guaranteed
  comparable because they were provably scored against the same criteria.

### Spend
Budgets stay on the workspace. The family IS the workspace, so forking five directions
multiplies nobody's budget and the old §9 "family spend" question has no graph to walk.

Active-uniqueness moves to `(workspace_id, direction_id)` for all six job families —
preserving today's semantics exactly, one active job per family per DOCUMENT. Per D4 no
per-direction budget *reservation* is built; the existing global daily ceiling stands, and
the direction count is bounded by `max_directions_per_application` (default 3, per-application
override). Enforced at direction-create, returning a message that names the limit and where
to raise it — never a bare constraint violation, because an index cannot explain itself.

The reservation is the piece deliberately left unbuilt, and it is safe to leave unbuilt
because it is purely additive: it needs no migration, no re-keying, and no change to any
decision above. If real use shows groups keeping six directions — or if the passes move to
a local model and the binding constraint becomes GPU slots rather than dollars — the same
setting is re-justified and a reservation is layered on top.

`llm_spend` and `workspace_events` keep `workspace_id` and gain a **nullable**
`direction_id` — attribution only. A new AST gate, modelled function-for-function on the
existing `spend_guard` (FUNCTION-level, PINNED dict with mandatory reasons, list may only
shrink), fails CI if a `direction_id` predicate appears in any query reading a cap column
or aggregating a token or dollar column. "A direction cannot buy itself a budget" becomes
mechanically checkable.

### Fencing — one generation, one mutex, one lock order
`lifecycle_generation` stays a plain integer on the `workspaces` row. `live_at_generation`,
Primitive B's under-mutex re-read, the commit-mutex key, the shared lease, the normative
M→J→W order and all seven `claimed_at_generation` stamp sites are **untouched**. The
claim/stamp layer is level-agnostic, so re-keying the job tables costs the fence one extra
join. There is no second lockable level, so the AB-BA deadlock is unconstructible.

One real correction. `live_at_generation` reads exactly three columns, all on the
`workspaces` row ✅ (`fencing.py:108-112`: `id`, `soft_deleted_at`,
`lifecycle_generation`). It **structurally cannot express** `directions.archived_at IS
NULL`, so a direction-scoped effect would pass the fence while targeting a retired
document. Fix:

- **`live_direction(workspace_id, direction_id, generation)`** beside it — an `exists()`
  joining `directions` to `workspaces`, asserting the direction belongs to this workspace,
  is not archived, the workspace is not soft-deleted, and the generation matches. Required
  in the SAME statement as every direction-scoped effect, pinned by the existing
  `tests/fencing` AST machinery. This also closes the WebSocket's unvalidated
  `direction_id`.
- **`directions.retire_epoch`** — an integer bumped by archive and stamped at the seven
  claim sites alongside the workspace generation. An epoch COLUMN is not a mutex: fenced
  per-direction retirement with no new lock ordering.

`checkpoint_artifact` LOSES its redundant `workspace_id` parameter and derives repo and
document path from the stamp, so the two-id disagreement the old §9 asked us to *assert*
becomes unrepresentable.

**Blocker to close first:** `git commit -q -m message` carries **no pathspec** ✅
(`citations/gitio.py:198`), so it commits the whole index. With N directions in one index,
bytes staged by one writer ride into another direction's commit and become its
`resulting_commit_sha`. The commit must become pathspec-scoped.

### Deletion
- **Archive a direction** (the normal end): set `archived_at`, bump `retire_epoch`, refuse
  new enqueues, tear down its rooms. Destroys zero bytes — the freeze-only-delete shape
  already merged. Because `retire_epoch` is inside the fence predicate, a late write from
  an in-flight pass is REFUSED rather than landing somewhere harmless.
- **Delete the application:** unchanged. One soft delete, one generation bump, one job
  sweep, ONE 6.0s retirement barrier — so retirement's "the budget is the MAX, not the
  sum" stays literally true, which every fork-the-workspace shape would have falsified.
  Correction required: the room-retirement hook is a hardcoded two-element (draft, spec)
  product and must learn to enumerate the application's directions.
- **Delete a reference:** the tombstone-vs-hard-delete question becomes answerable
  TOTALLY — a glob over every direction's `citations.json` at one HEAD under the one
  commit mutex. It also finally takes that mutex at that generation, where today reference
  deletion takes no mutex, no lease and no generation at all. Deliberately NOT adopting
  tombstone-always-plus-reaper: deferring byte destruction to a job that fails closed
  turns "delete this patient-derived file" into "we tombstoned it" — a compliance hole in
  a hospital tool.
- Cascades: `directions` CASCADE from `workspaces`; the 11 direction-only tables CASCADE
  from `directions`; the two nullable `direction_id` attribution columns SET NULL so money
  and audit survive.

### Reconciliation — two verbs, neither is a merge
An automatic merge here is silent corruption: `apply_pm_section_to_fragment` is
delete-then-reinsert, so two directions editing one section converge on a duplicated
section.

- **"Make this the main direction"** — the common case, one UPDATE, never touches the CRDT.
- **"Draft a blend from…"** — a drafting turn with two sources ("Main's framing of the
  clinical need, this direction's methods") producing ONE `ProposedChange` the human reads
  as a diff. Both sources are files at one HEAD, one membership check, one commit mutex.
  Citation markers survive verbatim because both directions cite from one keyspace and one
  ledger namespace — no evidence-row migration.

Two requirements that must be BUILT, not assumed:
- **Fork must checkpoint the source room first.** Git HEAD lags the Yjs log until a
  checkpoint fires, so a naive fork copies a draft that can be a whole working session
  behind what is on screen, with no history in the child to notice the loss by. Trivial
  here — same mutex.
- **Fork must carry the conversation.** `chat_turns`, `chat_summaries` and
  `last_sdk_session_id` are direction-scoped. Copy the parent's `chat_summaries` into the
  child and let the turn-context assembler read the parent's turns up to `forked_at`;
  leave `last_sdk_session_id` NULL. Otherwise the researcher's first experience of the
  feature is an assistant that has forgotten the project.

Free here and nowhere else: the fork is a real commit in one history, so
`git log --follow -- directions/<id>/proposal.md` traverses back through it. The version
graph can finally draw the fork point.

## 5. Naming

**User-facing:** Application, Direction, Main, Library. Verbs: "New direction from Main…",
"Make this the main direction", "Draft a blend from…", "Archive direction".
**Never** branch, fork, merge, root, trunk — the 2026-07-07 product review said not to
export a git mental model to clinicians, and an automatic merge here is a silent
corruption, so the word must not appear in the UI.

*Application, not Proposal*: `proposal.md`, `proposal-spec.md`, `ProposedChange`,
`proposal_assist` and `PROPOSAL_ASSIST_LLM_MODEL` already own that word, and `proposal_id`
beside `proposed_change_id` in one signature is a defect waiting to happen.

*Project stays unspent*: in a hospital "project" means the funded research project. The
genuinely call-spanning future needs that word, and `workspaces.project_id` remains
available as a pure addition later.

**Code-facing:** `Workspace` / `workspaces` / `workspace_id` are **NOT renamed**. The
ambiguity this exercise existed to remove is already gone once the split lands —
`workspace_id` unambiguously means the application, `direction_id` the attempt. A mass
rename across 26 tables, 75 route decorators and every `tests/fencing` pin buys zero
invariants and costs a review round in the fence area. The UI↔code mapping gets one line
in CLAUDE.md.

New: `Direction`, `directions`, `direction_id`, `Library`, `libraries`, `library_id`,
`DocumentRef(workspace_id, direction_id, artifact)` in `ports/document.py`,
`direction_doc_dir()`, `require_direction()`, events `direction_created` /
`direction_renamed` / `direction_promoted` / `direction_archived`. **UI "Main" maps
exactly to `main_direction_id`** — this repo's recorded failure mode is a name that lies
about behaviour.

## 6. `main_direction_id`

`workspaces.main_direction_id` → `directions(id)` ON DELETE RESTRICT, **NOT NULL,
DEFERRABLE INITIALLY DEFERRED**, declared with `use_alter=True` — the pattern already used
to break the users↔profiles cycle. Nullable-in-DDL gives only "at most one Main"; the
deferred NOT NULL gives "exactly one", which is what makes the bare application URL, the
list row and the export target always resolvable. Set inside the same transaction that
creates the workspace and its first direction, named "Main".

Two guards, both required:
- Promotion **must** write a rendered Activity event. That means widening `_RENDER`,
  which is a closed dict that silently drops unmapped kinds ✅
  (`activity/feed.py:172-175`: `# unknown/unmapped kind -> drop (fail-safe)`).
- The chrome **must** carry an ambient "this is not the main direction" marker. A mutable
  pointer that decides which document gets submitted, with no visible mode, is the classic
  modal-UI hazard placed on the highest-stakes decision in the product.

## 7. What the user sees

**Navigation.** The rail's top band renames Workspaces → **Applications**. The
per-application band gains a sixth entry, **Directions**. Each of the six gets a visible
scope marker: Library, References, Criteria and Members are labelled *shared across
directions*; Outline, Versions, Problems, Claims and Inbox belong to *the direction you
are in*. Without those labels a user must know which tables carry `direction_id` to
predict what a click does — and the Library delete confirm currently says only "Delete
this reference?" while destroying it for every direction.

**URL — a correction to the old spec, not a detail.** The direction is a **path segment**:
`/applications/:workspaceId/d/:directionId`, with the bare `/applications/:workspaceId`
resolving to Main. The old design made it a query param and advertised the consequence
("the cockpit does not remount") as the feature. That is fatal ✅: the unsaved-changes
guard blocks only when `currentLocation.pathname !== nextLocation.pathname`
(`UnsavedChangesGuard.tsx:48`), so a query-param switch never fires it — two paragraphs
typed into Aims vanish with no prompt. The remount key is derived from the path, with a
comment stating that a param-only nav "would otherwise reconcile the same
Cockpit/CockpitChat instances, leaking A's local state into B". Between two documents that
are byte-identical the day they are forked, the user cannot see that leak. Cost of the
fix: the pane layout no longer survives a switch.

**Breadcrumb.** Two segments on Main ("Innovativa Startups 2026"), three off it
("Innovativa Startups 2026 › Health-economics framing"), with a `Main ▾` switcher. A
researcher who never forks never encounters the word "direction".

**The list.** ONE row per application — *"Vinnova — AI triage of prostate biopsies ·
DRAFTING · Innovativa Startups 2026 · due 2026-10-15 · 2 directions · Updated
2026-09-06"* — not N near-identical siblings each printing the call title twice and each
independently settable to "Awarded". That already happened by hand: two live workspaces
titled "Pathology AI rapid-response draft" created the same day.

**Export.** Exports the direction in the URL; the filename carries both names
("Innovativa Startups 2026 — Health-economics framing.md"). The old design claimed to have
killed the filename collision and had in fact preserved it, since `title` is a single
application column.

**Words in the corridor.** *"I've got two directions on the Vinnova application — the
health-economics one scored better so I made it the main one, and I pulled the old opening
back into it."* Every noun there is a row. None is a repository, a branch, a root or a
parent.

## 8. Gates this design ships with

- Import gate (armed F3c idiom): modules under `references/` and `attachments/` may not
  name `Direction` or `direction_id`. The corpus must not learn which draft is reading it.
- AST gate: no `direction_id` predicate in any query reading a cap column or aggregating
  tokens/dollars.
- Fence gate: every direction-scoped effect carries `live_direction()` in its own
  statement, pinned by the existing `tests/fencing` machinery.
- The 15 cross-scope corpus tests are **re-authored** from "another workspace" to "another
  library", not deleted — they still pin a real boundary.

Every gate above must be proved by mutation, not by assertion.

## 9. What would flip this

- **The whole shape:** if a postdoc genuinely needs a direction the PI cannot see (D1 → Yes),
  the two-table split wins instead and we pay for the second lock level, the cross-repo
  blend and the new deadlock surface — because the alternative is a live authorization hole.
- **The library graft (D2):** nobody has timed a full corpus re-ingest. If a 549-chunk
  ingest turns out to take under a minute, the library row buys less than priced and
  keeping the corpus on the application is defensible for another year.
- **D4:** if real groups keep six directions rather than two, one exploratory direction can
  starve the draft being submitted and the shared ceiling needs a per-direction reservation.
  This one is *designed* to flip: the count is a setting, so flipping it is an operator
  action, and the reservation is additive.
- **The cost premise underneath D4:** review, coherence and evaluator are hosted **today**.
  If cheap open-weight models close the gap and those passes move to the local GPU, the
  constraint changes from dollars to GPU slots. No part of this design should have to
  change for that — which is why the limit is a setting and why `llm_spend` carries
  `direction_id` as attribution only. If any of §8's gates ever has to be relaxed to
  accommodate a model swap, that gate was encoding a pricing assumption and is wrong.

## 10. Designated exit (not now)

Move the citation evidence ledger from per-repo `citations.json` into a corpus-scoped
table. Under one repo per application the ledger is already atomic, so this buys nothing
today. It is the piece that lets one library serve many applications later without a
cross-repo scan. Written down so a future reader does not rediscover it as a blocker.

---

## 11. Errata — outside review, round 1 (Sol / gpt-5.6-sol, 2026-08-30)

**Status change: this spec is BLOCKED, not ready to plan.** Ten findings, zero refuted.
All verified against the tree before being recorded here.

### F1 [CRITICAL] — the load-bearing claim in §1 is FALSE as written

§1 reason 1 said a cross-workspace blend is unsafe *by construction* because the protocol
"forbids taking the source's mutex". It does not. `fencing.py:30-31, 195-201` forbids
**upgrading** — holding the shared lease then taking the exclusive mutex **for the same
workspace** — and forbids holding the mutex across context assembly or an LLM call. It says
nothing about two mutexes on two different workspaces. And the reviewer's constructive
alternative works: checkpoint the source under its mutex, release it, then blend from that
immutable commit SHA while holding only the target's mutex. Never two at once.

**Correction:** the honest claim is "materially simpler", NOT "possible vs impossible".
A cross-workspace blend is constructible; it needs a checkpoint-release-capture-SHA dance,
tolerates a stale source, and adds a failure mode (the source repo retired between capture
and read) that the single-repo blend does not have.

Reasons 2-5 of §1 are untouched and still stand. **Reason 2 (promotion is one UPDATE) is
now the strongest surviving argument** and should lead. The reviewer's proposed remedy —
"replace with the two-table split" — does not engage reasons 2-5 and is not adopted on the
strength of F1 alone.

### F2-F10 — defects in this document

| # | Sev | Defect |
|---|---|---|
| F2 | HIGH | `main_direction_id` → `directions(id)` is a plain FK; workspace A may point at a direction of workspace B. Needs a composite FK `(workspaces.id, main_direction_id)` → `directions(workspace_id, id)`. |
| F3 | HIGH | Nothing prevents archiving Main. ON DELETE RESTRICT covers deletion, not an `archived_at` UPDATE, so §6's "always resolvable" guarantee breaks and `require_direction` then rejects Main. Refuse to archive Main, or require atomic promotion first. |
| F4 | HIGH | `live_direction()` is an SQL predicate and cannot be embedded in a git or filesystem effect. §8's "every direction-scoped effect carries it in its own statement" cannot cover the non-DB effects. Needs an under-mutex direction re-read primitive, pinned separately. |
| F5 | HIGH | §4 says "`retire_epoch` is inside the fence predicate" but the signature given is `live_direction(workspace_id, direction_id, generation)` — no epoch parameter. Internal contradiction. |
| F6 | HIGH | `WorkspaceSetupJob` is one of the seven `claimed_at_generation` stamp sites but is deliberately kept application-level with no `direction_id`, so it has no direction epoch to stamp. Stamp only the six direction-scoped families. |
| F7 | HIGH | Fork is required to checkpoint first; **blend is not**. Git HEAD lags the Yjs log, so a blend can draft from stale source text — the exact data loss §4 identifies for fork. Both blend sources must be checkpointed before their SHAs are captured. |
| F8 | HIGH | §4 references a `forked_at` cutoff that §2 never defines (only `forked_at_sha`), and §2 omits `last_sdk_session_id` from `directions` while §4 prescribes its reset. |
| F9 | MED | "Active-uniqueness moves to `(workspace_id, direction_id)` for all six families — preserving today's semantics exactly" is **false**. Verified: `uq_turn_jobs_running` is partial on `status='running'` only, so multiple QUEUED are already allowed; `uq_claim_analysis_jobs_ws_sha_queued` is `(workspace_id, section_sha)` where `status='queued'`. Each family has its own predicate; `direction_id` must be appended to each, not imposed uniformly. |
| F10 | MED | §2 claims `directions` owns 11 tables and names 8 plus "the three per-document caches". Name all eleven with their replacement FK and uniqueness key. |

### The sequencing consequence — this spec now waits

F9 is not merely a wording defect. It shows the five pass families each carry a *different*
active-uniqueness predicate, so Directions is five bespoke migrations rather than one.
Measured underneath it:

- 7 workspace pass tables share `id`, `workspace_id`, `status`, `claimed_at_generation`
  (**all 7**), plus `completed_at` 6/7, `queued_at` 6/7, `started_at` 5/7, and
  `error` / `input_tokens` / `output_tokens` 4/7.
- `coherence/pass_runner.py` and `evaluator/pass_runner.py` are **both exactly 365 lines**
  and ~**76% identical** after normalising family names. `uncited_claims` is 379,
  `review` 576.
- Five separate enqueue helpers (170-260 lines each), five separate web routes
  (102-242 lines each), twelve separate daily-cap settings.
- **`claimed_at_generation` is 7/7** — the fence layer already treats these as one kind of
  thing. The tables never caught up.

**Decision (user, 2026-08-30): unify the pass families FIRST**, then land Directions as one
column on one table. Building Directions on the duplication means writing the job migration
five times and then undoing it. The unification gets its own spec and its own review round.
