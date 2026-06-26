# Admin Slice 2 — dashboard + drafting + golden-set — React migration (FINAL admin slice)

**Date:** 2026-06-26
**Status:** design approved, pre-plan
**Part of:** the full-React migration (strangler-fig, route-by-route). The admin
sub-project was split into two slices; this is **Slice 2 — everything under
bare `/admin` except the already-migrated user management** ([[project_admin_user_mgmt_merged]]).

## Goal

Migrate the three remaining admin pages — the ops dashboard (`/admin`), the
drafting panel (`/admin/drafting`), and the golden-set manager
(`/admin/golden-set`) — plus their six ops/CRUD actions from Jinja2+HTMX to the
React SPA, and finish the admin Jinja teardown. After this slice the only Jinja
left in the app is the auth flow (login/register/status), which is the last
sub-project.

## Decomposition (why this is one slice, and why it is last)

Admin was split by sub-prefix because `is_spa_route(path)` matches exact-or-beneath:
Slice 1 registered `/admin/users` (capturing users + pending) while bare `/admin`
and `/admin/golden-set` stayed Jinja. **Slice 2 registers the bare `/admin`
prefix, which subsumes every `/admin/*` sub-prefix — so it MUST be the last admin
slice.** The user chose to keep Slice 2 whole (one branch) rather than peel
golden-set off: the three pages are data-heavy but logically simple (read-only
aggregation + thin CRUD), so the extra spec→plan→SDD cycle of a split isn't
warranted.

## Surface being migrated

All under `/admin`, all admin-gated (`require_admin`). Source:
`web/routes/admin.py` (dashboard + drafting + ops, minus the Slice-1 user-mgmt
already removed) and `web/routes/golden_set.py`; templates `admin.html`,
`admin_drafting.html`, `golden_set.html`.

**Pages:**
- `GET /admin` → `admin.html` — ops dashboard: source health (+ per-source
  "Fetch now"), ranking-queue status cards (+ "Re-rank everything" with confirm),
  embedding coverage, weekly-digest panel (last sent / runs this week / disabled
  users + per-user re-enable, + "Send digest now"), golden-set Kendall's-τ
  summary, embedding-calibration warning. Banner-on-action via `?ok=`/`?err=`.
- `GET /admin/drafting` → `admin_drafting.html` — **read-only**: kill-switch
  state (toggling is explicitly a backlog item — NOT added here), global daily
  token cap (Europe/Stockholm), per-workspace usage today, last-50
  failed/cancelled cloud-turn events.
- `GET /admin/golden-set` → `golden_set.html` — curated-triples table (+ delete),
  manual add form, candidate-mining table (dismissed-high-score /
  cosine-disagreement / escalation-disagreement, + inline add).

**Actions (currently form-POST → 303 redirect):** `rerank`, `digest/send`,
`digest/reenable`, `sources/{slug}/fetch`, `golden-set/add`, `golden-set/delete`.

## Architecture

Standard slice shape — additive typed `/api` → React routes under the AppShell
behind the existing `RequireAdmin` guard → final admin Jinja teardown.

### Backend — two new `/api` modules

1. **`web/api/admin.py`** (new, registered in `web/api/__init__.py`) — the
   dashboard + drafting reads and the four ops actions. The pure query
   context-builders (`_digest_panel_context`, `_embedding_coverage_context`,
   `_drafting_panel_context`) **port out of the doomed `web/routes/admin.py`
   into this module** (the slice deletes the Jinja module, so the logic has a new
   home; it is pure session-in/data-out, so this is a clean move). Models
   namespaced `Admin*`.
2. **`web/api/admin_golden_set.py`** (new) — golden-set read + add + delete,
   reusing the unchanged `ranking/golden_set.py` service (`list_triples`,
   `candidates_*`, `add_triple`, `delete_triple` over a passed-in session).

Reused services (unchanged, all already off the Jinja route modules):
`ranking/eval.py:load_last_report`, `ranking/backfill.py` (`BACKFILL_DAYS`,
`backfill_profile`), `proposal_assist/turns/caps.py`
(`check_global_daily_token_cap`, `_stockholm_day_start_utc`),
`digest/send.py:week_start`, `scheduler.py:trigger_job_now`,
`app.state.embedding_calibration_warning`.

#### Read endpoints

- `GET /api/admin/dashboard` → one read-model with everything `admin.html`
  renders: `sources: [{name, slug, last_fetched_at, call_count}]`;
  `queue_counts: {pending, running, done, failed}` (the four `RankingJobStatus`
  values, defaulting to 0); `rerank_estimate, active_profiles_count,
  recent_calls_count, backfill_days`; `digest: {last_sent_at, runs_this_week,
  disabled_users, disabled_user_rows: [{id, email, disabled_at}]}`;
  `embedding_coverage: {call_embedded, call_total, call_stale, profile_embedded,
  profile_total, profile_stale, current_model}`; `golden_set_report: {n_fixture,
  n_compared, tau} | null`; `calibration_warning: str | null`. One endpoint, not
  six — mirrors the single-page render.
- `GET /api/admin/drafting` → `{drafting_enabled: bool, global_cap: {used_in,
  cap_in, used_out, cap_out, reason, allowed}, per_workspace: [{id, title,
  owner_email, used_in, used_out}], recent_events: [{created_at, workspace_id,
  kind, payload}]}`. `per_workspace` preserves the builder's **`used_in` desc**
  order (list order is the contract). Serialization: the ported builders return
  ORM rows / a `CapCheckResult` dataclass (`global_cap`) / `Row` objects — not
  JSON; the `/api` layer maps them to these declared shapes via Pydantic.
  `recent_events.payload` is admin-only JSONB already exposed in Jinja (nothing
  newly sensitive; confirm it serializes cleanly).
- `GET /api/admin/golden-set` → `{triples: [{id, call_id, profile_id,
  expected_score, note, call_title, profile_name}], candidates: [{call_id,
  profile_id, call_title, profile_name, aggregate_score, embed_similarity,
  reason}]}` (1:1 with the `TripleRow`/`CandidateRow` dataclasses).

#### Mutation endpoints

- `POST /api/admin/rerank` → `200 {enqueued: N}` — enqueue `backfill_profile`
  for every active profile; **N = active-profile count, not job count** (one bulk
  backfill per profile). Idempotent (partial unique index dedup, same as Jinja).
- `POST /api/admin/digest/send` → `200 {triggered: bool}` — `trigger_job_now`
  on `digest:weekly`; `false` = unconfigured (no email backend).
- `POST /api/admin/digest/reenable` `{user_id}` → `200 {id}` (the Slice-1
  `AdminActionOut` shape); `404` unknown user. Clears `digest_disabled_at`
  (idempotent no-op if already enabled).
- `POST /api/admin/sources/{slug}/fetch` → `200 {triggered: bool}`; `404` unknown
  slug. `trigger_job_now` on `ingest:{slug}`; `false` = scheduler/job unavailable.
- `POST /api/admin/golden-set` `{call_id, profile_id, expected_score, note?}` →
  `201 {id}`; `400` if `expected_score` ∉ 0–100; **`400` "unknown call or
  profile"** on FK `IntegrityError` (a parity *improvement* — the Jinja route
  would 500 here) — the handler MUST `session.rollback()` before raising the 400
  or the session is poisoned for the rest of the request. `created_by` = the
  acting admin.
- `DELETE /api/admin/golden-set/{triple_id}` → `204` — idempotent (the service's
  `delete_triple` is a no-op when the row is absent; matches the Jinja redirect).

**Banner-UX translation:** the Jinja `?ok=`/`?err=` post-redirect banners become
JSON the React UI renders inline. Trigger actions return `{triggered: bool}`
(and rerank `{enqueued}`) rather than an error envelope for the
"couldn't-trigger / unconfigured" case, because that is an infra/config *state*,
not a client error. Genuine client errors stay envelope errors (`404` unknown
slug/user, `400` bad score/FK).

### React surface

- **Routes:** a nested `/admin` layout route (`AdminLayout`, below) wraps
  `/admin` → `AdminDashboard.tsx`, `/admin/drafting` → `AdminDrafting.tsx`,
  `/admin/golden-set` → `AdminGoldenSet.tsx`, plus the migrated Slice-1
  `/admin/users` + `/admin/users/pending`. `RequireAdmin` moves to the layout
  (wrap once).
- **Hooks:** `api/adminDashboard.ts` (dashboard + drafting reads, 4 ops
  mutations) and `api/adminGoldenSet.ts` (golden-set read, add, delete).
  Existing `api/admin.ts` (Slice-1 user-mgmt) untouched. Query hooks `retry:
  false`. Ops mutations invalidate `["admin-dashboard"]`; golden-set add/delete
  invalidate `["admin-golden-set"]`.
- **`AdminLayout.tsx` (nested layout route) + `AdminNav.tsx`** — rather than
  editing the Slice-1 page components, restructure `router.tsx` so all `/admin/*`
  routes nest under one `<AdminLayout>` that renders `<RequireAdmin>` once +
  `<AdminNav>` + `<Outlet/>`. `AdminNav` is the shared subnav (Operational
  health · Drafting · Golden set · Users · Pending) as react-router `<Link>`s.
  This connects the Slice-1 `/admin/users[/pending]` pages into in-SPA nav
  (cross-document-only before) **without touching their components** — only
  `router.tsx` changes (Slice-1 routes move under the layout; their per-route
  `RequireAdmin` wrappers collapse into the layout's).
- **SPA ActivityBar Admin entry** — an "Admin" item in the shell's ActivityBar,
  shown only when `capabilities.admin`, linking to `/admin`. This is the "wire
  SPA admin nav" item deferred from Slice 1.
- **`AdminDashboard`** — read-only panels + four ops action buttons (a confirm
  step on rerank, per the Jinja `data-confirm`), inline result banners reflecting
  `{triggered}`/`{enqueued}` and the human messages **ported verbatim from the
  Jinja `_ok_msgs`/`_err_msgs`** (`admin.html`:23-31; `_err_msgs` covers only
  `fetch` and `digest_unconfigured` → the `{triggered: false}` cases). With
  `retry: false`, a single failed sub-query 500s the whole read-model, so the
  page MUST render a **clean error state, not a blank shell**.
  **`AdminDrafting`** — read-only tables; per-workspace rows link to
  `/cockpit/:id` (now in-SPA). **`AdminGoldenSet`** — triples table (+ delete),
  manual add form, candidates table (+ per-row inline add with a score input).
- **Styling:** CSS Modules + `--app-*` tokens, no bare element selectors.
- **Refresh:** static fetch on mount, no polling (parity with the static Jinja
  render; the data changes slowly). An action invalidates its query.

### Teardown (this slice's branch)

- **Delete** `web/routes/admin.py` and `web/routes/golden_set.py` entirely (after
  porting the three context-builders into `web/api/admin.py`); drop both
  `include_router(...)` lines from `main.py`. Delete `admin.html`,
  `admin_drafting.html`, `golden_set.html`.
- **Manifest:** replace `/admin/users` with **`/admin`** in `frontend/routes.json`
  `spa_routes` — this is the functional change (bare `/admin` now SPA-owned,
  subsuming every `/admin/*`). Also drop `/admin` from `backend_prefixes`, but
  note that is **documentation-only**: `BACKEND_PREFIXES` is "never used to
  decide SPA ownership" (`spa_routes.py`:6-9), so the prefix removal is not
  load-bearing. Regen `schema.gen.ts`
  (`EMBEDDING_MODEL=bge-m3 bash scripts/gen_openapi_types.sh`) **after** the
  teardown (route deletions change the schema — the Slice-E lesson) and
  `routes.gen.ts` + `spa_routes.py` (`uv run python scripts/gen_routes.py`).
- **Tests:** migrate the 8 Jinja admin test files (`test_web_admin.py`,
  `test_web_admin_ranking.py`, `test_web_admin_digest_panel.py`,
  `test_web_admin_embedding_coverage.py`, `test_web_admin_drafting_panel.py`,
  `test_web_admin_levers.py`, `test_web_admin_calibration_banner.py`,
  `test_web_golden_set_crud.py`) to `/api`, preserving every behavior
  (coverage-mapping check before deleting them — the profiles re-embed lesson).
  Extend the existing `tests/test_admin_users_jinja_surface_absent.py` (Slice 1,
  same `app.routes` pattern) with the bare `/admin` + drafting + golden-set + ops
  paths gone and the new `/api/admin/*` present.
- **`test_web_auth` repoint — NOT a 1:1 move (2 retired + 1 repointed + 1 new).**
  The 3 tests Slice 1 left pointing at `/admin` (`test_web_auth.py`:119-153)
  actually exercise the `_redirect_html_401` exception handler (`main.py`:330-391),
  which branches on `api_request()` (path under `/api`):
  - `test_html_401_get_threads_next` (GET `/admin?x=1`, Accept text/html → 303
    `/login?next=…`) and `test_html_401_post_does_not_thread_next` (POST
    `/admin/rerank` → 303 `/login`) test the **HTML-redirect branch**
    (`main.py`:356-375). `/api/admin/dashboard` takes the envelope branch, never a
    303 — that behavior is not expressible against `/api`, so these are
    **deleted**. (`safe_next`'s sanitizer is independently covered by
    `test_auth_redirects.py`; no sanitizer coverage is lost.)
  - `test_json_401_unaffected_by_next` (GET `/admin` non-HTML → 401 JSON) is
    **repointed** to `GET /api/admin/dashboard` → 401 envelope and **renamed**
    (e.g. `test_api_admin_requires_auth`) — "unaffected_by_next" no longer means
    anything.
  - **New:** `GET /api/admin/dashboard` as a non-admin → `403`.
  - **Tracked dormant-code decision (the Slice-1 silent-drift lesson):** after
    this slice the `_redirect_html_401` non-`/api` HTML branches (the 401-redirect
    at 356-375 and the styled-error path) are live-looking but **unreachable** —
    every surviving non-`/api` route is either public (auth/register/oauth/health)
    or an SPA path served at 200; no protected non-`/api` HTML route can 401. Do
    NOT synthesize a throwaway protected route to keep testing it. Add a one-line
    comment marking the branch dormant ("no protected non-/api HTML route triggers
    this after admin Slice 2; remove in the auth slice") so it is not mistaken for
    working, tested code.

## Testing

- **Backend** (`tests/` root, psycopg3 fixtures): per-page read-model tests
  (dashboard shape incl. queue-count defaulting, empty/“never”-fetched sources,
  digest disabled rows, embedding-coverage non-blank filters, golden-set τ
  null-vs-present; drafting cap/per-workspace/recent-events; golden-set
  triples/candidates). Action tests: rerank enqueues for active profiles only;
  digest send triggered/unconfigured; reenable clears `digest_disabled_at` + 404;
  source fetch triggered + 404 unknown slug; golden-set add 201 + 400 bad score +
  400 FK + delete 204 idempotent; all `require_admin` → 403 for a non-admin.
  Route-absence guard. These migrate the 8 Jinja test files (coverage-mapping
  check before deleting them — the profiles re-embed lesson).
- **Frontend** (vitest + RTL): hook tests (read paths, each ops/CRUD mutation +
  invalidation), `AdminDashboard`/`AdminDrafting`/`AdminGoldenSet` render +
  action-wiring tests (POST assertions wrapped in `waitFor`), `AdminNav` links,
  the ActivityBar Admin entry visible only for admins, and the `router.test.tsx`
  set-equality update (now collapsing all `/admin/*` to `/admin`).
- **Gates:** `tsc --noEmit` 0, `pnpm build`, `gen_routes.py --check` in sync,
  `ruff check .` green (lint tests/ too — the Slice-1 lesson). Whole-suite
  testcontainers Postgres flakes under load → run targeted files.

## Global constraints

- No DB schema change. No new dependency. Strict parity port (exceptions: the
  golden-set FK→400 and the banner-UX JSON translation, both blessed above).
- Errors via `raise HTTPException` → global `/api` envelope; never hand-built.
- `OriginCSRFMiddleware` allows an absent Origin; TestClient mutation tests pass
  without an Origin header — do not add one.
- CSS Modules + `--app-*` tokens; no bare element selectors in `.module.css`.
- `tsconfig` `noUnusedLocals` + `include: ["src"]` — no unused imports anywhere
  under `src/`.
- Manifest is prefix-not-`:param`: `/admin` replaces `/admin/users` in
  `spa_routes`; the three new React routes + the manifest change land in one
  commit (`router.test.tsx` set-equality).

## Out of scope

The **auth sub-project** — login/register/status → React, then delete
`base.html`/Pico/`app.css`/`app.js` + the deferred dead workspace CSS/JS;
coordinate with the in-flight `feat/google-oidc-pkce` branch. It is the LAST
sub-project and the riskiest; it gets its own brainstorm and a checkpoint with
the user before starting. Kill-switch UI toggling stays a backlog item (not
added). After Slice 2 merges, the only Jinja remaining is the auth flow.
