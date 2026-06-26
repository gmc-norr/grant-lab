# Calls-Detail React Migration (Design Spec)

**Date:** 2026-06-26
**Status:** Design approved; pending spec-review gate → writing-plans.
**Project:** Full-React migration → retire the remaining Jinja pages, one surface at a time.
**Builds on:** the `workspace_main` Jinja teardown is complete (G2 merged `5378277`); the calls **list** (dashboard) is already React (SPA Phase 2).

---

## 1. Why this slice exists

The calls **list** (dashboard at `/`) is already a React SPA. The call **detail** page is still
Jinja: `GET /calls/{call_id}` → `call_detail.html`, served by `src/call_manager/web/routes/calls.py`,
plus its companion `POST /calls/{call_id}/hide`. The React dashboard's `CallRow` title link is a
plain `<a href="/calls/{id}">` that full-page-navigates to that Jinja page (the code comment says
"to the un-ported Jinja detail page"). There is no `/api/calls/{id}` yet — only the dashboard's
`GET /api/calls`. This slice migrates the detail page + Hide to React + `/api`, then deletes the
Jinja route/template/module.

This is the same strangler-fig pattern as the cockpit slices: **additive typed `/api` → React route
under the existing AppShell → per-slice Jinja teardown.** No DB schema change.

---

## 2. Goal & invariant

**Goal:** a user clicking a call in the React dashboard lands on a React `/calls/:id` detail page at
parity with the Jinja page (ranking hero, analyst notes, details grid, criterion breakdown, keywords,
summary, Start-drafting, Hide), driven by a typed `/api/calls/{id}` read-model.

**Invariant after this slice:**
1. `/calls/:id` is a React route under the AppShell; the dashboard navigates to it via React Router
   (SPA nav, no full reload), threading the dashboard's effective profile.
2. The detail data + the "why pass/fail" criterion strings come from `GET /api/calls/{id}` — the same
   query + the same `_criterion_detail`/`_money_range`/`_trl_range` logic as the Jinja route (relocated,
   byte-identical), and the same `web/filters.py` label functions (`criterion_label`, `model_label`,
   `note_category_heading`), computed server-side.
3. Hide goes through `POST /api/calls/{id}/hide` (idempotent), then the client returns to the dashboard
   with the call dropped from the list.
4. The Jinja `call_detail` + `hide_call` routes, `call_detail.html`, the `web/routes/calls.py` module,
   and its `main.py` registration are **deleted** in this slice (the module contains only these two
   routes). No DB schema change.

---

## 3. Architecture

### 3.1 Backend — additive `/api/calls/{id}` + hide; relocate the "why" helpers

**Helper relocation (prerequisite).** `_criterion_detail`, `_money_range`, `_trl_range` currently live
**inside the doomed `web/routes/calls.py`**. The new `/api` needs them, and importing from a module
this slice deletes would be wrong. **Relocate them to a neutral kept home**
(`src/call_manager/web/call_detail_view.py`) — behavior byte-identical — and have the `/api` endpoint
import them from there. (`web/filters.py` is already a kept shared module; the `/api` endpoint imports
the label functions from it directly.)

**`GET /api/calls/{call_id}?profile=`** (any authenticated user — the calls surface is not
workspace-scoped) — the detail read-model. Mirrors the Jinja `call_detail` query exactly:
- Load the `Call` (+ joined `source`); 404 if missing.
- Parse `?profile=` with the existing empty-string-is-None semantics (port `_parse_optional_profile_id`:
  empty/whitespace → None, non-empty non-int → 400).
- If a valid **active** profile is given, load its `CallRanking` + `CallRankingCriterion` rows.
  **Parity note (do NOT add an ownership check):** the Jinja route resolves `?profile=` by `is_active`
  **alone** — it does *not* require the profile be owned by the current user (`web/routes/calls.py:69-71`
  loads the ranking on `p.is_active` only; the `_login` helper in `tests/test_api_calls_feed_behavior.py`
  states this explicitly). Ownership (`created_by == user.id`) governs ONLY `best_profile_id` below, not
  whether a ranking is shown. Mirror that exactly — an active not-owned profile MUST still show its ranking.
- Compute `best_profile_id` (highest-scoring profile **owned by the user** for this call — the same
  `aggregate_score.desc().nullslast()` query; ownership applies here and only here).
- Build the response with server-computed labels + the "why" strings.

Response model `CallDetailOut`:
- `call`: `{id, title, source_name, programme, external_id, published_at, deadline, budget_min,
  budget_max, currency, url, keywords: list[str], summary}` (raw ISO dates — React formats them, as the
  dashboard already does).
- `ranking`: `RankingDetailOut | None` — `{aggregate_score, aggregate_reason_label (via criterion_label),
  model_label (via model_label), rationale, embed_similarity, escalated, escalated_from_score,
  analysis_model_label, summary, applicant_notes: list[{category, heading (via note_category_heading),
  text}]}`.
- `criteria`: `list[CriterionOut]` — each `{criterion, label (via criterion_label), rating,
  is_topic_fit (rating is /5), detail (the relocated _criterion_detail "why" string, or null),
  rationale, is_reason_code (criterion in {budget,timeline,trl,embedding}), quoted_evidence}`.
- `best_profile_id: int | None`.

**`POST /api/calls/{call_id}/hide`** (any authenticated user) → `{hidden: true}`. Reuses the existing
`pg_insert(UserCallDismissal).on_conflict_do_nothing` (idempotent); 404 if the call is missing. (The
Jinja version 303-redirects to `/`; the `/api` version returns JSON and the client navigates.)

Both endpoints follow the `web/api` session-factory + envelope conventions. Mounted on the existing
`/api` router (`web/api/calls.py`).

### 3.2 Frontend — `CallDetail` route + dashboard linkage

- `api/calls.ts` (extend the dashboard hook file): `useCallDetail(id, profileId)` (query keyed
  `["call", id, profileId]`, calls `GET /api/calls/{id}?profile=`), `useHideCall()` (mutation →
  `POST /api/calls/{id}/hide`, on success invalidate `["calls"]` and navigate to `/`).
- `routes/CallDetail.tsx`: renders the sections at Jinja parity — back-link, header (title, source ·
  programme · external_id, **Start drafting** → `/cockpit/new?call_id=&profile_id=` as today), the
  **score hero** (banded: `strong` ≥70 / `mid` >0, aggregate-reason & model badges, rationale, cosine,
  escalation), **Analyst notes** (summary + categorized applicant notes), **Details** grid (published,
  deadline, budget, external link), keyword chips, **Summary**, **Criterion breakdown** cards (rating,
  the `detail` "why" string, rationale/reason-code, quoted evidence), and **Hide this call**. Loading /
  404 states.
- `routes/CallRow.tsx`: change the title `<a href="/calls/{id}">` to a React-Router `<Link to={...}>`
  carrying `?profile=${profileId}` (so the ranking shows for the dashboard's effective profile).
- **Route ownership — two distinct layers (this is the load-bearing detail).**
  1. **React Router route** (inside the app's route tree): add `<Route path="/calls/:id" element={<CallDetail/>}>`
     under the AppShell layout. The `:id` param lives only here, in the React app — NOT in the manifest.
  2. **SPA ownership manifest** (`frontend/routes.json`): the matcher in `scripts/gen_routes.py` /
     `dev-proxy.ts` is an **exact-or-prefix-subpath** test (`path == r || path.startswith(r + "/")`) — it does
     **NOT** understand React `:param` patterns. So the manifest entry is the **prefix `/calls`** (the
     `/cockpit` precedent), never `/calls/:id`. Concretely: **add `"/calls"` to `spa_routes` AND remove
     `"/calls"` from `backend_prefixes`** (after teardown no `/calls` route is mounted in `main.py`, so the
     documentary backend-prefix entry is stale). Then regenerate (`uv run python scripts/gen_routes.py`),
     which rewrites **both** `frontend/src/routes.gen.ts` and `src/call_manager/web/spa_routes.py`:
     `SPA_ROUTES` gains `/calls`, `BACKEND_PREFIXES` drops it. `is_spa_route("/calls/123")` →
     `"/calls/123".startswith("/calls/")` → True, so the catch-all serves the SPA shell and React Router
     renders `CallDetail`.
  - **Pitfall:** putting `/calls/:id` in the manifest leaves `/calls/123` matching nothing → after the Jinja
    route is deleted it falls through to FastAPI's real 404. The `:param` form is a prod-breaker; use the prefix.

---

## 4. Scope & boundaries

- **One slice:** 1 detail read-model + 1 hide action + 1 React route + the dashboard link change + the
  helper relocation + the Jinja teardown. Comparable to cockpit Slice A.
- **Additive + one relocation**, then **per-slice teardown** (the established discipline): delete the
  Jinja `call_detail` + `hide_call` routes, `call_detail.html`, the now-empty `web/routes/calls.py`
  module, and its `main.py` import + `include_router`. Relocate the 3 "why" helpers FIRST so nothing
  kept imports the deleted module. The `_parse_optional_profile_id` parser is ported into the `/api`
  endpoint (the Jinja route that used it is deleted).
- **Test manifest — deleting `web/routes/calls.py` touches SIX test surfaces, not one.** Enumerate them so
  the teardown leaves the suite green:
  1. `tests/test_web_call_detail.py` — port its criterion-breakdown/ranking assertions onto the
     `GET /api/calls/{id}` read-model, then **delete** (the Jinja render it asserts is gone).
  2. `tests/test_web_calls_hide.py` — port redirect/idempotent/404/auth onto `POST /api/calls/{id}/hide`
     (now `{hidden: true}` + idempotent + 404 + 401, no 303), then **delete**.
  3. `tests/test_web_calls_start_drafting.py` — port the Start-drafting-link + criterion-detail assertions
     onto the `/api` read-model, then **delete**.
  4. `tests/test_web_auth.py` — `test_html_401_get_threads_next`, `test_html_401_post_does_not_thread_next`,
     and `test_json_401_unaffected_by_next` use `/calls/5` + `/calls/5/hide` only as *sample* protected
     routes. After teardown `/calls/5` is an SPA-owned public shell (no 401/redirect), so **repoint** them
     to a kept protected Jinja route — GET → `/me` (kept profile/account page); the POST test → a kept Jinja
     POST (the plan picks one, e.g. a profile-edit POST). The auth-middleware behavior under test is generic;
     only the sample path changes.
  5. `tests/test_gen_routes.py` — `is_spa_route("/calls/123")` flips **False→True**; `is_backend_route("/calls/123")`
     flips **True→False** (because `/calls` is removed from `backend_prefixes`). Update both. The
     explicit-`("/",)`-manifest tests (`test_root_owns_only_itself`) are unaffected — they pin the root
     special-case with their own manifest.
  6. `frontend/src/dev-proxy.test.ts` — the **shipped-manifest** assertions `isSpaRoute("/calls/1")` and
     `shouldServeViaVite("/calls/1")` flip **False→True**; update them + the "un-ported" comments. The
     **explicit-`["/"]`-manifest** branch test (`isSpaRoute("/calls/1", ["/"])`) stays False — it pins the
     root special-case independent of the shipped routes; do NOT touch it.
- **No DB schema change.** No new dependency.
- **Deferred / out of scope:** any redesign of the detail page (parity port only); an in-page profile
  switcher (the ranking shown follows the profile carried from the dashboard, as today); the other
  Jinja sub-projects (profile/account, admin, auth+final-teardown).

---

## 5. Data flow / error handling

- **Routing:** today the Jinja `/calls/{id}` route is registered and **wins over the SPA catch-all** (a real
  registered route always beats the fallback; `is_spa_route` only governs the catch-all). So adding `/calls`
  to the manifest while the Jinja route still exists is inert — the real route keeps winning. After teardown
  the Jinja route is gone, the catch-all + manifest take over, and the React route renders. **Order:** add the
  React route + `/api` FIRST (so `/calls/:id` resolves to *something* throughout); then in the teardown step
  do the manifest flip (`+/calls` in `spa_routes`, `−/calls` in `backend_prefixes`, regen) **together with**
  the Jinja-route deletion — deleting the route WITHOUT the manifest flip would 404 `/calls/123`.
- **Profile param:** `?profile=` empty/whitespace → treated as "no profile" (no ranking shown); a
  non-empty non-integer → 400 (parity with `_parse_optional_profile_id`). A profile that exists but is
  **inactive → no ranking**. A profile that is **active but not owned by the user → ranking IS shown**
  (parity — see §3.1; ownership gates only `best_profile_id`, never ranking display).
- **Missing call → 404** (both endpoints). **Hide is idempotent** (clicking twice is a no-op 200).
- **Error envelope:** handlers raise `HTTPException`; the global `/api` handler produces the standard
  envelope.

---

## 6. Verification gates

- `uv run python -c "import call_manager.main"` boots; after teardown the bare `/calls/{id}` Jinja route
  is gone (a route-table check: no non-`/api` route under `/calls/` except the SPA catch-all).
- Backend suite green: detail read-model (call fields; ranking present/absent; criteria + the "why"
  strings for budget/timeline/trl/embedding; best_profile_id; profile empty→none, bad→400; missing→404;
  **active-but-not-owned profile → ranking IS returned** — the P1b parity test), hide (success +
  idempotent + 404 + 401). `ruff` clean. The relocated helpers produce identical output (port the
  `test_web_call_detail.py` / `test_web_calls_hide.py` / `test_web_calls_start_drafting.py` assertions onto
  the `/api` read-model + hide, then delete the three Jinja tests with the route). The repointed
  `test_web_auth.py` 401/redirect tests stay green against their new kept-Jinja sample paths.
- **Route-ownership gates:** `tests/test_gen_routes.py` updated — `is_spa_route("/calls/123")` is now True,
  `is_backend_route("/calls/123")` is now False; `frontend/src/dev-proxy.test.ts` updated —
  `isSpaRoute("/calls/1")` and `shouldServeViaVite("/calls/1")` now True (explicit-manifest branch tests
  untouched). A route-table assertion confirms no non-`/api` backend route remains under `/calls/` — which
  is *why* the repointed `test_web_auth.py` cases move off `/calls/5` (it no longer reaches the protected
  Jinja handler that produced the 303/401; the test-app `SPA_MODE` makes shell-vs-404 an implementation
  detail, so the gate is route absence, not a specific status).
- Frontend `tsc` clean, `vitest` green (CallDetail renders ranking/criteria/notes, banded hero,
  hide→navigate, CallRow links to `/calls/:id?profile=`), `build` clean.
- OpenAPI / manifest regen: `EMBEDDING_MODEL=bge-m3 bash scripts/gen_openapi_types.sh` adds the
  `/api/calls/{id}` + hide paths and drops the Jinja `/calls/{id}` HTML path; `uv run python
  scripts/gen_routes.py` rewrites `routes.gen.ts` + `spa_routes.py` so **`SPA_ROUTES` gains the prefix
  `/calls`** (NOT `/calls/:id`) and **`BACKEND_PREFIXES` drops `/calls`**. Verify the SPA_ROUTES change is
  an addition of the `/calls` prefix, and that no `:param` string leaked into the manifest.

---

## 7. Risks / notes

- **The "why" relocation is the one real-logic move.** Mitigation: byte-identical relocation + the ported
  criterion-detail tests pin the output; `ruff` catches an orphaned import after the module is deleted.
- **The manifest changes** — unlike every cockpit slice, `routes.gen.ts` + `spa_routes.py` are regenerated
  here (`+/calls` in `SPA_ROUTES`, `−/calls` in `BACKEND_PREFIXES`). This is the slice's single
  highest-risk edit: the matcher is prefix-not-`:param`, so the manifest entry MUST be `/calls` and the
  React `:id` lives only in the app's route tree (§3.2). The route-ownership gates (`test_gen_routes.py`,
  `dev-proxy.test.ts`) pin the flip; a `/calls/:id` string in the manifest, or a forgotten `backend_prefixes`
  removal, is the regression to watch for.
- **Two date formats:** the read-model returns raw ISO; React formats (reusing the dashboard's
  `Deadline`/date helpers) — no server-side `display_dt`/`display_date` needed, keeping parity with the
  already-React list.
- **`web/filters.py` stays** (shared by the still-Jinja profile/admin pages); the `/api` endpoint imports
  its label functions — acceptable coupling to a kept module.

---

## 8. Out of scope

- Detail-page redesign; in-page profile switcher.
- The profile/account, admin, and auth+final-teardown sub-projects.
- Deleting `web/filters.py` or `base.html`/Pico/`app.css` (final auth+teardown).
