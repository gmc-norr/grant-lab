# SP2b-3d — Uncited-claims (LLM) Problems signal — Design

> Follow-on slice deferred from SP2b-3b (Problems panel). The Activity feed is a
> separate sibling slice (SP2b-3e), out of scope here.

**Goal:** Add an LLM-backed "uncited claims" check to the cockpit: a manual,
disclosure-gated background pass scans the DRAFT for factual claims lacking a
citation and surfaces them as a 5th Problems signal (per-claim, dismissable).

**Architecture:** A faithful **copy-not-generalize clone of the evaluator**
(`proposal_assist/evaluator/`) — its own package, job table, endpoints, and
scheduler ticks — plus a new pure builder in the existing `problems/` package
and small additions to the React `ProblemsPanel`. No generalization of the
shared job machinery (the three siblings coherence/evaluator/review already
prove that copying beats premature abstraction here).

**Tech stack:** Python 3.12 / FastAPI / SQLAlchemy 2.x / Alembic / APScheduler
(single-instance) / Claude Agent SDK adapter (hosted `claude-opus-4-7`) on the
backend; React 18 + Vite + TanStack Query + openapi-fetch on the frontend.

---

## §0 Locked decisions (brainstorm 2026-06-22)

1. **Separate background job**, evaluator-shaped: manual "run now", disclosure-
   gated, daily **token** cap, one active job per workspace. (Not folded into the
   evaluator pass; not auto-run.)
2. **Per-claim Problems rows**, capped at **10** with a `"+ N more"` roll-up tail
   row; severity = **warning**.
3. **Dismissable + persistent** across re-runs. Keyed by
   `claim_hash = sha256(normalize(claim_text))`. **Reuses the existing
   `WorkspaceEvent kind="user_claim_dismissed"`**, `payload={"claim_hash": …}`
   — no dismissals table. Editing a claim changes its hash → it re-surfaces.
4. **Section-level deep-link** only (`top_tab:"draft"`, `section_index`) — no
   exact char-offset highlight in v1 (matches the scorecard's `criterion_gap`).
5. **Results stored as JSONB** on the job row (no per-claim table).
6. Soft-delete handled exactly as the evaluator does — the claim query excludes
   non-live workspaces; **no `cancelled` status, no explicit cancel hook**.

---

## §1 Data model — migration `0055_uncited_claims_jobs` (head is 0054)

New table **`uncited_claims_jobs`**, mirroring `evaluator_jobs` (0054) but
dropping the spec/rubric staleness markers (this check depends only on the
draft) and replacing `scorecard` with `claims`:

| column | type | notes |
|---|---|---|
| `id` | UUID pk | |
| `workspace_id` | UUID, FK workspaces ON DELETE CASCADE, NOT NULL | |
| `status` | Text NOT NULL default `'queued'` | CHECK `IN ('queued','running','completed','failed')` (mirror evaluator — no `cancelled`) |
| `requested_by` | BigInt, FK users ON DELETE SET NULL, NULL | |
| `error` | Text NULL | |
| `claims` | JSONB NULL | written by `submit_uncited_claims` while running; read when completed |
| `evaluated_draft_sha` | Text NULL | draft CONTENT sha captured at run start (staleness) |
| `queued_at` | timestamptz NOT NULL default now() | |
| `started_at` | timestamptz NULL | |
| `completed_at` | timestamptz NULL | |
| `sdk_session_id` | Text NULL | |
| `model` | Text NULL | |
| `input_tokens` | Integer NOT NULL default 0 | |
| `output_tokens` | Integer NOT NULL default 0 | |

Indexes (mirror evaluator): `ix_uncited_claims_jobs_ws_queued (workspace_id,
queued_at)`; partial-unique `uq_uncited_claims_jobs_active (workspace_id) WHERE
status IN ('queued','running')` → one active job per workspace. SQLAlchemy
`UncitedClaimsJob` model added to `db/models.py` mirroring `EvaluatorJob`.

**`claims` JSONB shape** — list of objects:
```json
{ "claim_text": "<verbatim quote from the draft>",
  "section_index": 2,
  "reason": "<why this assertion needs a citation>",
  "claim_hash": "<sha256 hex of normalized claim_text>" }
```
`claim_hash` is computed **server-side** in the pass runner (never trusted from
the model). `normalize` = lowercase, collapse internal whitespace, strip.

**No dismissals table.** Dismissed hashes = `SELECT DISTINCT payload->>'claim_hash'
FROM workspace_events WHERE workspace_id=? AND kind='user_claim_dismissed'`.

---

## §2 What counts as an "uncited claim" (prompt contract)

The pass receives the **draft text including its existing citation markers** and
the section structure (so the model can see what is already cited and report a
`section_index`). The model is instructed to flag **factual / empirical
assertions** — statistics, prior-work claims, mechanism/efficacy claims,
comparative claims — that lack an adjacent citation marker, and to **EXCLUDE**:

- the applicant's own proposed / novel work and aims,
- common knowledge and definitional statements,
- assertions already adjacent to a citation marker.

For each flagged claim the model returns the **verbatim claim text** (so it's
locatable and gives a stable hash), the `section_index`, and a one-line
`reason`. The system role is delivered in the instruction (the adapter has no
`system_prompt` param — same as evaluator/review).

---

## §3 `proposal_assist/uncited_claims/` package (clone evaluator)

- **`jobs.py`** — `enqueue_uncited_claims_run` (create-or-return-active, SAVEPOINT
  race-safe), `claim_next_uncited_claims_job` (oldest queued, workspace not
  already running, workspace live, `FOR UPDATE SKIP LOCKED`, flip→running +
  `started_at`), `reap_stuck_uncited_claims_jobs` (fail running past TTL, roll up
  tokens), `latest_uncited_claims_job`, `uncited_claims_cap_exceeded` (in+out
  tokens today ≥ cap), and `dismissed_claim_hashes(session, workspace_id) -> set[str]`.
- **`context.py`** — `assemble_uncited_claims_context` returning the draft text
  (with markers), section list, and `draft_sha` (content sha). Reuse the
  evaluator/review context helpers (`content_sha`, draft read) — copy bodies if a
  cross-package import is disallowed by lint; never edit the evaluator file.
- **`agent.py`** — `build_uncited_claims_adapter`: `ClaudeAgentSDKAdapter` with a
  `ToolContext` carrying `job_id`/`workspace_id`, `allowed_tools=["submit_uncited_claims"]`.
- **`pass_runner.py`** — `run_uncited_claims_pass`: short read session (guard
  running, pin `evaluated_draft_sha`, cap recheck) → build instruction → launch +
  drive inside `asyncio.wait_for` → bounded `terminate` in `finally` → row-locked
  finalize (still-running guard). Carries the hardening proven in review:
  pre-launch live recheck, reaper token roll-up, **late-write parent-run lock**
  (if the job is no longer running at finalize, don't resurrect it; still roll up
  tokens). `drain_uncited_claims_queue_tick`.

The runner validates the submitted list (each `section_index` in range,
`claim_text` non-empty), **dedupes by `claim_hash`**, computes hashes, and writes
the `claims` JSONB + `status='completed'`.

---

## §4 `submit_uncited_claims` tool

`tools/uncited_claims.py` — `submit_uncited_claims_impl(ctx, claims=[...])`,
registered in `tools/registry.py` exactly like `submit_scorecard` /
`submit_review` (forced, write-only, callable only while the job is `running`).
JSON schema: `{claims: [{claim_text: string (required, non-empty),
section_index: integer (required), reason: string (required)}]}`. Impl validates,
writes to the job's `claims` (hashes added by the runner, not the model), returns
an MCP ok/error envelope via the existing `_safe_tool` wrapper.

---

## §5 Problems integration — the 5th signal

- **`problems/builders.py`**: new pure builder
  `uncited_claim_problems(claims: list[dict], dismissed: set[str], *,
  stale: bool, cap: int = 10) -> list[dict]`. Filters out claims whose
  `claim_hash ∈ dismissed`; emits up to `cap` **warning** rows
  (`category="uncited_claim"`), each:
  ```json
  { "category":"uncited_claim", "severity":"warning",
    "title": "<truncated claim text>", "detail": "<reason>",
    "claim_hash": "<hash>",  // so the panel can dismiss it
    "target": {"top_tab":"draft", "section_index": N, "bottom_tab":"problems"} }
  ```
  If more than `cap` remain, append one roll-up tail row
  (`title="+ N more uncited claims"`, no `claim_hash`, no per-claim target).
  `claim_hash` is added to the row shape (additive — existing rows omit it).
- **`problems/aggregate.py`**: in its own `try` block (never-500), read the
  latest **completed** `uncited_claims_jobs` row + `dismissed_claim_hashes`,
  compute `stale = job.evaluated_draft_sha != current draft content_sha`, call the
  builder, and report staleness alongside the existing `scorecard_stale`
  mechanism (add the `"uncited_claims"` source to the staleness output the panel
  reads to render "may be outdated"). A missing/failed/empty job contributes no
  rows.

---

## §6 Endpoints — `proposal_assist/web/uncited_claims.py`

All under `/api/workspaces`, member-gated, with **`response_model`s** (lesson
from SP2b-3c: bare-dict endpoints emit no schema and hide client/server drift):

- `POST /{id}/uncited-claims-runs` → enqueue. `gate_cloud_llm(disclosure_action=
  "checking for uncited claims")`; **active-first** (return the active job if one
  exists, else cap-check then enqueue + write a `WorkspaceEvent`); 429 on cap.
  Returns the run DTO.
- `GET /{id}/uncited-claims-runs/latest` → latest job state + `claims` +
  `stale` flag (drives the panel's run button + status). Returns DTO or `null`.
- `POST /{id}/uncited-claims/{claim_hash}/dismiss` → writes
  `WorkspaceEvent(kind="user_claim_dismissed", actor_id=user.id,
  payload={"claim_hash": claim_hash})`. Idempotent (dismissing an unknown hash is
  harmless). Returns `{ok: true}`. (No un-dismiss in v1.)

Router imported/included in `main.py`.

---

## §7 Config + scheduler

- **`config.py`** (mirror evaluator block): `uncited_claims_daily_token_cap`
  (default matching evaluator's), `uncited_claims_job_timeout_seconds`,
  `uncited_claims_reaper_tick_seconds`, `uncited_claims_terminate_timeout_seconds`.
  The pass uses `proposal_assist_llm_model` (hosted Opus), like the siblings.
- **`scheduler.py`**: add `uncited_claims_drain` + `uncited_claims_reaper` ticks
  following the evaluator dispatch pattern. Single-instance constraint unchanged.

---

## §8 Frontend

- **`api/uncitedClaims.ts`**: `useLatestUncitedClaimsRun(id)` (poll 4s while
  queued/running), `useStartUncitedClaimsRun(id)` (POST; invalidate problems +
  latest on success), `useDismissClaim(id)` (POST dismiss; invalidate problems).
  DTOs from the **generated** `schema.gen.ts` (regen with
  `EMBEDDING_MODEL=bge-m3 pnpm gen:api`).
- **`cockpit/ProblemsPanel.tsx`**: header gains a **"Check uncited claims"**
  run-now button + last-run/stale status (from `useLatestUncitedClaimsRun`;
  disabled while running / on cap error, which stays retryable per the SP2b-3c
  lesson). For `category==="uncited_claim"` rows, render a **"Dismiss"** action
  next to "Go to" (calls `useDismissClaim`, button stays enabled). Rows arrive via
  the existing `/problems` poll — no separate fetch for the rows themselves.
- The `"+ N more"` roll-up row renders as plain text (no actions).

No new bottom-panel tab (uncited claims live inside Problems). `nav.ts` unchanged.

---

## §9 Testing (mirror evaluator/review suites)

- **jobs**: enqueue create-or-return-active; claim flips/excludes running +
  non-live workspaces; reap fails-and-rolls-up; cap-exceeded; `dismissed_claim_hashes`.
- **pass_runner**: happy path (claims written + hashed + deduped, status
  completed); submit-tool validation (bad section_index / empty text rejected);
  stale-after-edit; reaper + late-write parent-lock guard (clone the review tests).
- **submit tool**: schema validation, only-while-running, MCP envelope.
- **builder**: per-claim rows; cap + `"+ N more"` tail; dismissed filtered out;
  stale flag.
- **aggregate**: 5th signal composes; never-500 when the job table errors;
  staleness reported.
- **endpoints**: run (gating 403/409, cap 429, active-first), latest (shape +
  null), dismiss (writes event, idempotent); `response_model` drift-guard test.
- **frontend (vitest)**: ProblemsPanel renders uncited rows, dismiss invalidates,
  run button disabled-while-running + retryable-on-error; hook tests. `tsc` gate.

Full-suite is testcontainer-flaky → run affected files, re-run connection-refused
in isolation.

---

## §10 File-touch map

**Create**
- `alembic/versions/0055_uncited_claims_jobs.py`
- `src/call_manager/proposal_assist/uncited_claims/{__init__,jobs,context,agent,pass_runner}.py`
- `src/call_manager/proposal_assist/tools/uncited_claims.py`
- `src/call_manager/proposal_assist/web/uncited_claims.py`
- `frontend/src/api/uncitedClaims.ts`
- tests: `tests/proposal_assist/uncited_claims/test_{jobs,pass_runner,submit,context}.py`,
  `tests/proposal_assist/problems/test_uncited_claim_builder.py`,
  `tests/proposal_assist/test_web_uncited_claims.py`,
  `frontend/src/cockpit/ProblemsPanel.test.tsx` (extend)

**Modify**
- `src/call_manager/db/models.py` (+`UncitedClaimsJob`)
- `src/call_manager/proposal_assist/tools/registry.py` (register tool)
- `src/call_manager/proposal_assist/problems/{builders,aggregate}.py`
- `src/call_manager/proposal_assist/web/problems.py` (only if the response shape
  gains the staleness source / `claim_hash` on rows)
- `src/call_manager/config.py`, `src/call_manager/scheduler.py`, `src/call_manager/main.py`
- `frontend/src/cockpit/ProblemsPanel.tsx`, `frontend/src/api/schema.gen.ts` (regen)

**Must NOT touch:** `drafting/claude_adapter.py`, `drafting/contract.py`, the
evaluator/coherence/review packages (copy from them, don't edit them).

---

## §11 Build decomposition (preview for writing-plans)

1. Migration + `UncitedClaimsJob` model.
2. `jobs.py` (queue/caps/reap/dismissed-set) + tests.
3. `submit_uncited_claims` tool + registry + tests.
4. `context.py` + `agent.py` + `pass_runner.py` (with hardening) + tests.
5. Problems builder + aggregate wiring + tests.
6. Endpoints (run/latest/dismiss + response_models) + config + scheduler ticks + tests.
7. Frontend (hooks + ProblemsPanel run button + dismiss) + regen + tsc/vitest.

---

## §12 Out of scope (deferred)

- The **Activity feed** (separate sibling slice, SP2b-3e).
- Exact in-editor claim highlighting (section-level deep-link only).
- Un-dismiss / dismissal management UI.
- Auto-run on draft change.
