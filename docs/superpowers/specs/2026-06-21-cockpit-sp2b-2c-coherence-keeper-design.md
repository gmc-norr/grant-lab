# SP2b-2c — Coherence-Keeper — Design Spec

**Slice:** SP2b-2c (cockpit sub-project 2, the spec↔draft coherence loop).
**Base:** master `bc017a4` (after SP2b-2b: agent↔spec propose/accept + inline `<ProposalReview>`).
**Overarching design:** `docs/superpowers/specs/2026-06-17-cockpit-workspace-design.md` (§2 conceptual model, §3 lifecycle, §4.2 background sessions, §5 `proposed_change`, §8 Spec, §11.8 build item).

---

## 0. Context & scope

The cockpit's spine is a **spec↔draft coherence loop**: editing the spec should make the AI *propose* draft changes to realize it; growing the draft beyond the spec should make the AI *propose* a spec update. Always **proposed, never auto-applied** — a human accepts and owns it. SP2b-2c builds the **coherence-keeper**: a background AI session that holistically compares the spec and the draft and emits reconciliation proposals.

**This slice is deliberately the smallest end-to-end spine.** The coherence-keeper's output is `proposed_change` rows, which already have a review surface — the inline `<ProposalReview>` strips merged in SP2b-2b. So this slice delivers a complete user loop (trigger → background pass → proposals appear in the existing inbox → accept/reject) **with no new review UI**.

**Locked decisions (from brainstorming):**

1. **Output = proposals only.** The keeper emits `proposed_change` rows surfaced in the existing SP2b-2b inboxes. **No** Problems/drift-findings table or panel — that overlaps the evaluator's output (criteria gaps, uncited claims, over-length, broken citations) and is built **once, with the evaluator, in SP2b-3.**
2. **Trigger = manual only.** A "Check coherence now" action. No on-save debounce, no sweep timer for coherence in v1 (the job queue makes adding one later small).
3. **Direction = lock-aware bidirectional.** Spec **unlocked** (divergent): propose draft edits *and* spec edits. Spec **locked** (convergent): **draft edits only** — the locked spec is the authoritative target.
4. **Repeated passes = deterministic supersede.** A new pass supersedes the previous pass's still-pending coherence proposals, so the inbox always reflects the latest holistic assessment. Human/interactive proposals are never touched.
5. **Session = fresh stateless per pass.** Each pass is a new Claude SDK session fed full assembled context, no resume, never posting to the chat thread, never touching the interactive `last_sdk_session_id`. Safe to run concurrently with an interactive turn (propose-only + `base_section_sha` staleness, per §6 of the overarching design).

**Deferred to SP2b-3:** the continuous evaluator + Scorecard, the Problems/drift panel, on-save/debounced auto-trigger, prompt-cache-control wiring, holistic-drift findings that aren't a concrete section edit.

---

## 1. Architecture & data model (the substrate)

**Flow.** `POST .../coherence-runs` inserts a `queued` `coherence_jobs` row and returns its id → a new APScheduler tick `_coherence_tick` claims it → runs **one fresh stateless agent pass** → the pass emits `proposed_change` rows (`origin_kind='coherence_session'`) and supersedes the prior pass → job → `completed`. The SPA polls the job, then refetches the existing inboxes.

### 1.1 New table `coherence_jobs`

Mirrors `turn_jobs` (`db/models.py:827`). One Alembic migration (head is the SP2b-2a `0052`; this is the next revision).

| column | type | notes |
|---|---|---|
| `id` | UUID PK | |
| `workspace_id` | UUID FK→`workspaces` ON DELETE CASCADE | |
| `status` | TEXT NOT NULL | CHECK `IN ('queued','running','completed','failed')` |
| `requested_by` | BIGINT FK→`users` ON DELETE SET NULL, nullable | who clicked |
| `error` | TEXT nullable | failure message |
| `queued_at` | timestamptz NOT NULL server_default now() | |
| `started_at` | timestamptz nullable | |
| `completed_at` | timestamptz nullable | |
| `sdk_session_id` | TEXT nullable | the fresh pass's SDK session uuid (recorded at TurnDoneEvent) |
| `n_proposals` | INT NOT NULL server_default `0` | proposals emitted by this pass (UI summary) |
| `model` | TEXT nullable | the model used (audit) |
| `input_tokens` | INT NOT NULL server_default `0` | from the pass's `ResultMessage` usage |
| `output_tokens` | INT NOT NULL server_default `0` | from the pass's `ResultMessage` usage |

Coherence is **token-accounted on this row** — the existing per-workspace daily cap (`turns/caps.py`) sums `ChatTurn` rows, and the keeper writes **no** `ChatTurn`, so it is invisible to that cap. See §2.5 for the separate coherence cap that reads these columns.

Indexes:
- `(workspace_id, queued_at)` — FIFO claim order (as `turn_jobs`).
- **Partial-unique on `workspace_id WHERE status IN ('queued','running')`** — at most **one active job per workspace**. (This is intentionally *stronger* than `turn_jobs`' `WHERE status='running'`: a user may queue several chat turns, but only one coherence pass should ever be pending+running for a workspace.) It both enforces one-pass-at-a-time *and* makes the `POST` "create-or-return-active" race-safe — two simultaneous POSTs can't both enqueue; the loser hits the unique violation and re-reads the existing active job. The row stays a single row as it transitions `queued → running → completed`, leaving the active set only on completion, which frees the next enqueue.

### 1.2 `proposed_changes` — no migration

`origin_kind`'s CHECK already permits `coherence_session` (`db/models.py:975`), and `origin_job_id` is a bare nullable UUID column (`db/models.py:1002`, no FK) that holds the `coherence_jobs.id`. Nothing in `proposed_changes` changes.

### 1.3 Scheduler tick

A new `_coherence_tick` registered in `build_scheduler()` beside `_phase0_tick` (`scheduler.py:370`), `IntervalTrigger(seconds=settings.coherence_tick_seconds)`, `id="proposal_assist_coherence_tick"`, `max_instances=1` — honoring the single-instance constraint. Backed by `run_coherence_tick(session_factory, client_factory, settings)` (mirrors `run_phase0_tick`), which each tick:

1. **Reaps stuck jobs first** — flips any `running` job whose `started_at` is older than `settings.coherence_job_timeout_seconds` to `failed` (`error="timed out"`) and runs the §2.5 failure cleanup. **This is required**: because the active-unique index counts `running`, a process crash mid-pass would otherwise wedge the workspace forever (`create-or-return-active` keeps returning the dead job). The reaper is the liveness escape hatch.
2. **Then claims one job** — the oldest `queued` job whose workspace has no `running` job, claimed `FOR UPDATE SKIP LOCKED`, flipped to `running` (sets `started_at`), then handed to the pass handler.

---

## 2. The coherence pass (`coherence/pass.py`)

`run_coherence_pass(job, *, session_factory, client_factory, settings)`:

### 2.1 Context assembly (`coherence/context.py`, net-new)

`assemble_coherence_context(session, workspace_id) -> CoherenceContext` reads: workspace criteria (`ws.rubric_criteria` + `ws.rubric_funder_notes`), the **full** spec (`proposal-spec.md`), the **full** draft (`proposal.md`), and the spec-lock state (`ws.spec_locked_at`). Composed into one keeper instruction string. (Anthropic prompt-caching of the stable criteria+spec prefix is a **later optimization**, not v1 — manual-only bounds cost.)

### 2.2 The agent turn + lock-aware toolset

One fresh stateless turn via the existing adapter `ClaudeAgentSDKAdapter`: `launch` with **no** `resume` (new session), then `send_turn` with the keeper instruction. The instruction frames the role (compare spec intent vs draft; propose conservative, per-section, full-text reconciliation edits; don't restate non-drift).

**Isolation (do NOT reuse `WorkspaceController`).** The interactive `WorkspaceController` caches a per-workspace SDK client + a retained `ToolContext` and *resumes* the interactive session — routing the keeper through it would reuse the interactive subprocess + its provenance, breaking "fresh stateless" and corrupting `origin_kind`. The pass instead constructs its **own one-shot stack**: a dedicated adapter instance + its own in-process MCP `@tool` server + its own `ToolContext` (stamped with coherence provenance, §2.3), launched with no resume, and **terminated in a `finally`** (the SDK subprocess is closed even on error). Nothing touches `Workspace.last_sdk_session_id`.

**Lock-aware toolset (structural, not just prompted):**
- spec **unlocked** → allowlist `{read_proposal, read_spec, propose_edit, propose_spec_edit}`
- spec **locked** → allowlist `{read_proposal, read_spec, propose_edit}` (`propose_spec_edit` removed; a spec edit is impossible even if the model tries)

The keeper must call `read_proposal`/`read_spec` first — that sets the read-time `agent_view_*` snapshot the `propose_*` tools key on for the anti-clobber base.

**Allowlist parameterization.** The adapter's `allowed_tools` is today one fixed ~21-tool list (`claude_adapter.py`). The keeper launch path supplies a **role-scoped `allowed_tools` override**; the adapter accepts an optional override and falls back to the current interactive list when unset — the interactive path is unchanged.

### 2.3 Provenance plumbing

`_propose_artifact_edit_impl` currently hardcodes `origin_kind="interactive_turn"` (`tools/proposal.py:140`). Change:
- `ToolContext` gains `origin_kind: str = "interactive_turn"` and `origin_job_id: uuid.UUID | None = None` (`tools/registry.py`).
- `_propose_artifact_edit_impl` passes `origin_kind=ctx.origin_kind`, `origin_job_id=ctx.origin_job_id`. Defaults preserve the interactive path exactly.
- The keeper's tool-context factory sets `origin_kind="coherence_session"`, `origin_job_id=job.id`, `created_by=job.requested_by`, `triggering_turn_id=None`.
- **`origin_session_id` is left `NULL` on coherence rows.** Unlike a *resumed* interactive turn, a fresh session's SDK uuid is unknown until `TurnDoneEvent` (after the `propose_*` tools have already committed their rows), so it cannot be stamped at propose time. The durable session link is **`proposed_change.origin_job_id` → `coherence_jobs.sdk_session_id`** (the uuid recorded on the job when the turn completes). No post-hoc backfill of the proposal rows is needed.

### 2.4 Supersede + finalize (on success)

After the turn completes, in one transaction:
```
UPDATE proposed_changes
   SET status='superseded'
 WHERE workspace_id = :ws
   AND origin_kind = 'coherence_session'
   AND status = 'pending'
   AND (origin_job_id IS NULL OR origin_job_id <> :job_id);
```
then set `coherence_jobs.n_proposals` = count of this pass's rows, `completed_at`, status `completed`, and persist `sdk_session_id`. A clean pass that finds no drift legitimately clears prior stale coherence proposals.

### 2.5 Failure cleanup

On agent/API error (or timeout reap, §1.3) the job is marked `failed` with the message. Because the `propose_*` tools **commit each row as they run**, a pass that fails *after* emitting some proposals would otherwise leave orphaned half-assessment rows pending under a failed job. So failure cleanup is the **inverse** of the success path:

- **On failure** — supersede **this job's own** still-pending rows (`origin_job_id = job.id AND status='pending'`); leave **prior** jobs' proposals **intact** (the user keeps the last good assessment).
- **On success** (§2.4) — supersede **prior** jobs' rows (`origin_job_id <> job.id`); keep this job's rows.

The pass is non-mutating to `proposal.md`/`proposal-spec.md` (it only writes `proposed_change` rows and reads), so it runs concurrently with an interactive turn with no locks — any race is resolved by `base_section_sha` staleness.

### 2.6 Cost cap (separate coherence budget)

The interactive cap (`turns/caps.py`) sums `ChatTurn` tokens and the keeper writes none, so coherence needs its **own** budget rather than retrofitting that query. **Before claiming/running a pass**, the tick checks a per-workspace **daily coherence token cap**: `SUM(input_tokens + output_tokens)` over today's (Stockholm-day, matching `caps.py`) `coherence_jobs` for the workspace `< settings.coherence_daily_token_cap`. Over budget → the job is marked `failed` with a clear "daily coherence budget reached" message (no LLM call made). Each completed pass records `model` / `input_tokens` / `output_tokens` from its `ResultMessage` usage. (A future unified cross-surface budget can combine chat + coherence; out of scope here.)

---

## 3. Endpoints & frontend

### 3.1 Endpoints (`web/coherence.py`, under `/api`, member-gated)

Same envelope + `require_member` patterns as `web/proposals.py`.

- **`POST /api/workspaces/{id}/coherence-runs`** — *create-or-return-active*: if a `queued`/`running` job already exists for the workspace, return it (no stacking); else insert a `queued` row (`requested_by`=current user) and return `{id, status}`. The §1.1 active-unique index makes this race-safe — on a concurrent-insert unique violation, catch and re-read the existing active job.

  **Cloud-LLM hard gates (mirror the interactive turns route, `web/workspaces.py:790‑801`).** A pass calls a hosted Claude, so the POST enforces the *same* preconditions before enqueueing: `settings.drafting_enabled` (else 403/409 "drafting disabled"); the provider/API key configured (else 409 "AI drafting not configured"); and a prior **per-user `disclosure_acknowledged`** `WorkspaceEvent` for this workspace (else 409/428 prompting the disclosure modal — the SPA reuses the existing `POST .../disclosure` ack flow). Same gate set the chat turn submit uses; do not invent a weaker one.
- **`GET /api/workspaces/{id}/coherence-runs/latest`** — the most recent job or `null`: `{id, status, n_proposals, error, started_at, completed_at}`. The SPA polls this while `status ∈ {queued, running}`; it also restores state on reload.

Router mounted in `src/call_manager/main.py` (≈`:530`) alongside the proposed-changes router.

### 3.2 Frontend

- `frontend/src/api/coherence.ts` (+ schema regen via `EMBEDDING_MODEL=bge-m3 pnpm gen:api`): `useStartCoherence(id)` (POST) and `useLatestCoherence(id)` (GET, `refetchInterval` while queued/running, else disabled). On a run transitioning to `completed`, invalidate **both** inbox queries `["proposed-changes", id, "spec"]` and `["proposed-changes", id, "draft"]` so the existing `<ProposalReview>` strips refresh in place.
- A **"Check coherence" control in the Spec panel header** (`frontend/src/cockpit/SpecPanel.tsx`, next to the lock controls — the spec is the coherence anchor) with a compact status line: idle → button; `running` → disabled + "Checking coherence…"; `completed` → "Last check: N proposal(s)"; `failed` → error notice + retry. Resulting proposals appear in the Spec **and** Draft inboxes already built — no new review UI. (A cockpit-level placement can move it when the fuller cockpit chrome lands.)

---

## 4. Testing & risks

### 4.1 Tests

All agent-pass tests use a **stub adapter** that emits scripted tool calls — no real Claude/network in CI.

**Backend (pytest + testcontainers):**
- **Schema/migration:** `coherence_jobs` created; the active-unique index rejects a second active (`queued` *or* `running`) row for one workspace (IntegrityError) while allowing a new enqueue once the prior job is `completed`/`failed`; migration up/down.
- **Tick/claim:** claims oldest `queued`; skips a workspace with a `running` job; `FOR UPDATE SKIP LOCKED`.
- **Pass handler:** stub emits `propose_edit`/`propose_spec_edit` → rows land with `origin_kind="coherence_session"`, `origin_job_id=job.id`, `created_by=requested_by`; job → `completed`, `n_proposals` set, `sdk_session_id` persisted.
- **Supersede:** seed a prior coherence-pending (old `origin_job_id`) + an interactive-pending + a human row; run a new pass → old coherence → `superseded`, **interactive/human untouched**, this pass's rows stay pending.
- **Lock-aware toolset:** assert the assembled allowlist *excludes* `propose_spec_edit` when locked, *includes* it when unlocked.
- **Failure cleanup:** stub emits one proposal then raises → job `failed` + `error` set; **this job's** emitted row → `superseded`; a seeded **prior** coherence-pending row stays `pending` (inverse of the success path).
- **Stuck-job reaper:** a `running` job with `started_at` older than `coherence_job_timeout_seconds` → flipped `failed` by the tick (+ its partial rows superseded), which frees a fresh enqueue for that workspace.
- **Cost cap:** seed today's `coherence_jobs` token sum at/over `coherence_daily_token_cap` → the next pass is marked `failed` ("budget reached") with **no adapter call** (stub asserts it was never launched); a completed pass records `input_tokens`/`output_tokens`/`model`.
- **POST hard gates:** `drafting_enabled=False` → blocked; missing per-user `disclosure_acknowledged` → blocked (mirrors the chat-turn gate); provider-unconfigured → blocked.
- **Session isolation:** the pass does not resume/mutate `Workspace.last_sdk_session_id`; uses its own adapter/ToolContext (assert via the stub that no interactive session id is read/written).
- **Provenance regression:** the interactive propose path still stamps `origin_kind="interactive_turn"` (ToolContext defaults unchanged).
- **Endpoints:** POST create-or-return-active (2nd POST returns the same job); GET-latest shape + `null` when none; non-member → 403; `/api` envelope.
- **Staleness interop (light):** a coherence proposal whose `base_section_sha` then moves flips `stale` on the next inbox list (existing machinery).

**Frontend (vitest + `tsc --noEmit` gate):** `useStartCoherence`/`useLatestCoherence` (POST starts; GET polls; `completed` → invalidates both inboxes); Spec-panel button states.

### 4.2 Risks

- **Over-eager / low-value proposals** — deterministic supersede + a conservatism-instructed prompt; residual ones the user rejects. Acceptable v1.
- **Cost/runaway** — manual-only + a **separate per-workspace daily coherence token cap** (§2.6, since the chat-turn cap can't see coherence) + one-pass-per-workspace guard. No auto-trigger.
- **Wedged workspace** — the active-unique index counts `running`, so a crash mid-pass could block all future passes; the §1.3 **timeout reaper** is the required escape hatch (verified by the reaper test).
- **Leaked partial proposals on failure** — addressed by §2.5 failure cleanup (supersede this job's own pending rows).
- **Cross-session contamination** — the pass uses its **own** one-shot adapter/MCP/ToolContext, never `WorkspaceController`, and never touches `last_sdk_session_id` (§2.2).
- **Proposals racing user edits** — existing `base_section_sha` staleness; born-stale surfaces as `stale` (§6 of the overarching design). No new mechanism.
- **Holistic drift with no concrete edit** — **not surfaced in v1** (no Problems panel); deferred to SP2b-3. Known, accepted limitation.
- **Single-instance scheduler** — the tick respects `max_instances=1`; no new multi-instance exposure.

---

## 5. File-touch map

**Backend — create:**
- `src/call_manager/proposal_assist/coherence/__init__.py`
- `src/call_manager/proposal_assist/coherence/context.py` — `assemble_coherence_context`
- `src/call_manager/proposal_assist/coherence/jobs.py` — enqueue (create-or-return-active), claim, reaper (timeout→failed), daily-cap check, supersede (success + failure variants), status helpers
- `src/call_manager/proposal_assist/coherence/pass.py` — `run_coherence_pass`, `run_coherence_tick`
- `src/call_manager/proposal_assist/web/coherence.py` — the two endpoints
- a new Alembic migration (down_rev `0052`) — the `coherence_jobs` table + indexes

**Backend — modify:**
- `src/call_manager/db/models.py` — `CoherenceJob` model
- `src/call_manager/scheduler.py` — register `_coherence_tick`
- `src/call_manager/proposal_assist/tools/registry.py` — `ToolContext.origin_kind` + `origin_job_id`
- `src/call_manager/proposal_assist/tools/proposal.py` — pass `ctx.origin_kind`/`ctx.origin_job_id`
- `src/call_manager/proposal_assist/drafting/claude_adapter.py` (+ `factory.py`) — optional `allowed_tools` override; keeper one-shot adapter/MCP/ToolContext (NOT via `WorkspaceController`)
- `src/call_manager/config.py` — `coherence_tick_seconds`, `coherence_job_timeout_seconds`, `coherence_daily_token_cap` settings (alongside `drafting_enabled:94`, `phase0_tick_seconds:122`)
- `src/call_manager/main.py` (≈`:530`) — mount the coherence router

**Frontend — create:** `frontend/src/api/coherence.ts` (+ `coherence.test.ts`).
**Frontend — modify:** `frontend/src/cockpit/SpecPanel.tsx` (button + status); `frontend/src/api/schema.gen.ts` (regen); a small CSS add.

---

## 6. Build decomposition

A single plan is appropriate; it is **backend-heavy** (substrate + pass + endpoints) with a thin frontend cap. The writing-plans step will sequence it TDD task-by-task. A natural ordering: (1) `coherence_jobs` model + migration (incl. token/model columns + active-unique index); (2) `ToolContext` provenance fields + propose plumbing (with the interactive regression test); (3) context-assembly; (4) allowlist override + the keeper's one-shot adapter/ToolContext; (5) pass handler + success-supersede + **failure cleanup** (stub adapter); (6) jobs helpers — **reaper + daily-cap check**; (7) tick + scheduler wiring; (8) endpoints (incl. the **drafting_enabled / provider / disclosure-ack gates**); (9) frontend hooks + Spec-panel control + schema regen. No "no-green-intermediate" rewrites are anticipated (the provenance change is additive via defaults).

## 7. Out of scope (SP2b-3 and beyond)

Continuous evaluator + Scorecard; the Problems/drift panel; on-save/debounced/auto triggering; prompt-cache-control wiring; non-edit "drift findings"; cockpit-level placement of the trigger; per-group provider routing (cross-cutting, unscheduled).
