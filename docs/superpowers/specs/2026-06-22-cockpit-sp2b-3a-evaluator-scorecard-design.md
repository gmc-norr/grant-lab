# Cockpit SP2b-3a — Continuous Evaluator + Scorecard — Design Spec

> Sub-project 2 (Workspace Cockpit), slice SP2b-3a. Follows SP2b-2c (coherence-keeper,
> merged to master `e145f84`). Overarching design:
> `docs/superpowers/specs/2026-06-17-cockpit-workspace-design.md` (§8 Spec, §10 Coverage/Scorecard).

## 0. Scope & locked decisions

SP2b-3a builds the **readout half** of the SPEC↔DRAFT quality loop whose proposal-emitting
half (the coherence-keeper) shipped in SP2b-2c. It is a **manual-triggered, fresh-stateless
background AI pass** (hosted Opus — `proposal_assist_llm_model`) that reads the **funder
criteria + spec + draft** and emits a **per-criterion qualitative scorecard** (Strong /
Adequate / At-risk / Gap, a specific gap, an optional deep-link to act), plus a
**server-computed overall readiness band**, surfaced as a third **Scorecard** tab in the
cockpit.

**Locked decisions (brainstorm 2026-06-22):**

1. **Trigger = manual-only.** A "Re-evaluate" button, gated exactly like the coherence-keeper
   (phase=active + `drafting_enabled` + disclosure-ack). No auto-debounce (deferred — easy
   fast-follow once the scorecard shape is proven).
2. **Output storage = `evaluator_jobs` row + `scorecard` JSONB.** New table mirroring
   `coherence_jobs`; the completed row carries the scorecard payload. "Latest completed job =
   current scorecard." History-for-free via job rows.
3. **Overall roll-up = deterministic weighted** from the per-criterion bands (the agent never
   emits a number — no false precision).
4. **Substrate reuse = copy the coherence pattern** into a new `evaluator/` package +
   `evaluator_jobs` table. No shared-base refactor of the just-shipped coherence code. (The
   mock-panel — SP2b-3c — would be the 3rd background job and the right moment to extract a
   shared base, if ever.)

**Explicitly NOT in this slice (see §8):** proposals (coherence-keeper's job), the Problems
panel + bottom-panel region (SP2b-3b), the mock-panel persona fan-out (SP2b-3c), auto-debounce,
convergent-mode emphasis flip, per-criterion trend history, evaluator-emitted proposals.

**Foundation already on master (do not rebuild):**

- Funder criteria are live: `CallRubric.criteria` (JSONB `[{name, description, weight,
  scale_max, subcriteria?}]` + `funder_notes`) is snapshotted onto `Workspace.rubric_criteria`
  + `Workspace.rubric_funder_notes` at Phase 0. The coherence-keeper context already reads it.
- The coherence-keeper substrate is the template to copy: `coherence_jobs` model
  (`db/models.py:884`), migration `alembic/versions/0053_coherence_jobs.py`, the
  `proposal_assist/coherence/` package (`context`, `jobs`, `agent`, `pass_runner`), the
  scheduler ticks (`scheduler.py:503` drain + `:539` reaper), the gated endpoints
  (`proposal_assist/web/coherence.py`), and the config block (`config.py:211–251`).
- `ProposedChange.origin_kind` already permits `'evaluator_session'` (`db/models.py:1014`) —
  reserved for a future evaluator-emitted-proposal mode; **unused in 3a**.
- `ToolContext` carries `origin_kind` / `origin_job_id` (`tools/registry.py`); the propose
  tools stamp them. `submit_scorecard` follows the same ToolContext-carries-job_id pattern.

## 1. Data model

### 1.1 `evaluator_jobs` table — migration `0054_evaluator_jobs.py` (down_revision `0053`)

Mirror `CoherenceJob` (`db/models.py:884–921`) column-for-column, **dropping** `n_proposals`
and **adding** the scorecard + staleness columns:

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | `default uuid4` |
| `workspace_id` | UUID | FK → `workspaces.id` **ON DELETE CASCADE**, NOT NULL |
| `status` | Text | CHECK `IN ('queued','running','completed','failed')`, default `'queued'` |
| `requested_by` | BigInteger | FK → `users.id` **ON DELETE SET NULL**, nullable |
| `error` | Text | nullable; sanitized member-visible message on failure |
| `scorecard` | JSONB | nullable; written by `submit_scorecard` while running; read only when `status='completed'` |
| `evaluated_draft_sha` | Text | nullable; draft head at run time (staleness display) |
| `evaluated_spec_sha` | Text | nullable; spec head at run time |
| `queued_at` | DateTime(tz) | server-default `now()` |
| `started_at` | DateTime(tz) | nullable; stamped on claim |
| `completed_at` | DateTime(tz) | nullable; stamped on terminal state |
| `sdk_session_id` | Text | nullable |
| `model` | Text | nullable |
| `input_tokens` | Integer | default 0 |
| `output_tokens` | Integer | default 0 |

**Indexes/constraints (verbatim from coherence):**
- `ck_evaluator_jobs_status` CHECK on the status enum.
- `ix_evaluator_jobs_ws_queued` on `(workspace_id, queued_at)`.
- `uq_evaluator_jobs_active` — partial-unique on `workspace_id` `WHERE status IN
  ('queued','running')` (at most one active evaluator pass per workspace).

The evaluator and coherence active-unique slots are **independent** (separate tables), so a
workspace may have one coherence pass and one evaluator pass in flight simultaneously — fine,
disjoint outputs.

### 1.2 `scorecard` JSONB shape

```jsonc
{
  "criteria": [
    {
      "name": "<exact name from rubric_criteria>",
      "band": "strong" | "adequate" | "at_risk" | "gap",
      "gap": "<one specific paragraph: what is missing or weak for THIS criterion>",
      "deep_link": { "artifact": "draft" | "spec", "section_index": <int> } | null,
      "weight": <float>                // echoed verbatim from the rubric criterion
    }
    // ... one entry per rubric criterion, in rubric order
  ],
  "overall_band": "strong" | "adequate" | "at_risk" | "gap",   // SERVER-computed (§2.4)
  "summary": "<short overall readiness note authored by the agent>"
}
```

The agent supplies `name`, `band`, `gap`, `deep_link`, and `summary`. The server fills `weight`
(echoed from the rubric) and computes `overall_band`. Persisted shape is the union above.

## 2. The evaluator pass — `proposal_assist/evaluator/` package

A near-copy of `proposal_assist/coherence/`. Five modules: `__init__`, `context`, `jobs`,
`agent`, `pass_runner`, plus the `submit_scorecard` tool in the tools layer.

### 2.1 `context.py`

```python
def assemble_evaluator_context(*, session: Session, workspace_id: uuid.UUID,
                               workspaces_dir: Path | str) -> str: ...
```

Mirrors `assemble_coherence_context` (`coherence/context.py:38`). Reads `ws.rubric_criteria`
(names **and weights** — the agent must cover each criterion exactly once and is told the
weights so it understands relative importance), `ws.rubric_funder_notes`, the full
`proposal-spec.md`, and the full `proposal.md`. Assembles a single instruction string:

1. **Role preamble** — "You are the chair of a mock review panel. Assess this proposal against
   each funder criterion below and call `submit_scorecard` exactly once."
2. The funder criteria (name + description + weight + subcriteria) as readable text.
3. Funder notes.
4. Full spec (or "(spec is empty)").
5. Full draft (or "(draft is empty)").

Edge: if `rubric_criteria` is empty, the instruction states there are no criteria; the agent
calls `submit_scorecard` with an empty `criteria` list (handled in §2.3 / §2.4 / §4).

### 2.2 `agent.py`

```python
def evaluator_allowed_tools() -> list[str]:
    return ["read_proposal", "read_spec", "get_call_details", "submit_scorecard"]

def build_evaluator_adapter(*, settings, session_factory, workspace_id, requested_by,
                            job_id) -> ClaudeAgentSDKAdapter: ...
```

Mirrors `coherence/agent.py:32`. A dedicated one-shot `ClaudeAgentSDKAdapter` (never the shared
`WorkspaceController`), no resume. `tools_factory` builds a `ToolContext` with
`origin_kind="evaluator_session"` and `origin_job_id=job_id` so `submit_scorecard` can write to
the right job row. Allowlist is **read tools + `submit_scorecard` only** — no `propose_*`
(the evaluator produces a scorecard, never proposals, in 3a). Uses the optional `allowed_tools`
constructor override on `ClaudeAgentSDKAdapter` (added in SP2b-2c).

### 2.3 `submit_scorecard` tool

Lives with the other proposal-assist tools (alongside `propose_edit` in
`proposal_assist/tools/`), exported through the MCP server builder
(`tools/registry.build_proposal_assist_mcp_server`). Allowlisted **only** for the evaluator
adapter.

Signature the agent sees: `submit_scorecard(criteria: list[{name, band, gap, deep_link?}],
summary: str)`.

Server-side `_submit_scorecard_impl(ctx: ToolContext, *, criteria, summary)`:

1. **Load** the workspace's `rubric_criteria` (the authoritative criterion set + weights).
2. **Validate:**
   - Every rubric criterion name is present **exactly once**; no extra/unknown names. (Empty
     rubric → `criteria` must be empty.) On mismatch → return a tool error telling the agent
     which names are missing/extra (the agent can retry within the turn).
   - Each `band ∈ {strong, adequate, at_risk, gap}`.
   - Each `deep_link`, if present, has `artifact ∈ {draft, spec}` and an in-range
     `section_index` for that artifact's current section count.
3. **Echo** each criterion's `weight` from the rubric into the stored entry.
4. **Compute** `overall_band` deterministically (§2.4).
5. **Write** the assembled `scorecard` JSONB onto the **running** `evaluator_jobs` row
   (`ctx.origin_job_id`), overwriting any prior partial write (last-call-wins). Does **not**
   flip status — `pass_runner` finalize owns that.
6. Return a small success payload to the agent (e.g. `{ "stored": true, "overall_band": ... }`).

(Parsing the agent's final assistant message as JSON was rejected — same rationale as the
coherence-keeper's propose tools: a validated tool call is robust where free-text parsing is
fragile.)

### 2.4 Deterministic overall roll-up

Pure function, unit-tested in isolation:

```
ordinal = {gap: 0, at_risk: 1, adequate: 2, strong: 3}
weight_i = criterion weight, or 1.0 if missing / <= 0
score   = Σ(weight_i * ordinal[band_i]) / Σ(weight_i)
overall = gap      if score ∈ [0.00, 0.75)
          at_risk  if score ∈ [0.75, 1.75)
          adequate if score ∈ [1.75, 2.50)
          strong   if score ∈ [2.50, 3.00]
```

Empty `criteria` → `overall_band = "gap"` and the UI shows the empty-criteria state (§4). The
thresholds are tunable; they live in one constant block and are covered by table-driven tests
(including the exact boundary values 0.75 / 1.75 / 2.50).

### 2.5 `jobs.py`

Copy `coherence/jobs.py` verbatim with `CoherenceJob`→`EvaluatorJob` and the
`coherence_session`/cap settings swapped to evaluator equivalents:

- `enqueue_evaluator_run(session, *, workspace_id, requested_by)` — race-safe create-or-return
  active via the partial-unique index inside a SAVEPOINT (the SP2b-2c enqueue pattern: add +
  flush inside `begin_nested`, expunge on `IntegrityError`, re-read winner).
- `claim_next_evaluator_job(session)` — oldest queued job for a LIVE (not soft-deleted)
  workspace with no running evaluator job; `FOR UPDATE SKIP LOCKED`; flip to running +
  stamp `started_at`.
- `cancel_evaluator_jobs_for_workspace(session, *, workspace_id)` — the **atomic
  `UPDATE ... WHERE status IN ('queued','running') RETURNING id`** form (SP2b-2c Codex fix #1 —
  must NOT be an ORM load-then-write, to avoid clobbering a concurrently-completing job).
- `reap_stuck_evaluator_jobs(session, *, timeout_seconds)` — `FOR UPDATE SKIP LOCKED`, fail
  running jobs older than the TTL.
- `evaluator_cap_exceeded(session, *, workspace_id, settings)` — Stockholm-day sum of
  `input_tokens + output_tokens` ≥ `evaluator_daily_token_cap`.
- `latest_evaluator_job(session, *, workspace_id)` — most recent by `queued_at` (any status).
- `latest_completed_evaluator_job(session, *, workspace_id)` — the most recent **completed**
  job (carrying `scorecard` + `evaluated_draft_sha`/`evaluated_spec_sha` + `completed_at`), or
  `None`. (New helper, no coherence analog; powers `GET /scorecard`.)

There is **no `supersede_*`** logic: the scorecard is a single JSONB on the job row (not a set
of inbox rows), so "latest completed wins" is just a read-time `ORDER BY queued_at DESC` over
completed jobs. (Cancellation/reaper simply fail the job; nothing to supersede.)

### 2.6 `pass_runner.py`

Copy `coherence/pass_runner.py` skeleton with all SP2b-2c hardening intact:

`run_evaluator_pass(*, job_id, session_factory, settings, adapter_builder=build_evaluator_adapter)`:

1. **Short read-session:** load job (return if not `running`); read `requested_by`; compute and
   store `evaluated_draft_sha` / `evaluated_spec_sha` (the artifact heads now); check
   `evaluator_cap_exceeded` (fail pre-run if over); assemble the instruction.
2. **Launch + drive bounded together** in one `asyncio.wait_for(_run(), timeout=
   settings.evaluator_pass_timeout_seconds)` — `_run()` does `launch` then streams `send_turn`
   (SP2b-2c fix #2: launch is INSIDE the wait_for). Any exception → fail the job with a
   sanitized message (distinguish timeout), return.
3. **`finally`:** bounded `await asyncio.wait_for(adapter.terminate(handle),
   timeout=settings.evaluator_terminate_timeout_seconds)`.
4. **Finalize (success):** re-read job `FOR UPDATE`; guard `status=='running'` (reaper may have
   failed it during a hang); **require a `scorecard` was written** by the tool — if `scorecard
   IS NULL`, fail the job with "evaluator produced no scorecard"; else flip `completed`, stamp
   `completed_at`, record tokens/model/session.

`drain_evaluator_queue_tick(*, session_factory, settings, adapter_builder=...)` — claim one job
in a short session + commit, then `run_evaluator_pass`. (Reaper is the separate sync tick.)

## 3. Endpoints, scheduler, config

### 3.1 Endpoints — `proposal_assist/web/evaluator.py` (router `prefix="/api/workspaces"`, mounted in `main.py`)

- `POST /api/workspaces/{workspace_id}/evaluator-runs` — member-gated; `_gate_cloud_llm`
  (reuse/mirror coherence's: `current_phase=='setup'`→**409**, `not drafting_enabled`→**503**,
  missing disclosure-ack→**403**); create-or-return-active via `enqueue_evaluator_run`; respond
  with the serialized job (`id, status, error, started_at, completed_at`).
- `GET /api/workspaces/{workspace_id}/evaluator-runs/latest` — member-gated; `latest_evaluator_job`
  serialized (or `null`). Drives the run-status chip + polling.
- `GET /api/workspaces/{workspace_id}/scorecard` — member-gated; serializes
  `latest_completed_evaluator_job` as `{ scorecard, evaluated_draft_sha, evaluated_spec_sha,
  completed_at }` (or `null` when no completed run exists). Separate from `/latest` so a
  *failed* re-run keeps showing the last good scorecard.

The two reads are split so the frontend can poll run status on an interval while caching the
(heavier, change-rarely) scorecard separately.

### 3.2 Scheduler — `scheduler.py`

`_evaluator_tick` (async drain) and `_evaluator_reaper_tick` (sync), registered next to the
coherence ticks, mirroring them exactly:
- drain: `IntervalTrigger(seconds=settings.evaluator_tick_seconds)`, skip when `not
  drafting_enabled`, dispatch via `run_coroutine_threadsafe(coro, app_loop)` with
  `fut.result(timeout=evaluator_pass_timeout_seconds + 60)`, `asyncio.run` fallback when
  `app_loop is None`.
- reaper: `IntervalTrigger(seconds=settings.evaluator_reaper_tick_seconds)`, pure-DB
  `reap_stuck_evaluator_jobs(s, timeout_seconds=settings.evaluator_job_timeout_seconds)`.

### 3.3 Config — `config.py` (clone the coherence block with `evaluator_` prefix)

| Setting | Default | Alias |
|---|---|---|
| `evaluator_tick_seconds` | 20 | `EVALUATOR_TICK_SECONDS` |
| `evaluator_pass_timeout_seconds` | 180 | `EVALUATOR_PASS_TIMEOUT_SECONDS` |
| `evaluator_job_timeout_seconds` | 300 | `EVALUATOR_JOB_TIMEOUT_SECONDS` |
| `evaluator_reaper_tick_seconds` | 60 | `EVALUATOR_REAPER_TICK_SECONDS` |
| `evaluator_terminate_timeout_seconds` | 30 | `EVALUATOR_TERMINATE_TIMEOUT_SECONDS` |
| `evaluator_daily_token_cap` | 200_000 | `EVALUATOR_DAILY_TOKEN_CAP` |

### 3.4 Soft-delete

The workspace soft-delete handler (`proposal_assist/web/workspaces.py`, which already calls
`cancel_coherence_jobs_for_workspace`) also calls `cancel_evaluator_jobs_for_workspace`.

## 4. Frontend — Scorecard as a third top-level tab

### 4.1 `api/evaluator.ts`

- `useLatestEvaluator(id)` — `GET .../evaluator-runs/latest`; `refetchInterval` while
  `status ∈ {queued, running}` (mirror `useLatestCoherence`, 1500 ms).
- `useStartEvaluator(id)` — `POST .../evaluator-runs`; on settle, invalidate `latest` + the
  scorecard query.
- `useScorecard(id)` — `GET .../scorecard`; refetched when a run completes.

Regenerate `schema.gen.ts` with `EMBEDDING_MODEL=bge-m3 pnpm gen:api` after the endpoints land.

### 4.2 `routes/Cockpit.tsx`

Extend the artifact tab strip from `Draft | Spec` to `Draft | Spec | Scorecard`. The Scorecard
is a sibling view (not an artifact in the editor sense); `artifact` state becomes a 3-value
view selector (`"draft" | "spec" | "scorecard"`). When `scorecard` is selected, render
`<ScorecardPanel id={id} />`.

### 4.3 `cockpit/ScorecardPanel.tsx`

- **Header:** overall readiness band pill + `[ Re-evaluate ]` button + run-status chip
  (idle / running… / failed: <msg>). While running, poll; on completion, refetch scorecard.
- **Staleness line:** "evaluated <completed_at>"; if `evaluated_draft_sha !== useDraft(id).head`,
  append a "⚠ draft changed since" hint.
- **Criteria list:** sorted by `weight` desc; each row = band pill + criterion name + weight +
  optional "Go to §" deep-link; the `gap` paragraph under the row.
- **Band → token mapping:** strong → `--cm-good-*` (green), adequate → `--cm-neutral-*`,
  at_risk → `--cm-warn-*` (amber), gap → `--cm-bad-*` (red). Reuse the existing pill style from
  `.source-viewer-stance` (`styles/cockpit.css:94`).
- **Empty states:** no completed run yet → "No evaluation yet — Re-evaluate to generate a
  scorecard." Empty rubric → "This call has no extracted criteria to evaluate."
- **Gate errors:** surface 409/503/403 from the POST inline (no new `current_phase` exposure —
  consistent with the coherence button, which also relies on server gating).

### 4.4 Deep-link

"Go to §" calls back into `Cockpit.tsx` to switch the view to the target `artifact`
(draft/spec) and best-effort `scrollIntoView` the section anchor the editor renders. **Floor:**
if the editor exposes no stable per-section anchor, 3a ships the tab-switch only and
section-scroll is a thin follow-up. (Confirm the editor's section-anchor contract during
planning — `ArtifactSectionEditor`.)

## 5. Testing & risks

### 5.1 Tests (mirror `tests/proposal_assist/coherence/`, new dir `…/evaluator/`)

- **Model/migration:** `uq_evaluator_jobs_active` rejects a second active row.
- **Context:** assembles criteria (with weights) + funder notes + spec + draft; handles missing
  artifact files + empty rubric.
- **`submit_scorecard` tool:** rejects missing criterion / extra-unknown criterion / bad band /
  out-of-range deep-link; echoes weights; writes the JSONB onto the running job; last-call-wins.
- **Roll-up:** table-driven band/weight combinations → expected overall, including the exact
  threshold boundaries (0.75 / 1.75 / 2.50) and the all-equal-weight and missing-weight cases.
- **Jobs:** enqueue create-or-return-active; claim skips running + soft-deleted; cap blocks
  over budget; reaper fails stale; cancel atomic-update leaves a completed job untouched
  (the SP2b-2c race test, ported).
- **Pass runner:** success writes scorecard + completes + records tokens; missing-scorecard →
  failed; failure path; in-pass timeout on launch and on drive both fail + terminate.
- **Endpoints:** POST gates (409/503/403) + create-or-return-active + non-member 403; latest
  null-then-job; scorecard null-then-value; a failed re-run still returns the prior scorecard.
- **Scheduler wiring:** drain + reaper registered with the right intervals; drain respects
  `drafting_enabled`.
- **Soft-delete:** cancels active evaluator jobs.
- **Frontend (vitest/jsdom):** ScorecardPanel renders bands from a fixture, sorts by weight,
  shows empty + empty-rubric states, Re-evaluate fires the POST, deep-link switches the view,
  staleness hint appears when shas differ. `pnpm exec tsc --noEmit` is the type gate.

### 5.2 Risks & mitigations

- **Agent omits/invents a criterion** → `submit_scorecard` validates against the rubric set
  (cover each exactly once; reject unknowns) and returns a corrective error the agent can retry
  within the turn. A pass that never produces a valid scorecard ends `failed` (finalize guard).
- **Empty rubric** → empty scorecard + dedicated empty-criteria UI state (not an error).
- **Cost** → manual-only + per-workspace Stockholm-day token cap (`evaluator_daily_token_cap`).
- **Scorecard staleness** → store + display `evaluated_draft_sha`/`completed_at`; "changed
  since" hint compares to the live draft head.
- **Concurrency** → evaluator and coherence have independent active-unique slots; they may run
  together with disjoint outputs. The drain runs one evaluator pass per tick (sequential).
- **Determinism** → LLM bands vary run-to-run; acceptable (the scorecard is advisory, and the
  deterministic roll-up makes the overall reproducible given the per-criterion bands).

## 6. File-touch map

**New (backend):**
- `alembic/versions/0054_evaluator_jobs.py`
- `src/call_manager/proposal_assist/evaluator/__init__.py`
- `src/call_manager/proposal_assist/evaluator/context.py`
- `src/call_manager/proposal_assist/evaluator/jobs.py`
- `src/call_manager/proposal_assist/evaluator/agent.py`
- `src/call_manager/proposal_assist/evaluator/pass_runner.py`
- `src/call_manager/proposal_assist/web/evaluator.py`
- the `submit_scorecard` tool (new file in `proposal_assist/tools/`, e.g. `scorecard.py`)

**Modified (backend):**
- `src/call_manager/db/models.py` — `EvaluatorJob` model.
- `src/call_manager/config.py` — six `evaluator_*` settings.
- `src/call_manager/scheduler.py` — `_evaluator_tick` + `_evaluator_reaper_tick`.
- `src/call_manager/proposal_assist/tools/registry.py` — register `submit_scorecard` in the MCP
  server builder.
- `src/call_manager/main.py` — mount the evaluator router.
- `src/call_manager/proposal_assist/web/workspaces.py` — soft-delete also cancels evaluator jobs.

**New (frontend):**
- `frontend/src/api/evaluator.ts`
- `frontend/src/cockpit/ScorecardPanel.tsx`

**Modified (frontend):**
- `frontend/src/routes/Cockpit.tsx` — third view tab.
- `frontend/src/styles/cockpit.css` — scorecard + band-pill styles.
- `frontend/src/api/schema.gen.ts` — regenerated.

## 7. Build decomposition (for the implementation plan)

TDD tasks, each independently testable, roughly in dependency order:

1. `EvaluatorJob` model + migration `0054` + active-unique test.
2. `evaluator_daily_token_cap` + the five timeout/tick settings in `config.py`.
3. `evaluator/context.assemble_evaluator_context` (+ tests).
4. The deterministic roll-up pure function (+ table-driven tests).
5. `submit_scorecard` tool + registry wiring (+ validation/echo/write tests).
6. `evaluator/jobs.py` helpers (enqueue/claim/cancel-atomic/reap/cap/latest/latest_completed) (+ tests).
7. `evaluator/agent.build_evaluator_adapter` + `evaluator_allowed_tools` (+ tests).
8. `evaluator/pass_runner` (run + drain) with the SP2b-2c hardening (+ success/missing-scorecard/timeout tests).
9. `web/evaluator.py` endpoints + `main.py` mount + gating (+ endpoint tests).
10. `scheduler.py` drain + reaper ticks (+ wiring test); soft-delete cancellation hook (+ test).
11. Frontend: `api/evaluator.ts` + `ScorecardPanel.tsx` + `Cockpit.tsx` third tab + CSS + schema regen (+ vitest).

## 8. Out of scope (later slices)

- **SP2b-3b** — Problems panel + the bottom-panel region (relocate the proposals inbox + add
  Problems aggregation: deterministic checks — over-length §, broken citations, uncited claims —
  plus evaluator/coherence-derived drift + criteria gaps) + Activity.
- **SP2b-3c** — the on-demand full mock-panel persona fan-out (the dormant `ReviewRun` /
  `ReviewRunItem` tables): `review_personas.yaml`, persona prompts, executor, `fit-check.md` /
  `rubric-review.md` artifacts, trigger endpoint + UI.
- Auto-debounce trigger; convergent-mode emphasis flip (Partner→Evaluator role label,
  Coverage/Readiness defaulted forward at spec-lock); per-criterion trend history; the
  evaluator emitting actionable proposals (the reserved `evaluator_session` origin_kind).
