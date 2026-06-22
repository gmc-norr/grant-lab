# Cockpit SP2b-3c — On-demand Mock-Panel Review — Design Spec

The last decomposed slice of SP2b-3. Adds an explicit **"Run full mock-panel review now"**
action: a one-shot, ~6-call hosted-Opus fan-out (fit-check + 4 personas + consolidation) that
writes two markdown artifacts (`fit-check.md`, `rubric-review.md`) into the workspace git repo.
It complements the continuous evaluator (SP2b-3a) — the evaluator is the "mock-panel made
continuous" (cheap, debounced, per-criterion bands); this is the deeper, expensive, deliberate
review the panel survives as.

Overarching design: `docs/superpowers/specs/2026-06-17-cockpit-workspace-design.md` §10 + §11 item 9.
Templates mirrored: the coherence-keeper (SP2b-2c) and evaluator (SP2b-3a) background jobs, and
the SP2b-3b Problems read-model/defensive style.

---

## 0. Scope & locked decisions

- **Architecture (the deferred fork) → TARGETED EXTRACTION.** The only verbatim-duplicated thing
  across the two merged siblings is `_gate_cloud_llm` (identical in `web/coherence.py:36` and
  `web/evaluator.py:39`). Extract that one helper into `proposal_assist/web/_cloud_gate.py` and
  rewire coherence + evaluator + the new review endpoint to it. **Do NOT** retrofit the two stable,
  Codex-reviewed sibling subsystems onto a generic `BackgroundJobQueue` base: their queue helpers
  are ~150 lines of well-understood CRUD, the marginal dedup is small, and re-touching them forces
  re-verifying their suites for no functional gain. The mock-panel's queue + fan-out runner are
  **bespoke** — the 6-call fan-out is genuinely different control flow from a single-call pass, so
  there is no sibling code to reuse there anyway.
- **Reuse the dormant schema as-is → ZERO new migration.** `ReviewRun` (`db/models.py:1377`) and
  `ReviewRunItem` (`db/models.py:1439`) shipped in migrations `0026`/`0033`; the
  `Workspace.review_runs_per_day_override` + `review_dollar_cap_override` columns shipped in `0023`
  (`db/models.py:702`/`708`). All ≤ alembic head `0054`. The `ReviewRunItem.kind` CHECK fixes the
  persona set: `fit_check`, `persona_domain_skeptic`, `persona_methodology_critic`,
  `persona_translation_skeptic`, `persona_devils_advocate`, `consolidation`. **Alembic head stays
  `0054`.**
- **Fan-out → CONCURRENT + PARTIAL.** Wave 1 = fit_check + 4 personas run concurrently (bounded by
  a semaphore); Wave 2 = consolidation consumes whatever personas succeeded. A failed sub-call →
  that item `failed`/`skipped`, run continues. Run terminal status: `done` (all succeeded),
  `partial` (≥1 persona failed but consolidation produced output), `failed` (consolidation failed
  or all personas failed).
- **Output → plain committed review files**, fetched by stored git **blob** SHA via a new read
  endpoint. **Not** first-class artifacts (no `ArtifactVersion` rows; kept out of `proposal.md`
  history, which `tools/history.py` deliberately scopes to `proposal.md`). Each run is versioned by
  its `ReviewRun` row.
- **Caps → run-count AND dollar, enforced synchronously at the trigger endpoint** (429 if exceeded,
  no run row created). `estimated_dollars` computed from config-driven per-MTok rates seeded from
  current `claude-opus-4-7` pricing ($5 in / $25 out / $0.50 cache-read).
- **Trigger gating → any workspace member** (`require_member` + `_gate_cloud_llm`), consistent with
  coherence/evaluator. Cost is bounded by the caps.
- **Model → `settings.proposal_assist_llm_model`** (`config.py:116`, currently `claude-opus-4-7`),
  whose description already names "fit-check, persona scoring, and consolidation."
- **UI → a dedicated "Panel review" top-level tab** (Draft | Spec | Scorecard | **Panel review**).

Naming trap (do not confuse): `proposal_assist/web/rubric_review.py` is the **unrelated** Phase-0
owner rubric-confirmation gate (setup→active). This slice's `rubric-review.md` /
`rubric_review_sha` are the mock-panel's consolidated review output and have nothing to do with it.

---

## 1. Data model — reuse the dormant tables, ZERO migration

### 1.1 `ReviewRun` (`db/models.py:1377`) — one row per triggered run

Columns and how this slice uses each (no changes to the table):

| column | use |
|---|---|
| `id` | run id; also `ToolContext.origin_job_id` for provenance |
| `workspace_id` | FK; `uq_review_runs_active` (partial unique on `status IN ('queued','running')`) → at most one active run per workspace |
| `status` | `queued`→`running`→ terminal `done`/`partial`/`failed` (`cancelled` reserved for the soft-delete handler) |
| `triggered_at` | server default `now()`; the Stockholm-day run-count cap counts rows by this |
| `completed_at` | set on terminal transition |
| `draft_sha` (NOT NULL) | **sha256 of `proposal.md` bytes** the panel reviewed (staleness marker; mirrors `evaluator.context.content_sha`). Set at enqueue (NOT NULL), re-pinned to the exact assembled bytes at pass start. |
| `fit_check_sha` / `rubric_review_sha` (nullable) | git **blob** SHAs of the two written files (set on success/partial) |
| `waived_from_review_run_id` (self-FK) | unused this slice (left null) |
| `input_tokens` / `output_tokens` / `cache_read_input_tokens` | summed across the run's items at finalize |
| `estimated_dollars` (`Numeric(6,3)`) | summed across items at finalize |

No `requested_by` and no run-level `error` column exist. We do not add them (would need a
migration). The triggering user is audited via a `WorkspaceEvent` (kind `review_run_triggered`,
`actor_id=user.id`) written at enqueue, matching how disclosure-ack and other workspace actions are
recorded. Run-level failure reasons live in the item `error_message`s + logs; cap rejection happens
at the endpoint (429) before any row exists, so it needs no row to carry a message.

### 1.2 `ReviewRunItem` (`db/models.py:1439`) — one row per sub-call

Six rows pre-created `queued` when the pass starts (kinds fixed by the CHECK). Per item:
`status` (`queued`→`running`→`done`/`failed`/`skipped`/`cancelled`), `started_at`/`completed_at`,
`model`, `input_tokens`/`output_tokens`/`cache_read_input_tokens`, `estimated_dollars`
(`Numeric(6,4)`), `error_message`, and `raw_response_json` (JSONB) — where each sub-call's produced
markdown is stored: `{"markdown": "..."}`. `UNIQUE(review_run_id, kind)` guarantees one row per kind.

### 1.3 Per-workspace overrides (`db/models.py:702`/`708`)

`review_runs_per_day_override` and `review_dollar_cap_override` are read for the first time by this
slice (today nothing references them). Both fall back to a global config default when null.

---

## 2. Personas — `review_personas.yaml`

A new file, **`src/call_manager/proposal_assist/review/review_personas.yaml`**, holds the six system
prompts keyed by `ReviewRunItem.kind`. Loaded once and validated at import that its keys are exactly
the CHECK-constraint set (fail fast on drift). Each entry: `{role: <system prompt>}`. Persona angles
(grant-review framing for a Swedish hospital / Vinnova context):

- **fit_check** — Does this proposal actually fit *this* call? Eligibility, scope match, mandatory
  criteria, deal-breakers. Produces `fit-check.md`: a go/marginal/no-fit verdict + the concrete
  mismatches.
- **persona_domain_skeptic** — A subject-matter expert in the proposal's research domain. Is the
  science novel, significant, and credible? Where is it thin, overclaimed, or behind the field?
- **persona_methodology_critic** — A methods/stats reviewer. Are the aims, design, power, controls,
  feasibility, and timeline sound? What would a tough methods panelist flag?
- **persona_translation_skeptic** — The research→clinic / impact reviewer (the Vinnova–hospital
  angle). Is the path from result to clinical/societal benefit real and credible, or hand-waved? Is
  the implementation/adoption story concrete?
- **persona_devils_advocate** — The reviewer looking for reasons to reject: weakest link, internal
  contradictions, unstated assumptions, the argument a hostile panelist would make.
- **consolidation** — The panel chair. Synthesize the four persona critiques (plus the fit-check
  verdict) into one prioritized, de-duplicated review against the funder rubric → `rubric-review.md`.

Prompts instruct: judge ONLY the SPEC/DRAFT/rubric provided in the instruction (no other tools),
call `submit_review` EXACTLY ONCE with a markdown body, and keep within a length budget.

---

## 3. The review pass — `proposal_assist/review/` package

New package mirroring `evaluator/` structure but bespoke for the fan-out:
`{__init__,context,personas,agent,submit (tool lives in tools/),jobs,pass_runner}.py`.

### 3.1 `context.py`

`assemble_review_context(session, workspace_id, workspaces_dir) -> ReviewContext`. Reads
`proposal.md` + `proposal-spec.md` bytes ONCE (mirrors `evaluator.context._read_artifact` — closes
the TOCTOU gap) and the workspace rubric snapshot. Returns:

```python
@dataclass
class ReviewContext:
    draft_text: str
    spec_text: str
    rubric: list[dict]          # PINNED snapshot, rendered into every persona instruction
    funder_notes: str
    draft_sha: str              # sha256 of the EXACT proposal.md bytes assembled
```

A pure renderer builds each sub-call's instruction: `_persona_instruction(kind, ctx)` =
persona role (from yaml) + rendered rubric + funder notes + SPEC block + DRAFT block. The
consolidation instruction additionally appends the completed persona critiques + fit-check verdict
(assembled by `pass_runner` from the done items), so consolidation is sequenced after Wave 1.

### 3.2 `personas.py`

`load_personas() -> dict[str, str]` reads `review_personas.yaml` (cached at module load), validates
keys == the kind set, exposes `persona_prompt(kind)`. `REVIEW_KINDS`, `PERSONA_KINDS` (the four),
`WAVE1_KINDS` (fit_check + the four) constants live here.

### 3.3 `agent.py`

`build_review_adapter(*, settings, session_factory, workspace_id, review_run_id, item_id, kind) ->
ClaudeAgentSDKAdapter`. Identical shape to `evaluator/agent.py`: a dedicated one-shot adapter, MCP
server with a `ToolContext` stamped `origin_kind="review_session"`, `origin_job_id=review_run_id`,
and a new `review_run_item_id=item_id` field; system prompt = `persona_prompt(kind)`; **allowed
tools = `["submit_review"]` only** (no live reads — same safety posture as the evaluator). One
adapter per sub-call (each persona needs its own system prompt).

`ToolContext` gains one field: `review_run_item_id: uuid.UUID | None = None` (additive, defaulted —
no behavior change for existing callers).

### 3.4 `submit_review` tool — `tools/review_submit.py`

Mirrors `tools/scorecard.py`. Signature `submit_review_impl(ctx, *, markdown: str)`. Validates
`markdown` is a non-empty string (returns `{"error": ...}`, never raises). Guarded write: row-lock
the `ReviewRunItem` by `ctx.review_run_item_id`, require it belongs to `ctx.origin_job_id`/
`ctx.workspace_id` and `status == "running"` and `raw_response_json is None` (exactly-once), then
write `raw_response_json = {"markdown": markdown}` and commit. Returns `{"stored": True}`.
Registered in `tools/registry.py` and gated to the review adapters via the allowlist (the
interactive/coherence/evaluator allowlists do not include it).

### 3.5 `jobs.py` — queue helpers + caps + pricing

Pure-DB helpers (flush, never commit; caller owns the txn), bespoke but structurally familiar:

- `enqueue_review_run(session, *, workspace_id, draft_sha) -> ReviewRun` — create-or-return-active,
  race-safe via the same SAVEPOINT pattern as `evaluator.enqueue_evaluator_run` against
  `uq_review_runs_active`. Sets `draft_sha` (NOT NULL) at insert.
- `claim_next_review_run(session) -> ReviewRun | None` — `FOR UPDATE SKIP LOCKED`, oldest queued
  whose workspace is live and has no running run; flips to `running`. (Mirrors
  `claim_next_evaluator_job`.)
- `cancel_review_runs_for_workspace(session, *, workspace_id) -> int` — one atomic
  `UPDATE ... WHERE status IN ('queued','running') ... RETURNING` setting `status='cancelled'`,
  `completed_at=now()` (soft-delete handler; never an ORM load-then-write — see
  `cancel_evaluator_jobs_for_workspace`). Wired into the workspace soft-delete path alongside the
  coherence/evaluator cancels.
- `reap_stuck_review_runs(session, *, timeout_seconds) -> int` — fail any `running` run older than
  the TTL (`status='failed'`), unwedging the active slot.
- `latest_review_run(session, *, workspace_id)` and
  `latest_terminal_review_run(session, *, workspace_id)` — for the status/latest endpoint.
- **Caps (read at the trigger endpoint, Stockholm day):**
  - `review_runs_today(session, *, workspace_id) -> int` — count `ReviewRun` rows with
    `triggered_at >= _stockholm_day_start_utc()` and `status != 'cancelled'`.
  - `review_dollars_today(session, *, workspace_id) -> Decimal` — `sum(estimated_dollars)` over
    those rows (NULL-coalesced; in-flight runs contribute 0 until finalized).
  - `review_run_cap(ws, settings) -> int` = `ws.review_runs_per_day_override ?? settings.review_runs_per_day_default`.
  - `review_dollar_cap(ws, settings) -> Decimal` = `ws.review_dollar_cap_override ?? settings.review_dollar_cap_default`.
- **Pricing:** `estimate_dollars(input_tokens, output_tokens, cache_read_tokens, settings) -> Decimal`
  = `in/1e6*rate_in + out/1e6*rate_out + cache_read/1e6*rate_cache_read`, quantized to the column
  scale. Used per item and summed onto the run.

### 3.6 `pass_runner.py` — the fan-out (bespoke)

`run_review_pass(*, run_id, session_factory, settings, adapter_builder=build_review_adapter)` and
`drain_review_queue_tick(...)` (claim one + run), mirroring the evaluator tick. The pass:

1. **Short read session** — load the run (guard `status=='running'`); re-assemble context from
   current `proposal.md`/`proposal-spec.md`/rubric (pinned for the whole pass); UPDATE
   `run.draft_sha` to the exact assembled bytes' sha256. Re-check the daily caps here too (cheap
   defense against a long queue wait pushing the workspace over budget); if exceeded, fail the run.
   Pre-create the 6 `ReviewRunItem` rows `queued`.
2. **Wave 1 (concurrent)** — `asyncio.gather` over `WAVE1_KINDS` (fit_check + 4 personas), bounded
   by an `asyncio.Semaphore(settings.review_persona_concurrency)`. For each: mark item `running`;
   build the one-shot adapter (system prompt = persona, instruction = pinned context); launch+drive
   inside `asyncio.wait_for(..., settings.review_persona_timeout_seconds)`; terminate the adapter in
   `finally` (bounded by `review_terminate_timeout_seconds`); on the turn's `TurnDoneEvent` record
   tokens + `estimate_dollars` and mark the item `done` (the `submit_review` tool already wrote
   `raw_response_json`); if no markdown was submitted or the call errored/timed out, mark `failed`
   with a sanitized `error_message`. Each sub-call is independently guarded — one failure never
   aborts the others (`return_exceptions=True` semantics; failures are recorded, not raised).
3. **Wave 2 (consolidation)** — only if ≥1 persona is `done`. Re-read the done persona items'
   markdown + the fit-check markdown, assemble the consolidation instruction, run the consolidation
   adapter (same guard/timeout/terminate). Its `submit_review` output is `rubric-review.md`'s body.
   If all personas failed → skip consolidation; if consolidation itself fails → its item `failed`.
4. **Write artifacts + finalize** (single short session, run row-locked, `status=='running'`
   guard — the reaper may have failed it during a hang):
   - `fit_check_md` = the fit_check item's markdown (or a placeholder note if it failed);
     `rubric_review_md` = the consolidation item's markdown.
   - One `write_and_commit_files(repo_dir, {"fit-check.md": ..., "rubric-review.md": ...},
     "mock-panel review <run_id>")`; capture each file's blob SHA (`blob_sha(repo_dir, "HEAD", name)`)
     into `fit_check_sha` / `rubric_review_sha`.
   - Sum item tokens + dollars onto the run.
   - **Artifact write rule (unambiguous):** always write whichever files were produced and set only
     those SHAs — write `fit-check.md` + set `fit_check_sha` iff the fit_check item is `done`; write
     `rubric-review.md` + set `rubric_review_sha` iff the consolidation item is `done`; leave the
     other SHA null. (So a `failed` run with a successful fit-check still exposes `fit-check.md`.)
   - Terminal status: `done` if all six items `done`; `partial` if ≥1 persona failed but
     consolidation is `done` (a usable `rubric-review.md` exists); `failed` if consolidation did not
     complete (consolidation failed, or all personas failed so it was skipped) — no usable
     `rubric-review.md`. A run failed at the pass-start cap recheck (before any item is created) is
     `failed` with the reason logged server-side (no run-level error column to surface it).
   - `completed_at=now()`.

Hardening carried from the evaluator/coherence runners: launch INSIDE `wait_for` (bounds a hung SDK
connect), bounded terminate in `finally`, `with_for_update` finalize guard, no double-finalize, the
separate sync reaper for crash recovery. Concurrency lives entirely within one drain tick
(`max_instances=1`) — it does not violate the single-instance scheduler constraint.

---

## 4. Shared gate extraction — `proposal_assist/web/_cloud_gate.py`

Move the identical `_gate_cloud_llm(*, ws, request, session, user)` (currently duplicated at
`web/coherence.py:36` and `web/evaluator.py:39`) into a new `web/_cloud_gate.py` and import it from
all three (`coherence`, `evaluator`, `review`). Behavior is byte-identical (setup→409,
`drafting_enabled` false→503, missing per-actor `disclosure_acknowledged`→403). The two existing
call sites switch to the import; their tests continue to pass unchanged (same status codes/messages).

---

## 5. Endpoints, scheduler, config

### 5.1 Endpoints — `proposal_assist/web/review.py` (router `prefix="/api/workspaces"`, mounted in `main.py`)

- `POST /{workspace_id}/review-runs` → member-gated (`require_member`) + `_gate_cloud_llm` + cap
  check. Cap check (synchronous): if `review_runs_today >= review_run_cap` **or**
  `review_dollars_today >= review_dollar_cap` → **429** with a clear message (no row created). Else
  read `draft_sha = content_sha(proposal.md)`, `enqueue_review_run(...)`, write the
  `review_run_triggered` `WorkspaceEvent`, commit. Returns the serialized run.
- `GET /{workspace_id}/review-runs/latest` → member-gated. Serializes the latest run + its items
  (per-persona progress) + cost + **staleness** (`run.draft_sha != content_sha(current proposal.md)`)
  + the two output SHAs + remaining daily budget (`cap - used` for both run-count and dollars). The
  SPA polls this while a run is active (like evaluator's latest).
- `GET /{workspace_id}/review-runs/{run_id}/files/{name}` → member-gated; `name` allowlisted to
  `fit-check.md` | `rubric-review.md`. Looks up the run's stored blob SHA for that file and returns
  its content via `read_blob(repo_dir, sha)` (`git cat-file -p`); 404 if the SHA is null (not yet
  produced) or the name is not allowlisted. Returns `{ "name", "sha", "markdown" }`.

Serialization shape (mirrors evaluator's `_serialize`, extended):
`{ id, status, triggered_at, completed_at, draft_sha, fit_check_sha, rubric_review_sha,
   estimated_dollars, items: [{kind, status, error_message, estimated_dollars}], stale,
   budget: {runs_used, runs_cap, dollars_used, dollars_cap} }`.

### 5.2 Scheduler — `scheduler.py`

Two new ticks mirroring the evaluator block (`scheduler.py:562`): `review_drain`
(`IntervalTrigger(seconds=settings.review_tick_seconds)`, `max_instances=1, coalesce=True`,
dispatched on the app loop like the SDK-driven evaluator tick) calling `drain_review_queue_tick`;
and `review_reaper` (`IntervalTrigger(seconds=settings.review_reaper_tick_seconds)`) calling a sync
reaper that runs `reap_stuck_review_runs(..., timeout_seconds=settings.review_job_timeout_seconds)`.

### 5.3 Config — `config.py` (new `review_*` block; clone the evaluator block + add pricing/caps)

```python
review_tick_seconds: int = 20
review_reaper_tick_seconds: int = 60
review_persona_timeout_seconds: int = 180     # per sub-call
review_job_timeout_seconds: int = 900         # whole run reaper TTL (6 calls > one pass)
review_terminate_timeout_seconds: int = 30
review_persona_concurrency: int = 3           # bound on simultaneous sub-calls
review_runs_per_day_default: int = 5          # global default; per-ws override on Workspace
review_dollar_cap_default: Decimal = Decimal("5.000")   # global default; per-ws override
review_price_input_per_mtok: Decimal = Decimal("5.00")        # claude-opus-4-7
review_price_output_per_mtok: Decimal = Decimal("25.00")
review_price_cache_read_per_mtok: Decimal = Decimal("0.50")
```

All with `REVIEW_*` aliases. Pricing/caps are config so a model/price change is a config edit. (The
model id itself is `proposal_assist_llm_model`, shared with coherence/evaluator.)

### 5.4 Git helpers — `citations/gitio.py`

Two small additive helpers (the module already owns git plumbing — `write_and_commit_files`,
`current_head`):

- `blob_sha(repo_dir, ref, path) -> str` — `git rev-parse <ref>:<path>` (the path's blob SHA).
- `read_blob(repo_dir, sha) -> str` — `git cat-file -p <sha>` (blob content), `GIT_CEILING_DIRECTORIES`
  guarded like `current_head`. The `files/{name}` endpoint validates the SHA shape (hex) before use.

`content_sha(path)` for the staleness sha256 is reused from `evaluator/context.py` (or re-exported).

---

## 6. Output artifacts — plain review files

`fit-check.md` and `rubric-review.md` are written by `write_and_commit_files` into the workspace
repo but are deliberately **not** added to `phases/setup.py:_PHASE_ARTIFACT_FILES` and get **no**
`ArtifactVersion` rows — they are review outputs, regenerated each run, not user-edited artifacts,
and must stay out of the `proposal.md` history (`tools/history.py` is intentionally scoped to
`proposal.md`). Versioning across runs is the `ReviewRun` rows themselves. They are read only via
the dedicated `review-runs/{id}/files/{name}` endpoint by the run's stored blob SHA — so the SPA
always shows exactly the bytes that run produced, immune to a later run overwriting the file.

---

## 7. Frontend — "Panel review" tab

- `frontend/src/api/review.ts` — typed client (`openapi-fetch`) for the three endpoints; regenerate
  `schema.gen.ts` with `EMBEDDING_MODEL=bge-m3 pnpm gen:api` (the default `.env` model logs a stdout
  WARNING that corrupts the file).
- `frontend/src/cockpit/ReviewPanel.tsx` — new top-level tab (added to `routes/Cockpit.tsx`'s
  `top_tab` set, alongside Draft | Spec | Scorecard). Contents:
  - **Run button** "Run full mock-panel review" — disabled with the reason when gated (setup/
    drafting-disabled/disclosure) or when budget is exhausted; shows remaining daily budget
    (`runs_used/runs_cap`, `$dollars_used/$dollars_cap`).
  - **Live status** — `useReviewRun` polls `…/latest` (~3–5s like the evaluator's poll) while a run
    is `queued`/`running`, rendering per-persona item progress (fit_check + 4 personas +
    consolidation, each queued/running/done/failed) and a `partial`/`failed` banner.
  - **Result** — the two rendered markdown docs (fit-check, rubric-review) fetched via
    `…/files/{name}`, a **staleness badge** ("reviewed an older draft") when `stale`, the run's
    cost, and `triggered_at`/`completed_at`.
- Frontend tests (the gate is `pnpm exec tsc --noEmit`; there is no TS linter). Vitest specs for the
  panel: gated/disabled states, budget rendering, live-progress, partial/failed banners, stale badge.

---

## 8. Testing & risks

### 8.1 Tests (new dir `tests/proposal_assist/review/`, mirroring `tests/proposal_assist/evaluator/`)

- **jobs**: enqueue create-or-return-active + race (SAVEPOINT) ; claim `FOR UPDATE SKIP LOCKED` +
  live-workspace filter; cancel-for-workspace atomic UPDATE; reaper TTL; `review_runs_today` /
  `review_dollars_today` (Stockholm day, excludes `cancelled`); cap helpers honor the per-workspace
  override and the global default; `estimate_dollars` arithmetic + quantization.
- **submit_review tool**: stores markdown to the right item; rejects empty; exactly-once (second
  call errors); rejects a mismatched/non-running target.
- **pass_runner (adapter stubbed)**: happy path → all 6 items `done`, both files committed, SHAs +
  tokens + dollars on the run, status `done`; one persona fails → item `failed`, consolidation runs,
  status `partial`; all personas fail → consolidation skipped, status `failed`, no
  `rubric-review.md` SHA; consolidation fails → status `failed`; per-call timeout path; the
  pass-start cap recheck fails the run.
- **endpoints**: gate paths (409 setup / 503 drafting-disabled / 403 no-disclosure); 429 when over
  run-count cap and when over dollar cap (no row created); create-or-return-active; `latest`
  serialization incl. staleness + budget; `files/{name}` returns bytes at the stored SHA, 404s on
  null SHA / non-allowlisted name; `WorkspaceEvent` written on trigger.
- **shared gate**: `_cloud_gate` extraction — the existing coherence/evaluator gate tests still pass
  against the imported helper; one direct test of the helper.
- **frontend**: the ReviewPanel specs above.

Run only affected files with `; echo $?` (full `uv run pytest` is testcontainer-flaky); re-run any
connection-refused failure in isolation.

### 8.2 Risks & mitigations

- **Cost overrun** — bounded by the synchronous endpoint cap (run-count + dollar) and the pass-start
  recheck; per-item dollars recorded so the dollar cap reflects actual spend on the next trigger.
- **Concurrency load** — 5 simultaneous hosted-Opus sessions max, bounded by
  `review_persona_concurrency` (default 3); network-bound, not GPU; well within one drain tick.
- **Stale review** — `draft_sha` staleness marker + UI badge; the panel reviews a pinned snapshot so
  the verdict always matches a known draft.
- **Partial failure surfaced honestly** — `partial`/`failed` status + per-item `error_message` +
  banner; never silently presents an incomplete panel as complete.
- **Reviewing newer bytes** — personas have only `submit_review` (no live reads), so they can only
  judge the pinned instruction, exactly like the evaluator.

---

## 9. File-touch map

**New:**
- `src/call_manager/proposal_assist/review/__init__.py`
- `src/call_manager/proposal_assist/review/context.py`
- `src/call_manager/proposal_assist/review/personas.py`
- `src/call_manager/proposal_assist/review/review_personas.yaml`
- `src/call_manager/proposal_assist/review/agent.py`
- `src/call_manager/proposal_assist/review/jobs.py`
- `src/call_manager/proposal_assist/review/pass_runner.py`
- `src/call_manager/proposal_assist/tools/review_submit.py`
- `src/call_manager/proposal_assist/web/_cloud_gate.py`
- `src/call_manager/proposal_assist/web/review.py`
- `frontend/src/api/review.ts`
- `frontend/src/cockpit/ReviewPanel.tsx`
- `tests/proposal_assist/review/` (+ frontend specs)

**Edited:**
- `src/call_manager/proposal_assist/tools/registry.py` — `ToolContext.review_run_item_id` field;
  register `submit_review`.
- `src/call_manager/proposal_assist/web/coherence.py` + `web/evaluator.py` — import the extracted
  `_gate_cloud_llm`.
- `src/call_manager/proposal_assist/citations/gitio.py` — `blob_sha` + `read_blob`.
- `src/call_manager/config.py` — `review_*` block.
- `src/call_manager/scheduler.py` — `review_drain` + `review_reaper` ticks.
- `src/call_manager/main.py` — mount the review router.
- the workspace soft-delete handler — call `cancel_review_runs_for_workspace`.
- `frontend/src/routes/Cockpit.tsx` — add the Panel-review top tab; regenerate `schema.gen.ts`.

**NO migration** (alembic head stays `0054`).

---

## 10. Build decomposition (for the implementation plan)

1. **Shared gate + git helpers + config + pricing** — extract `_cloud_gate.py` (+ rewire two
   siblings); `gitio.blob_sha`/`read_blob`; `config.review_*` block; `estimate_dollars`. (No new
   behavior; safe foundation. Re-run coherence/evaluator endpoint tests.)
2. **Review package core** — `context`, `personas` + `review_personas.yaml`, `agent`,
   `submit_review` tool + `ToolContext` field + registry wiring. Unit tests for personas-load,
   context assembly, submit tool.
3. **Queue + caps + pass_runner** — `jobs.py` (queue/caps/pricing), `pass_runner.py` (fan-out),
   scheduler ticks, soft-delete cancel wiring. Tests with a stubbed adapter (happy/partial/failed/
   timeout/cap-recheck) + jobs tests.
4. **Endpoints** — `web/review.py` (POST/latest/files), mount in `main.py`. Endpoint tests
   (gate/cap/serialization/files).
5. **Frontend** — regenerate API client, `api/review.ts`, `ReviewPanel.tsx`, Cockpit tab + nav,
   Vitest specs; `pnpm exec tsc --noEmit` gate.

---

## 11. Out of scope

- Auto-trigger / debounce (this is explicitly on-demand).
- Per-persona retry/escalation; alternate models per persona.
- Diff/history browsing of past runs' artifacts beyond "latest" + by-SHA fetch (each run keeps its
  SHAs; a run-history list view is a later slice if wanted).
- Surfacing review findings as actionable `proposed_change` rows (the evaluator/coherence inbox path)
  — the mock-panel writes prose docs only.
- The `waived_from_review_run_id` waiver workflow (column left unused).
