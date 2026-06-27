# Code-Review Remediation Plan

> **For agentic workers:** each batch below is dispatched to a fresh implementer subagent. Steps follow TDD where a behavioural test is meaningful; pure doc/comment/constant fixes commit without a new test. Source of truth for each defect is `docs/superpowers/notes/2026-06-27-thorough-code-review.md`.

**Goal:** Remediate the 132 verified defects from the 2026-06-27 multi-agent review (0 Critical, 10 High, 29 Medium, 70 Low, 23 Info), then run a focused follow-up review to close the completeness-critic coverage gaps.

**Architecture:** Single feature branch `fix/review-remediation` off master. File-grouped batches committed sequentially in one worktree. Each batch is independently testable. Final whole-branch verification (full CI surface) + adversarial review before merge to local master (no remote; not pushed).

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy 2.x / Alembic / pgvector; React 19 + Vite + TanStack Query; pytest (testcontainers PG) / ruff / vitest / tsc.

## Global Constraints

- **Branch:** all work on `fix/review-remediation` in `.worktrees/review-remediation`. Subagents MUST `cd` into the worktree, use `git -C <worktree>`, and verify `git branch --show-current` == `fix/review-remediation` before any edit/commit.
- **Tests are flaky whole-suite:** run AFFECTED test files GROUPED with a neighbour (testcontainers PG drops under load; connection-refused = infra, re-run isolated). Never gate on a single whole-suite run.
- **CI gate (must stay green):** `uv run python scripts/gen_no_build.py --check`, `uv run python scripts/gen_routes.py --check`, `uv run ruff check .`, `uv run pytest -v`; frontend `pnpm run test`, `tsc && vite build`, OpenAPI drift (`bash scripts/gen_openapi_types.sh` then `git diff --exit-code frontend/src/api/schema.gen.ts`). CI runs `ruff check .` ONLY (not `ruff format --check`); frontend gate is `tsc --noEmit` (no eslint), tsconfig has `noUnusedLocals`.
- **New endpoints / schema changes:** regenerate `schema.gen.ts` with `EMBEDDING_MODEL=bge-m3 bash scripts/gen_openapi_types.sh` (mxbai default logs a WARNING that breaks openapi-typescript). New SPA routes ⇒ `uv run python scripts/gen_routes.py` (manifest is PREFIX-not-`:param`).
- **DB changes:** new Alembic migration (never edit a shipped one); mirror `ondelete`/columns in the model; cockpit `/api` tests live under `tests/proposal_assist/`.
- **No behaviour change to deliberate decisions** unless the fix is the documented remedy. The 3 explicit "accepted-decision / non-issue" items (CSRF absent-Origin, register timing oracle, 30-day session) get the documenting comment the report recommends — session-lifetime default is NOT changed without owner sign-off (flag it).
- **Scope:** the 132 defects only. The 38 product ideas are OUT of scope for this branch.

---

## Phase A — Backend

### B1: Scheduler single-instance + thread-pool — `scheduler.py`
- **[High]** Single-instance constraint convention-only (`scheduler.py:83-94`). Acquire `pg_try_advisory_lock(hashtext('call-manager-scheduler'))` on a dedicated session in `lifespan` before `sched.start()`; if not held, build with `start=False` and log loudly. Lock session held for process life.
- **[Medium]** 23 jobs share a default 10-thread pool; blocking ticks starve cron (`scheduler.py:433-440`). Configure a larger executor (e.g. 30) or split a blocking pool from the fast cron pool; add a startup assertion on worker count.
- **Test:** advisory-lock helper returns False when lock already held (unit, mock/`pg_try_advisory_lock`); scheduler builds with `start=False` when lock not acquired. Executor max_workers assertion.

### B2: Persist ingestion failure alerter — `ingestion/alerts.py`, `db/models.py` (Source), `ingestion/orchestrator.py`/`run_ingestion`
- **[High]** In-memory streak re-armed on restart (`alerts.py:20-90`). Persist `consecutive_failures` + `last_alerted_at` on `Source` (new migration), update transactionally alongside `last_fetched_at`. Add a restart-independent backstop: alert on `last_fetched_at older than N days`.
- **Test:** streak survives a simulated "restart" (new tracker instance reads persisted count → still alerts at threshold); backstop fires on stale `last_fetched_at`.

### B3: Source-fetcher hardening — `sources/eu_portal.py`, `sources/vinnova.py`, `sources/extraction.py`, `references/fetcher.py`, `references/extract.py`
- **[Medium]** EU-portal first run scrapes ≤20k pages serially (`eu_portal.py:143-209`): cap fresh-body scrapes/tick, share one `httpx.Client`, wall-clock budget.
- **[Medium]** Redirect chain 6×fresh 60s/50MB budget (`fetcher.py:218-285`): single cumulative deadline + byte budget across hops.
- **[Medium]** ZIP guard trusts declared sizes, no ratio check (`references/extract.py:41-61`): enforce per-entry + aggregate compression-ratio cap.
- **[Low]** Vinnova per-round fetch bypasses `request_with_retry` (`vinnova.py:179-200`): wrap it.
- **[Low]** SSRF pinning missing on source-API fetches (`vinnova.py:139-143`, `eu_portal.py:132`): route through `pinned_transport_for` or comment the deliberate omission.
- **[Low]** `is_safe_fetch_url`/`resolve_pinned_ip` double DNS (`extraction.py:33-49`): define one in terms of the other.
- **[Low]** trafilatura no parse/output cap (`extraction.py:152-166`): cap extracted-text length before persist; wall-clock guard.
- **Test:** EU-portal cap leaves remainder; redirect budget exhausts once across hops; zip ratio rejects a high-ratio member; vinnova round fetch retries 5xx.

### B4: Drafting subprocess pool + lifecycle — `proposal_assist/drafting/registry.py`, `controller.py`, `claude_adapter.py`, `turns/drain.py`, `db/session.py`, background agents
- **[High]** Unbounded live subprocesses each pin a pooled connection (`registry.py:49`, `controller.py:116`): bound concurrent subprocesses (semaphore / LRU-evict in `claim_launch_or_wait`) and/or dedicated small pool for advisory-lock sessions. Document hard ceiling < pool_size+max_overflow.
- **[Low]** Background sub-agents expose full MCP toolset, soft-gated only (`evaluator/agent.py:53`, `review/agent.py:46`, `uncited_claims/agent.py:47`, `claude_adapter.py:216-289`): set `disallowed_tools = collected - allowed` + hard-deny `can_use_tool`; test a background adapter can't reach `fetch_reference`/`propose_*`.
- **[Low]** `ANTHROPIC_API_KEY` only via inherited `os.environ` (`claude_adapter.py:76`): pass `options_kwargs['env']['ANTHROPIC_API_KEY'] = settings.anthropic_api_key`.
- **[Low]** `client.disconnect()` failure orphans subprocess (`claude_adapter.py:452`): force-kill fallback / pending-kill set.
- **[Low]** Idle teardown discards resumable session with queued turn (`registry.py:214`): skip/extend window when a turn is queued.
- **[Low]** Timed-out turn skips SDK `interrupt()` (`drain.py:105`): try bounded `interrupt()` before `terminate()`.
- **[Low/test]** Fake adapter models neither serialization nor post-cancel desync (`fake_adapter.py:61`): add asserting fake mode.
- **Test:** semaphore refuses/evicts at ceiling; background adapter denies non-allowlisted MCP tool; disconnect-failure path force-kills.

### B5: Analysis pipeline retry/dead-letter — `proposal_assist/analysis/pipeline.py` (+ `citations/audit.py`)
- **[High]** No intermediate commit ⇒ retry re-spends; `any(audit_unavailable)` dead-letters on one bad verdict (`pipeline.py:338-340`, `121-131`, `205-236`, `510-533`). Commit classification progress before the verdict phase; only re-raise on the cap signal (degrade-and-continue on transient/invalid row).
- **Test:** a single `audit_unavailable` row does NOT raise (section degrades, continues); cap signal still raises; partial classifications survive a mid-loop exception.

### B6: Citation export + bibliography — `proposal_assist/citations/bibliography.py`
- **[High]** Literal `[@token]` hard-blocks all export (`bibliography.py:716-728`, `_BARE_MARKER_RE:36`): make residual scrub fence/code-span aware (mirror `iter_proposal_sentences` fence tracking) or scope to `find_markers`-matchable spans.
- **[Low]** Title ending `?`/`!` renders `?.` (`bibliography.py:221-230`): strip any single trailing `.!?`.
- **[Low]** Author-year sort non-deterministic in ties (`bibliography.py:375-387`): secondary key `(casefolded, citekey)`.
- **Test:** `[@x]` inside a fenced code block exports cleanly; a real unresolved marker still blocks; `Title?` → `Title?` not `Title?.`; tie-sort stable.

### B7: Citation tooling correctness — `proposal_assist/tools/citations.py`, `citations/claims.py`, `citations/audit.py`, `citations/service.py`
- **[Medium]** `propose_citation` section keyed from raw `.find()` (`tools/citations.py:498-513`): use `markers.anchor_occurrences`.
- **[Medium]** `claim_sentence_around` not heading-aware (`claims.py:24-79`): stop backward scan at `^\s*#`.
- **[Medium]** Spurious permanent `claim_drift` for empty-claim marker (`audit.py:394-417` vs `tools/citations.py:649-660`): share one hash-basis helper.
- **[Low]** `rename_citekey` cache backfill updates only one of N (`tools/citations.py:1168-1190`): `UPDATE ... WHERE citekey == old`.
- **[Low]** `update_citation` locator rebuild drops extra keys (`tools/citations.py:940-975`): overlay onto a copy.
- **[Low]** `list_citations` no pagination/cap (`tools/citations.py:302-348`): `limit` default 100 / hard cap 500 + `truncated`.
- **[Low]** `_safe_record` swallows ledger write error → cap under-counts (`service.py:66-78`): treat cap as conservatively exceeded after a post-spend record failure (or circuit-break).
- **Test:** propose with pre-existing markers keys correct section; claim after heading excludes heading; empty-claim insert/validate hashes agree (no drift); rename updates all matching cache rows; list cap+truncated.

### B8: Citation-audit endpoints — `proposal_assist/web/api/references.py`, `web/api/citation_audit.py`, `citations/audit.py`
- **[Medium]** Metadata-approval IntegrityError retry can 500 + keep colliding citekey (`references.py:326-341`): None-guard the re-fetch (404), regenerate citekey only if still colliding, scope `except` to the citekey unique constraint via SQLSTATE.
- **[Medium]** Deep-check unbounded request-blocking fan-out (`audit.py:539-604`, `citation_audit.py:135-150`): cap fresh judgements/request (report `checked` vs deferred) and/or move to background queue.
- **Test:** concurrent-delete re-fetch → 404 not 500; only citekey-collision IntegrityError is retried; deep-check caps fresh judgements and reports deferred count.

### B9: Turns caps + outcomes + gating + audit — `proposal_assist/turns/outcomes.py`, `caps.py`, `processor.py`, `web/review.py`, `web/coherence.py`, `web/evaluator.py`, `web/uncited_claims.py`, `review/jobs.py`, `review/pass_runner.py`
- **[Medium]** Failed-turn tokens never charged (`outcomes.py:78`, `caps.py:107`, `processor.py:335`): stamp consumed tokens on a `ChatTurn` row; clear pre-count only when nothing consumed.
- **[Medium]** Global cap counts only chat turns (`caps.py:103-150`): UNION the three job tables, or document chat-only explicitly.
- **[Medium]** Member-triggerable expensive fan-outs (`web/review.py:143`, `coherence.py:52`, `evaluator.py:53`, `uncited_claims.py:80`): decide authority — restrict to owner OR per-actor sub-cap. **Decision needed → default: match `submit_turn` (owner-only) unless the report's per-actor-cap is cheaper; document the chosen model.**
- **[Medium]** Coherence/evaluator enqueue emit no actor-attributed audit event (`coherence.py:54-55`, `evaluator.py:55-56`): write `coherence_run_triggered`/`evaluator_run_triggered` with `actor_id`.
- **[Low]** `review_dollars_today` excludes cancelled while late spend re-rolls onto cancelled (`review/jobs.py:167-190`, `pass_runner.py:65-88`): include cancelled (or stop re-rolling); `with_for_update` in pre-launch recheck.
- **[Low]** `_run_one_call` assumes `ReviewRunItem` exists (`pass_runner.py:66-73`): `if item is None: return`.
- **Test:** failed turn with consumed tokens counts against cap; global cap sees a background-job row; non-owner member blocked (or sub-capped) per chosen model; audit events written with actor_id.

### B10: Failed-setup brick recovery — `proposal_assist/phases/setup.py`, `web/api/rubric.py` (or new endpoint), workspace read-model
- **[High]** Failed Phase-0 setup strands workspace in `setup` forever (`setup.py:183-190`): owner/admin endpoint to re-queue a `failed` `WorkspaceSetupJob` (reset status/attempts/timestamps, clear last_error); surface failed-setup in the read-model so UI can offer retry.
- **[Low]** Setup worker one long-lived Session across git+LLM (`setup.py:155-215`): scope short sessions around git/extract sub-steps.
- **Test:** re-queue endpoint resets a `failed` job (owner-only, 404 for non-failed); read-model exposes failed-setup state.

### B11: Ranking must_include + re-bill + EV/calibration cluster — `ranking/rank_pair.py`, `digest/assemble.py`, `ranking/worker.py`, `ranking/ev.py`, `ranking/anthropic_client.py`, `embeddings/calibration.py`, `ranking/queue.py`, `ranking/validate_ev.py`, `ranking/prompt.py`
- **[High]** `must_include` raw substring match drags off-topic into paid LLM (`rank_pair.py:334-365`): word-boundary `\b{re.escape(term)}\b` + min length, identical in `rank_pair` and `assemble.py`.
- **[Medium]** DB-commit failure after billed Sonnet re-bills on retry (`worker.py:208-260`): record remote attempt in its own committed txn immediately after `rank_pair` returns (or cache result on job row).
- **[Low]** Model free-text fields not provenance-checked (`prompt.py:339-379`): cap length, optionally strip URLs, "model-generated" affordance.
- **[Low]** EV/shrink/fence/calibration cluster (`ev.py:101-148`, `anthropic_client.py:214-242`, `calibration.py:20-45`): log EV no-op; prefer last/first-parseable fence; normalize model names (strip `:tag`) in both comparison sites.
- **[Low]** Attempts constants drift + `validate_ev` literal 0.30 (`queue.py:181-186`, `worker.py:34`, `validate_ev.py:58-64`): define attempts once; read thresholds from Settings.
- **[Low]** No per-tick wall-clock budget for serial ranking (`worker.py:91-147`): soft deadline, leave remainder PENDING.
- **Test:** `ai` does NOT match inside `training` (both sites); re-bill path doesn't re-call on commit failure (records attempt first); model-name normalization matches `bge-m3:567m`.

### B12: Embeddings truncation + batching — `embeddings/client.py`, `proposal_assist/references/ingest.py`, `embeddings/refresh.py`, `ingestion/orchestrator.py`, `ranking/queue.py`
- **[Medium]** Shrink-and-retry stores vector for truncated text but full text alongside (`client.py:63-89`, `ingest.py:144-160`): return embedded-char count so ingest stores matching text / split-and-embed / flag `embedding_truncated`.
- **[Low]** Orchestrator N+1 + one-row-per-statement enqueue/refresh (`orchestrator.py:166-189,263-269`, `queue.py:72`, `refresh.py:284-291`, `client.py:90-109`): `INSERT … RETURNING`, bulk enqueue via `unnest`, batch embed inputs (16-64/request).
- **Test:** over-long chunk stores text matching the embedded prefix (or sets `embedding_truncated`); bulk enqueue inserts N rows in one statement.

### B13: Web core — `main.py`, `web/api/me.py`, `web/api/calls.py`, `web/filters.py`, `db/models.py`, `web/dashboard.py` (+ frontend `Deadline.tsx`/`CallDetail.tsx` coordinated in B17)
- **[Medium]** `/docs`/`/redoc`/`/openapi.json` unauth in prod (`main.py:264`): `docs_url=None,...` when `spa_mode != 'dev'` (or gate behind admin); test 404 in prod.
- **[Medium]** `POST /api/me/name` 500s >200 chars (`me.py:104-126`): `Field(max_length=200)` → clean 422.
- **[Medium]** `PUT /api/me/subscriptions` deletes inactive-profile subs (`me.py:184-225`): scope diff to active profiles.
- **[Medium]** "Hide call" permanent/irreversible (`calls.py:157-174`): `DELETE /calls/{id}/hide` + Hidden view (backend endpoint here; UI optional follow-up) — at minimum un-hide endpoint.
- **[Medium]** Digest deadline (Stockholm) vs SPA deadline (UTC) disagree (`filters.py:29-34`): localize both to Stockholm (pre-localized date string in read-model preferred). Backend side here.
- **[Medium]** `ranking_jobs` missing indexes (`models.py:368-377`): add `ix_ranking_jobs_call_profile`, `ix_ranking_jobs_profile_status` (migration).
- **Test:** prod app 404s `/openapi.json`; name >200 → 422; subscriptions PUT leaves inactive subs; un-hide restores feed; deadline date matches across surfaces.

### B14: Auth / middleware / session / config — `auth/passwords.py`, `web/api/auth.py`, `web/api/me.py`, `web/routes/oauth_google.py`, `auth/accounts.py`, `auth/csrf.py`, `auth/session_cookie.py`, `config.py`, `web/middleware.py`, `auth/rate_limit.py`
- **[Low]** Password fields unbounded (argon2 CPU) (`auth.py:35`, `me.py:131`, `passwords.py:28`): `Field(max_length=1024)`.
- **[Low]** `verify_password` never rehashes on upgrade (`passwords.py:28-36`): `check_needs_rehash` + re-persist in handlers.
- **[Low]** Connect-Google link skips domain gate (`oauth_google.py:134-143`, `accounts.py:63-79`): enforce `domain_allowed()` / email-equality on linked email; pin behaviour with a test.
- **[Low]** Body-cap/500 responses bypass security headers (`middleware.py:155-172`): set minimal header set in `_send_error` (or reorder); header-presence regression test.
- **[Low]** `MaxBodySizeMiddleware` trusts first Content-Length (`middleware.py:125-137`): reject >1 distinct content-length.
- **[Low]** `EMAIL_FROM` placeholder never validated (`config.py:45`): `field_validator` rejecting `example.com` (and requiring `@domain`) when `cookie_secure`.
- **[Low/doc]** rate_limit stale docstring (`rate_limit.py:56-59`): correct; consider per-limiter max-key cap.
- **[Low/doc]** `style-src 'unsafe-inline'` undocumented (`middleware.py:45`): document the Vite/React requirement.
- **[Info/doc]** CSRF absent-Origin allow (`csrf.py:118-124`): document accepted decision.
- **[Info/doc]** Register timing oracle (`auth.py:137-144`): comment that the allowlist is intentionally public.
- **[Low/doc]** 30-day session (`session_cookie.py:32`, `config.py:516`): **document as deliberate risk acceptance; do NOT change default without owner sign-off (flag).**
- **Test:** password >1024 → 422; rehash triggers on needs-rehash; off-domain Google link rejected; duplicate content-length rejected; EMAIL_FROM placeholder fails validation when cookie_secure.

### B15: DB schema + email/digest + perf cluster — migrations, `digest/assemble.py`, `email/client.py`, `web/dashboard.py`, `main.py`, `web/spa_serving.py`, `db/models.py`, `auth/rate_limit.py`, `profiles/bootstrap_worker.py`
- **[Low]** `review_runs.waived_from_review_run_id` self-FK no `ON DELETE` (`0026_*:49-52`): migration `ondelete='SET NULL'` + model.
- **[Low]** `0050` downgrade omits original indexes: copy DDL or comment schema-only.
- **[Low]** Digest must-include trigram bypassed for short/keywords terms (`assemble.py:178-201`): enforce ≥3-char min or drop the over-promising comment.
- **[Low]** must_include cap per-profile not per-email (`assemble.py:38,318,131`): whole-payload cap + "+N more" footer.
- **[Low]** Excluded `NOT IN` list repeated 3×N (`assemble.py:151,191,221,242`): materialize once (VALUES CTE / `<> ALL(:arr)`).
- **[Low]** Resend classify substring-based (`email/client.py:47-62`): prefer structured `name`; word-boundary + tests.
- **[Low]** Bootstrap worker re-runs PubMed+LLM on crash (`bootstrap_worker.py:140-154`): persist sub-state / skip if description already non-empty.
- **[Low]** Hot dashboard query full sort+limit (`dashboard.py:248-289`): document the latent cliff; (keyset migration deferred unless cheap) — at minimum confirm plan + add the `>=70` named constant (`dashboard.py:330`).
- **[Low]** No gzip / immutable Cache-Control / status enums cluster (`main.py:269-282`, `spa_serving.py:24-49`, `db/models.py`): add `GZipMiddleware`, `Cache-Control: public, max-age=31536000, immutable` for `/assets/*` (index.html no-cache).
- **[Info]** SPA fallback enumerates routes per unmatched request (`spa_serving.py:58-71`): short-circuit 404 unless plausibly backend.
- **Test:** FK SET NULL on parent delete; gzip header present on large response; assets immutable cache header; resend classify word-boundary.

## Phase B — Frontend

### B16: Cockpit data-loss (High) — `frontend/src/editor/CodeMirrorEditor.tsx`, `routes/Cockpit.tsx`, `cockpit/chat/CockpitChat.tsx`, `shell/AppShell.tsx`, `router.tsx`, `cockpit/CockpitSidebar.tsx`, `editor/ArtifactSectionEditor.tsx`, `editorStore`
- **[High]** Ctrl+Z after section switch corrupts active section (`CodeMirrorEditor.tsx:225-235`): add `Transaction.addToHistory.of(false)` to the controlled value-push; clear history on full-doc swap.
- **[High]** Unsaved buffers discarded on tab switch/nav (`Cockpit.tsx:59-62`): unsaved-changes guard (`useBlocker` + `beforeunload`) and/or keep editors mounted (CSS-hide).
- **[High]** CockpitChat streams wrong workspace mid-nav (`CockpitChat.tsx:29-39,61-66`, `AppShell.tsx:44`, `router.tsx:42`): key the cockpit subtree by workspace id (remount) — also resolves stale `bottomTab`/`collapsed`/`openPeek` (`Cockpit.tsx:14-36`).
- **[Low/Med]** `CockpitSidebar` re-renders on every caret move (`CockpitSidebar.tsx:42`): selector subscription (`useSyncExternalStoreWithSelector`) for `dirty` only.
- **[Low]** `ArtifactSectionEditor` save-state effect omits deps (`ArtifactSectionEditor.tsx:121-127`): include `sections`/`byIndex` or compute inline.
- **Test (vitest):** undo after section switch does not mutate the other section's buffer; navigation with a dirty buffer is blocked/confirmed; switching workspace remounts (no stale jobId/stream); sidebar does not re-render on caret-only change.

### B17: Frontend misc + SSE headers — `frontend/src/App.tsx`, `router.tsx`, `api/client.ts`, `api/events.ts`, `routes/AdminUsers.tsx`, `routes/AdminPendingUsers.tsx`, `routes/Settings.tsx`, `cockpit/CitationComposer.tsx`, `api/proposals.ts`, `routes/Deadline.tsx`, `routes/CallDetail.tsx`; backend `web/api/turns.py`, `web/api/events.py`
- **[Medium]** Global `staleTime=0` + focus-refetch thundering herd (`App.tsx:9`): set `staleTime` (10-30s); keep `refetchInterval` only for live pollers.
- **[Medium]** Single un-code-split 1.23 MB bundle (`router.tsx:1-30`): `React.lazy`+`Suspense` for Cockpit/CodeMirror and `admin/*`.
- **[Medium]** SSE prod stream omits anti-buffering headers (backend `turns.py:340-349`): shared `SSE_HEADERS` (`Cache-Control: no-cache`, `X-Accel-Buffering: no`) used by `turns.py` + `events.py`.
- **[Low]** `unwrap` treats non-JSON `/api` error as success (`client.ts:34-54`): throw on any `res.response.ok === false`.
- **[Low]** `useEventStream` stale handlers (`events.ts:11-86`): read handlers through a ref updated each render.
- **[Low]** Admin `<a href="/admin">` full reload (`AdminUsers.tsx:120-121`, `AdminPendingUsers.tsx:69-70`): `<Link>`; delete stale Jinja comments; grep other `href="/admin|/me|/profiles"`.
- **[Low]** Settings subscriptions local state diverges after save (`Settings.tsx:144-148`): reconcile from mutation response.
- **[Low]** CitationComposer offset assumes single `<pre>` text node (`CitationComposer.tsx:34-53`): measure a Range from `pre` start.
- **[Medium/coordinate B13]** Deadline date tz (`Deadline.tsx:8-16`, `CallDetail.tsx:112`): consume the Stockholm-localized read-model field from B13.
- **Test (vitest):** unwrap throws on a 502 non-JSON mutation; admin link is client-side; settings reconciles from response; lazy chunks resolve.

---

## Phase C — Verify & merge

- Full CI surface in the worktree: `gen_no_build.py --check`, `gen_routes.py --check`, `ruff check .`, `pytest` (grouped by touched area), frontend `vitest` + `tsc && vite build` + OpenAPI drift.
- Adversarial whole-branch review (most-capable model) over `git merge-base master HEAD`..HEAD: parity/over-build + correctness/security. Fix wave for Critical/Important.
- `finishing-a-development-branch`: present merge options; merge `--no-ff` to LOCAL master (not pushed).

## Phase D — Coverage-gap follow-up review

Focused review pass (the completeness-critic next round), highest-value first:
1. `auth/redirects.py` (never opened) + residual magic-link path (`main.py`/`auth/email.py`/migration `0050`) — alternate-login / open-redirect.
2. `admin_bootstrap.py` (privilege-granting) — re-trigger / env-gating / arbitrary-email promotion.
3. Shipped CSP/HSTS/X-Frame/Referrer/Permissions-Policy values on `/api` + SPA `index.html`.
4. Migration reversibility across 55 migrations; root-cause the `UndefinedTable "references"` ordering flake.
5. Double-instance scheduler behaviour (now testable against B1's lock).
6. `schema.gen.ts` drift gate + embedding-dimension contract seams.
7. `logging.py` + auth-path log injection / PII (CRLF from email/Origin/next).
8. `workspaces.py` (top-level) authz; `web/api/__init__.py` router assembly.
