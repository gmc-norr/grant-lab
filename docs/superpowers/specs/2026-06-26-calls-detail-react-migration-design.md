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
- If a valid active profile is given, load its `CallRanking` + `CallRankingCriterion` rows.
- Compute `best_profile_id` (highest-scoring profile owned by the user for this call — the same
  `aggregate_score.desc().nullslast()` query).
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
- Register `/calls/:id` as a new React route under the AppShell layout — **`routes.gen.ts` changes here**
  (a genuine new SPA route, unlike the cockpit bottom-tabs).

---

## 4. Scope & boundaries

- **One slice:** 1 detail read-model + 1 hide action + 1 React route + the dashboard link change + the
  helper relocation + the Jinja teardown. Comparable to cockpit Slice A.
- **Additive + one relocation**, then **per-slice teardown** (the established discipline): delete the
  Jinja `call_detail` + `hide_call` routes, `call_detail.html`, the now-empty `web/routes/calls.py`
  module, and its `main.py` import + `include_router`. Relocate the 3 "why" helpers FIRST so nothing
  kept imports the deleted module. The `_parse_optional_profile_id` parser is ported into the `/api`
  endpoint (the Jinja route that used it is deleted).
- **No DB schema change.** No new dependency.
- **Deferred / out of scope:** any redesign of the detail page (parity port only); an in-page profile
  switcher (the ranking shown follows the profile carried from the dashboard, as today); the other
  Jinja sub-projects (profile/account, admin, auth+final-teardown).

---

## 5. Data flow / error handling

- **Routing:** today the Jinja `/calls/{id}` route is registered and wins over the SPA catch-all. After
  teardown the route is gone, the SPA catch-all serves `/calls/:id`, and the new React route renders it.
  (Order matters: add the React route + `/api` first, delete the Jinja route last — so `/calls/:id`
  always resolves to *something* during the slice.)
- **Profile param:** `?profile=` empty/whitespace → treated as "no profile" (no ranking shown); a
  non-empty non-integer → 400 (parity with `_parse_optional_profile_id`). A profile that exists but is
  inactive or not the user's → no ranking (parity).
- **Missing call → 404** (both endpoints). **Hide is idempotent** (clicking twice is a no-op 200).
- **Error envelope:** handlers raise `HTTPException`; the global `/api` handler produces the standard
  envelope.

---

## 6. Verification gates

- `uv run python -c "import call_manager.main"` boots; after teardown the bare `/calls/{id}` Jinja route
  is gone (a route-table check: no non-`/api` route under `/calls/` except the SPA catch-all).
- Backend suite green: detail read-model (call fields; ranking present/absent; criteria + the "why"
  strings for budget/timeline/trl/embedding; best_profile_id; profile empty→none, bad→400; missing→404),
  hide (success + idempotent + 404). `ruff` clean. The relocated helpers produce identical output (port
  the existing `test_web_calls_start_drafting.py` assertions onto the `/api` read-model, then delete the
  Jinja test with the route).
- Frontend `tsc` clean, `vitest` green (CallDetail renders ranking/criteria/notes, banded hero,
  hide→navigate, CallRow links to `/calls/:id?profile=`), `build` clean.
- OpenAPI regen (`EMBEDDING_MODEL=bge-m3 bash scripts/gen_openapi_types.sh`): new `/api/calls/{id}` +
  hide paths added; the Jinja `/calls/{id}` HTML path dropped; **`routes.gen.ts` gains `/calls/:id`**
  (expected change — verify it's an addition, not a removal of an existing route).

---

## 7. Risks / notes

- **The "why" relocation is the one real-logic move.** Mitigation: byte-identical relocation + the ported
  criterion-detail tests pin the output; `ruff` catches an orphaned import after the module is deleted.
- **`routes.gen.ts` changes** (new `/calls/:id` route) — unlike every cockpit slice, this is expected.
  The gate asserts it's an addition; a removal would be a regression.
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
