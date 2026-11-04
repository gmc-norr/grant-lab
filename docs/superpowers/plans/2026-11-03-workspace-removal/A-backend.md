# Backend code that stays: edits needed to remove the proposal workspace and the edition split

HEAD is `388494c8` (READ). The working tree has 2 doc changes, both outside `src/`. I edited nothing and ran no tests, builds or app.

"READ" means I opened the file and saw it. "INFERRED" means I reasoned it without checking directly. Line numbers are at `388494c8`; each edit also names its enclosing function, class or block.

## 0. Findings the plan must know first

**1. The live match app runs proposal-assist code today.** This all stops after the removal. None of it is visible to users.
- **Scheduler jobs, always registered (READ).** `scheduler.py` registers these in match regardless of edition:
  - `collab:checkpoint_tails`, every 60 s. `COLLAB_CHECKPOINT_SCAN_SECONDS` defaults to 60 and is not set in `.env`; I checked key names only. It imports PA `collab.checkpoint` and `collab.handover` and runs `checkpoint_active_tails`, a DB scan that can write git commits under `var/workspaces`.
  - `jobs_retirement_reconcile_tick`, every 60 s. Runs PA `reconcile_cancelled_jobs`, a DB update plus commit.
  - `passes_reconcile_tick`, every 60 s. Runs PA `PASS_REGISTRY` on the app loop.
  - `turn_drain`, every 5 s. Imports PA, then logs "skipped".
  - `coherence_drain`, `evaluator_drain`, `review_drain` and `uncited_claims_drain`, every 20 s. Each imports PA before its drafting guard, then returns.
  - Five reapers (`coherence_reaper`, `evaluator_reaper`, `review_reaper`, `uncited_claims_reaper`, `turn_reaper`). They run PA DB updates.
  - `proposal_assist_phase0_tick` (30 s) and `compaction_tick` (300 s). These only log "skipped"; they do not import PA.
  - `config.py:1010-1021` confirms all this in its own words: "a match process DOES run those ticks".
- **Sign-out routes (READ).** The 5 routes that call `signal_user_revocation` import `grant_lab.proposal_assist.collab.permissions` at request time. That import pulls in pycrdt. The call then schedules `revoke_user_sockets` on `app.state.app_loop` (which is set in match, `main.py:319`) and blocks up to 5 s (`_REVOCATION_BARRIER_SECONDS = 5.0`). With no rooms, `MANAGER` returns 0, so it does nothing.
- **Start-up reset (READ).** Every boot runs `UPDATE references SET text_status='failed' … WHERE text_status='running'` (`main.py:154-182`). This is not edition-gated.
- **Not run in the live app (READ, key names only).** `COLLAB_KILL_SWITCH` is not in `.env` and defaults to False, so the start-up handover (`main.py:125`) does not run. In match, `collab_state` arming, the 4 workspace exception handlers, `citation_service`, `reference_ingest_limiter` and the PA routers are all skipped.

**2. Live modules that become orphaned or break (READ):**
- `web/api/events.py:10` imports `SSE_HEADERS` from `web/api/turns.py`, which is being deleted. It must be changed.
- `web/http_headers.py` has only two importers, `export.py` and `files.py`, both deleted. Delete the file; `tests/test_content_disposition.py` goes with it.
- `db/session.py`: `make_drafting_engine` and `get_db_dependency` are only used by PA (`proposal_assist/web/_deps.py`).
- `net_safety.SSRF_REFUSAL_MESSAGE` is only used by `web/api/references.py` and by `tests/test_net_safety.py:31,169`.

**3. Coupled scripts outside `src/` that break when this lands (READ):**
- `scripts/gen_env_example.py` `DAY_ONE` (lines 61-73) raises `SystemExit` for any key that is no longer a `Settings` alias. It must drop:
  - `DRAFTING_ENABLED`, `DRAFTING_ADAPTER`, `PROPOSAL_ASSIST_LLM_MODEL`, `DRAFTING_MAX_LIVE_SUBPROCESSES`, `DRAFTING_DB_POOL_SIZE`
  - `TURN_MAX_SECONDS`, `WORKSPACES_DIR`, `COLLAB_KILL_SWITCH`, `RETENTION_INSPECTION_ENABLED`
  - `CITATION_LLM_PROVIDER`, `CITATION_LLM_ALLOW_CLOUD`, `REVIEW_DOLLAR_CAP_DEFAULT`, `REVIEW_RUNS_PER_DAY_DEFAULT`
- `scripts/gen_architecture.py:24` sets `FENCING = src/grant_lab/proposal_assist/fencing.py` and reads it at line 284. It will crash once the package is gone.
- `scripts/gen_openapi_types.sh:46` passes `APP_EDITION=full`. That becomes harmless, but the line and its comment are stale.

**4. Two edits that would change an observable response byte.** Both are optional; see the open questions.
- Removing the CSP `ws_origin` token. In prod, `connect-src 'self' wss://<host>` becomes `connect-src 'self'`.
- Removing `capabilities.retention_inspection` from `GET /api/me`.

**5. Removing `/cockpit` from `frontend/routes.json` changes one URL's response.** Today `GET /cockpit` serves `index.html`, which shows the match SPA's soft 404 page. Afterwards it is FastAPI's JSON 404.

## Task A: sign-out websocket hook (×5) and admin drafting

### A1. `src/grant_lab/web/api/admin_users.py` (READ)
- **`admin_approve_user` (138-175):** delete lines 166-174, the whole `if body.initial_password:` block and its lazy import. The epoch bump at line 160 stays. Result:
  ```python
          u.approved_by_user_id = admin.id
          u.approved_at = datetime.now(UTC)
          db.commit()
      return AdminActionOut(id=user_id)
  ```
- **`admin_disable_user` (216-239):** delete lines 233-238. The `u.session_epoch = … + 1` at line 231 stays.
  ```python
          u.status = "disabled"
          u.session_epoch = (u.session_epoch or 0) + 1
          db.commit()
      return AdminActionOut(id=user_id)
  ```
- **`admin_reset_password` (282-306):** delete lines 300-305. The epoch bump at line 298 stays. It now ends with `db.commit()` then `return AdminActionOut(id=user_id)`.
- No import changes are needed.

### A2. `src/grant_lab/web/api/auth.py` (READ)
- **`logout` (191-237):** delete the `revoked` flag and the signal block. If only the call were deleted, `revoked` would be assigned but never read, and ruff F841 would fail.

  Before (209-233):
  ```python
              revoked = False
              with session_factory() as db:
                  user = lock_user(...)
                  # The epoch must MATCH ...
                  if user is not None and (user.session_epoch or 0) == data.session_epoch:
                      user.session_epoch = (user.session_epoch or 0) + 1
                      db.commit()
                      revoked = True
              if revoked:
                  ... from grant_lab.proposal_assist.collab.permissions import signal_user_revocation
                  signal_user_revocation(request, data.user_id)
  ```
  After (keep the comment at 212-219 as it is):
  ```python
          if data is not None:
              with session_factory() as db:
                  user = lock_user(db, User.id == data.user_id)  # S1: epoch writer
                  # The epoch must MATCH, not merely be signature-valid. ...(unchanged 212-219)
                  if user is not None and (user.session_epoch or 0) == data.session_epoch:
                      user.session_epoch = (user.session_epoch or 0) + 1
                      db.commit()
      response.delete_cookie(...)
  ```
- **Comment in `register` (158-160).** It talks about the agent SDK's 19-tool allowlist. Replace it with:
  ```python
      # Silent return keeps anti-enumeration intact.
  ```

### A3. `src/grant_lab/web/api/me.py` (READ)
- **`change_password` (187-260):** delete lines 240-250, the comment, the PA import and the `signal_user_revocation(..., close_code=WS_RECONNECTABLE_CLOSE)` call. The epoch bump (237), `new_epoch` (238) and the cookie re-mint (252-259) stay. Optional: line 251 says "same cookie attrs as the Jinja route", which is history; it could read "same cookie attributes as POST /auth/login".
- **`get_me` (45-66):** drop the retention capability. Its only reader is `AdminDeletedWorkspaces.tsx`, which is being deleted (READ by grep).
  ```python
  @router.get("/me", response_model=MeOut)
  def get_me(user: User = Depends(get_current_user)):  # noqa: B008
      return MeOut(
          user=UserOut(id=user.id, email=user.email, name=user.name, status=user.status,
                       default_profile_id=user.default_profile_id),
          capabilities={"admin": user.is_admin},
      )
  ```
  - `capabilities` is `dict[str, bool]`, so the OpenAPI schema does not change. The JSON loses `"retention_inspection": false`; it is always false today because the setting defaults to off and `.env` does not set it.
  - `tests/test_api_me.py` needs trimming.

**What becomes dead after A1-A3:** the import of `WS_RECONNECTABLE_CLOSE` and the `close_code` keyword. Both were only ever used from PA `collab/permissions.py`, so nothing in live code still references them (READ). `app.state.app_loop` loses its last live reader (see C1).

**Tests outside `tests/proposal_assist`:**
- `tests/test_api_session_revocation.py` patches `grant_lab.proposal_assist.collab.permissions.signal_user_revocation`. Its whole subject is this hook, so delete it. Epoch bumps are covered by `test_api_auth.py`, `test_api_admin_users.py`, `test_api_me_password.py` and `test_auth_credential_write_races.py` (READ by grep).
- `tests/test_admin_users_jinja_surface_absent.py` also mentions the hook.

### A4. `src/grant_lab/web/api/admin.py` (READ)
- Line 3: delete `import uuid`. Its only users are `PerWorkspaceOut` and `RecentEventOut`.
- Lines 12-23: drop `ChatTurn`, `Workspace` and `WorkspaceEvent` from the `grant_lab.db.models` import. Keep `Call`, `DigestRun`, `Profile`, `RankingJob`, `RankingJobStatus`, `Source` and `User`.
- Lines 30-36: delete the comment and `drafting_router = APIRouter()`.
- Lines 276-390: delete `_drafting_panel_context` (with its lazy imports of PA `turns.caps` and `timeutil`), `GlobalCapOut`, `PerWorkspaceOut`, `RecentEventOut`, `AdminDraftingOut` and the `admin_drafting` route.
- Everything else stays. `text`, `or_`, `func`, `timedelta`, `HTTPException` and `Literal` are all still used.
- Tests: `tests/test_api_admin_drafting.py`, plus `tests/architecture/test_edition_surface.py:377` and `full_edition_routes.txt`.

## Task B: `src/grant_lab/scheduler.py` (READ)

**Imports and helpers**
- Line 8: delete `from pathlib import Path`. It is only used by `_analysis_*` (1436, 1452); `_turn_drain_tick` re-imports it locally.
- Line 9: change to `from typing import Protocol`.
- Lines 32-35: delete the `if TYPE_CHECKING:` block that imports `WorkspaceController`.
- Lines 43-50: rewrite the comment and keep the value 30, so behaviour does not change:
  ```python
  # APScheduler's default thread pool is 10 workers. Some ticks hold a worker for
  # minutes (the rank drain, up to RANKING_TICK_BUDGET_SECONDS; a per-source
  # ingest). The pool is sized well above the job count so a slow tick never
  # delays the crons that must run on time — daily ingest, the embedding
  # backfill, the weekly digest.
  SCHEDULER_EXECUTOR_MAX_WORKERS = 30
  ```
- `start_scheduler_lock_watchdog` docstring (288-289): change "blocked on an LLM pass — which is the whole reason SCHEDULER_EXECUTOR_MAX_WORKERS is 30" to "busy".
- Lines 329-349: delete `_drain_pass_future`. Its only callers are the pass ticks.

**`build_scheduler` signature (352-379):** delete lines 371-377 (`workspace_controller`, `citation_service`, `analysis_embedder`, `app_loop` and their comments). The final signature is:
```python
def build_scheduler(*, session_factory: sessionmaker, settings: Settings,
    alerter: ConsecutiveFailureTracker | _Alerter, ollama_url: str, gemma_model: str,
    ranking_tick_seconds: int, ranking_queue_batch: int, embedding_pregate_threshold: float,
    embedding_hard_fail_threshold: float | None = None, anthropic_api_key: str | None = None,
    remote_rank_model: str = "claude-sonnet-5-5", ranking_escalation_min_score: int = 45,
    ranking_remote_daily_cap: int = 300, embed_client: OllamaEmbedClient,
    digest_email_client: EmailClient | None = None, digest_settings=None,
    limiters: list[SlidingWindowLimiter] | None = None, start: bool = True,
) -> BackgroundScheduler:
```

**Remote-rank client comment (436-438):** delete the sentence "Compaction, Phase 0 and the citation client pin the same 0." Keep `max_retries=0`.

**Jobs to delete:**

| Lines | Job id | Function | Registered in live match? |
|---|---|---|---|
| 545-571 | `collab:checkpoint_tails` | `_collab_checkpoint_job` | yes; runs PA |
| 889-932 | `proposal_assist_phase0_tick` | `_phase0_tick` | yes; returns early |
| 934-952 | `jobs_retirement_reconcile_tick` | `_jobs_reconcile_tick` | yes; runs PA |
| 954-991 | `passes_reconcile_tick` | `_passes_reconcile_tick` | yes; runs PA |
| 1020-1088 | `drafting_idle_sweep` | `_drafting_idle_sweep` | no (only when drafting is on) |
| 1090-1134 | `turn_drain` | `_turn_drain_tick` | yes; imports PA |
| 1136-1170 | `coherence_drain` | `_coherence_tick` | yes; imports PA |
| 1172-1196 | `coherence_reaper` | `_coherence_reaper_tick` | yes; runs PA |
| 1198-1232 | `evaluator_drain` | `_evaluator_tick` | yes; imports PA |
| 1234-1254 | `evaluator_reaper` | `_evaluator_reaper_tick` | yes; runs PA |
| 1256-1282 | `review_drain` | `_review_tick` | yes; imports PA |
| 1284-1300 | `review_reaper` | `_review_reaper_tick` | yes; runs PA |
| 1302-1332 | `uncited_claims_drain` | `_uncited_claims_tick` | yes; imports PA |
| 1334-1355 | `uncited_claims_reaper` | `_uncited_claims_reaper_tick` | yes; runs PA |
| 1357-1382 | `turn_reaper` | `_turn_reaper_tick` | yes; runs PA |
| 1384-1421 | `compaction_tick` | `_compaction_tick` | yes; logs "skipped" |
| 1423-1487 | `claim_analysis_drain`, `claim_analysis_reconcile` and the `else` log | `_analysis_drain_tick`, `_analysis_reconcile_tick` | no |

In practice, delete **545-571**, **889-991** and **1020-1487**. Keep 993-1018 (`profile_bootstrap_tick`), 1489-1491 (`if start` / `return`) and `trigger_job_now`.

**Jobs that remain:**
- `ingest:<slug>` (one per source)
- `ingest:stale_check` (only if the alerter has a sender)
- `rank:drain`
- `golden_eval:weekly`
- `embeddings:refresh-missing`
- `ranking:reconcile`
- `boards:auto-publish`
- `digest:weekly` (only if email is configured)
- `rate_limit:sweep`
- `profile_bootstrap_tick`

`admin.py` triggers `digest:weekly` and `ingest:{slug}`; both stay.

**Effect on match:** no user-visible change. Some job rows may already be stuck in a running state from earlier; they stay as they are, and no code reads them. The log stops getting a "skipped" line every 5 s and 30 s.

**Callers of `build_scheduler`:**
- In `src/`, only `main.py` (import at line 185, call at line 283).
- In tests, 22 files mention it; 12 of them are in `tests/proposal_assist`.
- Outside that folder:
  - 8 files make 21 calls: `test_scheduler.py` (12), `test_scheduler_embedding_cursor.py` (3), and 1 each in `test_profiles_bootstrap_worker`, `test_ranking_freshness_wiring`, `test_ranking_remote_effort`, `test_scheduler_digest`, `test_scheduler_ranking_logprobs` and `test_scheduler_single_instance`.
  - `test_config_escalation_settings` and `test_ranking_scoring_config` mention it without calling it.
  - About 22 lines in 6 files pass removed keywords: `test_scheduler.py` (14), `test_scheduler_embedding_cursor` (3), and 1 each in `test_profiles_bootstrap_worker`, `test_scheduler_digest`, `test_scheduler_single_instance` and `test_analysis_pipeline`.
  - Because the parameters are keyword-only, those calls raise `TypeError`.

## Task C: main.py, the router package, SPA serving and the edition

Tasks B and C must land in the same commit: `main.py` passes the keywords that B deletes.

### C1. `src/grant_lab/main.py` (READ)

**Lifespan**
1. Lines 46-50: the comment's list "(workspaces.py, scheduler.py, admin_bootstrap.py)" becomes "(scheduler.py, admin_bootstrap.py)".
2. Lines 55-63: replace the lock comment with:
   ```python
       # Single-instance enforcement comes FIRST — before the admin bootstrap below,
       # which mutates shared state a second process would fight over. The scheduler
       # is in-process APScheduler, so two replicas would each fire every job; the
       # login limiters are per-process too. Holding the session-level advisory lock
       # on a dedicated connection for the process lifetime means "I am the instance".
   ```
3. Lines 118-121: "(a missing retirement hook, a bootstrap failure)" becomes "(a bootstrap failure)".
4. Delete 123-140, the `collab_kill_switch` handover.
5. Delete 154-182, the `Reference.text_status` reset and its `_sql_update` and `Reference` imports.
6. Delete 211-271, the "EDITION GATE" comment and the whole `if settings.drafting_enabled:` block (`workspace_controller`, `analysis_embedder`, retirement, lifecycle). Keep line 273, `import asyncio as _asyncio`; the teardown's `to_thread` still needs it.
7. Delete 308-315 in the `build_scheduler(...)` call: `workspace_controller=`, the `citation_service` comment and argument, `analysis_embedder=` and `app_loop=`. `start=start_scheduler` stays.
8. Delete line 319, `app.state.app_loop = _asyncio.get_running_loop()`. After A and B, nothing live reads it.
9. Teardown: delete 340-365 (drafting subprocess terminate). Rewrite the comment at 366-376:
   ```python
          # wait=True so an in-flight rank-drain or ingest job completes against the
          # still-open pool (the rank tick runs every RANKING_TICK_SECONDS).
          #
          # Run the BLOCKING shutdown off the event loop (to_thread) so the loop is
          # not held for as long as the longest in-flight tick.
          #
          # No `running` guard needed: ... (keep 377-383 unchanged)
   ```

Resulting lifespan:
```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    # (comment from step 2)
    from grant_lab.scheduler import SingleInstanceViolation, probe_scheduler_lock
    # 71-116 unchanged (reentrant check, probe, standby/fatal handling)
    try:
        from grant_lab.admin_bootstrap import bootstrap_admins
        bootstrap_admins(...)
        from grant_lab.ingestion.alerts import ConsecutiveFailureTracker
        from grant_lab.scheduler import build_scheduler
        settings = app.state.settings
        alerter = ConsecutiveFailureTracker(...); app.state.ingestion_alerter = alerter
        # digest_email_client block unchanged (196-209)
        import asyncio as _asyncio
        scheduler = build_scheduler(session_factory=..., settings=settings, alerter=alerter,
            ollama_url=..., gemma_model=..., ranking_tick_seconds=..., ranking_queue_batch=...,
            embedding_pregate_threshold=..., embedding_hard_fail_threshold=...,
            anthropic_api_key=..., remote_rank_model=..., ranking_escalation_min_score=...,
            ranking_remote_daily_cap=..., embed_client=app.state.embed_client,
            digest_email_client=digest_email_client, digest_settings=settings,
            limiters=[app.state.email_limiter, app.state.ip_limiter, app.state.board_limiter],
            start=start_scheduler)
        app.state.scheduler = scheduler
        # watchdog block unchanged (320-332)
    except BaseException:
        if scheduler_lock is not None: scheduler_lock.release()
        raise
    try:
        yield
    finally:
        # watchdog stop, to_thread(scheduler.shutdown, wait=True), lock release, engine.dispose()
```

**`create_app`**

10. Delete 457-463, the `if settings.app_edition != "match":` collab kill-switch arming.
11. CSP (530-546). **Option B, keep (recommended for piece 1)**: no code change. Replace the comment at 533-536 with:
    ```python
        # The non-dev CSP adds one same-origin ws(s) token derived from APP_BASE_URL;
        # deriving it also validates the APP_BASE_URL host at startup (_derive_ws_origin).
    ```
    **Option A, remove**: see the open questions for the exact edits.
12. Delete 621-752, the four workspace exception handlers and their PA imports. Starlette looks handlers up by exception class, so the remaining ones are unaffected (INFERRED).
13. `_lock_timeout` (754-817): keep the code. Replace the comment at 756-769 with:
    ```python
        # SQLSTATE 55P03 = lock_not_available: a `lock_timeout` fired. That is routine
        # contention, not a server fault, so it answers 429 + Retry-After instead of
        # 500. No route sets `lock_timeout` today (db/session.py does not either).
    ```
    Replace the comment at 798-803 with `# Routine contention: WARNING, not ERROR, but logged so it is discoverable.`
    - Keep the event names `mutex_tail.lock_timeout` and `mutex_tail_lock_timeout`, and the `BUSY_ERROR_MESSAGE` bytes. These keep logs and wire output unchanged.
14. Delete 891-908, the `citation_service` block. Nothing live reads `app.state.citation_service` (READ).
15. Delete 949-957, the `reference_ingest_limiter` block.
16. Delete 967-999, the PA router imports and the 11 `include_router` calls.
17. Line 1000 becomes `app.include_router(build_api_router())`.
18. Line 1016 becomes `mount_spa(app, spa_mode=settings.spa_mode)`.

Imports stay as they are: `BUSY_ERROR_MESSAGE`, `is_lock_timeout`, `urlsplit`, `re` and `JSONResponse` are still used.

**Effect on match:** the route table, handlers and middleware are unchanged under option B. The only behaviour difference is that boot no longer runs the `references` UPDATE.

Resulting `create_app` order:
1. Logging and `Settings`.
2. Embedding-dimension check.
3. Email sender.
4. Engine.
5. `FastAPI(...)`.
6. CSRF, security headers, body cap, gzip.
7. Proxy warning.
8. Handlers: `_http_exception`, `_lock_timeout`, `_inactive_account`, `_api_validation`, `_api_500`.
9. `app.state` settings, engine and session factory, then the calibration check.
10. Email sender, the three limiters, signer, cipher, OIDC client, `embed_client`.
11. Mount `/static`.
12. Routers: `health`, `oauth_google`, then `build_api_router()`.
13. The dev-only `events` router.
14. `mount_spa`.

### C2. `src/grant_lab/web/api/__init__.py` (READ): final file

Router order is the same as today's `_ALWAYS`, so the OpenAPI order and route-matching precedence do not change.

```python
from __future__ import annotations

from fastapi import APIRouter

from grant_lab.web.api import (
    admin, admin_golden_set, admin_users, auth, boards, calls, me, profiles, public_boards,
)
from grant_lab.web.api.errors import STATUS_CODES, ApiError

# (keep the error-envelope comment, lines 27-34)

_ROUTERS = (
    auth.router,
    me.router,
    admin.router,
    admin_users.router,
    calls.router,
    profiles.router,
    boards.router,
    public_boards.router,
    admin_golden_set.router,
)


def build_api_router() -> APIRouter:
    """Compose the /api router."""
    r = APIRouter(prefix="/api", responses={code: {"model": ApiError} for code in STATUS_CODES})
    for sub in _ROUTERS:
        r.include_router(sub)
    return r
```
- This deletes `from typing import Literal`, the comment at 7-13, the comments inside `_ALWAYS` (36-61), `_full_only_routers()` (65-116) and the `edition` parameter. The old default was `"full"`.
- Renaming `_ALWAYS` breaks `tests/architecture/test_edition_surface.py`, which parses that name with `ast`, and makes stale the comments at `frontend/src/router.simple.tsx:127` and `AdminDashboard.test.tsx:143`. Keep the name `_ALWAYS` if you would rather not touch those.

### C3. `src/grant_lab/web/spa_serving.py` (READ)
- Delete `packaged_edition()` (30-59).
- New `mount_spa` head, replacing 62-90:
  ```python
  def mount_spa(app: FastAPI, *, spa_mode: str) -> None:
      """Serve the built SPA when spa_mode == 'prod'. 'dev' is served by Vite;
      'disabled' mounts nothing. Must be called AFTER all API/HTML routers are
      included so real routes win and only unmatched paths reach the fallback."""
      if spa_mode != "prod":
          return
      index = SPA_DIR / "index.html"
      if not index.exists():
          raise RuntimeError(f"SPA build missing at {index} (SPA_MODE=prod)")
      assets = SPA_DIR / "assets"
      ...
  ```
- Comment at 145-146: "(/healthz, /auth, /workspaces …)" becomes "(/healthz, /auth …)".
- Effect: the gitignored marker `src/grant_lab/web/spa/edition.txt` (currently in the live tree) is simply no longer read. The live check passes today, so nothing changes.
- Tests: `tests/test_board_preview_headers.py:25` passes `edition="match"`. `tests/architecture/test_spa_edition_match.py` (23 lines) and `test_package_spa.py` (5) touch the marker.

### C4. `src/grant_lab/web/spa_routes.py` (READ)
- This file is generated; do not hand-edit it.
- In `frontend/routes.json`, remove `"/cockpit"` from `spa_routes` and `"/workspaces"` from `backend_prefixes`, then run `uv run python scripts/gen_routes.py`. That regenerates both this file and `frontend/src/routes.gen.ts`.
- Expected `SPA_ROUTES` (INFERRED from the generator's sort by length, descending, which keeps the existing order for ties): `("/auth/no-access", "/auth/pending", "/auth/error", "/profiles", "/register", "/boards", "/calls", "/admin", "/login", "/me", "/b", "/")`.
- Expected `BACKEND_PREFIXES = ("/healthz", "/static", "/auth", "/api")`.
- There are no `drafting` or `deleted-workspaces` entries; those only exist in `router.tsx`.

## Task D: `src/grant_lab/config.py` (READ)

**Fields to delete.** I grepped each field name; after tasks A-C and E, no live code outside `config.py` reads any of these:

| Lines | Fields (env var names) |
|---|---|
| 208-300 | `drafting_enabled` (`DRAFTING_ENABLED`), `app_edition` (`APP_EDITION`), `extractor_version`, `rubric_extractor_version`, `proposal_assist_llm_model`, `proposal_assist_max_turns_interactive`, `proposal_assist_max_turns_pass`, `phase0_tick_seconds`, `passes_reconcile_tick_seconds`, `jobs_reconcile_tick_seconds` |
| 309-387 | `workspaces_dir` (`WORKSPACES_DIR`), `drafting_adapter`, `drafting_max_live_subprocesses`, `drafting_db_pool_size`, `workspace_idle_timeout_seconds`, `workspace_idle_sweep_seconds`, `collab_checkpoint_scan_seconds`, `collab_checkpoint_min_tail` |
| 401-764 | see the list below |

Lines 401-764 hold:
- `collab_kill_switch`, `retention_inspection_enabled`
- `daily_token_cap_input`, `daily_token_cap_output`, `global_daily_token_cap_input`, `global_daily_token_cap_output`
- all `coherence_*` (6), `evaluator_*` (6), `uncited_claims_*` (6, plus the comment at 543) and `review_*` (11, plus the comment at 579)
- `problem_section_char_budget`, `citation_deep_check_max_per_request`
- all 9 `citation_llm_*` and `global_citation_llm_*` fields
- `reference_extract_timeout_seconds`, `reference_ingest_concurrency` (no alias, so its env name is `REFERENCE_INGEST_CONCURRENCY`)
- `compaction_turn_threshold`, `compaction_token_threshold`, `compaction_tick_seconds`
- `analysis_tick_seconds`, `analysis_reconcile_tick_seconds`, `turn_drain_tick_seconds`, `turn_max_seconds`

**Keep:** `profile_bootstrap_tick_seconds` (301-308) and `scheduler_standby_ok` (388-400). `remote_rank_model` starts at 765.

**Also delete:**
- Line 1, `from decimal import Decimal` (only the `review_*` fields use it).
- Lines 30-38, `_LOCAL_SPEND_TAG` and its comment.
- Lines 902-932, the `_citation_model_cannot_forge_the_local_tag` validator.
- Lines 1000-1031, the `_match_edition_disables_drafting` validator.

**Description and comment rewrites.** The first two flow into `.env.example`, so regenerate it.
- `app_base_url` description (113-117):
  ```
  "The origin browsers use, scheme included, no path. It is what the CSRF origin check compares against, so in SPA_MODE=dev it is the Vite origin (http://localhost:5173); https:// switches on the production checks."
  ```
- `scheduler_standby_ok` description (391-398):
  ```
  "This app is single-instance: the in-process scheduler and the login limiters are per-process state. When another process already holds the single-instance advisory lock, startup is FATAL by default. Set true to boot an HTTP-only standby instead: it serves requests, runs no scheduler, and /healthz answers 200 with scheduler_lock=false. Without this flag a process that does not hold the lock answers /healthz 503."
  ```
- Comment at 852-855 (`max_request_body_bytes`): change to "App-level cap on request body size (bytes). Backstop behind a reverse proxy's client_max_body_size. 0 disables the check." Keep the 64 MiB default so behaviour does not change.

**Settings that live code still reads:** none of the deleted ones.
- `workspaces_dir` is not part of the data-directory layout. `CALL_MANAGER_DATA_DIR` is read directly through `os.environ` by `ranking/eval.py:32,35` and `ranking/validate_ev.py:183`, not through `Settings`. Do not rename it.
- Migrations 0037, 0066 and 0068 read `os.environ["WORKSPACES_DIR"]` directly, so removing the setting does not affect them.
- `Settings` keeps `extra="ignore"` (line 54), so the live `.env` keys `APP_EDITION` and `DRAFTING_ENABLED` (both present) are harmlessly ignored.
- Coupled: the `scripts/gen_env_example.py` `DAY_ONE` edits (section 0), then regenerate `.env.example`.
- About 49 test files outside `tests/proposal_assist` mention removed setting names; anything that reads `settings.<removed>` will raise `AttributeError`.

## Task E: all other files

- **`web/api/events.py` (READ, required).** Replace line 10 with a local constant whose bytes match `turns.py:42-46`:
  ```python
  router = APIRouter()

  # Every SSE response: no caching, and no proxy buffering, so each event is
  # delivered as it is written.
  SSE_HEADERS: dict[str, str] = {
      "Cache-Control": "no-cache",
      "X-Accel-Buffering": "no",
      "Connection": "keep-alive",
  }
  ```
  Line 33: drop "(mirrors workspaces.py)". The route is dev-only, mounted only when `spa_mode == "dev"`. See open question 3.
- **`web/middleware.py` (READ).**
  - Module docstring, lines 4-6: "breaks the SSE turn-stream endpoint (``/workspaces/.../turns/.../stream``)" becomes "breaks streaming responses (the dev-only ``/events/ping`` SSE stream)".
  - `build_csp`: under option B, rewrite the docstring at 42-46 to "``ws_origin`` (non-dev only) adds one same-origin ws(s) token, derived from APP_BASE_URL, to ``connect-src``." and the comment at 57-59 to "# Exactly ONE origin, derived from APP_BASE_URL — never a bare scheme."
- **`db/session.py` (READ).**
  - Delete `make_drafting_engine` (41-65), `get_db_dependency` (81-88) and the import at line 1, `from collections.abc import Iterator`.
  - Replace the comment at 22-30 with: `# This factory also backs the ingestion/embeddings CLIs and the test db_engine fixture, so a starved checkout under uv run pytest -n 16 fails in 5 s rather than 30.` That "backfill CLI" meant PA `references/backfill` (INFERRED).
- **`errors.py` (READ).** Keep both code objects. Rewrite:
  - The module docstring, lines 1-16: "Errors and wire-visible error strings shared by the whole application. Top-level on purpose: `grant_lab/db/__init__.py` imports every model, so a `grant_lab.db.*` home would drag SQLAlchemy into every importer."
  - The `is_lock_timeout` docstring at 25-27: "55P03 is routine contention, not a server fault."
  - The comment at 38-43: "Served with 429 + Retry-After: 2 when a lock timeout reaches the wire (main.py's OperationalError handler)."
  - Keep the string `"workspace is busy — try again in a moment"` byte for byte (see open questions).
- **`timeutil.py` (READ).** It stays because `ranking/remote_attempts.py:59` uses it. Change the docstring at 1-13 to: "Time boundaries the whole app agrees on. A function that decides when the current period started must TAKE the instant, never read the clock itself, so a test can pass one. `digest/send.py:week_start(now)` has the same shape for the week."
- **`web/filters.py` (READ).** Delete `phase_label` (95-105). The module stays: `digest/render.py:41-42` registers `display_date` and `display_dt` as Jinja filters for the email digest templates, and `calls.py` and `public_boards.py` use the other helpers. Nothing in `src/` or the templates uses `phase_label`; `tests/test_web_filters.py` tests only it, so delete that test file.
- **`web/http_headers.py`.** Delete the file, and `tests/test_content_disposition.py` with it.
- **`net_safety.py` (READ).**
  - Rewrite the docstring at 8-35 to name one caller, `sources/types.py`.
  - The `Resolver` comment at 52-60 mentions `fetcher._resolve_and_validate`; reword it.
  - Delete `SSRF_REFUSAL_MESSAGE` and its comment (61-75), and trim `tests/test_net_safety.py:31,169`. Alternatively keep it (open question 4).
- **Docstring paragraphs to delete:**
  - `web/profile_access.py` docstring 10-21: remove the `web/api/workspaces.py` references and keep the "eleven of twelve surfaces" rationale.
  - `boards/alerts.py` 3-8, `web/api/boards.py` 3-8 and `web/api/public_boards.py` 3-7: delete the "MATCH-edition module… test_match_edition_process_cut" paragraph.
  - `digest/send.py` 41-43: delete the last sentence.
- **Small comment edits:**
  - `web/api/profiles.py`: comment at 308-313 (drop "workspaces.py:427-428 already made this call…" and keep the enumeration reason); docstring at 750-760 (drop the `settings.py` references).
  - `web/api/errors.py` 51-55: replace "Measured 2026-10-28: `PUT /api/workspaces/{id}/rubric`…" with "A body field that refuses NaN echoes it back in the 422 detail, which is exactly that case."
  - `web/api/admin_golden_set.py` 107-109: "(proposal_assist/drafting/locking.py)" becomes "(grant_lab/errors.py:is_lock_timeout)".
  - `auth/domain.py` 66: "…get_current_user and the collab socket." becomes "…and get_current_user."
  - `profiles/create.py` 66: drop "Mirrors create_workspace —".
  - `profiles/bootstrap_worker.py` 3: "Mirrors run_phase0_tick:" becomes "Claims one queued job…".
  - `ranking/remote_attempts.py` 50-54: "Reuses the proposal-assist day-boundary helper" becomes "Uses timeutil.stockholm_day_start_utc", and drop `sum_spend_today`.
  - `ranking/anthropic_client.py`: comments only, at 48-51, 81-83, 89-91, 116-118, 162-166 and 185-191, which name compaction, citations, Phase 0 or proposal-assist. All code stays on the ranking path.
- **"Full edition" / "match edition" wording (comments only; API unchanged):**
  - `web/dashboard.py` 55-56 and 68: "the full edition sends no `view`" becomes "a client that sends no `view` gets the archive".
  - `web/dashboard.py` 459 and `web/api/calls.py` 719: "match edition ships no `min_score` control" becomes "no screen offers a `min_score` control" (INFERRED to be true once `Dashboard.tsx` goes).
  - `web/api/calls.py` 120, 385, 657 and 924: replace "the full edition" / "neither edition" with "the unranked archive" / "no client".
- **Model files (report only, not my area).** These still contain "proposal_assist" text, and G1 requires them to change: `db/models/boards.py` 3 and 6 (and 147), `documents.py` 253, `spend.py` 28, `__init__.py` 4-6, `profiles.py` 150-152.
- **Live code importing workspace-era models (READ):** only `main.py:162` (`Reference`) and `admin.py:14,21,22` (`ChatTurn`, `Workspace`, `WorkspaceEvent`). Both are removed above. No live code uses raw SQL against workspace tables. `LlmSpend` has no live user.
- **Entry points (READ).**
  - `pyproject.toml` has no `[project.scripts]`.
  - The CLIs `ingestion/cli.py`, `embeddings/cli.py` and `ranking/validate_ev.py` do not reference PA.
  - The systemd unit runs `uvicorn grant_lab.main:create_app --factory`; keep the name `create_app`.
  - No live `src/`, `scripts/` or `alembic/` file imports any library slated for removal. Migration 0030 mentions `proposal_assist` only in its docstring.

## Item 10: starting the app without the scheduler

**There is no such setting (READ).**
- The scheduler starts if and only if this process gets the advisory lock `pg_try_advisory_lock(hashtext('call-manager-scheduler'))` (`main.py:77-83`, `scheduler.py:150-231`).
- `SCHEDULER_STANDBY_OK=true` gives an HTTP-only boot only when another session already holds that lock.

Workaround for a boot check:
- Hold the lock from a `psql` session on the throwaway database, then boot with `SCHEDULER_STANDBY_OK=true` and `SPA_MODE=disabled`.
- Advisory locks are per database, so this does not touch the live app's lock (INFERRED).
- Even with the scheduler started, the first interval jobs fire after 30 s (`profile_bootstrap`) and 60 s (`rank:drain`). On an empty database neither reaches Ollama or the sources. Today `turn_drain` fires at 5 s, which is moot after the removal. The cron jobs (02:30-07:00, and the per-source ingest hours) would hit real sources only if the run spans those times (INFERRED).

## Order of work
1. A and E (including `events.py`), with all their readers of removed settings gone.
2. B and C together in one commit.
3. Delete the PA package and the 10 web API modules, plus `workspaces.py` and `http_headers.py`.
4. D, together with the `gen_env_example.py` `DAY_ONE` change and `.env.example` regeneration.
5. Regenerate the routes from `routes.json`, the OpenAPI types and the architecture map (after fixing `gen_architecture.py`'s `FENCING`).

## Open questions
1. **CSP `ws_origin`.** Option B (keep) leaves headers byte-identical. Option A removes it:
   - Edits: `main.py` 533-546 becomes `_csp = build_csp(dev=..., form_action_origin=...)`; delete `_derive_ws_origin` (420-432); retarget the `_HOST_PORT_RE` comment (409-416) and the `_derive_form_action_origin` docstring (441-442) to `GOOGLE_OIDC_DISCOVERY_URL`; drop the `ws_origin` parameter, docstring and branch from `build_csp`.
   - Effect: in prod, `connect-src` loses `wss://host`; no match page uses WebSockets. Malformed-`APP_BASE_URL` validation at start-up goes away too.
   - Tests to trim: `tests/test_security_headers.py:111-172`.
2. **`/api/me` `capabilities.retention_inspection`.** Drop it (recommended), or hard-code `False`?
3. **`web/api/events.py` and `/events/ping`.** Their only frontend client is `api/events.ts`, which only `CockpitChat` uses. If the frontend deletes `events.ts`, also delete `events.py`, the dev mount in `main.py` (1002-1008), `tests/test_events_ping.py` and the `test_auth_allowlist.py:46` entry. Otherwise inline the headers as in Task E.
4. **Dead constants.** Delete or keep `net_safety.SSRF_REFUSAL_MESSAGE`. Should the `BUSY_ERROR_MESSAGE` text "workspace is busy…" change? It is wire-visible but practically unreachable, since nothing sets `lock_timeout`.
5. **`/cockpit`.** Removing it from `routes.json` turns its soft 404 into a JSON 404. That is acceptable for bookmarks, or keep the entry.
6. **`_ALWAYS` renamed to `_ROUTERS`.** This breaks the AST parse in `test_edition_surface.py`; that test is likely deleted anyway.
7. **Already-dead code, outside this piece.** `sources/extraction.py` has no importers in `src/` (only tests). It is the only importer of `pymupdf` and `trafilatura`, which contradicts the spec's "libraries the live app uses". Its comment in `sources/__init__.py` (lines 18 and 24) is stale too.
8. **Match API parameters that only the full build sent.** `?view` defaulting to `archive`, and `min_score`. These are piece 3 candidates and are not touched here.
9. **The test fixture depends on the deleted test folder.** `tests/conftest.py`'s `_close_helper_entered_lifespans` imports `tests.proposal_assist._refs_helpers`. It is a test-owner item, but it breaks collection once that folder is gone.

### Critical files for implementation
- /home/parlar_ai/dev/grant-lab/src/grant_lab/main.py
- /home/parlar_ai/dev/grant-lab/src/grant_lab/scheduler.py
- /home/parlar_ai/dev/grant-lab/src/grant_lab/config.py
- /home/parlar_ai/dev/grant-lab/src/grant_lab/web/api/__init__.py
- /home/parlar_ai/dev/grant-lab/src/grant_lab/web/spa_serving.py
- /home/parlar_ai/dev/grant-lab/scripts/gen_env_example.py
