# Proposal-Assist Phased Workflow — Design Spec

**Status:** Future-phase design, gated.
**Created:** 2026-05-19
**Layers on top of:** [`2026-04-18-call-manager-proposal-assist-design.md`](2026-04-18-call-manager-proposal-assist-design.md) (the base proposal-assist spec).
**Implementation gate** (must clear *before* any of this is built):
1. Populated golden set with ≥50 reviewed (call, profile) pairs + false-negative tracking.
2. Calibrated embedding hard-fail threshold from the golden-set distribution (not the current default-by-guess 0.20).
3. Section-aware extractor in production — `call_section_extracts` mechanism built and validated against the existing call corpus.

Reason for the gate: proposal-assist consumes both the ranking cosine and the section extracts. Building on top of uncalibrated foundations would amplify any error in either through the entire phased workflow.

---

## Purpose

The base proposal-assist spec gives us a single-chat workspace that drafts an `proposal.md` against a (profile, call) pair with git-backed artifact versioning, turn queueing, compaction, kill switch, and disclosure. This spec extends that with a **phased workflow** that mirrors the `superpowers` spec→plan→implement→review pattern: explicit phases with pinned goals, pinned artifacts, phase-gated tools, and the funder's own evaluation criteria as the scoring anchor throughout.

The motivating insight came from a user note that the existing open-chat shape doesn't enforce structure, and that proposals — like code — benefit from explicit planning before drafting. A second insight came mid-design: **matching** (does this profile see this call) and **proposal evaluation** (would the funder fund this draft) are different concerns using different rubrics. Matching stays as it is (the existing CallRanking module). Proposal evaluation uses `call_rubrics` — the funder's actual reviewer criteria — as the single scoring framework throughout proposal-assist.

---

## Architecture overview

**Inherits from base spec** (no change):
- One workspace = one git repo + one chat session.
- Turn queue (`turn_jobs`), compaction (`chat_summaries`), kill switch, disclosure banner, single-write lock, daily per-workspace token cap.
- Workspace ownership model: `workspaces.owner_id` (single owner) + `workspace_viewers` (read-only collaborators). Only the owner sends turns, edits artifacts, uploads attachments — and (new this spec) edits the rubric.
- Rename `call-monitor` → `call-manager`.
- Module location: `src/call_manager/proposal_assist/`.

**Overrides from base spec** (deliberately narrows scope):
- **Drops** the `search_pubmed` literature-search tool — deferred to v2 (see Out-of-scope).
- **Drops** the git branching tools (`create_branch` / `switch_branch` / `list_branches` / `diff_artifact`) and the branch indicator UI — branching is incompatible with the phased FSM (a branch fork would split phase approvals across branches with no defined merge semantics). Re-introducing branches requires a separate design.
- **Drops** the `list_past_grants` / `read_past_grant` tools — past-grant retrieval is in Out-of-scope (see "Reference past-proposal retrieval").
- **Replaces** the free-form single-artifact (`proposal.md`) chat with the Spec/Plan/Draft/Review phased workflow defined below. Base spec's chat machinery (turn queue, compaction, kill switch) is reused unchanged; the phase modules layer on top of it.

**Adds**: a finite state machine over the workspace lifecycle.

```
Phase 0 (Setup) ─► Spec ─► Plan ─► Draft ─► Review
   ↑ (non-interactive)    ↑ backward nav allowed
                          │ downstream artifacts go stale (computed
                          ▼ from phase_approvals timestamps, not stored)
                    phase_approvals row per forward transition
```

- **Phase 0** is a non-interactive background job that runs once per workspace. Snapshots, section extraction (if missing), rubric extraction (if missing), rubric user-review gate. User lands on Spec when done.
- **Spec / Plan / Draft / Review** are interactive chat phases with: a pinned goal (rendered in UI), a pinned artifact (the file the agent edits), a phase-gated tool subset, and the workspace's rubric copy (`workspaces.rubric_criteria`) always in the system prompt.
- **Forward gates are strict.** Approval is on the artifact (latest git sha recorded in `phase_approvals`). No skipping.
- **Backward navigation is unilateral.** Any user with workspace write access can jump back; downstream phases become **stale** by timestamp comparison and the UI shows "re-approve to clear" badges.
- **Review phase is non-chat** — a deterministic fan-out of ~6 Opus calls (1 fit-check + 4 persona scorings + 1 consolidation). Produces two artifacts: `fit-check.md` and `rubric-review.md`.

**Module boundary**: `src/call_manager/proposal_assist/` with `workspace.py` (state machine), `phases/{setup,spec,plan,draft,review}.py` (one module per phase, owns prompt + tool gates), `review_personas.py` (the four personas + consolidation), `artifacts.py` (git-backed read/write), `db.py` (new tables). Base spec's chat machinery imports phase modules; phase modules don't reach back into chat.

---

## Data model

Reuses base spec's `workspaces`, `chat_turns`, `turn_jobs`, `chat_summaries`, `artifact_versions`, `attachments`, `attachment_jobs`, `workspace_viewers`, `workspace_events`. Phased workflow adds six things.

### 1. Columns added to `workspaces`

```
current_phase             text NOT NULL DEFAULT 'setup'
                          CHECK (current_phase IN
                            ('setup','spec','plan','draft','review'))
call_body_snapshot        text NOT NULL DEFAULT ''
profile_desc_snapshot     text NOT NULL DEFAULT ''
snapshot_refreshed_at     timestamptz NOT NULL DEFAULT now()
rubric_criteria           jsonb NOT NULL                 -- copy of call_rubrics.criteria
                                                         --   at workspace creation; only
                                                         --   workspaces.owner_id may edit
rubric_funder_notes       text NULL                      -- copy of call_rubrics.funder_notes
rubric_updated_at         timestamptz NOT NULL DEFAULT now()
                                                         -- bumped on every owner edit;
                                                         --   drives staleness on downstream
                                                         --   phase approvals
review_runs_per_day_override int NULL          -- per-workspace override of
                                               --   Settings.REVIEW_RUNS_PER_DAY
review_dollar_cap_override numeric(6,3) NULL   -- per-workspace override of
                                               --   Settings.REVIEW_DOLLAR_CAP
```

**Q7a hybrid binding** (REVISED 2026-05-19): small structured fields (deadline, budget range, profile areas + must_include) are **live-read** from the joined `calls` + `profiles` rows. Bulky narrative content (call body, profile description) **and the funder rubric** are snapshotted into the above columns at workspace creation. The rubric move (previously live-read from `call_rubrics`) is motivated by isolation: the rubric is the central scoring yardstick, and cross-workspace edits could invalidate another PI's in-flight scoring without their knowledge — see decision log Q3 (REVISED). A "Refresh from source" UI button bumps `snapshot_refreshed_at` and re-reads the narrative fields. The rubric is **not** re-synced by Refresh; re-importing the call's template rubric is a separate explicit owner action from the workspace-settings rubric editor.

### 2. `phase_approvals` — forward-transition record

```
phase_approvals
  id                       uuid PRIMARY KEY
  workspace_id             uuid FK workspaces(id) ON DELETE CASCADE
  phase                    text CHECK (phase IN
                            ('spec','plan','draft','review'))
  approved_at              timestamptz NOT NULL DEFAULT now()
  approving_user_id        bigint FK users(id)
  approved_artifact_sha    text NOT NULL          -- 40-char repo commit sha
                                                   --   (NOT a blob sha); covers all
                                                   --   artifacts in the tree, including
                                                   --   multi-artifact phases like Review
                                                   --   (fit-check.md + rubric-review.md)
  INDEX (workspace_id, phase, approved_at DESC)
```

Forward transition = new row. Staleness computed by comparing `MAX(approved_at) GROUP BY phase` per workspace — no stored flag. Backward navigation writes nothing; only re-approval does.

### 3. `workspace_setup_jobs` — Phase 0 background task

```
workspace_setup_jobs
  id                       uuid PRIMARY KEY
  workspace_id             uuid FK workspaces(id) ON DELETE CASCADE UNIQUE
  status                   text CHECK (status IN
                            ('queued','running','waiting_for_rubric_review',
                             'done','failed'))
  attempts                 smallint NOT NULL DEFAULT 0
  last_error               text NULL
  started_at, completed_at timestamptz NULL
```

UNIQUE on `workspace_id` (one job per workspace). The job: snapshot the call body + profile description, ensure `call_section_extracts` + `call_rubrics` exist for the call, init git repo with empty artifacts, advance `current_phase: setup → spec`.

### 4. `call_section_extracts` — Codex's hard prereq (per call, not per workspace)

```
call_section_extracts
  call_id                  bigint FK calls(id) ON DELETE CASCADE
  section                  text CHECK (section IN
                            ('eligibility','scope','budget',
                             'expected_outcomes','restrictions','mandatory'))
  extract_text             text NOT NULL
  extracted_at             timestamptz NOT NULL DEFAULT now()
  extracted_by_model       text NOT NULL
  PRIMARY KEY (call_id, section)
```

Computed once per call (separate background job at ingestion, blocking nothing). Multiple workspaces against the same call share these.

### 5. `call_rubrics` — per-call template (Q3, REVISED 2026-05-19)

```
call_rubrics
  call_id                  bigint FK calls(id) ON DELETE CASCADE PRIMARY KEY
  criteria                 jsonb NOT NULL
                           -- shape: [{name: str, description: str,
                           --          weight: float, scale_max: int,
                           --          subcriteria?: list[{name, description}]}]
  funder_notes             text NULL              -- free-text
  extracted_at             timestamptz NOT NULL
```

Auto-extracted at ingestion. **Template only** — not the live source-of-truth for any workspace's evaluation. Each workspace gets its own editable copy (`workspaces.rubric_criteria`, `workspaces.rubric_funder_notes`) at creation. This row is read once per workspace (at Phase 0, to seed the workspace copy) and never again; editing this row affects future workspaces' starting point but never an existing workspace. No `user_reviewed_at` gate at the call level — review happens per-workspace in Phase 0 (see Phase 0 step 6).

### 6. `review_runs` — per-review-run cost ceiling tracking (Q9)

```
review_runs
  id                       uuid PRIMARY KEY
  workspace_id             uuid FK workspaces(id) ON DELETE CASCADE
  status                   text CHECK (status IN
                            ('queued','running','done','failed','cancelled'))
  triggered_at             timestamptz NOT NULL DEFAULT now()
  completed_at             timestamptz NULL
  draft_sha                text NOT NULL          -- proposal.md sha reviewed
  fit_check_sha            text NULL              -- written first; gates
                                                   --   the persona reviews
  rubric_review_sha        text NULL              -- written last
  input_tokens             integer NULL
  output_tokens            integer NULL
  cache_read_input_tokens  integer NULL
  estimated_dollars        numeric(6,3) NULL
  INDEX (workspace_id, triggered_at DESC)
```

The per-workspace-per-day count + the dollar estimate feed the cap: default 5 review runs per workspace per day AND ≤ $0.75 estimated cost per run (refused before any LLM call when prompt-token count would exceed budget). Both admin-overridable globally and per-workspace.

**Migration ordering**: all six in one Alembic migration. No data backfill (greenfield in the post-rename codebase).

---

## Phase-by-phase workflow

Each phase has: a pinned **goal** (rendered in UI), a pinned **artifact**, a phase-gated **tool subset**, and the funder rubric in the system prompt throughout. Phase transitions are explicit user actions; downstream artifacts go stale via timestamp comparison.

### Phase 0 — Setup (non-interactive)

**Trigger**: user clicks "Start proposal" on a (profile, call) pair.

**Jobs the `workspace_setup_jobs` row drives**, in order:

1. Initialize git repo at `var/workspaces/<id>/repo/` with empty `proposal-spec.md`, `proposal-plan.md`, `proposal.md`. (Review artifacts created when first Review run fires.)
2. Snapshot `calls.full_text` into `workspaces.call_body_snapshot`.
3. Snapshot `profiles.description` into `workspaces.profile_desc_snapshot`.
4. Ensure `call_section_extracts` exists for this call (6 rows). If missing, run the extractor (one Opus call) and write them.
5. Ensure `call_rubrics` exists for this call (template). If missing, auto-extract via Opus and write the row.
6. Copy `call_rubrics.criteria` and `call_rubrics.funder_notes` into `workspaces.rubric_criteria` and `workspaces.rubric_funder_notes`. Set `workspaces.rubric_updated_at = now()`. **Block** transition to Spec at `workspace_setup_jobs.status = 'waiting_for_rubric_review'` until the workspace **owner** opens the rubric-review screen, edits if needed, and clicks confirm. Every workspace gets its own review beat — no skip-if-call-already-reviewed shortcut, because each workspace's copy is independently editable. While the job is `waiting_for_rubric_review` it is **not** counted as `running` (the worker yielded) — this avoids holding a worker slot indefinitely on a human gate.
7. Advance `current_phase: setup → spec`.

**Failure handling**: retries on transient failures up to 5 attempts. Permanent failure surfaces with option to manually paste rubric.

**Goal statement (rendered on UI)**: "Setting up your workspace — extracting the funder's evaluation criteria and snapshotting the call."

### Spec phase

**Pinned goal**: *"Write a 1-2 page `proposal-spec.md` that states the scientific question, hypotheses, why this matters, how it maps to the funder's criteria, and what 'done' looks like. The spec is the single source of truth for the rest of the workflow."*

**Pinned artifact**: `proposal-spec.md`.

**System prompt skeleton**:
- Phase = Spec.
- Live structured: profile name + areas + must_include + budget range; call programme + deadline + budget; **`workspaces.rubric_criteria` verbatim** (the workspace's own copy — see "Rubric editing").
- Bulky content behind tools.
- Pinned goal as above.
- Instruction: "The spec should articulate at least one sentence per top-level rubric criterion explaining how the planned work addresses it. Don't draft prose — that's for Draft phase."

**Tools available**: `read_spec`, `edit_spec`, `replace_spec`; all read-only context tools; `score_against_rubric`; version history. **Tools NOT available**: edits on plan or proposal (returns 400 with phase-mismatch message).

**Approval**: user clicks "Approve Spec → Plan" → `phase_approvals` row written with current spec sha.

### Plan phase

**Pinned goal**: *"Write `proposal-plan.md`: a section-by-section outline of the actual proposal. For each section, state which rubric criteria it addresses, how long it'll be, what evidence it'll cite, and what remains uncertain. The plan locks scope before any prose is written."*

**Pinned artifact**: `proposal-plan.md`.

**System prompt skeleton**:
- Phase = Plan.
- Live structured + `proposal-spec.md` content (read fresh every turn — small).
- Pinned goal as above.
- Instruction: "Every rubric criterion must be assigned to at least one proposal section. Flag criteria that don't map cleanly."

**Tools available**: edit/read `proposal-plan.md`; read-only on `proposal-spec.md`; all Spec read-only tools; `score_against_rubric`. **Tools NOT available**: edits on spec (use backward nav) or proposal.

**Approval**: user clicks "Approve Plan → Draft" → `phase_approvals` row written.

### Draft phase

**Pinned goal**: *"Write the full proposal in `proposal.md`. Follow the section structure from the Plan. Cite preliminary data where the Plan promised. Use the funder's voice — formal, structured, evidence-backed."*

**Pinned artifact**: `proposal.md`.

**System prompt skeleton**:
- Phase = Draft.
- Live structured + `proposal-spec.md` + `proposal-plan.md` content fresh every turn.
- Pinned goal as above.

**Tools available**: edit/read `proposal.md`; read-only on spec + plan; all Spec read tools; `score_against_rubric`; **new** `count_words`. **Tools NOT available**: edits on spec or plan; triggering Review (must approve forward).

**Approval**: user clicks "Approve Draft → Review" → `phase_approvals` row written.

### Review phase

**Pinned goal**: *"Produce `fit-check.md` (does the proposal address every line of the rubric, yes/no per line) and `rubric-review.md` (graded scoring against the funder's criteria with consolidation across four reviewer personas)."*

**Pinned artifacts**: `fit-check.md` then `rubric-review.md` (in that order; fit-check gates the rest).

**Execution model** — a `review_runs` row drives the fan-out (not the interactive chat):

1. **Fit-check pass**. Deterministic Opus call. Input: `proposal.md` + `workspaces.rubric_criteria` (workspace copy). Output: per-criterion `{addressed: bool, evidence_quote: str, gap_description?: str}`. Writes `fit-check.md`. If any `addressed: false` → user prompted to either backward-nav to Draft or explicitly waive the gate. Waiver recorded in `workspace_events`.
2. **Four persona scorings** in parallel (asyncio gather). Each is one Opus call. Each receives: `proposal.md` + `workspaces.rubric_criteria` + persona framing. Each outputs per-criterion `{rating: int_on_funder_scale, rationale: str, quoted_evidence: str}`. Personas: Domain skeptic / Methodology critic / Translation-impact skeptic / Devil's advocate. They evaluate the same criteria; persona only changes emphasis and what they look critically at.
3. **Consolidation pass**. One Opus call. Inputs: 4 persona JSONs + fit-check JSON. Output: `rubric-review.md` with per-criterion consolidated rating (median or worst-case, configurable), agreement/disagreement notes, "if these reviewers were funder reviewers, your score would likely be X."

**Tools available**: read-only on everything; no editing. (Review is non-chat — there's no agent loop to drive a tool surface here.)

**Approval**: user clicks "Approve Review" → `phase_approvals` row written. Workspace status can transition to `submitted` from the UI.

### Backward navigation behaviour

- Spec → Plan → Draft → Review is the canonical forward path.
- Any user with workspace write access can click on a prior phase to re-enter it.
- The clicked phase becomes the new `current_phase`; chat re-opens with that phase's pinned goal and gated tools.
- Backward nav writes nothing in `phase_approvals` and on its own makes nothing stale.
- **Staleness fires from two sources.**
  - **Upstream re-approval:** phase X is stale when `MAX(phase_approvals.approved_at WHERE phase = X) < MAX(phase_approvals.approved_at WHERE phase = <any earlier phase>)`. Example: user backward-navs from Draft to Spec, edits the spec, re-approves Spec → spec's approval timestamp now exceeds Plan's and Draft's → both flagged stale in the UI.
  - **Rubric edit:** phase X is also stale when `MAX(phase_approvals.approved_at WHERE phase = X) < workspaces.rubric_updated_at`. The rubric is the central scoring yardstick; a rubric change invalidates the assumption under which the phase was previously approved. Rubric edits are owner-only (see "Rubric editing"); no machinery beyond the timestamp comparison is needed. Both sources use the same UI badge — the user sees "needs re-approval" either way.
- No deletion of stale artifacts. Re-approving Plan and Draft (with whatever updates the spec or rubric edit motivates) writes fresh `phase_approvals` rows that exceed both timestamps, clearing the badges.

### Rubric editing

The workspace's rubric copy (`workspaces.rubric_criteria`, `workspaces.rubric_funder_notes`) is editable post-Phase-0 from a **workspace-settings form**. **Owner-only**: viewers see the rubric read-only; the edit affordance is hidden for non-owners and the API endpoint returns 403 if a non-owner attempts to PATCH.

- **Form, not chat tool.** Criteria are structured JSON — a form UI is more natural than markdown editing. The agent has **no** `edit_rubric` tool, and no read tool either: the rubric is in the system prompt every turn (see "System prompt skeleton" in each phase), so an explicit read would be redundant.
- **Edit semantics.** Each save writes the new criteria + funder_notes to `workspaces`, bumps `rubric_updated_at`, and writes a `workspace_events` row with `kind='rubric_edited'` and `payload={'old_criteria': ..., 'new_criteria': ..., 'old_funder_notes': ..., 'new_funder_notes': ..., 'edited_by_user_id': <owner_id>}`. The audit log is the reproducibility surface: given any past phase-approval timestamp, the rubric content in force at that time can be reconstructed by replaying `rubric_edited` events up to that timestamp against the current row.
- **Staleness.** Every rubric edit stales all downstream phase approvals via the timestamp rule above. The owner is shown a confirmation modal before save: *"This edit will mark Spec / Plan / Draft as needing re-approval. Continue?"*

---

## Tool surface

Single coherent tool list, **phase-gated at the call layer** (Q10). The agent always sees the full surface; each tool's handler checks `workspaces.current_phase` and returns 400 if out-of-phase. Prompt cache stays warm across phase transitions; misfires are recoverable in-conversation.

### Artifact-editing tools (write side)

| Tool | Spec | Plan | Draft | Review |
|---|---|---|---|---|
| `read_spec()` | ✅ | read | read | read |
| `edit_spec(edits)` | ✅ | — | — | — |
| `replace_spec(content)` | ✅ | — | — | — |
| `read_plan()` | — | ✅ | read | read |
| `edit_plan(edits)` | — | ✅ | — | — |
| `replace_plan(content)` | — | ✅ | — | — |
| `read_proposal()` | — | — | ✅ | read |
| `edit_proposal(edits)` | — | — | ✅ | — |
| `replace_proposal(content)` | — | — | ✅ | — |

All edits auto-commit to the workspace git repo (one commit per tool call); `artifact_versions` row written in the same DB transaction (base spec).

### Read-only context tools

| Tool | Description |
|---|---|
| `get_call_body()` | Returns `workspaces.call_body_snapshot` (the snapshot, not live). |
| `get_section_extract(section)` | One of `eligibility / scope / budget / expected_outcomes / restrictions / mandatory`. |
| `get_rubric()` | Returns the full workspace rubric: `workspaces.rubric_criteria` + `workspaces.rubric_funder_notes`. Reads the workspace's own copy, not the call template. (Redundant with the system-prompt verbatim copy, but kept for cases where the agent wants to re-cite specific text without consuming context window.) |
| `get_profile_description()` | Returns `workspaces.profile_desc_snapshot`. |
| `list_preliminary_data()` | Returns filename + description for each user-uploaded attachment. Spec/Plan/Draft only. |
| `read_preliminary_data(filename)` | Returns the extracted text (or describe-figure output) of the named attachment. Spec/Plan/Draft only. |

### Self-check tools

| Tool | Description | Available in |
|---|---|---|
| `score_against_rubric(text)` | Single cheap Opus call: scores the given text snippet against the **workspace's rubric copy** (`workspaces.rubric_criteria`), returns per-criterion provisional ratings + rationale. Not committed anywhere — purely informational. ~$0.01–0.03 per call. | Spec, Plan, Draft |
| `count_words(content)` | Local count of words. Critical in Draft for funder per-section page/word limits. | Draft |

### Version-history tools

| Tool | Description | Available in |
|---|---|---|
| `get_artifact_history(artifact, limit=20)` | Recent commits touching the named artifact — sha, message, timestamp, author. | All phases |
| `read_artifact_at(artifact, commit_sha)` | Older version of any artifact. | All phases |

### Tools deliberately NOT in v1

- No `bash` execution
- No generic `web_fetch`
- No `write_file` for arbitrary paths
- No file delete
- No literature-search tools — deferred to v2 (see Out-of-scope).
- No scratchpad / notes file outside the canonical artifacts — chat history serves this role.

---

## Cost guardrails

Three layers, stacked.

### Layer 1 — Daily per-workspace token cap (inherited from base spec)

Base spec defines per-workspace daily token budget with graceful degrade (workspace pauses until 00:00 Europe/Stockholm). Phased workflow inherits unchanged.

### Layer 2 — Per-review-run hard ceiling (Q9)

Two stacked limits, both admin-overridable globally (`Settings.REVIEW_RUNS_PER_DAY`, `Settings.REVIEW_DOLLAR_CAP`) and per-workspace (override columns on `workspaces`, null falls through to Settings).

| Limit | Default | At cap |
|---|---|---|
| Reviews per workspace per day | 5 | Inline error "Daily review quota reached. Resumes in `<N>` hours." Backward nav + Draft editing don't consume quota. |
| Estimated $ per review run | $0.75 | Run refused before any LLM call. UI suggests "trim `proposal.md` or split into per-section reviews." Pre-estimate = prompt size × 6 calls × Opus 4.7 input price + worst-case output × Opus 4.7 output price. |

### Layer 3 — Live cost telemetry

Per-turn telemetry already in base spec (`chat_turns.tokens_in / tokens_out / model`). Add to `review_runs`: `input_tokens`, `output_tokens`, `cache_read_input_tokens`, `estimated_dollars`.

Settings carries a price table (`OPUS_INPUT_PRICE_PER_MTOK`, `OPUS_OUTPUT_PRICE_PER_MTOK`, `OPUS_CACHED_INPUT_PRICE_PER_MTOK`) so estimates update on reprice without code change.

### UI surfacing

- **Workspace top bar**: "Today: $X / $Y budget · N of M reviews used".
- **Pre-review confirmation modal**: "This review run is estimated at $X (6 Opus calls). Proceed?" — explicit consent before fan-out even under cap.
- **`/admin` aggregate view**: spend per workspace + per user, sort.

### What's not in scope (cost-side)

- Token-counting before drafting turns (daily cap suffices; per-turn pre-check doubles latency for marginal benefit).
- Per-user monthly cap (daily-per-workspace is enough for v1).
- Pricing for models other than Opus 4.7 (all proposal-assist LLM calls go to Opus 4.7 with adaptive thinking, `effort: high` for Review consolidation, `effort: medium` elsewhere).

---

## Testing strategy

Mirrors the project's existing pattern (real Postgres via testcontainers, mocked LLM via stubs returning OpenAI-shaped envelopes).

**Phase state machine** (high-leverage, tested heavily):
- Forward transition writes `phase_approvals`; `approved_artifact_sha` matches the repo HEAD commit (not a blob sha).
- Backward nav does NOT write `phase_approvals`.
- Stale detection — upstream re-approval: after re-approving Spec, `MAX(approved_at WHERE phase='plan') < MAX(approved_at WHERE phase='spec')` → plan is stale.
- Stale detection — rubric edit: after `rubric_updated_at` is bumped, `MAX(approved_at WHERE phase='plan') < rubric_updated_at` → plan stale; same rule for Spec, Draft, Review.
- Out-of-phase tool calls return 400 with helpful message.
- Phase 0 blocks at `workspace_setup_jobs.status='waiting_for_rubric_review'` until the workspace owner confirms the workspace rubric copy.

**Rubric editing**:
- Owner PATCH succeeds, bumps `rubric_updated_at`, writes `workspace_events(kind='rubric_edited')` with old + new content in payload.
- Non-owner (viewer) PATCH returns 403; no DB writes.
- Audit reconstruction: replaying `rubric_edited` events up to a past timestamp reproduces the rubric content in force at that moment.
- Rubric edit invalidates downstream approvals (see staleness tests).

**Artifact / git layer**:
- `edit_*` and `replace_*` commit; `artifact_versions` row written in same tx.
- `read_artifact_at(sha)` returns content from prior commit.
- Concurrent edits blocked by base spec's single-write lock.

**Section + rubric extraction**:
- `call_section_extracts` idempotent — second call returns DB-cached rows, no LLM hit.
- `call_rubrics` extraction tolerates malformed JSON; surfaces as "user must paste rubric" UI flag, not a crash.
- Section extracts dedup'd per call across workspaces.

**Review fan-out**:
- Fit-check before quality: any `addressed: false` → `review_runs.rubric_review_sha` stays NULL until user resolves or waives.
- Four persona calls run in parallel; one timing out doesn't block the others — consolidation runs on surviving 3 with "1 reviewer unavailable" note.
- Per-run dollar cap refuses BEFORE any Opus call when pre-estimate > cap.
- Per-workspace daily review count enforced.

**Cost telemetry**:
- `chat_turns.tokens_in/out` populated for every Spec/Plan/Draft turn.
- `review_runs.input_tokens/output_tokens/cache_read_input_tokens` populated post-run.
- `estimated_dollars` computed from token counts × Settings price table.

**End-to-end happy path** (one slow test, real LLM disabled — stub returns canned responses):
- Create workspace → Phase 0 completes → Spec → Plan → Draft → Review → user approves Review. Verify `phase_approvals` has 4 rows, repo has 4+2 artifacts, no orphan turns/jobs.

**Out of test scope for v1**:
- Multi-user concurrent editing (single-writer lock handles correctness; UX not validated).
- 5+ persona reviewers or dynamic persona selection — fixed at four.

---

## Out of scope for v1

| Feature | Reason | Revisit when |
|---|---|---|
| Literature-search tools (PubMed / OpenAlex / Semantic Scholar) | Three external API integrations + caching + rate-limit etiquette stretches v1 too far before the core loop is validated. | Core loop in production ~1 month; users explicitly request lit-search. |
| Funder archetypes (NIH / Horizon / Vinnova style snippets) | Curated knowledge base + maintenance burden. | Users say "this reads like grant-template noise, not real Horizon." |
| Reference past-proposal retrieval | Separate corpus, embedding index, access control across multiple PIs' work. Real privacy concerns. | Hospital has 50+ past funded proposals + credible surface mechanism. |
| Scratchpad / notes file separate from artifacts | Chat history serves this role; second writing surface complicates the mental model. | Users explicitly ask after using v1. |
| Multi-author real-time co-editing | Base spec ships single-writer locking. Async hand-off works. | >50% of proposals have multiple authors and async hand-off is the bottleneck. |
| Pinecone / PageIndex / vector DBs | Out of scope at current scale; pgvector handles `call_rubrics` + everything else. | If past-proposal retrieval lands AND corpus crosses ~10k papers. |
| Mid-phase auto-evaluation triggers | `score_against_rubric` is opt-in; auto-triggers risk a self-improvement loop that spends money. | Users report they keep forgetting to self-check. |
| Cross-workspace rubric sharing (org-wide rubric edits) | Workspaces are isolated by design as of 2026-05-19 — each owns its own rubric copy. Org-wide propagation would re-introduce the cross-workspace drift problem that motivated isolation. | Operator reports they're maintaining the same rubric edit by hand across many workspaces against the same call. |
| Generic scientific-quality dimensions (Scientific background, Novelty, Significance, Methodology, Feasibility, Preliminary data, Risk & mitigation, Clarity & coherence) | Replaced by the funder's own rubric throughout (revised Q4/Q5). Cross-funder comparability deferred until needed. | Cross-funder comparison becomes a real user need. |

---

## Decision log

Brainstorm decisions resolved in order. Documented so we don't relitigate.

| Q | Resolution | Notes |
|---|---|---|
| Q1 | One mode — phased wraps freeform | Each phase is chat with pinned goal + pinned artifact. Workspace tracks phase via state machine. |
| Q2 | Phases: Phase 0 → Spec → Plan → Draft → Review | Backward nav allowed; downstream artifacts go stale (not deleted). No separate "revision" phase. |
| Q3 | **REVISED 2026-05-19** — Per-call template, per-workspace editable copy, owner-only edits | Generic schema `{name, description, weight, scale_max, subcriteria?}`. Auto-extracted at ingestion into `call_rubrics` (template). Each workspace copies the template into its own columns at Phase 0; the owner reviews/edits before Spec phase. Post-Phase-0 edits (owner only, via workspace settings form) bump `rubric_updated_at` and stale downstream approvals. **Previously:** rubric was live-read per-call (shared) with a call-level `user_reviewed_at` gate; changed because a shared edit could invalidate another PI's in-flight scoring without their knowledge, and because the spec's own staleness machinery worked only on artifact approvals, not rubric changes. |
| Q4 | **REVISED 2026-05-19** — Funder's rubric (`call_rubrics`) is the scoring anchor throughout, not just orientation context | Previously: 8 generic quality dimensions (Scientific background, Novelty, Significance, Methodology, Feasibility, Preliminary data, Risk & mitigation, Clarity & coherence). Dropped: that was a parallel framework that muddied the funder-specific view. User drew the matching-vs-evaluation distinction. |
| Q5 | **REVISED 2026-05-19** — Review fan-out: fit-check + 4 personas × funder rubric + consolidation | Two review artifacts: `fit-check.md` + `rubric-review.md`. Personas (Domain skeptic / Methodology critic / Translation-impact skeptic / Devil's advocate) score against the SAME funder criteria with different framings. |
| Q6 | Strict forward gates. Approval is on the artifact. Backward navigation is unilateral. New table `phase_approvals(workspace_id, phase, approved_at, approving_user_id, approved_artifact_sha)`. Staleness via timestamp comparison. | No stored stale flag — computed. |
| Q7a | **REVISED 2026-05-19** — Hybrid binding, rubric moved to snapshot side | Live-read small structured fields (deadline, budget range, profile name + areas + must_include). Snapshot bulky narrative content (full call body, profile description) **and the funder rubric**. Refresh button bumps `snapshot_refreshed_at` and re-reads narrative fields only; rubric re-sync is a separate explicit owner action. Rubric was moved out of live-read as part of the Q3 revision — see Q3 (REVISED). |
| Q7b | Hybrid: small structured in prompt, bulky via tools | System prompt always carries small structured context. Bulky content (call body, section extracts, preliminary data) behind tools. |
| Q7c | Defer both funder archetypes + reference past proposals | Phase 0 loads: structured call + raw call body, profile, preliminary data, section extracts. That's it. Archetypes + past-proposal retrieval are v2 features. |
| Q8 | One git repo per workspace, all artifacts | Matches base spec's git-backed-artifact design. `git log` is the audit trail. Backup is `tar` the repo. |
| Q9 | Daily token cap (from base spec) + per-review-run hard ceiling | Default: 5 reviews/workspace/day, $0.75 estimated cost per run. Both admin-overridable globally + per-workspace. |
| Q10 | Single tool set, phase-gated at the call layer | Tool handlers check current_phase; 400 with helpful message on out-of-phase use. Prompt cache stays warm. |
| v1 scope | Literature-search tools deferred to v2 | Scratchpad deferred to v2. |

---

## Cross-references

- Base spec (turn queue, git artifacts, compaction, kill switch, disclosure, rename): [`2026-04-18-call-manager-proposal-assist-design.md`](2026-04-18-call-manager-proposal-assist-design.md)
- Ranking module v1 design: [`2026-04-14-call-monitor-design.md`](2026-04-14-call-monitor-design.md)
- Ranking v2 criterion design: [`2026-04-17-ranking-v2-criterion-design.md`](2026-04-17-ranking-v2-criterion-design.md)
- Ranking v2 embedding pre-filter: [`2026-04-18-ranking-v2-d-embedding-prefilter-design.md`](2026-04-18-ranking-v2-d-embedding-prefilter-design.md)
- Full-text ingestion: [`2026-04-19-full-text-ingestion-design.md`](2026-04-19-full-text-ingestion-design.md)
